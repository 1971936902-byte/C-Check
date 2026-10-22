import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publisher
from build_images import pinned
from upload_github_release import verify_remote_tag
from ccheck.core import DeploymentError


class PublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def baseline(self):
        (self.root / 'input.json').write_text('{}')
        digest = hashlib.sha256(b'{}').hexdigest()
        (self.root / 'baseline.json').write_text(json.dumps({'files': {'input.json': digest}}))

    def test_curated_baseline_and_all_catalogs_match(self):
        root = Path(publisher.__file__).parent / 'baselines/rc13'
        baseline = publisher.verify_baseline(root)
        self.assertEqual(baseline['release_version'], 'v0.1.0-rc.13')
        records = json.loads((root / 'wheels.json').read_text())
        for group in publisher.GROUPS:
            catalog = json.loads((root / 'locks' / (group + '.wheels.json')).read_text())
            selected = [row for row in records if row['environment'] == group]
            self.assertEqual({r['file'] for r in selected}, {r['file'] for r in catalog})
            for row in catalog:
                found = next(r for r in selected if r['file'] == row['file'])
                self.assertTrue(all(found[key] == value for key, value in row.items()))
        self.assertEqual(len(records), 272)

    def test_baseline_tampering_is_rejected(self):
        self.baseline()
        (self.root / 'input.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(DeploymentError, 'checksum'):
            publisher.verify_baseline(self.root)

    def test_unlisted_private_file_is_rejected(self):
        self.baseline()
        (self.root / 'private.key').write_text('not a real key')
        with self.assertRaisesRegex(DeploymentError, 'unexpected'):
            publisher.verify_baseline(self.root)

    def test_base_digest_lock_ignores_mutable_host_tags(self):
        reference = 'python@sha256:' + 'a' * 64
        path = self.root / 'bases.json'
        path.write_text(json.dumps({'python:3.12-slim-bookworm': reference}))
        with patch.dict(os.environ, {'CCHECK_BASE_IMAGE_LOCK': str(path)}), patch('subprocess.check_output') as execute:
            self.assertEqual(pinned('python:3.12-slim-bookworm'), reference)
            execute.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'Missing/invalid'):
                pinned('unknown:latest')

    def test_malformed_base_digest_is_rejected(self):
        path = self.root / 'bases.json'
        path.write_text(json.dumps({'python:3.12-slim-bookworm': 'python:latest'}))
        with patch.dict(os.environ, {'CCHECK_BASE_IMAGE_LOCK': str(path)}):
            with self.assertRaises(RuntimeError):
                pinned('python:3.12-slim-bookworm')

    def test_offline_prepare_never_resolves_or_downloads(self):
        baseline, source = self.root / 'baseline', self.root / 'source'
        baseline.mkdir()
        payload = b'not a real wheel; cache contract test'
        cached = baseline / 'compiler.whl'
        cached.write_bytes(payload)
        row = {'environment': 'compiler', 'file': 'compiler.whl', 'local': 'compiler.whl',
               'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}
        with patch('publisher.wheel_url', side_effect=AssertionError('network forbidden')):
            result = publisher.prepare_wheel(row, baseline, source, {'wheel_download_policy': 'offline'})
        self.assertEqual(result, 'cache')
        self.assertEqual((source / 'wheelhouse/compiler/compiler.whl').read_bytes(), payload)
        self.assertEqual(cached.read_bytes(), payload)

    def test_missing_offline_wheel_fails_without_network(self):
        row = {'environment': 'backend', 'file': 'missing.whl', 'sha256': 'a' * 64, 'size': 1}
        with patch('publisher.wheel_url', side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(DeploymentError, 'offline'):
                publisher.prepare_wheel(row, self.root, self.root / 'source', {'wheel_download_policy': 'offline'})

    def test_wheel_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, 'unsafe'):
            publisher.prepare_wheel({'environment': 'backend', 'file': '../escape.whl'}, self.root, self.root, {})

    def test_unapproved_download_host_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, 'unapproved'):
            publisher.wheel_url({'url': 'https://user:password@example.invalid/file.whl'})

    def test_remote_tag_must_match_signed_commit(self):
        response = json.dumps({'object': {'type': 'commit', 'sha': 'a' * 40}}).encode()
        with patch('subprocess.check_output', return_value=response):
            verify_remote_tag('owner/repository', 'release-v1.0.0', 'a' * 40)
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                verify_remote_tag('owner/repository', 'release-v1.0.0', 'b' * 40)

    def test_annotated_tag_is_dereferenced(self):
        responses = [json.dumps({'object': {'type': 'tag', 'sha': 'b' * 40}}).encode(),
                     json.dumps({'object': {'type': 'commit', 'sha': 'a' * 40}}).encode()]
        with patch('subprocess.check_output', side_effect=responses) as execute:
            verify_remote_tag('owner/repository', 'release-v1.0.0', 'a' * 40)
            self.assertEqual(execute.call_count, 2)

    def test_source_worktree_and_modifications_are_rejected(self):
        files = {'test.py': hashlib.sha256(b'original').hexdigest()}
        manifest = {'files': files, 'tree_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                    'worktree_snapshot': True}
        (self.root / 'build-source.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(DeploymentError, 'committed'):
            publisher.verify_source(self.root)
        manifest['worktree_snapshot'] = False
        (self.root / 'build-source.json').write_text(json.dumps(manifest))
        (self.root / 'test.py').write_bytes(b'changed')
        with self.assertRaisesRegex(DeploymentError, 'source changed'):
            publisher.verify_source(self.root)

    def test_prepare_and_recheck_use_only_verified_local_inputs(self):
        source = self.root / 'source'
        baseline = source / 'deploy/release/baselines/tiny'
        (baseline / 'vendor').mkdir(parents=True)
        (baseline / 'locks').mkdir()
        (baseline / 'models').mkdir()
        (baseline / 'locks/backend.lock').write_text('fixture lock')
        (baseline / 'models/model-manifest.json').write_text('{"models":[]}')
        (baseline / 'base-images.json').write_text('{}')
        wheel_path = baseline / 'vendor/yaml.whl'
        with zipfile.ZipFile(wheel_path, 'w') as wheel:
            wheel.writestr('yaml/__init__.py', '# pure Python fixture\n')
            wheel.writestr('yaml/_yaml.so', 'must not be extracted')
        row = {'environment': 'backend', 'distribution': 'pyyaml', 'file': 'yaml.whl',
               'local': 'vendor/yaml.whl', 'size': wheel_path.stat().st_size,
               'sha256': hashlib.sha256(wheel_path.read_bytes()).hexdigest()}
        (baseline / 'wheels.json').write_text(json.dumps([row]))
        files = {p.relative_to(baseline).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in baseline.rglob('*') if p.is_file()}
        (baseline / 'baseline.json').write_text(json.dumps({'files': files}))
        frozen = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in source.rglob('*') if p.is_file()}
        (source / 'build-source.json').write_text(json.dumps({'files': frozen, 'git_sha': 'a' * 40,
            'tree_sha256': hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
            'worktree_snapshot': False}))
        config = {'baseline': 'tiny', 'version': 'v1.0.0', 'wheel_download_policy': 'offline'}
        with patch('publisher.urlopen', side_effect=AssertionError('network forbidden')), \
                patch('publisher.command', side_effect=AssertionError('no external command expected')):
            publisher.prepare(self.root, config)
            publisher.inputs(self.root, config)
        self.assertTrue((self.root / 'tools/yaml/__init__.py').is_file())
        self.assertFalse((self.root / 'tools/yaml/_yaml.so').exists())
        self.assertEqual(json.loads((self.root / 'prepared.json').read_text())['source'], 'a' * 40)
        (source / 'locks/backend.lock').write_text('tampered')
        with self.assertRaisesRegex(DeploymentError, 'dependency input changed'):
            publisher.inputs(self.root, config)

    def test_configuration_rejects_checkout_as_work_root(self):
        config = {'version': 'v1.0.0', 'source_ref': 'HEAD', 'work_root': str(publisher.REPOSITORY / 'build'),
                  'baseline': 'rc13', 'wheel_download_policy': 'online'}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(DeploymentError, 'outside the checkout'):
            publisher.settings(path)
        config['work_root'] = str(self.root / 'build')
        config['password'] = 'not a real password'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(DeploymentError, 'unknown'):
            publisher.settings(path)

    @unittest.skipUnless(os.name == 'posix', 'publisher locks require Linux/POSIX')
    def test_workspace_lock_and_configuration_identity(self):
        config = {'work_root': str(self.root / 'build'), 'version': 'v1.0.0'}
        with publisher.workspace(config):
            with self.assertRaisesRegex(DeploymentError, 'another publisher'):
                with publisher.workspace(config):
                    self.fail('concurrent operation accepted')
        with self.assertRaisesRegex(DeploymentError, 'configuration changed'):
            with publisher.workspace(dict(config, version='v1.0.1')):
                self.fail('changed configuration accepted')


if __name__ == '__main__':
    unittest.main()
