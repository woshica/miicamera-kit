"""Diagnostic orchestration checks; never open a camera or launch an encoder."""
import argparse
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import diagnose_pc as doctor


class DiagnosticTests(unittest.TestCase):
    def test_camera_no_device_usb2_unknown_or_multiple_not_ready(self):
        good = {'model': 'fake', 'usb': 'USB 3.0 capable/connected', 'resolutions': [[3840, 2160]]}
        self.assertEqual(doctor.camera_assessment({'cameras': [good]}, 45)[0::2], ('pass', True))
        for cameras in ([], [good, good], [dict(good, usb='USB 2.0 connection (USB 3 camera)')],
                        [dict(good, usb='USB mode not reported')], [dict(good, resolutions=[[1920, 1080]])]):
            self.assertFalse(doctor.camera_assessment({'cameras': cameras}, 45)[2])

    def test_real_recording_pass_requires_decoded_counts_and_no_drops(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / 'capture-example'
            run.mkdir()
            metadata = {'status': 'complete', 'segments': [{'frames_submitted': 900, 'frames_decoded': 900}],
                        'requested_fps': 45, 'measured_acquisition_fps': 45.1, 'frames_captured': 900,
                        'capture_elapsed_s': 20, 'sdk_sequence_gaps': 0, 'sdk_reported_software_drops': 0,
                        'sdk_frontend_full_count': 0, 'sdk_backend_full_count': 0}
            child = {'returncode': 0, 'timed_out': False, 'log': 'test.log'}
            def assess(data, process=child):
                (run / 'capture.json').write_text(json.dumps(data), encoding='utf-8')
                return doctor.assess_recording(root, process)
            self.assertEqual(assess(metadata)['status'], 'pass')
            for change in ({'sdk_reported_software_drops': 1}, {'status': 'incomplete'},
                           {'measured_acquisition_fps': 22}, {'segments': []},
                           {'segments': [{'frames_submitted': 900, 'frames_decoded': 899}]}):
                self.assertEqual(assess(dict(metadata, **change))['status'], 'fail')
            self.assertEqual(assess(metadata, dict(child, timed_out=True))['status'], 'fail')
            self.assertIn('SDK后端帧缓存溢出', assess(dict(metadata, status='incomplete',
                                                        error='Camera fatal event: 0x0011'))['detail'])
            self.assertIn('编码输入队列堆满', assess(dict(metadata, status='incomplete',
                                                        error='Encoder queue overflow (16 frames)'))['detail'])

    def run_fake_diagnostic(self, env, low_space=False):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            target = Path(folder) / '录像保存盘'
            directory = Path(folder) / '报告'
            directory.mkdir()
            args = argparse.Namespace(config=Path('unused'), output_dir=None, skip_camera=True)
            report = {'created_at': 'test', 'report_dir': str(directory), 'checks': [], 'encoders': []}
            def resolve(args):
                args.resolved_profile = {'encoder': 'hevc_nvenc', 'fps': 45, 'output_dir': str(target)}
                args.profile_source = None
                args.output_dir = target
                args.storage_plan = None
            def probe(command, log, timeout):
                return env if '--environment' in command else {'cameras': []}
            row = {'encoder': 'hevc_nvenc', 'label': 'NVIDIA HEVC', 'supported_by_recorder': True,
                   'functional': True, 'benchmark': {'meets_target': True}}
            with patch.object(doctor, 'resolve_record_args', side_effect=resolve), \
                    patch.object(doctor, 'probe_json', side_effect=probe) as probe_mock, \
                    patch.object(doctor, 'run_logged', return_value={'returncode': 0, 'timed_out': False}) as run, \
                    patch.object(doctor.shutil, 'disk_usage', return_value=NS(free=1 if low_space else 100*1024**3)), \
                    patch('encoder_diagnostics.probe_encoders', return_value=[row]) as encoders:
                doctor.run_diagnostics(args, report)
            return copy.deepcopy(report), probe_mock, run, encoders

    def test_busy_or_unknown_occupancy_skips_every_heavy_check(self):
        for env in ({'active_recording_processes': [{'name': 'ImageView.exe', 'pid': 1}], 'errors': []},
                    {'active_recording_processes': [], 'errors': ['process_inventory_unavailable']},
                    {'active_recording_processes': [], 'errors': ['windows_inventory_unavailable_or_timed_out']}):
            report, probe, run, encoders = self.run_fake_diagnostic(env)
            self.assertEqual(probe.call_count, 1)
            run.assert_not_called()
            encoders.assert_not_called()
            self.assertNotIn('recording_test', report)

    def test_low_disk_space_does_not_write_probe_or_start_camera(self):
        env = {'errors': [], 'active_recording_processes': [], 'volume': {'free_bytes': 1}}
        report, probe, run, encoders = self.run_fake_diagnostic(env, low_space=True)
        self.assertFalse(any('--disk' in call.args[0] for call in probe.call_args_list))
        self.assertEqual(run.call_count, 1)  # Only integrity verification, no camera capture.
        self.assertTrue(any(c['name'] == '保存盘容量' and c['status'] == 'fail' for c in report['checks']))

    def test_report_distinguishes_unsupported_hardware_and_incomplete_benchmark(self):
        report = {'created_at': 'test', 'report_dir': 'test', 'checks': [], 'encoders': [
            {'encoder': 'hevc_qsv', 'label': 'Intel QSV HEVC', 'supported_by_recorder': False, 'functional': True,
             'benchmark': {'status': 'skipped', 'fps': None}},
            {'encoder': 'libx265', 'label': 'Software HEVC', 'supported_by_recorder': True, 'functional': True,
             'benchmark': {'status': 'timeout', 'fps': 2.1, 'error': 'Exceeded20s'}}]}
        text = doctor.render_report(report)
        self.assertIn('本工具尚未接入', text)
        self.assertIn('超时，2.1fps', text)
        self.assertIn('小片通过只说明', text)


if __name__ == '__main__':
    unittest.main()
