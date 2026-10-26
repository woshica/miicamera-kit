"""Continuous encoder: one ffmpeg process for all capture segments.

The caller submits consecutive RGB24 frames. Segment boundaries are by encoded
frame number (CFR video time), while each CSV keeps the original acquisition
timestamps. No frame is created or discarded by this writer.
"""

import csv
from collections import deque
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

from x265_settings import X265_PRESETS, validate_x265_params


FRAME_CSV = ("encoded_index", "sdk_seq", "sdk_timestamp_us", "host_utc_ns",
             "host_monotonic_ns", "sdk_flag", "exposure_us", "gain")
QUEUE_FRAMES = 8
# A paced 4K/45 fps HEVC NVENC start briefly queues 11 RGB frames while ffmpeg
# initializes; the queue then drains to one frame. Keep five frames of margin.
HEVC_QUEUE_FRAMES = 16


class _WriteTiming:
    """Bounded, thread-safe history of completed pipe writes, in nanoseconds."""

    WINDOW_NS = 1_000_000_000
    MAX_SAMPLES = 4096

    def __init__(self, clock=None):
        self.clock = clock if clock is not None else time.perf_counter_ns
        self._lock = threading.Lock()
        self._samples = deque(maxlen=self.MAX_SAMPLES)
        self._in_progress_ns = None

    def begin(self):
        with self._lock:
            self._in_progress_ns = self.clock()

    def complete(self, start_ns, end_ns, successful):
        with self._lock:
            if successful:
                self._samples.append((end_ns, end_ns - start_ns))
            self._in_progress_ns = None

    def snapshot(self):
        with self._lock:
            now_ns = self.clock()
            samples = tuple(self._samples)
            in_progress_ns = self._in_progress_ns
        # Filtering and arithmetic do not hold up the writer thread.
        durations = [duration for completed, duration in samples
                     if now_ns - self.WINDOW_NS < completed <= now_ns]
        return {"window_seconds": 1.0,
                "sample_count": len(durations),
                "mean_ms": sum(durations) / len(durations) / 1_000_000 if durations else None,
                "in_progress_ms": (now_ns - in_progress_ns) / 1_000_000
                                  if in_progress_ns is not None else None}


def _ffmpeg_path(ffmpeg):
    return Path(ffmpeg) if ffmpeg is not None else Path(__file__).resolve().parents[1] / "runtime/ffmpeg/bin/ffmpeg.exe"


def resolve_encoder(requested, ffmpeg=None):
    """Select an encoder after a short real encode, without creating output files."""
    if requested not in ("auto", "libx264", "libx265", "h264_nvenc", "hevc_nvenc"):
        raise ValueError(f"Unsupported encoder: {requested}")
    if requested == "libx264":
        return requested
    if requested == "libx265":
        cmd = [str(_ffmpeg_path(ffmpeg)), "-hide_banner", "-nostdin", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5:d=0.6",
               "-frames:v", "3", "-c:v", "libx265", "-preset", "superfast",
               "-x265-params", "pools=1:frame-threads=1", "-f", "null", "-"]
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=10, check=False)
            if result.returncode == 0:
                return requested
            reason = result.stderr.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            reason = str(exc)
        raise RuntimeError(f"libx265 is unavailable: {reason}")
    cmd = [str(_ffmpeg_path(ffmpeg)), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "256x256",
           "-framerate", "5", "-i", "pipe:0", "-frames:v", "3",
           "-c:v", "hevc_nvenc" if requested == "hevc_nvenc" else "h264_nvenc",
           "-bf", "0", "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, input=bytes(256 * 256 * 3 * 3),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=10, check=False)
        if result.returncode == 0:
            return "hevc_nvenc" if requested == "hevc_nvenc" else "h264_nvenc"
        reason = result.stderr.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    if requested == "auto":
        return "libx264"
    raise RuntimeError(f"{requested} is unavailable: {reason}")


class Writer(threading.Thread):
    def __init__(self, run_dir, width, height, fps, segment_seconds, encoder, ffmpeg=None,
                 *, gop_frames=None, b_frames=0, crf=18, nvenc_cq=19,
                 rate_control="quality", bitrate_mbps=24, maxrate_mbps=26,
                 buffer_mbits=52, nvenc_preset="p4", x265_preset="superfast",
                 x265_params=None):
        super().__init__(daemon=True)
        if encoder not in ("libx264", "libx265", "h264_nvenc", "hevc_nvenc"):
            raise ValueError(f"Unsupported encoder: {encoder}")
        for name, value, low, high in (("width", width, 1, None),
                                       ("height", height, 1, None),
                                       ("fps", fps, 1, None),
                                       ("segment_seconds", segment_seconds, 1, None),
                                       ("b_frames", b_frames, 0, 3 if encoder in ("hevc_nvenc", "libx265") else 0),
                                       ("crf", crf, 0, 51),
                                       ("nvenc_cq", nvenc_cq, 0, 51)):
            if type(value) is not int or value < low or (high is not None and value > high):
                raise ValueError(f"{name} must be an integer in {low}..{high if high is not None else '∞'}")
        if gop_frames is None:
            gop_frames = fps * 2
        if type(gop_frames) is not int or gop_frames < 1:
            raise ValueError("gop_frames must be a positive integer")
        if nvenc_preset not in ("p1", "p2", "p3", "p4", "p5", "p6", "p7"):
            raise ValueError("nvenc_preset must be p1..p7")
        if x265_preset not in X265_PRESETS:
            raise ValueError(f"x265_preset must be one of {X265_PRESETS}")
        x265_params = validate_x265_params(x265_params)
        if encoder == "libx265" and "min-keyint" in x265_params and int(x265_params["min-keyint"]) > gop_frames:
            raise ValueError("x265 min-keyint must not exceed gop_frames")
        if rate_control not in ("quality", "capped_vbr"):
            raise ValueError("rate_control must be 'quality' or 'capped_vbr'")
        if encoder == "libx265" and rate_control != "quality":
            raise ValueError("libx265 supports quality rate control only")
        for name, value in (("bitrate_mbps", bitrate_mbps),
                            ("maxrate_mbps", maxrate_mbps),
                            ("buffer_mbits", buffer_mbits)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if maxrate_mbps < bitrate_mbps:
            raise ValueError("maxrate_mbps must be at least bitrate_mbps")
        self.run_dir = Path(run_dir)
        self.width, self.height, self.fps = width, height, fps
        self.segment_seconds, self.encoder = segment_seconds, encoder
        self.ffmpeg = _ffmpeg_path(ffmpeg)
        self.frames_per_segment = fps * segment_seconds
        self.gop_frames = gop_frames
        self.b_frames = b_frames
        self.crf = crf
        self.nvenc_cq = nvenc_cq
        self.nvenc_preset = nvenc_preset
        self.x265_preset = x265_preset
        self.x265_params = x265_params
        self.rate_control = rate_control
        self.bitrate_mbps = bitrate_mbps
        self.maxrate_mbps = maxrate_mbps
        self.buffer_mbits = buffer_mbits
        self.queue_frames = HEVC_QUEUE_FRAMES if encoder in ("hevc_nvenc", "libx265") else QUEUE_FRAMES
        self.encoding_settings = {"encoder": encoder, "gop_frames": gop_frames,
                                  "b_frames": b_frames,
                                  "queue_frames": self.queue_frames,
                                  "frames_per_segment": self.frames_per_segment,
                                  "pixel_format": "yuv420p",
                                  "preset": "ultrafast" if encoder == "libx264" else
                                            x265_preset if encoder == "libx265" else nvenc_preset,
                                  "rate_control": rate_control}
        if encoder == "libx265":
            self.encoding_settings["x265_params"] = dict(x265_params)
        if rate_control == "capped_vbr":
            self.encoding_settings.update(bitrate_mbps=bitrate_mbps,
                                          maxrate_mbps=maxrate_mbps,
                                          buffer_mbits=buffer_mbits)
        elif encoder in ("libx264", "libx265"):
            self.encoding_settings["crf"] = crf
        else:
            self.encoding_settings["nvenc_cq"] = nvenc_cq
        self.jobs = queue.Queue(maxsize=self.queue_frames)
        self.stop_event = threading.Event()
        self.abort_event = threading.Event()
        self.error = None
        self.process = None
        self.segments = []
        self._log_file = None
        self._csv_file = None
        self._csv_writer = None
        self._frames_written = 0
        self._write_timing = _WriteTiming()

    def write_timing_snapshot(self):
        """Return pipe-write latency for completions in the trailing one second.

        This excludes queue wait, CSV work, and encoder finalization. An ongoing
        write is reported separately and is never included in the mean.
        """
        return self._write_timing.snapshot()

    def start(self):
        """Launch ffmpeg before acquisition starts, avoiding a process spawn on frame one."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.run_dir / "continuous.ffmpeg.log"
        self._log_file = log_path.open("x", encoding="utf-8")
        cmd = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
               "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size",
               f"{self.width}x{self.height}", "-framerate", str(self.fps),
               "-i", "pipe:0", "-an", "-c:v", self.encoder]
        if self.encoder == "libx264":
            cmd += ["-preset", "ultrafast"]
            if self.rate_control == "quality":
                cmd += ["-crf", str(self.crf)]
            else:
                cmd += ["-b:v", f"{self.bitrate_mbps:g}M",
                        "-maxrate", f"{self.maxrate_mbps:g}M",
                        "-bufsize", f"{self.buffer_mbits:g}M"]
            cmd += ["-pix_fmt", "yuv420p",
                    "-x264-params", f"keyint={self.gop_frames}:min-keyint={self.gop_frames}:scenecut=0:open-gop=0:bframes=0"]
        elif self.encoder == "libx265":
            params = {"keyint": self.gop_frames, "bframes": self.b_frames,
                      **self.x265_params}
            cmd += ["-preset", self.x265_preset, "-crf", str(self.crf),
                    "-pix_fmt", "yuv420p", "-forced-idr", "1",
                    "-x265-params", ":".join(f"{key}={value}" for key, value in params.items())]
        else:
            cmd += ["-preset", self.nvenc_preset, "-rc", "vbr"]
            if self.rate_control == "quality":
                cmd += ["-b:v", "0", "-cq", str(self.nvenc_cq)]
            else:
                cmd += ["-b:v", f"{self.bitrate_mbps:g}M",
                        "-maxrate", f"{self.maxrate_mbps:g}M",
                        "-bufsize", f"{self.buffer_mbits:g}M"]
            cmd += ["-pix_fmt", "yuv420p",
                    "-g", str(self.gop_frames), "-bf", str(self.b_frames), "-forced-idr", "1",
                    "-no-scenecut", "1", "-rc-lookahead", "0"]
        # Force the union of seek points and segment cuts, even when their periods do not divide.
        forced_keys = (f"expr:eq(mod(n,{self.frames_per_segment}),0)" if self.encoder == "libx265"
                       else f"expr:eq(mod(n,{self.gop_frames}),0)+eq(mod(n,{self.frames_per_segment}),0)")
        cmd += ["-force_key_frames", forced_keys,
                "-f", "segment", "-segment_time", str(self.segment_seconds),
                "-segment_start_number", "1", "-reset_timestamps", "1",
                "-segment_format", "matroska", str(self.run_dir / "segment-%04d.mkv")]
        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=self._log_file)
            super().start()
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.wait(timeout=5)
            self._log_file.close()
            raise

    def submit(self, rgb, meta):
        if self.error:
            raise RuntimeError(f"Video writer failed: {self.error}")
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(f"ffmpeg exited early ({self.process.returncode}); see continuous.ffmpeg.log")
        try:
            self.jobs.put_nowait((rgb, meta))
        except queue.Full as exc:
            raise RuntimeError(f"Encoder queue overflow ({self.queue_frames} frames); capture incomplete") from exc

    def finish(self):
        self.stop_event.set()
        if self.ident is None:
            return  # start() already cleans resources when it fails before Thread.start().
        self.join(timeout=45)
        if self.is_alive():
            self.abort_event.set()
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.join(timeout=5)
            raise RuntimeError("Video writer did not stop within 45 seconds")
        if self.error:
            raise RuntimeError(f"Video writer failed: {self.error}")

    def _open_index(self, number):
        if self._csv_file is not None:
            self._csv_file.close()
        stem = f"segment-{number:04d}"
        csv_path = self.run_dir / f"{stem}.frames.csv"
        self._csv_file = csv_path.open("x", encoding="utf-8", newline="")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=FRAME_CSV)
        self._csv_writer.writeheader()
        self.segments.append({"number": number, "video": f"{stem}.mkv",
                              "frames_csv": csv_path.name, "ffmpeg_log": "continuous.ffmpeg.log",
                              "frames_submitted": 0, "first_host_monotonic_ns": None,
                              "last_host_monotonic_ns": None})

    def run(self):
        try:
            while not (self.stop_event.is_set() and self.jobs.empty()):
                try:
                    rgb, meta = self.jobs.get(timeout=0.1)
                except queue.Empty:
                    if self.process.poll() is not None:
                        raise RuntimeError(f"ffmpeg exited early ({self.process.returncode})")
                    continue
                if self.abort_event.is_set():
                    raise RuntimeError("Writer shutdown aborted pending frames")
                number = self._frames_written // self.frames_per_segment + 1
                if not self.segments or self.segments[-1]["number"] != number:
                    self._open_index(number)
                if self.process.poll() is not None:
                    raise RuntimeError(f"ffmpeg exited early ({self.process.returncode})")
                self._write_timing.begin()
                successful = False
                start_ns = self._write_timing.clock()
                try:
                    written = self.process.stdin.write(rgb)
                    end_ns = self._write_timing.clock()
                    successful = written == len(rgb)
                finally:
                    if not successful:
                        end_ns = self._write_timing.clock()
                    self._write_timing.complete(start_ns, end_ns, successful)
                segment = self.segments[-1]
                self._csv_writer.writerow({"encoded_index": segment["frames_submitted"], **meta})
                self._csv_file.flush()
                segment["frames_submitted"] += 1
                segment["first_host_monotonic_ns"] = (segment["first_host_monotonic_ns"]
                                                       or meta["host_monotonic_ns"])
                segment["last_host_monotonic_ns"] = meta["host_monotonic_ns"]
                self._frames_written += 1
                self.jobs.task_done()
            self.process.stdin.close()
            try:
                code = self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise RuntimeError("ffmpeg timed out after input close")
            if code:
                raise RuntimeError(f"ffmpeg exited {code}; see continuous.ffmpeg.log")
        except Exception as exc:
            self.error = exc
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            if self._csv_file is not None:
                self._csv_file.close()
            if self._log_file is not None:
                self._log_file.close()
