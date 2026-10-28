"""Offline operator preview/control tests; never load the vendor SDK."""
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import capture
import operator_camera as operator


class MonitorTests(unittest.TestCase):
    def test_direct_embedded_cli_help_outside_kit(self):
        kit = Path(operator.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([str(kit/'runtime/python/python.exe'), '-B',
                                     str(kit/'scripts/operator_camera.py'), '--help'],
                                    cwd=tmp, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'--control-dir', result.stdout)

    def test_early_record_failure_closes_monitor_and_publishes_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitors = []
            class Monitor(operator.CameraMonitor):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    monitors.append(self)
            args = NS(control_dir=Path(tmp), command='snapshot')
            with patch.object(operator, 'CameraMonitor', Monitor), \
                 patch.object(capture, 'sdk', side_effect=RuntimeError('missing SDK')):
                with self.assertRaisesRegex(RuntimeError, 'missing SDK'):
                    capture.run_capture(args)
            self.assertFalse(monitors[0].thread.is_alive())
            state = json.loads((Path(tmp)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['stage'], 'incomplete')
            self.assertIn('missing SDK', state['error'])

    def test_png_and_stop_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = operator.CameraMonitor(tmp, 4, 2)
            try:
                frame = bytes(range(24))
                monitor.offer(frame)
                deadline = time.monotonic() + 3
                preview = Path(tmp)/'preview.png'
                while not preview.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(preview.read_bytes().startswith(b'\x89PNG'))
                monitor.publish('recording', frames=2)
                monitor.publish('complete', frames=3)
                self.assertEqual(json.loads((Path(tmp)/'status.json').read_text(encoding='utf-8'))['frames'], 3)
                self.assertFalse(monitor.stop_requested())
                (Path(tmp)/'stop.request').touch()
                self.assertTrue(monitor.stop_requested())
            finally:
                monitor.finish()
            self.assertFalse(monitor.thread.is_alive())

    def test_settings_readback_required(self):
        camera = NS(put_ExpoTime=lambda x: None, put_ExpoAGain=lambda x: None,
                    get_ExpoTime=lambda: 10000, get_ExpoAGain=lambda: 100)
        self.assertEqual(operator.apply_settings(camera, 10000, 100)['gain'], 100)
        with self.assertRaises(RuntimeError):
            operator.apply_settings(camera, 20000, 100)
        with self.assertRaises(ValueError):
            operator.apply_settings(camera, True, 100)

    def test_calibration_setting_failure_closes_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'settings.json').write_text('{"exposure_us":10000,"gain":101}')
            events = []
            camera = NS(StartPullModeWithCallback=lambda *a: events.append('start'),
                Stop=lambda: events.append('stop'), Close=lambda: events.append('close'),
                put_ExpoTime=lambda x: None, put_ExpoAGain=lambda x: None,
                get_ExpoTime=lambda:10000, get_ExpoAGain=lambda:100)
            profile = dict(exposure_us=10000, gain=100, speed_level=2,
                           sdk_frame_rate_limit=0, frontend_buffer=16,
                           backend_buffer=16, usb_thread_priority=2)
            with patch('recording_profile.load_profile', return_value=(profile, {})), \
                 patch.object(capture, 'sdk', return_value=NS()), \
                 patch.object(capture, 'select_camera', return_value=(NS(), camera)), \
                 patch.object(capture, 'configure', return_value=(4,2,{})):
                self.assertEqual(operator.calibrate('fake.json', tmp), 1)
            self.assertEqual(events, ['start','stop','close'])
            state = json.loads((Path(tmp)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['stage'], 'incomplete')
            self.assertIn('readback mismatch', state['error'])


class OwnerTests(unittest.TestCase):
    def test_restart_probe_closes_handle_and_blocks_unknown_errors(self):
        owner = NS(alive=Mock(return_value=True), close=Mock())
        with patch.object(operator, 'WindowsOwner', return_value=owner):
            self.assertTrue(operator.parent_is_alive(123))
        owner.close.assert_called_once()
        with patch.object(operator, 'WindowsOwner', side_effect=RuntimeError('unavailable')), \
             patch.object(operator.ctypes, 'get_last_error', return_value=87):
            self.assertFalse(operator.parent_is_alive(123))
        with patch.object(operator, 'WindowsOwner', side_effect=RuntimeError('denied')), \
             patch.object(operator.ctypes, 'get_last_error', return_value=5):
            self.assertTrue(operator.parent_is_alive(123))

    def test_windows_handle_is_cached_and_closed_without_truncation(self):
        handle = 0x123456789ABC
        kernel = NS(OpenProcess=Mock(return_value=handle),
                    WaitForSingleObject=Mock(side_effect=[258, 0]),
                    CloseHandle=Mock(return_value=1))
        owner = operator.WindowsOwner(123, kernel=kernel)
        self.assertTrue(owner.alive())
        self.assertFalse(owner.alive())
        self.assertIs(kernel.OpenProcess.restype, operator.ctypes.c_void_p)
        kernel.WaitForSingleObject.assert_called_with(handle, 0)
        kernel.OpenProcess.assert_called_once_with(0x00100000, 0, 123)
        owner.close()
        owner.close()
        kernel.CloseHandle.assert_called_once_with(handle)

    def test_owner_death_is_failure_not_user_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'owner.json').write_text('{"pid":123}')
            Path(tmp, 'stop.request').touch()
            owner = NS(alive=Mock(side_effect=[True, False]), close=Mock())
            with patch.object(operator, 'WindowsOwner', return_value=owner) as factory:
                monitor = operator.CameraMonitor(tmp)
                try:
                    self.assertTrue(monitor.stop_requested())
                    with self.assertRaisesRegex(RuntimeError, '工作台已退出'):
                        monitor.stop_requested()
                    factory.assert_called_once_with(123)
                finally:
                    monitor.finish()
                owner.close.assert_called_once()

    def test_unopenable_parent_fails_closed(self):
        kernel = NS(OpenProcess=Mock(return_value=None), WaitForSingleObject=Mock(), CloseHandle=Mock())
        with self.assertRaisesRegex(RuntimeError, '工作台已退出'):
            operator.WindowsOwner(123, kernel=kernel)


class RecordingControlTests(unittest.TestCase):
    def test_parent_death_drains_recording_and_marks_incomplete(self):
        from test_capture_preview import CapturePreviewTests
        from recording_profile import resolve_record_args
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'owner.json').write_text('{"pid":123}')
            owner = NS(alive=Mock(side_effect=[True, True, True, False]), close=Mock())
            def resolve(args):
                resolve_record_args(args)
                args.control_dir = Path(tmp)
            with patch('test_capture_preview.resolve_record_args', resolve), \
                 patch.object(operator, 'WindowsOwner', return_value=owner):
                metadata, observed = CapturePreviewTests().run_recording('normal', enabled=False, fail_capture=True)
            self.assertFalse(metadata['stopped_by_user'])
            self.assertEqual(len(observed.writer.frames), 3)
            self.assertIn('工作台已退出', metadata['error'])
            self.assertEqual(json.loads(Path(tmp, 'status.json').read_text(encoding='utf-8'))['stage'], 'incomplete')

    def test_close_failure_marks_record_incomplete_and_returns_failure(self):
        from test_capture_preview import CapturePreviewTests
        metadata, observed = CapturePreviewTests().run_recording('normal', enabled=False, fail_close=True)
        self.assertIn('close failed', metadata['error'])

    def test_graceful_short_record_retains_validation(self):
        from test_capture_preview import CapturePreviewTests
        from recording_profile import resolve_record_args
        with tempfile.TemporaryDirectory() as tmp:
            offered = []
            class Monitor(operator.CameraMonitor):
                def offer(self, rgb):
                    offered.append(rgb)
                    if len(offered) == 3:
                        (self.directory/'stop.request').touch()
            def resolve(args):
                resolve_record_args(args)
                args.control_dir = Path(tmp)
            with patch('test_capture_preview.resolve_record_args', resolve), \
                 patch.object(operator, 'CameraMonitor', Monitor):
                metadata, observed = CapturePreviewTests().run_recording('normal', enabled=False, expected_frames=3)
            self.assertTrue(metadata['stopped_by_user'])
            self.assertEqual(metadata['status'], 'complete')
            self.assertEqual(metadata['frames_captured'], 3)
            state = json.loads((Path(tmp)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['stage'], 'complete')
            self.assertTrue(state['stopped_by_user'])
            self.assertEqual(state['frames'], 3)


if __name__ == '__main__':
    unittest.main()
