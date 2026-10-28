"""Local operator camera child and bounded browser preview (stdlib only)."""
import argparse
from collections import deque
import ctypes
import json
import math
import os
import sys
from pathlib import Path
import threading
import time
import zlib

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'scripts'))
from live_preview import downsample_rgb


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def make_png(rgb, width, height):
    def chunk(kind, payload):
        body = kind + payload
        return len(payload).to_bytes(4, 'big') + body + (zlib.crc32(body) & 0xffffffff).to_bytes(4, 'big')
    stride = width * 3
    rows = b''.join(b'\0' + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', width.to_bytes(4, 'big') + height.to_bytes(4, 'big') + b'\x08\x02\0\0\0')
            + chunk(b'IDAT', zlib.compress(rows, 1)) + chunk(b'IEND', b''))


class WindowsOwner:
    """Keep one process handle, avoiding PID reuse and 64-bit handle truncation."""
    def __init__(self, pid, kernel=None):
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError('owner pid must be a positive Windows process identifier')
        if kernel is None:
            if sys.platform != 'win32':
                raise RuntimeError('Owner process checks require Windows')
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel = kernel
        self.handle = None
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        # SYNCHRONIZE is sufficient for process exit observation.
        self.handle = kernel.OpenProcess(0x00100000, 0, pid)
        if not self.handle:
            raise RuntimeError('录制工作台已退出或无法检查其状态；正在停止并保存已采集的视频。')

    def alive(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0x00000102:  # WAIT_TIMEOUT: process has not exited.
            return True
        if result == 0:  # WAIT_OBJECT_0: process exited.
            return False
        raise RuntimeError('无法检查录制工作台状态；正在停止并保存已采集的视频。')

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def parent_is_alive(pid):
    """Probe a persisted child PID for console restart exclusion.

    An invalid/absent process returns False; access denial and unknown errors
    return True so a new console cannot overlap a possibly draining child.
    """
    try:
        owner = WindowsOwner(pid)
    except ValueError:
        return False
    except RuntimeError:
        # ERROR_INVALID_PARAMETER means this PID no longer exists. An unknown
        # error (including access denial) must preserve camera exclusion.
        return ctypes.get_last_error() != 87
    try:
        return owner.alive()
    except RuntimeError:
        return True
    finally:
        owner.close()


class CameraMonitor:
    """One replaceable pending immutable RGB frame; never waits for PNG encoding."""
    def __init__(self, control_dir, width=None, height=None):
        self.directory = Path(control_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.width, self.height = width, height
        self.pending = deque(maxlen=1)
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.last_offer = self.last_status = float('-inf')
        self.error = None
        self.owner = None
        self.owner_checked = False
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def ready(self):
        return time.monotonic() - self.last_offer >= .5 and not self.stop.is_set()

    def offer(self, rgb):
        now = time.monotonic()
        if now - self.last_offer >= .5 and not self.stop.is_set():
            self.last_offer = now
            self.pending.append(rgb)
            self.wake.set()

    def _work(self):
        while not self.stop.is_set():
            self.wake.wait(.5)
            self.wake.clear()
            try:
                rgb = self.pending.pop()
            except IndexError:
                continue
            try:
                step = max(1, math.ceil(self.width / 960))
                small = downsample_rgb(rgb, self.width, self.height, step)
                atomic_write(self.directory / 'preview.png', make_png(small, (self.width + step - 1)//step, (self.height + step - 1)//step))
            except Exception as exc:
                self.error = str(exc)

    def publish(self, stage, **state):
        now = time.monotonic()
        # Always publish stage transitions; repeated updates are capped at 2 Hz.
        if stage == getattr(self, 'stage', None) and now - self.last_status < .5:
            return
        self.stage = stage
        self.last_status = now
        state.update(stage=stage, preview_error=self.error)
        atomic_write(self.directory / 'status.json', (json.dumps(state, ensure_ascii=False) + '\n').encode('utf-8'))

    def stop_requested(self):
        if not self.owner_checked:
            owner_file = self.directory / 'owner.json'
            if owner_file.exists():
                document = json.loads(owner_file.read_text(encoding='utf-8'))
                if not isinstance(document, dict) or set(document) != {'pid'}:
                    raise ValueError('owner.json requires exactly pid')
                self.owner = WindowsOwner(document['pid'])
            self.owner_checked = True
        if self.owner is not None and not self.owner.alive():
            raise RuntimeError('录制工作台已退出；正在停止并保存已采集的视频，本次运行将标记为未完成。')
        return (self.directory / 'stop.request').exists()

    def finish(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=5)
        if self.owner is not None:
            self.owner.close()


def apply_settings(camera, exposure_us, gain):
    if type(exposure_us) is not int or not 1 <= exposure_us <= 5000000:
        raise ValueError('exposure_us must be an integer in 1..5000000')
    if type(gain) is not int or not 100 <= gain <= 65535:
        raise ValueError('gain must be an integer in 100..65535')
    camera.put_ExpoTime(exposure_us)
    camera.put_ExpoAGain(gain)
    readback = dict(exposure_us=camera.get_ExpoTime(), gain=camera.get_ExpoAGain())
    if readback != dict(exposure_us=exposure_us, gain=gain):
        raise RuntimeError(f'Exposure/gain readback mismatch: {readback}')
    return readback


def calibrate(config, control_dir):
    import capture
    from recording_profile import load_profile
    from camera_profile import apply_imaging
    monitor = CameraMonitor(control_dir)
    camera = None
    started = False
    frames = 0
    beginning = time.monotonic()
    readback = None
    try:
        monitor.publish('starting', frames=0)
        profile, _ = load_profile(Path(config))
        mii = capture.sdk()
        dev, camera = capture.select_camera(mii)
        width, height, readback = capture.configure(camera, dev, mii, profile['exposure_us'], profile['gain'],
            profile['speed_level'], profile['sdk_frame_rate_limit'], profile['frontend_buffer'],
            profile['backend_buffer'], profile['usb_thread_priority'])
        if profile.get('imaging') is not None:
            readback['imaging'] = apply_imaging(camera, mii, profile['imaging'])
            if camera.get_FinalSize() != (width, height):
                raise RuntimeError('Final image size changed after imaging settings')
        monitor.width, monitor.height = width, height
        fatal = []
        def on_event(event, unused):
            if event in capture.FATAL_EVENTS:
                fatal.append(event)
        camera.StartPullModeWithCallback(on_event, None)
        started = True
        buffer = ctypes.create_string_buffer(width * height * 3)
        previous = None
        while not monitor.stop_requested():
            if fatal:
                raise RuntimeError(f'Camera fatal event: 0x{fatal[0]:04x}')
            setting_file = monitor.directory / 'settings.json'
            if setting_file.exists():
                content = setting_file.read_bytes()
                if content != previous:
                    setting = json.loads(content)
                    if set(setting) != {'exposure_us', 'gain'}:
                        raise ValueError('settings.json requires exactly exposure_us and gain')
                    updated = apply_settings(camera, setting['exposure_us'], setting['gain'])
                    readback.update(updated)
                    previous = content
            info = mii.MiicamFrameInfoV4()
            camera.WaitImageV4(2000, buffer, 0, 24, -1, info)
            if (info.v3.width, info.v3.height) != (width, height):
                raise RuntimeError('Frame resolution changed')
            if monitor.ready():
                monitor.offer(buffer.raw)
            frames += 1
            monitor.publish('calibrating', frames=frames, elapsed_s=time.monotonic()-beginning, readback=readback, error=None)
        monitor.publish('stopping', frames=frames, readback=readback)
        if started:
            camera.Stop()
            started = False
        camera.Close()
        camera = None
        monitor.publish('complete', frames=frames, readback=readback, stopped_by_user=True, error=None)
        return 0
    except Exception as exc:
        monitor.publish('incomplete', frames=frames, error=f'{type(exc).__name__}: {exc}', readback=readback)
        return 1
    finally:
        try:
            if camera is not None:
                try:
                    if started:
                        camera.Stop()
                finally:
                    camera.Close()
        except Exception as exc:
            monitor.publish('incomplete', frames=frames, error=f'Camera cleanup failed: {exc}', readback=readback)
        finally:
            monitor.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--control-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    return calibrate(args.config, args.control_dir)


if __name__ == '__main__':
    raise SystemExit(main())
