#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2017-2019 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""SEVIRI native format reader.

References:
    MSG Level 1.5 Native Format File Definition
    https://www.eumetsat.int/website/wcm/idc/idcplg?IdcService=GET_FILE&dDocName=PDF_FG15_MSG-NATIVE-FORMAT-15&RevisionSelectionMethod=LatestReleased&Rendition=Web
    MSG Level 1.5 Image Data Format Description
    https://www.eumetsat.int/website/wcm/idc/idcplg?IdcService=GET_FILE&dDocName=PDF_TEN_05105_MSG_IMG_DATA&RevisionSelectionMethod=LatestReleased&Rendition=Web

"""

import logging
from datetime import datetime
import numpy as np

import xarray as xr
import dask.array as da

from satpy import CHUNK_SIZE

from pyresample import geometry

from satpy.readers.file_handlers import BaseFileHandler
from satpy.readers.eum_base import recarray2dict
from satpy.readers.seviri_base import (SEVIRICalibrationHandler,
                                       CHANNEL_NAMES, CALIB, SATNUM,
                                       dec10216, VISIR_NUM_COLUMNS,
                                       VISIR_NUM_LINES, HRV_NUM_COLUMNS,
                                       VIS_CHANNELS)
from satpy.readers.seviri_l1b_native_hdr import (GSDTRecords, native_header,
                                                 native_trailer)
from satpy.readers._geos_area import get_area_definition

logger = logging.getLogger('native_msg')


class NativeMSGFileHandler(BaseFileHandler, SEVIRICalibrationHandler):
    """SEVIRI native format reader.

    The Level1.5 Image data calibration method can be changed by adding the
    required mode to the Scene object instantiation  kwargs eg
    kwargs = {"calib_mode": "gsics",}
    """

    def __init__(self, filename, filename_info, filetype_info, calib_mode='nominal'):
        """Initialize the reader."""
        super(NativeMSGFileHandler, self).__init__(filename,
                                                   filename_info,
                                                   filetype_info)
        self.platform_name = None
        self.calib_mode = calib_mode

        # Declare required variables.
        # Assume a full disk file, reset in _read_header if otherwise.
        self.header = {}
        self.mda = {}
        self.mda['is_full_disk'] = True
        self.trailer = {}

        # Read header, prepare dask-array, read trailer
        # Available channels are known only after the header has been read
        self._read_header()
        self.dask_array = da.from_array(self._get_memmap(), chunks=(CHUNK_SIZE,))
        self._read_trailer()

    @property
    def start_time(self):
        """Read the repeat cycle start time from metadata."""
        return self.header['15_DATA_HEADER']['ImageAcquisition'][
            'PlannedAcquisitionTime']['TrueRepeatCycleStart']

    @property
    def end_time(self):
        """Read the repeat cycle end time from metadata."""
        return self.header['15_DATA_HEADER']['ImageAcquisition'][
            'PlannedAcquisitionTime']['PlannedRepeatCycleEnd']

    @staticmethod
    def _calculate_area_extent(center_point, north, east, south, west,
                               we_offset, ns_offset, column_step, line_step):
        # For Earth model 2 and full disk VISIR, (center_point - west - 0.5 + we_offset) must be -1856.5 .
        # See MSG Level 1.5 Image Data Format Description Figure 7 - Alignment and numbering of the non-HRV pixels.

        ll_c = (center_point - east + 0.5 + we_offset) * column_step
        ll_l = (north - center_point + 0.5 + ns_offset) * line_step
        ur_c = (center_point - west - 0.5 + we_offset) * column_step
        ur_l = (south - center_point - 0.5 + ns_offset) * line_step

        return (ll_c, ll_l, ur_c, ur_l)

    def _get_data_dtype(self):
        """Get the dtype of the file based on the actual available channels."""
        pkhrec = [
            ('GP_PK_HEADER', GSDTRecords.gp_pk_header),
            ('GP_PK_SH1', GSDTRecords.gp_pk_sh1)
        ]
        pk_head_dtype = np.dtype(pkhrec)

        def get_lrec(cols):
            lrec = [
                ("gp_pk", pk_head_dtype),
                ("version", np.uint8),
                ("satid", np.uint16),
                ("time", (np.uint16, 5)),
                ("lineno", np.uint32),
                ("chan_id", np.uint8),
                ("acq_time", (np.uint16, 3)),
                ("line_validity", np.uint8),
                ("line_rquality", np.uint8),
                ("line_gquality", np.uint8),
                ("line_data", (np.uint8, cols))
            ]

            return lrec

        # each pixel is 10-bits -> one line of data has 25% more bytes
        # than the number of columns suggest (10/8 = 1.25)
        visir_rec = get_lrec(int(self.mda['number_of_columns'] * 1.25))
        number_of_visir_channels = len(
            [s for s in self.mda['channel_list'] if not s == 'HRV'])
        drec = [('visir', (visir_rec, number_of_visir_channels))]

        if self.mda['available_channels']['HRV']:
            hrv_rec = get_lrec(int(self.mda['hrv_number_of_columns'] * 1.25))
            drec.append(('hrv', (hrv_rec, 3)))

        return np.dtype(drec)

    def _get_memmap(self):
        """Get the memory map for the SEVIRI data."""
        with open(self.filename) as fp:
            data_dtype = self._get_data_dtype()
            hdr_size = native_header.itemsize

            return np.memmap(fp, dtype=data_dtype,
                             shape=(self.mda['number_of_lines'],),
                             offset=hdr_size, mode="r")

    def _read_header(self):
        """Read the header info."""
        data = np.fromfile(self.filename,
                           dtype=native_header, count=1)

        self.header.update(recarray2dict(data))

        data15hd = self.header['15_DATA_HEADER']
        sec15hd = self.header['15_SECONDARY_PRODUCT_HEADER']

        # Set the list of available channels:
        self.mda['available_channels'] = get_available_channels(self.header)
        self.mda['channel_list'] = [i for i in CHANNEL_NAMES.values()
                                    if self.mda['available_channels'][i]]

        self.platform_id = data15hd[
            'SatelliteStatus']['SatelliteDefinition']['SatelliteId']
        self.mda['platform_name'] = "Meteosat-" + SATNUM[self.platform_id]

        equator_radius = data15hd['GeometricProcessing'][
                             'EarthModel']['EquatorialRadius'] * 1000.
        north_polar_radius = data15hd[
                                 'GeometricProcessing']['EarthModel']['NorthPolarRadius'] * 1000.
        south_polar_radius = data15hd[
                                 'GeometricProcessing']['EarthModel']['SouthPolarRadius'] * 1000.
        polar_radius = (north_polar_radius + south_polar_radius) * 0.5
        ssp_lon = data15hd['ImageDescription'][
            'ProjectionDescription']['LongitudeOfSSP']

        self.mda['projection_parameters'] = {'a': equator_radius,
                                             'b': polar_radius,
                                             'h': 35785831.00,
                                             'ssp_longitude': ssp_lon}

        north = int(sec15hd['NorthLineSelectedRectangle']['Value'])
        east = int(sec15hd['EastColumnSelectedRectangle']['Value'])
        south = int(sec15hd['SouthLineSelectedRectangle']['Value'])
        west = int(sec15hd['WestColumnSelectedRectangle']['Value'])

        ncolumns = west - east + 1
        nrows = north - south + 1

        # check if the file has less rows or columns than
        # the maximum, if so it is an area of interest file
        if (nrows < VISIR_NUM_LINES) or (ncolumns < VISIR_NUM_COLUMNS):
            self.mda['is_full_disk'] = False

        # If the number of columns in the file is not divisible by 4,
        # UMARF will add extra columns to the file
        modulo = ncolumns % 4
        padding = 0
        if modulo > 0:
            padding = 4 - modulo
        cols_visir = ncolumns + padding

        # Check the VISIR calculated column dimension against
        # the header information
        cols_visir_hdr = int(sec15hd['NumberColumnsVISIR']['Value'])
        if cols_visir_hdr != cols_visir:
            logger.warning(
                "Number of VISIR columns from the header is incorrect!")
            logger.warning("Header: %d", cols_visir_hdr)
            logger.warning("Calculated: = %d", cols_visir)

        # HRV Channel - check if the area is reduced in east west
        # direction as this affects the number of columns in the file
        cols_hrv_hdr = int(sec15hd['NumberColumnsHRV']['Value'])
        if ncolumns < VISIR_NUM_COLUMNS:
            cols_hrv = cols_hrv_hdr
        else:
            cols_hrv = int(cols_hrv_hdr / 2)

        # self.mda represents the 16bit dimensions not 10bit
        self.mda['number_of_lines'] = int(sec15hd['NumberLinesVISIR']['Value'])
        self.mda['number_of_columns'] = cols_visir
        self.mda['hrv_number_of_lines'] = int(sec15hd["NumberLinesHRV"]['Value'])
        self.mda['hrv_number_of_columns'] = cols_hrv

    def _read_trailer(self):

        hdr_size = native_header.itemsize
        data_size = (self._get_data_dtype().itemsize *
                     self.mda['number_of_lines'])

        with open(self.filename) as fp:
            fp.seek(hdr_size + data_size)
            data = np.fromfile(fp, dtype=native_trailer, count=1)

        self.trailer.update(recarray2dict(data))

    def get_area_def(self, dataset_id):
        """Get the area definition of the band."""
        pdict = {}
        pdict['a'] = self.mda['projection_parameters']['a']
        pdict['b'] = self.mda['projection_parameters']['b']
        pdict['h'] = self.mda['projection_parameters']['h']
        pdict['ssp_lon'] = self.mda['projection_parameters']['ssp_longitude']

        if dataset_id['name'] == 'HRV':
            pdict['nlines'] = self.mda['hrv_number_of_lines']
            pdict['ncols'] = self.mda['hrv_number_of_columns']
            pdict['a_name'] = 'geos_seviri_hrv'
            pdict['a_desc'] = 'SEVIRI high resolution channel area'
            pdict['p_id'] = 'seviri_hrv'

            if self.mda['is_full_disk']:
                # handle full disk HRV data with two separated area definitions
                [upper_area_extent, lower_area_extent,
                 upper_nlines, upper_ncols, lower_nlines, lower_ncols] = self.get_area_extent(dataset_id)

                # upper area
                pdict['a_desc'] = 'SEVIRI high resolution channel, upper window'
                pdict['nlines'] = upper_nlines
                pdict['ncols'] = upper_ncols
                upper_area = get_area_definition(pdict, upper_area_extent)

                # lower area
                pdict['a_desc'] = 'SEVIRI high resolution channel, lower window'
                pdict['nlines'] = lower_nlines
                pdict['ncols'] = lower_ncols
                lower_area = get_area_definition(pdict, lower_area_extent)

                area = geometry.StackedAreaDefinition(lower_area, upper_area)
                area = area.squeeze()
            else:
                # if the HRV data is in a ROI, the HRV channel is delivered in one area
                area = get_area_definition(pdict, self.get_area_extent(dataset_id))

        else:
            pdict['nlines'] = self.mda['number_of_lines']
            pdict['ncols'] = self.mda['number_of_columns']
            pdict['a_name'] = 'geos_seviri_visir'
            pdict['a_desc'] = 'SEVIRI low resolution channel area'
            pdict['p_id'] = 'seviri_visir'

            area = get_area_definition(pdict, self.get_area_extent(dataset_id))

        return area

    def get_area_extent(self, dataset_id):
        """Get the area extent of the file.

        Until December 2017, the data is shifted by 1.5km SSP North and West against the nominal GEOS projection. Since
        December 2017 this offset has been corrected. A flag in the data indicates if the correction has been applied.
        If no correction was applied, adjust the area extent to match the shifted data.

        For more information see Section 3.1.4.2 in the MSG Level 1.5 Image Data Format Description. The correction
        of the area extent is documented in a `developer's memo <https://github.com/pytroll/satpy/wiki/
        SEVIRI-georeferencing-offset-correction>`_.
        """
        data15hd = self.header['15_DATA_HEADER']
        sec15hd = self.header['15_SECONDARY_PRODUCT_HEADER']

        # check for Earth model as this affects the north-south and
        # west-east offsets
        # section 3.1.4.2 of MSG Level 1.5 Image Data Format Description
        earth_model = data15hd['GeometricProcessing']['EarthModel'][
            'TypeOfEarthModel']
        if earth_model == 2:
            ns_offset = 0
            we_offset = 0
        elif earth_model == 1:
            ns_offset = -0.5
            we_offset = 0.5
            if dataset_id['name'] == 'HRV':
                ns_offset = -1.5
                we_offset = 1.5
        else:
            raise NotImplementedError(
                'Unrecognised Earth model: {}'.format(earth_model)
            )

        if dataset_id['name'] == 'HRV':
            grid_origin = data15hd['ImageDescription']['ReferenceGridHRV']['GridOrigin']
            center_point = (HRV_NUM_COLUMNS / 2) - 2
            coeff = 3
            column_step = data15hd['ImageDescription']['ReferenceGridHRV']['ColumnDirGridStep'] * 1000.0
            line_step = data15hd['ImageDescription']['ReferenceGridHRV']['LineDirGridStep'] * 1000.0
        else:
            grid_origin = data15hd['ImageDescription']['ReferenceGridVIS_IR']['GridOrigin']
            center_point = VISIR_NUM_COLUMNS / 2
            coeff = 1
            column_step = data15hd['ImageDescription']['ReferenceGridVIS_IR']['ColumnDirGridStep'] * 1000.0
            line_step = data15hd['ImageDescription']['ReferenceGridVIS_IR']['LineDirGridStep'] * 1000.0

        # Calculations assume grid origin is south-east corner
        # section 7.2.4 of MSG Level 1.5 Image Data Format Description
        origins = {0: 'NW', 1: 'SW', 2: 'SE', 3: 'NE'}
        if grid_origin != 2:
            msg = 'Grid origin not supported number: {}, {} corner'.format(
                grid_origin, origins[grid_origin]
            )
            raise NotImplementedError(msg)

        # check if data is in Rapid Scanning Service mode (RSS)
        is_rapid_scan = self.trailer['15TRAILER']['ImageProductionStats']['ActualScanningSummary']['ReducedScan']

        # The HRV channel in full disk mode comes in two separate areas, and each area has its own area extent stored
        # in the trailer.
        # In Rapid Scanning mode, only the "Lower" area (typically over Europe) is acquired and included in the files.
        if (dataset_id['name'] == 'HRV') and (self.mda['is_full_disk'] or is_rapid_scan):

            # get actual navigation parameters from trailer data
            data15tr = self.trailer['15TRAILER']
            HRV_bounds = data15tr['ImageProductionStats']['ActualL15CoverageHRV']

            # lower window
            lower_north_line = HRV_bounds['LowerNorthLineActual']
            lower_west_column = HRV_bounds['LowerWestColumnActual']
            lower_south_line = HRV_bounds['LowerSouthLineActual']
            lower_east_column = HRV_bounds['LowerEastColumnActual']

            lower_area_extent = self._calculate_area_extent(
                center_point, lower_north_line, lower_east_column,
                lower_south_line, lower_west_column, we_offset,
                ns_offset, column_step, line_step
            )

            if is_rapid_scan:
                return lower_area_extent

            lower_nlines = lower_north_line - lower_south_line + 1
            lower_ncols = lower_west_column - lower_east_column + 1

            # upper window
            upper_north_line = HRV_bounds['UpperNorthLineActual']
            upper_west_column = HRV_bounds['UpperWestColumnActual']
            upper_south_line = HRV_bounds['UpperSouthLineActual']
            upper_east_column = HRV_bounds['UpperEastColumnActual']

            upper_area_extent = self._calculate_area_extent(
                center_point, upper_north_line, upper_east_column,
                upper_south_line, upper_west_column, we_offset,
                ns_offset, column_step, line_step
            )

            upper_nlines = upper_north_line - upper_south_line + 1
            upper_ncols = upper_west_column - upper_east_column + 1

            return [upper_area_extent, lower_area_extent, upper_nlines, upper_ncols, lower_nlines, lower_ncols]

        # If the data was ordered in a defined ROI, the area extent is in one piece, the corner points are
        # the same as for VISIR channels, and the HRV channel is having three times the amount of columns and rows.
        else:

            north = coeff * int(sec15hd['NorthLineSelectedRectangle']['Value'])
            east = coeff * int(sec15hd['EastColumnSelectedRectangle']['Value'])
            west = coeff * int(sec15hd['WestColumnSelectedRectangle']['Value'])
            south = coeff * int(sec15hd['SouthLineSelectedRectangle']['Value'])

            area_extent = self._calculate_area_extent(
                center_point, north, east,
                south, west, we_offset,
                ns_offset, column_step, line_step
            )

        return area_extent

    def get_dataset(self, dataset_id, dataset_info):
        """Get the dataset."""
        if dataset_id['name'] not in self.mda['channel_list']:
            raise KeyError('Channel % s not available in the file' % dataset_id['name'])
        elif dataset_id['name'] not in ['HRV']:
            shape = (self.mda['number_of_lines'], self.mda['number_of_columns'])

            # Check if there is only 1 channel in the list as a change
            # is needed in the arrray assignment ie channl id is not present
            if len(self.mda['channel_list']) == 1:
                raw = self.dask_array['visir']['line_data']
            else:
                i = self.mda['channel_list'].index(dataset_id['name'])
                raw = self.dask_array['visir']['line_data'][:, i, :]

            data = dec10216(raw.flatten())
            data = data.reshape(shape)

        else:
            shape = (self.mda['hrv_number_of_lines'], self.mda['hrv_number_of_columns'])

            raw2 = self.dask_array['hrv']['line_data'][:, 2, :]
            raw1 = self.dask_array['hrv']['line_data'][:, 1, :]
            raw0 = self.dask_array['hrv']['line_data'][:, 0, :]

            shape_layer = (self.mda['number_of_lines'], self.mda['hrv_number_of_columns'])
            data2 = dec10216(raw2.flatten())
            data2 = data2.reshape(shape_layer)
            data1 = dec10216(raw1.flatten())
            data1 = data1.reshape(shape_layer)
            data0 = dec10216(raw0.flatten())
            data0 = data0.reshape(shape_layer)

            data = np.stack((data0, data1, data2), axis=1).reshape(shape)

        xarr = xr.DataArray(data, dims=['y', 'x']).where(data != 0).astype(np.float32)

        if xarr is None:
            dataset = None
        else:
            dataset = self.calibrate(xarr, dataset_id)
            dataset.attrs['units'] = dataset_info['units']
            dataset.attrs['wavelength'] = dataset_info['wavelength']
            dataset.attrs['standard_name'] = dataset_info['standard_name']
            dataset.attrs['platform_name'] = self.mda['platform_name']
            dataset.attrs['sensor'] = 'seviri'
            dataset.attrs['orbital_parameters'] = {
                'projection_longitude': self.mda['projection_parameters']['ssp_longitude'],
                'projection_latitude': 0.,
                'projection_altitude': self.mda['projection_parameters']['h']}

        return dataset

    def calibrate(self, data, dataset_id):
        """Calibrate the data."""
        tic = datetime.now()

        data15hdr = self.header['15_DATA_HEADER']
        calibration = dataset_id['calibration']
        channel = dataset_id['name']

        # even though all the channels may not be present in the file,
        # the header does have calibration coefficients for all the channels
        # hence, this channel index needs to refer to full channel list
        i = list(CHANNEL_NAMES.values()).index(channel)

        if calibration == 'counts':
            return data

        if calibration in ['radiance', 'reflectance', 'brightness_temperature']:
            # determine the required calibration coefficients to use
            # for the Level 1.5 Header
            if (self.calib_mode.upper() != 'GSICS' and self.calib_mode.upper() != 'NOMINAL'):
                raise NotImplementedError(
                    'Unknown Calibration mode : Please check')

            # NB GSICS doesn't have calibration coeffs for VIS channels
            if (self.calib_mode.upper() != 'GSICS' or channel in VIS_CHANNELS):
                coeffs = data15hdr[
                    'RadiometricProcessing']['Level15ImageCalibration']
                gain = coeffs['CalSlope'][i]
                offset = coeffs['CalOffset'][i]
            else:
                coeffs = data15hdr[
                    'RadiometricProcessing']['MPEFCalFeedback']
                gain = coeffs['GSICSCalCoeff'][i]
                offset = coeffs['GSICSOffsetCount'][i]
                offset = offset * gain
            res = self._convert_to_radiance(data, gain, offset)

        if calibration == 'reflectance':
            solar_irradiance = CALIB[self.platform_id][channel]["F"]
            res = self._vis_calibrate(res, solar_irradiance)

        elif calibration == 'brightness_temperature':
            cal_type = data15hdr['ImageDescription'][
                'Level15ImageProduction']['PlannedChanProcessing'][i]
            res = self._ir_calibrate(res, channel, cal_type)

        logger.debug("Calibration time " + str(datetime.now() - tic))
        return res


def get_available_channels(header):
    """Get the available channels from the header information."""
    chlist_str = header['15_SECONDARY_PRODUCT_HEADER'][
        'SelectedBandIDs']['Value']
    retv = {}

    for idx, char in zip(range(12), chlist_str):
        retv[CHANNEL_NAMES[idx + 1]] = (char == 'X')

    return retv
