#!/usr/bin/env python3
"""Exercise real Guacamole authentication/permissions without any model tasks."""
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

import terminal


def main():
    env = dict(line.split('=', 1) for line in (terminal.STATE / 'compose.env').read_text().splitlines())
    base = 'https://localhost:' + env['TERMINAL_PORT'] + '/guacamole/api/'
    context = ssl.create_default_context(cafile=str(terminal.STATE / 'tls.crt'))
    credentials = json.loads((terminal.STATE / 'admin-credentials.json').read_text())

    def request(path, method='GET', body=None, token=None, form=False):
        if token:
            path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'token': token})
        data = None if body is None else (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers, method=method),
                                        context=context, timeout=20) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            return exc.code, None

    assert request('tokens', 'POST', {'username': 'guacadmin', 'password': 'guacadmin'}, form=True)[0] in (401, 403)
    assert request('session/data/postgresql/connections')[0] in (401, 403)
    status, login = request('tokens', 'POST', credentials, form=True)
    assert status == 200, 'Saved bootstrap credential no longer works; use an administrator test credential securely.'
    token = login['authToken']
    name = 'verify-' + secrets.token_hex(6)
    password = secrets.token_urlsafe(30)
    viewer_token = None
    created = False
    report = {'tls_verified': True, 'default_admin_rejected': True, 'anonymous_rejected': True}
    try:
        status, connections = request('session/data/postgresql/connections', token=token)
        assert status == 200 and connections, 'Configure a trusted SSH target before verification.'
        connection = next(iter(connections))
        status, parameters = request('session/data/postgresql/connections/' + connection + '/parameters', token=token)
        assert status == 200 and parameters.get('host-key')
        assert not parameters.get('password') and not parameters.get('private-key')
        report['ssh_host_key_required'] = True
        report['ssh_credentials_not_saved'] = True
        status, _ = request('session/data/postgresql/users', 'POST',
                            {'username': name, 'password': password, 'attributes': {}}, token)
        assert status in (200, 201, 204)
        created = True
        status, viewer = request('tokens', 'POST', {'username': name, 'password': password}, form=True)
        assert status == 200
        viewer_token = viewer['authToken']
        status, visible = request('session/data/postgresql/connections', token=viewer_token)
        assert status == 200 and visible == {}
        denied, _ = request('session/data/postgresql/connections', 'POST',
                            {'name': 'unauthorized', 'protocol': 'ssh', 'parameters': {}, 'attributes': {}}, viewer_token)
        assert denied in (401, 403), 'Unprivileged user can create arbitrary connections!'
        status, _ = request('session/data/postgresql/users/' + name + '/permissions', 'PATCH',
                            [{'op': 'add', 'path': '/connectionPermissions/' + connection, 'value': 'READ'}], token)
        assert status in (200, 204)
        status, visible = request('session/data/postgresql/connections', token=viewer_token)
        assert status == 200 and set(visible) == {connection}
        report['unprivileged_create_denied'] = True
        report['connection_acl_verified'] = True
    finally:
        if viewer_token:
            request('tokens/' + viewer_token, 'DELETE', token=viewer_token)
        if created:
            status, _ = request('session/data/postgresql/users/' + name, 'DELETE', token=token)
            assert status in (200, 204), 'Remove verification user manually: ' + name
        request('tokens/' + token, 'DELETE', token=token)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
