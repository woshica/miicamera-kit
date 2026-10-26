"""Capture integration: preview failure/closure cannot discard recording frames."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import capture
from recording_profile import resolve_record_args


class CapturePreviewTests(unittest.TestCase):
    def run_recording(self, mode, enabled=True, status_mode=None, fail_capture=False):
        clock = NS(ns=1_000_000_000)
        observed = NS(preview=None, writer=None, status=None)

        class Camera:
            def StartPullModeWithCallback(self, *args): pass
            def Stop(self): pass
            def Close(self): pass
            def get_Option(self, option): return 0
            def WaitImageV4(self, timeout, buffer, a, b, c, info):
                if fail_capture and len(observed.writer.frames) == 3:
                    raise RuntimeError('fake camera disconnected')
                clock.ns += 22_222_222
                info.v3 = NS(width=4, height=2, flag=3,
                             seq=(clock.ns-1_000_000_000)//22_222_222,
                             timestamp=clock.ns//1000)

        class Writer:
            def __init__(self, *args, **kwargs):
                self.error = None
                self.segments = []
                self.encoding_settings = {}
                self.frames = []
                observed.writer = self
            def start(self): pass
            def submit(self, rgb, meta):
                self.frames.append((rgb, meta))
            def finish(self): pass
            def write_timing_snapshot(self):
                return {'window_seconds': 1, 'sample_count': len(self.frames),
                        'mean_ms': 3.5 if self.frames else None, 'in_progress_ms': None}

        class Status:
            def __init__(self, *args, **kwargs):
                self.states = []
                self.messages = []
                self.finished = False
                self.write_timing_provider = kwargs['write_timing_provider']
                observed.status = self
            def start(self):
                if status_mode == 'start_failure':
                    raise OSError('status startup failed')
            def publish(self, **state):
                if status_mode == 'publish_failure':
                    raise OSError('status publish failed')
                self.states.append(state)
            def finish(self): self.finished = True
            def snapshot(self): return {'worker_alive': False}
            def notice(self, message): self.messages.append(message)

        def validate(run_dir, segments, progress=None):
            if progress:
                progress(0, 'fake.mkv')
                progress(1, None)

        class Preview:
            def __init__(self, *args, **kwargs):
                self.calls = 0
                self.finished = False
                self.status = 'running'
                observed.preview = self
            def start(self):
                if mode == 'start_failure':
                    self.status = 'failed'
                    return False
                return True
            def offer(self, rgb):
                self.calls += 1
                if mode == 'offer_failure':
                    raise OSError('preview pipe unavailable')
                if mode == 'closed' and self.calls >= 2:
                    self.status = 'closed'
                    return False
                self.assert_recorded_copy = rgb is observed.writer.frames[-1][0]
                return True
            def finish(self): self.finished = True
            def snapshot(self):
                return {'enabled': True, 'status': self.status,
                        'error': 'missing player' if self.status == 'failed' else None}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            args = argparse.Namespace(command='record', config=None, duration=.12,
                                      output_dir=Path(directory), fps=45, gop_frames=None,
                                      preview_enabled=enabled, status_enabled=status_mode is not None)
            resolve_record_args(args)
            args.keep_awake = False
            mii = NS(MIICAM_FLAG_USB30_OVER_USB20=0x100,
                     MIICAM_OPTION_NUMBER_DROP_FRAME=1, MIICAM_OPTION_FRONTEND_FULL=2,
                     MIICAM_OPTION_BACKEND_FULL=3, Miicam=NS(Version=lambda: 'fake'),
                     MiicamFrameInfoV4=lambda: NS(v3=None), HRESULTException=OSError)
            patches = {
                'sdk': lambda: mii, 'select_camera': lambda unused: (NS(model=NS(flag=0)), Camera()),
                'configure': lambda *args: (4, 2, {}), 'describe': lambda *args: {},
                'require_disk': lambda *args: None, 'resolve_encoder': lambda *args: 'libx264',
                'Writer': Writer, 'validate_video_counts': validate,
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(capture, name, value))
            stack.enter_context(patch.object(capture.time, 'monotonic_ns', lambda: clock.ns))
            factory = stack.enter_context(patch('live_preview.LivePreview', Preview))
            stack.enter_context(patch('recording_status.RecordingStatus', Status))
            code = capture.run_capture(args)
            metadata = json.loads(next(Path(directory).glob('capture-*/capture.json')).read_text(encoding='utf-8'))
        if fail_capture:
            self.assertEqual(code, 1)
            self.assertEqual(metadata['status'], 'incomplete')
            return metadata, observed
        self.assertEqual(code, 0)
        self.assertEqual(metadata['status'], 'complete')
        self.assertEqual(metadata['frames_captured'], 6)
        self.assertEqual(len(observed.writer.frames), 6)
        self.assertEqual(metadata['sdk_sequence_gaps'], 0)
        if enabled:
            self.assertTrue(observed.preview.finished)
        else:
            self.assertIsNone(observed.preview)
            self.assertFalse(metadata['preview']['enabled'])
        return metadata, observed

    def test_disabled_preview_never_constructed(self):
        self.run_recording('normal', enabled=False)

    def test_manual_preview_close_keeps_all_recording_frames(self):
        metadata, observed = self.run_recording('closed')
        self.assertEqual(metadata['preview']['status'], 'closed')
        self.assertTrue(observed.preview.assert_recorded_copy)

    def test_preview_start_failure_nonfatal(self):
        metadata, observed = self.run_recording('start_failure')
        self.assertEqual(observed.preview.calls, 0)
        self.assertEqual(metadata['preview']['status'], 'failed')

    def test_unexpected_preview_offer_failure_nonfatal(self):
        metadata, observed = self.run_recording('offer_failure')
        self.assertEqual(observed.preview.calls, 1)
        self.assertIn('preview pipe unavailable', metadata['preview']['integration_error'])

    def test_status_lifecycle_and_frozen_capture_time_during_validation(self):
        metadata, observed = self.run_recording('normal', status_mode='normal')
        states = observed.status.states
        self.assertEqual([s['stage'] for s in states],
                         ['recording', 'finishing', 'validating', 'validating', 'validating', 'complete'])
        self.assertEqual(states[-1]['validated_segments'], 1)
        self.assertEqual(states[-1]['frames'], 6)
        self.assertEqual(states[3]['validating_segment'], 'fake.mkv')
        self.assertEqual(states[-1]['elapsed_s'], metadata['capture_elapsed_s'])
        self.assertEqual(len({s['elapsed_s'] for s in states[1:]}), 1)
        self.assertTrue(observed.status.finished)
        self.assertFalse(metadata['status_display']['worker_alive'])
        self.assertTrue(any('采集结束' in m for m in observed.status.messages))
        self.assertEqual(observed.status.write_timing_provider()['mean_ms'], 3.5)

    def test_status_failures_are_nonfatal(self):
        for mode in ('start_failure', 'publish_failure'):
            with self.subTest(mode=mode):
                metadata, observed = self.run_recording('normal', enabled=False, status_mode=mode)
                self.assertIn('failed', metadata['status_display']['error'])
                self.assertTrue(observed.status.finished)

    def test_capture_failure_never_reports_completed(self):
        metadata, observed = self.run_recording('normal', enabled=False,
                                                status_mode='normal', fail_capture=True)
        states = observed.status.states
        self.assertNotIn('complete', [s['stage'] for s in states])
        self.assertEqual(states[-1]['stage'], 'incomplete')
        self.assertIn('disconnected', states[-1]['error'])
        self.assertTrue(any('disconnected' in m for m in observed.status.messages))
        self.assertTrue(observed.status.finished)


if __name__ == '__main__':
    unittest.main()
