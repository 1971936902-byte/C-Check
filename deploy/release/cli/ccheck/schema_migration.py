"""Allow only explicitly approved, signed additive schema transitions."""
import hashlib
import json

from .core import DeploymentError


def epoch(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validate(previous, target, *, rollback=False):
    old, new = previous.get('schema_files', {}), target.get('schema_files', {})
    approval = target.get('schema_migration', {})
    transitions = {
        ('v0.1.0-rc.16', 'v0.1.0-rc.18'): ('0016_manual_graph_build.py', 'additive-graph-state'),
        ('v0.1.0-rc.18', 'v0.1.0-rc.19'): ('0017_review_scan_progress.py', 'additive-scan-progress'),
    }
    transition = transitions.get((previous.get('version'), target.get('version')))
    if transition is None:
        from . import assistant
        if (previous.get('version') not in {'v0.1.0-rc.19', 'v0.1.0-rc.20'}
                or target.get('version') == previous.get('version')
                or approval.get('kind') != 'additive-assistant-state'
                or target.get('assistant_runtime') != assistant.hashes()
                or 'anythingllm' not in target.get('images', {})):
            raise DeploymentError('schema transition is not approved')
        added = set(assistant.MIGRATIONS)
        expected_migrations = assistant.migration_hashes()
        if any(new.get(name) != checksum for name, checksum in expected_migrations.items()):
            raise DeploymentError('assistant migration differs from the approved release CLI')
        kind = 'additive-assistant-state'
        migration_checksum = expected_migrations
    else:
        name, kind = transition
        added = {name}
        migration_checksum = new.get(name)
    if (rollback
            or not old or not new or set(new) - set(old) != added
            or any(new.get(name) != value for name, value in old.items())
            or previous.get('schema_epoch') != epoch(old) or target.get('schema_epoch') != epoch(new)
            or approval != {'kind': kind, 'from_release_sha256': previous['_sha256'],
                            'from_epoch': previous['schema_epoch'], 'to_epoch': target['schema_epoch'],
                            'migration_sha256': migration_checksum, 'backup_required': True}):
        raise DeploymentError('schema compatibility is not proven; use an approved migration and backup restore')
    if previous['files']['models/model-manifest.json'] != target['files']['models/model-manifest.json']:
        raise DeploymentError('schema and model changes cannot be combined in this patch')
