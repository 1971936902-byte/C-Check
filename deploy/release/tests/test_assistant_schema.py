import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import assistant
from ccheck.core import DeploymentError
from ccheck.schema_migration import epoch, validate


class AssistantSchemaTests(unittest.TestCase):
    def manifests(self, version='v0.1.0-rc.20'):
        old = {'0017_review_scan_progress.py': 'existing'}
        new = dict(old, **assistant.migration_hashes())
        previous = {'version': version, '_sha256': 'trusted-base', 'schema_files': old,
            'schema_epoch': epoch(old), 'files': {'models/model-manifest.json': 'unchanged-models'}}
        target = {'version': 'v0.1.0-assistant-test', 'schema_files': new, 'schema_epoch': epoch(new),
            'files': previous['files'], 'assistant_runtime': assistant.hashes(), 'images': {'anythingllm': {}},
            'schema_migration': {'kind': 'additive-assistant-state', 'from_release_sha256': 'trusted-base',
                'from_epoch': epoch(old), 'to_epoch': epoch(new),
                'migration_sha256': assistant.migration_hashes(), 'backup_required': True}}
        return previous, target

    def test_only_known_additive_scripts_are_approved(self):
        for version in ('v0.1.0-rc.19', 'v0.1.0-rc.20'):
            with self.subTest(version=version):
                validate(*self.manifests(version))

    def test_changed_script_model_and_missing_backup_approval_rejected(self):
        previous, target = self.manifests()
        changed_script = copy.deepcopy(target)
        changed_script['schema_files'][assistant.MIGRATIONS[0]] = 'changed'
        changed_models = copy.deepcopy(target)
        changed_models['files']['models/model-manifest.json'] = 'other-model'
        no_backup = copy.deepcopy(target)
        no_backup['schema_migration']['backup_required'] = False
        changed_old = copy.deepcopy(target)
        changed_old['schema_files']['0017_review_scan_progress.py'] = 'rewritten'
        for candidate in (changed_script, changed_models, no_backup, changed_old):
            with self.subTest(candidate=candidate), self.assertRaises(DeploymentError):
                validate(previous, candidate)

    def test_rollback_and_unknown_baseline_rejected(self):
        with self.assertRaises(DeploymentError):
            validate(*self.manifests(), rollback=True)
        with self.assertRaises(DeploymentError):
            validate(*self.manifests('v0.1.0-rc.17'))
