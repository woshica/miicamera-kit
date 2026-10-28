"""Read-only Windows inventory and a small, explicitly scoped disk write screen.

Run disk probing in a disposable child: an OS filesystem write can block beyond
Python's control. This cached 64 MiB screen is not a sustained-write guarantee.
Only the unique file created by this probe is ever removed.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import subprocess
import time
import uuid


_INVENTORY_PS = r"""
$ErrorActionPreference = 'Stop'
$r = @{os=@{}; cpu=@{}; memory=@{}; gpus=@(); active_recording_processes=@(); errors=@()}
try { $o=Get-CimInstance Win32_OperatingSystem; if (!$o) {throw 'missing'}; $r.os=@{caption=$o.Caption;version=$o.Version;architecture=$o.OSArchitecture}; $r.memory=@{total_bytes=$(if ($null -ne $o.TotalVisibleMemorySize) {[long]$o.TotalVisibleMemorySize*1024} else {$null});available_bytes=$(if ($null -ne $o.FreePhysicalMemory) {[long]$o.FreePhysicalMemory*1024} else {$null})} } catch {$r.errors+='operating_system_unavailable'}
try { $c=@(Get-CimInstance Win32_Processor); if (!$c.Count) {throw 'missing'}; $r.cpu=@{name=(($c|ForEach-Object {$_.Name}) -join '; ');logical_processors=($c|Measure-Object NumberOfLogicalProcessors -Sum).Sum} } catch {$r.errors+='cpu_unavailable'}
try { $r.gpus=@(Get-CimInstance Win32_VideoController | ForEach-Object {@{name=$_.Name;driver_version=$_.DriverVersion}}) } catch {$r.errors+='gpu_unavailable'}
try { $r.active_recording_processes=@(Get-CimInstance Win32_Process | ForEach-Object {
  $kind=$null
  if ($_.Name -match '(?i)^imageview.*\.exe$') {$kind='imageview'}
  elseif ($_.Name -match '(?i)^ffmpeg(?:\.exe)?$') {$kind='ffmpeg'}
  elseif ($_.Name -match '(?i)^python(?:w|[0-9.]*)?(?:\.exe)?$' -and $_.CommandLine -match '(?i)(?:^|[\\/"\s])capture\.py(?:["\s]|$)') {$kind='capture'}
  elseif ($_.Name -match '(?i)^python(?:w|[0-9.]*)?(?:\.exe)?$' -and $_.CommandLine -match '(?i)(?:^|[\\/"\s])operator_camera\.py(?:["\s]|$)') {$kind='calibration'}
  if ($kind) {@{name=$_.Name;pid=$_.ProcessId;kind=$kind}}
}) } catch {$r.errors+='process_inventory_unavailable'}
$r | ConvertTo-Json -Depth 5 -Compress
"""


def _run(command, timeout, encoding='utf-8-sig'):
    """No shell, profiles, installations, or caller-supplied command text."""
    result = subprocess.run(command, capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('command_failed')
    # Windows PowerShell output is explicitly configured as UTF-8 below.
    return result.stdout.decode(encoding, errors='replace').strip()


def _collect_windows_inventory():
    script = '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new();' + _INVENTORY_PS
    return json.loads(_run(['powershell.exe', '-NoProfile', '-NonInteractive',
                           '-Command', script], 12))


def _power_status():
    class Status(ctypes.Structure):
        _fields_ = [('ACLineStatus', ctypes.c_ubyte), ('BatteryFlag', ctypes.c_ubyte),
                    ('BatteryLifePercent', ctypes.c_ubyte), ('SystemStatusFlag', ctypes.c_ubyte),
                    ('BatteryLifeTime', ctypes.c_ulong), ('BatteryFullLifeTime', ctypes.c_ulong)]
    state = Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(state)):
        raise OSError('power_status_unavailable')
    return {'ac_connected': {0: False, 1: True}.get(state.ACLineStatus),
            'battery_present': None if state.BatteryFlag == 255 else not bool(state.BatteryFlag & 128),
            'battery_percent': None if state.BatteryLifePercent == 255 else state.BatteryLifePercent}


def _volume_info(output_dir):
    """Resolve the actual mounted volume, including paths below mount folders."""
    path = Path(output_dir).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    kernel = ctypes.windll.kernel32
    volume = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(path), volume, len(volume)):
        raise OSError('target_volume_unavailable')
    free, total, total_free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not kernel.GetDiskFreeSpaceExW(volume.value, ctypes.byref(free), ctypes.byref(total), ctypes.byref(total_free)):
        raise OSError('target_disk_capacity_unavailable')
    filesystem = ctypes.create_unicode_buffer(256)
    fs_ok = kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem))
    drive_type = {0: 'unknown', 1: 'unknown', 2: 'removable', 3: 'fixed', 4: 'network', 5: 'optical', 6: 'ramdisk'}.get(kernel.GetDriveTypeW(volume.value), 'unknown')
    return {'filesystem': filesystem.value if fs_ok else None, 'drive_type': drive_type,
            'free_bytes': free.value, 'total_bytes': total.value}


def collect_environment(output_dir):
    """JSON-safe inventory; unavailable measurements remain null, never zero."""
    result = {'os': {'caption': None, 'version': None, 'architecture': None},
              'cpu': {'name': None, 'logical_processors': None},
              'memory': {'total_bytes': None, 'available_bytes': None}, 'gpus': [],
              'active_recording_processes': [],
              'power': {'ac_connected': None, 'battery_present': None, 'battery_percent': None,
                        'scheme': None, 'sleep_settings': None},
              'volume': {'filesystem': None, 'drive_type': 'unknown', 'free_bytes': None, 'total_bytes': None},
              'errors': []}
    if platform.system() != 'Windows':
        result['errors'].append('windows_inventory_unavailable_on_this_platform')
        return result
    try:
        inventory = _collect_windows_inventory()
        for key in ('os', 'cpu', 'memory'):
            result[key].update(inventory.get(key) or {})
        for key in ('gpus', 'active_recording_processes'):
            result[key] = inventory.get(key) or []
        result['errors'].extend(inventory.get('errors') or [])
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, TypeError):
        result['errors'].append('windows_inventory_unavailable_or_timed_out')
    for key, operation in (('power', _power_status), ('volume', lambda: _volume_info(output_dir))):
        try:
            result[key].update(operation())
        except (OSError, AttributeError, ValueError):
            result['errors'].append(key + '_unavailable')
    for key, args in (('scheme', ['/getactivescheme']), ('sleep_settings', ['/query', 'SCHEME_CURRENT', 'SUB_SLEEP'])):
        try:
            # powercfg uses the local console OEM encoding; preserve text best-effort.
            encoding = 'cp' + str(ctypes.windll.kernel32.GetOEMCP())
            result['power'][key] = _run(['powercfg.exe'] + args, 3, encoding=encoding)[:12000]
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            result['errors'].append('power_' + key + '_unavailable')
    return result


def probe_disk(output_dir, max_bytes=64 * 1024 * 1024, target_seconds=3):
    """Write up to max_bytes, flush, fsync, then unlink only our own file.

    A parent process must impose a hard deadline for stalled network/device I/O.
    The directory must already exist; this function never creates directories.
    """
    result = {'ok': False, 'bytes_written': 0, 'elapsed_s': None, 'mib_per_s': None,
              'error': None, 'cleanup_error': None, 'path': None,
              'limitation': 'Small cached write screen; not guaranteed sustained disk speed.'}
    if max_bytes <= 0 or target_seconds <= 0:
        result['error'] = 'invalid_probe_limits'
        return result
    path = Path(output_dir) / ('.miicamera-disk-probe-' + uuid.uuid4().hex + '.tmp')
    result['path'] = str(path)
    created = False
    started = time.monotonic()
    try:
        # Exclusive creation is essential: a collision must not delete an existing file.
        with path.open('xb', buffering=0) as stream:
            created = True
            block = bytes(range(1, 256)) * 4113
            block = (block + b'\x7f' * (1024 * 1024))[:1024 * 1024]
            while result['bytes_written'] < max_bytes:
                remaining = max_bytes - result['bytes_written']
                count = stream.write(block[:min(len(block), remaining)])
                if not count:
                    raise OSError('zero_length_write')
                result['bytes_written'] += count
                if time.monotonic() - started >= target_seconds:
                    break
            stream.flush()
            os.fsync(stream.fileno())
        result['elapsed_s'] = max(time.monotonic() - started, 1e-9)
        result['mib_per_s'] = result['bytes_written'] / (1024 * 1024) / result['elapsed_s']
        result['ok'] = True
    except OSError as exc:
        result['elapsed_s'] = max(time.monotonic() - started, 0)
        result['error'] = type(exc).__name__ + (': winerror=' + str(exc.winerror) if getattr(exc, 'winerror', None) else ': errno=' + str(exc.errno))
    finally:
        if created:
            try:
                path.unlink()
            except OSError as exc:
                result['cleanup_error'] = type(exc).__name__
                result['ok'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--environment', metavar='PATH')
    group.add_argument('--disk', metavar='PATH')
    args = parser.parse_args()
    result = collect_environment(args.environment) if args.environment else probe_disk(args.disk)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
