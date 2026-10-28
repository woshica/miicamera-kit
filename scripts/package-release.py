"""Package exactly the checked delivery manifest plus editable configuration.

Run build-manifest.py first. The destination must be outside this kit.
Git history, credentials, recordings, and loose files are never added implicitly.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_paths import release_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New ZIP path outside the kit')
    args = parser.parse_args()
    kit = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    checksum = output.with_suffix(output.suffix + '.sha256')
    if output == kit or kit in output.parents:
        parser.error('Release ZIP must be outside the kit')
    if output.exists() or checksum.exists():
        parser.error('Release ZIP or checksum already exists; use a new filename')
    manifest_path = kit / 'docs/package/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    expected = {entry['path']: entry for entry in manifest['files']}
    if len(expected) != len(manifest['files']):
        raise ValueError('Duplicate manifest paths')
    editable = ['deprecated/录制参数.toml'] + [p.relative_to(kit).as_posix()
                                     for p in sorted((kit / 'deprecated/config').glob('*.json'))]
    names = sorted(set(expected) | set(editable) | {'docs/package/manifest.json'})
    files = []
    for name in names:
        source = release_source(kit, name)
        if not source.is_file():
            raise FileNotFoundError(name)
        files.append((name, source))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=3, allowZip64=True) as archive:
        for name, source in files:
            digest = hashlib.sha256()
            count = 0
            with source.open('rb') as incoming, archive.open(
                    'miicamera-kit/' + name, 'w', force_zip64=True) as outgoing:
                while block := incoming.read(1024 * 1024):
                    outgoing.write(block)
                    digest.update(block)
                    count += len(block)
            entry = expected.get(name)
            if entry and (count != entry['bytes'] or digest.hexdigest() != entry['sha256']):
                raise RuntimeError(f'Manifest mismatch while packaging: {name}')
    with output.open('rb') as incoming:
        digest = hashlib.file_digest(incoming, 'sha256').hexdigest()
    checksum.write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(json.dumps({'zip': str(output), 'files': len(files),
                      'bytes': output.stat().st_size, 'sha256': digest,
                      'checksum': str(checksum)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
