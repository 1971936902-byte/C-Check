"""Configuration and signed resources for the independent managed SSH terminal."""
import hashlib
import contextlib
import importlib.resources
import ipaddress
import json
import os
from pathlib import Path
import re
from types import ModuleType
from types import SimpleNamespace
import subprocess
from urllib.parse import urlsplit

from .core import DeploymentError, atomic_json, checked_path, digest, run, verify_release
from .terminal_config import validate_url

RESOURCES = ('terminal.py', 'compose.yaml', 'nginx.conf', 'images.lock.json')
DEFAULT_SUBNETS = {'data': '172.31.240.0/26', 'ssh': '172.31.240.64/26', 'web': '172.31.240.128/26'}
IMAGE_KEYS = {'GUACAMOLE_IMAGE', 'GUACD_IMAGE', 'POSTGRES_IMAGE', 'NGINX_IMAGE'}


def resource(name):
    if name not in RESOURCES:
        raise DeploymentError('unapproved terminal resource')
    bundled = importlib.resources.files('ccheck').joinpath('terminal_runtime', name)
    if bundled.is_file():
        return bundled.read_bytes()
    source = Path(__file__).resolve().parents[3] / 'terminal' / name
    if Path(__file__).is_file() and source.is_file():
        return source.read_bytes()
    raise DeploymentError('terminal runtime is missing; use a release built with --with-terminal')


def hashes():
    return {name: hashlib.sha256(resource(name)).hexdigest() for name in RESOURCES}


def library():
    module = ModuleType('ccheck_terminal_runtime')
    module.__file__ = '<bundled-terminal/terminal.py>'
    exec(compile(resource('terminal.py'), module.__file__, 'exec'), module.__dict__)
    return module


def private_directory(value, site):
    if not isinstance(value, str):
        raise DeploymentError('terminal.data_dir must be a safe absolute dedicated directory')
    check = value
    if os.name == 'nt':
        check = check.removeprefix(Path(value).drive).replace('\\', '/')
    if any(c.isspace() or c in '\x00,$:\\#\"\'' for c in check):
        raise DeploymentError('terminal.data_dir must be a safe absolute dedicated directory')
    path = Path(value).expanduser()
    if (not path.is_absolute() or len(path.parts) < 3 or path == Path.home()
            or any(parent.is_symlink() for parent in [path, *path.parents])):
        raise DeploymentError('terminal.data_dir must be absolute, dedicated and not symlinked')
    application = Path(site['data_dir']).expanduser().resolve()
    path = path.resolve()
    if path.is_relative_to(application) or application.is_relative_to(path):
        raise DeploymentError('terminal storage must be separate from the C-Check installation')
    return str(path)


def settings(site):
    value = site.get('terminal', {})
    allowed = {'enabled', 'data_dir', 'bind', 'port', 'public_url', 'tls', 'subnets',
               'image_policy', 'image_sources', 'connections'}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get('enabled', False)) is not bool:
        raise DeploymentError('invalid terminal configuration keys or enabled value')
    if not value or value == {'enabled': False}:
        return {'enabled': False}
    if value.get('enabled') and site.get('web', {}).get('terminal_config'):
        raise DeploymentError('use managed terminal or web.terminal_config, not both')
    application = Path(site['data_dir']).expanduser()
    result = dict(value)
    result.setdefault('enabled', False)
    result['data_dir'] = private_directory(value.get('data_dir', str(application.with_name(application.name + '-terminal'))), site)
    result.setdefault('bind', '127.0.0.1')
    result.setdefault('port', 8803)
    try:
        address = ipaddress.IPv4Address(result['bind'])
    except (ValueError, TypeError) as exc:
        raise DeploymentError('terminal.bind must be an IPv4 address') from exc
    if type(result['port']) is not int or not 1024 <= result['port'] <= 65535:
        raise DeploymentError('terminal.port must be an integer between 1024 and 65535')
    if address.is_unspecified and not value.get('public_url'):
        raise DeploymentError('wildcard terminal bind requires an explicit public_url')
    default_host = 'localhost' if str(address) == '127.0.0.1' else str(address)
    result.setdefault('public_url', f'https://{default_host}:{result["port"]}/guacamole/')
    try:
        validate_url(result['public_url'])
        if not result['public_url'] or urlsplit(result['public_url']).path != '/guacamole/':
            raise ValueError('terminal URL must end with /guacamole/')
        hostname = urlsplit(result['public_url']).hostname
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', hostname or ''):
            raise ValueError('terminal URL requires an IP address or DNS hostname')
    except ValueError as exc:
        raise DeploymentError('invalid terminal.public_url: ' + str(exc)) from exc
    tls = result.setdefault('tls', {})
    if not isinstance(tls, dict) or (tls and set(tls) != {'cert_file', 'key_file'}):
        raise DeploymentError('terminal.tls requires exactly cert_file and key_file')
    for path in tls.values():
        if not isinstance(path, str) or not Path(path).is_absolute() or any(c in path for c in '\x00\r\n'):
            raise DeploymentError('terminal TLS paths must be absolute')
    if result['enabled'] and not address.is_loopback and not tls:
        raise DeploymentError('non-loopback terminal requires operator-provided TLS files')
    subnets = result.setdefault('subnets', {})
    if not isinstance(subnets, dict) or set(subnets) - set(DEFAULT_SUBNETS):
        raise DeploymentError('terminal.subnets accepts only data, ssh and web')
    result['subnets'] = dict(DEFAULT_SUBNETS, **subnets)
    try:
        parsed = [ipaddress.IPv4Network(cidr, strict=True) for cidr in result['subnets'].values()]
        private = [ipaddress.IPv4Network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
        if any(not any(net.subnet_of(block) for block in private) or not 16 <= net.prefixlen <= 28 for net in parsed):
            raise ValueError('use RFC1918 /16 through /28 IPv4 networks')
        if any(left.overlaps(right) for index, left in enumerate(parsed) for right in parsed[index + 1:]):
            raise ValueError('terminal networks overlap')
        from .networks import subnets as application_subnets
        if any(net.overlaps(ipaddress.IPv4Network(cidr)) for net in parsed for cidr in application_subnets(site).values()):
            raise ValueError('terminal networks overlap C-Check networks')
    except (ValueError, TypeError) as exc:
        raise DeploymentError('invalid terminal subnets: ' + str(exc)) from exc
    result.setdefault('image_policy', 'online')
    if result['image_policy'] not in {'online', 'offline'}:
        raise DeploymentError('terminal.image_policy must be online or offline')
    sources = result.setdefault('image_sources', {})
    if not isinstance(sources, dict) or set(sources) - IMAGE_KEYS:
        raise DeploymentError('unknown terminal image source')
    for reference in sources.values():
        if not isinstance(reference, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}', reference):
            raise DeploymentError('terminal image sources must be immutable registry digests')
    connections = result.setdefault('connections', [])
    if not isinstance(connections, list) or len(connections) > 64:
        raise DeploymentError('terminal.connections must be a list of at most 64 targets')
    if connections:
        implementation = library()
        names = set()
        for connection in connections:
            if not isinstance(connection, dict):
                raise DeploymentError('SSH connection must be an object')
            if (type(connection.get('port')) is not int or any(not isinstance(connection.get(key), str)
                    for key in ('name', 'hostname', 'username', 'host-key'))):
                raise DeploymentError('SSH targets require string fields and an integer port')
            if (not connection['username'].strip() or len(connection['username']) > 128
                    or any(ord(c) < 32 for c in connection['username'])):
                raise DeploymentError('invalid SSH username')
            try:
                implementation.connection_sql(connection)
            except (ValueError, TypeError, KeyError) as exc:
                raise DeploymentError('invalid SSH connection: ' + str(exc)) from exc
            name = connection['name']
            if (not isinstance(name, str) or not name.strip() or len(name) > 120
                    or name in names or any(ord(c) < 32 for c in name)):
                raise DeploymentError('SSH target names must be nonempty, unique and at most 120 characters')
            names.add(name)
    return result


def project_name(site, config):
    suffix = hashlib.sha256(config['data_dir'].encode()).hexdigest()[:8]
    return 'ccheck-terminal-' + site['instance'] + '-' + suffix


@contextlib.contextmanager
def locked_root(site, config, *, create=False):
    if os.name != 'posix':
        raise DeploymentError('managed terminal deployment requires Linux')
    import fcntl
    root = Path(config['data_dir'])
    owner = root / 'owner.json'
    if root.exists():
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise DeploymentError('terminal directory must belong to the invoking user and have mode 0700')
        if not owner.exists() and any(root.iterdir()):
            raise DeploymentError('refusing to take over an unowned terminal directory')
    elif create:
        root.mkdir(mode=0o700, parents=True)
    else:
        raise DeploymentError('terminal is not installed; run terminal-install first')
    with checked_path(root, '.terminal.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError('another operation owns the terminal lock') from exc
        record = None
        if owner.exists():
            record = read_private(root, 'owner.json')
            if (record.get('instance') != site['instance']
                    or record.get('project') != project_name(site, config)):
                raise DeploymentError('terminal ownership does not match this instance')
        yield root, record


def read_private(root, name):
    path = checked_path(root, name)
    if (not path.is_file() or path.stat().st_size > 4 * 1024 * 1024
            or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()):
        raise DeploymentError('terminal metadata must be an owner-only regular file: ' + name)
    return json.loads(path.read_text(encoding='utf-8'))


def package_id(mapping):
    if (not isinstance(mapping, dict) or set(mapping) != set(RESOURCES)
            or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                   for value in mapping.values())):
        raise DeploymentError('invalid signed terminal runtime hashes')
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()


def cached_runtime(root, record):
    identifier = record['release_sha256']
    if not re.fullmatch('[0-9a-f]{64}', identifier):
        raise DeploymentError('invalid cached terminal release identity')
    directory = checked_path(root, 'releases/' + identifier)
    metadata = checked_path(directory, 'release.json')
    signature = checked_path(directory, 'release.sig')
    key = checked_path(directory, 'publisher.pem')
    if (metadata.stat().st_size > 4 * 1024 * 1024 or signature.stat().st_size != 64
            or digest(metadata) != identifier or digest(key) != record['publisher']):
        raise DeploymentError('cached terminal trust metadata was modified')
    run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(key), '-rawin',
         '-in', str(metadata), '-sigfile', str(signature)])
    release = json.loads(metadata.read_text(encoding='utf-8'))
    mapping = release.get('terminal_runtime')
    identifier = package_id(mapping)
    if record['runtime'] != identifier:
        raise DeploymentError('terminal runtime does not match signed metadata')
    directory = checked_path(root, 'runtime/' + identifier)
    for name, expected in mapping.items():
        if digest(checked_path(directory, name)) != expected:
            raise DeploymentError('cached terminal runtime was modified: ' + name)
    module = ModuleType('ccheck_managed_terminal')
    module.__file__ = str(directory / 'terminal.py')
    exec(compile((directory / 'terminal.py').read_bytes(), module.__file__, 'exec'), module.__dict__)
    module.ROOT = directory
    module.STATE = root / 'state'
    module.LOCK = directory / 'images.lock.json'
    module.PROJECT = record['project']
    history = record.get('runtime_history', [])
    if any(not re.fullmatch('[0-9a-f]{64}', value) for value in history):
        raise DeploymentError('invalid terminal runtime history')
    module.CONFIG_FILES = {str(root / 'runtime' / item / 'compose.yaml') for item in [identifier, *history]}
    return module


def configure_library(module, config):
    module.IMAGE_POLICY = config['image_policy']
    module.IMAGE_OVERRIDES = config['image_sources']
    module.PUBLIC_URL = config['public_url']
    tls = config['tls']
    module.TLS_CERT = Path(tls['cert_file']) if tls else None
    module.TLS_KEY = Path(tls['key_file']) if tls else None


def validate_state(module, record):
    state = module.STATE
    if not state.is_dir() or state.is_symlink() or state.stat().st_mode & 0o077:
        raise DeploymentError('terminal state is missing or unsafe; restore its backup, do not reinitialize')
    for name in ('compose.env', 'init.sql', 'database-password', 'admin-credentials.json', 'tls.crt', 'tls.key'):
        path = checked_path(state, name)
        if not path.is_file():
            raise DeploymentError('terminal state is incomplete; preserve it and restore from backup')
    if record.get('started_once'):
        info = json.loads(module.capture(['docker', 'volume', 'inspect', module.PROJECT + '_database']))[0]
        if (info.get('Labels') or {}).get('com.docker.compose.project') != module.PROJECT:
            raise DeploymentError('terminal database volume belongs to a different project')


def install(site, release_root, trusted_key):
    config = settings(site)
    if not config['enabled']:
        raise DeploymentError('set terminal.enabled=true before terminal-install')
    # Full package verification precedes any cache write or container action.
    release = verify_release(release_root, trusted_key)
    mapping = release.get('terminal_runtime')
    identifier = package_id(mapping)
    if mapping != hashes():
        raise DeploymentError('run the installer supplied with this signed terminal release')
    with locked_root(site, config, create=True) as (root, old):
        if old and old['publisher'] != release['_publisher']:
            raise DeploymentError('terminal publisher differs from installed trust anchor')
        if old:
            previous = cached_runtime(root, old)
            if previous.STATE.exists() or old.get('started_once') or old.get('stage') != 'installing':
                validate_state(previous, old)
            if previous.LOCK.read_bytes() != resource('images.lock.json'):
                raise DeploymentError('terminal image changes require an explicit database upgrade plan')
            if old['settings'] != config:
                raise DeploymentError('apply terminal configuration with terminal-configure before upgrading its runtime')
        record = dict(old or {}, instance=site['instance'], project=project_name(site, config),
                      publisher=release['_publisher'], release_sha256=release['_sha256'],
                      runtime=identifier, settings=config, stage='installing')
        record['runtime_history'] = sorted(set((old or {}).get('runtime_history', []) + ([old['runtime']] if old else [])))
        package = checked_path(root, 'runtime/' + identifier)
        package.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in RESOURCES:
            target = checked_path(package, name)
            if target.exists():
                if digest(target) != mapping[name]:
                    raise DeploymentError('refusing to overwrite modified terminal resource')
            else:
                with target.open('xb') as stream:
                    stream.write(resource(name))
                target.chmod(0o444)
        cache = checked_path(root, 'releases/' + release['_sha256'])
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, source in (('release.json', Path(release_root) / 'release.json'),
                             ('release.sig', Path(release_root) / 'release.sig'), ('publisher.pem', Path(trusted_key))):
            target = checked_path(cache, name)
            data = source.read_bytes()
            if target.exists() and target.read_bytes() != data:
                raise DeploymentError('cached terminal signature metadata differs')
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(data)
                target.chmod(0o600)
        atomic_json(root / 'owner.json', record)
        module = cached_runtime(root, record)
        configure_library(module, config)
        if not module.STATE.exists():
            module.initialize(SimpleNamespace(bind=config['bind'], port=config['port'],
                data_subnet=config['subnets']['data'], ssh_subnet=config['subnets']['ssh'], web_subnet=config['subnets']['web']))
        validate_state(module, record)
        record['stage'] = 'installed'
        atomic_json(root / 'owner.json', record)
        prepare_public_config(site)
        print('Terminal installed; start with terminal-start. Credentials: ' + str(module.STATE / 'admin-credentials.json'))


def operate(site, action):
    config = settings(site)
    if config == {'enabled': False}:
        # Stop/status remain available after the operator disables the feature.
        site = dict(site, terminal={'enabled': False, 'data_dir': str(Path(site['data_dir']).with_name(
            Path(site['data_dir']).name + '-terminal'))})
        config = settings(site)
    with locked_root(site, config) as (root, record):
        if record is None:
            raise DeploymentError('terminal is not installed')
        module = cached_runtime(root, record)
        validate_state(module, record)
        if action == 'stop':
            module.compose('stop', '--timeout', '30')
            print('Terminal stopped; database and credentials preserved. C-Check services were not stopped.')
            return
        if action == 'status':
            module.compose('ps', '-a')
            print(json.dumps({'project': record['project'], 'stage': record['stage'],
                'url': record['settings']['public_url'], 'data_dir': str(root)}, indent=2))
            return
        if action == 'logs':
            module.compose('logs', '--tail', '100')
            return
        if not config['enabled']:
            raise DeploymentError('terminal is disabled')
        if record['stage'] not in {'installed', 'ready', 'starting'}:
            raise DeploymentError('terminal operation is incomplete; retry terminal-install or terminal-configure')
        if config != record['settings']:
            raise DeploymentError('terminal configuration changed; run terminal-configure first')
        if action == 'check':
            module.check()
            return
        if action != 'start':
            raise DeploymentError('unknown terminal action')
        configure_library(module, config)
        record['stage'] = 'starting'
        atomic_json(root / 'owner.json', record)
        start_runtime(module, record)
        record['stage'] = 'ready'
        atomic_json(root / 'owner.json', record)


def start_runtime(module, record):
    locks = json.loads(module.LOCK.read_text())
    for key, item in locks.items():
        module.runtime_image(key, item)
    module.compose('up', '-d', '--wait', '--wait-timeout', '180')
    # Mark volume creation before HTTP/SQL checks so a later missing volume cannot reset accounts.
    record['started_once'] = True
    atomic_json(module.STATE.parent / 'owner.json', record)
    module.check()
    targets = record['settings']['connections']
    sql = module.managed_connections_sql(targets, record.get('managed_names', []), module.PROJECT)
    module.compose('exec', '-T', 'database', 'psql', '-U', 'guacamole', '-d', 'guacamole',
                   '-v', 'ON_ERROR_STOP=1', input=sql, capture_output=True)
    record['managed_names'] = [item['name'] for item in targets]


def configure(site):
    config = settings(site)
    if not config['enabled']:
        raise DeploymentError('enable the terminal before configuring it; terminal-stop preserves disabled deployments')
    with locked_root(site, config) as (root, record):
        if record is None:
            raise DeploymentError('terminal is not installed')
        module = cached_runtime(root, record)
        validate_state(module, record)
        configure_library(module, config)
        module.prepare_tls(urlsplit(config['public_url']).hostname)
        if not config['tls'] and config['public_url'] != record['settings']['public_url']:
            raise DeploymentError('a changed terminal URL requires matching operator-provided TLS files')
        locks = json.loads(module.LOCK.read_text())
        images = {key: module.runtime_image(key, item) for key, item in locks.items()}
        # Network migration is intentionally separate: never remove unknown bridges or routes.
        if config['subnets'] != record['settings']['subnets']:
            raise DeploymentError('changing terminal subnets requires a planned terminal network migration')
        record['pending_settings'] = config
        record['stage'] = 'configuring'
        atomic_json(root / 'owner.json', record)
        module.compose('stop', '--timeout', '30')
        environment = dict(line.split('=', 1) for line in (module.STATE / 'compose.env').read_text().splitlines())
        environment.update(images, TERMINAL_BIND=config['bind'], TERMINAL_PORT=str(config['port']),
            TERMINAL_TLS_HOST=urlsplit(config['public_url']).hostname, TERMINAL_PUBLIC_URL=config['public_url'])
        for name, source in (('tls.crt', module.TLS_CERT), ('tls.key', module.TLS_KEY)):
            if source is not None:
                destination = checked_path(module.STATE, name)
                temporary = checked_path(module.STATE, '.' + name + '.new')
                with temporary.open('xb') as stream:
                    stream.write(source.read_bytes())
                temporary.chmod(0o444)
                os.replace(temporary, destination)
        destination = checked_path(module.STATE, 'compose.env')
        temporary = checked_path(module.STATE, '.compose.env.new')
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(''.join(f'{key}={value}\n' for key, value in environment.items()))
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        record['settings'] = config
        start_runtime(module, record)
        record.pop('pending_settings', None)
        record['stage'] = 'ready'
        atomic_json(root / 'owner.json', record)
        prepare_public_config(site)


def command(site, action, *, release_root=None, trusted_key=None):
    try:
        if action == 'install':
            install(site, release_root, trusted_key)
        elif action == 'configure':
            configure(site)
        else:
            operate(site, action)
    except subprocess.CalledProcessError as exc:
        raise DeploymentError('terminal container command failed (exit ' + str(exc.returncode)
                              + '); state was preserved; inspect terminal-status and terminal-logs') from None
    except subprocess.TimeoutExpired:
        raise DeploymentError('terminal command timed out; state was preserved; inspect terminal-status before retrying') from None


def public_config_path(site):
    return checked_path(site['data_dir'], 'config/terminal-public.json')


def prepare_public_config(site):
    if 'terminal' not in site or site.get('web', {}).get('terminal_config'):
        return
    config = settings(site)
    root = Path(site['data_dir'])
    owner = checked_path(root, 'state/owner.json')
    if not owner.is_file():
        return
    if json.loads(owner.read_text()).get('instance') != site['instance']:
        raise DeploymentError('cannot publish terminal link into an unowned C-Check installation')
    data = json.dumps({'url': config['public_url'] if config['enabled'] else ''}) + '\n'
    path = public_config_path(site)
    # Preserve the inode of the live read-only file bind mount. This file contains only a public URL.
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o644)
