import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError, checked_path, digest, gpu_memory_fraction, load_site, verify_release
from ccheck.engine import Engine, doctor, gateway_snippet, preflight, render_compose
from ccheck.runtime import build_runtime
from ccheck.models import model_cache_directory, obtain_file, prepare_models, validate_model_layout, validate_offline_models
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_release import image_archives
from ccheck.image_identity import identity
from ccheck.backup import helper, key_material
from ccheck.lifecycle import cache_release, start_stack, wait_data_services
from ccheck.acceptance import fixture_archive
from validate_images import execute as gate_execute, GATE_RUN_ID
from assemble_backend import assemble


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_hardware_profiles_share_rendered_and_admission_memory_budgets(self):
        images = {name: 'sha256:' + 'a' * 64 for name in
                  ['backend', 'mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'frontend']}
        for profile, fraction, total in [('a100-40g-fp8', 0.94, 40960), ('ada-48g-fp8', 0.88, 49140)]:
            with self.subTest(profile=profile):
                site = {'data_dir': str(self.root), 'instance': 'profile-test', 'gpu': '0',
                        'profile': profile, 'web': {'bind': '127.0.0.1', 'port': 18800,
                                                   'public_url': 'http://127.0.0.1:18800'}}
                document = render_compose(site, images, 10001, 10001)
                self.assertEqual(gpu_memory_fraction(profile), fraction)
                self.assertEqual(document['services']['qwen']['environment']['GPU_MEMORY_UTILIZATION'], str(fraction))
                self.assertEqual([name for name, service in document['services'].items() if service.get('ports')], ['frontend'])
                engine = Engine(site)
                for free, admitted in [(total * fraction + 1, True), (total * fraction - 1, False)]:
                    with patch.object(engine, 'compose', return_value=''), \
                            patch('ccheck.engine.run', side_effect=['', f'{free},{total}']):
                        if admitted:
                            engine.check_gpu_admission()
                        else:
                            with self.assertRaisesRegex(DeploymentError, 'below the selected profile budget'):
                                engine.check_gpu_admission()

    def test_unknown_profile_never_silently_uses_ada_budget(self):
        with self.assertRaisesRegex(DeploymentError, 'supported hardware profile'):
            gpu_memory_fraction('rtx-unknown')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX runtime modes require Linux')
    def test_assembled_runtime_does_not_inherit_private_source_permissions(self):
        source, output = self.root / 'source', self.root / 'output'
        (source / 'app').mkdir(parents=True)
        (source / 'app/plain.py').write_text('public framework fixture')
        (source / 'app/core.py').write_text('private core fixture')
        (source / 'alembic/versions').mkdir(parents=True)
        (source / 'alembic/env.py').write_text('migration fixture')
        (source / 'alembic/versions/initial.py').write_text('revision fixture')
        (source / 'alembic.ini').write_text('[alembic]\nscript_location = alembic\n')
        (output / 'app').mkdir(parents=True)
        (output / 'app/core.fixture.so').write_bytes(b'native-artifact-fixture-not-a-real-model')
        policy = self.root / 'policy.json'
        policy.write_text(json.dumps({'compiled_modules': ['app.core'],
                                     'plaintext_allowlist': ['app/plain.py'], 'resource_allowlist': []}))
        for path in [source, *source.rglob('*'), output, output / 'app']:
            path.chmod(0o700 if path.is_dir() else 0o600)
        assemble(source, output, policy)
        for path in [output, *output.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o755 if path.is_dir() else 0o644)
        self.assertEqual((source / 'alembic').stat().st_mode & 0o777, 0o700)
        self.assertEqual((source / 'alembic/env.py').stat().st_mode & 0o777, 0o600)
        self.assertFalse((output / 'app/core.py').exists())

    def test_paths_reject_traversal_and_symlinks(self):
        for name in ['../private', '/etc/passwd', 'models/../../private', 'models\\x']:
            with self.assertRaises(DeploymentError):
                checked_path(self.root, name)
        if sys.platform != 'win32':
            (self.root / 'link').symlink_to(self.root.parent)
            with self.assertRaises(DeploymentError):
                checked_path(self.root, 'link/file')

    def test_model_cache_reuse_checks_hash_without_mutating_source(self):
        source = self.root / 'source.bin'
        source.write_bytes(b'expected-content')
        expected = {'size': source.stat().st_size, 'sha256': digest(source), 'url': 'https://example.invalid/file'}
        target = self.root / 'models/file.bin'
        self.assertEqual(obtain_file(expected, target, source), 'cache')
        self.assertEqual(obtain_file(expected, target, source), 'reused')
        target.unlink()
        self.assertTrue(source.is_file())
        self.assertEqual(source.read_bytes(), b'expected-content')

    def test_explicit_model_paths_override_legacy_cache_names(self):
        model = {'key': 'qwen', 'cache_name': 'qwen32b-fp8'}
        legacy = self.root / 'old-cache'
        explicit = self.root / 'Qwen2.5-Coder-32B-Instruct-FP8'
        site = {'model_cache': str(legacy), 'model_paths': {'qwen': str(explicit)}}
        self.assertEqual(model_cache_directory(site, model), explicit.resolve())
        self.assertEqual(model_cache_directory({'model_cache': str(legacy)}, model), legacy.resolve() / model['cache_name'])
        self.assertIsNone(model_cache_directory({}, model))

    def offline_model_fixture(self):
        models, paths = [], {}
        for key, folder in [('qwen', 'Qwen2.5-Coder-32B-Instruct-FP8'), ('jina', 'jina-code-embeddings-1.5b')]:
            cache = self.root / 'downloads' / folder
            cache.mkdir(parents=True)
            config = {'model_type': 'qwen2', 'quantization_config': {'quant_method': 'fp8'}}
            values = {'config.json': json.dumps(config).encode(),
                      'tokenizer.json': b'{"model":{"type":"BPE"}}',
                      'model.safetensors': b'unit fixture, not actual model weights'}
            entries = []
            for name, value in values.items():
                (cache / name).write_bytes(value)
                entries.append({'path': name, 'size': len(value), 'sha256': digest(cache / name),
                                'url': 'https://example.invalid/' + name})
            models.append({'key': key, 'cache_name': 'deliberately-different-' + key,
                           'repository': 'unit-fixture', 'revision': 'unit-fixture', 'files': entries})
            paths[key] = str(cache)
        release = self.root / 'release'
        (release / 'models').mkdir(parents=True)
        (release / 'models/model-manifest.json').write_text(json.dumps({'models': models}))
        return {'_root': str(release)}, {'data_dir': str(self.root / 'instance'),
            'model_paths': paths, 'model_download_policy': 'offline'}

    def test_offline_model_paths_reuse_both_custom_directories_without_network(self):
        release, site = self.offline_model_fixture()
        originals = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.root / 'downloads').rglob('*') if p.is_file()}
        with patch('ccheck.models.urlopen', side_effect=AssertionError('offline network call')) as network:
            validate_offline_models(release, site)
            self.assertFalse(Path(site['data_dir']).exists())
            result = prepare_models(release, site)
        network.assert_not_called()
        self.assertEqual([r['key'] for r in result], ['qwen', 'jina'])
        self.assertTrue(all(f['action'] == 'cache' for r in result for f in r['files']))
        self.assertEqual(originals, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in originals})

    def test_offline_mismatch_fails_before_creating_instance_and_preserves_sources(self):
        release, site = self.offline_model_fixture()
        source = Path(site['model_paths']['qwen']) / 'model.safetensors'
        source.write_bytes(b'x' * source.stat().st_size)
        before = source.read_bytes()
        with patch('ccheck.models.urlopen') as network:
            with self.assertRaisesRegex(DeploymentError, 'qwen/model.safetensors'):
                validate_offline_models(release, site)
        network.assert_not_called()
        self.assertFalse(Path(site['data_dir']).exists())
        self.assertEqual(source.read_bytes(), before)

    def test_offline_obtain_never_deletes_a_mismatched_existing_file(self):
        target = self.root / 'model.safetensors'
        target.write_bytes(b'other-version')
        entry = {'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest(), 'url': 'https://example.invalid/model'}
        with patch('ccheck.models.urlopen') as network:
            with self.assertRaisesRegex(DeploymentError, 'refusing network download'):
                obtain_file(entry, target, allow_download=False)
        network.assert_not_called()
        self.assertEqual(target.read_bytes(), b'other-version')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX cache permissions require Linux')
    def test_private_model_cache_is_copied_without_chmod_or_chown_of_original(self):
        source = self.root / 'private-weight'
        source.write_bytes(b'private model fixture')
        source.chmod(0o600)
        before = source.stat()
        target = self.root / 'instance/model.safetensors'
        entry = {'size': before.st_size, 'sha256': digest(source), 'url': 'https://example.invalid/model'}
        self.assertEqual(obtain_file(entry, target, source, allow_download=False), 'cache')
        self.assertEqual(source.stat().st_mode & 0o777, 0o600)
        self.assertEqual(source.stat().st_ino, before.st_ino)
        self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def model_layout(self, files, key='jina'):
        for name, content in files.items():
            (self.root / name).write_text(content)
        return {'key': key, 'files': [{'path': name} for name in files]}

    def test_bpe_layout_without_tokenizer_config_is_supported(self):
        model = self.model_layout({'config.json': '{"model_type":"qwen2"}',
            'vocab.json': '{"a":0,"b":1}', 'merges.txt': '#version: 0.2\na b\n',
            'model.safetensors': 'layout-only unit fixture; not loadable weights'})
        validate_model_layout(model, self.root)

    def test_fast_tokenizer_layout_is_supported(self):
        model = self.model_layout({'config.json': '{"model_type":"qwen2"}',
            'tokenizer.json': '{"model":{"type":"BPE","vocab":{"a":0}}}',
            'model.safetensors': 'layout-only unit fixture'})
        validate_model_layout(model, self.root)

    def test_missing_tokenizer_data_is_rejected(self):
        model = self.model_layout({'config.json': '{"model_type":"qwen2"}',
            'tokenizer_config.json': '{}', 'vocab.json': '{"a":0}', 'model.safetensors': 'fixture'})
        with self.assertRaisesRegex(DeploymentError, 'missing tokenizer data'):
            validate_model_layout(model, self.root)

    def test_missing_config_is_rejected(self):
        model = self.model_layout({'vocab.json': '{"a":0}', 'merges.txt': 'a b', 'model.safetensors': 'fixture'})
        with self.assertRaisesRegex(DeploymentError, 'missing config'):
            validate_model_layout(model, self.root)

    def test_missing_shard_is_rejected(self):
        model = self.model_layout({'config.json': '{"model_type":"qwen2"}', 'vocab.json': '{"a":0}',
            'merges.txt': 'a b', 'model.safetensors.index.json': '{"weight_map":{"a":"missing.safetensors"}}'})
        with self.assertRaisesRegex(DeploymentError, 'shard manifest is incomplete'):
            validate_model_layout(model, self.root)

    def test_qwen_must_still_be_fp8(self):
        model = self.model_layout({'config.json': '{"model_type":"qwen2"}', 'vocab.json': '{"a":0}',
            'merges.txt': 'a b', 'model.safetensors': 'fixture'}, key='qwen')
        with self.assertRaisesRegex(DeploymentError, 'not the declared FP8'):
            validate_model_layout(model, self.root)

    def test_reused_image_archive_is_verified_and_copied_without_aliasing(self):
        old, new = self.root / 'old', self.root / 'new'
        (old / 'images').mkdir(parents=True)
        new.mkdir()
        artifact = old / 'images/runtime.tar.gz.part0000'
        artifact.write_bytes(b'previous verified image archive')
        metadata = {'Os': 'linux', 'Architecture': 'amd64', 'Config': {}, 'RootFS': {'Type': 'layers', 'Layers': []}}
        name = 'images/runtime.tar.gz.part0000'
        previous = {'_root': str(old), 'files': {name: digest(artifact)}, 'images': {'qwen': {
            'filesystem_config_sha256': identity(metadata), 'archive_parts': [name]}}}
        reused = []
        self.assertEqual(image_archives('unused', metadata, new, 'qwen', previous, reused), [name])
        self.assertEqual(reused, ['qwen'])
        (new / name).write_bytes(b'only the independent destination changes')
        self.assertEqual(artifact.read_bytes(), b'previous verified image archive')

    def test_different_image_identity_is_never_reused(self):
        metadata = {'Os': 'linux', 'Architecture': 'amd64', 'Config': {}, 'RootFS': {'Type': 'layers', 'Layers': []}}
        previous = {'images': {'qwen': {'filesystem_config_sha256': 'different', 'archive_parts': ['images/old']}}}
        with patch('build_release.export_image', return_value=['images/new']) as export:
            self.assertEqual(image_archives('actual-image', metadata, self.root, 'qwen', previous, []), ['images/new'])
        export.assert_called_once_with('actual-image', self.root / 'images', 'qwen')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file permissions are enforced by the Linux release')
    def test_backup_uses_full_single_line_hexadecimal_key_material(self):
        key = self.root / 'backup.key'
        key.write_bytes(b'ab' * 48 + b'\n')
        key.chmod(0o600)
        path, material = key_material(key)
        self.assertEqual(path, key.resolve())
        self.assertEqual(len(material), 32)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file permissions are enforced by the Linux release')
    def test_backup_rejects_binary_multiline_or_short_keys(self):
        key = self.root / 'backup.key'
        for content in [b'\x00' * 64, b'\n' + b'ab' * 48, b'ab' * 48 + b'\nsecond-line', b'ab' * 16]:
            with self.subTest(content_length=len(content)):
                key.write_bytes(content)
                key.chmod(0o600)
                with self.assertRaisesRegex(DeploymentError, 'backup key must be'):
                    key_material(key)

    def test_backup_helpers_do_not_persist_plaintext_stdout_in_docker_logs(self):
        engine = type('EngineFixture', (), {'root': self.root})()
        state = {'uid': 10001, 'gid': 10001, 'images': {'backend': 'test-backend'}}
        for operation in ['measure', 'create', 'restore']:
            command = helper(engine, state, operation)
            self.assertEqual(command[command.index('--log-driver') + 1], 'none')
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertIn('--read-only', command)
            mount = command[command.index('--mount') + 1]
            self.assertEqual(mount.endswith(',readonly'), operation != 'restore')

    def cache_fixture(self):
        source, installation = self.root / 'package', self.root / 'installation'
        source.mkdir()
        (source / 'release.json').write_text('{}')
        (source / 'payload').write_text('fixture')
        for directory in ['config', 'releases']:
            (installation / directory).mkdir(parents=True)
        key = self.root / 'publisher.pem'
        key.write_text('unit fixture; signature behavior is tested separately')
        release = {'version': 'v0.1.0-rc.3', '_root': str(source), '_sha256': digest(source / 'release.json'), '_publisher': digest(key)}
        return {'data_dir': str(installation)}, release, key, installation

    def test_failed_release_copy_does_not_leave_an_immutable_version_directory(self):
        site, release, key, installation = self.cache_fixture()
        with patch('ccheck.lifecycle.shutil.copytree', side_effect=OSError('simulated interrupted copy')):
            with self.assertRaises(OSError):
                cache_release(site, release, key)
        self.assertEqual(list((installation / 'releases').iterdir()), [])

    def test_failed_staging_verification_never_commits_release_directory(self):
        site, release, key, installation = self.cache_fixture()
        with patch('ccheck.lifecycle.verify_release', side_effect=DeploymentError('simulated tamper')):
            with self.assertRaises(DeploymentError):
                cache_release(site, release, key)
        self.assertEqual(list((installation / 'releases').iterdir()), [])

    def test_verified_release_is_committed_atomically(self):
        site, release, key, installation = self.cache_fixture()
        observed = []
        def verify_staging(path, publisher, fingerprint):
            observed.append(Path(path))
            self.assertFalse((installation / 'releases' / release['version']).exists())
            self.assertTrue((Path(path) / 'payload').is_file())
            return dict(release)
        with patch('ccheck.lifecycle.verify_release', side_effect=verify_staging):
            result = cache_release(site, release, key)
        target = installation / 'releases' / release['version']
        self.assertEqual(result['_root'], str(target))
        self.assertEqual(list((installation / 'releases').iterdir()), [target])
        self.assertFalse(observed[0].exists())

    def test_cli_archive_accepts_epoch_sources_and_has_reproducible_timestamps(self):
        publisher = self.root / 'publisher'
        publisher.mkdir()
        implementation = Path(__file__).resolve().parents[1]
        shutil.copy2(implementation / 'build_cli.py', publisher / 'build_cli.py')
        shutil.copytree(implementation / 'cli', publisher / 'cli', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        yaml = self.root / 'yaml'
        yaml.mkdir()
        (yaml / '__init__.py').write_text('# packaging fixture; YAML behavior is tested with the actual vendor package\n')
        for path in [*publisher.rglob('*'), *yaml.rglob('*')]:
            os.utime(path, (0, 0))
        outputs = [self.root / 'first.pyz', self.root / 'second.pyz']
        for output in outputs:
            subprocess.run([sys.executable, str(publisher / 'build_cli.py'), '--output', str(output),
                            '--yaml-package', str(yaml)], check=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertTrue(all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in archive.infolist()))
        self.assertEqual(digest(outputs[0]), digest(outputs[1]))
        result = subprocess.run([sys.executable, str(outputs[0]), '--help'], text=True, capture_output=True, check=True)
        self.assertIn('package-verify', result.stdout)

    def test_gate_containers_have_per_run_cleanup_ownership_labels(self):
        with patch('validate_images.subprocess.run') as run:
            gate_execute(['docker', 'run', '--rm', 'test-image', 'true'], self.root / 'gate.log')
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['docker', 'run', '--label', 'io.ccheck.release-gate=' + GATE_RUN_ID])

    def test_doctor_does_not_create_installation_or_download_dependencies(self):
        future = self.root / 'not-installed'
        site = {'data_dir': str(future), 'profile': 'a100-40g-fp8', 'gpu': '0'}
        with patch('ccheck.engine.preflight') as preflight, patch('ccheck.engine.run',
                side_effect=['29.9.2', '5.6.1', 'OpenSSL 3.0', 'A100, GPU-test, 550, 40946, 40960']) as run:
            result = doctor(site)
        preflight.assert_called_once_with(site)
        self.assertFalse(future.exists())
        self.assertTrue(result['prerequisites_passed'])
        self.assertFalse(result['container_cuda_and_real_inference_tested'])
        self.assertGreater(result['data_filesystem_free_bytes'], 0)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['docker', 'docker', 'openssl', 'nvidia-smi'])

    def test_acceptance_fixture_archive_handles_reproducible_epoch_files(self):
        fixture = self.root / 'project'
        fixture.mkdir()
        source = fixture / 'protocol.c'
        source.write_bytes(b'int safe(void) { return 0; }\n')
        os.utime(source, (0, 0))
        result = fixture_archive(fixture)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            self.assertEqual(archive.namelist(), ['protocol.c'])
            self.assertEqual(archive.read('protocol.c'), source.read_bytes())
            self.assertEqual(archive.getinfo('protocol.c').date_time, (1980, 1, 1, 0, 0, 0))
            self.assertIsNone(archive.testzip())
        self.assertEqual(result, fixture_archive(fixture))

    def test_doctor_propagates_failed_prerequisites_without_followup_actions(self):
        with patch('ccheck.engine.preflight', side_effect=DeploymentError('missing prerequisite')), \
                patch('ccheck.engine.run') as run:
            with self.assertRaisesRegex(DeploymentError, 'missing prerequisite'):
                doctor({'data_dir': str(self.root / 'not-installed')})
        run.assert_not_called()

    def runtime_budget_fixture(self):
        release_root, installation = self.root / 'release', self.root / 'installation'
        prefix = release_root / 'runtime/qwen'
        prefix.mkdir(parents=True)
        for name in ['requirements.lock', 'Dockerfile', 'qwen_entry.py']:
            (prefix / name).write_text('capacity-only unit fixture: ' + name)
        (prefix / 'wheels.json').write_text(json.dumps([{'size': 4 * 1024**3}]))
        (release_root / 'models').mkdir()
        (release_root / 'models/model-manifest.json').write_text('{"models": []}')
        (installation / 'state').mkdir(parents=True)
        (installation / 'state/deployment.json').write_text('{}')
        (installation / 'releases/v1.0.0').mkdir(parents=True)
        recipe = {'directory': 'runtime/qwen', 'base_image': 'python@sha256:' + 'a' * 64}
        release = {'_root': str(release_root), 'version': 'v1.0.0', 'runtime_recipe': recipe,
                   'images': {'qwen': {'id': 'sha256:' + 'a' * 64, 'size_bytes': 13 * 1024**3}}}
        fingerprint = hashlib.sha256(json.dumps({'base_image': recipe['base_image'],
            'files': {name: digest(prefix / name) for name in ['requirements.lock', 'Dockerfile', 'qwen_entry.py', 'wheels.json']}},
            sort_keys=True).encode()).hexdigest()
        image = 'sha256:' + 'b' * 64
        marker = installation / 'state/runtime-build.json'
        marker.write_text(json.dumps({'recipe': fingerprint, 'image': image}))
        return {'data_dir': str(installation), 'runtime_source': 'build', 'gpu': '0'}, release, image, marker

    def runtime_budget_preflight(self, site, release, image, free_gib=20, image_exists=True):
        usage = SimpleNamespace(free=free_gib * 1024**3)
        def engine_run(command):
            if command[:3] == ['docker', 'info', '--format']:
                return str(self.root)
            return ''
        with patch('ccheck.engine.platform.system', return_value='Linux'), \
                patch('ccheck.engine.platform.machine', return_value='x86_64'), \
                patch('ccheck.engine.shutil.which', return_value='/test/tool'), \
                patch('ccheck.engine.shutil.disk_usage', return_value=usage), \
                patch('ccheck.engine.run', side_effect=engine_run), \
                patch('ccheck.runtime.run', return_value=image,
                      side_effect=None if image_exists else DeploymentError('image not found')):
            preflight(site, release)

    def test_cached_runtime_does_not_reserve_another_full_build(self):
        site, release, image, marker = self.runtime_budget_fixture()
        original = marker.read_bytes()
        self.runtime_budget_preflight(site, release, image)
        self.assertEqual(marker.read_bytes(), original)
        with self.assertRaisesRegex(DeploymentError, 'at least 12 GiB'):
            self.runtime_budget_preflight(site, release, image, free_gib=11)
        with patch('ccheck.runtime.run', return_value=image) as run, \
                patch('ccheck.runtime.obtain_file') as download, patch('ccheck.runtime.shutil.copy2') as copy_file:
            self.assertEqual(build_runtime(release, site), image)
        self.assertEqual([call.args[0][:3] for call in run.call_args_list], [['docker', 'image', 'inspect']])
        download.assert_not_called()
        copy_file.assert_not_called()

    def test_runtime_recipe_changes_preserve_full_cold_build_budget(self):
        site, release, image, _ = self.runtime_budget_fixture()
        prefix = Path(release['_root']) / 'runtime/qwen'
        for name in ['requirements.lock', 'Dockerfile', 'qwen_entry.py', 'wheels.json']:
            with self.subTest(changed=name):
                path = prefix / name
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                with self.assertRaisesRegex(DeploymentError, 'peak budget'):
                    self.runtime_budget_preflight(site, release, image)
                path.write_bytes(original)
        release['runtime_recipe']['base_image'] += '-different'
        with self.assertRaisesRegex(DeploymentError, 'peak budget'):
            self.runtime_budget_preflight(site, release, image)

    def test_missing_or_invalid_runtime_cache_preserves_cold_build_budget(self):
        site, release, image, marker = self.runtime_budget_fixture()
        for value in ['not-json', '{}', '[]', json.dumps({'recipe': 'different', 'image': image})]:
            with self.subTest(marker=value):
                marker.write_text(value)
                with self.assertRaisesRegex(DeploymentError, 'peak budget'):
                    self.runtime_budget_preflight(site, release, image)
        marker.unlink()
        with self.assertRaisesRegex(DeploymentError, 'peak budget'):
            self.runtime_budget_preflight(site, release, image)

    def test_runtime_image_identity_mismatch_preserves_cold_build_budget(self):
        site, release, image, _ = self.runtime_budget_fixture()
        with self.assertRaisesRegex(DeploymentError, 'peak budget'):
            self.runtime_budget_preflight(site, release, 'sha256:' + 'c' * 64)
        with self.assertRaisesRegex(DeploymentError, 'peak budget'):
            self.runtime_budget_preflight(site, release, image, image_exists=False)

    def test_gateway_snippet_uses_explicit_web_port_and_documents_external_boundary(self):
        site = {'web': {'bind': '0.0.0.0', 'port': 18800}}
        snippet = gateway_snippet(site)
        self.assertIn('proxy_pass http://127.0.0.1:18800;', snippet)
        self.assertIn('proxy_read_timeout 1800s;', snippet)
        self.assertIn('X-Forwarded-Proto $scheme', snippet)
        self.assertIn('DNS/TLS/NAT are managed separately', snippet)
        site['web']['bind'] = '192.0.2.250'
        self.assertIn('proxy_pass http://192.0.2.250:18800;', gateway_snippet(site))

    def test_start_waits_for_application_data_connections_before_migration(self):
        engine = MagicMock()
        engine.management.side_effect = [DeploymentError('initializing'), '{}', '', '']
        with patch('ccheck.lifecycle.time.monotonic', side_effect=[0, 0.5]), \
                patch('ccheck.lifecycle.time.sleep') as sleep, \
                patch('ccheck.lifecycle.networks.verify'):
            start_stack(engine)
        self.assertEqual([call.args[0] for call in engine.management.call_args_list],
                         ['data-health', 'data-health', 'migrate', 'register-model'])
        sleep.assert_called_once_with(2)

    def test_data_readiness_deadline_never_proceeds_with_failed_dependencies(self):
        engine = MagicMock()
        engine.management.side_effect = DeploymentError('not ready')
        with patch('ccheck.lifecycle.time.monotonic', side_effect=[0, 100]), \
                patch('ccheck.lifecycle.time.sleep') as sleep:
            with self.assertRaisesRegex(DeploymentError, 'readiness deadline exceeded'):
                wait_data_services(engine, timeout=90)
        engine.management.assert_called_once_with('data-health')
        sleep.assert_not_called()

    def test_compose_only_publishes_web_and_uses_internal_service_names(self):
        site = {'data_dir': str(self.root), 'instance': 'test-release', 'gpu': '0', 'profile': 'a100-40g-fp8',
                'web': {'bind': '127.0.0.1', 'port': 18800, 'public_url': 'http://127.0.0.1:18800'}}
        images = {name: 'sha256:' + 'a' * 64 for name in ['backend', 'mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'frontend']}
        document = render_compose(site, images, 10001, 10001)
        exposed = [name for name, service in document['services'].items() if service.get('ports')]
        self.assertEqual(exposed, ['frontend'])
        self.assertTrue(document['networks']['data']['internal'])
        self.assertTrue(document['networks']['inference']['internal'])
        mysql_health = document['services']['mysql']['healthcheck']['test']
        self.assertEqual(mysql_health[0], 'CMD-SHELL')
        self.assertIn('MYSQL_PWD="$$(cat /run/secrets/mysql_password)"', mysql_health[1])
        self.assertIn('--protocol=TCP', mysql_health[1])
        self.assertIn('--database=c_check', mysql_health[1])
        self.assertIn('SELECT 1', mysql_health[1])
        self.assertNotIn('mysqladmin', mysql_health[1])
        api = document['services']['api']
        self.assertEqual(api['environment']['RAG_EMBEDDING_BASE_URL'], 'http://embedding:8002')
        self.assertEqual(api['environment']['RAG_EMBEDDING_ALLOW_HASH_FALLBACK'], 'false')
        self.assertIn('DATABASE_URL_FILE', api['environment'])
        self.assertNotIn('DATABASE_URL', api['environment'])
        self.assertEqual(document['services']['qwen']['environment']['GPU_MEMORY_UTILIZATION'], '0.94')
        self.assertEqual(document['services']['qwen']['environment']['TORCHINDUCTOR_CACHE_DIR'], '/cache/torchinductor')
        self.assertEqual(document['services']['qwen']['environment']['USER'], 'ccheck')
        self.assertEqual(document['services']['embedding']['environment']['HOME'], '/tmp')
        self.assertFalse(any('/var/run/docker.sock' in json.dumps(service) for service in document['services'].values()))

    @unittest.skipUnless(shutil.which('openssl'), 'openssl required for real signature verification')
    def test_signature_and_all_signed_assets_are_verified(self):
        private, public = self.root / 'private.pem', self.root / 'public.pem'
        package = self.root / 'package'
        package.mkdir()
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
        files = {}
        for name in ['core-policy.json', 'models/model-manifest.json', 'cli/ccheck.pyz']:
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
            files[name] = digest(path)
        manifest = package / 'release.json'
        manifest.write_text(json.dumps({'format': 1, 'version': 'v0.1.0-rc.1', 'tag': 'release-v0.1.0-rc.1', 'files': files}))
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(private), '-rawin', '-in', str(manifest),
                        '-out', str(package / 'release.sig')], check=True, capture_output=True)
        result = verify_release(package, public)
        self.assertEqual(result['version'], 'v0.1.0-rc.1')
        (package / 'core-policy.json').write_text('tampered')
        with self.assertRaisesRegex(DeploymentError, 'checksum'):
            verify_release(package, public)
        with self.assertRaisesRegex(DeploymentError, 'trust anchor'):
            verify_release(package, public, expected_fingerprint='not-trusted')
        (package / 'core-policy.json').write_text('fixture')
        (package / 'unexpected-private.pem').write_text('not a real key; must not be published')
        with self.assertRaisesRegex(DeploymentError, 'unlisted'):
            verify_release(package, public)


if __name__ == '__main__':
    unittest.main()
