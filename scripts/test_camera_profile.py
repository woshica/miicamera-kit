"""Offline checks for the camera imaging profile; no device is opened."""

import unittest

from camera_profile import (
    ImagingApplyError, apply_imaging, diagnose_imaging_support, validate_imaging,
)


SETTINGS = {
    'bit_depth': 8, 'binning': 1, 'roi': 'full', 'gamma': 100,
    'contrast': 0, 'brightness': 0, 'hue': 0, 'saturation': 128,
    'sharpening': 0, 'denoise': 0, 'white_balance_temp': 6503,
    'white_balance_tint': 1000, 'level_low': [0, 0, 0, 0],
    'level_high': [255, 255, 255, 255],
}


class FakeSdk:
    MIICAM_OPTION_BITDEPTH = 6
    MIICAM_OPTION_BINNING = 23
    MIICAM_OPTION_SHARPENING = 30
    MIICAM_OPTION_DENOISE = 53


class FakeCamera:
    def __init__(self):
        self.options = {30: 2 << 16}
        self.values = {}
        self.roi = (0, 0, 0, 0)
        self.white_balance = (6503, 1000)
        self.levels = ((0, 0, 0, 0), (255, 255, 255, 255))
        self.fail_option = None
        self.ignore_gamma = False

    def put_Option(self, option, value):
        if option == self.fail_option:
            raise OSError('E_NOTIMPL')
        self.options[option] = value

    def get_Option(self, option):
        if option == self.fail_option:
            raise OSError('E_NOTIMPL')
        return self.options[option]

    def put_Roi(self, *roi):
        self.roi = roi

    def get_Roi(self):
        return self.roi

    def get_Size(self):
        return 3840, 2160

    def put_TempTint(self, temp, tint):
        self.white_balance = temp, tint

    def get_TempTint(self):
        return self.white_balance

    def put_LevelRange(self, low, high):
        self.levels = tuple(low), tuple(high)

    def get_LevelRange(self):
        return self.levels

    def __getattr__(self, name):
        if name.startswith('put_'):
            field = name[4:]
            return lambda value: self.values.__setitem__(field, value) if not (field == 'Gamma' and self.ignore_gamma) else None
        if name.startswith('get_'):
            field = name[4:]
            return lambda: self.values.get(field, 0)
        raise AttributeError(name)


class CameraProfileTest(unittest.TestCase):
    def test_apply_returns_every_verified_value(self):
        camera = FakeCamera()
        self.assertEqual(apply_imaging(camera, FakeSdk, SETTINGS), SETTINGS)

    def test_full_roi_may_read_back_as_native_dimensions(self):
        camera = FakeCamera()
        camera.get_Roi = lambda: (0, 0, 3840, 2160)
        self.assertEqual(apply_imaging(camera, FakeSdk, SETTINGS)['roi'], 'full')
        camera.get_Roi = lambda: (0, 0, 3200, 1800)
        with self.assertRaises(ImagingApplyError) as error:
            apply_imaging(camera, FakeSdk, SETTINGS)
        self.assertEqual(error.exception.field, 'roi')

    def test_null_denoise_skips_unimplemented_option(self):
        camera = FakeCamera()
        camera.fail_option = FakeSdk.MIICAM_OPTION_DENOISE
        actual = apply_imaging(camera, FakeSdk, SETTINGS | {'denoise': None})
        self.assertIsNone(actual['denoise'])
        self.assertIn('not controlled', actual['denoise_note'])
        self.assertNotIn(FakeSdk.MIICAM_OPTION_DENOISE, camera.options)

    def test_invalid_schema_never_writes(self):
        camera = FakeCamera()
        for field, value in [('gamma', True), ('binning', 2), ('level_low', [0, 0, 0])]:
            wrong = SETTINGS | {field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply_imaging(camera, FakeSdk, wrong)
        self.assertEqual(camera.options, {30: 2 << 16})
        with self.assertRaises(ValueError):
            validate_imaging(SETTINGS | {'blacklevel': 0})

    def test_unsupported_option_names_field(self):
        camera = FakeCamera()
        camera.fail_option = FakeSdk.MIICAM_OPTION_DENOISE
        with self.assertRaises(ImagingApplyError) as error:
            apply_imaging(camera, FakeSdk, SETTINGS)
        self.assertEqual(error.exception.field, 'denoise')
        self.assertIn('E_NOTIMPL', str(error.exception))

    def test_diagnostic_reports_unsupported_getter_without_writing(self):
        camera = FakeCamera()
        camera.fail_option = FakeSdk.MIICAM_OPTION_DENOISE
        result = diagnose_imaging_support(camera, FakeSdk)
        self.assertFalse(result['denoise']['supported_read'])
        self.assertTrue(result['white_balance_temp/white_balance_tint']['supported_read'])
        self.assertEqual(camera.options, {30: 2 << 16})

    def test_ignored_write_is_not_reported_applied(self):
        camera = FakeCamera()
        camera.ignore_gamma = True
        with self.assertRaises(ImagingApplyError) as error:
            apply_imaging(camera, FakeSdk, SETTINGS)
        self.assertEqual(error.exception.field, 'gamma')
        self.assertEqual(error.exception.operation, 'readback')


if __name__ == '__main__':
    unittest.main()
