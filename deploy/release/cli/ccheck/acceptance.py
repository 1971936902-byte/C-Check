import io
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from .core import DeploymentError, atomic_json
from .inference import served_name


class API:
    def __init__(self, site):
        bind = site['web']['bind']
        self.base = 'http://' + ('127.0.0.1' if bind == '0.0.0.0' else bind) + ':' + str(site['web']['port'])
        self.token = None

    def request(self, path, data=None, content_type=None, method=None, binary=False):
        headers = {}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            content_type = 'application/json'
        if content_type:
            headers['Content-Type'] = content_type
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=90) as response:
                result = response.read()
        except HTTPError as exc:
            raise DeploymentError('HTTP verification failed: ' + path + ' (status ' + str(exc.code) + ')') from exc
        return result if binary else json.loads(result)


def multipart(fields, filename, content):
    boundary = 'ccheck-' + secrets.token_hex(16)
    body = bytearray()
    for name, value in fields.items():
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 'Content-Type: application/zip\r\n\r\n').encode())
    body.extend(content)
    body.extend(f'\r\n--{boundary}--\r\n'.encode())
    return bytes(body), 'multipart/form-data; boundary=' + boundary


def fixture_archive(fixture):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', strict_timestamps=False) as output:
        for path in sorted(Path(fixture).iterdir()):
            output.write(path, path.name)
    return archive.getvalue()


def acceptance(site, release, engine, *, fresh=False):
    root = Path(site['data_dir'])
    engine.management('audit-core')
    runtime = engine.management('verify-runtime')
    api = API(site)
    password = (root / 'secrets/admin_password').read_text()
    login = api.request('/api/auth/login', {'username': 'admin', 'password': password})
    api.token = login['access_token']
    models = api.request('/api/models')
    selected = [model for model in models if model['is_default']]
    if len(selected) != 1 or selected[0]['model_identifier'] != served_name(site):
        raise DeploymentError('expected exactly one real default model')
    html = api.request('/', binary=True).decode()
    assets = re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html)
    if not assets or any(len(api.request(path, binary=True)) < 100 for path in assets):
        raise DeploymentError('frontend assets are missing')
    composition = json.dumps(json.loads(engine.file.read_text()), sort_keys=True)
    context = hashlib.sha256(composition.encode()).hexdigest()[:16]
    marker = root / 'state' / ('acceptance-' + release['_sha256'] + '-' + context + '.json')
    old = json.loads(marker.read_text()) if marker.exists() and not fresh else None
    if old and old.get('task_id'):
        task = api.request('/api/reviews/' + old['task_id'])
    else:
        fixture = Path(release['_root']) / 'verify/project'
        body, content_type = multipart({'model_node_id': selected[0]['id'], 'check_types': '["buffer_overflow"]',
                                       'display_name': 'Release-' + release['version'] + '-acceptance'},
                                      'release-acceptance.zip', fixture_archive(fixture))
        task = api.request('/api/reviews/archive', body, content_type)
        atomic_json(marker, {'task_id': task['id'], 'status': 'submitted'})
    deadline = time.monotonic() + 1200
    previous = None
    while task['status'] in {'queued', 'running'} and time.monotonic() < deadline:
        state = (task['status'], task['progress'])
        if state != previous:
            print('  review', task['id'], state[0], str(state[1]) + '%', flush=True)
            previous = state
        time.sleep(3)
        task = api.request('/api/reviews/' + task['id'])
    if task['status'] != 'completed':
        raise DeploymentError('review did not complete; inspect existing task ' + task['id'])
    if 'format_failed_batches=0' not in (task.get('model_log') or '') or 'fallback used' in task['model_log']:
        raise DeploymentError('real structured formatting did not succeed')
    report = api.request('/api/reports/' + task['report_id'])
    findings = report['result_json']['findings']
    if len(findings) != 1 or findings[0]['file_path'] != 'protocol.c' or findings[0]['line'] != 7:
        raise DeploymentError('golden unsafe/safe pair or exact source line verification failed')
    symbols = api.request('/api/code-index/' + task['id'] + '/symbols')
    edges = api.request('/api/code-index/' + task['id'] + '/graph')
    ids = {s['name']: s['id'] for s in symbols if s['kind'] == 'function'}
    if not any(e['edge_type'] == 'FUNCTION_CALLS_FUNCTION' and e['source_id'] == ids['parse_frame']
               and e['target_id'] == ids['receive_payload'] for e in edges):
        raise DeploymentError('expected cross-file graph call edge is missing')
    index_audit = engine.management('audit-index', '--task-id', task['id'])
    markdown = api.request('/api/reports/' + task['report_id'] + '/markdown', binary=True)
    pdf = api.request('/api/reports/' + task['report_id'] + '/pdf', binary=True)
    if 'protocol.c' not in markdown.decode('utf-8') or not pdf.startswith(b'%PDF') or len(pdf) < 2000:
        raise DeploymentError('report exports did not validate')
    resources = api.request('/api/admin/resources')
    if not any(model.get('metrics_available') for model in resources['models']):
        raise DeploymentError('vLLM metrics unavailable')
    if not resources['gpus'] or not any(gpu.get('memory_used_mb', 0) > 30000 for gpu in resources['gpus']):
        raise DeploymentError('GPU telemetry is missing from the containerized API')
    result = {'task_id': task['id'], 'report_id': task['report_id'], 'status': 'completed',
              'duration_ms': task['duration_ms'], 'finding_count': len(findings), 'line': findings[0]['line'],
              'symbols': len(symbols), 'edges': len(edges), 'runtime': runtime.strip(), 'index_audit': index_audit.strip(),
              'markdown_bytes': len(markdown), 'pdf_bytes': len(pdf), 'time': time.time()}
    atomic_json(marker, result)
    (root / 'state/acceptance-report.md').write_bytes(markdown)
    (root / 'state/acceptance-report.pdf').write_bytes(pdf)
    return result
