"""Build one customer ZIP containing the full kit and a cumulative updater.

Run build-manifest.py and verify-kit.py first. The output must be outside the kit.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_paths import release_source

LAUNCHER = r'''@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
echo 请先停止录像并退出 ImageView 和工作台后台窗口。
echo 请输入原来完整的 miicamera-kit 文件夹路径，也可把文件夹拖入窗口：
set /p "camera_kit_path="
set "camera_kit_path=%camera_kit_path:"=%"
if not exist "%camera_kit_path%\runtime\python\python.exe" (
  echo 未找到原工具包内的 Python。请确认路径指向原来完整的 miicamera-kit。
  pause
  exit /b 1
)
"%camera_kit_path%\runtime\python\python.exe" -B -X utf8 "%~dp0miicamera-kit\scripts\install-update.py" "%camera_kit_path%"
set "update_exit_code=%errorlevel%"
pause
exit /b %update_exit_code%
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    kit = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    checksum = output.with_suffix(output.suffix + '.sha256')
    if output == kit or kit in output.parents or output.exists() or checksum.exists():
        parser.error('Use a new ZIP outside the kit; existing files are never overwritten')
    spec = importlib.util.spec_from_file_location('delivery_installer', kit / 'scripts/install-update.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    manifest = json.loads((kit / 'docs/package/manifest.json').read_text(encoding='utf-8'))
    payload = {}
    for entry in manifest['files']:
        name = entry['path']
        source = release_source(kit, name)
        if installer.payload_allowed(name):
            data = source.read_bytes()
            if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError(f'Manifest mismatch: {name}')
            payload[name] = data
    payload['docs/package/manifest.json'] = (kit / 'docs/package/manifest.json').read_bytes()
    checks = [{'path': name, 'sha256': hashlib.sha256(data).hexdigest()}
              for name, data in sorted(payload.items())]
    payload['update-files.json'] = (json.dumps(checks, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='camera-delivery-', dir=output.parent) as temp:
        root = Path(temp)
        update = root / 'upgrade-payload'
        for name, data in payload.items():
            target = update / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        installer.validated_payload(update, root / 'existing-kit')
        staged = root / output.name
        subprocess.run([sys.executable, '-B', '-X', 'utf8', str(kit / 'scripts/package-release.py'),
                        str(staged)], check=True)
        with zipfile.ZipFile(staged, 'a', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr('先读-客户下载与安装.html', (kit / 'docs/customer-download-guide.html').read_bytes())
            archive.writestr('升级现有安装/安装更新.cmd', LAUNCHER.replace('\n', '\r\n').encode('utf-8'))
            for name, data in sorted(payload.items()):
                archive.writestr('升级现有安装/miicamera-kit/' + name, data)
        with zipfile.ZipFile(staged) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip() is not None:
                raise ValueError('ZIP integrity check failed')
            archive_files = len(archive.namelist())
        with staged.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        staged.rename(output)
        checksum.write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(json.dumps({'zip': str(output), 'files': archive_files, 'bytes': output.stat().st_size,
                      'sha256': digest, 'upgrade_files': len(payload)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
