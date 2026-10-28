"""Exercise the real Windows CMD entry points without opening the camera."""
from pathlib import Path
import subprocess
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_diagnostic_cmd_from_unrelated_directory(self):
        # --help exercises CMD quoting and embedded imports without any probes.
        with tempfile.TemporaryDirectory(prefix='camera diagnostic launcher ') as cwd:
            result = subprocess.run(['cmd.exe', '/d', '/c', str(KIT / '客户电脑快速诊断.cmd'), '--help'],
                                    input='\n', cwd=cwd, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('--skip-camera', output)
            self.assertNotIn('Traceback', output)

    def test_cmd_entry_points_with_embedded_python(self):
        # An unrelated working directory must not affect sibling-module imports.
        # Recommended entries reject this relative destination before capture.
        cases = (
            ('deprecated/test/启动相机工具.cmd', '0\n\n', '相机拍摄工具'),
            ('deprecated/test/检查录制配置.cmd', '\n', '配置检查通过'),
            ('deprecated/test/legacy/按推荐配置录制.cmd', 'relative-output\n\n', '请填写绝对路径'),
            ('deprecated/test/legacy/推荐配置试录60秒.cmd', 'relative-output\n\n', '请填写绝对路径'),
            ('deprecated/test/legacy/试录官方参数CRF一分钟.cmd', 'relative-output\n\n', '请填写绝对路径'),
            ('deprecated/test/legacy/按官方参数CRF录制.cmd', 'relative-output\n\n', '请填写绝对路径'),
        )
        with tempfile.TemporaryDirectory(prefix='camera launcher ') as cwd:
            for filename, answers, expected in cases:
                with self.subTest(launcher=filename):
                    result = subprocess.run(
                        ['cmd.exe', '/d', '/c', str(KIT / filename)],
                        input=answers, cwd=cwd, capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=15)
                    output = result.stdout + result.stderr
                    # CMD's trailing pause may mask Python's exit code.
                    self.assertNotIn('ModuleNotFoundError', output, output)
                    self.assertNotIn('Traceback', output, output)
                    self.assertIn(expected, output, output)


if __name__ == '__main__':
    unittest.main()
