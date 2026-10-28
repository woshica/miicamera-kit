"""Real Windows embedded-runtime startup and instance lock; no camera launch."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from urllib.parse import urlsplit

KIT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt' and (KIT / 'runtime/python/python.exe').exists(),
                     'Requires the bundled Windows Python runtime')
class EmbeddedLauncherTests(unittest.TestCase):
    def test_clean_start_and_second_instance_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kit = root / '工具包副本'
            scripts = kit / 'scripts'
            scripts.mkdir(parents=True)
            (kit / 'config').mkdir()
            for name in ('operator_console.py', 'operator_config.py', 'recording_profile.py',
                         'camera_profile.py', 'x265_settings.py'):
                shutil.copy2(KIT / 'scripts' / name, scripts / name)
            original = (KIT / 'config/operator-template.json').read_bytes()
            (kit / 'config/operator-template.json').write_bytes(original)
            command = [str(KIT / 'runtime/python/python.exe'), '-B', '-X', 'utf8', '-u',
                       str(scripts / 'operator_console.py'), '--no-browser']
            # Keep the startup cache inside this test's directory, including its lock.
            env = dict(os.environ, TEMP=str(root), TMP=str(root))
            identity = hashlib.sha256(str(kit.resolve()).encode()).hexdigest()[:12]
            work = root / ('miicamera-workbench-' + identity)
            startup_log = root / 'startup.log'
            with startup_log.open('wb') as output:
                process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if (work / 'instance.url').exists() or process.poll() is not None:
                            break
                        time.sleep(.05)
                    diagnostics = startup_log.read_text(encoding='utf-8', errors='replace')
                    self.assertIsNone(process.poll(), diagnostics)
                    self.assertTrue((work / 'instance.url').is_file(), diagnostics)
                    url = (work / 'instance.url').read_text(encoding='utf-8')
                    deadline = time.monotonic() + 2
                    while url not in diagnostics and time.monotonic() < deadline:
                        time.sleep(.02)
                        diagnostics = startup_log.read_text(encoding='utf-8', errors='replace')
                    self.assertIn(url, diagnostics)
                    for name in ('advanced', 'daily', 'schemes'):
                        self.assertTrue((kit / 'config/operator' / (name + '.json')).is_file())
                    address = urlsplit(url)
                    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=3)
                    try:
                        connection.request('GET', '/api/state', headers={'X-Camera-Token': address.fragment})
                        response = connection.getresponse()
                        state = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                        self.assertFalse(state['running'])
                        self.assertEqual(state['config']['daily']['scheme'], 'current')
                    finally:
                        connection.close()
                    second = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    message = second.stdout.decode('utf-8', errors='replace')
                    self.assertEqual(second.returncode, 0, message)
                    self.assertIn('工作台已在运行', message)
                    self.assertIsNone(process.poll())
                    self.assertEqual((kit / 'config/operator-template.json').read_bytes(), original)
                finally:
                    # Only terminate this test's explicitly created PID; no camera was started.
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
