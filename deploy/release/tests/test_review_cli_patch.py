import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'cli'))
from ccheck.core import DeploymentError
from ccheck.fetch import asset_prefix
from ccheck.schema_migration import epoch, validate


class ReviewCliPatchTests(unittest.TestCase):
    def manifests(self):
        old = {'version': 'v0.1.0-rc.18', '_sha256': 'old', 'schema_files': {'0016_manual_graph_build.py': 'graph'},
               'files': {'models/model-manifest.json': 'models'}}
        old['schema_epoch'] = epoch(old['schema_files'])
        new = copy.deepcopy(old)
        new.update(version='v0.1.0-rc.19')
        new['schema_files']['0017_review_scan_progress.py'] = 'counters'
        new['schema_epoch'] = epoch(new['schema_files'])
        new['schema_migration'] = {'kind': 'additive-scan-progress', 'from_release_sha256': 'old',
            'from_epoch': old['schema_epoch'], 'to_epoch': new['schema_epoch'],
            'migration_sha256': 'counters', 'backup_required': True}
        return old, new

    def test_exact_counter_migration(self):
        old, new = self.manifests()
        validate(old, new)
        for mutate in (
            lambda n: n['schema_files'].update({'0016_manual_graph_build.py': 'changed'}),
            lambda n: n['schema_files'].update({'unexpected.py': 'new'}),
            lambda n: n['schema_migration'].update(backup_required=False),
            lambda n: n['schema_migration'].update(from_release_sha256='wrong'),
            lambda n: n['files'].update({'models/model-manifest.json': 'qwen3'}),
        ):
            target = copy.deepcopy(new)
            mutate(target)
            with self.assertRaises(DeploymentError):
                validate(old, target)
        with self.assertRaises(DeploymentError):
            validate(old, new, rollback=True)
        old['version'] = 'v0.1.0-rc.16'
        with self.assertRaises(DeploymentError):
            validate(old, new)

    def test_mixed_signed_asset_origins(self):
        manifest = {'version': 'v0.1.0-rc.19', 'distribution': {
            'kind': 'deployment-patch', 'base_version': 'v0.1.0-rc.13',
            'reused_files': ['images/frontend.part', 'images/qwen.part'],
            'asset_versions': {'images/frontend.part': 'v0.1.0-rc.18'}}}
        origin = 'https://github.com/owner/repo/releases/download'
        self.assertTrue(asset_prefix(manifest, 'images/frontend.part', origin).endswith('/release-v0.1.0-rc.18/'))
        self.assertTrue(asset_prefix(manifest, 'images/qwen.part', origin).endswith('/release-v0.1.0-rc.13/'))
        self.assertTrue(asset_prefix(manifest, 'images/backend.part', origin).endswith('/release-v0.1.0-rc.19/'))
        for value in ('https://evil.invalid/', 'v0.1.0-rc.19', None):
            manifest['distribution']['asset_versions']['images/frontend.part'] = value
            with self.assertRaises(DeploymentError):
                asset_prefix(manifest, 'images/frontend.part', origin)

    def test_asset_override_must_be_reused(self):
        with self.assertRaises(DeploymentError):
            asset_prefix({'version': 'v0.1.0-rc.19', 'distribution': {
                'asset_versions': {'cli/ccheck.pyz': 'v0.1.0-rc.18'}}}, 'cli/ccheck.pyz', 'https://example.com')


if __name__ == '__main__':
    unittest.main()
