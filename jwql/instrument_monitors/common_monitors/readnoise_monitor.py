#! /usr/bin/env python

"""This module contains code for the readnoise monitor, which monitors
the readnoise levels in dark exposures as well as the accuracy of
the pipeline readnoise reference files over time.

For each instrument, the readnoise, technically the "CDS noise", is found
by calculating the standard deviation through a stack of consecutive
frame differences in each dark exposure. The sigma-clipped mean and
standard deviation in each of these readnoise images is recorded in the
``<Instrument>ReadnoiseStats`` database table.

Next, each of these readnoise images are differenced with the current
pipeline readnoise reference file to identify the need for new reference
files. A histogram distribution of these difference images, as well as
the sigma-clipped mean and standard deviation,are recorded in the
``<Instrument>ReadnoiseStats`` database table. A png version of these
difference images is also saved for visual inspection.

Author
------
    - Ben Sunnquist

Use
---
    This module can be used from the command line as such:

    ::

        python readnoise_monitor.py
"""

import datetime
import logging
import os
import shutil

from astropy.io import fits
from astropy.stats import sigma_clip, sigma_clipped_stats
from astropy.time import Time
from astropy.visualization import ZScaleInterval
import crds
from jwst.dq_init import DQInitStep
from jwst.group_scale import GroupScaleStep
from jwst.refpix import RefPixStep
from jwst.superbias import SuperBiasStep
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
import numpy as np
from pysiaf import Siaf
from sqlalchemy import func
from sqlalchemy.sql.expression import and_

from jwql.database.database_interface import session
#from jwql.database.database_interface import NIRCamReadnoiseQueryHistory, NIRCamReadnoiseStats
from jwql.instrument_monitors import pipeline_tools
from jwql.instrument_monitors.common_monitors.dark_monitor import mast_query_darks
from jwql.utils import instrument_properties
from jwql.utils.constants import JWST_INSTRUMENT_NAMES_MIXEDCASE
from jwql.utils.logging_functions import log_info, log_fail
from jwql.utils.permissions import set_permissions
from jwql.utils.utils import ensure_dir_exists, filesystem_path, get_config, initialize_instrument_monitor, update_monitor_table

class Readnoise():
    """Class for executing the readnoise monitor.

    This class will search for new dark current files in the file
    system for each instrument and will run the monitor on these
    files. The monitor will create a readnoise image for each of the
    new dark files. It will then perform statistical measurements
    on these readnoise images, as well as their differences with the
    current pipeline readnoise reference file, in order to monitor
    the readnoise levels over time as well as ensure the pipeline
    readnoise reference file is sufficiently capturing the current
    readnoise behavior. Results are all saved to database tables.

    Attributes
    ----------
    output_dir : str
        Path into which outputs will be placed

    data_dir : str
        Path into which new dark files will be copied to be worked on

    query_start : float
        MJD start date to use for querying MAST

    query_end : float
        MJD end date to use for querying MAST

    instrument : str
        Name of instrument used to collect the dark current data

    aperture : str
        Name of the aperture used for the dark current (e.g.
        ``NRCA1_FULL``)
    """

    def __init__(self):
        """Initialize an instance of the ``Readnoise`` class."""

    def file_exists_in_database(self, filename):
        """Checks if an entry for filename exists in the readnoise stats
        database.

        Parameters
        ----------
        filename : str
            The full path to the uncal filename

        Returns
        -------
        file_exists : bool
            ``True`` if filename exists in the readnoise stats database
        """

        query = session.query(self.stats_table)
        results = query.filter(self.stats_table.uncal_filename == filename).all()

        if len(results) != 0:
            file_exists = True
        else:
            file_exists = False

        return file_exists

    def get_amp_means(self, image, amps):
        """Calculates the sigma-clipped mean in the input image for each
        amplifier.

        Parameters
        ----------
        image : numpy.ndarray
            2D array on which to calculate statistics

        amps : dict
            Dictionary containing amp boundary coordinates (output from
            ``amplifier_info`` function)
            ``amps[key] = [(xmin, xmax, xstep), (ymin, ymax, ystep)]``

        Returns
        -------
        amp_means : dict
            Contains the mean values for each amp.
        """

        amp_means = {}

        for key in amps:
            x_start, x_end, x_step = amps[key][0]
            y_start, y_end, y_step = amps[key][1]

            # Find sigma-clipped mean value for this amp
            amp_data = image[y_start: y_end: y_step, x_start: x_end: x_step]
            clipped = sigma_clip(amp_data, sigma=3.0, maxiters=5)
            amp_means['amp{}_mean'.format(key)] = np.nanmean(clipped)

        return amp_means

    def get_metadata(self, filename):
        """Collect basic metadata from a fits file.
        
        Parameters
        ----------
        filename : str
            Name of fits file to examine
        """

        header = fits.getheader(filename)

        try:
            self.detector = header['DETECTOR']
            self.read_pattern = header['READPATT']
            self.subarray = header['SUBARRAY']
            self.nints = header['NINTS']
            self.ngroups = header['NGROUPS']
            self.date_obs = header['DATE-OBS']
            self.time_obs = header['TIME-OBS']
            self.expstart = '{}T{}'.format(self.date_obs, self.time_obs)
        except KeyError as e:
            logging.error(e)

    def identify_tables(self):
        """Determine which database tables to use for a run of the readnoise
        monitor.
        """

        mixed_case_name = JWST_INSTRUMENT_NAMES_MIXEDCASE[self.instrument]
        self.query_table = eval('{}ReadnoiseQueryHistory'.format(mixed_case_name))
        self.stats_table = eval('{}ReadnoiseStats'.format(mixed_case_name))

    def image_to_png(self, image, outname):
        """Ouputs an image array into a png file.

        Parameters
        ----------
        image : numpy.ndarray
            2D image array

        outname : str
            The name given to the output png file

        Returns
        -------
        output_filename : str
            The full path to the output png file
        """

        output_filename = os.path.join(self.data_dir, '{}.png'.format(outname))

        # Get image scale limits
        z = ZScaleInterval()
        vmin, vmax = z.get_limits(image)

        # Plot the image
        plt.figure(figsize=(12,12))
        ax = plt.gca()
        im = ax.imshow(image, cmap='gray', origin='lower', vmin=vmin, vmax=vmax)
        ax.set_title('{}'.format(outname))

        # Make the colorbar
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("right", size="5%", pad=0.4)
        cbar = plt.colorbar(im, cax=cax)
        cbar.set_label('Readnoise Difference (current - reffile) [DN]')

        # Save the figure
        plt.savefig(output_filename, bbox_inches='tight', dpi=200, overwrite=True)
        set_permissions(output_filename)
        logging.info('\t{} created'.format(output_filename))

        return output_filename

    def make_crds_parameter_dict(self):
        """Construct a paramter dictionary to be used for querying CRDS
        for the current reffiles in use by the JWST pipeline.

        Returns
        -------
        parameters : dict
            Dictionary of parameters, in the format expected by CRDS
        """

        parameters = {}
        parameters['INSTRUME'] = self.instrument.upper()
        parameters['DETECTOR'] = self.detector.upper()
        parameters['READPATT'] = self.read_pattern.upper()
        parameters['SUBARRAY'] = self.subarray.upper()
        parameters['DATE-OBS'] = datetime.date.today().isoformat()
        current_date = datetime.datetime.now()
        parameters['TIME-OBS'] = current_date.time().isoformat()

        return parameters

    def make_readnoise_image(self, data):
        """Calculates the readnoise for the given input dark current ramp.

        Parameters
        ----------
        data : numpy.ndarray
            The input ramp data. The data shape is assumed to be a 4D array in
            DMS format (integration, group, y, x).

        Returns
        -------
        readnoise : numpy.ndarray
            The 2D readnoise image.
        """

        # Create a stack of CDS images (group difference images) using the input ramp data, 
        # combining multiple integrations if necessary.
        logging.info('\tCreating stack of CDS frames')
        n_ints, n_groups, n_y, n_x = data.shape
        for integration in range(n_ints):
            cds = data[integration, 1::2, :, :] - data[integration, ::2, :, :]
            if integration == 0:
                cds_stack = cds
            else:
                cds_stack = np.concatenate((cds_stack, cds), axis=0)

        # Calculate the readnoise by taking the clipped stddev through the CDS stack
        logging.info('\tCreating readnoise image')
        clipped = sigma_clip(cds_stack, sigma=3.0, maxiters=3, axis=0)
        readnoise = np.std(clipped, axis=0)
        readnoise = readnoise.filled(fill_value=np.nan)  # converts masked array to normal array and fills missing data
        
        return readnoise

    def most_recent_search(self):
        """Query the query history database and return the information
        on the most recent query for the given ``aperture_name`` where
        the readnoise monitor was executed.

        Returns
        -------
        query_result : float
            Date (in MJD) of the ending range of the previous MAST query
            where the readnoise monitor was run.
        """

        sub_query = session.query(
            self.query_table.aperture,
            func.max(self.query_table.end_time_mjd).label('maxdate')
            ).group_by(self.query_table.aperture).subquery('t2')

        # Note that "self.query_table.run_monitor == True" below is
        # intentional. Switching = to "is" results in an error in the query.
        query = session.query(self.query_table).join(
            sub_query,
            and_(
                self.query_table.aperture == self.aperture,
                self.query_table.end_time_mjd == sub_query.c.maxdate,
                self.query_table.run_monitor == True
            )
        ).all()

        query_count = len(query)
        if query_count == 0:
            query_result = 57357.0  # a.k.a. Dec 1, 2015 == CV3
            logging.info(('\tNo query history for {}. Beginning search date will be set to {}.'.format(self.aperture, query_result)))
        elif query_count > 1:
            raise ValueError('More than one "most recent" query?')
        else:
            query_result = query[0].end_time_mjd

        return query_result

    def process(self, file_list):
        """The main method for processing darks.  See module docstrings
        for further details.

        Parameters
        ----------
        file_list : list
            List of filenames (including full paths) to the dark current files
        """

        for filename in file_list:
            logging.info('\tWorking on file: {}'.format(filename))

            # Get relevant header information for this file
            self.get_metadata(filename)

            # Determine if the file needs group_scale in pipeline run
            if self.read_pattern not in pipeline_tools.GROUPSCALE_READOUT_PATTERNS:
                group_scale = False
            else:
                group_scale = True

            # Run the file through the pipeline up through the refpix step
            logging.info('\tRunning pipeline on {}'.format(filename))
            processed_file = self.run_early_pipeline(filename, group_scale=group_scale)
            logging.info('\tPipeline complete. Output: {}'.format(processed_file))

            # Find amplifier boundaries so per-amp statistics can be calculated
            _, amp_bounds = instrument_properties.amplifier_info(processed_file, omit_reference_pixels=True)
            logging.info('\tAmplifier boundaries: {}'.format(amp_bounds))

            # Get the ramp data; remove first 5 groups and last group for MIRI to avoid reset/rscd effects
            cal_data = fits.getdata(processed_file, 'SCI', uint=False)
            if self.instrument == 'MIRI':
                cal_data = cal_data[:, 5:-1, :, :]

            # Make the readnoise image
            readnoise_outfile = os.path.join(self.data_dir, os.path.basename(processed_file.replace('_ramp.fits', '_readnoise.fits')))
            readnoise = self.make_readnoise_image(cal_data)
            fits.writeto(readnoise_outfile, readnoise, overwrite=True)
            logging.info('\tReadnoise image saved to {}'.format(readnoise_outfile))

            # Calculate the sigma-clipped mean readnoise value in each amp
            amp_means = self.get_amp_means(readnoise, amp_bounds)
            logging.info('\tReadnoise image stats: {}'.format(amp_means))

            # Get the current JWST Readnoise Reference File data
            parameters = self.make_crds_parameter_dict()
            reffile_mapping = crds.getreferences(parameters, reftypes=['readnoise'])
            readnoise_file = reffile_mapping['readnoise']
            if 'NOT FOUND' in readnoise_file:
                logging.warning('\tNo pipeline readnoise reffile match for this file - assuming all zeros.')
                pipeline_readnoise = np.zeros(readnoise.shape)
            else:
                logging.info('\tPipeline readnoise reffile is {}'.format(readnoise_file))
                pipeline_readnoise = fits.getdata(readnoise_file)

            # Find the difference between the current readnoise image and the pipeline readnoise reffile, and record image stats
            readnoise_diff = readnoise - pipeline_readnoise
            mean_diff, median_diff, stddev_diff = sigma_clipped_stats(readnoise_diff, sigma=3.0, maxiters=5)
            logging.info('\tReadnoise difference image stats: {:.5f} +/- {:.5f}'.format(mean_diff, stddev_diff))

            # Save a png of the readnoise difference image for visual inspection
            logging.info('\tCreating png of readnoise difference image')
            readnoise_diff_png = self.image_to_png(readnoise_diff, outname=os.path.basename(readnoise_outfile).replace('.fits', '_diff'))

            # # Construct new entry for this file for the readnoise database table.
            # # Can't insert values with numpy.float32 datatypes into database
            # # so need to change the datatypes of these values.
            # readnoise_db_entry = {'aperture': self.aperture,
            #                       'detector': self.detector,
            #                       'subarray': self.subarray,
            #                       'read_pattern': self.read_pattern,
            #                       'nints': self.nints,
            #                       'ngroups': self.ngroups,
            #                       'uncal_filename': filename,
            #                       'readnoise_filename': readnoise_outfile,
            #                       'readnoise_diff_image': readnoise_diff_png,
            #                       'expstart': self.expstart,
            #                       'mean_diff': float(mean_diff),
            #                       'median_diff': float(median_diff),
            #                       'stddev_diff': float(stddev_diff),
            #                       'entry_date': datetime.datetime.now()
            #                      }
            # for key in amp_means.keys():
            #     readnoise_db_entry[key] = float(amp_means[key])

            # # Add this new entry to the readnoise database table
            # #self.stats_table.__table__.insert().execute(readnoise_db_entry)
            # logging.info('\tNew entry added to readnoise database table: {}'.format(readnoise_db_entry))

            # # Remove the raw and calibrated files to save memory space
            # os.remove(filename)
            # os.remove(processed_file)

    @log_fail
    @log_info
    def run(self):
        """The main method.  See module docstrings for further details."""

        logging.info('Begin logging for readnoise_monitor')

        # Get the output directory and setup a directory to store the data
        self.output_dir = os.path.join(get_config()['outputs'], 'readnoise_monitor')
        ensure_dir_exists(os.path.join(self.output_dir, 'data'))

        # Use the current time as the end time for MAST query
        self.query_end = Time.now().mjd

        # Loop over all instruments
        for instrument in ['nircam']:
            self.instrument = instrument

        #     # Identify which database tables to use
        #     self.identify_tables()

            # Get a list of all possible apertures for this instrument
            siaf = Siaf(self.instrument)
            possible_apertures = list(siaf.apertures)

            for aperture in possible_apertures[12:14]:  # TODO remove index range

                logging.info('Working on aperture {} in {}'.format(aperture, instrument))
                self.aperture = aperture

        #         # Locate the record of the most recent MAST search; use this time
        #         # (plus a 30 day buffer to catch any missing files from the previous
        #         # run) as the start time in the new MAST search.
        #         most_recent_search = self.most_recent_search()
        #         self.query_start = most_recent_search - 30
                self.query_start = 57357.0  # a.k.a. Dec 1, 2015 == CV3  # TODO remove this and uncomment above

                # Query MAST for new dark files for this instrument/aperture
                logging.info('\tQuery times: {} {}'.format(self.query_start, self.query_end))
                new_entries = mast_query_darks(instrument, aperture, self.query_start, self.query_end)
                logging.info('\tAperture: {}, new entries: {}'.format(self.aperture, len(new_entries)))

                # Set up a directory to store the data for this aperture
                self.data_dir = os.path.join(self.output_dir, 'data/{}_{}'.format(self.instrument.lower(), self.aperture.lower()))
                if len(new_entries) > 0:
                    ensure_dir_exists(self.data_dir)

                # Get any new files to process
                new_files = []
                for file_entry in new_entries[0:1]:  # TODO remove index range
                    output_filename = os.path.join(self.data_dir, file_entry['filename'].replace('_dark', '_uncal'))
                    
                    # # Dont process files that already exist in the readnoise stats database
                    # file_exists = self.file_exists_in_database(output_filename)
                    # if file_exists:
                    #     logging.info('\t{} already exists in the readnoise database table.'.format(output_filename))
                    #     continue

                    # Save any new uncal files in the output directory; some dont exist in JWQL filesystem.
                    try:
                        filename = filesystem_path(file_entry['filename'])
                        uncal_filename = filename.replace('_dark', '_uncal')
                        if not os.path.isfile(uncal_filename):
                            logging.info('\t{} does not exist in JWQL filesystem, even though {} does'.format(uncal_filename, filename))
                        else:
                            n_groups = fits.getheader(uncal_filename)['NGROUPS']
                            if n_groups > 1:  # Skip processing if the file doesnt have enough groups to calculate the readnoise TODO change to 10 after testing so MIRI is also oK
                                shutil.copy(uncal_filename, self.data_dir)
                                logging.info('\tCopied {} to {}'.format(uncal_filename, output_filename))
                                set_permissions(output_filename)
                                new_files.append(output_filename)
                            else:
                                logging.info('\tNot enough groups to calculate readnoise in {}'.format(uncal_filename))
                    except FileNotFoundError:
                        logging.info('\t{} does not exist in JWQL filesystem'.format(file_entry['filename']))

                # Run the readnoise monitor on any new files
                if len(new_files) > 0:
                    self.process(new_files)
                    monitor_run = True
                else:
                    logging.info('\tReadnoise monitor skipped. {} new dark files for {}, {}.'.format(len(new_files), instrument, aperture))
                    monitor_run = False

        #         # Update the query history
        #         new_entry = {'instrument': instrument,
        #                      'aperture': aperture,
        #                      'start_time_mjd': self.query_start,
        #                      'end_time_mjd': self.query_end,
        #                      'entries_found': len(new_entries),
        #                      'files_found': len(new_files),
        #                      'run_monitor': monitor_run,
        #                      'entry_date': datetime.datetime.now()}
        #         #self.query_table.__table__.insert().execute(new_entry)
        #         logging.info('\tUpdated the query history table')

        logging.info('Readnoise Monitor completed successfully.')

    def run_early_pipeline(self, filename, group_scale=False):
        """Runs the early steps of the jwst pipeline on uncalibrated files
        and outputs the result.

        Parameters
        ----------
        filename : str
            File on which to run the pipeline steps

        group_scale : bool
            Option to rescale pixel values to correct for instances where
            on-board frame averaging did not result in the proper values

        Returns
        -------
        output_filename : str
            The full path to the calibrated file
        """

        output_filename = filename.replace('_uncal', '').replace('.fits', '_ramp.fits')

        # Run the group_scale and dq_init steps on the input file
        if group_scale:
            model = GroupScaleStep.call(filename)
            model = DQInitStep.call(model)
        else:
            model = DQInitStep.call(filename)

        # Run the superbias step for NIRCam
        if self.instrument.upper() == 'NIRCAM':
            model = SuperBiasStep.call(model)

        # Run the refpix step and save the output
        model = RefPixStep.call(model)
        model.save(output_filename)
        set_permissions(output_filename)

        return output_filename


if __name__ == '__main__':

    module = os.path.basename(__file__).strip('.py')
    start_time, log_file = initialize_instrument_monitor(module)

    monitor = Readnoise()
    monitor.run()

    #update_monitor_table(module, start_time, log_file)
