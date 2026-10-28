"""Build the delivery checksum manifest after intentionally updating the kit."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_paths import EXCLUDED, release_source, safe_release_name

kit = Path(__file__).resolve().parents[1]
excluded = EXCLUDED | {'docs/package/manifest.json', 'deprecated/config/*.json',
                       'deprecated/录制参数.toml'}
entries = []
for path in sorted(kit.rglob('*')):
    relative = path.relative_to(kit)
    if (not path.is_file() or not safe_release_name(relative.as_posix())
            or any(relative.match(pattern) for pattern in excluded)):
        continue
    release_source(kit, relative.as_posix())
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    entries.append({'path': relative.as_posix(), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
manifest = {'format': 1, 'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'excluded': sorted(excluded), 'files': entries}
manifest_path = kit / 'docs/package/manifest.json'
manifest_path.parent.mkdir(parents=True, exist_ok=True)
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'Built manifest: {len(entries)} files, {sum(e["bytes"] for e in entries) / 1024**3:.2f} GiB')
