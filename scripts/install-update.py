"""Install a checked code-only update while preserving client settings and archives."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import uuid

PAYLOAD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAYLOAD / 'scripts'))
from operator_config import OperatorConfig

LEGACY = set('''录制参数.toml
scripts/menu.py
打开官方录制软件.cmd
config/capture-defaults.json
config/mouse-4k45-recommended.json
config/mouse-4k45-official-crf.json
config/mouse-4k45-hevc-trial.json
test/按旧配置直接录制.cmd
test/检查录制配置.cmd
test/启动相机工具.cmd
test/试录当前配置一分钟.cmd
test/legacy/打开相机预览.cmd
test/legacy/按官方参数CRF录制.cmd
test/legacy/按推荐配置录制.cmd
test/legacy/推荐配置试录60秒.cmd
test/legacy/试录H265一分钟.cmd
test/legacy/试录官方参数CRF一分钟.cmd
docs/完整使用说明.md
docs/客户现场先看.md
docs/录制参数说明.md
docs/official-recording-workflow.md
docs/official-crf-recording.md
docs/hevc-recording-trial.md
docs/cq30-user-config-trial.md
docs/live-preview-validation.md
scripts/test_pc_environment.pyc
scripts/test_video_writer.pyc'''.splitlines())


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('Invalid update path: ' + str(relative))
    parts = PurePosixPath(relative).parts
    if (relative.startswith('/') or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)
            or '/'.join(parts) != relative):
        raise ValueError('Invalid update path: ' + relative)
    if any(re.match(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p, re.I)
           or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts):
        raise ValueError('Invalid Windows update path: ' + relative)
    path = root.joinpath(*parts)
    if root not in path.resolve().parents:
        raise ValueError('Update path escapes package: ' + relative)
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Linked update path: ' + relative)
    return path


def payload_allowed(relative):
    lower = relative.lower()
    parts = PurePosixPath(lower).parts
    if lower in ('.gitignore', '.gitattributes'):
        return True
    if any(p.startswith('.') for p in parts):
        return False
    if lower == 'config/operator-template.json':
        return True
    if (parts[0] not in ('scripts', 'docs', 'test', 'deprecated') and len(parts) != 1):
        return False
    if parts[0] == 'deprecated' and (len(parts) < 2 or parts[1] in ('config', 'user-archive')):
        return False
    if PurePosixPath(lower).suffix == '.json':
        return lower in {'docs/package/manifest.json', 'docs/package/sources.json',
                         'deprecated/migration-map.json'}
    if any(word in lower for word in ('credential', 'secret', 'password', 'token')):
        return False
    allowed = {'.py', '.md', '.txt', '.html', '.css', '.js', '.cmd', '.ico'}
    if PurePosixPath(lower).suffix not in allowed:
        return False
    if len(parts) == 1 and PurePosixPath(lower).suffix not in {'.md', '.cmd', '.html', '.ico'}:
        return False
    return True


def active_processes(target):
    """Read process inventory only; never terminate an application."""
    script = r'''[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();
$ErrorActionPreference='Stop';
@(Get-CimInstance Win32_Process | Select-Object Name,CommandLine,ProcessId) | ConvertTo-Json -Compress'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Cannot confirm camera/workbench processes have stopped')
    rows = json.loads(result.stdout.decode('utf-8-sig')) or []
    if isinstance(rows, dict):
        rows = [rows]
    busy = []
    for row in rows:
        name = str(row.get('Name') or '')
        command = str(row.get('CommandLine') or '').replace('\\', '/').casefold()
        if re.match(r'^imageview.*\.exe$', name, re.I):
            busy.append(name)
        elif (re.match(r'^python(?:w|[0-9.]*)?(?:\.exe)?$', name, re.I)
              and str(target).replace('\\', '/').casefold() in command
              and re.search(r'(?:^|[/"\s])(?:capture|operator_camera|operator_console)\.py(?:["\s]|$)', command)):
            busy.append(name + ': ' + str(row.get('ProcessId')))
    return busy


def validated_payload(payload, target):
    entries = json.loads(safe_path(payload, 'update-files.json').read_text(encoding='utf-8-sig'))
    if not isinstance(entries, list) or not entries:
        raise ValueError('Empty or invalid update manifest')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
            raise ValueError('Invalid update manifest entry')
        relative = entry['path']
        source = safe_path(payload, relative)
        safe_path(target, relative)
        if not payload_allowed(relative) or relative.casefold() in seen:
            raise ValueError('Forbidden or duplicate payload path: ' + relative)
        seen.add(relative.casefold())
        if not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256']):
            raise ValueError('Invalid digest: ' + relative)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Payload hash mismatch: ' + relative)
    if 'deprecated/migration-map.json' not in seen:
        raise ValueError('Migration map must be included in checked payload')
    moves = json.loads((payload / 'deprecated/migration-map.json').read_text(encoding='utf-8-sig'))
    if not isinstance(moves, list):
        raise ValueError('Invalid migration map')
    sources = set()
    for move in moves:
        if not isinstance(move, dict) or set(move) != {'from', 'to'}:
            raise ValueError('Invalid migration entry')
        source, dest = move['from'], move['to']
        if not isinstance(source, str) or not isinstance(dest, str):
            raise ValueError('Invalid migration path type')
        expected = ('deprecated/cache/' + Path(source).name if source.endswith('.pyc')
                    else 'deprecated/' + source)
        if source not in LEGACY or dest != expected or source in sources:
            raise ValueError('Unknown migration path: ' + str(source))
        sources.add(source)
        safe_path(target, source)
        safe_path(target, dest)
    return entries, moves


def install_update(target, payload=PAYLOAD, process_check=active_processes):
    target, payload = Path(target).resolve(), Path(payload).resolve()
    if target == payload or target in payload.parents or payload in target.parents:
        raise ValueError('Extract update separately from the existing package')
    if not safe_path(target, 'runtime/python/python.exe').is_file():
        raise ValueError('Select the existing miicamera-kit with runtime/python/python.exe')
    entries, moves = validated_payload(payload, target)
    busy = process_check(target)
    if busy:
        raise RuntimeError('Close recording, preview, ImageView and the workbench before updating: ' + str(busy))
    run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = target.parent / (target.name + '-backup-' + run_id)
    backup.mkdir(exist_ok=False)
    manifest_path = backup / 'restore-manifest.json'
    journal = {'target': str(target), 'state': 'running', 'files': [], 'moves': []}

    def save():
        manifest_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def prepare(relative):
        path = safe_path(target, relative)
        if any(row['path'] == relative for row in journal['files']):
            return path
        exists = path.exists()
        if exists:
            if not path.is_file():
                raise ValueError('Expected file: ' + relative)
            copy = safe_path(backup, 'files/' + relative)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
        journal['files'].append({'path': relative, 'existed': exists,
                                 'backup': 'files/' + relative if exists else None})
        save()
        return path

    def copy_checked(relative):
        path = prepare(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(payload, relative), path)

    save()
    try:
        # Preserve/recover the customer's settings before archiving the old TOML.
        for name in ('advanced.json', 'daily.json', 'schemes.json', '.transaction.json'):
            prepare('config/operator/' + name)
        if (not (target / 'config/operator').exists() and not (target / '录制参数.toml').exists()
                and not (target / 'config/operator-template.json').exists()):
            if not any(e['path'] == 'config/operator-template.json' for e in entries):
                raise ValueError('Installation template missing from update')
            copy_checked('config/operator-template.json')
        OperatorConfig(target).initialize()
        incoming = {entry['path'].casefold() for entry in entries}
        for move in moves:
            source = safe_path(target, move['from'])
            if not source.exists():
                continue
            destination = move['to']
            if safe_path(target, destination).exists() or destination.casefold() in incoming:
                destination = 'deprecated/user-archive/' + run_id + '/' + move['from']
            prepare(move['from'])
            archive = prepare(destination)
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, archive)
            source.unlink()
            journal['moves'].append({'from': move['from'], 'to': destination})
            save()
        for entry in entries:
            # Archived executable entry points need the corrected package paths.
            # Their original contents remain in the backup or unique user archive.
            copy_checked(entry['path'])
        journal['state'] = 'complete'
        save()
    except BaseException as exc:
        errors = []
        for row in reversed(journal['files']):
            try:
                path = safe_path(target, row['path'])
                if row['existed']:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(safe_path(backup, row['backup']), path)
                elif path.is_file():
                    path.unlink()
            except (OSError, ValueError) as error:
                errors.append(str(error))
        # Empty directories are harmless and allow a later clean initialization.
        operator = target / 'config/operator'
        if operator.exists() and not any(operator.iterdir()):
            operator.rmdir()
        journal.update(state='partial' if errors else 'rolled_back', error=str(exc), rollback_errors=errors)
        save()
        raise RuntimeError(f'Update failed ({journal["state"]}); restore manifest: {manifest_path}: {exc}') from exc
    return {'state': 'complete', 'manifest': str(manifest_path), 'backup': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    args = parser.parse_args(argv)
    try:
        result = install_update(args.target)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('更新完成。原配置和旧资料已保留；请运行软件包校验，再从工作台进行一分钟试录。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
