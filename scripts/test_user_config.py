"""Editable configuration and launcher contract tests, with no camera access."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import importlib.util
_MENU_SPEC = importlib.util.spec_from_file_location('archived_menu', Path(__file__).resolve().parents[1] / 'deprecated/scripts/menu.py')
menu = importlib.util.module_from_spec(_MENU_SPEC)
_MENU_SPEC.loader.exec_module(menu)
from recording_profile import KIT, default_output, legacy_profile, load_profile, resolve_record_args


def toml_text(profile):
    """Serialize just the fixture's supported scalar/list values and tables."""
    rows, tables = [], []
    for key, value in profile.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif value is not None:
            rows.append(f'{key} = {json.dumps(value, ensure_ascii=False)}')
    for key, table in tables:
        rows.append(f'[{key}]')
        rows.extend(f'{name} = {json.dumps(value, ensure_ascii=False)}'
                    for name, value in table.items() if value is not None)
    return '\n'.join(rows) + '\n'


class UserConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '录制参数.toml'
        self.profile = dict(legacy_profile(), profile='中文试录', output_dir='',
                            duration_hours=2.5, segment_seconds=3600,
                            encoder='hevc_nvenc', b_frames=3, nvenc_cq=30,
                            nvenc_preset='p5', gop_frames=250)
        self.write()

    def write(self, bom=False):
        self.path.write_text(toml_text(self.profile), encoding='utf-8-sig' if bom else 'utf-8')

    def run_entry(self, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(menu, 'capture', return_value=0) as capture:
            code = menu.configured(config=self.path, **kwargs)
        return code, capture, output.getvalue()

    def test_toml_bom_non_ascii_and_empty_output(self):
        self.write(bom=True)
        profile, source = load_profile(self.path)
        self.assertEqual(profile['profile'], '中文试录')
        self.assertIsNone(profile['output_dir'])
        self.assertEqual(len(source['sha256']), 64)
        self.assertEqual(source['path'], str(self.path.resolve()))

    def test_imaging_denoise_omission_is_only_null_normalization(self):
        p, _ = load_profile(KIT / 'deprecated/config/mouse-4k45-recommended.json')
        self.profile['imaging'] = p['imaging']
        self.write()
        loaded, _ = load_profile(self.path)
        self.assertIsNone(loaded['imaging']['denoise'])
        self.profile['imaging']['denoise'] = 12
        self.write()
        self.assertEqual(load_profile(self.path)[0]['imaging']['denoise'], 12)
        del self.profile['imaging']['gamma']
        self.write()
        with self.assertRaisesRegex(ValueError, 'gamma'):
            load_profile(self.path)

    def test_json_bom_legacy_profile_remains_usable(self):
        path = self.path.with_suffix('.json')
        profile = dict(self.profile, output_dir=None)
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding='utf-8-sig')
        loaded, _ = load_profile(path)
        self.assertEqual((loaded['profile'], loaded['nvenc_cq']), ('中文试录', 30))

    def test_malformed_unknown_and_invalid_fail_before_capture(self):
        for content in ('schema_version = [', toml_text(dict(self.profile, gop_frames=0)),
                        toml_text(dict(self.profile, unknown_key=3))):
            self.path.write_text(content, encoding='utf-8')
            with patch.object(menu, 'capture') as capture, self.assertRaises(ValueError):
                menu.configured(config=self.path)
            capture.assert_not_called()

    def test_start_uses_config_output_duration_segment_without_override(self):
        self.profile['output_dir'] = str(Path(self.temp.name) / '中文 输出')
        self.write()
        code, capture, output = self.run_entry()
        self.assertEqual(code, 0)
        arguments = capture.call_args.args[0]
        self.assertEqual(arguments, ['record', '--config', str(self.path.resolve())])
        args = argparse.Namespace(config=arguments[2], duration=None, output_dir=None)
        resolve_record_args(args)
        self.assertEqual((args.duration, args.segment_seconds), (9000, 3600))
        self.assertEqual(str(args.output_dir), self.profile['output_dir'])
        for text in ('CQ 30', 'GOP 250', 'B 帧 3', 'p5', '9000 秒', 'hevc_nvenc'):
            self.assertIn(text, output)

    def test_test_entry_only_overrides_60_seconds_and_20_second_segments(self):
        code, capture, output = self.run_entry(short=True)
        self.assertEqual(code, 0)
        self.assertEqual(capture.call_args.args[0], ['record', '--config', str(self.path.resolve()),
                                                   '--duration', '60', '--segment-seconds', '20'])
        args = argparse.Namespace(config=self.path, duration=60, segment_seconds=20, output_dir=None)
        resolve_record_args(args)
        self.assertEqual((args.duration, args.segment_seconds, args.nvenc_cq), (60, 20, 30))
        self.assertEqual(args.output_dir.resolve(), default_output().resolve())
        self.assertIn('试录明确覆盖', output)

    def test_check_does_not_launch_capture_or_create_output(self):
        self.profile['output_dir'] = str(Path(self.temp.name) / 'new-output')
        self.write()
        code, capture, output = self.run_entry(check=True)
        self.assertEqual(code, 0)
        capture.assert_not_called()
        self.assertFalse(Path(self.profile['output_dir']).exists())
        self.assertIn('配置检查通过', output)

    def test_no_automatic_encoder_fallback(self):
        self.profile.update(encoder='auto', b_frames=0)
        self.write()
        with patch.object(menu, 'capture') as capture, self.assertRaisesRegex(ValueError, 'auto'):
            menu.configured(config=self.path)
        capture.assert_not_called()

    def test_optional_preview_defaults_validation_and_cli_override(self):
        p, _ = load_profile(self.path)
        self.assertEqual((p['preview_enabled'], p['preview_fps'], p['preview_max_width']), (False, 5, 960))
        self.profile.update(preview_enabled=True, preview_fps=5, preview_max_width=960)
        self.write()
        _, _, output = self.run_entry(check=True)
        self.assertIn('录制预览已开启', output)
        args = argparse.Namespace(config=self.path, duration=None, output_dir=None,
                                  preview_enabled=False, preview_fps=3, preview_max_width=640)
        resolve_record_args(args)
        self.assertEqual((args.preview_enabled, args.preview_fps, args.preview_max_width), (False, 3, 640))
        self.assertEqual(args.fps, self.profile['fps'])
        for key, value in [('preview_enabled', 'true'), ('preview_fps', 0),
                           ('preview_fps', 11), ('preview_max_width', 319),
                           ('preview_max_width', 1281), ('preview_fps', True)]:
            with self.subTest(key=key, value=value):
                bad = dict(self.profile, **{key: value})
                self.path.write_text(toml_text(bad), encoding='utf-8')
                with patch.object(menu, 'capture') as recording, self.assertRaises(ValueError):
                    menu.configured(config=self.path)
                recording.assert_not_called()

    def test_capped_vbr_does_not_claim_cq_or_crf_is_active(self):
        self.profile['rate_control'] = 'capped_vbr'
        self.write()
        _, _, output = self.run_entry(check=True)
        self.assertIn('CQ / CRF 不生效', output)
        self.assertNotIn('CQ 30', output)

    def test_status_defaults_validation_and_cli_override(self):
        p, _ = load_profile(self.path)
        self.assertEqual((p['status_enabled'], p['status_interval_seconds']), (True, 2))
        self.profile.update(status_enabled=True, status_interval_seconds=3)
        self.write()
        _, _, output = self.run_entry(check=True)
        self.assertIn('每 3 秒刷新', output)
        args = argparse.Namespace(config=self.path, duration=None, output_dir=None,
                                  status_enabled=False, status_interval_seconds=1)
        resolve_record_args(args)
        self.assertEqual((args.status_enabled, args.status_interval_seconds), (False, 1))
        for key, value in [('status_enabled', 'true'), ('status_interval_seconds', 0),
                           ('status_interval_seconds', 61), ('status_interval_seconds', True),
                           ('status_interval_seconds', 1.5)]:
            with self.subTest(key=key, value=value):
                self.path.write_text(toml_text(dict(self.profile, **{key: value})), encoding='utf-8')
                with patch.object(menu, 'capture') as recording, self.assertRaises(ValueError):
                    menu.configured(config=self.path)
                recording.assert_not_called()

    def test_x265_warning_does_not_block_explicit_experiment(self):
        self.profile.update(encoder='libx265', fps=45, speed_level=4, crf=21)
        self.write()
        code, capture, output = self.run_entry()
        self.assertEqual(code, 0)
        capture.assert_called_once()
        self.assertIn('CRF 21', output)
        self.assertIn('吞吐不足', output)


if __name__ == '__main__':
    unittest.main()
