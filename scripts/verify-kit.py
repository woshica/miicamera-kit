"""Verify portable binaries and sources against the delivery manifest."""
import hashlib
import json
from pathlib import Path
import sys

kit = Path(__file__).resolve().parents[1]
manifest_path = kit / 'docs/package/manifest.json'
if not manifest_path.is_file():
    print('软件包校验清单缺失：docs/package/manifest.json；请完整复制软件包及其子文件夹。')
    sys.exit(1)
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
failed = []
if not (kit / 'config/operator-template.json').is_file():
    failed.append('config/operator-template.json: missing installation template')
for entry in manifest['files']:
    path = (kit / entry['path']).resolve()
    if kit not in path.parents:
        raise RuntimeError('Manifest contains a path outside the kit')
    if not path.is_file():
        failed.append(entry['path'] + ': missing')
        continue
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != entry['sha256']:
        failed.append(entry['path'] + ': changed')
print(f'Checked {len(manifest["files"])} files; failures: {len(failed)}')
for problem in failed:
    print(problem)
print('config/operator/ 中的现场配置和 deprecated/ 中的旧可编辑配置不纳入固定文件哈希；安装模板参与校验。')
sys.exit(1 if failed else 0)
