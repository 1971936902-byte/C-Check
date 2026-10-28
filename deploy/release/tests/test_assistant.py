import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import assistant, networks
from ccheck.core import DeploymentError
from ccheck.engine import render_compose
from ccheck.lifecycle import start_stack


class AssistantDeploymentTests(unittest.TestCase):
    def test_source_inventory_includes_assistant_and_detects_resource_changes(self):
        from repack_deployment import source_files, tree_hash
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            resource = root / 'deploy/assistant/render_compose.py'
            resource.parent.mkdir(parents=True)
            resource.write_text('original')
            cache = resource.parent / '__pycache__'
            cache.mkdir()
            (cache / 'ignored.pyc').write_bytes(b'cache')
            before = source_files(root)
            self.assertEqual(set(before), {'deploy/assistant/render_compose.py'})
            resource.write_text('changed')
            self.assertNotEqual(tree_hash(before), tree_hash(source_files(root)))

    def setUp(self):
        self.site = {'instance': 'assistant-test', 'data_dir': '/srv/c-check', 'gpu': '0',
            'profile': 'ada-48g-fp8', 'gpu_access_mode': 'cdi', 'performance': {},
            'assistant': {'enabled': True},
            'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}
        self.images = {name: 'sha256:' + 'a' * 64 for name in
            ('qwen', 'embedding', 'backend', 'mysql', 'redis', 'qdrant', 'frontend', 'anythingllm')}

    def test_disabled_recipe_preserves_legacy_service_set(self):
        site = copy.deepcopy(self.site)
        del site['assistant']
        document = render_compose(site, self.images, 10001, 10001)
        self.assertEqual(set(document['services']),
            {'mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'api', 'worker', 'frontend', 'init'})
        self.assertEqual(set(document['networks']), set(networks.DEFAULT_SUBNETS))

    def test_recipe_repeats_exactly_and_keeps_models_and_data_images(self):
        before = copy.deepcopy(self.site)
        first = render_compose(self.site, self.images, 10001, 10001)
        self.assertEqual(first, render_compose(self.site, self.images, 10001, 10001))
        self.assertEqual(self.site, before)
        self.assertEqual(set(first['networks']), {*networks.DEFAULT_SUBNETS, 'assistant'})
        self.assertTrue(first['networks']['assistant']['internal'])
        for name in ('mysql', 'redis', 'qdrant', 'qwen'):
            self.assertEqual(first['services'][name]['image'], self.images[name])
        self.assertEqual(first['services']['embedding']['image'], self.images['qwen'])
        for name in assistant.SERVICES:
            self.assertNotIn('ports', first['services'][name])
            self.assertNotIn('devices', first['services'][name])
        self.assertTrue(first['services']['anythingllm']['healthcheck'])

    def test_signed_resources_and_provider_are_required(self):
        release = {'assistant_runtime': assistant.hashes(), 'images': self.images}
        assistant.validate_release(self.site, release)
        for invalid in ({}, dict(release, assistant_runtime={}), dict(release, images={})):
            with self.subTest(invalid=invalid), self.assertRaises(DeploymentError):
                assistant.validate_release(self.site, invalid)

    def test_subnet_conflicts_and_invalid_enablement_rejected(self):
        self.site['assistant']['subnet'] = networks.DEFAULT_SUBNETS['data']
        with self.assertRaisesRegex(DeploymentError, 'overlap'):
            networks.subnets(self.site)
        self.site['assistant'] = {'enabled': 'true'}
        with self.assertRaises(DeploymentError):
            assistant.settings(self.site)
        self.site['assistant'] = {'enabled': True}
        self.site['performance'] = {'embedding_device': 'cpu'}
        with self.assertRaisesRegex(DeploymentError, 'GPU Jina'):
            assistant.settings(self.site)

    def test_start_waits_for_provider_and_gateway_before_public_services(self):
        engine = MagicMock()
        engine.site = self.site
        with patch('ccheck.lifecycle.acceleration.verify'), patch('ccheck.lifecycle.assistant.verify'), \
                patch('ccheck.lifecycle.networks.verify'):
            start_stack(engine)
        calls = [call.args for call in engine.compose.call_args_list]
        self.assertLess(calls.index(('up', '-d', *assistant.SERVICES)), calls.index(('up', '-d', 'api', 'worker')))
        self.assertIn(unittest.mock.call(list(assistant.SERVICES)), engine.wait.call_args_list)

    def test_bundled_runtime_is_complete_and_detects_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.site['data_dir'] = temporary
            with patch.object(assistant.helper('prepare_storage.py'), 'prepare'):
                assistant.prepare(self.site, 10001, 10001)
            assistant.verify(self.site)
            path = assistant.runtime_dir(self.site) / 'guidance_compat.py'
            path.chmod(0o644)
            path.write_text('changed')
            with self.assertRaises(DeploymentError):
                assistant.verify(self.site)
            with self.assertRaises(DeploymentError):
                assistant.prepare(self.site, 10001, 10001)
            path.chmod(0o644)

    def test_built_zipapp_contains_runtime_independent_of_source_tree(self):
        import yaml
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'ccheck.pyz'
            builder = Path(__file__).resolve().parents[1] / 'build_cli.py'
            subprocess.run([sys.executable, str(builder), '--output', str(output),
                            '--yaml-package', str(Path(yaml.__file__).parent), '--with-assistant'], check=True, capture_output=True)
            result = subprocess.run([sys.executable, '-I', '-c',
                'import sys,json;sys.path.insert(0,sys.argv[1]);from ccheck.assistant import hashes,migration_hashes;print(json.dumps([hashes(),migration_hashes()]))',
                str(output)], check=True, text=True, capture_output=True, cwd=temporary)
            self.assertEqual(json.loads(result.stdout), [assistant.hashes(), assistant.migration_hashes()])


if __name__ == '__main__':
    unittest.main()
