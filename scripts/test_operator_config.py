"""Operator persistence and failure safety, without camera hardware."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import operator_config
from operator_config import OperatorConfig
from recording_profile import KIT, load_profile


class OperatorConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.kit = Path(self.temp.name) / 'kit'
        self.kit.mkdir()
        source = KIT / '录制参数.toml'
        if not source.exists():
            source = KIT / 'config/operator-template.json'
        self.seed_profile, _ = load_profile(source)
        self.original = json.dumps(self.seed_profile, ensure_ascii=False).encode('utf-8')
        (self.kit / 'config').mkdir()
        (self.kit / 'config/operator-template.json').write_bytes(self.original)
        self.config = OperatorConfig(self.kit)
        self.view = self.config.initialize()

    def test_seed_preserves_original_and_restart_preserves_saved(self):
        original = self.seed_profile
        merged = self.config.resolved()
        for key, value in original.items():
            if key != 'output_dir':
                self.assertEqual(value, merged[key], key)
        daily = dict(self.view['daily'], duration_hours=2)
        self.config.save(daily, {'exposure_ms': 12.345, 'gain_multiplier': 1.23})
        restarted = OperatorConfig(self.kit)
        self.assertEqual(restarted.initialize()['daily'], daily)
        self.assertEqual(restarted.resolved()['exposure_us'], 12345)
        self.assertEqual((self.kit / 'config/operator-template.json').read_bytes(), self.original)

    def test_copy_and_switch_scheme_and_snapshot(self):
        view = self.config.create_scheme('夜间', 'current')
        key = view['daily']['scheme']
        self.assertNotEqual(key, 'current')
        before = self.config.resolved()
        self.config.save(view['daily'], {'exposure_ms': 20, 'gain_multiplier': 2})
        self.assertNotEqual(self.config.resolved()['gain'], before['gain'])
        self.assertEqual(self.config.view()['schemes']['current'], self.view['schemes']['current'])
        self.assertEqual(before['imaging'], self.config.view()['imaging'])
        self.assertEqual(self.config.resolved(short=True)['segment_seconds'], 20)
        self.assertEqual(self.config.resolved(short=True)['duration_hours'] * 3600, 60)

    def test_invalid_never_changes_saved_state(self):
        original = self.config.view()
        for updates in ({'output_dir': str(self.kit / 'data')}, {'output_dir': 'relative'},
                        {'duration_hours': float('nan')}, {'segment_minutes': 0.001},
                        {'duration_hours': True}, {'scheme': []}, {'unknown': 3}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.config.save(dict(original['daily'], **updates))
            self.assertEqual(original, self.config.view())
        for exposure in (float('inf'), float('nan'), True, 0, 0.0001):
            with self.subTest(exposure=exposure), self.assertRaises(ValueError):
                self.config.save(original['daily'], {'exposure_ms': exposure, 'gain_multiplier': 1})
        self.assertEqual(original, self.config.view())

    def test_save_as_copies_typed_values_only_to_new_scheme_atomically(self):
        old = self.config.view()
        daily = dict(old['daily'], duration_hours=3, scheme='ignored-for-copy')
        result = self.config.create_scheme('  夜间新值  ', 'current',
                                           {'exposure_ms': 20, 'gain_multiplier': 2}, daily)
        key = result['daily']['scheme']
        self.assertEqual(result['schemes']['current'], old['schemes']['current'])
        self.assertEqual(result['schemes'][key]['name'], '夜间新值')
        self.assertEqual(result['schemes'][key]['exposure_ms'], 20)
        self.assertEqual(result['schemes'][key]['gain_multiplier'], 2)
        self.assertEqual(result['daily']['duration_hours'], 3)
        before = self.config.view()
        invalid = (
            ('失败', {'exposure_ms': float('nan'), 'gain_multiplier': 2}, daily),
            ('失败', {'exposure_ms': 20, 'gain_multiplier': 2}, dict(daily, output_dir='relative')),
            (' 夜间新值 ', None, None),
            ('现场当前方案', None, None),
        )
        for name, values, daily_values in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.config.create_scheme(name, 'current', values, daily_values)
            self.assertEqual(self.config.view(), before)

    def test_failed_second_write_rolls_back(self):
        previous = self.config.view()
        real_write = operator_config._write
        failed = False

        def fail_once(path, value):
            nonlocal failed
            if path.name == 'schemes.json' and not failed:
                failed = True
                raise OSError('simulated disk failure')
            return real_write(path, value)

        with patch.object(operator_config, '_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.config.save(dict(previous['daily'], duration_hours=2))
        self.assertEqual(self.config.view(), previous)

    def test_interrupted_commit_recovers(self):
        previous = self.config.view()
        operator_config._write(self.config.directory / '.transaction.json',
                               {'daily': previous['daily'], 'schemes': previous['schemes']})
        operator_config._write(self.config.directory / 'daily.json',
                               dict(previous['daily'], duration_hours=2))
        self.assertEqual(OperatorConfig(self.kit).initialize(), previous)

    def test_scheme_advanced_override_and_rejection(self):
        schemes = copy.deepcopy(self.view['schemes'])
        schemes['current']['overrides'] = {'nvenc_cq': 25}
        operator_config._write(self.config.directory / 'schemes.json', schemes)
        self.assertEqual(self.config.resolved()['nvenc_cq'], 25)
        for override in ({'encoder': 'auto'}, {'output_dir': 'anything'}, {'nvenc_cq': float('nan')}):
            schemes['current']['overrides'] = override
            (self.config.directory / 'schemes.json').write_text(json.dumps(schemes), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.config.resolved()

    def test_template_only_initialization_and_existing_settings_ignore_template(self):
        self.assertFalse((self.kit / '录制参数.toml').exists())
        self.assertEqual(self.config.resolved()['imaging'], self.seed_profile['imaging'])
        (self.kit / 'config/operator-template.json').write_text('{}', encoding='utf-8')
        self.assertEqual(OperatorConfig(self.kit).initialize(), self.view)

    def test_imaging_selected_only_and_form_values_saved_atomically(self):
        view = self.config.create_scheme('新方案', 'current')
        key = view['daily']['scheme']
        schemes = copy.deepcopy(view['schemes'])
        schemes[key]['overrides']['nvenc_cq'] = 25
        operator_config._write(self.config.directory / 'schemes.json', schemes)
        advanced = (self.config.directory / 'advanced.json').read_bytes()
        imaging = copy.deepcopy(view['imaging'])
        imaging['gamma'] = 123
        result = self.config.save_imaging(imaging, dict(view['daily'], duration_hours=2),
                                          {'exposure_ms': 20, 'gain_multiplier': 2})
        self.assertEqual(result['imaging'], imaging)
        self.assertEqual(result['daily']['duration_hours'], 2)
        self.assertEqual(result['schemes']['current'], view['schemes']['current'])
        self.assertEqual(result['schemes'][key]['overrides']['nvenc_cq'], 25)
        self.assertEqual(self.config.resolved()['exposure_us'], 20000)
        self.assertEqual((self.config.directory / 'advanced.json').read_bytes(), advanced)
        imaging['level_low'][0] = 99
        result['imaging']['gamma'] = 22
        self.assertEqual(self.config.view()['imaging']['gamma'], 123)

    def test_invalid_imaging_and_form_never_change_files(self):
        before = {p.name: p.read_bytes() for p in self.config.directory.iterdir()}
        for updates in ({'gamma': 181}, {'gamma': True}, {'auto_exposure': True},
                        {'roi': [0, 0, 100, 100]}, {'bit_depth': 16},
                        {'level_low': [255, 0, 0, 0]}, {'denoise': 101}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.config.save_imaging(dict(self.view['imaging'], **updates))
        with self.assertRaises(ValueError):
            self.config.save_imaging(self.view['imaging'], dict(self.view['daily'], duration_hours=0))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config.directory.iterdir()})

    def test_imaging_snapshot_and_copy_capture_effective_values(self):
        snapshot = self.config.resolved()
        copied = self.config.create_scheme('副本', 'current')
        key = copied['daily']['scheme']
        self.assertEqual(copied['schemes'][key]['overrides']['imaging'], snapshot['imaging'])
        imaging = dict(copied['imaging'], gamma=122)
        self.config.save_imaging(imaging)
        self.assertEqual(snapshot['imaging'], self.view['imaging'])
        self.assertNotEqual(snapshot['imaging'], self.config.resolved()['imaging'])
        copied['schemes'][key]['overrides']['imaging']['level_low'][0] = 99
        self.assertNotEqual(self.config.view()['imaging']['level_low'][0], 99)

    def test_older_profile_without_imaging_accepts_complete_imaging(self):
        advanced = self.config._read('advanced.json')
        del advanced['imaging']
        operator_config._write(self.config.directory / 'advanced.json', advanced)
        self.assertIsNone(self.config.view()['imaging'])
        self.assertNotIn('imaging', self.config.resolved())
        self.assertEqual(self.config.save_imaging(self.view['imaging'])['imaging'], self.view['imaging'])

    def test_imaging_targets_form_selection_and_rolls_back_on_disk_failure(self):
        copied = self.config.create_scheme('副本', 'current')
        daily = dict(copied['daily'], scheme='current', duration_hours=3)
        imaging = dict(self.view['imaging'], gamma=121)
        result = self.config.save_imaging(imaging, daily)
        self.assertEqual(result['daily'], daily)
        self.assertEqual(result['imaging'], imaging)
        key = copied['daily']['scheme']
        self.assertEqual(result['schemes'][key], copied['schemes'][key])
        previous = self.config.view()
        real_write = operator_config._write
        failed = False

        def fail_once(path, value):
            nonlocal failed
            if path.name == 'schemes.json' and not failed:
                failed = True
                raise OSError('simulated disk failure')
            return real_write(path, value)

        with patch.object(operator_config, '_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.config.save_imaging(dict(imaging, gamma=122), dict(daily, duration_hours=4))
        self.assertEqual(self.config.view(), previous)


if __name__ == '__main__':
    unittest.main()
