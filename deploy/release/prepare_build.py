"""Publisher-side dependency preparation. Run on the validated Linux/amd64 builder."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote


def run(command):
    subprocess.run(command, check=True)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def environment(context, name, python, cpu=False):
    wheels = context / 'wheelhouse' / name
    wheels.mkdir(parents=True, exist_ok=True)
    frozen = subprocess.check_output([python, '-m', 'pip', 'freeze'], text=True)
    requirements = [line for line in frozen.splitlines() if re.fullmatch(r'[A-Za-z0-9_.-]+==[^\s]+', line)]
    requirements = [line for line in requirements if not line.lower().startswith('c-check-backend==')]
    requirement_file = context / 'locks' / (name + '.resolved.txt')
    requirement_file.write_text('\n'.join(sorted(requirements)) + '\n')
    command = [python, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps',
               '--dest', str(wheels), '-r', str(requirement_file)]
    if cpu:
        command += ['--extra-index-url', 'https://download.pytorch.org/whl/cpu']
    run(command)
    make_lock(context, name)


def make_lock(context, name):
    from packaging.utils import parse_wheel_filename
    wheels = context / 'wheelhouse' / name
    lines, records = [], []
    for path in sorted(wheels.glob('*.whl')):
        distribution, version, _, _ = parse_wheel_filename(path.name)
        checksum = sha(path)
        lines.append(f'{distribution}=={version} --hash=sha256:{checksum}')
        records.append({'file': path.name, 'distribution': distribution, 'version': str(version),
                        'sha256': checksum, 'size': path.stat().st_size})
    if not lines:
        raise RuntimeError('No wheels prepared: ' + name)
    (context / 'locks' / (name + '.lock')).write_text('\n'.join(lines) + '\n')
    (context / 'locks' / (name + '.wheels.json')).write_text(json.dumps(records, indent=2))


def compiler(context, python):
    wheels = context / 'wheelhouse/compiler'
    wheels.mkdir(parents=True, exist_ok=True)
    run([python, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(wheels),
         'Nuitka==2.7.12', 'ordered-set==4.1.0', 'zstandard==0.24.0'])
    make_lock(context, 'compiler')


def model_manifest(context, model_root):
    models = []
    output = context / 'release-input/models'
    output.mkdir(parents=True, exist_ok=True)
    for key, cache, repository, license_name in [
        ('qwen', 'qwen32b-fp8', 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8', 'qwen: see supplied LICENSE'),
        ('jina', 'jina-code-embeddings-1.5b', 'jinaai/jina-code-embeddings-1.5b', 'CC-BY-NC-4.0; company use requires separate review')]:
        source = model_root / cache
        manifest = source / 'download-manifest.json'
        raw = json.loads(manifest.read_text())
        files = []
        for row in raw['Data']['Files']:
            path = row['Path']
            if row['Type'] != 'blob' or path.startswith('.') or not (path.endswith(('.json', '.txt', '.safetensors', '.model', '.md')) or path.upper().startswith('LICENSE')):
                continue
            if Path(path).is_absolute() or '..' in Path(path).parts:
                raise RuntimeError('Unsafe model source path')
            if not re.fullmatch(r'[0-9a-f]{64}', row['Sha256']) or not re.fullmatch(r'[0-9a-f]{40}', row['Revision']):
                raise RuntimeError('Model source must provide immutable per-file revision/hash')
            url = 'https://modelscope.cn/api/v1/models/' + repository + '/repo?FilePath=' + quote(path, safe='') + '&Revision=' + row['Revision']
            files.append({'path': path, 'size': row['Size'], 'sha256': row['Sha256'], 'revision': row['Revision'], 'url': url})
        for row in files:
            path = source / row['path']
            if not path.is_file() or path.stat().st_size != row['size'] or sha(path) != row['sha256']:
                raise RuntimeError('Existing model cache failed verification: ' + row['path'])
        models.append({'key': key, 'cache_name': cache, 'repository': repository,
                       'revision': 'file-manifest-sha256:' + sha(manifest), 'license': license_name, 'files': files})
        (output / (key + '-MODEL-NOTICE.md')).write_text((source / 'README.md').read_text())
        for license_file in source.glob('LICENSE*'):
            (output / (key + '-' + license_file.name)).write_bytes(license_file.read_bytes())
    (output / 'model-manifest.json').write_text(json.dumps({'format': 1, 'models': models}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--model-root', type=Path, default=Path('/opt/models'))
    parser.add_argument('--backend-python', default='/opt/c-check/.venv/bin/python')
    parser.add_argument('--qwen-python', default='/opt/vllm-qwen/bin/python')
    parser.add_argument('--embedding-python', default='/opt/jina-embedding/bin/python')
    parser.add_argument('--compiler-python', help='defaults to --backend-python')
    args = parser.parse_args()
    context = args.context.resolve()
    (context / 'locks').mkdir(exist_ok=True)
    jobs = [('backend', args.backend_python, False),
            ('qwen', args.qwen_python, False),
            ('embedding', args.embedding_python, True)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(environment, context, *job) for job in jobs]
        futures.append(pool.submit(compiler, context, args.compiler_python or args.backend_python))
        for future in futures:
            future.result()
    model_manifest(context, args.model_root)
    print('BUILD_DEPENDENCIES_READY', flush=True)


if __name__ == '__main__':
    main()
