#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Author(s):
#
#   Martin Raspaud <martin.raspaud@smhi.se>
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

"""Testing the yaml_reader module."""

import os
import unittest
from tempfile import mkdtemp

from mock import patch

import satpy.readers.yaml_reader as yr


class TestUtils(unittest.TestCase):
    """Test the utility functions."""

    def test_get_filebase(self):
        """Check the get_filebase function."""
        pattern = ('{mission_id:3s}_OL_{processing_level:1s}_{datatype_id:_<6s'
                   '}_{start_time:%Y%m%dT%H%M%S}_{end_time:%Y%m%dT%H%M%S}_{cre'
                   'ation_time:%Y%m%dT%H%M%S}_{duration:4d}_{cycle:3d}_{relati'
                   've_orbit:3d}_{frame:4d}_{centre:3s}_{mode:1s}_{timeliness:'
                   '2s}_{collection:3s}.SEN3/geo_coordinates.nc')
        filename = ('/home/a001673/data/satellite/Sentinel-3/S3A_OL_1_EFR____2'
                    '0161020T081224_20161020T081524_20161020T102406_0179_010_0'
                    '78_2340_SVL_O_NR_002.SEN3/Oa05_radiance.nc')
        expected = ('S3A_OL_1_EFR____20161020T081224_20161020T081524_20161020T'
                    '102406_0179_010_078_2340_SVL_O_NR_002.SEN3/Oa05_radiance.'
                    'nc')
        self.assertEqual(yr.get_filebase(filename, pattern), expected)

    def test_match_filenames(self):
        """Check that matching filenames works."""
        pattern = ('{mission_id:3s}_OL_{processing_level:1s}_{datatype_id:_<6s'
                   '}_{start_time:%Y%m%dT%H%M%S}_{end_time:%Y%m%dT%H%M%S}_{cre'
                   'ation_time:%Y%m%dT%H%M%S}_{duration:4d}_{cycle:3d}_{relati'
                   've_orbit:3d}_{frame:4d}_{centre:3s}_{mode:1s}_{timeliness:'
                   '2s}_{collection:3s}.SEN3/geo_coordinates.nc')
        filenames = ['/home/a001673/data/satellite/Sentinel-3/S3A_OL_1_EFR____2'
                     '0161020T081224_20161020T081524_20161020T102406_0179_010_0'
                     '78_2340_SVL_O_NR_002.SEN3/Oa05_radiance.nc',
                     '/home/a001673/data/satellite/Sentinel-3/S3A_OL_1_EFR____2'
                     '0161020T081224_20161020T081524_20161020T102406_0179_010_0'
                     '78_2340_SVL_O_NR_002.SEN3/geo_coordinates.nc']
        expected = ('S3A_OL_1_EFR____20161020T081224_20161020T081524_20161020T'
                    '102406_0179_010_078_2340_SVL_O_NR_002.SEN3/geo_coordinates'
                    '.nc')
        self.assertEqual(yr.match_filenames(filenames, pattern),
                         ["/home/a001673/data/satellite/Sentinel-3/" +
                          expected])

    def test_listify_string(self):
        """Check listify_string."""
        self.assertEqual(yr.listify_string(None), [])
        self.assertEqual(yr.listify_string('some string'), ['some string'])
        self.assertEqual(yr.listify_string(['some', 'string']),
                         ['some', 'string'])


class TestFileSelection(unittest.TestCase):
    """Test the file selection methods from FileYAMLReader."""

    @patch('satpy.readers.yaml_reader.recursive_dict_update')
    @patch('satpy.readers.yaml_reader.yaml', spec=yr.yaml)
    def setUp(self, _, rec_up):  # pylint: disable=arguments-differ
        """Setup a reader instance with a fake config."""
        patterns = ['a{something:3s}.bla']
        res_dict = {'reader': {'name': 'fake',
                               'sensors': ['canon']},
                    'file_types': {'ftype1': {'name': 'ft1',
                                              'file_patterns': patterns}},
                    'datasets': {}}

        rec_up.return_value = res_dict
        self.reader = yr.FileYAMLReader([__file__])

    def test_select_from_pathnames(self):
        """Check select_files_from_pathnames."""
        filelist = ['a001.bla', 'a002.bla', 'abcd.bla', 'k001.bla', 'a003.bli']

        res = self.reader.select_files_from_pathnames(filelist)
        for expected in ['a001.bla', 'a002.bla', 'abcd.bla']:
            self.assertIn(expected, res)

        self.assertEqual(0, len(self.reader.select_files_from_pathnames([])))

    def test_select_from_directory(self):
        """Check select_files_from_directory."""
        filelist = ['a001.bla', 'a002.bla', 'abcd.bla', 'k001.bla', 'a003.bli']
        dpath = mkdtemp()
        for fname in filelist:
            with open(os.path.join(dpath, fname), 'w'):
                pass

        res = self.reader.select_files_from_directory(dpath)
        for expected in ['a001.bla', 'a002.bla', 'abcd.bla']:
            self.assertIn(os.path.join(dpath, expected), res)

        for fname in filelist:
            os.remove(os.path.join(dpath, fname))
        self.assertEqual(0,
                         len(self.reader.select_files_from_directory(dpath)))
        os.rmdir(dpath)


def suite():
    """The test suite for test_scene."""
    loader = unittest.TestLoader()
    mysuite = unittest.TestSuite()
    mysuite.addTest(loader.loadTestsFromTestCase(TestUtils))
    mysuite.addTest(loader.loadTestsFromTestCase(TestFileSelection))

    return mysuite


if __name__ == "__main__":
    unittest.main()
