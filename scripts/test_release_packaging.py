"""Small isolated release-boundary tests; never rebuild the real kit manifest."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from release_paths import safe_release_name


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.kit = self.root / 'kit'
        (self.kit / 'scripts').mkdir(parents=True)
        for name in ('build-manifest.py', 'package-release.py', 'release_paths.py'):
            shutil.copyfile(Path(__file__).parent / name, self.kit / 'scripts' / name)
        self.write('docs/package/sources.json', '{}')
        self.write('deprecated/录制参数.toml', 'output_dir = ""')

    def write(self, name, text='fixture'):
        path = self.kit / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(self.kit / 'scripts' / name), *map(str, args)],
                              capture_output=True, text=True)

    def test_filter_boundary_and_vendor_files(self):
        for name in ('.env', 'docs/.ENV.backup', 'a/server.KEY', 'sdk/private.pem',
                     'movie.MP4', 'a/data.frames.csv', 'a/run.log', 'a/cache.tmp',
                     'runtime/a.pyc', 'scripts/__pycache__/a.bin', '.git/config',
                     'camera-recordings/data.json', 'camera-diagnostics/result.json',
                     'recordings/a.txt', 'diagnostics/a.txt', 'config/operator/daily.json',
                     'config/.operator-stage/advanced.json', 'deprecated/user-archive/a.json',
                     'deprecated/cache/a.txt', '../outside', '/absolute', 'C:/absolute',
                     'scripts/../README.md', 'a\\b', 'a//b', 'a/./b'):
            with self.subTest(name=name):
                self.assertFalse(safe_release_name(name))
        for name in ('runtime/python/python.exe', 'sdk/samples/camera.py',
                     'installers/MIIImageViewSetup.exe', 'config/operator-template.json',
                     'deprecated/config/defaults.json', 'deprecated/录制参数.toml'):
            self.assertTrue(safe_release_name(name), name)

    def test_build_excludes_private_files_then_package_keeps_vendor(self):
        forbidden = ['.env.local', 'keys/a.pem', 'camera-recordings/session.json',
                     'docs/video.avi', 'diagnostics/report.json', 'config/operator/daily.json',
                     'deprecated/user-archive/settings.json', 'deprecated/cache/old.pyc']
        vendor = ['runtime/python/python.exe', 'sdk/example/camera.py', 'installers/setup.exe']
        for name in forbidden + vendor:
            self.write(name)
        result = self.run_script('build-manifest.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.kit / 'docs/package/manifest.json').read_text(encoding='utf-8'))
        names = {entry['path'] for entry in manifest['files']}
        self.assertTrue(set(vendor) <= names)
        self.assertFalse(set(forbidden) & names)
        output = self.root / 'release.zip'
        result = self.run_script('package-release.py', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertTrue({'miicamera-kit/' + name for name in vendor} <= names)
            self.assertFalse({'miicamera-kit/' + name for name in forbidden} & names)

    def test_malicious_manifest_rejected_before_zip_creation(self):
        for name in ('../outside.txt', 'config/operator/daily.json', '.env.local',
                     'deprecated/user-archive/old.json', 'sdk/sample.mp4', '.git/config'):
            with self.subTest(name=name):
                self.write('docs/package/manifest.json', json.dumps({'files': [
                    {'path': name, 'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}]}))
                output = self.root / 'rejected.zip'
                result = self.run_script('package-release.py', output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsafe manifest path', result.stderr)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
