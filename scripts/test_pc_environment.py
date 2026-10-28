"""Bounded, camera-free checks of inventory failure handling and safe probing."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_environment as pc


class EnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell matching rules')
    def test_inventory_detects_preview_but_not_idle_workbench(self):
        # Replace CIM entirely: test the actual inventory classifier without
        # inspecting hardware or depending on this computer's running apps.
        fixtures = [
            {'Name': 'python.exe', 'ProcessId': 11,
             'CommandLine': 'python.exe "C:\\camera kit\\scripts\\operator_camera.py" --preview'},
            {'Name': 'pythonw.exe', 'ProcessId': 12,
             'CommandLine': 'pythonw.exe C:/kit/scripts/operator_console.py'},
            {'Name': 'python.exe', 'ProcessId': 13,
             'CommandLine': 'python.exe C:/kit/scripts/capture.py record'},
            {'Name': 'python.exe', 'ProcessId': 14,
             'CommandLine': 'python.exe C:/kit/scripts/not_operator_camera.py'},
        ]
        payload = json.dumps(fixtures).replace("'", "''")
        script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new();"
                  "$script:fixtures='" + payload + "' | ConvertFrom-Json;"
                  "function Get-CimInstance { param($ClassName) "
                  "if ($ClassName -eq 'Win32_Process') {$script:fixtures} else {throw 'stubbed'} };"
                  + pc._INVENTORY_PS)
        result = json.loads(pc._run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], 12))
        self.assertEqual([(p['pid'], p['kind']) for p in result['active_recording_processes']],
                         [(11, 'calibration'), (13, 'capture')])

    def test_inventory_static_command_and_json(self):
        payload = {'os': {'caption': 'Windows'}, 'active_recording_processes': []}
        with patch.object(pc.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())) as run:
            self.assertEqual(pc._collect_windows_inventory(), payload)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command'])
        self.assertEqual(kwargs['timeout'], 12)
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('UserName', args[0][4])
        self.assertNotIn('SerialNumber', args[0][4])
        self.assertIn("\n$ErrorActionPreference = 'Stop'", args[0][4])

    def test_partial_inventory_preserves_unknowns(self):
        inventory = {'os': {'version': '10'}, 'memory': {'available_bytes': 456},
                     'gpus': [{'name': 'GPU', 'driver_version': '1'}],
                     'active_recording_processes': [{'name': 'ffmpeg.exe', 'pid': 5, 'kind': 'ffmpeg'}]}
        with patch.object(pc.platform, 'system', return_value='Windows'), \
             patch.object(pc, '_collect_windows_inventory', return_value=inventory), \
             patch.object(pc, '_power_status', return_value={}), \
             patch.object(pc, '_volume_info', return_value={}), \
             patch.object(pc, '_run', side_effect=FileNotFoundError()):
            result = pc.collect_environment('unused')
        self.assertIsNone(result['memory']['total_bytes'])
        self.assertEqual(result['memory']['available_bytes'], 456)
        self.assertEqual(result['active_recording_processes'][0]['kind'], 'ffmpeg')

    def test_nonwindows_does_not_launch_windows_commands(self):
        with patch.object(pc.platform, 'system', return_value='Linux'), \
             patch.object(pc, '_run') as run:
            result = pc.collect_environment('unused')
        run.assert_not_called()
        self.assertIsNone(result['os']['architecture'])
        self.assertTrue(result['errors'])

    def test_unknown_values_on_inventory_permission_failure(self):
        with patch.object(pc.platform, 'system', return_value='Windows'), \
             patch.object(pc, '_collect_windows_inventory', side_effect=PermissionError()), \
             patch.object(pc, '_power_status', side_effect=OSError()), \
             patch.object(pc, '_volume_info', side_effect=OSError()), \
             patch.object(pc, '_run', side_effect=PermissionError()):
            result = pc.collect_environment('unused')
        self.assertIsNone(result['memory']['total_bytes'])
        self.assertIsNone(result['volume']['free_bytes'])
        self.assertIsNone(result['cpu']['logical_processors'])
        self.assertTrue(result['errors'])
        json.dumps(result)

    def test_timeout_is_bounded_and_reported(self):
        with patch.object(pc.platform, 'system', return_value='Windows'), \
             patch.object(pc, '_collect_windows_inventory', side_effect=subprocess.TimeoutExpired('powershell', 12)), \
             patch.object(pc, '_power_status', return_value={'ac_connected': True}), \
             patch.object(pc, '_volume_info', return_value={'filesystem': 'NTFS', 'free_bytes': 123}), \
             patch.object(pc, '_run', side_effect=FileNotFoundError()):
            result = pc.collect_environment('unused')
        self.assertTrue(result['power']['ac_connected'])
        self.assertEqual(result['volume']['free_bytes'], 123)
        self.assertIn('windows_inventory_unavailable_or_timed_out', result['errors'])


class DiskProbeTests(unittest.TestCase):
    def test_small_write_fsync_and_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / 'recording.mkv'
            existing.write_bytes(b'keep')
            result = pc.probe_disk(folder, max_bytes=4097, target_seconds=0.1)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['bytes_written'], 4097)
            self.assertGreater(result['mib_per_s'], 0)
            self.assertEqual(list(Path(folder).iterdir()), [existing])
            self.assertEqual(existing.read_bytes(), b'keep')

    def test_collision_never_unlinks_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / '.miicamera-disk-probe-fixed.tmp'
            existing.write_bytes(b'keep')
            with patch.object(pc.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')):
                result = pc.probe_disk(folder, max_bytes=1)
            self.assertFalse(result['ok'])
            self.assertIn('FileExistsError', result['error'])
            self.assertEqual(existing.read_bytes(), b'keep')

    def test_permission_failure_never_attempts_cleanup(self):
        with patch.object(Path, 'open', side_effect=PermissionError(13, 'denied')), \
             patch.object(Path, 'unlink') as unlink:
            result = pc.probe_disk('unused', max_bytes=1)
        self.assertFalse(result['ok'])
        self.assertIn('PermissionError', result['error'])
        unlink.assert_not_called()

    def test_write_flush_failure_still_cleans_created_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(pc.os, 'fsync', side_effect=OSError(5, 'blocked')):
                result = pc.probe_disk(folder, max_bytes=1)
            self.assertFalse(result['ok'])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cleanup_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(Path, 'unlink', side_effect=PermissionError()):
                result = pc.probe_disk(folder, max_bytes=1)
            self.assertFalse(result['ok'])
            self.assertEqual(result['cleanup_error'], 'PermissionError')
            # Remove precisely the test's own file after restoring the unlink method.
            Path(result['path']).unlink()


if __name__ == '__main__':
    unittest.main()
