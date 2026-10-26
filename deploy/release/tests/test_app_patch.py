import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError
from ccheck.schema_migration import epoch, validate


class AppPatchTests(unittest.TestCase):
    def manifests(self):
        old = {'version': 'v0.1.0-rc.16', '_sha256': 'base', 'schema_files': {'0015.py': 'old'},
               'files': {'models/model-manifest.json': 'models'}}
        old['schema_epoch'] = epoch(old['schema_files'])
        new = copy.deepcopy(old)
        new['version'] = 'v0.1.0-rc.18'
        new['schema_files']['0016_manual_graph_build.py'] = 'new'
        new['schema_epoch'] = epoch(new['schema_files'])
        new['schema_migration'] = {'kind': 'additive-graph-state', 'from_release_sha256': 'base',
            'from_epoch': old['schema_epoch'], 'to_epoch': new['schema_epoch'],
            'migration_sha256': 'new', 'backup_required': True}
        return old, new

    def test_exact_additive_migration_only(self):
        old, new = self.manifests()
        validate(old, new)
        for mutate in [lambda n: n['schema_files'].update({'0015.py': 'changed'}),
                       lambda n: n['schema_files'].update({'extra.py': 'extra'}),
                       lambda n: n.update(version='v0.1.0-rc.19'),
                       lambda n: n['schema_migration'].update(backup_required=False),
                       lambda n: n['schema_migration'].update(from_release_sha256='wrong'),
                       lambda n: n['files'].update({'models/model-manifest.json': 'other'})]:
            changed = copy.deepcopy(new)
            mutate(changed)
            with self.assertRaises(DeploymentError):
                validate(old, changed)
        with self.assertRaises(DeploymentError):
            validate(old, new, rollback=True)

