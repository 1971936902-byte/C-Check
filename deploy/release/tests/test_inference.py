import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import acceleration, inference, model_migration
from ccheck.core import DeploymentError
from ccheck.engine import render_compose
from ccheck.image_identity import identity, locate
from ccheck.models import prepare_models
import hashlib


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.site = {'data_dir': str(self.root / 'data'), 'profile': 'ada-48g-fp8',
                     'instance': 'qwen-test', 'gpu': '0', 'gpu_access_mode': 'cdi',
                     'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}

    def release(self, name, identifier):
        root = self.root / identifier
        (root / 'models').mkdir(parents=True)
        repository = ('Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8' if name == inference.QWEN3
                      else 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8')
        (root / 'models/model-manifest.json').write_text(json.dumps({'models': [
            {'key': 'qwen', 'repository': repository, 'revision': identifier},
            {'key': 'jina', 'repository': 'jinaai/jina-code-embeddings-1.5b', 'revision': 'unchanged'}]}))
        return {'_root': str(root), 'schema_epoch': 1,
                'inference': {'served_model': name},
                'files': {'models/model-manifest.json': identifier * 64, 'core.whl': 'c' * 64},
                'core_wheel': {'file': 'core.whl'},
                'images': {key: {'id': 'sha256:' + 'e' * 64} for key in
                           ('backend', 'qwen', 'frontend', 'embedding', 'mysql', 'redis', 'qdrant')}}

    def test_legacy_paths_and_launcher_remain_unchanged(self):
        release = self.release(inference.LEGACY, 'a')
        inference.configure(self.site, release)
        self.assertNotIn('qwen_model_revision', self.site)
        self.assertEqual(inference.directory(self.site, 'qwen'), self.root / 'data/models/qwen')
        self.assertEqual(acceleration.qwen_launcher(self.site), 'qwen_gpu.py')
        images = {key: value['id'] for key, value in release['images'].items()}
        document = render_compose(self.site, images, 10001, 10001)
        self.assertNotIn('CCHECK_REVIEW_MODEL', document['services']['api']['environment'])
        self.assertNotIn('CCHECK_REVIEW_MODEL', document['services']['qwen']['environment'])

    def test_imported_untagged_image_is_found_by_signed_execution_identity(self):
        metadata = {'Id': 'sha256:' + 'b' * 64, 'Os': 'linux', 'Architecture': 'amd64',
                    'RootFS': {'Type': 'layers', 'Layers': ['sha256:' + 'c' * 64]}, 'Config': {}}
        specification = {'id': 'sha256:' + 'a' * 64, 'filesystem_config_sha256': identity(metadata)}
        def inspect(reference):
            if reference == specification['id']:
                raise DeploymentError('OCI index ID absent in legacy store')
            return metadata
        with patch('ccheck.image_identity.inspect_image', side_effect=inspect), \
                patch('ccheck.image_identity.run', return_value=metadata['Id']) as run:
            self.assertEqual(locate(specification), metadata['Id'])
            self.assertIn('--all', run.call_args.args[0])

    def test_qwen3_storage_is_disjoint_from_legacy_and_jina(self):
        release = self.release(inference.QWEN3, 'b')
        inference.configure(self.site, release)
        self.assertEqual(inference.directory(self.site, 'qwen'), self.root / ('data/models/qwen-revisions/' + 'b' * 64))
        self.assertEqual(inference.directory(self.site, 'jina'), self.root / 'data/models/jina')
        self.assertEqual(acceleration.qwen_launcher(self.site), 'qwen3_gpu.py')
        images = {key: value['id'] for key, value in release['images'].items()}
        document = render_compose(self.site, images, 10001, 10001)
        for name in ('api', 'worker', 'init', 'qwen'):
            self.assertEqual(document['services'][name]['environment']['CCHECK_REVIEW_MODEL'], inference.QWEN3)
        model_mount = document['services']['qwen']['volumes'][0]
        self.assertEqual(model_mount['source'], str(inference.directory(self.site, 'qwen')))
        self.assertTrue(model_mount['read_only'])

    def test_explicit_model_and_storage_mismatch_rejected(self):
        release = self.release(inference.QWEN3, 'b')
        for field, value in [('review_model', inference.LEGACY), ('qwen_model_revision', 'a' * 64)]:
            with self.subTest(field=field), self.assertRaises(DeploymentError):
                inference.configure(dict(self.site, **{field: value}), release)
        for value in ('../escape', 'g' * 64, 3):
            with self.assertRaises(DeploymentError):
                inference.directory(dict(self.site, qwen_model_revision=value), 'qwen')

    def test_rollback_configuration_restores_original_directory(self):
        inference.configure(self.site, self.release(inference.QWEN3, 'b'))
        inference.configure(self.site, self.release(inference.LEGACY, 'a'), transition=True)
        self.assertNotIn('review_model', self.site)
        self.assertEqual(inference.directory(self.site, 'qwen'), self.root / 'data/models/qwen')

    def test_qwen3_requires_validated_image_not_legacy_runtime_build(self):
        with self.assertRaisesRegex(DeploymentError, 'pinned runtime image'):
            inference.configure(dict(self.site, runtime_source='build'), self.release(inference.QWEN3, 'b'))

    def test_prefetch_new_revision_preserves_legacy_and_active_audit(self):
        root = Path(self.site['data_dir'])
        for relative, content in [('models/qwen/config.json', b'old-qwen'),
                                  ('models/jina/config.json', b'unchanged-jina'),
                                  ('state/models.json', b'old-active-audit')]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        cache = self.root/'new-qwen-cache'
        cache.mkdir()
        (cache/'config.json').write_bytes(b'new-qwen')
        release = self.release(inference.QWEN3, 'b')
        rows = []
        for key, content in [('qwen', b'new-qwen'), ('jina', b'unchanged-jina')]:
            rows.append({'key':key, 'repository':key, 'revision':'fixture', 'files':[
                {'path':'config.json', 'sha256':hashlib.sha256(content).hexdigest(),
                 'size':len(content), 'url':'https://example.invalid/not-used'}]})
        (Path(release['_root'])/'models/model-manifest.json').write_text(json.dumps({'models':rows}))
        inference.configure(self.site, release)
        self.site.update(model_paths={'qwen':str(cache)}, model_download_policy='offline')
        with patch('ccheck.models.validate_model_layout'):
            prepare_models(release, self.site, persist=False)
        self.assertEqual((root/'models/qwen/config.json').read_bytes(), b'old-qwen')
        self.assertEqual((inference.directory(self.site,'qwen')/'config.json').read_bytes(), b'new-qwen')
        self.assertEqual((root/'models/jina/config.json').read_bytes(), b'unchanged-jina')
        self.assertEqual((root/'state/models.json').read_bytes(), b'old-active-audit')

    def test_qwen_only_transition_and_reverse_are_allowed(self):
        old, new = self.release(inference.LEGACY, 'a'), self.release(inference.QWEN3, 'b')
        model_migration.validate(old, new)
        model_migration.validate(new, old)

    def test_other_images_core_and_schema_changes_are_rejected(self):
        old, new = self.release(inference.LEGACY, 'a'), self.release(inference.QWEN3, 'b')
        for mutate in [lambda r: r.update(schema_epoch=2),
                       lambda r: r['files'].update({'core.whl': 'd' * 64}),
                       lambda r: r['images']['embedding'].update(id='different')]:
            candidate = copy.deepcopy(new)
            mutate(candidate)
            with self.assertRaises(DeploymentError):
                model_migration.validate(old, candidate)

    def test_embedding_revision_or_repository_spoof_is_rejected(self):
        old, new = self.release(inference.LEGACY, 'a'), self.release(inference.QWEN3, 'b')
        path = Path(new['_root']) / 'models/model-manifest.json'
        original = json.loads(path.read_text())
        for index, field, value in [(1, 'revision', 'changed'), (0, 'repository', 'untrusted/model')]:
            data = copy.deepcopy(original)
            data['models'][index][field] = value
            path.write_text(json.dumps(data))
            with self.assertRaises(DeploymentError):
                model_migration.validate(old, new)


if __name__ == '__main__':
    unittest.main()
