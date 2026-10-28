import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'cli')]
from build_release import (assistant_notices, bind_assistant_metadata, validate_assistant_gates,
                           retain_assistant_baseline_interfaces, bind_patch_distribution, patch_bundle)
from ccheck import assistant
from ccheck.schema_migration import epoch
from ccheck.core import DeploymentError


class AssistantPublisherTests(unittest.TestCase):
    def test_combined_patch_preserves_original_asset_versions_and_omits_unchanged_archives(self):
        name = 'images/qwen.tar.gz.part0000'
        previous = {'version': 'v0.1.0-rc.20', '_sha256': 'base-sha', 'files': {name: 'unchanged'},
            'artifact_sizes': {name: 7}, 'distribution': {'kind': 'deployment-patch',
                'base_version': 'v0.1.0-rc.13', 'base_release_sha256': 'first-sha',
                'reused_files': [name], 'asset_versions': {name: 'v0.1.0-rc.13'}}}
        target = {'version': 'v0.1.0-rc.21', 'files': {name: 'unchanged', 'cli/ccheck.pyz': 'new'},
            'artifact_sizes': {name: 7, 'cli/ccheck.pyz': 3}, 'artifact_reuse': {'images': ['qwen']},
            'images': {'qwen': {'archive_parts': [name]}}}
        bind_patch_distribution(previous, target)
        self.assertEqual(target['distribution']['asset_versions'], {name: 'v0.1.0-rc.13'})
        self.assertEqual(target['distribution']['upgrade_from_version'], 'v0.1.0-rc.20')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'release-v0.1.0-rc.21'
            (root / 'images').mkdir(parents=True)
            (root / 'cli').mkdir()
            for path in (name, 'cli/ccheck.pyz', 'release.json', 'release.sig'):
                (root / path).write_text('fixture')
            bundle = patch_bundle(root, target)
            import tarfile
            with tarfile.open(bundle) as archive:
                names = archive.getnames()
            self.assertNotIn(root.name + '/' + name, names)
            self.assertIn(root.name + '/cli/ccheck.pyz', names)
            self.assertIn(root.name + '/release.sig', names)
        previous['files'][name] = 'tampered'
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            bind_patch_distribution(previous, target)

    def test_existing_cli_and_terminal_survive_assistant_assembly_without_aliasing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'old', root / 'new'
            old.mkdir()
            new.mkdir()
            names = ('cli/ccheck-review.pyz', 'ccheck-review', 'CLI-REVIEW.md', 'terminal/README.md')
            files = {}
            for name in names:
                path = old / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            interface = {'file': names[0], 'wrapper': names[1], 'guide': names[2], 'version': '0.2.1'}
            previous = {'_root': str(old), 'files': files, 'review_cli': interface}
            self.assertEqual(retain_assistant_baseline_interfaces(previous, new), interface)
            for name in names:
                self.assertEqual((old / name).read_bytes(), (new / name).read_bytes())
            (new / names[0]).write_bytes(b'changed destination')
            self.assertEqual((old / names[0]).read_bytes(), names[0].encode())
            del previous['files'][names[0]]
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                retain_assistant_baseline_interfaces(previous, new)

    def test_missing_or_unapproved_cli_metadata_is_not_silently_dropped(self):
        for value in (None, {'file': 'other', 'wrapper': 'ccheck-review', 'guide': 'CLI-REVIEW.md'}):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'review CLI'):
                retain_assistant_baseline_interfaces({'review_cli': value}, Path('/unused'))

    def test_provider_license_is_verified_before_distribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / 'source', root / 'release'
            resources = source / 'deploy/assistant'
            resources.mkdir(parents=True)
            (output / 'licenses').mkdir(parents=True)
            license_text = b'MIT fixture notice\n'
            (resources / 'anythingllm.lock.json').write_text(json.dumps({
                'license': 'MIT', 'license_sha256': hashlib.sha256(license_text).hexdigest()}))
            (resources / 'README.md').write_text('operator guide')
            with patch('build_release.subprocess.check_output', return_value=license_text) as run:
                assistant_notices(source, 'sha256:verified', output)
            self.assertEqual((output / 'licenses/AnythingLLM-MIT.txt').read_bytes(), license_text)
            self.assertEqual((output / 'ASSISTANT.md').read_text(), 'operator guide')
            self.assertIn('--read-only', run.call_args.args[0])
            with patch('build_release.subprocess.check_output', return_value=b'tampered'), \
                    self.assertRaisesRegex(RuntimeError, 'license'):
                assistant_notices(source, 'sha256:verified', output)

    def test_all_acceptance_gates_are_required(self):
        evidence = dict.fromkeys(('passed', 'real_gpu', 'review_preemption', 'rag',
            'owner_isolation', 'recovery', 'source_native_parity'), True)
        previous, images = {'version': 'v0.1.0-rc.20'}, {'anythingllm': 'immutable'}
        validate_assistant_gates(previous, images, {'assistant': evidence})
        for name in evidence:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, name):
                validate_assistant_gates(previous, images, {'assistant': dict(evidence, **{name: False})})
        for base in (None, {'version': 'v0.1.0-rc.17'}):
            with self.assertRaisesRegex(RuntimeError, 'baseline'):
                validate_assistant_gates(base, images, {'assistant': evidence})
        with self.assertRaisesRegex(RuntimeError, 'provider'):
            validate_assistant_gates(previous, {}, {'assistant': evidence})

    def manifests(self):
        old = {'0017_review_scan_progress.py': 'old'}
        new = dict(old, **assistant.migration_hashes())
        previous = {'version': 'v0.1.0-rc.20', '_sha256': 'trusted',
            'schema_files': old, 'schema_epoch': epoch(old),
            'files': {'models/model-manifest.json': 'same'}}
        target = {'version': 'v0.1.0-rc.999', 'schema_files': new, 'schema_epoch': epoch(new),
            'files': copy.deepcopy(previous['files']), 'images': {'anythingllm': {}}}
        return previous, target

    def test_metadata_is_bound_to_approved_runtime_migrations_and_model(self):
        previous, target = self.manifests()
        root = Path(__file__).resolve().parents[3]
        bind_assistant_metadata(root, previous, target)
        self.assertEqual(target['assistant_runtime'], assistant.hashes())
        self.assertTrue(target['schema_migration']['backup_required'])
        self.assertEqual(target['schema_migration']['migration_sha256'], assistant.migration_hashes())
        previous, target = self.manifests()
        target['files']['models/model-manifest.json'] = 'different-model'
        with self.assertRaises(DeploymentError):
            bind_assistant_metadata(root, previous, target)

    def test_publisher_and_resource_source_must_match(self):
        with patch('build_release.sha', return_value='different'), \
                self.assertRaisesRegex(RuntimeError, 'runtime differ'):
            bind_assistant_metadata(Path('/source'), *self.manifests())
