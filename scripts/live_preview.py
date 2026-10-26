"""Best-effort, bounded live RGB preview isolated from recording and camera APIs."""

from collections import deque
import math
from pathlib import Path
import subprocess
import threading
import time


FFPLAY = Path(__file__).resolve().parents[1] / "runtime/ffmpeg/bin/ffplay.exe"
TITLE = "录制预览（关闭窗口不停止录像）"


def downsample_rgb(rgb, width, height, step):
    """Nearest-neighbor RGB24 sampling; retain every channel without dependencies."""
    out_width = (width + step - 1) // step
    out_height = (height + step - 1) // step
    result = bytearray(out_width * out_height * 3)
    stride = width * 3
    for out_y, y in enumerate(range(0, height, step)):
        row = rgb[y * stride:(y + 1) * stride]
        offset = out_y * out_width * 3
        for channel in range(3):
            result[offset + channel:offset + out_width * 3:3] = row[channel::step * 3]
    return result


class LivePreview:
    """One worker and one replaceable queued frame; offer never waits for a pipe.

    The caller passes immutable, tightly packed RGB24 bytes already acquired for
    recording. The worker downsamples before writing. Counters describe accepted
    input and completed pipe writes, not frames confirmed displayed by ffplay.
    """

    def __init__(self, width, height, fps=5, max_width=960, ffplay=None):
        self.source_width = width
        self.source_height = height
        self.fps = fps
        self.max_width = max_width
        self.ffplay = str(FFPLAY if ffplay is None else ffplay)
        self._lock = threading.Lock()
        self._pending = deque(maxlen=1)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._last_offer = None
        self._status = "not_started"
        self._error = None
        self._counts = dict(frames_offered=0, frames_accepted=0,
                            frames_throttled=0, frames_replaced=0,
                            frames_sent_to_player=0)
        self.step = 1
        self.width = self.height = 0

    def _fail(self, error):
        with self._lock:
            self._error = str(error)
            self._status = "failed"
        self._stop.set()
        self._wake.set()

    def start(self):
        """Start once; all launch/configuration errors stay in snapshot()."""
        if self._status != "not_started":
            return self._status == "running"
        try:
            if (not isinstance(self.source_width, int) or self.source_width < 1
                    or not isinstance(self.source_height, int) or self.source_height < 1
                    or not isinstance(self.max_width, int) or self.max_width < 1
                    or not math.isfinite(self.fps) or self.fps <= 0):
                raise ValueError("Preview dimensions and fps must be positive")
            self.step = max(1, math.ceil(self.source_width / self.max_width))
            self.width = (self.source_width + self.step - 1) // self.step
            self.height = (self.source_height + self.step - 1) // self.step
            command = [self.ffplay, "-hide_banner", "-loglevel", "error",
                       "-probesize", "32", "-analyzeduration", "0",
                       "-f", "rawvideo", "-pixel_format", "rgb24",
                       "-video_size", f"{self.width}x{self.height}",
                       "-framerate", str(self.fps), "-an", "-sn", "-autoexit",
                       "-sync", "ext", "-framedrop",
                       "-window_title", TITLE, "-x", str(self.width),
                       "-y", str(self.height), "-i", "pipe:0"]
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._status = "running"
            self._thread = threading.Thread(target=self._run, name="live-preview", daemon=True)
            self._thread.start()
            return True
        except Exception as exc:
            self._fail(exc)
            self.finish()
            return False

    def offer(self, rgb):
        """Offer an immutable frame in constant time; replace stale pending work."""
        now = time.monotonic()
        with self._lock:
            self._counts["frames_offered"] += 1
            if self._status != "running" or self._stop.is_set():
                return False
            if not isinstance(rgb, bytes) or len(rgb) != self.source_width * self.source_height * 3:
                self._error = "Preview requires immutable, tightly packed RGB24 bytes"
                self._status = "failed"
                self._stop.set()
                self._wake.set()
                return False
            if self._last_offer is not None and now - self._last_offer < 1 / self.fps:
                self._counts["frames_throttled"] += 1
                return False
            self._last_offer = now
            self._counts["frames_replaced"] += bool(self._pending)
            self._pending.append(rgb)
            self._counts["frames_accepted"] += 1
            self._wake.set()
            return True

    def _run(self):
        next_send = 0
        try:
            while not self._stop.is_set():
                returncode = self._process.poll()
                if returncode is not None:
                    self._player_exited(returncode)
                    break
                self._wake.wait(0.1)
                delay = next_send - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                    continue
                with self._lock:
                    self._wake.clear()
                    rgb = self._pending.pop() if self._pending else None
                if rgb is None or self._stop.is_set():
                    continue
                small = downsample_rgb(rgb, self.source_width, self.source_height, self.step)
                view = memoryview(small)
                while view and not self._stop.is_set():
                    written = self._process.stdin.write(view)
                    if not written:
                        raise BrokenPipeError("Preview player closed its input")
                    view = view[written:]
                if not view:
                    next_send = time.monotonic() + 1 / self.fps
                    with self._lock:
                        self._counts["frames_sent_to_player"] += 1
        except BrokenPipeError as exc:
            if not self._stop.is_set():
                # The pipe can close just before the player's exit is visible.
                try:
                    returncode = self._process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    returncode = None
                if returncode is None:
                    self._fail(exc)
                else:
                    self._player_exited(returncode)
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc)
        finally:
            self._stop.set()
            with self._lock:
                self._pending.clear()
            # Unbuffered stdin: only this thread closes it after its write exits.
            try:
                self._process.stdin.close()
            except Exception:
                pass
            self._terminate_player()

    def _player_exited(self, returncode):
        if returncode == 0:
            with self._lock:
                self._status = "closed"
        else:
            self._fail(f"Preview player exited with code {returncode}")

    def _terminate_player(self):
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except Exception as exc:
                self._fail(exc)
        except Exception as exc:
            self._fail(exc)

    def finish(self):
        """Stop only our player; killing it releases a blocked pipe write.

        Do not close stdin here: a concurrent buffered close can wait on the
        writer's lock. Terminate/kill first, then let the worker close its pipe.
        """
        self._stop.set()
        self._wake.set()
        self._terminate_player()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        elif self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
        with self._lock:
            self._pending.clear()
            if self._status in ("running", "not_started"):
                self._status = "finished"
            if self._thread is not None and self._thread.is_alive():
                self._status = "failed"
                self._error = "Preview worker did not stop within shutdown deadline"
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return dict(self._counts, enabled=True, status=self._status, error=self._error,
                        width=self.width, height=self.height, fps=self.fps,
                        max_width=self.max_width, queue_depth=len(self._pending),
                        worker_alive=self._thread is not None and self._thread.is_alive(),
                        player_returncode=self._process.poll() if self._process is not None else None)
