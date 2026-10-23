import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import acceleration
from ccheck.core import DeploymentError
from ccheck.engine import render_compose, Engine
from ccheck.lifecycle import start_stack
from ccheck.__main__ import main


class AccelerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = {'instance': 'gpu-test', 'data_dir': self.tmp.name, 'gpu': '0',
            'profile': 'ada-48g-fp8', 'gpu_access_mode': 'cdi', 'performance': {},
            'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}
        self.images = {key: 'sha256:' + str(index) * 64 for index, key in enumerate(
            ['qwen', 'embedding', 'backend', 'mysql', 'redis', 'qdrant', 'frontend'])}

    def test_bounded_recipe_and_legacy_defaults(self):
        self.assertEqual(acceleration.settings(self.site)['max_num_seqs'], 2)
        self.assertEqual(acceleration.memory_fraction(self.site), 0.82)
        del self.site['performance']
        self.assertIsNone(acceleration.settings(self.site))
        self.assertEqual(acceleration.memory_fraction(self.site), 0.88)

    def test_invalid_and_overcommitted_settings_fail_closed(self):
        for value in [None, {'unexpected': 1}, {'embedding_device': 'auto'},
                      {'qwen_gpu_memory_utilization': 0.94}, {'max_num_seqs': 3},
                      {'embedding_batch_size': True}, {'max_num_seqs': 1, 'model_chunk_concurrency': 2}]:
            if value is None:
                value = []
            self.site['performance'] = value
            with self.subTest(value=value), self.assertRaises(DeploymentError):
                acceleration.settings(self.site)

    def test_cuda_recipe_reuses_qwen_image_and_retains_single_device_isolation(self):
        doc = render_compose(self.site, self.images, 10001, 10001)
        gpu = doc['services']['embedding']
        self.assertEqual(gpu['image'], self.images['qwen'])
        for name in ['qwen', 'api', 'embedding']:
            spec = doc['services'][name]
            self.assertEqual(spec['devices'], ['nvidia.com/gpu=0'])
            self.assertTrue(spec['read_only'])
            self.assertEqual(spec['user'], '10001:10001')
            self.assertEqual(spec['cap_drop'], ['ALL'])
            self.assertEqual(spec['environment']['NVIDIA_VISIBLE_DEVICES'], 'void')
        self.assertEqual(doc['services']['worker']['environment']['MODEL_CHUNK_CONCURRENCY'], '2')
        self.assertNotIn('devices', doc['services']['worker'])
        self.assertEqual(doc['services']['qwen']['environment']['MAX_NUM_BATCHED_TOKENS'], '8192')
        self.assertTrue(all(v['read_only'] for v in gpu['volumes']))

    def test_cpu_fallback_has_no_cuda_device_or_gpu_image(self):
        self.site['performance'] = {'embedding_device': 'cpu'}
        doc = render_compose(self.site, self.images, 10001, 10001)
        self.assertEqual(doc['services']['embedding']['image'], self.images['embedding'])
        self.assertNotIn('devices', doc['services']['embedding'])

    def test_immutable_launchers_detect_tamper_and_are_content_addressed(self):
        acceleration.prepare(self.site)
        acceleration.verify(self.site)
        path = acceleration.launcher_path(self.site, 'qwen_gpu.py')
        self.assertIn(hashlib.sha256(acceleration.resource('qwen_gpu.py')).hexdigest(), path.name)
        path.chmod(0o644)
        path.write_bytes(b'tamper')
        with self.assertRaises(DeploymentError):
            acceleration.verify(self.site)
        with self.assertRaises(DeploymentError):
            acceleration.prepare(self.site)

    def test_old_release_cannot_authorize_new_runtime(self):
        with self.assertRaises(DeploymentError):
            acceleration.validate_release(self.site, {})
        manifest = {'runtime_launchers': {name: hashlib.sha256(acceleration.resource(name)).hexdigest()
                    for name in ['qwen_gpu.py', 'embedding_gpu.py']}}
        acceleration.validate_release(self.site, manifest)

    def test_40gb_profile_cannot_silently_enable_48gb_shared_recipe(self):
        self.site['profile'] = 'a100-40g-fp8'
        with self.assertRaisesRegex(DeploymentError, '40GB'):
            acceleration.settings(self.site)

    def test_qwen_reservation_precedes_jina_loading(self):
        engine = MagicMock()
        engine.site = self.site
        with patch('ccheck.lifecycle.acceleration.verify'), patch('ccheck.lifecycle.networks.verify'):
            start_stack(engine)
        calls = [call.args for call in engine.compose.call_args_list]
        self.assertLess(calls.index(('stop', '--timeout', '120', 'qwen', 'embedding')),
                        calls.index(('up', '-d', 'qwen')))
        self.assertLess(calls.index(('up', '-d', 'qwen')), calls.index(('up', '-d', 'embedding')))
        self.assertIn(unittest.mock.call(['qwen']), engine.wait.call_args_list)

    def test_explicit_update_profile_applies_after_saved_site_is_loaded(self):
        root = Path(self.site['data_dir'])
        (root / 'config').mkdir()
        (root / 'config/site.json').write_text('{}')
        old = copy.deepcopy(self.site)
        old.pop('performance')
        for profile, enabled in [('shared-gpu', True), ('cpu-serial', False)]:
            args = ['ccheck', 'update', '--config', str(root / 'external.json'),
                    '--backup-key', str(root / 'backup.key'), '--performance-profile', profile]
            with patch.object(sys, 'argv', args), patch('ccheck.__main__.load_site',
                    side_effect=[copy.deepcopy(self.site), copy.deepcopy(old)]), \
                    patch('ccheck.__main__.lifecycle.update') as update:
                self.assertEqual(main(), 0)
                site = update.call_args.args[0]
                self.assertEqual('performance' in site, enabled)


if __name__ == '__main__':
    unittest.main()
