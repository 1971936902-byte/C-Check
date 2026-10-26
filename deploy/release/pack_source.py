"""Trusted publisher source snapshot; this archive is NOT a recipient release asset."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

PREFIXES = ['backend', 'frontend', 'deploy/models', 'deploy/release', 'tools/review-cli']
EXCLUDED = {'__pycache__', '.pytest_cache', '.venv', 'node_modules', 'dist', 'wheelhouse', 'release-input', 'built-image-records'}


def git(*args):
    return subprocess.check_output(['git', *args])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--working-tree', action='store_true', help='explicit evaluation snapshot, never a stable release')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    commit = git('rev-parse', args.ref + '^{commit}').decode().strip()
    if args.working_tree:
        names = set(git('ls-files', '-z', '--', *PREFIXES).decode().split('\0'))
        names.update(git('ls-files', '-z', '--others', '--exclude-standard', '--', *PREFIXES).decode().split('\0'))
    else:
        names = set(git('ls-tree', '-r', '-z', '--name-only', commit, '--', *PREFIXES).decode().split('\0'))
    files = {}
    for name in sorted(names - {''}):
        path = Path(name)
        if set(path.parts) & EXCLUDED or path.suffix in {'.pyc', '.pyd'}:
            continue
        if path.name == '.env' or path.suffix.lower() in {'.pem', '.key', '.safetensors', '.pt', '.pth'}:
            raise RuntimeError('private key/environment/weight file in source input: ' + name)
        if args.working_tree and path.is_symlink():
            raise RuntimeError('symlink in trusted source input: ' + name)
        data = path.read_bytes() if args.working_tree else git('show', commit + ':' + name)
        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError('unexpected large source input: ' + name)
        files[name] = data
    if not {'deploy/release/core-policy.json', 'backend/app/deployment.py', 'deploy/release/cli/ccheck/__main__.py'}.issubset(files):
        raise RuntimeError('selected ref does not contain the release implementation')
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    source = {'git_sha': commit, 'git_ref': args.ref, 'worktree_snapshot': args.working_tree, 'files': hashes,
              'tree_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, 'w:gz') as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            archive.addfile(entry, io.BytesIO(data))
        data = json.dumps(source, indent=2).encode()
        entry = tarfile.TarInfo('build-source.json')
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    args.output.chmod(0o600)
    print('SOURCE_SNAPSHOT', args.output, source['tree_sha256'], 'working-tree' if args.working_tree else commit)


if __name__ == '__main__':
    main()
