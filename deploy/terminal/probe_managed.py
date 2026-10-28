"""Exercise managed terminal lifecycle using a disposable signing key and private database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def execute(command, **kwargs):
    return subprocess.run(command, check=True, timeout=1800, **kwargs)


def verify_existing(root, source, connection_id, host_key_file):
    sys.path.insert(0, str(source / 'deploy/release/cli'))
    from ccheck import remote_terminal
    from ccheck.core import atomic_json
    site_file = root / 'site.json'
    site = json.loads(site_file.read_text())
    host_key = ' '.join(host_key_file.read_text().split()[:2])
    if site['terminal']['connections'][0]['host-key'] != host_key:
        site['terminal']['connections'][0]['host-key'] = host_key
        atomic_json(site_file, site)
        execute([sys.executable, str(root / 'release/cli/ccheck.pyz'),
            'terminal-configure', '--config', str(site_file)])
    record = json.loads((root / 'terminal/owner.json').read_text())
    module = remote_terminal.cached_runtime(root / 'terminal', record)
    remote_terminal.validate_state(module, record)
    assert record['stage'] == 'ready' and record['started_once']
    module.check()
    credentials = json.loads((module.STATE / 'admin-credentials.json').read_text())
    context = ssl.create_default_context(cafile=str(module.STATE / 'tls.crt'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    base = record['settings']['public_url'] + 'api/'
    body = urllib.parse.urlencode(credentials).encode()
    with opener.open(urllib.request.Request(base + 'tokens', data=body), timeout=30) as response:
        token = json.load(response)['authToken']
    def request(path):
        with opener.open(base + path + '?' + urllib.parse.urlencode({'token': token}), timeout=30) as response:
            return json.load(response)
    try:
        targets = request('session/data/postgresql/connections')
        assert set(targets) == {connection_id}
        assert targets[connection_id]['name'] == 'Compile host'
        parameters = request('session/data/postgresql/connections/' + connection_id + '/parameters')
        assert parameters['username'] == 'root' and parameters.get('host-key')
        assert not parameters.get('password') and not parameters.get('private-key')
        permissions = request('session/data/postgresql/users/probe-viewer/permissions')
        assert permissions['connectionPermissions'][connection_id] == ['READ'], 'connection READ permission was not preserved'
        report = {'passed': True, 'scope': 'CPU terminal signed fixture; not an application release',
            'tls': True, 'restart_preservation': True, 'configuration': True, 'isolation': True,
            'real_ssh': False, 'runtime': remote_terminal.hashes(), 'project': module.PROJECT,
            'connection_id': connection_id, 'root': str(root),
            'credentials_file': str(module.STATE / 'admin-credentials.json'),
            'assertion_recheck': 'Guacamole returns permission lists; original string assertion corrected'}
        atomic_json(root / 'result.json', report)
        print('TERMINAL_PROBE', json.dumps(report), flush=True)
    finally:
        with opener.open(urllib.request.Request(base + 'tokens/' + token + '?'
                + urllib.parse.urlencode({'token': token}), method='DELETE'), timeout=30):
            pass
    key = root / 'disposable.key'
    if key.exists():
        key.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18843)
    parser.add_argument('--registry-mirror', help='optional registry hostname/prefix; original locked digests are retained')
    parser.add_argument('--verify-existing', metavar='CONNECTION_ID', help='recheck a completed preserved fixture without reinitializing it')
    parser.add_argument('--host-key-file', type=Path, default=Path('/etc/ssh/ssh_host_ecdsa_key.pub'),
        help='trusted target public host key matching guacd SSH negotiation')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    if args.verify_existing is not None:
        verify_existing(root, args.source, args.verify_existing, args.host_key_file)
        return
    if root.exists():
        raise RuntimeError('probe root must be new; do not overwrite credentials or resume implicitly')
    root.mkdir(mode=0o700)
    sys.path.insert(0, str(args.source / 'deploy/release/cli'))
    from ccheck import remote_terminal
    from ccheck.core import atomic_json, digest
    key = root / 'disposable.key'
    trusted = root / 'disposable-public.pem'
    execute(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(key)])
    execute(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(trusted)])
    release = root / 'release'
    (release / 'cli').mkdir(parents=True)
    (release / 'models').mkdir()
    # The tiny fixture verifies terminal packaging only, not a deployable application release.
    (release / 'core-policy.json').write_text('{}')
    (release / 'models/model-manifest.json').write_text('{}')
    import zipfile
    yaml = root / 'yaml'
    with zipfile.ZipFile(args.baseline / 'cli/ccheck.pyz') as archive:
        for name in archive.namelist():
            if name.startswith('yaml/') and name.endswith('.py') and '..' not in Path(name).parts:
                target = root / name
                target.parent.mkdir(exist_ok=True, parents=True)
                target.write_bytes(archive.read(name))
    execute([sys.executable, str(args.source / 'deploy/release/build_cli.py'), '--output',
        str(release / 'cli/ccheck.pyz'), '--yaml-package', str(yaml), '--with-terminal', '--with-assistant'])
    files = {path.relative_to(release).as_posix(): digest(path) for path in release.rglob('*') if path.is_file()}
    manifest = {'format': 1, 'version': 'v0.1.0-rc.999', 'tag': 'release-v0.1.0-rc.999',
        'files': files, 'terminal_runtime': remote_terminal.hashes()}
    atomic_json(release / 'release.json', manifest)
    execute(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in',
        str(release / 'release.json'), '-out', str(release / 'release.sig')])
    site = json.loads((args.source / 'deploy/release/site-rtx5880.example.json').read_text())
    site.update(instance='terminal-probe-' + secrets.token_hex(4), data_dir=str(root / 'application'),
        terminal={'enabled': True, 'data_dir': str(root / 'terminal'), 'port': args.port,
        'connections': [{'name': 'Compile host', 'hostname': 'host.docker.internal', 'port': 22,
            'username': 'vipuser', 'host-key': ' '.join(args.host_key_file.read_text().split()[:2])}]})
    if args.registry_mirror:
        locks = json.loads(remote_terminal.resource('images.lock.json'))
        site['terminal']['image_sources'] = {key: args.registry_mirror.rstrip('/') + '/'
            + (record['image'] if '/' in record['image'].split('@')[0] else 'library/' + record['image'])
            for key, record in locks.items() if key != 'NGINX_IMAGE'}
    config_file = root / 'site.json'
    atomic_json(config_file, site)
    cli = [sys.executable, str(release / 'cli/ccheck.pyz')]
    def cli_command(action):
        execute([*cli, 'terminal-' + action, '--config', str(config_file)])
    execute([*cli, 'terminal-install', '--config', str(config_file), '--release', str(release),
        '--trusted-key', str(trusted)])
    cli_command('start')
    normalized = remote_terminal.settings(site)
    owner_path = root / 'terminal/owner.json'
    owner = json.loads(owner_path.read_text())
    module = remote_terminal.cached_runtime(root / 'terminal', owner)
    credentials_path = module.STATE / 'admin-credentials.json'
    credential_hash = digest(credentials_path)
    credentials = json.loads(credentials_path.read_text())
    context = ssl.create_default_context(cafile=str(module.STATE / 'tls.crt'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    base = normalized['public_url'] + 'api/'
    def request(path, *, method='GET', body=None, form=False, token=None):
        if token:
            path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'token': token})
        data = None if body is None else (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
            headers={'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'})
        try:
            with opener.open(req, timeout=30) as response:
                data = response.read()
                return response.status, json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            return exc.code, None
    assert request('tokens', method='POST', body={'username': 'guacadmin', 'password': 'guacadmin'}, form=True)[0] in (401, 403)
    assert request('session/data/postgresql/connections')[0] in (401, 403)
    status, auth = request('tokens', method='POST', body=credentials, form=True)
    assert status == 200
    token = auth['authToken']
    status, targets = request('session/data/postgresql/connections', token=token)
    assert status == 200 and len(targets) == 1
    connection_id = next(iter(targets))
    status, parameters = request('session/data/postgresql/connections/' + connection_id + '/parameters', token=token)
    assert status == 200 and parameters['hostname'] == 'host.docker.internal' and parameters.get('host-key')
    assert not parameters.get('password') and not parameters.get('private-key')
    viewer_name, viewer_password = 'probe-viewer', secrets.token_urlsafe(30)
    assert request('session/data/postgresql/users', method='POST', body={
        'username': viewer_name, 'password': viewer_password, 'attributes': {}}, token=token)[0] in (200, 201, 204)
    status, viewer = request('tokens', method='POST', body={'username': viewer_name, 'password': viewer_password}, form=True)
    assert status == 200
    viewer_token = viewer['authToken']
    assert request('session/data/postgresql/connections', token=viewer_token) == (200, {})
    assert request('session/data/postgresql/connections', method='POST', body={
        'name': 'forbidden', 'protocol': 'ssh', 'parameters': {}, 'attributes': {}}, token=viewer_token)[0] in (401, 403)
    assert request('session/data/postgresql/users/' + viewer_name + '/permissions', method='PATCH', body=[
        {'op': 'add', 'path': '/connectionPermissions/' + connection_id, 'value': 'READ'}], token=token)[0] in (200, 204)
    assert set(request('session/data/postgresql/connections', token=viewer_token)[1]) == {connection_id}
    request('tokens/' + viewer_token, method='DELETE', token=viewer_token)
    request('tokens/' + token, method='DELETE', token=token)
    cli_command('stop')
    cli_command('start')
    assert digest(credentials_path) == credential_hash
    # Seed only a synthetic secret; reapplying managed targets must remove it, retaining IDs/ACLs.
    module.compose('exec', '-T', 'database', 'psql', '-U', 'guacamole', '-d', 'guacamole', '-v', 'ON_ERROR_STOP=1',
        input="INSERT INTO guacamole_connection_parameter VALUES (" + connection_id + ",'password','synthetic-probe-only');", capture_output=True)
    site['terminal']['connections'][0]['username'] = 'root'
    atomic_json(config_file, site)
    cli_command('configure')
    assert digest(credentials_path) == credential_hash
    status, auth = request('tokens', method='POST', body=credentials, form=True)
    assert status == 200
    token = auth['authToken']
    targets = request('session/data/postgresql/connections', token=token)[1]
    assert set(targets) == {connection_id}
    parameters = request('session/data/postgresql/connections/' + connection_id + '/parameters', token=token)[1]
    assert parameters['username'] == 'root' and not parameters.get('password')
    permissions = request('session/data/postgresql/users/' + viewer_name + '/permissions', token=token)[1]
    assert permissions['connectionPermissions'][connection_id] == ['READ']
    request('tokens/' + token, method='DELETE', token=token)
    cli_command('status')
    cli_command('check')
    report = {'passed': True, 'scope': 'CPU terminal signed fixture; not an application release',
        'tls': True, 'restart_preservation': True, 'configuration': True, 'isolation': True,
        'real_ssh': False, 'runtime': remote_terminal.hashes(), 'project': module.PROJECT,
        'connection_id': connection_id, 'root': str(root), 'credentials_file': str(credentials_path)}
    atomic_json(root / 'result.json', report)
    key.unlink()
    print('TERMINAL_PROBE', json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
