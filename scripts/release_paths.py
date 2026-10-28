"""Shared fail-closed path boundary for delivery manifests and ZIPs."""
from pathlib import Path, PurePosixPath


EXCLUDED = {
    '**/.env*', '**/*.pem', '**/*.key', '**/*.mp4', '**/*.mkv', '**/*.avi',
    '**/*.mov', '**/*.wmv', '**/*.webm', '**/*.m4v', '**/*.h264', '**/*.h265',
    '**/*.hevc', '**/*.mpeg', '**/*.mpg', '**/*.ts', '**/*.mts', '**/*.m2ts',
    '**/*.3gp', '**/*.flv', '**/*.frames.csv',
    '**/*.log', '**/*.tmp', '**/*.pyc', '**/*.pyo', '**/.git/**',
    '**/__pycache__/**', '**/.pytest_cache/**', '**/.venv/**',
    '**/camera-recordings/**', '**/camera-diagnostics/**', '**/recordings/**',
    '**/diagnostics/**', 'config/operator/**', 'config/.operator-*/**',
    'deprecated/user-archive/**', 'deprecated/cache/**', '录制参数.toml',
}
_DIRECTORIES = {'.git', '__pycache__', '.pytest_cache', '.venv',
                'camera-recordings', 'camera-diagnostics', 'recordings', 'diagnostics'}
_SUFFIXES = {'.pem', '.key', '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm',
             '.m4v', '.h264', '.h265', '.hevc', '.mpeg', '.mpg', '.ts', '.mts',
             '.m2ts', '.3gp', '.flv', '.log', '.tmp', '.pyc', '.pyo'}


def safe_release_name(name):
    """Only canonical, relative POSIX file names may enter an archive."""
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(
            part in ('.', '..') or any(ord(c) < 32 for c in part) for part in path.parts):
        return False
    parts = tuple(part.lower() for part in path.parts)
    if any(part in _DIRECTORIES for part in parts):
        return False
    if parts[:2] in (('config', 'operator'), ('deprecated', 'user-archive'),
                     ('deprecated', 'cache')):
        return False
    if len(parts) > 1 and parts[0] == 'config' and parts[1].startswith('.operator-'):
        return False
    return (parts != ('录制参数.toml',) and not parts[-1].startswith('.env')
            and not parts[-1].endswith('.frames.csv')
            and PurePosixPath(parts[-1]).suffix not in _SUFFIXES)


def release_source(kit, name):
    """Validate both the archive name and its resolved filesystem target."""
    if not safe_release_name(name):
        raise ValueError(f'Unsafe manifest path: {name}')
    kit = Path(kit).resolve()
    source = (kit / name).resolve()
    try:
        target = source.relative_to(kit).as_posix()
    except ValueError:
        raise ValueError(f'Unsafe manifest path: {name}') from None
    if not safe_release_name(target):
        raise ValueError(f'Unsafe manifest path: {name}')
    return source
