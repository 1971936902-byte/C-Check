"""Exercise real signing/assembly with a disposable key, never the publisher key."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def execute(command, **kwargs):
    return subprocess.run(command, check=True, timeout=900, **kwargs)


def extract(source, destination):
    destination.mkdir()
    with tarfile.open(source) as archive:
        if any(not member.isfile() or Path(member.name).is_absolute() or
               '..' in Path(member.name).parts for member in archive.getmembers()):
            raise RuntimeError('unsafe source snapshot')
        archive.extractall(destination)


def runtime_files(files, resources):
    return {name: value for name, value in files.items()
        if name.startswith(('backend/', 'frontend/', 'deploy/release/cli/'))
        or name in {'deploy/release/core-policy.json', 'deploy/release/build_cli.py'}
        or name in {'deploy/assistant/' + resource for resource in resources}}


def fixture_base(original, destination, key, verify_release, digest, bootstrap):
    destination.mkdir()
    for name in original['files']:
        if name in {'bootstrap.tar.gz', 'checksums.sha256'}:
            continue
        source, target = Path(original['_root']) / name, destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith('images/'):
            os.link(source, target)
        else:
            shutil.copy2(source, target)
    execute(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(destination / 'publisher.pem')],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (destination / 'README.md').write_text('# Disposable signing fixture\nNot a published C-Check release.\n')
    bootstrap(destination)
    files = {path.relative_to(destination).as_posix(): digest(path)
             for path in destination.rglob('*') if path.is_file()}
    (destination / 'checksums.sha256').write_text(''.join(value + '  ' + name + '\n'
                                                        for name, value in sorted(files.items())))
    files['checksums.sha256'] = digest(destination / 'checksums.sha256')
    manifest = {name: value for name, value in original.items() if not name.startswith('_')}
    manifest.update(files=files, artifact_sizes={name: (destination / name).stat().st_size for name in files},
        bootstrap={'file': 'bootstrap.tar.gz', 'sha256': files['bootstrap.tar.gz'],
                   'size': (destination / 'bootstrap.tar.gz').stat().st_size})
    (destination / 'release.json').write_text(json.dumps(manifest, indent=2))
    execute(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in',
             str(destination / 'release.json'), '-out', str(destination / 'release.sig')])
    return verify_release(destination, destination / 'publisher.pem', digest(destination / 'publisher.pem'))


def prepare_inputs(context, base, wheel_cache, digest):
    shutil.copytree(Path(base['_root']) / 'models', context / 'release-input/models')
    shutil.copytree(Path(base['_root']) / 'locks', context / 'locks')
    for group in ('backend', 'qwen', 'embedding', 'compiler'):
        catalog = json.loads((context / 'locks' / (group + '.wheels.json')).read_text())
        destination = context / 'wheelhouse' / group
        destination.mkdir(parents=True)
        for row in catalog:
            source = wheel_cache / group / row['file']
            if digest(source) != row['sha256']:
                raise RuntimeError('cached wheel differs from the signed dependency lock')
            os.link(source, destination / source.name)
    yaml = context.parent / 'yaml-package'
    yaml.mkdir()
    with zipfile.ZipFile(Path(base['_root']) / 'cli/ccheck.pyz') as archive:
        for name in archive.namelist():
            if name.startswith('yaml/') and name.endswith('.py') and '..' not in Path(name).parts:
                target = yaml / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    if not (yaml / 'yaml/__init__.py').is_file():
        raise RuntimeError('verified baseline installer has no pure Python YAML package')
    return yaml / 'yaml'


def snapshot_roundtrip(root, backend_image):
    from ccheck.backup import helper, key_material
    from ccheck.engine import Engine
    bundle = root / 'snapshot-instance'
    for name in ('data/mysql', 'data/redis', 'data/qdrant', 'data/uploads', 'secrets', 'data/assistant/storage'):
        path = bundle / name
        path.mkdir(parents=True, mode=0o700)
        (path / 'sentinel').write_text(name)
        (path / 'sentinel').chmod(0o600)
    site = {'instance': 'assistant-snapshot-probe', 'data_dir': str(bundle)}
    engine = Engine(site)
    state = {'images': {'backend': backend_image}, 'uid': 10001, 'gid': 10001}
    key = root / 'backup.key'
    key.write_text(os.urandom(48).hex() + '\n')
    key.chmod(0o600)
    key_material(key)
    plain = subprocess.check_output(helper(engine, state, 'create'), timeout=60)
    cipher = subprocess.check_output(['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter',
        '200000', '-pass', 'file:' + str(key)], input=plain, timeout=60)
    assert plain not in cipher
    (bundle / 'data/assistant/storage/sentinel').write_text('modified')
    (bundle / 'data/assistant/orphan').write_text('must disappear')
    decoded = subprocess.check_output(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter',
        '200000', '-pass', 'file:' + str(key)], input=cipher, timeout=60)
    execute(helper(engine, state, 'restore'), input=decoded, stdout=subprocess.DEVNULL)
    assert not (bundle / 'data/assistant/orphan').exists()
    for name in ('data/uploads', 'data/assistant/storage', 'secrets'):
        path = bundle / name / 'sentinel'
        assert path.read_text() == name and path.stat().st_uid == 10001
        assert path.stat().st_mode & 0o077 == 0
    return {'passed': True, 'scope': 'native snapshot helper, encryption and assistant files; synthetic stopped data'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'base-release', 'trusted-key', 'native-source', 'native-parity',
                 'gpu-log', 'browser-evidence', 'wheel-cache', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('publisher-sha256', 'backend-image', 'frontend-image', 'provider-image'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--terminal-evidence', type=Path)
    parser.add_argument('--terminal-browser-evidence', type=Path)
    parser.add_argument('--scratch', type=Path, default=Path('/dev/shm'))
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path[:0] = [str(source / 'deploy/release'), str(source / 'deploy/release/cli')]
    from ccheck import assistant
    from ccheck import remote_terminal
    from ccheck.core import digest, verify_release, DeploymentError
    from ccheck.image_identity import inspect_image, locate
    from ccheck.schema_migration import validate
    from repack_deployment import bootstrap, verify_source
    original = verify_release(args.base_release, args.trusted_key, args.publisher_sha256)
    if original['version'] not in {'v0.1.0-rc.19', 'v0.1.0-rc.20'}:
        raise RuntimeError('only an original signed Qwen2.5 baseline is allowed')
    model = json.loads((args.base_release / 'models/model-manifest.json').read_text())
    assert next(row['repository'] for row in model['models'] if row['key'] == 'qwen') == 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8'
    current, tested = verify_source(source), verify_source(args.native_source)
    if runtime_files(current['files'], assistant.RESOURCES) != runtime_files(tested['files'], assistant.RESOURCES):
        raise RuntimeError('runtime source changed since native/GPU acceptance')
    images = {name: inspect_image(image)['Id'] for name, image in
        (('backend', args.backend_image), ('frontend', args.frontend_image), ('anythingllm', args.provider_image))}
    images.update({name: locate(original['images'][name]) for name in ('qwen', 'embedding')})
    if not all(images.values()):
        raise RuntimeError('all signed inference images must already be loaded')
    parity = json.loads(args.native_parity.read_text())
    assert parity['passed'] is True and parity['image_id'] == images['backend']
    log = args.gpu_log.read_text()
    required = ('PUBLIC_REVIEW_COMPLETED_AND_REPORT_DOWNLOADS_OK',
        'PUBLIC_CHAT_REVIEW_PREEMPTION_WORKER_REPORT_PDF_HISTORY_AND_RESUME_OK',
        'PUBLIC_API_CELERY_BATCH_UPLOAD_REAL_RAG_HISTORY_AND_USER_ISOLATION_OK',
        'ADMIN_RECOVERY_STALE_EPOCH_DOCUMENT_RETRY_REAL_RAG_AND_LATE_OWNER_FENCING_OK',
        'ORIGINAL_INFERENCE_HEALTH_RECOVERY_CONFIRMED')
    if not all(marker in log for marker in required):
        raise RuntimeError('real GPU acceptance evidence is incomplete')
    browser = json.loads(args.browser_evidence.read_text())
    if browser.get('mockedApi') is not False or not all(browser.get(name) is True for name in
            ('batchUpload', 'rag', 'sources', 'realReviewPreemption', 'busyDisabled', 'explicitStop')):
        raise RuntimeError('real browser evidence is incomplete')
    with_terminal = args.terminal_evidence is not None
    if with_terminal != (args.terminal_browser_evidence is not None):
        raise RuntimeError('terminal lifecycle and real browser evidence must be supplied together')
    if with_terminal:
        terminal = json.loads(args.terminal_evidence.read_text())
        terminal_browser = json.loads(args.terminal_browser_evidence.read_text())
        if (not all(terminal.get(name) is True for name in
                ('passed', 'tls', 'restart_preservation', 'configuration', 'isolation'))
                or terminal.get('runtime') != remote_terminal.hashes()
                or not all(terminal_browser.get(name) is True for name in
                    ('passed', 'real_ssh', 'interactive_commands', 'ctrl_c', 'fresh_remote_file_verified_and_removed'))):
            raise RuntimeError('managed terminal acceptance is incomplete or runtime changed')
    if shutil.disk_usage(args.scratch).free < 8 * 1024**3:
        raise RuntimeError('signing probe requires 8 GiB free scratch space')
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:')) * 1024
    if args.scratch == Path('/dev/shm') and available < 14 * 1024**3:
        raise RuntimeError('insufficient RAM for a non-disruptive tmpfs release probe')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='assistant-signing-fixture-', dir=args.output.parent) as temporary, \
            tempfile.TemporaryDirectory(prefix='assistant-release-', dir=args.scratch) as scratch:
        root = Path(temporary)
        key = root / 'fixture-private.pem'
        execute(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(key)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        base = fixture_base(original, root / 'base', key, verify_release, digest, bootstrap)
        checkout = root / 'checkout'
        shutil.copytree(source, checkout, ignore=shutil.ignore_patterns('build-source.json'))
        execute(['git', 'init', '-q', str(checkout)])
        execute(['git', '-C', str(checkout), 'config', 'core.autocrlf', 'false'])
        execute(['git', '-C', str(checkout), 'add', '.'])
        execute(['git', '-C', str(checkout), '-c', 'user.name=Signing Fixture', '-c',
            'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm',
            'Disposable assistant release evaluation; not a published source ref'])
        packed = root / 'committed-source.tar.gz'
        execute([sys.executable, '-B', str(checkout / 'deploy/release/pack_source.py'), '--output', str(packed)], cwd=checkout)
        context = root / 'context'
        extract(packed, context)
        frozen = verify_source(context, committed=True)
        yaml = prepare_inputs(context, base, args.wheel_cache, digest)
        (context / 'built-images.json').write_text(json.dumps({name: {'id': image} for name, image in images.items()}))
        gates = {'passed': True, 'version': 'v0.1.0-rc.999', 'source_tree': frozen['tree_sha256'],
            'images': images, 'evaluation_fixture': True, 'assistant': dict.fromkeys(
                ('passed', 'real_gpu', 'review_preemption', 'rag', 'owner_isolation', 'recovery', 'source_native_parity'), True)}
        if with_terminal:
            gates['terminal'] = dict.fromkeys(('passed', 'real_ssh', 'tls', 'restart_preservation', 'configuration', 'isolation'), True)
        gate_file = root / 'gates.json'
        gate_file.write_text(json.dumps(gates))
        pins = {'python:3.12-slim-bookworm': base['runtime_recipe']['base_image']}
        for name, tag in (('mysql', 'mysql:8.0'), ('redis', 'redis:7.4-alpine'), ('qdrant', 'qdrant/qdrant:v1.15.5')):
            pins[tag] = base['images'][name]['reference']
        pin_file = root / 'pins.json'
        pin_file.write_text(json.dumps(pins))
        environment = dict(os.environ, CCHECK_BASE_IMAGE_LOCK=str(pin_file), PYTHONDONTWRITEBYTECODE='1')
        release_root = Path(scratch) / 'release-v0.1.0-rc.999'
        print('SIGNING_STAGE real-release-assembly-with-disposable-key', flush=True)
        with (args.output / 'assembly.log').open('wb') as stream:
            execute([sys.executable, '-B', str(context / 'deploy/release/build_release.py'), str(context),
                '--version', gates['version'], '--output', str(release_root), '--gates', str(gate_file),
                '--signing-key', str(key), '--yaml-package', str(yaml), '--with-assistant',
                '--reuse-release', str(root / 'base'), '--reuse-trusted-key', str(root / 'base/publisher.pem')]
                + (['--with-terminal', '--deployment-patch'] if with_terminal else []),
                stdout=stream, stderr=subprocess.STDOUT, env=environment)
        release = verify_release(release_root, root / 'base/publisher.pem', base['_publisher'])
        validate(base, release)
        assert release.get('review_cli') == base['review_cli'], 'review CLI metadata was lost'
        inherited = {'cli/ccheck-review.pyz', 'ccheck-review', 'CLI-REVIEW.md'}
        if not with_terminal:
            inherited.update(name for name in base['files'] if name.startswith('terminal/'))
        assert all(release['files'].get(name) == base['files'][name] for name in inherited)
        with zipfile.ZipFile(release_root / 'cli/ccheck.pyz') as archive:
            for name, checksum in release['assistant_runtime'].items():
                assert hashlib.sha256(archive.read('ccheck/assistant_runtime/' + name)).hexdigest() == checksum
            for name, checksum in assistant.migration_hashes().items():
                assert hashlib.sha256(archive.read('ccheck/assistant_migrations/' + name)).hexdigest() == checksum
            if with_terminal:
                assert release['terminal_runtime'] == remote_terminal.hashes()
                for name, checksum in release['terminal_runtime'].items():
                    assert hashlib.sha256(archive.read('ccheck/terminal_runtime/' + name)).hexdigest() == checksum
                bundle = release_root.parent / ('release-' + release['version'] + '-patch.tar.gz')
                with tarfile.open(bundle) as patch:
                    entries = set(patch.getnames())
                assert all(release_root.name + '/' + name not in entries
                           for name in release['distribution']['reused_files'])
                assert release_root.name + '/REMOTE-SSH.md' in entries
        original_manifest = (release_root / 'release.json').read_bytes()
        (release_root / 'release.json').write_bytes(original_manifest + b'\n')
        try:
            verify_release(release_root, root / 'base/publisher.pem', base['_publisher'])
        except DeploymentError:
            pass
        else:
            raise AssertionError('tampered release signature was accepted')
        (release_root / 'release.json').write_bytes(original_manifest)
        snapshot = snapshot_roundtrip(root, images['backend'])
        shutil.copy2(release_root / 'release.json', args.output / 'fixture-release.json')
        report = {'passed': True, 'evaluation_fixture': True, 'published': False,
            'production_signing_key_used': False, 'fixture_private_key_retained': False,
            'source_tree': frozen['tree_sha256'], 'original_base_sha256': original['_sha256'],
            'fixture_release_sha256': release['_sha256'], 'images': images,
            'signature_tamper_rejected': True, 'bundled_resources_and_migrations': True,
            'existing_cli_and_terminal_preserved': True,
            'managed_terminal': with_terminal,
            'snapshot_roundtrip': snapshot, 'hardware_changed': False,
            'scope': 'real release assembly/signature, additive manifest approval and native encrypted snapshot protocol'}
        (args.output / 'release-pipeline.json').write_text(json.dumps(report, indent=2))
        print('ASSISTANT_DISPOSABLE_KEY_RELEASE_ASSEMBLY_AND_SNAPSHOT_PROTOCOL_OK', json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
