"""Portable status tests: no camera, GUI, encoder, or real recording."""

import io
import stat
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from recording_status import RecordingStatus, compute_metrics, format_status, sample_files


class BlockingStream:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, text):
        self.entered.set()
        self.release.wait(10)

    def flush(self):
        pass


class StatusTests(unittest.TestCase):
    def metrics(self, state=None, files=None, **kwargs):
        return compute_metrics(state or {}, files or {'files': [], 'bytes': 0, 'free_bytes': 100},
                               kwargs.pop('duration', 90), kwargs.pop('fps', 30),
                               kwargs.pop('segment', 45), kwargs.pop('now', 10),
                               kwargs.pop('previous', None))

    def test_time_and_progress_clamp_and_no_division_by_zero(self):
        metrics = self.metrics({'elapsed_s': -10, 'frames': -5})
        self.assertEqual(metrics['elapsed'], 0)
        self.assertEqual(metrics['remaining'], 90)
        self.assertEqual(metrics['percent'], 0)
        self.assertIsNone(metrics['measured_fps'])
        self.assertIsNone(metrics['average_mbps'])
        metrics = self.metrics({'elapsed_s': 100, 'frames': 3000})
        self.assertEqual(metrics['remaining'], 0)
        self.assertEqual(metrics['percent'], 100)
        metrics = self.metrics(duration=0, fps=0, segment=0)
        self.assertIsNone(metrics['percent'])
        self.assertIsNone(metrics['estimated_segments'])

    def test_measured_fps_and_cfr_tail_estimate(self):
        self.assertEqual(self.metrics()['estimated_segments'], 2)
        # The container segments by CFR frame count: 45.2 s at 30 fps has
        # 1,356 frames, so a nominal 45 s / 1,350-frame segment needs a tail.
        metrics = self.metrics({'elapsed_s': 45.2, 'frames': 1356}, duration=45.2)
        self.assertAlmostEqual(metrics['measured_fps'], 30)
        self.assertEqual(metrics['estimated_segments'], 2)
        metrics = self.metrics({'elapsed_s': 10, 'frames': 100}, duration=90)
        self.assertEqual(metrics['estimated_segments'], 1)

    def test_explicit_measured_cadence_overrides_elapsed_fallback(self):
        metrics = self.metrics({'elapsed_s': 10, 'frames': 100, 'measured_fps': 30})
        self.assertEqual(metrics['measured_fps'], 30)
        self.assertEqual(metrics['estimated_segments'], 2)
        self.assertEqual(self.metrics({'elapsed_s': 10, 'frames': 100})['measured_fps'], 10)
        self.assertEqual(self.metrics({'elapsed_s': 10, 'frames': 100, 'measured_fps': 0})['measured_fps'], 0)

    def test_no_frames_yet_uses_requested_rate_for_estimate(self):
        metrics = self.metrics({'elapsed_s': 0.001, 'frames': 0})
        self.assertEqual(metrics['estimated_segments'], 2)
        self.assertEqual(metrics['estimated_additional'], 2)

    def test_handle_metadata_bypasses_stale_path_size_and_reads_no_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'active.mkv'
            with path.open('wb', buffering=0) as writer:
                writer.write(b'x' * 128)
                with patch('recording_status.Path.stat', return_value=SimpleNamespace(st_size=0, st_mode=stat.S_IFDIR)):
                    self.assertEqual(sample_files(folder)['bytes'], 128)
                    writer.write(b'x' * 128)
                    self.assertEqual(sample_files(folder)['bytes'], 256)

    def test_handle_metadata_failure_falls_back_to_stat(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'active.mkv').write_bytes(b'123')
            with patch('recording_status.os.fstat', side_effect=OSError('sharing denied')):
                self.assertEqual(sample_files(folder)['bytes'], 3)
            with patch('recording_status.Path.open', side_effect=OSError('sharing denied')):
                self.assertEqual(sample_files(folder)['bytes'], 3)

    def test_growth_average_and_stationary_validation_samples(self):
        files = {'files': [], 'bytes': 2_000_000, 'free_bytes': 0}
        metrics = self.metrics({'elapsed_s': 10}, files, previous=(8, 1_000_000))
        self.assertEqual(metrics['recent_mbps'], 4)
        self.assertEqual(metrics['average_mbps'], 1.6)
        self.assertIsNone(self.metrics({'elapsed_s': 10}, files)['recent_mbps'])
        metrics = self.metrics({'stage': 'validating', 'elapsed_s': 10}, files,
                               previous=(8, 2_000_000))
        self.assertEqual(metrics['recent_mbps'], 0)
        self.assertEqual(metrics['average_mbps'], 1.6)
        self.assertIsNone(self.metrics(files=files, now=8, previous=(8, 0))['recent_mbps'])
        self.assertEqual(self.metrics(files=files, previous=(8, 3_000_000))['recent_mbps'], 0)

    def test_actual_files_closed_current_and_estimated_additional(self):
        with tempfile.TemporaryDirectory() as folder:
            for index, size in enumerate((12, 23)):
                (Path(folder) / f'{index:03d}.mkv').write_bytes(b'x' * size)
            (Path(folder) / 'ignore.txt').write_text('ignored')
            files = sample_files(folder)
            self.assertEqual(files['bytes'], 35)
            self.assertEqual(len(files['files']), 2)
            self.assertGreater(files['free_bytes'], 0)
            for stage in ('starting', 'recording', 'finishing'):
                metrics = self.metrics({'stage': stage}, files)
                self.assertEqual(metrics['count'], 2)
                self.assertEqual(metrics['closed'], 1)
                self.assertEqual(metrics['current'], '001.mkv')
                self.assertEqual(metrics['estimated_additional'], 0)
            for stage in ('validating', 'complete'):
                metrics = self.metrics({'stage': stage}, files)
                self.assertEqual(metrics['closed'], 2)
                self.assertIsNone(metrics['current'])
            metrics = self.metrics({'stage': 'incomplete'}, files)
            self.assertIsNone(metrics['closed'])
            self.assertIn('未确认', format_status({'stage': 'incomplete'}, metrics))

    def test_no_files_and_metadata_errors_are_displayable(self):
        with tempfile.TemporaryDirectory() as folder:
            files = sample_files(folder)
            self.assertEqual(files['files'], [])
            self.assertEqual(files['bytes'], 0)
            self.assertEqual(self.metrics(files=files)['closed'], 0)
            with patch('recording_status.Path.glob', side_effect=OSError('denied')), \
                    patch('recording_status.shutil.disk_usage', side_effect=OSError('denied')):
                files = sample_files(folder)
            self.assertIsNone(files['files'])
            self.assertIsNone(files['bytes'])
            self.assertIsNone(files['free_bytes'])
            self.assertIn('不可用', format_status({}, self.metrics(files=files)))
            (Path(folder) / 'part.mkv').write_bytes(b'x')
            with patch('recording_status.Path.stat', side_effect=OSError('denied')), \
                    patch('recording_status.Path.open', side_effect=OSError('denied')):
                files = sample_files(folder)
            self.assertIsNone(files['bytes'])

    def test_validation_text_is_not_completion(self):
        state = {'stage': 'validating', 'validated_segments': 1,
                 'validating_segment': '002.mkv', 'elapsed_s': 90}
        text = format_status(state, self.metrics(state))
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn('[校验中]', text)
        self.assertNotIn('[完成]', text)
        self.assertIn('正在校验 002.mkv', text)
        self.assertIn('预计', text)
        self.assertIn('已生成 0 段', text)
        self.assertIn('预计总计 0 段 / 再生成 0 段', text)
        self.assertIn('容器增长', text)

    def test_initial_state_displays_preparation(self):
        status = RecordingStatus('unused', 90, 30, 45)
        self.assertEqual(status._state['stage'], 'starting')
        self.assertIn('[准备录制]', format_status(status._state, self.metrics(status._state)))

    def test_write_latency_text_distinguishes_empty_window_and_inflight(self):
        timing = {'sample_count': 45, 'mean_ms': 6.234, 'in_progress_ms': None}
        text = format_status({'write_timing': timing}, self.metrics())
        self.assertIn('写入近1s均值 6.23 ms/帧（45帧）', text)
        timing = {'sample_count': 0, 'mean_ms': None, 'in_progress_ms': 1200.5}
        text = format_status({'write_timing': timing}, self.metrics())
        self.assertIn('无完成帧', text)
        self.assertIn('当前写入已等 1200.50 ms', text)
        self.assertNotIn('0.00 ms/帧', text)

    def test_write_timing_sampled_by_worker_on_each_refresh(self):
        caller_ids = []
        def provider():
            caller_ids.append(threading.get_ident())
            return {'sample_count': 2, 'mean_ms': 4.5, 'in_progress_ms': None}
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            status = RecordingStatus(folder, 90, 30, 45, stream=stream,
                                     write_timing_provider=provider).start()
            status.publish(stage='complete')
            status.finish()
            self.assertTrue(caller_ids)
            self.assertNotIn(threading.get_ident(), caller_ids)
            self.assertIn('4.50 ms/帧（2帧）', stream.getvalue())
            self.assertFalse(status.snapshot()['worker_alive'])

    def test_write_timing_provider_error_keeps_status_working(self):
        def provider():
            raise RuntimeError('timing sample failed')
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            status = RecordingStatus(folder, 90, 30, 45, stream=stream,
                                     write_timing_provider=provider).start()
            status.publish(stage='complete')
            status.finish()
            self.assertIn('[完成]', stream.getvalue())
            self.assertIn('写入近1s均值 不可用', stream.getvalue())
            self.assertIn('timing sample failed', status.snapshot()['error'])

    def test_publish_does_no_io_and_stays_responsive_with_blocked_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = BlockingStream()
            status = RecordingStatus(folder, 90, 30, 45, stream=stream).start()
            self.assertTrue(stream.entered.wait(2))
            try:
                with patch('recording_status.sample_files', side_effect=AssertionError('main thread I/O')):
                    start = time.monotonic()
                    for index in range(1000):
                        status.publish(frames=index)
                    self.assertLess(time.monotonic() - start, 0.5)
                start = time.monotonic()
                status.finish()
                self.assertLess(time.monotonic() - start, 1.2)
                self.assertTrue(status._thread.daemon)
                self.assertTrue(status.snapshot()['worker_alive'])
                self.assertIn('did not stop within 1 second', status.snapshot()['error'])
            finally:
                stream.release.set()
                status._thread.join(2)

    def test_finish_emits_latest_state_from_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            status = RecordingStatus(folder, 90, 30, 45, interval_seconds=60, stream=stream)
            status.start()
            status.publish(stage='complete', elapsed_s=90, frames=2700, validated_segments=2)
            status.finish()
            self.assertFalse(status._thread.is_alive())
            self.assertEqual(status.snapshot(), {'worker_alive': False})
            self.assertIn('[完成]', stream.getvalue().splitlines()[-2])
            self.assertIn('已校验 2/', stream.getvalue().splitlines()[-1])

    def test_permanent_messages_and_final_state_are_flushed_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            status = RecordingStatus(folder, 90, 30, 45, interval_seconds=60, stream=stream)
            status.start()
            status.notice('永久错误消息')
            status.publish(stage='incomplete', error='capture failed')
            status.finish()
            text = stream.getvalue()
            self.assertEqual(text.count('永久错误消息'), 1)
            self.assertLess(text.index('永久错误消息'), text.rindex('[未完成'))
            self.assertNotIn('\x1b', text)  # redirected logs contain plain text

    def test_notice_does_not_wait_for_blocked_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = BlockingStream()
            status = RecordingStatus(folder, 90, 30, 45, stream=stream).start()
            self.assertTrue(stream.entered.wait(2))
            try:
                before = time.monotonic()
                status.notice('nonblocking message')
                self.assertLess(time.monotonic() - before, .1)
            finally:
                stream.release.set()
                status.finish()

    def test_broken_terminal_is_best_effort(self):
        class BrokenStream:
            def write(self, value):
                raise BrokenPipeError()

        with tempfile.TemporaryDirectory() as folder:
            status = RecordingStatus(folder, 0, 0, 0, stream=BrokenStream()).start()
            status.publish(stage='incomplete', error='writer failed')
            status.finish()
            self.assertFalse(status._thread.is_alive())
            self.assertIn('BrokenPipeError', status.snapshot()['error'])

    def test_unexpected_sampling_error_is_reported_without_propagating(self):
        status = RecordingStatus('unused', 90, 30, 45, stream=io.StringIO())
        with patch('recording_status.sample_files', side_effect=RuntimeError('sampling failed')):
            status.start()
            status.finish()
        self.assertFalse(status.snapshot()['worker_alive'])
        self.assertIn('sampling failed', status.snapshot()['error'])


if __name__ == '__main__':
    unittest.main()
