"""Storage checks must fail without deleting data or acquiring the camera."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from capture import require_disk, require_recording_budget


class StorageTests(unittest.TestCase):
    def test_capacity_check_uses_required_plan(self):
        with patch('capture.shutil.disk_usage', return_value=SimpleNamespace(free=500000000000)):
            require_disk('unused', 10000000000)
            with self.assertRaises(RuntimeError):
                require_disk('unused', 900000000000)

    def test_budget_stop_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder)/'segment-0001.mkv'
            video.write_bytes(b'0123456789')
            require_recording_budget(folder, None)
            require_recording_budget(folder, 1)
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                require_recording_budget(folder, 0.000000005)
            self.assertEqual(video.read_bytes(), b'0123456789')


if __name__ == '__main__':
    unittest.main()
