"""Camera- and GUI-free preview lifecycle, pixel, and backpressure checks."""

from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_preview import LivePreview, TITLE, downsample_rgb


class FakePipe:
    def __init__(self, blocked=False, broken=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = blocked
        self.broken = broken
        self.closed = False
        self.frames = []

    def write(self, data):
        self.entered.set()
        if self.blocked:
            self.release.wait(5)
        if self.broken or self.closed:
            raise BrokenPipeError("player closed")
        self.frames.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, blocked=False, broken=False, needs_kill=False):
        self.stdin = FakePipe(blocked, broken)
        self.returncode = None
        self.needs_kill = needs_kill
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        if not self.needs_kill:
            self.kill()

    def kill(self):
        self.killed = True
        self.returncode = 0
        self.stdin.closed = True
        self.stdin.release.set()

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-player", timeout)
        return self.returncode


def eventually(predicate):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class LivePreviewTests(unittest.TestCase):
    def test_rgb_sampling_retains_channels_and_handles_odd_size(self):
        rgb = bytes(channel for y in range(3) for x in range(5)
                    for channel in (x, y, x + 10 * y))
        expected = bytes(channel for y in (0, 2) for x in (0, 2, 4)
                         for channel in (x, y, x + 10 * y))
        self.assertEqual(downsample_rgb(rgb, 5, 3, 2), expected)
        self.assertEqual(downsample_rgb(rgb, 5, 3, 1), rgb)

    def test_unavailable_player_is_nonfatal_and_remembers_error(self):
        preview = LivePreview(4, 2)
        with patch("live_preview.subprocess.Popen", side_effect=FileNotFoundError("missing ffplay")):
            self.assertFalse(preview.start())
        self.assertFalse(preview.offer(bytes(24)))
        state = preview.finish()
        self.assertEqual(state["status"], "failed")
        self.assertIn("missing ffplay", state["error"])

    def test_throttles_and_pipes_only_reduced_rgb(self):
        process = FakeProcess()
        preview = LivePreview(5, 3, fps=5, max_width=3)
        rgb = bytes(range(45))
        with patch("live_preview.subprocess.Popen", return_value=process) as popen:
            self.assertTrue(preview.start())
            with patch("live_preview.time.monotonic", return_value=100):
                self.assertTrue(preview.offer(rgb))
                self.assertFalse(preview.offer(rgb))
            self.assertTrue(eventually(lambda: preview.snapshot()["frames_sent_to_player"] == 1))
            state = preview.finish()
        self.assertEqual(process.stdin.frames, [downsample_rgb(rgb, 5, 3, 2)])
        self.assertEqual(len(process.stdin.frames[0]), 3 * 2 * 3)
        self.assertEqual(rgb, bytes(range(45)))
        self.assertEqual(state["frames_throttled"], 1)
        args, kwargs = popen.call_args
        self.assertIn(TITLE, args[0])
        self.assertIn("3x2", args[0])
        self.assertEqual(args[0][args[0].index("-probesize") + 1], "32")
        self.assertEqual(args[0][args[0].index("-analyzeduration") + 1], "0")
        self.assertEqual(args[0][args[0].index("-sync") + 1], "ext")
        self.assertIn("-framedrop", args[0])
        self.assertEqual(kwargs["bufsize"], 0)
        self.assertFalse(state["worker_alive"])

    def test_blocked_pipe_queue_latest_only_and_bounded_shutdown(self):
        process = FakeProcess(blocked=True, needs_kill=True)
        preview = LivePreview(2, 1, fps=5)
        with patch("live_preview.subprocess.Popen", return_value=process):
            self.assertTrue(preview.start())
            self.assertTrue(preview.offer(bytes([1]) * 6))
            self.assertTrue(process.stdin.entered.wait(1))
            start = time.monotonic()
            with patch("live_preview.time.monotonic", side_effect=[start + 1, start + 2, start + 3]):
                for value in (2, 3, 4):
                    self.assertTrue(preview.offer(bytes([value]) * 6))
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(preview.snapshot()["queue_depth"], 1)
            self.assertEqual(preview._pending[0], bytes([4]) * 6)
            self.assertEqual(preview.snapshot()["frames_replaced"], 2)
            start = time.monotonic()
            state = preview.finish()
            self.assertLess(time.monotonic() - start, 3)
            self.assertTrue(process.killed)
            self.assertFalse(state["worker_alive"])
            self.assertEqual(state["queue_depth"], 0)
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
            preview.finish()

    def test_broken_pipe_stops_only_preview(self):
        process = FakeProcess(broken=True)
        preview = LivePreview(2, 1)
        with patch("live_preview.subprocess.Popen", return_value=process):
            self.assertTrue(preview.start())
            preview.offer(bytes(6))
            self.assertTrue(eventually(lambda: preview.snapshot()["status"] == "failed"))
            self.assertFalse(preview.offer(bytes(6)))
            state = preview.finish()
        self.assertIn("player closed", state["error"])
        self.assertEqual(state["frames_sent_to_player"], 0)

    def test_window_exit_with_no_frames_is_detected(self):
        process = FakeProcess()
        preview = LivePreview(2, 1)
        with patch("live_preview.subprocess.Popen", return_value=process):
            self.assertTrue(preview.start())
            process.returncode = 0
            self.assertTrue(eventually(lambda: preview.snapshot()["status"] == "closed"))
            self.assertFalse(preview.offer(bytes(6)))
            preview.finish()

    def test_nonzero_player_exit_records_failure(self):
        process = FakeProcess()
        preview = LivePreview(2, 1)
        with patch("live_preview.subprocess.Popen", return_value=process):
            self.assertTrue(preview.start())
            process.returncode = 23
            self.assertTrue(eventually(lambda: preview.snapshot()["status"] == "failed"))
            self.assertIn("code 23", preview.snapshot()["error"])
            self.assertEqual(preview.finish()["player_returncode"], 23)

    def test_clean_window_close_while_writing_is_normal_close(self):
        process = FakeProcess()
        preview = LivePreview(2, 1)

        def close_during_write(data):
            process.returncode = 0
            raise BrokenPipeError("window closed")

        process.stdin.write = close_during_write
        with patch("live_preview.subprocess.Popen", return_value=process):
            self.assertTrue(preview.start())
            preview.offer(bytes(6))
            self.assertTrue(eventually(lambda: preview.snapshot()["status"] == "closed"))
            state = preview.finish()
            self.assertIsNone(state["error"])
            self.assertFalse(state["worker_alive"])
            self.assertEqual(state["frames_sent_to_player"], 0)


if __name__ == "__main__":
    unittest.main()
