"""Optional assistant recipe bound to the signed release and bundled runtime."""
from functools import lru_cache
import hashlib
import importlib.resources
import json
import os
from pathlib import Path, PurePosixPath
from types import ModuleType

from .core import DeploymentError, checked_path, digest


RESOURCES = (
    'qwen_assistant_gpu.py', 'vllm_control.py', 'guidance_compat.py',
    'embedding_assistant_gpu.py', 'embedding_control.py',
    'anythingllm-entrypoint.cjs', 'anythingllm.env.example',
    'render_compose.py', 'prepare_storage.py',
)
SERVICES = ('assistant-gateway', 'anythingllm', 'assistant-worker')
MIGRATIONS = ('0018_assistant_compute_gate.py', '0019_assistant_conversations.py',
              '0020_assistant_documents.py', '0021_assistant_control_checkpoint.py')


def settings(site):
    value = site.get('assistant', {})
    if not isinstance(value, dict) or set(value) - {'enabled', 'subnet'}:
        raise DeploymentError('assistant accepts only enabled and subnet')
    if type(value.get('enabled', False)) is not bool:
        raise DeploymentError('assistant.enabled must be a boolean')
    result = {'enabled': value.get('enabled', False), 'subnet': value.get('subnet', '172.30.10.192/26')}
    if result['enabled']:
        from . import acceleration, inference
        if not acceleration.gpu_embedding(site) or inference.served_name(site) != inference.LEGACY:
            raise DeploymentError('assistant requires the existing Qwen2.5 FP8 and GPU Jina recipe')
    return result


def enabled(site):
    return settings(site)['enabled']


def resource(name):
    if name not in RESOURCES:
        raise DeploymentError('unapproved assistant runtime resource')
    bundled = importlib.resources.files('ccheck').joinpath('assistant_runtime', name)
    if bundled.is_file():
        return bundled.read_bytes()
    # Source checkout only. Zipapp deployments must contain every resource.
    source = Path(__file__).resolve().parents[3] / 'assistant' / name
    if Path(__file__).is_file() and source.is_file():
        return source.read_bytes()
    raise DeploymentError('assistant runtime is missing from the release CLI')


def hashes():
    return {name: hashlib.sha256(resource(name)).hexdigest() for name in RESOURCES}


def migration_hashes():
    result = {}
    for name in MIGRATIONS:
        bundled = importlib.resources.files('ccheck').joinpath('assistant_migrations', name)
        source = Path(__file__).resolve().parents[4] / 'backend/alembic/versions' / name
        if bundled.is_file():
            value = bundled.read_bytes()
        elif Path(__file__).is_file() and source.is_file():
            value = source.read_bytes()
        else:
            raise DeploymentError('approved assistant migrations are missing from the release CLI')
        result[name] = hashlib.sha256(value).hexdigest()
    return result


def validate_release(site, release):
    if not enabled(site):
        return
    if release.get('assistant_runtime') != hashes() or 'anythingllm' not in release.get('images', {}):
        raise DeploymentError('assistant runtime/provider is not authorized by this signed release')


def runtime_relative():
    identity = hashlib.sha256(json.dumps(hashes(), sort_keys=True).encode()).hexdigest()
    return 'config/assistant-runtime/' + identity


def runtime_dir(site):
    return checked_path(site['data_dir'], runtime_relative())


@lru_cache(maxsize=2)
def helper(name):
    if name not in {'render_compose.py', 'prepare_storage.py'}:
        raise DeploymentError('unapproved assistant helper')
    module = ModuleType('ccheck_assistant_' + name[:-3])
    # These bytes are bundled with the CLI and checked against signed hashes.
    exec(compile(resource(name), '<bundled-assistant/' + name + '>', 'exec'), module.__dict__)
    return module


def prepare(site, uid, gid):
    if not enabled(site):
        return
    directory = runtime_dir(site)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in RESOURCES:
        path = checked_path(directory, name)
        expected = resource(name)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(fd, 'wb') as output:
                output.write(expected)
            path.chmod(0o444)
        elif path.read_bytes() != expected:
            raise DeploymentError('immutable assistant runtime was modified')
    helper('prepare_storage.py').prepare(Path(site['data_dir']), uid, gid)
    verify(site)


def verify(site):
    if not enabled(site):
        return
    directory = runtime_dir(site)
    for name, checksum in hashes().items():
        path = checked_path(directory, name)
        if not path.is_file() or digest(path) != checksum or path.stat().st_mode & 0o222:
            raise DeploymentError('signed assistant runtime verification failed')


def render(site, base, images):
    if not enabled(site):
        return base
    if 'anythingllm' not in images:
        raise DeploymentError('assistant provider image is missing')
    try:
        return helper('render_compose.py').render(base, backend_image=images['backend'],
            frontend_image=images['frontend'], anythingllm_image=images['anythingllm'],
            data_dir=site['data_dir'], runtime_dir=str(PurePosixPath(site['data_dir']) / runtime_relative()),
            subnet=settings(site)['subnet'])
    except ValueError as error:
        raise DeploymentError(str(error)) from error


def image_for_service(service):
    return 'backend' if service in {'api', 'worker', 'init', 'assistant-gateway', 'assistant-worker'} else service
