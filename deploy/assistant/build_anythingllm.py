"""Build the pinned assistant provider without modifying existing deployments."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from prepare_prisma import prepare as prepare_engines
from prepare_sharp import prepare as prepare_libvips


def run(*args, cwd=None, capture=False):
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def prepare(root, resources, source=None):
    lock = json.loads((resources / 'anythingllm.lock.json').read_text())
    if root.exists():
        raise ValueError('build directory already exists; choose a new isolated directory')
    patches = [resources / name for name in lock['patches']]
    for patch in patches:
        if not patch.is_file() or patch.parent != resources:
            raise ValueError('missing or invalid provider patch')
    # Existing local mirrors are read only; the new clone owns all modifications.
    run('git', 'clone', '--no-hardlinks', source or lock['repository'], str(root))
    run('git', 'checkout', '--detach', lock['commit'], cwd=root)
    if run('git', 'rev-parse', 'HEAD', cwd=root, capture=True) != lock['commit']:
        raise ValueError('provider source identity mismatch')
    run('git', 'apply', '--check', *(str(path) for path in patches), cwd=root)
    run('git', 'apply', *(str(path) for path in patches), cwd=root)
    license_sha256 = hashlib.sha256((root / 'LICENSE').read_bytes()).hexdigest()
    if license_sha256 != lock['license_sha256']:
        raise ValueError('provider license differs from the pinned upstream source')
    return {
        'repository': lock['repository'], 'commit': lock['commit'],
        'patches': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in patches},
        'models': lock['models'],
        'license': {'spdx': lock['license'], 'file': 'LICENSE', 'sha256': license_sha256},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--source', help='Optional local upstream Git mirror')
    parser.add_argument('--tag', default='ccheck-anythingllm:development')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--prisma-cache', type=Path, help='Verified engine download cache')
    args = parser.parse_args()
    root = args.build_dir.resolve()
    resources = Path(__file__).resolve().parent
    evidence = prepare(root, resources, args.source)
    dockerfile = resources / 'Dockerfile.anythingllm'
    evidence['dockerfile_sha256'] = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    if not args.prepare_only:
        engine_lock = json.loads((resources / 'prisma-engines.lock.json').read_text())
        package = json.loads((root / 'server/package.json').read_text())
        dependencies = package.get('dependencies', {}) | package.get('devDependencies', {})
        if any(dependencies.get(name) != engine_lock['prisma_version'] for name in ('prisma', '@prisma/client')):
            raise ValueError('provider Prisma dependency does not match the engine lock')
        evidence['prisma_engines'] = prepare_engines(
            args.prisma_cache or root.parent / '.prisma-cache', root / 'ccheck-prisma-engines', engine_lock)
        evidence['libvips'] = prepare_libvips(
            args.prisma_cache or root.parent / '.prisma-cache', root / 'ccheck-libvips',
            json.loads((resources / 'sharp-libvips.lock.json').read_text()))
        run('docker', 'build', '--platform', 'linux/amd64', '-f', str(dockerfile),
            '-t', args.tag, '.', cwd=root)
        evidence['image_id'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', args.tag, capture=True)
        if not evidence['image_id'].startswith('sha256:'):
            raise ValueError('missing immutable build identity')
    evidence['built'] = not args.prepare_only
    # Outside the source context; it cannot inadvertently affect the image build.
    receipt = root.with_name(root.name + '.receipt.json')
    with receipt.open('x', encoding='utf-8') as file:
        json.dump(evidence, file, indent=2)
        file.write('\n')
    print(f'Build receipt: {receipt}')


if __name__ == '__main__':
    main()
