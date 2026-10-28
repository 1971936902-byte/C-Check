#!/usr/bin/env python3
"""Manage only the independent ccheck-terminal Compose project."""
import argparse
import gzip
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import shutil
import subprocess
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
LOCK = ROOT / 'images.lock.json'
PROJECT = 'ccheck-terminal'
IMAGE_POLICY = 'online'
IMAGE_OVERRIDES = {}
TLS_CERT = TLS_KEY = None
PUBLIC_URL = None
CONFIG_FILES = None
IMAGES = {'GUACAMOLE_IMAGE': 'guacamole/guacamole:1.6.0',
          'GUACD_IMAGE': 'guacamole/guacd:1.6.0',
          'POSTGRES_IMAGE': 'postgres:17-alpine',
          'NGINX_IMAGE': 'public.ecr.aws/docker/library/nginx:1.28-alpine'}


def run(args, **kwargs):
    kwargs.setdefault('timeout', 600)
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(args):
    return run(args, capture_output=True).stdout


def compose(*args, **kwargs):
    ids = capture(['docker', 'ps', '-a', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    for identifier in ids:
        info = json.loads(capture(['docker', 'inspect', identifier]))[0]
        labels = info['Config'].get('Labels') or {}
        allowed = CONFIG_FILES or {str(ROOT / 'compose.yaml')}
        if labels.get('com.docker.compose.project.config_files') not in allowed:
            raise ValueError('Terminal project belongs to a different configuration; refusing cross-project action.')
    return run(['docker', 'compose', '--project-name', PROJECT,
                '--project-directory', str(ROOT), '--env-file', str(STATE / 'compose.env'),
                '-f', str(ROOT / 'compose.yaml'), *args], **kwargs)


def write(path, value, mode=0o600):
    # Never follow a preexisting path or silently replace credentials.
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(value)
    path.chmod(mode)


def password_hash(password, salt):
    return hashlib.sha256((password + salt.hex().upper()).encode()).hexdigest()


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def resolve_lock():
    if LOCK.exists():
        raise ValueError('Lock exists; review and archive it explicitly before updating versions.')
    images = {}
    for key, tag in IMAGES.items():
        info = ensure_image(tag)
        digest = next(item for item in info['RepoDigests'] if item.startswith(tag.split(':')[0] + '@'))
        images[key] = {'source': tag, 'image': digest, 'id': info['Id'],
                       'architecture': info['Architecture']}
    write(LOCK, json.dumps(images, indent=2) + '\n', 0o644)


def ensure_image(reference):
    try:
        return json.loads(capture(['docker', 'image', 'inspect', reference]))[0]
    except subprocess.CalledProcessError:
        if IMAGE_POLICY == 'offline':
            raise ValueError('Offline terminal image is missing: ' + reference) from None
        run(['docker', 'pull', reference])
        return json.loads(capture(['docker', 'image', 'inspect', reference]))[0]


def runtime_image(key, record):
    reference = IMAGE_OVERRIDES.get(key, record['image'])
    if (not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}', reference)
            or reference.rsplit('@', 1)[1] != record['image'].rsplit('@', 1)[1]):
        raise ValueError('Terminal image mirrors must preserve the locked digest.')
    metadata = ensure_image(reference)
    if metadata['Architecture'] != 'amd64' or metadata.get('Os', 'linux') != 'linux':
        raise ValueError('Terminal images require Linux/amd64.')
    return reference


def prepare_tls(host):
    if (TLS_CERT is None) != (TLS_KEY is None):
        raise ValueError('TLS certificate and private key must be supplied together.')
    if TLS_CERT is None:
        return
    for path in (TLS_CERT, TLS_KEY):
        if not path.is_absolute() or any(parent.is_symlink() for parent in [path, *path.parents]) or not path.is_file():
            raise ValueError('TLS inputs must be absolute regular files, not symlinks.')
    if TLS_KEY.stat().st_mode & 0o077 or TLS_KEY.stat().st_uid != os.getuid():
        raise ValueError('TLS private key must be owner-only.')
    run(['openssl', 'x509', '-in', str(TLS_CERT), '-noout', '-checkend', '0'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ipaddress.ip_address(host)
        option = '-checkip'
    except ValueError:
        option = '-checkhost'
    run(['openssl', 'x509', '-in', str(TLS_CERT), '-noout', option, host],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    public = capture(['openssl', 'x509', '-in', str(TLS_CERT), '-pubkey', '-noout'])
    private_public = capture(['openssl', 'pkey', '-in', str(TLS_KEY), '-pubout'])
    if public != private_public:
        raise ValueError('TLS certificate and private key do not match.')


def validate_networks(networks, occupied):
    parsed = [ipaddress.ip_network(value, strict=True) for value in networks]
    if any(net.version != 4 or not net.is_private for net in parsed):
        raise ValueError('Use private IPv4 subnets.')
    for index, net in enumerate(parsed):
        if any(net.overlaps(other) for other in parsed[:index]):
            raise ValueError('Terminal subnets overlap one another.')
        if any(net.overlaps(ipaddress.ip_network(value, strict=False)) for value in occupied):
            raise ValueError('Terminal subnet conflicts with an existing route/network: ' + str(net))


def initialize(args):
    if STATE.exists():
        raise ValueError('state already exists. Preserve it; do not reinitialize an existing deployment.')
    if capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT]).strip():
        raise ValueError('Existing project volumes found. Restore state from backup; do not reset credentials.')
    locks = json.loads(LOCK.read_text())
    if set(locks) != set(IMAGES):
        raise ValueError('Incomplete image lock.')
    images = {}
    for key, item in locks.items():
        if not re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', item['image']):
            raise ValueError('Images must be pinned by sha256 digest.')
        images[key] = runtime_image(key, item)
    networks = [args.data_subnet, args.ssh_subnet, args.web_subnet]
    ids = capture(['docker', 'network', 'ls', '-q']).split()
    existing = json.loads(capture(['docker', 'network', 'inspect', *ids])) if ids else []
    occupied = [c['Subnet'] for net in existing for c in (net['IPAM'].get('Config') or [])
                if c.get('Subnet') and ':' not in c['Subnet']]
    routes = json.loads(capture(['ip', '-j', '-4', 'route']))
    occupied += [r['dst'] for r in routes if r.get('dst') not in (None, 'default')]
    validate_networks(networks, occupied)
    if not 1024 <= args.port <= 65535:
        raise ValueError('Port must be between 1024 and 65535.')
    bind_address = ipaddress.ip_address(args.bind)
    if bind_address.version != 4:
        raise ValueError('Terminal requires an IPv4 bind address.')
    if not bind_address.is_loopback and (TLS_CERT is None or TLS_KEY is None):
        raise ValueError('Non-loopback terminal binding requires an operator-provided TLS certificate and key.')
    if PUBLIC_URL is None and str(bind_address) != '127.0.0.1':
        raise ValueError('A custom bind address also requires a configured public HTTPS URL.')
    host = urlsplit(PUBLIC_URL).hostname if PUBLIC_URL else 'localhost'
    prepare_tls(host)
    with socket.socket() as listener:
        listener.bind((args.bind, args.port))
    schema = capture(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                      '--security-opt', 'no-new-privileges:true', '--entrypoint',
                      '/opt/guacamole/bin/initdb.sh', images['GUACAMOLE_IMAGE'], '--postgresql'])
    password = secrets.token_urlsafe(30)
    salt = secrets.token_bytes(32)
    schema += ("\nBEGIN;\nUPDATE guacamole_user SET password_hash = decode('" + password_hash(password, salt)
               + "', 'hex'), password_salt = decode('" + salt.hex()
               + "', 'hex'), password_date = CURRENT_TIMESTAMP WHERE entity_id = "
               "(SELECT entity_id FROM guacamole_entity WHERE name='guacadmin' AND type='USER');\n"
               "UPDATE guacamole_entity SET name='terminal-admin' WHERE name='guacadmin' AND type='USER';\nCOMMIT;\n")
    STATE.mkdir(mode=0o700)
    write(STATE / 'init.sql', schema, 0o444)
    # The host directory is root-only; mounted files must also be readable by image service UIDs.
    write(STATE / 'database-password', secrets.token_urlsafe(36), 0o444)
    write(STATE / 'admin-credentials.json', json.dumps({'username': 'terminal-admin', 'password': password}) + '\n')
    environment = images
    environment.update(TERMINAL_BIND=args.bind, TERMINAL_PORT=str(args.port),
                       TERMINAL_DATA_SUBNET=args.data_subnet, TERMINAL_SSH_SUBNET=args.ssh_subnet,
                       TERMINAL_WEB_SUBNET=args.web_subnet, TERMINAL_STATE_DIR=str(STATE),
                       TERMINAL_TLS_HOST=host, TERMINAL_PUBLIC_URL=PUBLIC_URL or f'https://localhost:{args.port}/guacamole/')
    write(STATE / 'compose.env', ''.join(f'{key}={value}\n' for key, value in environment.items()))
    if TLS_CERT is not None:
        shutil.copyfile(TLS_CERT, STATE / 'tls.crt')
        shutil.copyfile(TLS_KEY, STATE / 'tls.key')
    else:
        try:
            ipaddress.ip_address(host)
            san = 'IP:' + host
        except ValueError:
            san = 'DNS:' + host
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', '90',
             '-keyout', str(STATE / 'tls.key'), '-out', str(STATE / 'tls.crt'),
             '-subj', '/CN=' + host, '-addext', 'subjectAltName=' + san],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    (STATE / 'tls.key').chmod(0o444)
    (STATE / 'tls.crt').chmod(0o444)
    compose('config', '--quiet')
    print('Initialized. Credentials: ' + str(STATE / 'admin-credentials.json'))
    print('Test certificate fingerprint:')
    run(['openssl', 'x509', '-in', str(STATE / 'tls.crt'), '-noout', '-fingerprint', '-sha256'])


def check():
    environment = dict(line.split('=', 1) for line in (STATE / 'compose.env').read_text().splitlines())
    port = int(environment['TERMINAL_PORT'])
    context = ssl.create_default_context(cafile=str(STATE / 'tls.crt'))
    # The operator-supplied certificate is an explicit local trust anchor, including private CA leaves.
    context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0)
    host = environment.get('TERMINAL_TLS_HOST', 'localhost')
    address = environment.get('TERMINAL_BIND', '127.0.0.1')
    if address == '0.0.0.0':
        address = '127.0.0.1'
    url = environment.get('TERMINAL_PUBLIC_URL', f'https://localhost:{port}/guacamole/')
    for _ in range(90):
        try:
            with socket.create_connection((address, port), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname=host) as secured:
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    connection.sock = secured
                    connection.request('GET', '/guacamole/', headers={'Host': host})
                    with connection.getresponse() as response:
                        if response.status == 200:
                            print('HTTPS ready: ' + url)
                            return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(2)
    raise RuntimeError('HTTPS readiness timeout. Inspect only this project with terminal.py logs.')


def connection_sql(config):
    allowed = {'name', 'hostname', 'port', 'username', 'host-key'}
    if set(config) != allowed or not all(isinstance(v, (str, int)) for v in config.values()):
        raise ValueError('Connection requires exactly name, hostname, port, username, host-key; no stored passwords.')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', str(config['hostname'])):
        raise ValueError('Invalid SSH hostname.')
    if not 1 <= int(config['port']) <= 65535:
        raise ValueError('Invalid SSH port.')
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+=*', config['host-key']):
        raise ValueError('A verified OpenSSH public host key (type + base64) is required.')
    name = quote(config['name'])
    delimiter = '$guard_' + secrets.token_hex(16) + '$'
    sql = ('BEGIN;\nLOCK TABLE guacamole_connection IN SHARE ROW EXCLUSIVE MODE;\nDO ' + delimiter
           + ' BEGIN IF EXISTS (SELECT 1 FROM guacamole_connection WHERE connection_name='
           + name + ") THEN RAISE EXCEPTION 'A connection with this name already exists'; END IF; END " + delimiter + ';\n')
    sql += f"INSERT INTO guacamole_connection (connection_name, protocol) VALUES ({name}, 'ssh');\n"
    parameters = connection_parameters(config)
    for key, value in parameters.items():
        sql += ('INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value) '
                f"VALUES (currval('guacamole_connection_connection_id_seq'), {quote(key)}, {quote(value)});\n")
    return sql + 'COMMIT;\n'


def connection_parameters(config):
    parameters = {k: str(v) for k, v in config.items() if k != 'name'}
    host = config['hostname'] if int(config['port']) == 22 else f"[{config['hostname']}]:{int(config['port'])}"
    parameters['host-key'] = host + ' ' + config['host-key']
    parameters.update({'enable-sftp': 'false', 'color-scheme': 'gray-black', 'font-size': '14',
                       'server-alive-interval': '30', 'scrollback': '2000'})
    return parameters


def managed_connections_sql(configs, previous_names, owner):
    for config in configs:
        connection_sql(config)
    attribute = 'ccheck-managed-project'
    sql = 'BEGIN;\nLOCK TABLE guacamole_connection IN SHARE ROW EXCLUSIVE MODE;\n'
    names = {config['name'] for config in configs}
    for name in set(previous_names) - names:
        sql += ('DELETE FROM guacamole_connection WHERE parent_id IS NULL AND connection_name=' + quote(name)
                + ' AND EXISTS (SELECT 1 FROM guacamole_connection_attribute a WHERE '
                'a.connection_id=guacamole_connection.connection_id AND a.attribute_name=' + quote(attribute)
                + ' AND a.attribute_value=' + quote(owner) + ');\n')
    for config in configs:
        delimiter = '$managed_' + secrets.token_hex(16) + '$'
        name = quote(config['name'])
        sql += ('DO ' + delimiter + ' DECLARE cid integer; matches integer; BEGIN '
                'SELECT count(*) INTO matches FROM guacamole_connection WHERE parent_id IS NULL AND connection_name='
                + name + "; IF matches > 1 THEN RAISE EXCEPTION 'Ambiguous terminal target name'; END IF; "
                'SELECT connection_id INTO cid FROM guacamole_connection WHERE parent_id IS NULL AND connection_name='
                + name + '; IF cid IS NULL THEN INSERT INTO guacamole_connection (connection_name,protocol) VALUES ('
                + name + ",'ssh') RETURNING connection_id INTO cid; "
                'INSERT INTO guacamole_connection_attribute (connection_id,attribute_name,attribute_value) VALUES (cid,'
                + quote(attribute) + ',' + quote(owner) + '); ELSE IF NOT EXISTS (SELECT 1 FROM '
                'guacamole_connection_attribute a JOIN guacamole_connection c USING(connection_id) '
                'WHERE a.connection_id=cid AND c.protocol=\'ssh\' AND a.attribute_name=' + quote(attribute)
                + ' AND a.attribute_value=' + quote(owner)
                + ") THEN RAISE EXCEPTION 'Target name is not owned by this terminal configuration'; END IF; END IF; "
                "DELETE FROM guacamole_connection_parameter WHERE connection_id=cid AND parameter_name IN "
                "('password','private-key','passphrase','private-key-passphrase'); ")
        for key, value in connection_parameters(config).items():
            sql += ('INSERT INTO guacamole_connection_parameter (connection_id,parameter_name,parameter_value) '
                    'VALUES (cid,' + quote(key) + ',' + quote(value) + ') ON CONFLICT (connection_id,parameter_name) '
                    'DO UPDATE SET parameter_value=EXCLUDED.parameter_value; ')
        sql += 'END ' + delimiter + ';\n'
    return sql + 'COMMIT;\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('lock', help='Resolve approved upstream tags to immutable digests (maintainers only)')
    init = commands.add_parser('init', help='Initialize a NEW deployment; never overwrites existing state')
    init.add_argument('--bind', default='127.0.0.1')
    init.add_argument('--port', type=int, default=8803)
    init.add_argument('--data-subnet', default='172.31.240.0/26')
    init.add_argument('--ssh-subnet', default='172.31.240.64/26')
    init.add_argument('--web-subnet', default='172.31.240.128/26')
    for name in ('start', 'stop', 'status', 'logs', 'check'):
        commands.add_parser(name)
    add = commands.add_parser('add-ssh', help='Add a trusted SSH target; users supply SSH passwords at connection time')
    add.add_argument('config', type=Path)
    backup = commands.add_parser('backup', help='Export database; back up state separately as documented')
    backup.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'lock':
        resolve_lock()
    elif args.command == 'init':
        initialize(args)
    elif args.command == 'start':
        compose('up', '-d', '--wait', '--wait-timeout', '180')
        check()
    elif args.command == 'check':
        check()
    elif args.command == 'stop':
        compose('stop', '--timeout', '30')
    elif args.command == 'status':
        compose('ps', '-a')
    elif args.command == 'logs':
        compose('logs', '--tail', '100')
    elif args.command == 'add-ssh':
        sql = connection_sql(json.loads(args.config.read_text()))
        compose('exec', '-T', 'database', 'psql', '-U', 'guacamole', '-d', 'guacamole',
                '-v', 'ON_ERROR_STOP=1', input=sql)
    elif args.command == 'backup':
        dump = compose('exec', '-T', 'database', 'pg_dump', '-U', 'guacamole', '-d', 'guacamole',
                       capture_output=True).stdout
        with args.output.open('xb') as raw:
            args.output.chmod(0o600)
            with gzip.GzipFile(fileobj=raw, mode='wb') as archive:
                archive.write(dump.encode())
        print('Database export saved. Also preserve state, image lock, Compose and TLS configuration.')


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        raise SystemExit(str(exc))
