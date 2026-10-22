"""Explicit publisher action; never invoked by the install/upgrade CLI."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from ccheck.core import verify_release


def verify_remote_tag(repository, tag, commit):
    prefix = 'repos/' + repository + '/git/'
    reference = json.loads(subprocess.check_output(['gh', 'api', prefix + 'ref/tags/' + tag]))['object']
    for _ in range(4):
        if reference['type'] == 'commit':
            if reference['sha'] != commit:
                raise RuntimeError('remote tag does not match the signed source commit')
            return
        if reference['type'] != 'tag' or not re.fullmatch(r'[0-9a-f]{40}', reference['sha']):
            break
        reference = json.loads(subprocess.check_output(['gh', 'api', prefix + 'tags/' + reference['sha']]))['object']
    raise RuntimeError('remote tag cannot be resolved to the signed source commit')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--trusted-key', type=Path, required=True)
    parser.add_argument('--repository', required=True, help='authorized binary distribution repository, not a source mirror')
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--publish-draft', action='store_true', help='explicit authorization to create a remote draft Release')
    args = parser.parse_args()
    release = verify_release(args.release, args.trusted_key)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        raise RuntimeError('invalid owner/repository')
    root = Path(release['_root'])
    assets = [root / name for name in ['release.json', 'release.sig', 'checksums.sha256', 'bootstrap.tar.gz',
                                     'publisher.pem', 'INSTALL-RTX5880.md', 'site-rtx5880.example.json']]
    assets.extend(root / name for name in release['files'] if name.startswith('images/'))
    if len({path.name for path in assets}) != len(assets) or any(path.stat().st_size >= 2 * 1024**3 for path in assets):
        raise RuntimeError('GitHub asset naming/size limit exceeded')
    command = ['gh', 'release', 'create', release['tag'], '--repo', args.repository, '--verify-tag', '--draft',
               '--title', 'C-Check ' + release['tag'], '--notes-file', str(args.notes), *map(str, assets)]
    if '-rc.' in release['version']:
        command.append('--prerelease')
    print(json.dumps({'tag': release['tag'], 'repository': args.repository, 'assets': [path.name for path in assets],
                      'draft_only': True, 'remote_action': args.publish_draft}, indent=2))
    if not args.publish_draft:
        return
    if release['source'].get('worktree_snapshot'):
        raise RuntimeError('remote publication requires a committed source snapshot, not evaluation working-tree input')
    if shutil.which('gh') is None:
        raise RuntimeError('publisher must install/authenticate GitHub CLI before explicit upload')
    verify_remote_tag(args.repository, release['tag'], release['source']['git_sha'])
    subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
