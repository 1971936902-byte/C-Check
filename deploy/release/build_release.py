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
from urllib.parse import quote, urlsplit
from urllib.request import urlopen
import zipfile

from build_images import pinned
from prepare_build import sha
sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from ccheck.image_identity import identity
from ccheck.core import checked_path, verify_release


def validate_assistant_gates(previous, images, gates):
    if not previous or previous['version'] not in {'v0.1.0-rc.19', 'v0.1.0-rc.20'}:
        raise RuntimeError('assistant release requires a verified Qwen2.5 rc19/rc20 baseline')
    if 'anythingllm' not in images:
        raise RuntimeError('assistant provider image is missing')
    evidence = gates.get('assistant', {})
    for name in ('passed', 'real_gpu', 'review_preemption', 'rag', 'owner_isolation',
                 'recovery', 'source_native_parity'):
        if evidence.get(name) is not True:
            raise RuntimeError('missing assistant acceptance gate: ' + name)


def bind_assistant_metadata(context, previous, release):
    from ccheck import assistant
    from ccheck.schema_migration import validate
    resources = {name: sha(context / 'deploy/assistant' / name) for name in assistant.RESOURCES}
    if resources != assistant.hashes():
        raise RuntimeError('assistant source and publisher runtime differ')
    release['assistant_runtime'] = resources
    release['schema_migration'] = {
        'kind': 'additive-assistant-state', 'from_release_sha256': previous['_sha256'],
        'from_epoch': previous['schema_epoch'], 'to_epoch': release['schema_epoch'],
        'migration_sha256': assistant.migration_hashes(), 'backup_required': True}
    validate(previous, release)


def bind_runtime_launchers(context, output, release):
    from ccheck import acceleration
    names = ('qwen_gpu.py', 'embedding_gpu.py')
    with zipfile.ZipFile(output / 'cli/ccheck.pyz') as archive:
        packaged = {name: hashlib.sha256(archive.read('ccheck/launchers/' + name)).hexdigest()
                    for name in names}
    source = {name: sha(context / 'deploy/release/cli/ccheck/launchers' / name) for name in names}
    runtime = {name: hashlib.sha256(acceleration.resource(name)).hexdigest() for name in names}
    if packaged != source or packaged != runtime:
        raise RuntimeError('packaged runtime launchers differ from the frozen publisher source')
    release['runtime_launchers'] = packaged
    acceleration.validate_release({'profile': 'ada-48g-fp8', 'performance': {}}, release)


def assistant_notices(context, image, output):
    lock = json.loads((context / 'deploy/assistant/anythingllm.lock.json').read_text())
    license_text = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'cat', image, '/usr/share/licenses/anythingllm/LICENSE'], timeout=30)
    if lock['license'] != 'MIT' or hashlib.sha256(license_text).hexdigest() != lock['license_sha256']:
        raise RuntimeError('provider image license does not match its lock')
    (output / 'licenses/AnythingLLM-MIT.txt').write_bytes(license_text)
    shutil.copy2(context / 'deploy/assistant/anythingllm.lock.json', output / 'licenses/anythingllm.lock.json')
    shutil.copy2(context / 'deploy/assistant/README.md', output / 'ASSISTANT.md')


def retain_assistant_baseline_interfaces(previous, output):
    review_cli = previous.get('review_cli')
    if review_cli is None or {key: review_cli.get(key) for key in ('file', 'wrapper', 'guide')} != {
            'file': 'cli/ccheck-review.pyz', 'wrapper': 'ccheck-review', 'guide': 'CLI-REVIEW.md'}:
        raise RuntimeError('assistant baseline must contain the approved review CLI interface')
    names = {'cli/ccheck-review.pyz', 'ccheck-review', 'CLI-REVIEW.md'}
    names.update(name for name in previous['files'] if name.startswith('terminal/'))
    for name in sorted(names):
        if name not in previous['files']:
            raise RuntimeError('baseline interface file is missing: ' + name)
        source = checked_path(previous['_root'], name)
        target = checked_path(output, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha(target) != previous['files'][name]:
            raise RuntimeError('baseline interface changed during assembly: ' + name)
    (output / 'ccheck-review').chmod(0o755)
    return dict(review_cli)


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


def reusable_wheel_provenance(previous, records):
    recipe = previous.get('runtime_recipe') if previous else None
    if not recipe:
        return None
    name = recipe['directory'] + '/wheels.json'
    if name not in previous['files']:
        return None
    path = checked_path(previous['_root'], name)
    if sha(path) != previous['files'][name]:
        raise RuntimeError('signed runtime wheel provenance changed')
    old = json.loads(path.read_text())
    if not isinstance(old, list) or any(not isinstance(row, dict) or 'file' not in row for row in old):
        raise RuntimeError('invalid signed runtime wheel provenance')
    by_file = {row['file']: row for row in old}
    if len(by_file) != len(old):
        raise RuntimeError('duplicate signed runtime wheel provenance')
    if len({row['file'] for row in records}) != len(records):
        raise RuntimeError('duplicate current runtime wheel inventory')
    if set(by_file) != {row['file'] for row in records}:
        return None
    result = []
    for row in records:
        saved = by_file[row['file']]
        if ({key: value for key, value in saved.items() if key != 'url'} !=
                {key: value for key, value in row.items() if key != 'url'}):
            return None
        address = urlsplit(saved.get('url', ''))
        if (address.scheme != 'https' or not address.netloc or address.username or address.password
                or address.query or address.fragment):
            raise RuntimeError('insecure signed runtime wheel URL')
        if row.get('url') is not None and row['url'] != saved['url']:
            return None
        result.append(dict(row, url=saved['url']))
    return result


def runtime_recipe(context, output, qwen_image, *, previous=None):
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
    cached = reusable_wheel_provenance(previous, records)
    if cached is None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(url, records))
    else:
        records = cached
        print('VERIFIED_RUNTIME_WHEEL_PROVENANCE_REUSED', flush=True)
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


def bind_patch_distribution(previous, release):
    from ccheck.fetch import asset_prefix
    if not previous:
        raise RuntimeError('deployment patch requires a verified baseline')
    reused = []
    for name in release.get('artifact_reuse', {}).get('images', []):
        for part in release['images'][name].get('archive_parts', []):
            if (not part.startswith('images/') or previous['files'].get(part) != release['files'].get(part)
                    or previous.get('artifact_sizes', {}).get(part) != release['artifact_sizes'].get(part)):
                raise RuntimeError('reused patch archive differs from the verified baseline: ' + part)
            reused.append(part)
    baseline = previous.get('distribution') or {}
    release['distribution'] = {'kind': 'deployment-patch',
        'base_version': baseline.get('base_version', previous['version']),
        'base_release_sha256': baseline.get('base_release_sha256', previous['_sha256']),
        'upgrade_from_version': previous['version'], 'upgrade_from_sha256': previous['_sha256'],
        'reused_files': sorted(reused), 'asset_versions': {
            name: asset_prefix(previous, name, 'https://example.invalid').rstrip('/').rsplit('/release-', 1)[1]
            for name in reused}}


def patch_bundle(output, release):
    bundle = output.parent / ('release-' + release['version'] + '-patch.tar.gz')
    reused = set(release['distribution']['reused_files'])
    with bundle.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in sorted((set(release['files']) - reused) | {'release.json', 'release.sig'}):
            archive.add(output / name, arcname=output.name + '/' + name, recursive=False)
    bundle.with_name(bundle.name + '.sha256').write_text(sha(bundle) + '  ' + bundle.name + '\n')
    return bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--gates', required=True, type=Path)
    parser.add_argument('--signing-key', required=True, type=Path)
    parser.add_argument('--yaml-package', required=True, type=Path)
    parser.add_argument('--allow-worktree-snapshot', action='store_true')
    parser.add_argument('--with-assistant', action='store_true',
                        help='include assistant runtime and approved additive schema transition')
    parser.add_argument('--with-terminal', action='store_true',
                        help='include the signed independently managed SSH terminal runtime')
    parser.add_argument('--deployment-patch', action='store_true',
                        help='publish a patch bundle excluding verified unchanged inference/service archives')
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
    if args.deployment_patch and not previous:
        raise RuntimeError('deployment patch requires --reuse-release and --reuse-trusted-key')
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
    if args.with_assistant:
        validate_assistant_gates(previous, images, gates)
        if source.get('worktree_snapshot'):
            raise RuntimeError('assistant publication requires committed source, not a development snapshot')
    elif 'anythingllm' in images:
        raise RuntimeError('provider image requires explicit --with-assistant')
    if args.with_terminal:
        from ccheck import remote_terminal
        evidence = gates.get('terminal', {})
        for name in ('passed', 'real_ssh', 'tls', 'restart_preservation', 'configuration', 'isolation'):
            if evidence.get(name) is not True:
                raise RuntimeError('missing terminal acceptance gate: ' + name)
        if source.get('worktree_snapshot'):
            raise RuntimeError('terminal publication requires committed source')
        terminal_resources = {name: sha(context / 'deploy/terminal' / name) for name in remote_terminal.RESOURCES}
        if terminal_resources != remote_terminal.hashes():
            raise RuntimeError('terminal source and publisher runtime differ')
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
    review_cli = retain_assistant_baseline_interfaces(previous, output) if args.with_assistant else None
    subprocess.run([sys.executable, str(context / 'deploy/release/build_cli.py'), '--output', str(output / 'cli/ccheck.pyz'),
                    '--yaml-package', str(args.yaml_package)] +
                   (['--with-assistant'] if args.with_assistant else []) +
                   (['--with-terminal'] if args.with_terminal else []), check=True)
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output / 'publisher.pem')], check=True)
    if args.with_assistant and sha(output / 'publisher.pem') != previous['_publisher']:
        raise RuntimeError('assistant upgrade must retain the installed publisher identity')
    guide_name = 'INSTALL-ASSISTANT-PATCH.template.md' if args.with_assistant else 'INSTALL-RTX5880.template.md'
    template = (context / 'deploy/release' / guide_name).read_text(encoding='utf-8')
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
    if args.with_assistant:
        assistant_notices(context, images['anythingllm'], output)
    if args.with_terminal:
        shutil.copy2(context / 'deploy/terminal/MANAGED.md', output / 'REMOTE-SSH.md')
        (output / 'terminal').mkdir(exist_ok=True)
        shutil.copy2(context / 'deploy/terminal/MANAGED.md', output / 'terminal/README.md')
        for name in remote_terminal.RESOURCES:
            target = output / 'terminal' / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(context / 'deploy/terminal' / name, target)
    old_recipe = previous.get('runtime_recipe') if previous else None
    old_prefix = checked_path(previous['_root'], old_recipe['directory']) if old_recipe else None
    if ('qwen' in reused and old_recipe and old_recipe['base_image'] == pinned('python:3.12-slim-bookworm')
            and sha(old_prefix / 'requirements.lock') == sha(context / 'locks/qwen.lock')
            and sha(old_prefix / 'qwen_entry.py') == sha(context / 'deploy/release/runtime/qwen_entry.py')):
        shutil.copytree(old_prefix, output / 'runtime/qwen')
        recipe = {'directory': 'runtime/qwen', 'base_image': old_recipe['base_image']}
        print('VERIFIED_RUNTIME_RECIPE_REUSED', flush=True)
    else:
        recipe = runtime_recipe(context, output, images['qwen'], previous=previous)
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
    if args.with_assistant:
        with (output / 'README.md').open('a') as guide:
            guide.write('\nAI assistant is opt-in. Read ASSISTANT.md before enabling it; '
                        'the additive database migration requires an encrypted backup.\n')
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
    if args.with_assistant:
        bind_assistant_metadata(context, previous, release)
        release['review_cli'] = review_cli
    if args.with_terminal:
        release['terminal_runtime'] = terminal_resources
    bind_runtime_launchers(context, output, release)
    if args.deployment_patch:
        bind_patch_distribution(previous, release)
    (output / 'release.json').write_text(json.dumps(release, indent=2) + '\n')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin',
                    '-in', str(output / 'release.json'), '-out', str(output / 'release.sig')], check=True)
    verified = verify_release(output, output / 'publisher.pem', sha(output / 'publisher.pem'))
    if args.deployment_patch:
        bundle = patch_bundle(output, verified)
        print('SIGNED_PATCH', bundle, bundle.stat().st_size, flush=True)
    print('SIGNED_RELEASE', output, 'publisher public key SHA256:', sha(output / 'publisher.pem'))


if __name__ == '__main__':
    import sys
    main()
