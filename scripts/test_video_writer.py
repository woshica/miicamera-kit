"""Synthetic, camera-free integration checks for the continuous video writer."""

import csv
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from video_writer import Writer, _WriteTiming, resolve_encoder
from x265_settings import validate_x265_params


KIT = Path(__file__).resolve().parents[1]
FFMPEG = KIT / "runtime/ffmpeg/bin/ffmpeg.exe"
FFPROBE = KIT / "runtime/ffmpeg/bin/ffprobe.exe"


class WriteTimingTests(unittest.TestCase):
    """Deterministic timing checks; no camera or encoder process is started."""

    def setUp(self):
        self.now_ns = 0
        self.timing = _WriteTiming(lambda: self.now_ns)

    def test_rolling_window_uses_completion_and_expires_at_boundary(self):
        self.timing.complete(0, 100_000_000, True)
        self.timing.complete(400_000_000, 700_000_000, True)
        self.now_ns = 1_000_000_000
        self.assertEqual(self.timing.snapshot(), {
            "window_seconds": 1.0, "sample_count": 2,
            "mean_ms": 200.0, "in_progress_ms": None})
        self.now_ns = 1_100_000_000
        self.assertEqual(self.timing.snapshot()["sample_count"], 1)
        self.assertEqual(self.timing.snapshot()["mean_ms"], 300.0)
        self.now_ns = 1_700_000_000
        self.assertIsNone(self.timing.snapshot()["mean_ms"])
        self.assertEqual(self.timing.snapshot()["sample_count"], 0)

    def test_long_write_contributes_its_entire_duration_on_completion(self):
        self.timing.begin()
        self.now_ns = 2_500_000_000
        self.assertEqual(self.timing.snapshot()["in_progress_ms"], 2500.0)
        self.assertIsNone(self.timing.snapshot()["mean_ms"])
        self.timing.complete(0, self.now_ns, True)
        self.assertEqual(self.timing.snapshot()["mean_ms"], 2500.0)
        self.assertIsNone(self.timing.snapshot()["in_progress_ms"])

    def test_history_is_bounded_and_failed_write_clears_progress(self):
        self.timing.begin()
        self.timing.complete(0, 1, False)
        self.assertEqual(self.timing.snapshot()["sample_count"], 0)
        self.assertIsNone(self.timing.snapshot()["in_progress_ms"])
        for end_ns in range(1, 5001):
            self.timing.complete(end_ns - 1, end_ns, True)
        self.now_ns = 5000
        self.assertEqual(len(self.timing._samples), self.timing.MAX_SAMPLES)
        self.assertEqual(self.timing.snapshot()["sample_count"], 4096)

    def _writer(self, pipe, *, csv_advance_ns=0):
        writer = Writer("unused-timing-test", 1, 1, 1, 1, "libx264")
        writer._write_timing = self.timing
        writer.process = SimpleNamespace(stdin=pipe, poll=lambda: None,
                                         wait=lambda timeout: 0, kill=lambda: None)
        writer._csv_file = io.StringIO()

        def write_row(row):
            self.now_ns += csv_advance_ns

        writer._csv_writer = SimpleNamespace(writerow=write_row)

        def open_index(number):
            self.now_ns += 4_000_000_000  # Index setup is outside the measured write.
            writer.segments.append({"number": number, "frames_submitted": 0,
                                    "first_host_monotonic_ns": None,
                                    "last_host_monotonic_ns": None})

        writer._open_index = open_index
        writer.submit(b"rgb", {"host_monotonic_ns": 1})
        writer.stop_event.set()
        return writer

    def test_run_measures_only_actual_write_and_mean_expires_during_later_work(self):
        def write(rgb):
            self.now_ns += 250_000_000
            return len(rgb)

        writer = self._writer(SimpleNamespace(write=write, close=lambda: None),
                              csv_advance_ns=900_000_000)
        self.now_ns = 3_000_000_000  # Simulate time already spent in the queue.
        writer.run()
        self.assertIsNone(writer.error)
        self.assertEqual(writer.write_timing_snapshot()["mean_ms"], 250.0)
        self.now_ns += 100_000_000
        self.assertEqual(writer.write_timing_snapshot()["sample_count"], 0)
        self.assertIsNone(writer.write_timing_snapshot()["mean_ms"])

    def test_run_failed_and_short_writes_do_not_count(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                self.timing = _WriteTiming(lambda: self.now_ns)

                def write(rgb):
                    self.now_ns += 500_000_000
                    if fail:
                        raise BrokenPipeError("test pipe failure")
                    return len(rgb) - 1

                writer = self._writer(SimpleNamespace(write=write, close=lambda: None))
                writer.run()
                self.assertEqual(isinstance(writer.error, BrokenPipeError), fail)
                self.assertEqual(writer.write_timing_snapshot()["sample_count"], 0)
                self.assertIsNone(writer.write_timing_snapshot()["in_progress_ms"])

    def test_snapshot_does_not_wait_for_blocked_pipe_or_hold_lock_across_write(self):
        entered = threading.Event()
        release = threading.Event()

        def write(rgb):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release pipe")
            return len(rgb)

        writer = self._writer(SimpleNamespace(write=write, close=lambda: None))
        worker = threading.Thread(target=writer.run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.now_ns += 2_000_000_000
            result = []
            snapshot_thread = threading.Thread(target=lambda: result.append(writer.write_timing_snapshot()))
            snapshot_thread.start()
            snapshot_thread.join(timeout=1)
            self.assertFalse(snapshot_thread.is_alive(), "snapshot blocked behind pipe write")
            self.assertEqual(result[0]["in_progress_ms"], 2000.0)
            self.assertEqual(result[0]["sample_count"], 0)
            acquired = self.timing._lock.acquire(blocking=False)
            self.assertTrue(acquired, "timing lock held across pipe write")
            if acquired:
                self.timing._lock.release()
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(writer.error)
        self.assertEqual(writer.write_timing_snapshot()["mean_ms"], 2000.0)
        self.assertIsNone(writer.write_timing_snapshot()["in_progress_ms"])


class VideoWriterTests(unittest.TestCase):
    def test_switching_to_nvenc_ignores_inactive_x265_gop_limit(self):
        with tempfile.TemporaryDirectory() as root:
            writer = Writer(root, 128, 128, 45, 20, 'hevc_nvenc',
                            gop_frames=10, b_frames=3, nvenc_cq=30,
                            x265_params={'min-keyint': 25})
            self.assertEqual(writer.gop_frames, 10)
            self.assertNotIn('x265_params', writer.encoding_settings)
            with self.assertRaisesRegex(ValueError, 'min-keyint'):
                Writer(root, 128, 128, 45, 20, 'libx265',
                       gop_frames=10, b_frames=3,
                       x265_params={'min-keyint': 25})

    def test_validation_and_resolution(self):
        self.assertEqual(resolve_encoder("libx264", FFMPEG), "libx264")
        self.assertIn(resolve_encoder("auto", FFMPEG), ("libx264", "h264_nvenc"))
        with self.assertRaises(ValueError):
            resolve_encoder("unknown", FFMPEG)
        with tempfile.TemporaryDirectory() as root:
            for options in ({"gop_frames": 0}, {"gop_frames": 1.5},
                            {"b_frames": 1}, {"crf": 52}, {"nvenc_cq": -1},
                            {"nvenc_preset": "slow"},
                            {"rate_control": "bad"},
                            {"bitrate_mbps": 0}, {"bitrate_mbps": -1},
                            {"bitrate_mbps": True}, {"bitrate_mbps": "24"},
                            {"bitrate_mbps": float("inf")},
                            {"maxrate_mbps": float("nan")},
                            {"buffer_mbits": 0},
                            {"bitrate_mbps": 27, "maxrate_mbps": 26}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    Writer(root, 128, 128, 7, 3, "libx264", FFMPEG, **options)
            writer = Writer(root, 128, 128, 7, 300, "libx264", FFMPEG)
            self.assertEqual(writer.gop_frames, 14)
            self.assertEqual(writer.frames_per_segment, 2100)
            self.assertEqual(writer.encoding_settings["rate_control"], "quality")
            self.assertEqual(writer.encoding_settings["crf"], 18)
            self.assertNotIn("bitrate_mbps", writer.encoding_settings)
            hevc = Writer(root, 128, 128, 7, 3, "hevc_nvenc", FFMPEG,
                          gop_frames=250, b_frames=3, nvenc_cq=23,
                          nvenc_preset="p5")
            self.assertEqual(hevc.encoding_settings["preset"], "p5")
            self.assertEqual(hevc.encoding_settings["b_frames"], 3)
            self.assertEqual(hevc.encoding_settings["queue_frames"], 16)
            self.assertEqual(writer.encoding_settings["queue_frames"], 8)
            x265 = Writer(root, 128, 128, 7, 3, "libx265", FFMPEG,
                          gop_frames=25, b_frames=3, crf=21,
                          x265_params={"min-keyint": 5, "open-gop": 1})
            self.assertEqual(x265.encoding_settings["queue_frames"], 16)
            self.assertEqual(x265.encoding_settings["preset"], "superfast")
            self.assertEqual(x265.encoding_settings["crf"], 21)
            self.assertEqual(x265.encoding_settings["x265_params"],
                             {"min-keyint": 5, "open-gop": 1})
            for options in ({"rate_control": "capped_vbr"}, {"b_frames": 4},
                            {"x265_preset": "invalid"},
                            {"x265_params": {"keyint": 250}},
                            {"x265_params": {"open-gop": "1:crf=0"}}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    Writer(root, 128, 128, 7, 3, "libx265", FFMPEG, **options)
        self.assertEqual(validate_x265_params({"deblock": "0,0", "aq-strength": 0}),
                         {"deblock": "0,0", "aq-strength": 0})
        self.assertEqual(resolve_encoder("libx265", FFMPEG), "libx265")
        with self.assertRaisesRegex(RuntimeError, "libx265 is unavailable"):
            resolve_encoder("libx265", FFMPEG.with_name("missing-ffmpeg.exe"))

    def test_hevc_4k45_paced_startup(self):
        try:
            resolve_encoder("hevc_nvenc", FFMPEG)
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as root:
            writer = Writer(root, 3840, 2160, 45, 1, "hevc_nvenc", FFMPEG,
                            gop_frames=250, b_frames=3, nvenc_cq=23,
                            nvenc_preset="p5")
            frame = bytes([32, 128, 220]) * (3840 * 2160)
            writer.start()
            start = time.perf_counter()
            for index in range(90):
                remaining = start + index / 45 - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                writer.submit(frame, {"sdk_seq": index,
                                      "sdk_timestamp_us": index * 22222,
                                      "host_utc_ns": index,
                                      "host_monotonic_ns": index + 1,
                                      "sdk_flag": 0, "exposure_us": 1000,
                                      "gain": 1})
            writer.finish()
            self.assertEqual([s["frames_submitted"] for s in writer.segments],
                             [45, 45])
            for segment in writer.segments:
                with (Path(root) / segment["frames_csv"]).open(newline="") as stream:
                    rows = list(csv.DictReader(stream))
                probe = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                                        "-show_frames", "-show_entries", "frame=key_frame",
                                        "-of", "json", str(Path(root) / segment["video"])],
                                       capture_output=True, text=True, check=True, timeout=30)
                frames = json.loads(probe.stdout)["frames"]
                self.assertEqual(len(frames), len(rows))
                self.assertTrue(frames[0]["key_frame"])

    def test_hevc_b_frames_segment_identity(self):
        try:
            self.assertEqual(resolve_encoder("hevc_nvenc", FFMPEG), "hevc_nvenc")
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self._check_b_frames_segment_identity("hevc_nvenc", {"nvenc_cq": 23,
                                                             "nvenc_preset": "p5"})

    def test_x265_b_frames_segment_identity(self):
        try:
            self.assertEqual(resolve_encoder("libx265", FFMPEG), "libx265")
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self._check_b_frames_segment_identity("libx265", {"crf": 21,
                                                          "x265_preset": "superfast",
                                                          "x265_params": {
                                                              "frame-threads": 1, "pools": 20,
                                                              "ref": 1, "min-keyint": 5,
                                                              "b-adapt": 0, "rc-lookahead": 10,
                                                              "scenecut": 40, "ctu": 32,
                                                              "min-cu-size": 8, "rect": 0,
                                                              "amp": 0, "max-tu-size": 32,
                                                              "tu-inter-depth": 1,
                                                              "tu-intra-depth": 1, "me": 1,
                                                              "subme": 1, "merange": 57,
                                                              "weightp": 0, "weightb": 0,
                                                              "sao": 0, "rd": 2,
                                                              "aq-mode": 1, "aq-strength": 0,
                                                              "cutree": 1, "open-gop": 1,
                                                              "b-pyramid": 1,
                                                              "strong-intra-smoothing": 1,
                                                              "psy-rd": 2,
                                                              "deblock": "0,0"}})

    def _check_b_frames_segment_identity(self, encoder, settings):
        with tempfile.TemporaryDirectory() as root:
            width = height = 256
            writer = Writer(root, width, height, 7, 3, encoder, FFMPEG,
                            gop_frames=25, b_frames=3, **settings)
            writer.start()
            for index in range(70):
                # Eight high-contrast bars encode an index that survives lossy encode.
                bars = bytes(255 if index & (1 << bit) else 0
                             for bit in range(8) for _ in range(32))
                rgb = b"".join(bytes([value]) * 3 for value in bars) * height
                meta = {"sdk_seq": index, "sdk_timestamp_us": index * 142857,
                        "host_utc_ns": index, "host_monotonic_ns": index + 1,
                        "sdk_flag": 0, "exposure_us": 1000, "gain": 1}
                while True:
                    try:
                        writer.submit(rgb, meta)
                        break
                    except RuntimeError as exc:
                        if "queue overflow" not in str(exc):
                            raise
                        time.sleep(0.02)
            writer.finish()
            self.assertEqual([s["frames_submitted"] for s in writer.segments],
                             [21, 21, 21, 7])
            all_pictures = []
            decoded_ids = []
            for segment in writer.segments:
                path = Path(root) / segment["video"]
                with (Path(root) / segment["frames_csv"]).open(newline="") as stream:
                    rows = list(csv.DictReader(stream))
                probe = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                                        "-show_frames", "-show_entries", "frame=key_frame,pict_type",
                                        "-of", "json", str(path)], capture_output=True,
                                       text=True, check=True, timeout=20)
                pictures = json.loads(probe.stdout)["frames"]
                self.assertEqual(len(pictures), len(rows))
                self.assertTrue(pictures[0]["key_frame"])
                all_pictures.extend(item["pict_type"] for item in pictures)
                decoded = subprocess.run([str(FFMPEG), "-v", "error", "-i", str(path),
                                          "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                                         capture_output=True, check=True, timeout=20).stdout
                frame_bytes = width * height
                self.assertEqual(len(decoded), len(rows) * frame_bytes)
                for offset, row in enumerate(rows):
                    frame = decoded[offset * frame_bytes:(offset + 1) * frame_bytes]
                    actual_id = sum((frame[128 * width + bit * 32 + 16] > 128) << bit
                                    for bit in range(8))
                    decoded_ids.append(actual_id)
                    self.assertEqual(actual_id, int(row["sdk_seq"]))
            self.assertIn("B", all_pictures)
            self.assertEqual(decoded_ids, list(range(70)))

    def test_both_installed_encoders(self):
        encoders = ["libx264"]
        try:
            resolve_encoder("h264_nvenc", FFMPEG)
        except RuntimeError:
            pass
        else:
            encoders.append("h264_nvenc")
        for encoder in encoders:
            for rate_control in ("quality", "capped_vbr"):
                with self.subTest(encoder=encoder, rate_control=rate_control), tempfile.TemporaryDirectory() as root:
                    self._check_encode(root, encoder, rate_control)

    def _check_encode(self, root, encoder, rate_control):
        writer = Writer(root, 256, 256, 7, 3, encoder, FFMPEG, gop_frames=5,
                        rate_control=rate_control, bitrate_mbps=2.5,
                        maxrate_mbps=3, buffer_mbits=6)
        self.assertEqual(writer.encoding_settings["gop_frames"], 5)
        self.assertEqual(writer.encoding_settings["rate_control"], rate_control)
        if rate_control == "capped_vbr":
            self.assertEqual(writer.encoding_settings["bitrate_mbps"], 2.5)
            self.assertEqual(writer.encoding_settings["maxrate_mbps"], 3)
            self.assertEqual(writer.encoding_settings["buffer_mbits"], 6)
            self.assertNotIn("crf", writer.encoding_settings)
            self.assertNotIn("nvenc_cq", writer.encoding_settings)
        else:
            self.assertIn("crf" if encoder == "libx264" else "nvenc_cq",
                          writer.encoding_settings)
            self.assertNotIn("bitrate_mbps", writer.encoding_settings)
        writer.start()
        frame = bytes([50, 100, 150]) * (256 * 256)
        for index in range(49):
            meta = {"sdk_seq": index, "sdk_timestamp_us": index * 142857,
                    "host_utc_ns": index, "host_monotonic_ns": index + 1,
                    "sdk_flag": 0, "exposure_us": 1000, "gain": 1}
            while True:
                try:
                    writer.submit(frame, meta)
                    break
                except RuntimeError as exc:
                    if "queue overflow" not in str(exc):
                        raise
                    time.sleep(0.02)
        writer.finish()
        self.assertEqual([s["frames_submitted"] for s in writer.segments], [21, 21, 7])
        all_keys = []
        count = 0
        for segment in writer.segments:
            with (Path(root) / segment["frames_csv"]).open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), segment["frames_submitted"])
            self.assertEqual([int(row["encoded_index"]) for row in rows], list(range(len(rows))))
            probe = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                                    "-show_frames", "-show_entries", "frame=key_frame,pict_type",
                                    "-of", "json", str(Path(root) / segment["video"])],
                                   capture_output=True, text=True, check=True, timeout=20)
            frames = json.loads(probe.stdout)["frames"]
            self.assertEqual(len(frames), len(rows))
            self.assertTrue(frames[0]["key_frame"])
            self.assertNotIn("B", [item["pict_type"] for item in frames])
            all_keys.extend(count + i for i, item in enumerate(frames) if item["key_frame"])
            count += len(frames)
        self.assertEqual(count, 49)
        self.assertTrue({0, 5, 10, 15, 20, 21, 25, 30, 35, 40, 42, 45}.issubset(all_keys))
        self.assertTrue(all(b - a <= 5 for a, b in zip(all_keys, all_keys[1:])))


if __name__ == "__main__":
    unittest.main()
