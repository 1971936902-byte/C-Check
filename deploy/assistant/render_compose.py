"""Generate a separate assistant-enabled Compose candidate; never mutate rc20 state."""
import argparse
from copy import deepcopy
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re


def immutable_image(value):
    if not re.fullmatch(r'(?:[a-zA-Z0-9._:/-]+@)?sha256:[0-9a-f]{64}', value):
        raise ValueError('images must be immutable IDs or repository digests')
    return value


def linux_path(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('deployment paths must be absolute Linux paths')
    return path


def bind(source, target, readonly=False):
    return {'type': 'bind', 'source': str(source), 'target': target, 'read_only': readonly,
            'bind': {'create_host_path': False}}


def render(base, *, backend_image, frontend_image, anythingllm_image, data_dir, runtime_dir,
           subnet='172.30.10.192/26'):
    result = deepcopy(base)
    result['services'] = {name: deepcopy(spec) for name, spec in base['services'].items()}
    root, runtime = linux_path(data_dir), linux_path(runtime_dir)
    network = ipaddress.ip_network(subnet, strict=True)
    if network.version != 4 or not network.is_private:
        raise ValueError('assistant subnet must be a private IPv4 network')
    if any(name in result['services'] for name in ('assistant-worker', 'assistant-gateway', 'anythingllm')):
        raise ValueError('assistant services already exist; render from the original base')
    for existing in result.get('networks', {}).values():
        if existing is result.get('networks', {}).get('assistant'):
            if (existing.get('internal') is not True or
                    [entry.get('subnet') for entry in existing.get('ipam', {}).get('config', [])] != [str(network)]):
                raise ValueError('assistant network differs from configured subnet')
            continue
        for config in existing.get('ipam', {}).get('config', []):
            other = ipaddress.ip_network(config['subnet'])
            if other.version == network.version and network.overlaps(other):
                raise ValueError('assistant subnet overlaps an existing project network')
    result.setdefault('networks', {}).setdefault('assistant', {'internal': True,
        'ipam': {'config': [{'subnet': str(network)}]}})
    services = result['services']
    if services['qwen'].get('environment', {}).get('CCHECK_REVIEW_MODEL', 'qwen2.5-coder-32b-instruct-fp8') != 'qwen2.5-coder-32b-instruct-fp8':
        raise ValueError('this assistant build supports only the existing Qwen2.5 FP8 deployment')
    for name in ('api', 'worker', 'init'):
        services[name]['image'] = immutable_image(backend_image)
        services[name]['environment']['ASSISTANT_ENABLED'] = 'true'
    services['frontend']['image'] = immutable_image(frontend_image)
    for name in ('assistant_gateway_key', 'assistant_control_key', 'assistant_anythingllm_api_key'):
        result['secrets'][name] = {'file': str(root / 'secrets' / name)}
    api = services['api']
    api['networks'] = list(api['networks']) + ['assistant']
    api['environment'].update(ASSISTANT_ANYTHINGLLM_URL='http://anythingllm:3001',
        ASSISTANT_ANYTHINGLLM_API_KEY_FILE='/run/secrets/assistant_anythingllm_api_key')
    api['secrets'].append('assistant_anythingllm_api_key')

    worker = deepcopy(services['worker'])
    worker['networks'] = ['data', 'assistant']
    worker['environment'].update(ASSISTANT_ANYTHINGLLM_URL='http://anythingllm:3001',
        ASSISTANT_ANYTHINGLLM_API_KEY_FILE='/run/secrets/assistant_anythingllm_api_key')
    worker['secrets'].append('assistant_anythingllm_api_key')
    worker['command'] = ['celery', '-A', 'app.worker.celery_app', 'worker', '--loglevel=INFO',
                         '--concurrency=1', '--prefetch-multiplier=1', '--queues=assistant']
    worker['cpus'] = 2
    worker['mem_limit'] = '3g'
    services['assistant-worker'] = worker
    services['worker']['command'] = list(services['worker']['command']) + ['--queues=celery']

    gateway = deepcopy(services['worker'])
    gateway['networks'] = ['data', 'inference', 'assistant']
    gateway['command'] = ['uvicorn', 'app.assistant_gateway:app', '--host', '0.0.0.0', '--port', '8010', '--workers', '1']
    gateway['environment'].update(ASSISTANT_GATEWAY_KEY_FILE='/run/secrets/assistant_gateway_key',
        ASSISTANT_CONTROL_KEY_FILE='/run/secrets/assistant_control_key',
        ASSISTANT_QWEN_API_KEY_FILE='/run/secrets/vllm_api_key', ASSISTANT_QWEN_BASE_URL='http://qwen:8001')
    gateway['secrets'] += ['assistant_gateway_key', 'assistant_control_key']
    gateway['healthcheck'] = {'test': ['CMD', 'python', '-c',
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8010/health',timeout=5)"],
        'interval': '15s', 'timeout': '10s', 'retries': 5}
    gateway['mem_limit'] = '1g'
    services['assistant-gateway'] = gateway

    qwen = services['qwen']
    qwen['entrypoint'] = ['python', '/opt/assistant/qwen_assistant_gpu.py']
    qwen['volumes'].append(bind(runtime, '/opt/assistant', True))
    qwen['environment']['ASSISTANT_CONTROL_KEY_FILE'] = '/run/secrets/assistant_control_key'
    qwen['secrets'].append('assistant_control_key')
    embedding = services['embedding']
    if not any(volume.get('target') == '/opt/ccheck-runtime/ccheck_embedding.py'
               for volume in embedding.get('volumes', []) if isinstance(volume, dict)):
        raise ValueError('assistant recovery requires the existing GPU embedding launcher')
    embedding['entrypoint'] = ['python', '/opt/assistant/embedding_assistant_gpu.py']
    embedding['command'] = []
    embedding['volumes'].append(bind(runtime, '/opt/assistant', True))
    embedding['environment']['ASSISTANT_CONTROL_KEY_FILE'] = '/run/secrets/assistant_control_key'
    embedding['secrets'].append('assistant_control_key')
    if not all(key in qwen['environment'] for key in ('MAX_NUM_SEQS', 'MAX_NUM_BATCHED_TOKENS')):
        raise ValueError('base must contain the existing rc16+ FP8 performance configuration')

    services['anythingllm'] = {
        'image': immutable_image(anythingllm_image), 'pull_policy': 'never', 'restart': 'unless-stopped',
        'user': api['user'], 'runtime': 'runc', 'read_only': True, 'init': True,
        'entrypoint': ['node', '/opt/assistant/anythingllm-entrypoint.cjs'],
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'], 'pids_limit': 512,
        'mem_limit': '4g', 'cpus': 2, 'networks': ['assistant'],
        'tmpfs': ['/tmp:size=536870912,mode=1777'],
        'env_file': [str(runtime / 'anythingllm.env.example')],
        'environment': {'ASSISTANT_GATEWAY_KEY_FILE': '/run/secrets/assistant_gateway_key',
            'ASSISTANT_ANYTHINGLLM_API_KEY_FILE': '/run/secrets/assistant_anythingllm_api_key',
            'NVIDIA_VISIBLE_DEVICES': 'void', 'HOME': '/tmp', 'STORAGE_DIR': '/app/server/storage'},
        'secrets': ['assistant_gateway_key', 'assistant_anythingllm_api_key'],
        'volumes': [bind(runtime, '/opt/assistant', True),
            bind(root / 'data/assistant/storage', '/app/server/storage'),
            bind(root / 'data/assistant/collector-hotdir', '/app/collector/hotdir'),
            bind(root / 'data/assistant/collector-storage', '/app/collector/storage'),
            bind(root / 'data/assistant/server.env', '/app/server/.env')],
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
        'healthcheck': {'test': ['CMD', 'node', '-e',
            "Promise.all(['http://127.0.0.1:3001/api/ping','http://127.0.0.1:8888/accepts'].map(u=>fetch(u,{signal:AbortSignal.timeout(3000)}))).then(rs=>{if(rs.some(r=>!r.ok))process.exit(1)}).catch(()=>process.exit(1))"],
            'interval': '15s', 'timeout': '10s', 'start_period': '30s', 'retries': 20},
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'output', 'backend-image', 'frontend-image', 'anythingllm-image', 'data-dir', 'runtime-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--subnet', default='172.30.10.192/26')
    args = parser.parse_args()
    if Path(args.base).resolve() == Path(args.output).resolve():
        parser.error('output must not overwrite the original Compose file')
    result = render(json.loads(Path(args.base).read_text()), backend_image=args.backend_image,
        frontend_image=args.frontend_image, anythingllm_image=args.anythingllm_image,
        data_dir=args.data_dir, runtime_dir=args.runtime_dir, subnet=args.subnet)
    with Path(args.output).open('x', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()
