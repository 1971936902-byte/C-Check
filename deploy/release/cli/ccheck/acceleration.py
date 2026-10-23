"""Opt-in shared GPU recipe with immutable, signed launcher resources."""
import hashlib
import importlib.resources
import os
from pathlib import Path

from .core import DeploymentError, checked_path, digest, gpu_memory_fraction


def settings(site):
    value = site.get('performance')
    if value is None:
        return None
    defaults = {'embedding_device': 'cuda', 'qwen_gpu_memory_utilization': 0.82,
                'max_num_seqs': 2, 'max_num_batched_tokens': 8192,
                'model_chunk_concurrency': 2, 'embedding_batch_size': 1,
                'embedding_token_budget': 8192}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise DeploymentError('unknown performance setting')
    result = dict(defaults, **value)
    if result['embedding_device'] not in {'cuda', 'cpu'}:
        raise DeploymentError('embedding_device must be cuda or cpu')
    if result['embedding_device'] == 'cuda' and site.get('profile') != 'ada-48g-fp8':
        raise DeploymentError('shared GPU embedding currently requires the validated-recipe target ada-48g-fp8; 40GB profiles are not supported')
    fraction = result['qwen_gpu_memory_utilization']
    if type(fraction) not in {float, int} or not 0.75 <= fraction <= 0.90:
        raise DeploymentError('Qwen GPU fraction must be between 0.75 and 0.90')
    if result['embedding_device'] == 'cuda' and fraction > 0.84:
        raise DeploymentError('shared GPU recipe reserves at least 16 percent for Jina and overhead')
    for key, low, high in [('max_num_seqs', 1, 2), ('max_num_batched_tokens', 4096, 8192),
                           ('model_chunk_concurrency', 1, 2), ('embedding_batch_size', 1, 2),
                           ('embedding_token_budget', 512, 8192)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise DeploymentError('out of range performance setting: ' + key)
    if result['model_chunk_concurrency'] > result['max_num_seqs']:
        raise DeploymentError('chunk concurrency must not exceed max_num_seqs')
    return result


def memory_fraction(site):
    value = settings(site)
    return value['qwen_gpu_memory_utilization'] if value else gpu_memory_fraction(site['profile'])


def gpu_embedding(site):
    value = settings(site)
    return bool(value and value['embedding_device'] == 'cuda')


def validate_release(site, release):
    if settings(site):
        expected = {name: hashlib.sha256(resource(name)).hexdigest()
                    for name in launcher_names(site)}
        if release.get('runtime_launchers') != expected:
            raise DeploymentError('performance recipe is not authorized by this signed release; use the matching release CLI')


def resource(name):
    if name not in {'qwen_gpu.py', 'qwen3_gpu.py', 'embedding_gpu.py'}:
        raise DeploymentError('unapproved runtime launcher')
    return importlib.resources.files('ccheck').joinpath('launchers', name).read_bytes()


def qwen_launcher(site):
    from .inference import QWEN3, served_name
    return 'qwen3_gpu.py' if served_name(site) == QWEN3 else 'qwen_gpu.py'


def launcher_names(site):
    return (qwen_launcher(site), 'embedding_gpu.py')


def launcher_path(site, name):
    checksum = hashlib.sha256(resource(name)).hexdigest()
    return checked_path(site['data_dir'], 'config/launchers/' + checksum + '-' + name)


def prepare(site):
    if not settings(site):
        return
    directory = checked_path(site['data_dir'], 'config/launchers')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in launcher_names(site):
        path = launcher_path(site, name)
        expected = resource(name)
        if path.exists():
            if path.read_bytes() != expected:
                raise DeploymentError('immutable runtime launcher was modified')
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(fd, 'wb') as output:
                output.write(expected)
            path.chmod(0o444)
    verify(site)


def verify(site):
    if not settings(site):
        return
    for name in launcher_names(site):
        path = launcher_path(site, name)
        if not path.is_file() or digest(path) != hashlib.sha256(resource(name)).hexdigest():
            raise DeploymentError('signed runtime launcher verification failed')
        if path.stat().st_mode & 0o222:
            raise DeploymentError('runtime launcher must be read-only')
