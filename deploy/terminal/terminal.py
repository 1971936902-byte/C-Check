#!/usr/bin/env python3
"""Manage only the independent ccheck-terminal Compose project."""
import argparse
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
LOCK = ROOT / 'images.lock.json'
IMAGES = {'GUACAMOLE_IMAGE': 'guacamole/guacamole:1.6.0',
          'GUACD_IMAGE': 'guacamole/guacd:1.6.0',
          'POSTGRES_IMAGE': 'postgres:17-alpine',
          'NGINX_IMAGE': 'public.ecr.aws/docker/library/nginx:1.28-alpine'}


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(args):
    return run(args, capture_output=True).stdout


def compose(*args, **kwargs):
    return run(['docker', 'compose', '--project-name', 'ccheck-terminal',
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
        run(['docker', 'pull', reference])
        return json.loads(capture(['docker', 'image', 'inspect', reference]))[0]


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
    if capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=ccheck-terminal']).strip():
        raise ValueError('Existing project volumes found. Restore state from backup; do not reset credentials.')
    locks = json.loads(LOCK.read_text())
    if set(locks) != set(IMAGES):
        raise ValueError('Incomplete image lock.')
    for item in locks.values():
        if not re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', item['image']):
            raise ValueError('Images must be pinned by sha256 digest.')
        ensure_image(item['image'])
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
    if str(bind_address) != '127.0.0.1':
        raise ValueError('Initial deployment must bind 127.0.0.1. Configure trusted TLS/access controls before exposing it.')
    with socket.socket() as listener:
        listener.bind((args.bind, args.port))
    schema = capture(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                      '--security-opt', 'no-new-privileges:true', '--entrypoint',
                      '/opt/guacamole/bin/initdb.sh', locks['GUACAMOLE_IMAGE']['image'], '--postgresql'])
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
    environment = {key: value['image'] for key, value in locks.items()}
    environment.update(TERMINAL_BIND=args.bind, TERMINAL_PORT=str(args.port),
                       TERMINAL_DATA_SUBNET=args.data_subnet, TERMINAL_SSH_SUBNET=args.ssh_subnet,
                       TERMINAL_WEB_SUBNET=args.web_subnet)
    write(STATE / 'compose.env', ''.join(f'{key}={value}\n' for key, value in environment.items()))
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', '90',
         '-keyout', str(STATE / 'tls.key'), '-out', str(STATE / 'tls.crt'),
         '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
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
    url = f'https://localhost:{port}/guacamole/'
    for _ in range(90):
        try:
            with urllib.request.urlopen(url, context=context, timeout=5) as response:
                if response.status == 200:
                    print('HTTPS ready: ' + url)
                    return
        except (OSError, ValueError):
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
    parameters = {k: str(v) for k, v in config.items() if k != 'name'}
    host = config['hostname'] if int(config['port']) == 22 else f"[{config['hostname']}]:{int(config['port'])}"
    parameters['host-key'] = host + ' ' + config['host-key']
    parameters.update({'enable-sftp': 'false', 'color-scheme': 'gray-black', 'font-size': '14',
                       'server-alive-interval': '30', 'scrollback': '2000'})
    for key, value in parameters.items():
        sql += ('INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value) '
                f"VALUES (currval('guacamole_connection_connection_id_seq'), {quote(key)}, {quote(value)});\n")
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
