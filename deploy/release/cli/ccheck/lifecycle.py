import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .acceptance import acceptance
from .core import DeploymentError, atomic_json, checked_path, digest, installation_lock, stage, verify_release
from .engine import Engine, preflight, prepare_data, prepare_images, project_name, render_compose
from .models import prepare_models, verify_models
from .gpu import validate_inspection
from . import networks
from . import acceleration
from . import inference


def installed(site):
    root = Path(site['data_dir'])
    owner = checked_path(root, 'state/owner.json')
    expected = {'instance': site['instance'], 'root': str(root)}
    if not owner.exists() or any(json.loads(owner.read_text()).get(key) != value for key, value in expected.items()):
        raise DeploymentError('installation ownership mismatch')
    state = json.loads(checked_path(root, 'state/deployment.json').read_text())
    return state


def cache_release(site, release, key):
    target = checked_path(site['data_dir'], 'releases/' + release['version'])
    publisher = checked_path(site['data_dir'], 'config/publisher.pem')
    if publisher.exists() and digest(publisher) != release['_publisher']:
        raise DeploymentError('publisher key changed')
    if not publisher.exists():
        publisher.write_bytes(Path(key).read_bytes())
        publisher.chmod(0o600)
    if target.exists():
        if digest(target / 'release.json') != release['_sha256']:
            raise DeploymentError('same release version already exists with different content')
        return verify_release(target, publisher, release['_publisher'])
    staging = Path(tempfile.mkdtemp(prefix='.incoming-' + release['version'] + '-', dir=target.parent))
    try:
        shutil.copytree(release['_root'], staging, dirs_exist_ok=True)
        verified = verify_release(staging, publisher, release['_publisher'])
        staging.rename(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    verified['_root'] = str(target)
    return verified


def fix_model_permissions(site):
    # Cache files may be hard links: never chmod/chown them or mutate their contents.
    for model in (Path(site['data_dir']) / 'models').iterdir():
        if model.is_symlink():
            raise DeploymentError('symlinked model directories are not supported')
        if model.is_dir():
            model.chmod(0o755)
            for folder in model.rglob('*'):
                if folder.is_symlink():
                    raise DeploymentError('symlinked model artifact refused')
                if folder.is_dir():
                    folder.chmod(0o755)


def wait_data_services(engine, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return engine.management('data-health')
        except DeploymentError as exc:
            if time.monotonic() >= deadline:
                raise DeploymentError('data-service readiness deadline exceeded; inspect MySQL/Redis/Qdrant logs') from exc
            print('Waiting for data services from the application network.', flush=True)
            time.sleep(2)


def start_stack(engine):
    engine.compose('up', '-d', 'mysql', 'redis', 'qdrant')
    engine.wait(['mysql', 'redis', 'qdrant'])
    wait_data_services(engine)
    engine.management('migrate')
    acceleration.verify(engine.site)
    if acceleration.gpu_embedding(engine.site):
        # Stop only this project's models before re-budgeting the shared GPU.
        engine.compose('stop', '--timeout', '120', 'qwen', 'embedding')
    engine.check_gpu_admission()
    engine.check_gpu_access()
    if acceleration.gpu_embedding(engine.site):
        engine.compose('up', '-d', 'qwen')
        engine.wait(['qwen'])
        engine.compose('up', '-d', 'embedding')
        engine.wait(['embedding'])
    else:
        engine.compose('up', '-d', 'qwen', 'embedding')
        engine.wait(['qwen', 'embedding'])
    engine.compose('up', '-d', 'api', 'worker', 'frontend')
    engine.wait(['api', 'worker', 'frontend'])
    engine.verify_gpu_access()
    networks.verify(engine)
    engine.management('register-model')


def install(site, release_root, trusted_key, version=None, backup_key=None):
    old = None
    root = Path(site['data_dir'])
    if (root / 'state/deployment.json').exists():
        old = installed(site)
    release = verify_release(release_root, trusted_key, old.get('publisher') if old else None)
    if version and version != release['version']:
        raise DeploymentError('requested version differs from supplied signed release')
    if old and old['release_sha256'] != release['_sha256']:
        raise DeploymentError('use update for a different release; install never silently upgrades')
    preflight(site, release)
    networks.preflight(site)
    with installation_lock(root, create=True):
        previous_site = json.loads((root / 'config/site.json').read_text()) if (root / 'config/site.json').exists() else None
        previous_compose = json.loads((root / 'state/compose.json').read_text()) if (root / 'state/compose.json').exists() else None
        assert_no_pending_network_migration(root)
        initial_migration = previous_compose is not None and networks.needs_recreate(Engine(site), render_compose(site,
                old['images'] if old else {name: value['id'] for name, value in release['images'].items()}, 10001, 10001))
        if initial_migration:
            if backup_key is None:
                raise DeploymentError('network migration of an existing instance requires --backup-key; data and credentials were not reset')
            from .backup import key_material
            key_material(backup_key)
            begin_network_migration(Engine(site), previous_site, previous_compose, old, release['_sha256'])
        uid, gid = stage(root, 'prepare-data', lambda: prepare_data(site, release))
        release = stage(root, 'verify-release-cache', lambda: cache_release(site, release, trusted_key))
        build = site.get('runtime_source') == 'build'
        images = stage(root, 'import-images', lambda: prepare_images(release, skip={'qwen'} if build else (), site=site))
        if build:
            from .runtime import build_runtime
            images['qwen'] = stage(root, 'prepare-runtime', lambda: build_runtime(release, site))
        model_state = stage(root, 'download-models', lambda: prepare_models(release, site))
        fix_model_permissions(site)
        engine = Engine(site)
        if old:
            stage(root, 'drain', engine.drain)
        document = render_compose(site, images, uid, gid)
        migrating = previous_compose is not None and networks.needs_recreate(engine, document)
        if migrating:
            begin_network_migration(engine, previous_site, previous_compose, old, release['_sha256'])
            engine.maintenance(True)
            engine.compose('stop', '--timeout', '120')
            from .backup import make_backup
            from .image_identity import identity, inspect_image
            backup_state = old or {'version': 'partial-install', 'release_sha256': None,
                'schema_epoch': release['schema_epoch'], 'images': images, 'uid': uid, 'gid': gid,
                'model_manifest_sha256': release['files']['models/model-manifest.json'],
                'image_identities': {name: identity(inspect_image(image)) for name, image in images.items()}}
            stage(root, 'cold-backup', lambda: make_backup(site, backup_state, backup_key, locked=True, cold=True))
            networks.teardown(engine)
        atomic_json(engine.file, document)
        stage(root, 'start-services', lambda: start_stack(engine))
        engine.maintenance(False)
        try:
            result = stage(root, 'acceptance', lambda: acceptance(site, release, engine))
        except Exception:
            engine.maintenance(True)
            raise
        deployment = {'version': release['version'], 'tag': release['tag'], 'source': release['source'],
                      'release_root': release['_root'], 'release_sha256': release['_sha256'],
                      'publisher': release['_publisher'], 'schema_epoch': release['schema_epoch'],
                      'images': images, 'site_sha256': digest(root / 'config/site.json'),
                      'uid': uid, 'gid': gid,
                      'model_manifest_sha256': release['files']['models/model-manifest.json'],
                      'acceptance': result, 'time': time.time()}
        from .image_identity import identity, inspect_image
        deployment['image_identities'] = {name: identity(inspect_image(image)) for name, image in images.items()}
        atomic_json(root / 'state/deployment.json', deployment)
        finish_network_migration(root)
        print('READY local application verified; configured origin:', site['web']['public_url'], release['version'])
        print('External DNS/gateway/NAT reachability still requires verification from an actual client.')
        print('Initial administrator: admin; password file:', root / 'secrets/admin_password')
        return deployment


def verify(site, *, fresh=False):
    state = installed(site)
    root = Path(site['data_dir'])
    release = verify_release(state['release_root'], root / 'config/publisher.pem', state['publisher'])
    inference.configure(site, release)
    acceleration.validate_release(site, release)
    engine = Engine(site)
    engine.wait(['mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'api', 'worker', 'frontend'], timeout=120)
    expected = state['images']
    if digest(root / 'config/site.json') != state['site_sha256']:
        raise DeploymentError('site configuration changed outside ccheck publish/update')
    canonical = render_compose(site, expected, state['uid'], state['gid'])
    acceleration.verify(site)
    if json.loads(engine.file.read_text()) != canonical:
        raise DeploymentError('managed Compose configuration deviates from the deployment recipe')
    for image, spec in release['images'].items():
        if image == 'qwen' and site.get('runtime_source') == 'build':
            runtime = json.loads((root / 'state/runtime-build.json').read_text())
            if runtime['image'] != expected[image]:
                raise DeploymentError('rebuilt runtime identity differs from installation record')
        elif state['image_identities'][image] != spec['filesystem_config_sha256']:
            raise DeploymentError('installation image identity differs from signed release: ' + image)
    rows = json.loads(engine.compose('config', '--format', 'json'))
    for service, specification in rows['services'].items():
        image = 'backend' if service in {'api', 'worker', 'init'} else service
        if service == 'embedding' and acceleration.gpu_embedding(site):
            image = 'qwen'
        if specification['image'] != expected[image]:
            raise DeploymentError('compose image deviated from installed release: ' + service)
        if service == 'init':
            continue
        container = engine.compose('ps', '-q', service).strip()
        from .core import run
        inspection = json.loads(run(['docker', 'inspect', container]))[0]
        actual = inspection['Image']
        if actual != expected[image]:
            raise DeploymentError('running container image deviated: ' + service)
        from .image_identity import identity, inspect_image
        if identity(inspect_image(actual)) != state['image_identities'][image]:
            raise DeploymentError('running image filesystem/config identity deviated: ' + service)
        host = inspection['HostConfig']
        validate_inspection(site, inspection, service in {'api', 'qwen'} or (service == 'embedding' and acceleration.gpu_embedding(site)))
        if host.get('Privileged') or host['ReadonlyRootfs'] != canonical['services'][service]['read_only'] or host['NetworkMode'] == 'host':
            raise DeploymentError('running container isolation deviated: ' + service)
        spec = canonical['services'][service]
        networks.verify_container(engine, spec, inspection, canonical)
        if spec.get('user') and inspection['Config']['User'] != spec['user']:
            raise DeploymentError('running container user deviated: ' + service)
        if set(host.get('CapDrop') or []) != set(spec.get('cap_drop') or []):
            raise DeploymentError('running container capability restrictions deviated: ' + service)
        if 'no-new-privileges:true' not in (host.get('SecurityOpt') or []):
            raise DeploymentError('running container privilege escalation restriction deviated: ' + service)
        expected_binds = {volume['target']: (volume['source'], not volume.get('read_only', False))
                          for volume in spec.get('volumes', [])}
        expected_binds.update({'/run/secrets/' + name: (str(root / 'secrets' / name), False)
                               for name in spec.get('secrets', [])})
        for mount in inspection.get('Mounts', []):
            if mount['Type'] == 'bind':
                actual_bind = (mount['Source'], mount['RW'])
                if expected_binds.pop(mount['Destination'], None) != actual_bind:
                    raise DeploymentError('unexpected or changed container bind mount: ' + service)
        if expected_binds:
            raise DeploymentError('expected container bind mount missing: ' + service)
    engine.verify_gpu_access()
    networks.verify(engine)
    verify_models(release, site)
    result = acceptance(site, release, engine, fresh=fresh)
    state['acceptance'] = result
    state['last_verified'] = time.time()
    atomic_json(root / 'state/deployment.json', state)
    print(json.dumps(result, indent=2))
    return result


def start(site):
    state = installed(site)
    engine = Engine(site)
    with installation_lock(engine.root):
        assert_no_pending_network_migration(engine.root)
        release = verify_release(state['release_root'], engine.root / 'config/publisher.pem', state['publisher'])
        inference.configure(site, release)
        verify_models(release, site)
        start_stack(engine)
        engine.maintenance(False)


def stop(site, *, models_only=False):
    installed(site)
    engine = Engine(site)
    with installation_lock(engine.root):
        engine.drain()
        engine.management('disable-model')
        if models_only:
            engine.compose('stop', '--timeout', '90', 'qwen', 'embedding')
        else:
            engine.compose('stop', '--timeout', '120')


def update(site, release_root, trusted_key=None, *, rollback=False, backup_key=None, cold_network=False,
           model_migration=False):
    state = installed(site)
    root = Path(site['data_dir'])
    trusted_key = trusted_key or root / 'config/publisher.pem'
    release = verify_release(release_root, trusted_key, state['publisher'])
    if release['schema_epoch'] != state['schema_epoch']:
        raise DeploymentError('schema compatibility is not proven; use an approved migration/backup restore, not blind image rollback')
    changing_model = release['files']['models/model-manifest.json'] != state['model_manifest_sha256']
    if changing_model:
        if not model_migration:
            raise DeploymentError('model/index revision changed; validate separately, then explicitly use --model-migration')
        previous = verify_release(state['release_root'], trusted_key, state['publisher'])
        if (previous['_sha256'] != state['release_sha256']
                or previous['files']['models/model-manifest.json'] != state['model_manifest_sha256']):
            raise DeploymentError('installed release differs from recorded model migration source')
        from .model_migration import validate
        validate(previous, release)
        inference.configure(site, release, transition=True)
    if backup_key is None:
        raise DeploymentError('update/rollback requires an external --backup-key for a consistent encrypted backup')
    preflight(site, release)
    networks.preflight(site)
    with installation_lock(root):
        assert_no_pending_network_migration(root)
        from . import delivery
        if delivery.is_online(release):
            # Network failures must occur before draining a healthy installation.
            delivery.prepare_assets(release, site)
        engine = Engine(site)
        previous_site = (root / 'config/site.json').read_bytes()
        previous_compose = engine.file.read_bytes()
        desired_networks = networks.specifications(site)
        recipe_change = json.loads(previous_compose)['networks'] != desired_networks
        from .backup import make_backup
        if cold_network:
            if not recipe_change and not networks.needs_recreate(engine, {'networks': desired_networks}):
                raise DeploymentError('cold-network-migration is restricted to a network recipe change')
            from .backup import key_material
            key_material(backup_key)
            begin_network_migration(engine, json.loads(previous_site), json.loads(previous_compose), state, release['_sha256'])
            engine.maintenance(True)
            engine.compose('stop', '--timeout', '120')
            make_backup(json.loads(previous_site), state, backup_key, locked=True, cold=True)
        else:
            engine.drain()
            make_backup(json.loads(previous_site), state, backup_key, locked=True)
            engine.drain()
        preflight(site, release)
        release = cache_release(site, release, trusted_key)
        build = site.get('runtime_source') == 'build'
        images = prepare_images(release, skip={'qwen'} if build else (), site=site)
        if build:
            from .runtime import build_runtime
            images['qwen'] = build_runtime(release, site)
        prepare_models(release, site)
        fix_model_permissions(site)
        uid, gid = state['uid'], state['gid']
        document = render_compose(site, images, uid, gid)
        migrating = recipe_change or networks.needs_recreate(engine, document)
        atomic_json(root / 'state/previous-deployment.json', state)
        if migrating:
            begin_network_migration(engine, json.loads(previous_site), json.loads(previous_compose), state, release['_sha256'])
        try:
            if changing_model:
                # The old backend can only re-register its legacy model on rollback.
                engine.management('disable-model')
            if migrating:
                networks.teardown(engine)
            prepare_data(site, release)
            atomic_json(engine.file, document)
            start_stack(engine)
            engine.maintenance(False)
            result = acceptance(site, release, engine, fresh=True)
        except Exception:
            engine.maintenance(True)
            if migrating:
                networks.teardown(engine)
            (root / 'config/site.json').write_bytes(previous_site)
            engine.file.write_bytes(previous_compose)
            start_stack(Engine(json.loads(previous_site)))
            if migrating:
                finish_network_migration(root, status='recovered')
            # Keep submissions paused until the operator verifies the recovered version.
            raise
        new = dict(state)
        new.update(version=release['version'], tag=release['tag'], source=release['source'],
                   release_root=release['_root'], release_sha256=release['_sha256'], images=images,
                   model_manifest_sha256=release['files']['models/model-manifest.json'], acceptance=result,
                   site_sha256=digest(root / 'config/site.json'),
                   time=time.time(), operation='rollback' if rollback else 'update')
        from .image_identity import identity, inspect_image
        new['image_identities'] = {name: identity(inspect_image(image)) for name, image in images.items()}
        atomic_json(root / 'state/deployment.json', new)
        finish_network_migration(root)
        print('READY', site['web']['public_url'], release['version'])


def assert_no_pending_network_migration(root):
    path = Path(root) / 'state/network-migration.json'
    if path.exists() and json.loads(path.read_text()).get('status') == 'pending':
        raise DeploymentError('interrupted network migration; use the signed CLI network-recover command before retrying')


def begin_network_migration(engine, previous_site, previous_compose, state, target_release_sha256=None):
    if (not isinstance(previous_site, dict) or previous_site.get('instance') != engine.site['instance']
            or previous_site.get('data_dir') != str(engine.root)):
        raise DeploymentError('previous site ownership differs; refusing network migration')
    if previous_compose.get('name') != project_name(engine.site):
        raise DeploymentError('previous Compose project identity differs; refusing network teardown')
    if set(previous_compose.get('networks', {})) != set(networks.DEFAULT_SUBNETS):
        raise DeploymentError('unexpected previous project networks; refusing migration')
    atomic_json(engine.root / 'state/network-migration.json', {'status': 'pending',
        'instance': engine.site['instance'], 'root': str(engine.root), 'previous_site': previous_site,
        'previous_compose': previous_compose, 'previous_deployment': state,
        'target_release_sha256': target_release_sha256, 'time': time.time()})


def finish_network_migration(root, status='completed'):
    path = Path(root) / 'state/network-migration.json'
    if path.exists():
        value = json.loads(path.read_text())
        value.update(status=status, finished=time.time())
        atomic_json(path, value)


def recover_network(site):
    root = Path(site['data_dir'])
    with installation_lock(root):
        path = root / 'state/network-migration.json'
        journal = json.loads(path.read_text())
        if (journal.get('status') != 'pending' or journal['instance'] != site['instance']
                or journal['root'] != str(root)):
            raise DeploymentError('no matching pending network migration')
        old_site = journal['previous_site']
        if old_site['data_dir'] != str(root) or old_site['instance'] != site['instance']:
            raise DeploymentError('network recovery ownership mismatch')
        previous = journal['previous_deployment']
        if (root / 'state/deployment.json').exists():
            current = installed(site)
            if current['release_sha256'] == journal.get('target_release_sha256'):
                # State committed after acceptance, but journal completion was interrupted.
                from .core import load_site
                committed_site = load_site(root / 'config/site.json')
                verify_release(current['release_root'], root / 'config/publisher.pem', current['publisher'])
                current_engine = Engine(committed_site)
                if json.loads(current_engine.file.read_text()) != render_compose(committed_site, current['images'], current['uid'], current['gid']):
                    raise DeploymentError('committed deployment recipe differs; refusing transaction finalization')
                current_engine.maintenance(True)
                start_stack(current_engine)
                verify(committed_site)
                finish_network_migration(root)
                print('Validated committed target and completed interrupted network transaction; submissions remain paused.')
                return
        if previous:
            current = installed(site)
            if current['release_sha256'] != previous['release_sha256']:
                raise DeploymentError('deployment already changed; refusing stale recovery')
            verify_release(previous['release_root'], root / 'config/publisher.pem', previous['publisher'])
        engine = Engine(site)
        engine.maintenance(True)
        networks.teardown(engine)
        atomic_json(root / 'config/site.json', old_site)
        atomic_json(engine.file, journal['previous_compose'])
        start_stack(Engine(old_site))
        finish_network_migration(root, status='recovered')
        print('Recovered previous containers/networks; submissions remain paused until verification.')


def publish(site, *, mode, bind=None, port=None, public_url=None):
    from .core import load_site
    candidate = json.loads(json.dumps(site))
    candidate['web'].update({key: value for key, value in {'exposure': mode, 'bind': bind, 'port': port, 'public_url': public_url}.items() if value is not None})
    root = Path(site['data_dir'])
    temporary = root / 'state/proposed-site.json'
    atomic_json(temporary, candidate)
    candidate = load_site(temporary)
    if candidate['web']['bind'] != site['web']['bind'] or candidate['web']['port'] != site['web']['port']:
        check = dict(candidate)
        # preflight's port probe is skipped only for the existing installation; explicitly bind a changed endpoint.
        import socket
        with socket.socket() as sock:
            sock.bind((candidate['web']['bind'], int(candidate['web']['port'])))
    state = installed(site)
    engine = Engine(site)
    with installation_lock(root):
        assert_no_pending_network_migration(root)
        if networks.needs_recreate(engine, render_compose(candidate, state['images'], state['uid'], state['gid'])):
            raise DeploymentError('network migration requires signed update/install; publish does not rebuild project networks')
        engine.drain()
        uid, gid = state['uid'], state['gid']
        atomic_json(engine.file, render_compose(candidate, state['images'], uid, gid))
        engine.compose('up', '-d', 'api', 'worker', 'frontend')
        engine.wait(['api', 'worker', 'frontend'])
        atomic_json(root / 'config/site.json', candidate)
        state['site_sha256'] = digest(root / 'config/site.json')
        atomic_json(root / 'state/deployment.json', state)
        engine.maintenance(False)
    print('Application bound to', candidate['web']['bind'], candidate['web']['port'])
    if candidate['web']['exposure'] == 'gateway':
        from .engine import gateway_snippet
        snippet = root / 'config/gateway-upstream.conf'
        snippet.write_text(gateway_snippet(candidate))
        snippet.chmod(0o600)
        print('Review-only same-host Nginx snippet:', snippet)
        print('After gateway configuration, verify login, upload, report export and long-request timeouts from the actual client.')
    print('Gateway/DNS/TLS/NAT changes are external and are not automatically applied or declared verified.')
