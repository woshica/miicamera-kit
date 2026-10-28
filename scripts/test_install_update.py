"""Update transactions use temporary fake packages and never touch hardware."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from operator_config import OperatorConfig

spec = importlib.util.spec_from_file_location('install_update', Path(__file__).with_name('install-update.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.target, self.payload = root / 'client', root / 'payload'
        self.target.mkdir()
        self.payload.mkdir()
        self.put(self.target, 'runtime/python/python.exe', b'fake runtime')
        template = Path(__file__).resolve().parents[1] / 'config/operator-template.json'
        self.put(self.payload, 'config/operator-template.json', template.read_bytes())
        self.put(self.payload, 'scripts/example.py', b'new code')
        self.put(self.payload, 'deprecated/migration-map.json', json.dumps([
            {'from': '录制参数.toml', 'to': 'deprecated/录制参数.toml'},
            {'from': 'scripts/menu.py', 'to': 'deprecated/scripts/menu.py'}]).encode())
        self.put(self.payload, 'deprecated/scripts/menu.py', b'packaged legacy code')
        self.rehash()

    def put(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def rehash(self):
        entries = [{'path': p.relative_to(self.payload).as_posix(),
                    'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in self.payload.rglob('*') if p.is_file() and p.name != 'update-files.json']
        self.put(self.payload, 'update-files.json', json.dumps(entries).encode())

    def install(self):
        return installer.install_update(self.target, self.payload, process_check=lambda _: [])

    def test_original_toml_imported_before_archive_and_existing_archive_preserved(self):
        # Complete JSON default profile -> TOML scalar settings acceptable to loader.
        profile = json.loads((self.payload / 'config/operator-template.json').read_text(encoding='utf-8'))
        profile.update(exposure_us=17000, gain=200, output_dir=str(self.target.parent / 'recordings'))
        def scalar(value):
            return json.dumps(value, ensure_ascii=False)
        original = ('\n'.join(k + '=' + scalar(v) for k, v in profile.items()
                              if not isinstance(v, dict)) + '\n').encode('utf-8')
        self.put(self.target, '录制参数.toml', original)
        self.put(self.target, 'deprecated/录制参数.toml', b'previous archive')
        self.put(self.target, 'scripts/menu.py', b'client legacy menu')
        result = self.install()
        resolved = OperatorConfig(self.target).resolved()
        self.assertEqual(resolved['exposure_us'], 17000)
        self.assertEqual(resolved['gain'], 200)
        self.assertFalse((self.target / '录制参数.toml').exists())
        self.assertEqual((self.target / 'deprecated/录制参数.toml').read_bytes(), b'previous archive')
        archives = list((self.target / 'deprecated/user-archive').rglob('录制参数.toml'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), original)
        self.assertEqual((self.target / 'deprecated/scripts/menu.py').read_bytes(), b'packaged legacy code')
        self.assertEqual((Path(result['backup']) / 'files/scripts/menu.py').read_bytes(), b'client legacy menu')
        menus = list((self.target / 'deprecated/user-archive').rglob('menu.py'))
        self.assertEqual([p.read_bytes() for p in menus], [b'client legacy menu'])
        self.assertEqual((Path(result['backup']) / 'files/录制参数.toml').read_bytes(), original)

    def test_repeat_update_preserves_selected_scene_and_backs_up_only_affected_files(self):
        self.install()
        settings = OperatorConfig(self.target)
        settings.create_scheme('Client scene', 'current', {'exposure_ms': 19, 'gain_multiplier': 3})
        original = {p.name: p.read_bytes() for p in settings.directory.iterdir()}
        self.put(self.target, 'unrelated-client-note.txt', b'keep')
        self.put(self.target, 'recordings/sample.mkv', b'video')
        result = self.install()
        self.assertEqual(original, {p.name: p.read_bytes() for p in settings.directory.iterdir()})
        self.assertEqual(settings.resolved()['exposure_us'], 19000)
        self.assertEqual((self.target / 'recordings/sample.mkv').read_bytes(), b'video')
        self.assertFalse((Path(result['backup']) / 'files/unrelated-client-note.txt').exists())
        self.assertTrue((Path(result['backup']) / 'files/scripts/example.py').is_file())

    def test_active_workbench_blocks_all_target_writes(self):
        with self.assertRaisesRegex(RuntimeError, 'Close recording'):
            installer.install_update(self.target, self.payload, process_check=lambda _: ['operator_console'])
        self.assertFalse((self.target / 'config').exists())
        self.assertEqual(list(self.target.parent.glob('client-backup-*')), [])

    def test_bad_hash_checked_before_initialization_or_backup(self):
        self.put(self.payload, 'scripts/example.py', b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.install()
        self.assertFalse((self.target / 'config').exists())
        self.assertEqual(list(self.target.parent.glob('client-backup-*')), [])

    def test_forbidden_manifest_paths_rejected_before_writes(self):
        for name in ('../outside.py', 'runtime/python/python.exe', 'config/operator/daily.json',
                     '录制参数.toml', 'deprecated/config/client.json', 'scripts/credentials.json',
                     'scripts/../escape.py', 'C:/escape.py'):
            with self.subTest(name=name):
                entry = [{'path': name, 'sha256': '0' * 64}]
                self.put(self.payload, 'update-files.json', json.dumps(entry).encode())
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse((self.target / 'config').exists())

    def test_copy_failure_rolls_back_existing_and_new_files(self):
        self.install()
        self.put(self.target, 'scripts/example.py', b'previous code')
        self.put(self.payload, 'scripts/z-new.py', b'new file')
        self.rehash()
        previous = {p.relative_to(self.target).as_posix(): p.read_bytes()
                    for p in self.target.rglob('*') if p.is_file()}
        original_copy = installer.shutil.copy2
        def fail(source, destination):
            if Path(source) == self.payload / 'scripts/z-new.py':
                raise OSError('injected copy failure')
            return original_copy(source, destination)
        with patch.object(installer.shutil, 'copy2', side_effect=fail), \
                self.assertRaisesRegex(RuntimeError, 'rolled_back'):
            self.install()
        after = {p.relative_to(self.target).as_posix(): p.read_bytes()
                 for p in self.target.rglob('*') if p.is_file()}
        self.assertEqual(previous, after)


if __name__ == '__main__':
    unittest.main()
