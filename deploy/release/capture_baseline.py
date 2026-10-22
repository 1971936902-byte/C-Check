"""Capture reviewed, redistributable build inputs; never archive a builder home directory."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from ccheck.core import digest, verify_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--trusted-key', type=Path, required=True)
    parser.add_argument('--base-images', type=Path, required=True, help='approved digest lock actually used by this build')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    release = verify_release(args.release, args.trusted_key)
    source = json.loads((args.context / 'build-source.json').read_text())
    if source['git_sha'] != release['source']['git_sha'] or source['tree_sha256'] != release['source']['tree_sha256']:
        raise RuntimeError('build context does not match the signed release')
    bases = json.loads(args.base_images.read_text())
    expected = {'python:3.12-slim-bookworm', 'node:22.21.1-bookworm-slim', 'nginx:1.28-alpine',
                'mysql:8.0', 'redis:7.4-alpine', 'qdrant/qdrant:v1.15.5'}
    if set(bases) != expected or any(not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}', value) for value in bases.values()):
        raise RuntimeError('a complete approved base digest lock is required')
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(args.context.resolve()) or output.is_relative_to(args.release.resolve()):
        raise RuntimeError('use a new output outside the frozen build context and release')
    output.mkdir(mode=0o700, parents=True)
    for directory in ['locks', 'models']:
        shutil.copytree(args.release / directory, output / directory)
    notices = args.release / 'licenses/wheels/compiler'
    if notices.exists():
        shutil.copytree(notices, output / 'compiler-notices')
    known = {row['file']: row['url'] for row in json.loads((args.release / 'runtime/qwen/wheels.json').read_text())}
    records = []
    for group in ['backend', 'qwen', 'embedding', 'compiler']:
        for row in json.loads((output / 'locks' / (group + '.wheels.json')).read_text()):
            if Path(row['file']).name != row['file'] or '\\' in row['file']:
                raise RuntimeError('unsafe wheel name')
            wheel = args.context / 'wheelhouse' / group / row['file']
            if wheel.stat().st_size != row['size'] or digest(wheel) != row['sha256']:
                raise RuntimeError('builder wheel differs from signed catalog: ' + row['file'])
            record = dict(row, environment=group)
            if row['distribution'].lower() == 'nuitka':
                (output / 'vendor').mkdir(exist_ok=True)
                shutil.copy2(wheel, output / 'vendor' / row['file'])
                record['local'] = 'vendor/' + row['file']
            elif row['file'] in known:
                record['url'] = known[row['file']]
            elif row['distribution'] == 'torch' and row['version'].endswith('+cpu'):
                record['url'] = 'https://download.pytorch.org/whl/cpu/' + quote(row['file'])
            else:
                record['metadata_url'] = 'https://pypi.org/pypi/' + quote(row['distribution']) + '/' + quote(row['version']) + '/json'
            records.append(record)
    (output / 'wheels.json').write_text(json.dumps(records, indent=2) + '\n')
    (output / 'base-images.json').write_text(json.dumps(bases, indent=2) + '\n')
    for name in ['backend', 'qwen', 'embedding']:
        inventory = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--read-only',
            '--entrypoint', 'dpkg-query', release['images'][name]['id'], '-W', '-f=${Package}=${Version}\n'])
        (output / ('apt-inventory-' + name + '.txt')).write_bytes(inventory)
    files = {path.relative_to(output).as_posix(): digest(path) for path in sorted(output.rglob('*')) if path.is_file()}
    (output / 'baseline.json').write_text(json.dumps({'format': 1, 'release_version': release['version'],
        'release_manifest_sha256': release['_sha256'], 'source_commit': release['source']['git_sha'],
        'files': files, 'bit_identical_rebuild_guaranteed': False}, indent=2) + '\n')
    print('CAPTURED', len(files), 'public build inputs; review before committing:', output)


if __name__ == '__main__':
    main()
