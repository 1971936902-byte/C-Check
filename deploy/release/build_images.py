import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def run(command):
    subprocess.run(command, check=True)


def pinned(image):
    lock = os.environ.get('CCHECK_BASE_IMAGE_LOCK')
    if lock:
        references = json.loads(Path(lock).read_text())
        reference = references.get(image, '')
        import re
        if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}', reference):
            raise RuntimeError('Missing/invalid approved base image digest: ' + image)
        return reference
    data = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    if not data.get('RepoDigests'):
        raise RuntimeError('Base image must have a registry digest: ' + image)
    return data['RepoDigests'][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--only', choices=['backend', 'frontend', 'qwen', 'embedding', 'all'], default='all')
    args = parser.parse_args()
    context = args.context.resolve()
    source = json.loads((context / 'build-source.json').read_text())
    policy = json.loads((context / 'deploy/release/core-policy.json').read_text())
    policy_file = context / ('build-core-policy.' + str(os.getpid()) + '.tmp')
    policy_file.write_text(json.dumps({'compiled_modules': policy['compiled_modules']}, sort_keys=True))
    policy_file.replace(context / 'build-core-policy.json')
    result = {}
    image_file = context / 'built-images.json'
    image_directory = context / 'built-image-records'
    image_directory.mkdir(exist_ok=True)
    if image_file.exists():
        result = json.loads(image_file.read_text())
    for name in ['backend', 'frontend', 'embedding', 'qwen']:
        if args.only not in {'all', name}:
            continue
        required = ['backend', 'compiler'] if name == 'backend' else [name]
        if name != 'frontend' and not all((context / 'locks' / (key + '.lock')).is_file() for key in required):
            raise RuntimeError('Dependency locks not ready: ' + name)
        tag = 'c-check-' + name + ':' + args.version.lstrip('v')
        command = ['docker', 'build', '--progress=plain', '--provenance=false', '--sbom=false', '-t', tag,
                   '-f', str(context / 'deploy/release' / ('Dockerfile.' + name))]
        if name in {'backend', 'frontend'}:
            command += ['--build-arg', 'RELEASE_VERSION=' + args.version, '--build-arg', 'SOURCE_SHA=' + source['git_sha'],
                        '--label', 'io.ccheck.source-tree-sha256=' + source['tree_sha256']]
        else:
            files = [context / 'locks' / (name + '.lock'), context / 'deploy/release' / ('Dockerfile.' + name),
                     context / 'deploy/release/runtime' / ('qwen_entry.py' if name == 'qwen' else 'embedding_api.py')]
            payload = {'base': pinned('python:3.12-slim-bookworm'),
                       'files': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            command += ['--label', 'io.ccheck.runtime-lock=' + fingerprint]
        if name == 'frontend':
            command += ['--build-arg', 'NODE_IMAGE=' + pinned('node:22.21.1-bookworm-slim'),
                        '--build-arg', 'NGINX_IMAGE=' + pinned('nginx:1.28-alpine')]
        else:
            command += ['--build-arg', 'BASE_IMAGE=' + pinned('python:3.12-slim-bookworm')]
        command.append(str(context))
        run(command)
        actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]
        result[name] = {'id': actual['Id'], 'tag': tag}
        record = image_directory / (name + '.' + str(os.getpid()) + '.tmp')
        record.write_text(json.dumps(result[name]))
        record.replace(image_directory / (name + '.json'))
        result.update({path.stem: json.loads(path.read_text()) for path in image_directory.glob('*.json')})
        snapshot = context / ('built-images.' + str(os.getpid()) + '.tmp')
        snapshot.write_text(json.dumps(result, indent=2))
        snapshot.replace(image_file)
        print('IMAGE_BUILT', name, actual['Id'], flush=True)


if __name__ == '__main__':
    main()
