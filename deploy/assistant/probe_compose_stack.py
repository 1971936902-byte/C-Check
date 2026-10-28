"""Disposable full Compose acceptance; restore the explicitly named inference services."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from probe_qwen_gpu import run, wait_for_restored_inference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--qwen-container', required=True)
    parser.add_argument('--embedding-container', required=True)
    parser.add_argument('--mysql-container', required=True)
    parser.add_argument('--redis-container', required=True)
    parser.add_argument('--qdrant-container', required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--jina-model-dir', type=Path, required=True)
    parser.add_argument('--inference-image', required=True)
    parser.add_argument('--browser-ready-file', type=Path)
    parser.add_argument('--browser-done-file', type=Path)
    parser.add_argument('--exercise-recovery', action='store_true')
    parser.add_argument('--backend-image', default='ccheck-assistant-backend:development')
    parser.add_argument('--frontend-image', default='ccheck-assistant-frontend:development')
    parser.add_argument('--provider-image', default='ccheck-anythingllm:verified-development')
    parser.add_argument('--compiled-backend', action='store_true',
                        help='use the canonical /app/backend runtime layout')
    args = parser.parse_args()
    if bool(args.browser_ready_file) != bool(args.browser_done_file):
        parser.error('browser ready/done paths must be supplied together')
    if args.browser_ready_file and any(path.exists() for path in (args.browser_ready_file, args.browser_done_file)):
        parser.error('browser session marker paths must be fresh')
    sys.path.insert(0, str(args.cli.resolve()))
    from ccheck import assistant, networks
    from ccheck.engine import Engine, prepare_data, render_compose

    originals = [args.qwen_container, args.embedding_container]
    snapshots = [json.loads(run('docker', 'inspect', name))[0] for name in originals]
    if len(set(originals)) != 2 or not all(item['State']['Running'] for item in snapshots):
        raise RuntimeError('both distinct original inference containers must be running')
    if shutil.disk_usage(tempfile.gettempdir()).free < 2 * 1024**3:
        raise RuntimeError('full Compose probe requires 2 GiB free temporary space')
    for model in (args.model_dir, args.jina_model_dir):
        if not model.is_absolute() or not (model / 'config.json').is_file():
            raise RuntimeError('probe requires existing absolute model directories')
    images = {name: run('docker', 'inspect', '--format', '{{.Image}}', container)
              for name, container in (('mysql', args.mysql_container), ('redis', args.redis_container),
                                      ('qdrant', args.qdrant_container), ('embedding', args.embedding_container))}
    images.update({name: run('docker', 'image', 'inspect', '--format', '{{.Id}}', tag)
        for name, tag in (('backend', args.backend_image),
                          ('frontend', args.frontend_image),
                          ('anythingllm', args.provider_image),
                          ('qwen', args.inference_image))})
    resources = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='ccheck-assistant-compose-') as temporary:
        root = Path(temporary)
        site = {'instance': 'assistant-probe-' + uuid.uuid4().hex[:8], 'data_dir': temporary,
            'gpu': '0', 'profile': 'ada-48g-fp8', 'gpu_access_mode': 'legacy', 'performance': {},
            'assistant': {'enabled': True, 'subnet': '172.29.222.48/28'},
            'network_subnets': {'data': '172.29.222.0/28', 'inference': '172.29.222.16/28', 'edge': '172.29.222.32/28'},
            'web': {'bind': '127.0.0.1', 'port': 18801, 'public_url': 'http://127.0.0.1:18801'}}
        networks.preflight(site)
        prepare_data(site, {})
        document = render_compose(site, images, 10001, 10001)
        services = document['services']
        # The build host has 40 GiB; only this disposable probe uses the tested
        # 40-GiB budget. The supported 48-GiB deployment recipe is unchanged.
        services['qwen']['environment'].update(GPU_MEMORY_UTILIZATION='0.88', MAX_NUM_SEQS='1', MAX_NUM_BATCHED_TOKENS='1024')
        for name, model in (('qwen', args.model_dir), ('embedding', args.jina_model_dir)):
            for volume in services[name]['volumes']:
                if volume['target'].startswith('/models/'):
                    volume['source'] = str(model)
        # Validation images keep updated Python separate from the old runtime.
        if not args.compiled_backend:
            for name in ('api', 'worker', 'init', 'assistant-gateway', 'assistant-worker'):
                services[name]['working_dir'] = '/opt/c-check-assistant/backend'
        services['api']['networks'] = {name: ({'aliases': ['probe-api']} if name == 'data' else {})
                                      for name in services['api']['networks']}
        services['qwen']['networks'] = {'inference': {'aliases': ['probe-qwen']}}
        services['init']['volumes'] = list(services['init']['volumes']) + [
            {'type': 'bind', 'source': str(resources), 'target': '/probe/validation', 'read_only': True}]
        services['init']['secrets'] = list(services['init']['secrets']) + [
            {'source': 'admin_password', 'target': 'probe_admin'},
            {'source': 'vllm_api_key', 'target': 'probe_api'}]
        recovery_identity = 'ccheck-assistant-compose-probe-' + uuid.uuid4().hex
        services['init']['environment'] = dict(services['init']['environment'], PROBE_REVIEW_PREEMPTION='1',
            PROBE_ISOLATED_ASSISTANT_RECOVERY=recovery_identity)
        engine = Engine(site)
        engine.file.write_text(json.dumps(document))
        engine.compose('config', '--quiet')
        stopped = False
        browser_ready = False
        try:
            engine.compose('up', '-d', 'mysql', 'redis', 'qdrant')
            engine.wait(['mysql', 'redis', 'qdrant'], timeout=120)
            engine.management('migrate')
            stopped = True
            run('docker', 'stop', '--time', '30', *originals, timeout=90)
            engine.compose('up', '-d', 'qwen')
            engine.wait(['qwen'], timeout=300)
            engine.compose('up', '-d', 'embedding')
            engine.wait(['embedding'], timeout=180)
            engine.compose('up', '-d', *assistant.SERVICES)
            engine.wait(list(assistant.SERVICES), timeout=120)
            engine.compose('up', '-d', 'api', 'worker', 'frontend')
            engine.wait(['api', 'worker', 'frontend'], timeout=90)
            engine.management('register-model')
            networks.verify(engine)
            print(engine.compose('run', '--rm', '--no-deps', 'init', 'python',
                '/probe/validation/probe_public_rag.py', timeout=420), flush=True)
            print('SEPARATE_CONTAINER_COMPOSE_MODELS_CELERY_PROVIDER_AND_PUBLIC_API_OK', flush=True)
            if args.exercise_recovery:
                from ccheck.assistant_recovery import recover_stack
                def recovery_probe(operation):
                    print('RECOVERY_PROBE_STAGE', operation, flush=True)
                    result = subprocess.run(['docker', 'compose', '-f', str(engine.file),
                        'run', '--rm', '--no-deps', 'init', 'python', '-c',
                        'import runpy; runpy.run_path("/probe/validation/probe_recovery_state.py", run_name="__main__")',
                        operation, '--identity', recovery_identity],
                        capture_output=True, text=True, timeout=180)
                    if result.returncode:
                        diagnostic = result.stdout + result.stderr
                        for secret in (root / 'secrets').iterdir():
                            value = secret.read_text().strip()
                            if value:
                                diagnostic = diagnostic.replace(value, '[redacted]')
                        raise RuntimeError('isolated recovery probe failed at ' + operation + ': ' + diagnostic[-6000:])
                    return result.stdout
                before = {name: engine.compose('ps', '-q', name).strip() for name in ('qwen', 'embedding')}
                recovery_probe('seed')
                from ccheck.core import DeploymentError
                try:
                    recover_stack(engine, 'isolated-compose-recovery')
                except DeploymentError:
                    pass
                else:
                    raise AssertionError('active review did not prevent model recovery')
                assert before == {name: engine.compose('ps', '-q', name).strip() for name in before}
                assert not (root / 'data/control/maintenance').exists()
                recovery_probe('clear-busy')
                recover_stack(engine, 'isolated-compose-recovery')
                assert all(engine.compose('ps', '-q', name).strip() != identifier for name, identifier in before.items())
                print(recovery_probe('verify'), flush=True)
            if args.browser_ready_file:
                fd = os.open(args.browser_ready_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump({'server_port': 18801, 'username': 'admin',
                        'password': (root / 'secrets/admin_password').read_text().strip()}, output)
                browser_ready = True
                print('REAL_BROWSER_SESSION_READY', flush=True)
                deadline = time.monotonic() + 600
                while not args.browser_done_file.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('browser acceptance completion deadline exceeded')
                    time.sleep(2)
                print('REAL_BROWSER_SESSION_FINISHED', flush=True)
        except Exception as error:
            result = subprocess.run(['docker', 'compose', '-f', str(engine.file), 'logs', '--tail', '30'],
                capture_output=True, text=True, timeout=30)
            diagnostic = result.stdout + result.stderr
            for secret in (root / 'secrets').iterdir():
                value = secret.read_text().strip()
                if value:
                    diagnostic = diagnostic.replace(value, '[redacted]')
            print(diagnostic[-20000:], flush=True)
            raise RuntimeError('full Compose acceptance failed; see redacted diagnostics') from error
        finally:
            if browser_ready:
                args.browser_ready_file.unlink(missing_ok=True)
            engine.compose('down', '--timeout', '30', '--remove-orphans', timeout=120)
            if stopped:
                run('docker', 'start', *originals, timeout=60)
                wait_for_restored_inference(originals, snapshots)
                print('ORIGINAL_INFERENCE_HEALTH_RECOVERY_CONFIRMED', flush=True)


if __name__ == '__main__':
    main()
