"""Online delivery: signed immutable inputs, private resumable cache, no host pip."""
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote, urlsplit

from .core import DeploymentError, checked_path, run


def settings(site):
    value = site.get('delivery', {})
    if not isinstance(value, dict) or set(value) - {'cache_dir', 'offline', 'artifact_base_url', 'registry_mirrors'}:
        raise DeploymentError('unknown delivery configuration')
    result = dict(cache_dir=str(Path(site['data_dir']).parent / '.ccheck-downloads'),
                  offline=False, registry_mirrors={})
    result.update(value)
    result['registry_mirrors'] = value.get('registry_mirrors', {})
    path = Path(result['cache_dir'])
    if not path.is_absolute() or len(path.parts) < 3 or any(p.is_symlink() for p in [path, *path.parents]):
        raise DeploymentError('delivery cache must be an absolute dedicated non-symlink directory')
    if path == Path(site['data_dir']) or path == Path.home():
        raise DeploymentError('delivery cache must not be the instance or home directory')
    if type(result['offline']) is not bool:
        raise DeploymentError('delivery.offline must be a boolean')
    if 'artifact_base_url' in result:
        url = urlsplit(result['artifact_base_url'])
        if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise DeploymentError('artifact mirror must be a credential-free HTTPS base URL')
    mirrors = result['registry_mirrors']
    if not isinstance(mirrors, dict):
        raise DeploymentError('registry_mirrors must be a mapping')
    for origin, target in mirrors.items():
        if (not isinstance(origin, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*', origin)
                or not isinstance(target, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*', target)
                or '://' in target or '//' in target
                or '..' in target.split('/')):
            raise DeploymentError('registry mirrors require registry hosts and optional repository prefixes, not URLs or credentials')
    return result


def is_online(release):
    return release.get('distribution', {}).get('kind') == 'online'


def validate_release(release):
    if not is_online(release):
        return
    if release.get('runtime_recipe') is not None:
        raise DeploymentError('online image delivery does not allow deployment-time source builds')
    if release.get('_root'):
        lock = release.get('components_lock', {})
        if lock.get('file') != 'components.lock.json' or release['files'].get(lock['file']) != lock.get('sha256'):
            raise DeploymentError('online component lock is not bound to the signed inventory')
        inventory = json.loads(checked_path(release['_root'], lock['file']).read_text())
        models = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())
        if inventory.get('version') != release['version'] or inventory.get('images') != release['images'] or inventory.get('models') != models:
            raise DeploymentError('online component lock and signed release disagree')
    for spec in release['images'].values():
        if spec.get('archive_parts') or spec.get('archive'):
            raise DeploymentError('online release must not require bundled image archives')
        parts = spec.get('remote_parts', [])
        if not parts:
            raise DeploymentError('online image requires signed resumable archive fallback')
        if spec.get('reference'):
            registry_reference(spec['reference'], {})
        for part in parts:
            if (set(part) != {'name', 'url', 'size', 'sha256'}
                    or not re.fullmatch(r'[a-z0-9][a-z0-9._-]+', part['name'])
                    or not re.fullmatch(r'[0-9a-f]{64}', part['sha256'])
                    or type(part['size']) is not int or not 0 < part['size'] <= 2 * 1024**3):
                raise DeploymentError('invalid signed remote image part')
            url = urlsplit(part['url'])
            if url.scheme != 'https' or not url.netloc or url.username or url.password or url.fragment:
                raise DeploymentError('signed artifact URL must be HTTPS without credentials')


def registry_reference(reference, mirrors):
    if (not isinstance(reference, str) or '://' in reference or '//' in reference
            or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}', reference)):
        raise DeploymentError('image registry reference must be pinned by SHA256 digest, not a mutable tag')
    repository, checksum = reference.split('@')
    parts = repository.split('/')
    if len(parts) == 1:
        repository = 'docker.io/library/' + repository
    elif '.' not in parts[0] and ':' not in parts[0] and parts[0] != 'localhost':
        repository = 'docker.io/' + repository
    origin, rest = repository.split('/', 1)
    if origin in mirrors:
        repository = mirrors[origin].rstrip('/') + '/' + rest
    return repository + '@' + checksum


@contextlib.contextmanager
def cache_lock(site):
    path = Path(settings(site)['cache_dir'])
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise DeploymentError('symlinked delivery cache refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise DeploymentError('delivery cache must belong to the invoking user with mode 0700')
    marker = checked_path(path, '.download.lock')
    import fcntl
    with marker.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError('another deployment is preparing this delivery cache') from exc
        yield path


def model_file(site, model, entry):
    return checked_path(settings(site)['cache_dir'], 'models/' + model['cache_name'] + '/' + entry['path'])


def cache_image_parts(spec, site):
    from .models import obtain_file
    options = settings(site)
    paths = []
    for part in spec['remote_parts']:
        entry = dict(part)
        if options.get('artifact_base_url'):
            entry['url'] = options['artifact_base_url'].rstrip('/') + '/' + quote(part['name'], safe='')
        target = checked_path(options['cache_dir'], 'images/' + part['sha256'] + '/' + part['name'])
        obtain_file(entry, target, allow_download=not options['offline'])
        paths.append(target)
    return paths


def import_image(spec, site):
    from .image_identity import locate
    options = settings(site)
    if spec.get('reference') and not options['offline']:
        reference = registry_reference(spec['reference'], options['registry_mirrors'])
        try:
            run(['docker', 'pull', '--platform', 'linux/amd64', reference], timeout=180)
            if locate(spec) is None:
                raise DeploymentError('registry image differs from the signed filesystem/config identity')
            return 'registry'
        except (DeploymentError, subprocess.TimeoutExpired):
            print('  pinned registry unavailable; using signed archive fallback', flush=True)
    paths = cache_image_parts(spec, site)
    import subprocess
    import tempfile
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['docker', 'image', 'load'], stdin=subprocess.PIPE, stdout=log, stderr=log)
        try:
            for path in paths:
                with path.open('rb') as stream:
                    import shutil
                    shutil.copyfileobj(stream, process.stdin, length=4 * 1024**2)
            process.stdin.close()
            if process.wait(timeout=3600):
                raise DeploymentError('online image archive import failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if locate(spec) is None:
        raise DeploymentError('downloaded image differs from signed filesystem/config identity')
    return 'signed-archive'


def plan(release, site):
    validate_release(release)
    if not is_online(release):
        raise DeploymentError('this operation requires the lightweight online release')
    from .image_identity import locate
    rows = []
    for name, spec in release['images'].items():
        cached = locate(spec) is not None
        rows.append({'name': name, 'cached': cached,
            'registry': registry_reference(spec['reference'], settings(site)['registry_mirrors']) if spec.get('reference') else None,
            'archive_download_bytes_if_needed': 0 if cached else sum(p['size'] for p in spec['remote_parts'])})
    return {'version': release['version'], 'mode': 'online', 'cache_dir': settings(site)['cache_dir'],
            'images': rows, 'offline': settings(site)['offline'], 'host_packages_installed': False}


def prepare_assets(release, site, *, include_models=True):
    validate_release(release)
    if not is_online(release):
        raise DeploymentError('prepare-assets requires the lightweight online release')
    from .engine import prepare_images
    from .models import model_cache_file, obtain_file
    with cache_lock(site):
        images = prepare_images(release, site=site, cache_locked=True)
        counts = {}
        if include_models:
            models = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())['models']
            for model in models:
                for entry in model['files']:
                    source = model_cache_file(site, model, entry)
                    action = obtain_file(entry, model_file(site, model, entry), source,
                        allow_download=site.get('model_download_policy', 'online') != 'offline' and not settings(site)['offline'])
                    counts[action] = counts.get(action, 0) + 1
    return {'version': release['version'], 'images_verified': sorted(images), 'model_actions': counts,
            'services_started': False, 'cache_dir': settings(site)['cache_dir']}
