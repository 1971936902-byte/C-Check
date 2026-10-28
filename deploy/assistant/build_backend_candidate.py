"""Build an unsigned assistant backend candidate using verified unchanged native code."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

RELEASE = Path(__file__).resolve().parents[1] / 'release'
sys.path[:0] = [str(RELEASE), str(RELEASE / 'cli')]
from audit_images import inspect_layers
from repack_deployment import read_json, verify_source, write_json
from ccheck.core import verify_release
from ccheck.image_identity import identity, inspect_image, locate


def validate_sources(base, current, previous, policy, old_policy):
    if base['version'] not in {'v0.1.0-rc.19', 'v0.1.0-rc.20'}:
        raise RuntimeError('only a signed Qwen2.5 rc19/rc20 baseline is supported')
    if any(previous.get(key) != base['source'][key] for key in ('git_sha', 'tree_sha256')):
        raise RuntimeError('baseline source does not match the signed release')
    if policy['compiled_modules'] != old_policy['compiled_modules']:
        raise RuntimeError('compiled module policy changed; trusted compilation is required')
    frozen = ['backend/' + name.replace('.', '/') + '.py' for name in policy['compiled_modules']]
    frozen += ['backend/pyproject.toml', 'backend/requirements.txt']
    for name in frozen:
        if current['files'].get(name) != previous['files'].get(name):
            raise RuntimeError('native source or dependency changed; trusted compilation is required: ' + name)


def native_hashes(image, expected):
    program = ('import hashlib,json;from pathlib import Path;files=' + repr(list(expected)) + ';'
               'print(json.dumps({p:hashlib.sha256((Path("/app/backend")/p).read_bytes()).hexdigest() for p in files}))')
    result = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--entrypoint', 'python',
        image, '-B', '-c', program], timeout=60)
    actual = json.loads(result)
    if actual != expected:
        raise RuntimeError('native artifacts differ from the signed baseline')
    return actual


def build(args):
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        raise RuntimeError('a new candidate output directory is required')
    base = verify_release(args.base_release, args.trusted_key, args.publisher_sha256)
    current = verify_source(source, committed=not args.allow_worktree_snapshot)
    previous = verify_source(args.base_source, committed=True)
    policy = read_json(source / 'deploy/release/core-policy.json')
    validate_sources(base, current, previous, policy,
                     read_json(args.base_source / 'deploy/release/core-policy.json'))
    models = read_json(args.base_release / 'models/model-manifest.json')
    if next(row['repository'] for row in models['models'] if row['key'] == 'qwen') != 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8':
        raise RuntimeError('A3B/model migration is not allowed')
    image = locate(base['images']['backend'])
    if not image:
        raise RuntimeError('load the signed baseline backend image before building')
    expected = {row['file']: row['sha256'] for row in base['core_wheel']['modules']}
    native_hashes(image, expected)
    output.mkdir(mode=0o700, parents=True)
    context = output / 'context'
    for name in policy['plaintext_allowlist'] + policy['resource_allowlist']:
        target = context / 'backend' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / 'backend' / name, target)
    shutil.copytree(source / 'backend/alembic', context / 'backend/alembic',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(source / 'backend/alembic.ini', context / 'backend/alembic.ini')
    shutil.copyfile(source / 'deploy/release/core-policy.json', context / 'core-policy.json')
    for path in [context, *context.rglob('*')]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    base_tag = 'ccheck-assistant-native-base:' + image.split(':')[-1][:16]
    subprocess.run(['docker', 'tag', image, base_tag], check=True, timeout=30)
    subprocess.run(['docker', 'build', '--network', 'none', '--provenance=false', '--sbom=false',
        '--build-arg', 'BASE_IMAGE=' + base_tag, '--build-arg', 'RELEASE_VERSION=assistant-candidate',
        '--build-arg', 'SOURCE_SHA=' + current['git_sha'], '-f',
        str(source / 'deploy/release/Dockerfile.backend-app-patch'), '-t', args.tag, str(context)],
        check=True, timeout=600)
    metadata = inspect_image(args.tag)
    actual = native_hashes(metadata['Id'], expected)
    private = {current['files']['backend/' + name.replace('.', '/') + '.py']
               for name in policy['compiled_modules']}
    audit = inspect_layers(metadata['Id'], policy, private)
    if set(audit['native_modules']) != set(policy['compiled_modules']):
        raise RuntimeError('native module inventory mismatch')
    if verify_source(source, committed=not args.allow_worktree_snapshot) != current:
        raise RuntimeError('source changed during candidate build')
    report = {'kind': 'unsigned-assistant-backend-candidate', 'release_ready': False,
        'source_tree': current['tree_sha256'], 'worktree_snapshot': current['worktree_snapshot'],
        'base_release_sha256': base['_sha256'], 'image_id': metadata['Id'],
        'image_identity': identity(metadata), 'native_sha256': actual, 'layer_audit': audit,
        'remaining_gates': ['source/native test parity', 'real GPU acceptance', 'signed release assembly']}
    write_json(output / 'candidate.json', report)
    print('ASSISTANT_NATIVE_CANDIDATE_BUILT', json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'base-source', 'base-release', 'trusted-key', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--publisher-sha256', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--allow-worktree-snapshot', action='store_true',
                        help='evaluation candidate only; never a signed release')
    build(parser.parse_args())
