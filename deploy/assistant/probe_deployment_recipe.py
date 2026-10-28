"""Validate the built installer recipe without starting or changing any services."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--frontend-image', required=True)
    parser.add_argument('--provider-image', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.cli.resolve()))
    from ccheck import assistant, networks
    from ccheck.engine import prepare_data, render_compose
    from ccheck.core import DeploymentError

    with tempfile.TemporaryDirectory(prefix='ccheck-assistant-recipe-') as temporary:
        root = Path(temporary)
        site = {'instance': 'assistant-recipe-probe', 'data_dir': temporary, 'gpu': '0',
            'profile': 'ada-48g-fp8', 'gpu_access_mode': 'cdi', 'performance': {},
            'assistant': {'enabled': True},
            'web': {'bind': '127.0.0.1', 'port': 18801, 'public_url': 'http://127.0.0.1:18801'}}
        images = dict.fromkeys(('mysql', 'redis', 'qdrant', 'qwen', 'embedding'), args.backend_image)
        images.update(backend=args.backend_image, frontend=args.frontend_image, anythingllm=args.provider_image)
        assistant.validate_release(site, {'assistant_runtime': assistant.hashes(), 'images': images})
        prepare_data(site, {})
        prepare_data(site, {})
        first = render_compose(site, images, 10001, 10001)
        assert first == render_compose(site, images, 10001, 10001)
        assert first['networks'] == networks.specifications(site)
        file = root / 'compose.json'
        file.write_text(json.dumps(first))
        result = subprocess.run(['docker', 'compose', '-f', str(file), 'config', '--format', 'json'],
            check=True, text=True, capture_output=True, timeout=30)
        parsed = json.loads(result.stdout)
        assert set(assistant.SERVICES) <= set(parsed['services'])
        for name in assistant.SERVICES:
            assert not parsed['services'][name].get('ports')
            assert not parsed['services'][name].get('devices')
        before = {path.name: path.read_bytes() for path in (root / 'secrets').iterdir()}
        assistant.prepare(site, 10001, 10001)
        assert before == {path.name: path.read_bytes() for path in (root / 'secrets').iterdir()}
        for path in (root / 'secrets').iterdir():
            assert path.stat().st_uid == 10001 and path.stat().st_mode & 0o077 == 0
        storage = root / 'data/assistant/storage'
        storage.chmod(0o755)
        try:
            assistant.prepare(site, 10001, 10001)
        except ValueError:
            pass
        else:
            raise AssertionError('publicly readable knowledge storage was accepted')
        storage.chmod(0o700)
        environment = root / 'data/assistant/server.env'
        environment.chmod(0o644)
        try:
            assistant.prepare(site, 10001, 10001)
        except ValueError:
            pass
        else:
            raise AssertionError('publicly readable provider configuration was accepted')
        environment.chmod(0o600)
        launcher = assistant.runtime_dir(site) / 'guidance_compat.py'
        launcher.chmod(0o644)
        launcher.write_text('tampered')
        try:
            assistant.verify(site)
        except DeploymentError:
            pass
        else:
            raise AssertionError('modified runtime was accepted')
    print('BUILT_CLI_ASSISTANT_RECIPE_COMPOSE_PERMISSIONS_REPEATABILITY_AND_TAMPER_CHECK_OK')


if __name__ == '__main__':
    main()
