"""Best-effort CMD status; only the daemon worker touches disk or stdout.

MKV byte rates describe container growth (including mux buffering), not the
encoder's exact bitrate. A blocked filesystem or terminal can leave the daemon
behind; finish deliberately waits at most one second for it.
"""

import math
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time


def _file_size(path):
    # Windows directory-entry size metadata can lag an actively written MKV.
    # Query an opened handle without reading payload; some writers may deny
    # read sharing, in which case ordinary stat is still useful best effort.
    try:
        with path.open('rb', buffering=0) as handle:
            return os.fstat(handle.fileno()).st_size
    except OSError:
        return path.stat().st_size


def sample_files(run_dir):
    """Collect one best-effort metadata sample without reading video contents."""
    paths = None
    total_bytes = None
    free_bytes = None
    try:
        paths = sorted(Path(run_dir).glob('*.mkv'))
        total_bytes = sum(_file_size(path) for path in paths)
    except OSError:
        pass
    try:
        free_bytes = shutil.disk_usage(run_dir).free
    except OSError:
        pass
    return {'files': paths, 'bytes': total_bytes, 'free_bytes': free_bytes}


def compute_metrics(state, files, duration_seconds, fps, segment_seconds,
                    sample_time, previous=None):
    """Compute display values; previous is (wall_time, total_mkv_bytes)."""
    elapsed = max(0.0, float(state.get('elapsed_s', 0)))
    duration = max(0.0, float(duration_seconds))
    remaining = max(0.0, duration - elapsed)
    frames = max(0, int(state.get('frames', 0)))
    measured_fps = (max(0.0, float(state['measured_fps']))
                    if state.get('measured_fps') is not None
                    else frames / elapsed if elapsed > 0 else None)
    percent = min(100.0, 100 * elapsed / duration) if duration > 0 else None
    total_bytes = files['bytes']
    recent_mbps = None
    if previous is not None and total_bytes is not None:
        prior_time, prior_bytes = previous
        if prior_bytes is not None and sample_time > prior_time:
            recent_mbps = max(0, total_bytes - prior_bytes) * 8 / (sample_time - prior_time) / 1e6
    average_mbps = total_bytes * 8 / elapsed / 1e6 if total_bytes is not None and elapsed > 0 else None
    count = len(files['files']) if files['files'] is not None else None
    stage = state.get('stage', 'recording')
    # Incomplete output has unknown writer/validation state: do not call it closed.
    writer_open = stage in ('starting', 'recording', 'finishing')
    closed = (max(0, count - 1) if writer_open else count) if count is not None and stage != 'incomplete' else None
    current = files['files'][-1].name if writer_open and count else None
    denominator = float(fps) * float(segment_seconds)
    estimate = None
    if denominator > 0:
        rate = measured_fps if measured_fps is not None and frames > 0 else max(0.0, float(fps))
        estimate = math.ceil((frames + rate * remaining) / denominator)
    return dict(elapsed=elapsed, remaining=remaining, percent=percent,
                measured_fps=measured_fps, recent_mbps=recent_mbps,
                average_mbps=average_mbps, count=count, closed=closed,
                current=current, estimated_segments=estimate,
                estimated_additional=max(0, estimate - count) if estimate is not None and count is not None else None,
                bytes=total_bytes, free_bytes=files['free_bytes'])


def _number(value, decimals=1):
    return '不可用' if value is None else f'{value:.{decimals}f}'


def _duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def _write_timing_text(timing):
    if timing is None:
        return '写入近1s均值 不可用'
    count = timing['sample_count']
    if count:
        text = f"写入近1s均值 {timing['mean_ms']:.2f} ms/帧（{count}帧）"
    else:
        text = '写入近1s均值 无完成帧'
    if timing.get('in_progress_ms') is not None:
        text += f"；当前写入已等 {timing['in_progress_ms']:.2f} ms"
    return text


def format_status(state, metrics):
    """Return two concise Chinese lines; formatting itself performs no I/O."""
    stage = state.get('stage', 'recording')
    label = {'starting': '准备录制', 'recording': '录制中', 'finishing': '收尾写入中',
             'validating': '校验中', 'complete': '完成',
             'incomplete': '未完成（文件未确认）'}.get(stage, stage)
    progress = f"{_number(metrics['percent'])}%" if metrics['percent'] is not None else '不可用'
    line1 = (f"[{label}] 已用 {_duration(metrics['elapsed'])} / 剩余 {_duration(metrics['remaining'])} "
             f"进度 {progress} | 实测 {_number(metrics['measured_fps'])} fps "
             f"帧 {state.get('frames', 0)} 序号缺口 {state.get('sequence_gaps', 0)} "
             f"队列 {state.get('queue_depth', 0)}/{state.get('queue_capacity', '?')} | "
             + _write_timing_text(state.get('write_timing')))
    count = metrics['count'] if metrics['count'] is not None else '不可用'
    closed = metrics['closed'] if metrics['closed'] is not None else '未确认'
    current = metrics['current'] or ('未确认' if stage == 'incomplete' else '无')
    size = _number(None if metrics['bytes'] is None else metrics['bytes'] / 2**20)
    free = _number(None if metrics['free_bytes'] is None else metrics['free_bytes'] / 2**30)
    extra = metrics['estimated_additional'] if metrics['estimated_additional'] is not None else '不可用'
    estimated = metrics['estimated_segments'] if metrics['estimated_segments'] is not None else '不可用'
    line2 = (f"MKV {size} MiB 空闲 {free} GiB | 近期约 {_number(metrics['recent_mbps'])} / "
             f"均值约 {_number(metrics['average_mbps'])} Mbps（容器增长） | "
             f"已生成 {count} 段 已结束写入 {closed} 当前写入 {current} "
             f"预计总计 {estimated} 段 / 再生成 {extra} 段")
    if stage in ('validating', 'complete', 'incomplete'):
        line2 += f" | 已校验 {state.get('validated_segments', 0)}/{count}"
        if stage == 'validating' and state.get('validating_segment'):
            line2 += f" 正在校验 {state['validating_segment']}"
    if state.get('error'):
        line2 += ' | 错误 ' + str(state['error']).replace('\n', ' ').replace('\r', ' ')
    return line1 + '\n' + line2


class RecordingStatus:
    def __init__(self, run_dir, duration_seconds, fps, segment_seconds,
                 interval_seconds=2, stream=None, write_timing_provider=None):
        self.run_dir = Path(run_dir)
        self.duration_seconds = duration_seconds
        self.fps = fps
        self.segment_seconds = segment_seconds
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.stream = sys.stdout if stream is None else stream
        self.write_timing_provider = write_timing_provider
        self._lock = threading.Lock()
        self._state = {'stage': 'starting', 'elapsed_s': 0, 'frames': 0}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._messages = queue.SimpleQueue()
        self._thread = None
        self._error = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='recording-status', daemon=True)
            self._thread.start()
        return self

    def publish(self, **state):
        # No metadata, terminal writes, worker join, or external callback here.
        with self._lock:
            changed_stage = state.get('stage', self._state['stage']) != self._state['stage']
            self._state = {**self._state, **state}
        if changed_stage:
            self._wake.set()

    def notice(self, message):
        """Queue a permanent message; no console I/O on the acquisition thread."""
        self._messages.put(str(message))
        self._wake.set()

    def finish(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def snapshot(self):
        """Return lifecycle metadata without filesystem or terminal I/O."""
        alive = self._thread is not None and self._thread.is_alive()
        with self._lock:
            error = self._error
        if alive and self._stop.is_set():
            timeout = 'Status worker did not stop within 1 second (filesystem or output may be blocked)'
            error = f'{error}; {timeout}' if error else timeout
        result = {'worker_alive': alive}
        if error:
            result['error'] = error
        return result

    def _record_error(self, exc):
        with self._lock:
            self._error = self._error or f'{type(exc).__name__}: {exc}'

    def _run(self):
        renderer = None
        try:
            from console_status import ConsoleStatus
            renderer = ConsoleStatus(self.stream)
            self._render_loop(renderer)
        except Exception as exc:
            self._record_error(exc)
        finally:
            try:
                if renderer is not None:
                    renderer.finish()
            except Exception as exc:
                self._record_error(exc)

    def _render_loop(self, renderer):
        previous = None
        while True:
            self._wake.clear()
            stopping = self._stop.is_set()
            with self._lock:
                state = self._state.copy()
            try:
                files = sample_files(self.run_dir)
                sample_time = time.monotonic()
                metrics = compute_metrics(state, files, self.duration_seconds, self.fps,
                                          self.segment_seconds, sample_time, previous)
            except Exception as exc:
                self._record_error(exc)
                return
            previous = sample_time, files['bytes']
            if self.write_timing_provider is not None:
                try:
                    # Sample at display time, including after acquisition stops;
                    # old successes must expire rather than appear as fresh work.
                    state['write_timing'] = self.write_timing_provider()
                except Exception as exc:
                    state['write_timing'] = None
                    self._record_error(exc)
            try:
                while True:
                    try:
                        message = self._messages.get_nowait()
                    except queue.Empty:
                        break
                    renderer.notice(message)
                renderer.render(format_status(state, metrics))
            except Exception as exc:
                # A terminal closing must never stop camera acquisition.
                self._record_error(exc)
            if stopping:
                return
            self._wake.wait(self.interval_seconds)
