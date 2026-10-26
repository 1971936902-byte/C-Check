import copy
import contextlib
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

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

    @contextlib.contextmanager
    def update_fixture(self, failure=False):
        from ccheck.lifecycle import update
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'state').mkdir()
            site = {'data_dir': str(root), 'instance': 'test', 'web': {'public_url': 'http://localhost'}}
            (root / 'config/site.json').write_text(json.dumps(site))
            document = root / 'state/compose.json'
            document.write_text(json.dumps({'networks': {}}))
            old, new = self.manifests()
            new.update(_sha256='target', _root='/new', tag='release-v0.1.0-rc.18', source={})
            state = dict(old, publisher='trusted', release_root='/old', release_sha256='base',
                         model_manifest_sha256='models', uid=10001, gid=10001, images={'backend': 'old'})
            engine = MagicMock()
            engine.file, engine.root = document, root
            events = []
            patches = {
                'ccheck.lifecycle.installed': {'return_value': state},
                'ccheck.lifecycle.verify_release': {'side_effect': [new, old]},
                'ccheck.lifecycle.preflight': {}, 'ccheck.lifecycle.networks.preflight': {},
                'ccheck.lifecycle.installation_lock': {'return_value': contextlib.nullcontext()},
                'ccheck.lifecycle.Engine': {'return_value': engine},
                'ccheck.lifecycle.networks.specifications': {'return_value': {}},
                'ccheck.lifecycle.networks.needs_recreate': {'return_value': False},
                'ccheck.backup.make_backup': {'side_effect': lambda *a, **k: events.append('backup')},
                'ccheck.lifecycle.cache_release': {'return_value': new},
                'ccheck.lifecycle.prepare_images': {'return_value': {'backend': 'new'}},
                'ccheck.lifecycle.prepare_models': {}, 'ccheck.lifecycle.fix_model_permissions': {},
                'ccheck.lifecycle.prepare_data': {},
                'ccheck.lifecycle.render_compose': {'return_value': {'networks': {}}},
                'ccheck.lifecycle.start_stack': {'side_effect': lambda *a: events.append('start')},
                'ccheck.lifecycle.acceptance': {'side_effect': RuntimeError('failure')} if failure else {'return_value': {}},
                'ccheck.image_identity.identity': {'return_value': 'identity'},
                'ccheck.image_identity.inspect_image': {'return_value': {}},
            }
            for name, values in patches.items():
                stack.enter_context(patch(name, **values))
            yield lambda: update(site, Path('/new'), backup_key=Path('/external-key')), events, engine, root, new

    def test_schema_update_backs_up_and_records_new_epoch(self):
        with self.update_fixture() as (run, events, engine, root, target):
            run()
            self.assertEqual(events, ['backup', 'start'])
            self.assertEqual(json.loads((root / 'state/deployment.json').read_text())['schema_epoch'], target['schema_epoch'])
            self.assertEqual(engine.drain.call_count, 2)

    def test_schema_failure_stops_writers_without_restarting_old_images(self):
        with self.update_fixture(failure=True) as (run, events, engine, root, _):
            with self.assertRaisesRegex(DeploymentError, 'schema upgrade acceptance failed'):
                run()
            self.assertEqual(events, ['backup', 'start'])
            engine.maintenance.assert_called_with(True)
            engine.compose.assert_called_with('stop', '--timeout', '120', 'api', 'worker')
            self.assertFalse((root / 'state/deployment.json').exists())

