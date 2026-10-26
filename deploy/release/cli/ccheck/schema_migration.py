"""Allow only the signed rc16 -> rc18 additive graph-state migration."""
import hashlib
import json

from .core import DeploymentError


def epoch(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validate(previous, target, *, rollback=False):
    old, new = previous.get('schema_files', {}), target.get('schema_files', {})
    approval = target.get('schema_migration', {})
    added = '0016_manual_graph_build.py'
    if (rollback or previous.get('version') != 'v0.1.0-rc.16'
            or target.get('version') != 'v0.1.0-rc.18'
            or not old or not new or set(new) - set(old) != {added}
            or any(new.get(name) != value for name, value in old.items())
            or previous.get('schema_epoch') != epoch(old) or target.get('schema_epoch') != epoch(new)
            or approval != {'kind': 'additive-graph-state', 'from_release_sha256': previous['_sha256'],
                            'from_epoch': previous['schema_epoch'], 'to_epoch': target['schema_epoch'],
                            'migration_sha256': new[added], 'backup_required': True}):
        raise DeploymentError('schema compatibility is not proven; use an approved migration and backup restore')
    if previous['files']['models/model-manifest.json'] != target['files']['models/model-manifest.json']:
        raise DeploymentError('schema and model changes cannot be combined in this patch')
