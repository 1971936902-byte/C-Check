"""Explicit Qwen-only transitions; embedding/index migrations remain prohibited."""
import json

from .core import DeploymentError, checked_path
from .inference import LEGACY, QWEN3


def models(release):
    rows = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())['models']
    result = {row['key']: row for row in rows}
    if len(result) != len(rows) or set(result) != {'qwen', 'jina'}:
        raise DeploymentError('Qwen migration requires exactly the qwen and jina manifests')
    return result


def validate(previous, target):
    old, new = models(previous), models(target)
    if old['jina'] != new['jina']:
        raise DeploymentError('embedding/index migration is not supported by Qwen-only migration')
    repositories = {LEGACY: 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8',
                    QWEN3: 'Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8'}
    for release, rows in [(previous, old), (target, new)]:
        name = release.get('inference', {}).get('served_model', LEGACY)
        if name not in repositories or rows['qwen']['repository'] != repositories[name]:
            raise DeploymentError('signed review model and Qwen repository disagree')
    if previous['schema_epoch'] != target['schema_epoch']:
        raise DeploymentError('Qwen migration cannot change the database schema epoch')
    if previous['files'].get('core-policy.json') != target['files'].get('core-policy.json'):
        raise DeploymentError('Qwen migration cannot change the compiled core policy')
    for release in (previous, target):
        if release['files'][release['core_wheel']['file']] != previous['files'][previous['core_wheel']['file']]:
            raise DeploymentError('Qwen-only migration requires unchanged compiled core wheel')
    for name in ('frontend', 'embedding', 'mysql', 'redis', 'qdrant'):
        if (previous['images'][name]['id'] != target['images'][name]['id']
                or previous['images'][name].get('filesystem_config_sha256') != target['images'][name].get('filesystem_config_sha256')):
            raise DeploymentError('Qwen-only migration cannot replace unrelated images: ' + name)
