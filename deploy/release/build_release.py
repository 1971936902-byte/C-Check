"""Assemble a signed release from verified final images, never from a host environment export."""
import argparse
import base64
import concurrent.futures
import csv
import email
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

from build_images import pinned
from prepare_build import sha
sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from ccheck.image_identity import identity
from ccheck.core import checked_path, verify_release


def render_install_guide(template, version, fingerprint):
    markers = set(re.findall(r'@@([A-Z0-9_]+)@@', template))
    if markers != {'VERSION', 'PUBLISHER_PEM_SHA256'}:
        raise RuntimeError('installation guide template must contain exactly the approved placeholders')
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', version) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise RuntimeError('invalid guide version or publisher fingerprint')
    return template.replace('@@VERSION@@', version).replace('@@PUBLISHER_PEM_SHA256@@', fingerprint)


class SplitWriter:
    def __init__(self, directory, name, limit=1536 * 1024 * 1024):
        self.directory, self.name, self.limit = directory, name, limit
        self.paths, self.stream, self.size = [], None, 0

    def write(self, data):
        total = len(data)
        while data:
            if self.stream is None or self.size >= self.limit:
                if self.stream:
                    self.stream.close()
                path = self.directory / (self.name + '.tar.gz.part' + str(len(self.paths)).zfill(4))
                self.paths.append(path)
                self.stream, self.size = path.open('wb'), 0
            count = min(len(data), self.limit - self.size)
            self.stream.write(data[:count])
            self.size += count
            data = data[count:]
        return total

    def flush(self):
        if self.stream:
            self.stream.flush()

    def close(self):
        if self.stream:
            self.stream.close()


def export_image(image, directory, name):
    writer = SplitWriter(directory, name)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['docker', 'image', 'save', image], stdout=subprocess.PIPE, stderr=log)
        try:
            with gzip.GzipFile(fileobj=writer, mode='wb', compresslevel=1, mtime=0) as compressed:
                shutil.copyfileobj(process.stdout, compressed, length=4 * 1024 * 1024)
            if process.wait(timeout=3600):
                raise RuntimeError('image export failed: ' + name)
        finally:
            writer.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    return [str(Path('images') / path.name) for path in writer.paths]


def image_archives(image, metadata, output, name, previous, reused):
    old = previous.get('images', {}).get(name, {}) if previous else {}
    parts = old.get('archive_parts')
    if old.get('filesystem_config_sha256') != identity(metadata) or not parts:
        return export_image(image, output / 'images', name)
    for relative in parts:
        source = checked_path(previous['_root'], relative)
        target = checked_path(output, relative)
        if not relative.startswith('images/') or target.exists():
            raise RuntimeError('invalid/conflicting reusable image asset')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha(target) != previous['files'][relative]:
            raise RuntimeError('reused image asset changed during copy')
    reused.append(name)
    print('VERIFIED_IMAGE_ASSETS_REUSED', name, flush=True)
    return parts


def license_inventory(context, output):
    records = []
    destination = output / 'licenses/wheels'
    destination.mkdir(parents=True)
    for group in ('backend', 'qwen', 'embedding', 'compiler'):
        for wheel in sorted((context / 'wheelhouse' / group).glob('*.whl')):
            with zipfile.ZipFile(wheel) as archive:
                metadata_name = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
                metadata = email.message_from_bytes(archive.read(metadata_name))
                distribution, version = metadata['Name'], metadata['Version']
                name = re.sub(r'[^A-Za-z0-9_.-]', '_', distribution + '-' + version)
                record = {'environment': group, 'name': distribution, 'version': version,
                          'license_expression': metadata.get('License-Expression'), 'license': metadata.get('License'),
                          'wheel_sha256': sha(wheel), 'license_files': []}
                for path in archive.namelist():
                    filename = Path(path).name
                    if not filename.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')) or path.endswith('/'):
                        continue
                    target = destination / group / name / (hashlib.sha256(path.encode()).hexdigest()[:8] + '-' + filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(path))
                    record['license_files'].append(target.relative_to(output).as_posix())
                records.append(record)
    (output / 'licenses/python-components.json').write_text(json.dumps(records, indent=2))
    (output / 'licenses/THIRD_PARTY_NOTICES.md').write_text(
        '# Third-party notices\n\nSee python-components.json and accompanying license files. '
        'Model notices are in models/. GPU dependencies include NVIDIA CUDA runtime components and their terms. '
        'Service images retain their upstream notices. Redis 7.4 must not be treated as legacy BSD Redis. '
        'Redis, MySQL, Qdrant, Nginx and model redistribution/commercial rights require operator review. '
        'Compiled first-party modules remain proprietary unless separately licensed.\n')


def runtime_recipe(context, output, qwen_image):
    directory = output / 'runtime/qwen'
    directory.mkdir(parents=True)
    shutil.copy2(context / 'locks/qwen.lock', directory / 'requirements.lock')
    shutil.copy2(context / 'deploy/release/runtime/qwen_entry.py', directory / 'qwen_entry.py')
    records = json.loads((context / 'locks/qwen.wheels.json').read_text())
    def url(record):
        address = 'https://pypi.org/pypi/' + quote(record['distribution']) + '/' + quote(record['version']) + '/json'
        with urlopen(address, timeout=60) as response:
            metadata = json.load(response)
        match = [item for item in metadata['urls'] if item['filename'] == record['file'] and item['digests']['sha256'] == record['sha256']]
        if len(match) != 1:
            raise RuntimeError('wheel provenance cannot be pinned: ' + record['file'])
        return dict(record, url=match[0]['url'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(url, records))
    (directory / 'wheels.json').write_text(json.dumps(records, indent=2))
    packages = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'dpkg-query', qwen_image,
                                        '-W', '-f=${Package}=${Version}\n'], text=True).splitlines()
    if not all(re.fullmatch(r'[a-z0-9+.:_-]+=[A-Za-z0-9.+:~_-]+', item) for item in packages):
        raise RuntimeError('unexpected system package inventory')
    (directory / 'system-packages.lock').write_text('\n'.join(packages) + '\n')
    (directory / 'Dockerfile').write_text(
        'ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n'
        "RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources "
        '&& apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=3 update '
        '&& apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=3 install -y --no-install-recommends ' + ' '.join(packages) +
        ' && rm -rf /var/lib/apt/lists/*\n'
        'RUN mkdir -p /opt/cuda-link && ln -s /usr/lib/x86_64-linux-gnu/libcuda.so.1 /opt/cuda-link/libcuda.so\n'
        'COPY requirements.lock /tmp/requirements.lock\n'
        'RUN --mount=type=bind,source=wheels,target=/wheels pip install --no-index --find-links=/wheels --require-hashes -r /tmp/requirements.lock && pip check\n'
        'COPY qwen_entry.py /opt/runtime/qwen_entry.py\n'
        'RUN groupadd -g 10001 ccheck && useradd -u 10001 -g 10001 -M ccheck\n'
        'ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LIBRARY_PATH=/opt/cuda-link USER=ccheck LOGNAME=ccheck HOME=/cache TORCHINDUCTOR_CACHE_DIR=/cache/torchinductor\n'
        'USER 10001:10001\nENTRYPOINT ["python","/opt/runtime/qwen_entry.py"]\n')
    return {'directory': 'runtime/qwen', 'base_image': pinned('python:3.12-slim-bookworm')}


def core_wheel(context, image, output, version):
    policy = json.loads((context / 'deploy/release/core-policy.json').read_text())
    python_version = version.lstrip('v').replace('-rc.', 'rc')
    distribution = 'c_check_core-' + python_version + '.dist-info'
    target = output / ('c_check_core-' + python_version + '-cp312-cp312-linux_x86_64.whl')
    records, binaries = [], []
    container = subprocess.check_output(['docker', 'create', image], text=True).strip()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            subprocess.run(['docker', 'cp', container + ':/app/backend/app', str(tree / 'app')], check=True)
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
                def add(name, content):
                    archive.writestr(name, content)
                    checksum = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
                    records.append((name, 'sha256=' + checksum, len(content)))
                for module in policy['compiled_modules']:
                    relative = Path(module.replace('.', '/'))
                    matches = list((tree / relative.parent).glob(relative.name + '.*.so'))
                    if len(matches) != 1:
                        raise RuntimeError('native wheel module missing: ' + module)
                    content = matches[0].read_bytes()
                    name = matches[0].relative_to(tree).as_posix()
                    add(name, content)
                    binaries.append({'module': module, 'file': name, 'sha256': hashlib.sha256(content).hexdigest()})
                add(distribution + '/METADATA', ('Metadata-Version: 2.1\nName: c-check-core\nVersion: ' + python_version + '\nLicense: Proprietary\nRequires-Python: >=3.12,<3.13\n').encode())
                add(distribution + '/WHEEL', b'Wheel-Version: 1.0\nGenerator: c-check-release\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n')
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator='\n')
                writer.writerows(records + [(distribution + '/RECORD', '', '')])
                archive.writestr(distribution + '/RECORD', buffer.getvalue())
    finally:
        subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
    return {'file': target.name, 'sha256': sha(target), 'modules': binaries, 'abi': 'cp312-linux-x86_64'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--gates', required=True, type=Path)
    parser.add_argument('--signing-key', required=True, type=Path)
    parser.add_argument('--yaml-package', required=True, type=Path)
    parser.add_argument('--allow-worktree-snapshot', action='store_true')
    parser.add_argument('--reuse-release', type=Path, help='reuse identical runtime archives from a verified prior release')
    parser.add_argument('--reuse-trusted-key', type=Path)
    args = parser.parse_args()
    context, output, key = args.context.resolve(), args.output.resolve(), args.signing_key.resolve()
    if key.is_relative_to(context) or key.is_relative_to(output):
        raise RuntimeError('signing private key must be outside build context and release')
    if key.stat().st_mode & 0o077:
        raise RuntimeError('signing key must be owner-only')
    if bool(args.reuse_release) != bool(args.reuse_trusted_key):
        raise RuntimeError('reusing artifacts requires both a prior release and an independently trusted public key')
    previous = verify_release(args.reuse_release, args.reuse_trusted_key) if args.reuse_release else None
    if output.exists():
        raise RuntimeError('release output must be new; published versions are immutable')
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', args.version):
        raise RuntimeError('use a semantic release or rc version')
    source = json.loads((context / 'build-source.json').read_text())
    if source.get('worktree_snapshot') and (not args.allow_worktree_snapshot or '-rc.' not in args.version):
        raise RuntimeError('worktree snapshots require explicit opt-in and an rc version')
    built = json.loads((context / 'built-images.json').read_text())
    images = {name: row['id'] for name, row in built.items()}
    gates = json.loads(args.gates.read_text())
    if not gates.get('passed') or gates['images'] != images or gates['source_tree'] != source['tree_sha256'] or gates['version'] != args.version:
        raise RuntimeError('passing gates must match the exact images, source snapshot and version')
    for path, checksum in source['files'].items():
        if sha(context / path) != checksum:
            raise RuntimeError('source changed after snapshot/gates: ' + path)
    output.mkdir(mode=0o700, parents=True)
    for name in ('images', 'models', 'locks', 'cli', 'verify', 'licenses'):
        (output / name).mkdir()
    shutil.copytree(context / 'release-input/models', output / 'models', dirs_exist_ok=True)
    shutil.copytree(context / 'deploy/release/verify', output / 'verify', dirs_exist_ok=True)
    shutil.copytree(context / 'locks', output / 'locks', dirs_exist_ok=True)
    shutil.copy2(context / 'deploy/release/core-policy.json', output / 'core-policy.json')
    shutil.copy2(args.gates, output / 'verify/build-gates.json')
    for name in ('ccheck', 'install.sh'):
        shutil.copy2(context / 'deploy/release' / name, output / name)
        (output / name).chmod(0o755)
    for name in ('site-rtx5880.example.json',):
        shutil.copy2(context / 'deploy/release' / name, output / name)
    shutil.copy2(output / 'ccheck', output / 'ccheckctl')
    subprocess.run([sys.executable, str(context / 'deploy/release/build_cli.py'), '--output', str(output / 'cli/ccheck.pyz'),
                    '--yaml-package', str(args.yaml_package)], check=True)
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output / 'publisher.pem')], check=True)
    template = (context / 'deploy/release/INSTALL-RTX5880.template.md').read_text(encoding='utf-8')
    (output / 'INSTALL-RTX5880.md').write_text(render_install_guide(template, args.version,
                                                                 sha(output / 'publisher.pem')), encoding='utf-8')
    specifications, reused = {}, []
    for name, image in images.items():
        metadata = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
        specifications[name] = {'id': image, 'size_bytes': metadata['Size'], 'filesystem_config_sha256': identity(metadata),
                                'archive_parts': image_archives(image, metadata, output, name, previous, reused)}
    for name, tag in [('mysql', 'mysql:8.0'), ('redis', 'redis:7.4-alpine'), ('qdrant', 'qdrant/qdrant:v1.15.5')]:
        metadata = json.loads(subprocess.check_output(['docker', 'image', 'inspect', pinned(tag)]))[0]
        image = metadata['Id']
        specifications[name] = {'id': image, 'size_bytes': metadata['Size'], 'filesystem_config_sha256': identity(metadata),
                                'reference': metadata['RepoDigests'][0],
                                'archive_parts': image_archives(image, metadata, output, name, previous, reused)}
    license_inventory(context, output)
    old_recipe = previous.get('runtime_recipe') if previous else None
    old_prefix = checked_path(previous['_root'], old_recipe['directory']) if old_recipe else None
    if ('qwen' in reused and old_recipe and old_recipe['base_image'] == pinned('python:3.12-slim-bookworm')
            and sha(old_prefix / 'requirements.lock') == sha(context / 'locks/qwen.lock')
            and sha(old_prefix / 'qwen_entry.py') == sha(context / 'deploy/release/runtime/qwen_entry.py')):
        shutil.copytree(old_prefix, output / 'runtime/qwen')
        recipe = {'directory': 'runtime/qwen', 'base_image': old_recipe['base_image']}
        print('VERIFIED_RUNTIME_RECIPE_REUSED', flush=True)
    else:
        recipe = runtime_recipe(context, output, images['qwen'])
    wheel = core_wheel(context, images['backend'], output, args.version)
    schema = {path.name: sha(path) for path in sorted((context / 'backend/alembic/versions').glob('*.py'))}
    schema_epoch = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    (output / 'README.md').write_text(
        '# C-Check ' + args.version + '\n\nSigned Linux/amd64 evaluation release. '
        'Require Python 3.10+, OpenSSL, Docker Compose and NVIDIA GPU container support. '
        'Verify publisher.pem against an independently obtained fingerprint before installation. '
        'Run ./ccheck install --config site.yaml --trusted-key /trusted/publisher.pem --version ' + args.version + '. '
        'Do not infer model commercial rights from installation success; read models/ notices. '
        'Only final runtime images are distributed, not private source or trusted compiler stages.\n')
    bootstrap_path = output / 'bootstrap.tar.gz'
    small_files = [path for path in sorted(output.rglob('*')) if path.is_file() and not path.is_relative_to(output / 'images')]
    with tarfile.open(bootstrap_path, 'w:gz') as archive:
        for path in small_files:
            archive.add(path, arcname=path.relative_to(output).as_posix(), recursive=False)
    files = {path.relative_to(output).as_posix(): sha(path) for path in sorted(output.rglob('*')) if path.is_file()}
    (output / 'checksums.sha256').write_text(''.join(checksum + '  ' + name + '\n' for name, checksum in sorted(files.items())))
    files['checksums.sha256'] = sha(output / 'checksums.sha256')
    release = {'format': 1, 'version': args.version, 'tag': 'release-' + args.version,
               'platform': 'linux-amd64', 'source': {'git_sha': source['git_sha'], 'tree_sha256': source['tree_sha256'],
               'worktree_snapshot': source.get('worktree_snapshot', False)}, 'images': specifications,
               'schema_epoch': schema_epoch, 'schema_files': schema, 'core_wheel': wheel,
               'artifact_reuse': {'release_sha256': previous['_sha256'], 'images': reused} if previous else None,
               'runtime_recipe': recipe, 'files': files,
               'bootstrap': {'file': 'bootstrap.tar.gz', 'sha256': sha(bootstrap_path), 'size': bootstrap_path.stat().st_size},
               'artifact_sizes': {name: (output / name).stat().st_size for name in files},
               'profiles': {'a100-40g-fp8': 'target hardware under validation', 'ada-48g-fp8': 'requires hardware acceptance'}}
    (output / 'release.json').write_text(json.dumps(release, indent=2) + '\n')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin',
                    '-in', str(output / 'release.json'), '-out', str(output / 'release.sig')], check=True)
    print('SIGNED_RELEASE', output, 'publisher public key SHA256:', sha(output / 'publisher.pem'))


if __name__ == '__main__':
    import sys
    main()
