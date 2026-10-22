import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import tempfile
import time

from .core import DeploymentError, atomic_json, checked_path, digest, installation_lock, run
from .engine import Engine


def key_material(path):
    path = Path(path).resolve(strict=True)
    if path.stat().st_mode & 0o077 or path.stat().st_size < 64 or path.stat().st_size > 4098:
        raise DeploymentError('backup key must be owner-only hexadecimal text; generate with openssl rand -hex 48')
    material = path.read_bytes()
    body = material.rstrip(b'\r\n')
    if not re.fullmatch(rb'[0-9a-fA-F]{64,4096}(?:\r?\n)?', material) or len(body) % 2:
        raise DeploymentError('backup key must be a single hexadecimal line; generate with openssl rand -hex 48')
    return path, hashlib.sha256(b'c-check-backup-auth-v1\0' + material).digest()


def helper(engine, state, operation):
    mount = 'type=bind,src=' + str(engine.root) + ',dst=/bundle' + (',readonly' if operation != 'restore' else '')
    return ['docker', 'run', '--rm', '-i', '--user', '0:0', '--network', 'none', '--read-only', '--log-driver', 'none',
            '--label', 'io.ccheck.backup-helper=' + operation,
            '--mount', mount,
            '--entrypoint', 'python', state['images']['backend'], '-m', 'app.backup', operation,
            '--uid', str(state['uid']), '--gid', str(state['gid'])]


def prune_completed(engine, site, mac_key):
    valid = []
    for directory in (engine.root / 'backups').iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        files = list(directory.iterdir())
        if any(path.is_symlink() or not path.is_file() or path.name not in {
                'backup.json', 'snapshot.tar.enc', 'snapshot.log', 'restore.log'} for path in files):
            continue
        try:
            metadata = json.loads((directory / 'backup.json').read_text())
            signature = metadata.pop('hmac_sha256')
            expected = hmac.new(mac_key, json.dumps(metadata, sort_keys=True).encode(), hashlib.sha256).hexdigest()
            if (metadata['instance'] == site['instance'] and hmac.compare_digest(signature, expected)
                    and digest(directory / 'snapshot.tar.enc') == metadata['sha256']):
                valid.append((metadata['created_at'], directory))
        except (OSError, ValueError, KeyError):
            continue
    keep = site.get('backup', {}).get('keep', 3)
    for _, directory in sorted(valid, reverse=True)[keep:]:
        checked_path(engine.root, directory.relative_to(engine.root).as_posix())
        shutil.rmtree(directory)


def make_backup(site, state, key_file, *, locked=False):
    engine = Engine(site)
    key_file, mac_key = key_material(key_file)
    if key_file.is_relative_to(engine.root):
        raise DeploymentError('backup key must be outside the installation and its backups')
    if not locked:
        with installation_lock(engine.root):
            return make_backup(site, state, key_file, locked=True)
    prior_maintenance = (engine.root / 'data/control/maintenance').exists()
    engine.wait(['mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'api', 'worker', 'frontend'], timeout=60)
    amount = json.loads(run(helper(engine, state, 'measure')).strip())
    needed = amount['bytes'] + amount['entries'] * 4096 + 3 * 1024**3
    if shutil.disk_usage(engine.root).free < needed:
        raise DeploymentError('insufficient space for encrypted backup and safety reserve')
    engine.drain()
    directory = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S') + '-', dir=engine.root / 'backups'))
    encrypted = directory / 'snapshot.tar.enc'
    try:
        engine.compose('stop', '--timeout', '120', 'api', 'worker', 'frontend', 'mysql', 'redis', 'qdrant')
        with encrypted.open('wb') as output, (directory / 'snapshot.log').open('wb') as log:
            source = subprocess.Popen(helper(engine, state, 'create'), stdout=subprocess.PIPE, stderr=log)
            try:
                process = subprocess.Popen(['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '200000',
                                            '-pass', 'file:' + str(key_file)], stdin=source.stdout, stdout=output, stderr=log)
            except Exception:
                source.stdout.close()
                source.terminate()
                try:
                    source.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    source.kill()
                    source.wait()
                raise
            source.stdout.close()
            try:
                encrypted_status = process.wait(timeout=900)
                source_status = source.wait(timeout=60)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                source.kill()
                process.wait()
                source.wait()
                raise DeploymentError('backup timed out') from exc
            if encrypted_status or source_status:
                raise DeploymentError('backup encryption/snapshot failed')
        encrypted.chmod(0o600)
        metadata = {'format': 1, 'version': state['version'], 'release_sha256': state['release_sha256'],
                    'schema_epoch': state['schema_epoch'], 'images': state['images'], 'image_identities': state['image_identities'],
                    'model_manifest_sha256': state['model_manifest_sha256'], 'cipher': 'aes-256-cbc-pbkdf2-200000',
                    'sha256': digest(encrypted), 'created_at': time.time(), 'instance': site['instance']}
        metadata['hmac_sha256'] = hmac.new(mac_key, json.dumps(metadata, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        atomic_json(directory / 'backup.json', metadata)
        prune_completed(engine, site, mac_key)
    except Exception:
        if not (directory / 'backup.json').exists():
            encrypted.unlink(missing_ok=True)
        raise
    finally:
        from .lifecycle import start_stack
        start_stack(engine)
        engine.maintenance(prior_maintenance)
    print('ENCRYPTED_BACKUP', directory)
    return directory


def restore_backup(site, state, directory, key_file, confirm):
    engine = Engine(site)
    if confirm != site['instance']:
        raise DeploymentError('restore requires --confirm-instance matching the managed instance')
    directory = Path(directory).resolve(strict=True)
    key_file, mac_key = key_material(key_file)
    if key_file.is_relative_to(engine.root):
        raise DeploymentError('backup key must be outside the installation')
    metadata = json.loads((directory / 'backup.json').read_text())
    signature = metadata.pop('hmac_sha256')
    expected = hmac.new(mac_key, json.dumps(metadata, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected) or digest(directory / 'snapshot.tar.enc') != metadata['sha256']:
        raise DeploymentError('backup authentication/checksum failed; no data was changed')
    if (metadata['schema_epoch'] != state['schema_epoch'] or metadata['image_identities'] != state['image_identities']
            or metadata['model_manifest_sha256'] != state['model_manifest_sha256']):
        raise DeploymentError('backup requires matching schema and exact service images')
    with installation_lock(engine.root):
        engine.drain()
        engine.compose('stop', '--timeout', '120')
        try:
            with (directory / 'restore.log').open('wb') as log:
                decrypted = subprocess.Popen(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
                    '-pass', 'file:' + str(key_file), '-in', str(directory / 'snapshot.tar.enc')], stdout=subprocess.PIPE, stderr=log)
                extract = subprocess.Popen(helper(engine, state, 'restore'), stdin=decrypted.stdout, stdout=log, stderr=log)
                decrypted.stdout.close()
                try:
                    extract_status = extract.wait(timeout=900)
                    decrypted_status = decrypted.wait(timeout=60)
                except subprocess.TimeoutExpired as exc:
                    extract.kill()
                    decrypted.kill()
                    extract.wait()
                    decrypted.wait()
                    raise DeploymentError('restore timed out; services remain stopped') from exc
                if extract_status or decrypted_status:
                    raise DeploymentError('restore failed; services remain stopped, inspect owner-only restore log')
            # Secret files were restored; Compose mounts resolve them when containers are recreated.
            engine.compose('up', '-d', '--force-recreate', 'mysql', 'redis', 'qdrant')
            engine.wait(['mysql', 'redis', 'qdrant'])
            engine.management('migrate')
            engine.compose('up', '-d', '--force-recreate', 'qwen', 'embedding', 'api', 'worker', 'frontend')
            engine.wait(['qwen', 'embedding', 'api', 'worker', 'frontend'])
            engine.management('register-model')
            engine.maintenance(False)
            for marker in (engine.root / 'state').glob('acceptance-*.json'):
                marker.unlink()
            from .lifecycle import verify
            verify(site, fresh=True)
        except Exception:
            engine.maintenance(True)
            raise
    print('RESTORED', directory)
