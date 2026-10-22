"""Portable, staged Linux publisher; never installs host packages or publishes remotely."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from ccheck.core import DeploymentError, atomic_json, checked_path, digest
from ccheck.models import obtain_file

REPOSITORY = Path(__file__).resolve().parent.parent.parent
GROUPS = {'backend', 'qwen', 'embedding', 'compiler'}


def settings(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    allowed = {'version', 'source_ref', 'work_root', 'baseline', 'wheel_cache', 'wheel_download_policy'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise DeploymentError('unknown publisher configuration fields')
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', data.get('version', '')):
        raise DeploymentError('use a new semantic release version')
    if not isinstance(data.get('source_ref'), str) or not data['source_ref'] or data['source_ref'].startswith('-'):
        raise DeploymentError('source_ref must be an approved committed Git reference')
    root = Path(data['work_root']).expanduser()
    if (not root.is_absolute() or len(root.parts) < 3 or root == Path.home()
            or any(character in str(root) for character in '\x00\r\n,')
            or any(p.is_symlink() for p in [root, *root.parents])
            or root.resolve().is_relative_to(REPOSITORY)):
        raise DeploymentError('work_root must be a dedicated absolute directory outside the checkout, without symlinks')
    data['work_root'] = str(root.resolve())
    if not re.fullmatch(r'[a-z0-9-]+', data.get('baseline', '')):
        raise DeploymentError('select a checked-in baseline name')
    cache = data.get('wheel_cache')
    if cache is not None and (not isinstance(cache, str) or not Path(cache).is_absolute()):
        raise DeploymentError('wheel_cache must be null or an absolute read-only cache directory')
    if data.get('wheel_download_policy') not in {'online', 'offline'}:
        raise DeploymentError('explicit wheel_download_policy online/offline is required')
    return data


def verify_baseline(root):
    manifest = json.loads((root / 'baseline.json').read_text())
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(manifest['files']) | {'baseline.json'}:
        raise DeploymentError('unexpected or missing build baseline file')
    if any(p.is_symlink() for p in root.rglob('*')):
        raise DeploymentError('symlinked build baseline refused')
    for name, expected in manifest['files'].items():
        if digest(checked_path(root, name)) != expected:
            raise DeploymentError('build baseline checksum mismatch: ' + name)
    return manifest


def verify_source(source):
    manifest = json.loads((source / 'build-source.json').read_text())
    if manifest.get('worktree_snapshot'):
        raise DeploymentError('publisher requires a committed source snapshot')
    tree_hash = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    if tree_hash != manifest['tree_sha256']:
        raise DeploymentError('source inventory identity mismatch')
    for name, expected in manifest['files'].items():
        if digest(checked_path(source, name)) != expected:
            raise DeploymentError('frozen source changed: ' + name)
    return manifest


@contextlib.contextmanager
def workspace(config):
    import fcntl
    root = Path(config['work_root'])
    marker = root / 'publisher-owner.json'
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise DeploymentError('refusing a nonempty unowned build directory')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if marker.exists() and json.loads(marker.read_text()) != config:
        raise DeploymentError('build configuration changed; use a new work_root')
    if not marker.exists():
        atomic_json(marker, config)
    with (root / '.publisher.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError('another publisher operation owns this directory') from exc
        yield root


def command(root, name, arguments, env=None):
    logs = root / 'logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    print(name, 'log:', logs / (name + '.log'), flush=True)
    with (logs / (name + '.log')).open('ab') as log:
        subprocess.run(arguments, cwd=REPOSITORY, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def wheel_url(row):
    if row.get('url'):
        address = row['url']
    else:
        metadata_url = row['metadata_url']
        origin = urlsplit(metadata_url)
        if origin.scheme != 'https' or origin.hostname != 'pypi.org' or origin.username or origin.password:
            raise DeploymentError('unapproved wheel metadata origin')
        for attempt in range(4):
            try:
                with urlopen(metadata_url, timeout=60) as response:
                    if urlsplit(response.url).scheme != 'https':
                        raise DeploymentError('insecure wheel metadata redirect')
                    metadata = json.load(response)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        matches = [item for item in metadata['urls'] if item['filename'] == row['file']
                   and item['digests']['sha256'] == row['sha256'] and item['size'] == row['size']]
        if len(matches) != 1:
            raise DeploymentError('pinned wheel is unavailable upstream: ' + row['file'])
        address = matches[0]['url']
    origin = urlsplit(address)
    if origin.scheme != 'https' or origin.username or origin.password or origin.hostname not in {
            'files.pythonhosted.org', 'download.pytorch.org'}:
        raise DeploymentError('unapproved wheel download origin')
    return address


def prepare_wheel(row, baseline, source, config):
    if row['environment'] not in GROUPS or Path(row['file']).name != row['file'] or '\\' in row['file']:
        raise DeploymentError('unsafe wheel record')
    target = checked_path(source, 'wheelhouse/' + row['environment'] + '/' + row['file'])
    cached = checked_path(baseline, row['local']) if row.get('local') else (
        checked_path(config['wheel_cache'], row['environment'] + '/' + row['file'])
        if config.get('wheel_cache') else None)
    valid = lambda p: p is not None and p.is_file() and p.stat().st_size == row['size'] and digest(p) == row['sha256']
    entry = dict(row)
    online = config['wheel_download_policy'] == 'online' and not row.get('local')
    if not valid(target) and not valid(cached) and online:
        entry['url'] = wheel_url(row)
    return obtain_file(entry, target, cached, allow_download=online)


def prepare(root, config):
    source = root / 'source'
    if not source.exists():
        snapshot = root / 'source.tar.gz'
        command(root, 'export-source', [sys.executable, str(REPOSITORY / 'deploy/release/pack_source.py'),
                                      '--ref', config['source_ref'], '--output', str(snapshot)])
        source.mkdir(mode=0o700)
        with tarfile.open(snapshot) as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if (not member.isfile() or name.is_absolute() or '..' in name.parts or '\\' in member.name
                        or member.size > 20 * 1024**2 or not (member.name == 'build-source.json'
                        or member.name.startswith(('backend/', 'frontend/', 'deploy/models/', 'deploy/release/')))):
                    raise DeploymentError('unsafe source snapshot member')
            archive.extractall(source)
    identity = verify_source(source)
    baseline = source / 'deploy/release/baselines' / config['baseline']
    verify_baseline(baseline)
    for source_name, destination in [('locks', source / 'locks'), ('models', source / 'release-input/models')]:
        shutil.copytree(baseline / source_name, destination, dirs_exist_ok=True)
    records = json.loads((baseline / 'wheels.json').read_text())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda row: prepare_wheel(row, baseline, source, config), records))
    # Vendor only the pure-Python YAML reader into the deployment CLI tool directory.
    yaml_row = next(row for row in records if row['environment'] == 'backend'
                    and row['distribution'].lower() == 'pyyaml')
    yaml_root = root / 'tools'
    with zipfile.ZipFile(source / 'wheelhouse/backend' / yaml_row['file']) as wheel:
        for name in wheel.namelist():
            if name.startswith('yaml/') and name.endswith('.py'):
                target = checked_path(yaml_root, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(wheel.read(name))
    if not (yaml_root / 'yaml/__init__.py').is_file():
        raise DeploymentError('verified wheel has no pure-Python YAML package')
    atomic_json(root / 'prepared.json', {'source': identity['git_sha'], 'tree_sha256': identity['tree_sha256'],
                                       'version': config['version'], 'wheels': len(records)})


def inputs(root, config):
    if not (root / 'prepared.json').is_file():
        raise DeploymentError('run prepare successfully first')
    source = root / 'source'
    verify_source(source)
    baseline = source / 'deploy/release/baselines' / config['baseline']
    verify_baseline(baseline)
    for prefix, destination in [('locks/', source / 'locks'), ('models/', source / 'release-input/models')]:
        for name, checksum in json.loads((baseline / 'baseline.json').read_text())['files'].items():
            if name.startswith(prefix) and digest(checked_path(destination, name[len(prefix):])) != checksum:
                raise DeploymentError('prepared dependency input changed: ' + name)
    for row in json.loads((baseline / 'wheels.json').read_text()):
        target = checked_path(source, 'wheelhouse/' + row['environment'] + '/' + row['file'])
        if target.stat().st_size != row['size'] or digest(target) != row['sha256']:
            raise DeploymentError('prepared wheel changed: ' + row['file'])
    env = os.environ.copy()
    env.update(PYTHONPATH=str(root / 'tools'), PYTHONDONTWRITEBYTECODE='1',
               CCHECK_BASE_IMAGE_LOCK=str(baseline / 'base-images.json'))
    return source, baseline, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'bases', 'build', 'gates', 'package', 'status'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--only', choices=['backend', 'frontend', 'qwen', 'embedding', 'all'], default='all')
    parser.add_argument('--signing-key', type=Path)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'amd64'}:
        parser.error('use a Linux/amd64 publisher; this is not the recipient installer')
    os.umask(0o077)
    config = settings(args.config)
    if args.phase == 'status':
        for name in ['publisher-owner.json', 'operation.json', 'prepared.json']:
            path = Path(config['work_root']) / name
            if path.exists():
                print(name, path.read_text())
        return
    with workspace(config) as root:
        atomic_json(root / 'operation.json', {'phase': args.phase, 'status': 'running', 'pid': os.getpid(), 'time': time.time()})
        try:
            if args.phase == 'prepare':
                prepare(root, config)
            else:
                source, baseline, env = inputs(root, config)
                tool = source / 'deploy/release'
                if args.phase == 'bases':
                    for index, reference in enumerate(json.loads((baseline / 'base-images.json').read_text()).values()):
                        command(root, 'pull-base-' + str(index), ['docker', 'pull', '--platform', 'linux/amd64', reference], env)
                elif args.phase == 'build':
                    command(root, 'build-' + args.only, [sys.executable, str(tool / 'build_images.py'), str(source),
                            '--version', config['version'], '--only', args.only], env)
                elif args.phase == 'gates':
                    command(root, 'gates', [sys.executable, str(tool / 'validate_images.py'), str(source),
                            '--version', config['version'], '--output', str(root / 'gates')], env)
                elif args.phase == 'package':
                    if not args.signing_key or args.signing_key.resolve().is_relative_to(root):
                        raise DeploymentError('supply an external owner-only Ed25519 key with --signing-key')
                    command(root, 'package', [sys.executable, str(tool / 'build_release.py'), str(source),
                            '--version', config['version'], '--output', str(root / ('release-' + config['version'])),
                            '--gates', str(root / 'gates/gates.json'), '--signing-key', str(args.signing_key.resolve()),
                            '--yaml-package', str(root / 'tools/yaml')], env)
            atomic_json(root / 'operation.json', {'phase': args.phase, 'status': 'completed', 'time': time.time()})
            print('COMPLETED', args.phase, config['version'])
        except BaseException:
            atomic_json(root / 'operation.json', {'phase': args.phase, 'status': 'failed', 'time': time.time()})
            raise


if __name__ == '__main__':
    main()
