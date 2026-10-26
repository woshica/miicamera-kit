"""No encoder/GPU/camera is accessed by these tests."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import encoder_diagnostics as diagnostics


PROFILE = {"fps": 45, "gop_frames": 90, "b_frames": 3, "segment_seconds": 300,
           "crf": 21, "nvenc_cq": 23, "nvenc_preset": "p5", "x265_preset": "fast",
           "x265_params": {"pools": 2, "min-keyint": 45}, "rate_control": "quality"}


def outcome(stdout="", status="completed", stderr="", elapsed=1):
    return {"status": status, "error": None if status == "completed" else status,
            "returncode": 0 if status == "completed" else 1, "stdout": stdout,
            "stderr": stderr, "elapsed_s": elapsed}


def listed(*names):
    return outcome("\n".join(f" V....D {name} Description" for name in names))


def probed(codec="h264", frames=3):
    return outcome(json.dumps({"streams": [{"codec_name": codec, "width": 256,
                                            "height": 256, "nb_read_frames": str(frames)}]}))


class ProbeTests(unittest.TestCase):
    def execute(self, results):
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnostics, "_run", side_effect=results) as run:
            rows = diagnostics.probe_encoders("ffmpeg", "ffprobe", PROFILE, folder)
            return rows, run.call_args_list

    def test_compile_flags_alone_do_not_imply_functional(self):
        rows, calls = self.execute([listed("h264_nvenc"), outcome(status="failed", stderr="driver unavailable")])
        row = rows[2]
        self.assertTrue(row["compiled"])
        self.assertFalse(row["functional"])
        self.assertIn("driver unavailable", row["error"])
        self.assertEqual(row["benchmark"]["status"], "skipped")
        self.assertEqual(len(calls), 2)

    def test_no_gpu_name_guess_and_all_eight_names(self):
        rows, calls = self.execute([listed()])
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(row["supported_by_recorder"] for row in rows), 4)
        self.assertTrue(all(row["compiled"] is False for row in rows))
        self.assertEqual(len(calls), 1)

    def test_functional_decode_then_completed_benchmark(self):
        rows, calls = self.execute([listed("libx264"), outcome(), probed(),
                                    outcome("frame=45\nout_time=00:00:01.000000\nprogress=end\n", elapsed=.5)])
        row = rows[0]
        self.assertTrue(row["functional"])
        self.assertEqual(row["capability"]["frames"], 3)
        self.assertEqual(row["benchmark"]["fps"], 90)
        self.assertTrue(row["benchmark"]["meets_target"])
        command = calls[-1].args[0]
        self.assertIn("testsrc2=size=3840x2160:rate=45", command)
        self.assertIn("format=rgb24,format=yuv420p", command)
        self.assertEqual(calls[-1].args[1], 20)
        self.assertEqual(len(calls[1].args[3]), 256 * 256 * 3 * 3)

    def test_timeout_is_not_unavailable_and_preserves_partial_progress(self):
        rows, _ = self.execute([listed("libx265"), outcome(), probed("hevc"),
                                outcome("frame=12\nout_time=00:00:00.266667\n", status="timeout", elapsed=20)])
        row = rows[1]
        self.assertTrue(row["functional"])
        self.assertEqual(row["benchmark"]["status"], "timeout")
        self.assertEqual(row["benchmark"]["fps"], .6)
        self.assertIsNone(row["benchmark"]["meets_target"])

    def test_slow_completion_is_reported_without_unavailable(self):
        rows, _ = self.execute([listed("libx264"), outcome(), probed(), outcome("frame=45\n", elapsed=10)])
        self.assertTrue(rows[0]["functional"])
        self.assertFalse(rows[0]["benchmark"]["meets_target"])
        self.assertEqual(rows[0]["benchmark"]["status"], "completed")

    def test_malformed_probe_and_wrong_frame_count_fail(self):
        for result in (outcome("not json"), probed(frames=2), probed("hevc"),
                       outcome(probed()["stdout"], stderr="decode error")):
            with self.subTest(result=result):
                rows, calls = self.execute([listed("libx264"), outcome(), result])
                self.assertFalse(rows[0]["functional"])
                self.assertEqual(rows[0]["status"], "validation_failed")
                self.assertEqual(len(calls), 3)

    def test_qsv_works_but_recorder_unsupported_no_benchmark(self):
        rows, calls = self.execute([listed("h264_qsv"), outcome(), probed()])
        row = rows[4]
        self.assertTrue(row["functional"])
        self.assertFalse(row["supported_by_recorder"])
        self.assertEqual(row["benchmark"]["status"], "skipped")
        self.assertEqual(len(calls), 3)
        self.assertNotIn("-preset", calls[1].args[0])

    def test_failed_listing_is_unknown_and_still_probes(self):
        rows, _ = self.execute([outcome(status="failed")] + [outcome(status="timeout")] * 8)
        self.assertTrue(all(row["compiled"] is None for row in rows))
        self.assertTrue(all(row["status"] == "capability_timeout" for row in rows))

    def test_zero_exit_without_completed_frame_count_is_incomplete(self):
        rows, _ = self.execute([listed("libx264"), outcome(), probed(), outcome("frame=2\n")])
        self.assertEqual(rows[0]["benchmark"]["status"], "incomplete")
        self.assertIsNone(rows[0]["benchmark"]["meets_target"])

    def test_profile_commands_and_h264_forced_b_zero(self):
        for encoder in ("libx264", "h264_nvenc", "hevc_nvenc", "libx265"):
            with self.subTest(encoder=encoder):
                settings = diagnostics._settings(encoder, PROFILE)
                args = diagnostics._encoder_args(encoder, settings)
                self.assertIn("-force_key_frames", args)
                self.assertIn("yuv420p", args)
                if encoder == "libx264":
                    self.assertIn("ultrafast", args)
                    self.assertIn("keyint=90:min-keyint=90:scenecut=0:open-gop=0:bframes=0", args)
                elif encoder == "libx265":
                    self.assertIn("fast", args)
                    self.assertIn("keyint=90:bframes=3:pools=2:min-keyint=45", args)
                    self.assertIn("21", args)
                else:
                    self.assertIn("p5", args)
                    self.assertEqual(args[args.index("-rc") + 1], "vbr")
                    self.assertEqual(args[args.index("-cq") + 1], "23")
                    self.assertEqual(args[args.index("-bf") + 1], "0" if encoder == "h264_nvenc" else "3")

    def test_capped_rate_control_and_x265_override_are_explicit(self):
        profile = dict(PROFILE, rate_control="capped_vbr", bitrate_mbps=12, maxrate_mbps=14, buffer_mbits=28)
        for encoder in ("libx264", "hevc_nvenc"):
            args = diagnostics._encoder_args(encoder, diagnostics._settings(encoder, profile))
            self.assertEqual(args[args.index("-b:v") + 1], "12M")
            self.assertIn("14M", args)
            self.assertIn("28M", args)
            self.assertNotIn("-cq", args)
        settings = diagnostics._settings("libx265", profile)
        self.assertEqual(settings["rate_control"], "quality")
        self.assertIn("not capped VBR", settings["note"])


class OwnedProcessTests(unittest.TestCase):
    def test_timeout_kills_only_child_and_writes_replaced_utf8_log(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnostics.subprocess, "Popen") as popen:
            child = popen.return_value
            child.communicate.side_effect = [subprocess.TimeoutExpired("ffmpeg", 8), (b"frame=2\n", b"bad\xff")]
            child.returncode = -9
            log = Path(folder) / "probe.log"
            result = diagnostics._run(["ffmpeg"], 8, log)
            child.kill.assert_called_once_with()
            self.assertEqual(result["status"], "timeout")
            self.assertIn("frame=2", result["stdout"])
            self.assertIn("bad\ufffd", log.read_text(encoding="utf-8"))

    def test_keyboard_interrupt_cleans_up_owned_child_and_propagates(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnostics.subprocess, "Popen") as popen:
            child = popen.return_value
            child.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
            child.poll.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                diagnostics._run(["ffmpeg"], 8, Path(folder) / "probe.log")
            child.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
