import importlib.util
from pathlib import Path
import sys
from copy import deepcopy

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/release/cli'))
from ccheck.engine import render_compose

spec = importlib.util.spec_from_file_location('assistant_compose_test', ROOT / 'deploy/assistant/render_compose.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def base():
    site = {'instance': 'test', 'data_dir': '/srv/c-check', 'gpu': '0', 'profile': 'ada-48g-fp8',
            'gpu_access_mode': 'cdi', 'performance': {},
            'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}
    images = {name: 'sha256:' + str(index) * 64 for index, name in enumerate(
        ['qwen', 'embedding', 'backend', 'mysql', 'redis', 'qdrant', 'frontend'])}
    return render_compose(site, images, 10001, 10001)


def render(base, **overrides):
    return module.render(base, **({'backend_image': 'sha256:' + 'a' * 64,
        'frontend_image': 'sha256:' + 'b' * 64, 'anythingllm_image': 'example/anythingllm@sha256:' + 'c' * 64,
        'data_dir': '/srv/c-check', 'runtime_dir': '/srv/c-check/assistant'} | overrides))


def test_render_preserves_original_and_existing_models_and_data(base):
    original = deepcopy(base)
    result = render(base)
    assert base == original
    for name in ('mysql', 'redis', 'qdrant'):
        assert result['services'][name] == original['services'][name]
    assert result['services']['embedding']['image'] == original['services']['embedding']['image']
    assert result['services']['embedding']['devices'] == original['services']['embedding']['devices']
    assert result['services']['embedding']['entrypoint'] == ['python', '/opt/assistant/embedding_assistant_gpu.py']
    assert result['services']['qwen']['image'] == original['services']['qwen']['image']
    assert result['services']['qwen']['devices'] == original['services']['qwen']['devices']
    assert all(volume in result['services']['qwen']['volumes'] for volume in original['services']['qwen']['volumes'])


def test_new_services_are_private_cpu_only_and_hardened(base):
    result = render(base)
    for name in ('anythingllm', 'assistant-gateway', 'assistant-worker'):
        service = result['services'][name]
        assert 'ports' not in service and 'devices' not in service
        assert service['runtime'] == 'runc'
        assert service['read_only'] and service['cap_drop'] == ['ALL']
        assert service['user'] == '10001:10001'
    assert result['services']['anythingllm']['networks'] == ['assistant']
    assert result['networks']['assistant']['internal']
    assert 'inference' not in result['services']['assistant-worker']['networks']
    assert '--queues=assistant' in result['services']['assistant-worker']['command']
    assert '--queues=celery' in result['services']['worker']['command']
    assert 'assistant_control_key' not in result['services']['api']['secrets']
    assert 'assistant_anythingllm_api_key' not in result['services']['worker']['secrets']


def test_mutable_images_and_overlapping_subnet_are_rejected(base):
    with pytest.raises(ValueError, match='immutable'):
        render(base, anythingllm_image='anythingllm:latest')
    base['networks']['data']['ipam'] = {'config': [{'subnet': '172.30.10.0/24'}]}
    with pytest.raises(ValueError, match='overlaps'):
        render(base)


def test_model_migration_is_not_accepted(base):
    base['services']['qwen']['environment']['CCHECK_REVIEW_MODEL'] = 'qwen3-a3b'
    with pytest.raises(ValueError, match='Qwen2.5'):
        render(base)
