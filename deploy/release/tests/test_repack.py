import copy
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repack_deployment as repack
from ccheck.core import DeploymentError, digest, verify_release


class RepackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def put(self, root, name, data=b'fixture\n'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def freeze(self, root, commit='a' * 40, worktree=False):
        source = {'git_sha': commit, 'git_ref': 'HEAD', 'worktree_snapshot': worktree,
                  'files': repack.source_files(root)}
        source['tree_sha256'] = repack.tree_hash(source['files'])
        repack.write_json(root / 'build-source.json', source)
        return source

    def source_fixture(self):
        root = self.root / 'source'
        for name in ('backend/app/deployment.py', 'frontend/src/main.js', 'deploy/models/catalog.json',
                     'deploy/release/Dockerfile.backend', 'deploy/release/runtime/qwen_entry.py',
                     'deploy/release/Dockerfile.frontend', 'deploy/release/Dockerfile.embedding',
                     'deploy/release/Dockerfile.qwen', 'deploy/release/runtime/embedding_api.py',
                     'deploy/release/core-policy.json', 'deploy/release/compile_core.py',
                     'deploy/release/assemble_backend.py', 'deploy/release/build_images.py',
                     'deploy/release/prepare_build.py', 'deploy/release/nginx.conf',
                     'deploy/release/cli/ccheck/__main__.py'):
            self.put(root, name)
        return root, self.freeze(root)

    def test_source_verifies_every_hash_tree_and_committed_flag(self):
        root, source = self.source_fixture()
        self.assertEqual(repack.verify_source(root, committed=True), source)
        path = root / 'backend/app/deployment.py'
        path.write_bytes(b'tamper')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            repack.verify_source(root)
        path.write_bytes(b'fixture\n')
        source['tree_sha256'] = 'b' * 64
        repack.write_json(root / 'build-source.json', source)
        with self.assertRaisesRegex(RuntimeError, 'tree_sha256'):
            repack.verify_source(root)
        self.freeze(root, worktree=True)
        with self.assertRaisesRegex(RuntimeError, 'committed'):
            repack.verify_source(root, committed=True)

    def test_source_rejects_unlisted_missing_unsafe_and_duplicate_inputs(self):
        root, source = self.source_fixture()
        extra = self.put(root, 'unlisted-build-input.txt')
        with self.assertRaisesRegex(RuntimeError, 'unlisted'):
            repack.verify_source(root, committed=True)
        extra.unlink()
        extra = self.put(root, 'backend/extra.py')
        with self.assertRaisesRegex(RuntimeError, 'inventory'):
            repack.verify_source(root)
        extra.unlink()
        source['files']['../escape'] = 'a' * 64
        repack.write_json(root / 'build-source.json', source)
        with self.assertRaisesRegex(DeploymentError, 'unsafe'):
            repack.verify_source(root)
        (root / 'build-source.json').write_text('{"files": {}, "files": {}}')
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            repack.verify_source(root)
        self.freeze(root)
        (root / 'backend/app/deployment.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            repack.verify_source(root)

    def test_only_deployment_tools_may_change_including_added_and_deleted_inputs(self):
        root, original = self.source_fixture()
        base = {'source': original}
        for name in original['files']:
            source = copy.deepcopy(original)
            source['files'][name] = 'b' * 64
            if name.startswith('deploy/release/cli/'):
                self.assertEqual(repack.approved_sources(base, source, root), [name])
            else:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'non-tooling'):
                    repack.approved_sources(base, source, root)
        for name in ('backend/added.py', 'deploy/release/tests/Dockerfile.evil',
                     'deploy/release/runtime/added.py', 'deploy/release/new-builder.py'):
            source = copy.deepcopy(original)
            source['files'][name] = 'b' * 64
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'non-tooling'):
                repack.approved_sources(base, source, root)
        source = copy.deepcopy(original)
        del source['files']['frontend/src/main.js']
        with self.assertRaisesRegex(RuntimeError, 'non-tooling'):
            repack.approved_sources(base, source, root)
        base['source'] = dict(original, git_sha='f' * 40)
        with self.assertRaisesRegex(RuntimeError, 'base.source.git_sha'):
            repack.approved_sources(base, original, root)
        base['source'] = dict(original, tree_sha256='f' * 64)
        with self.assertRaisesRegex(RuntimeError, 'base.source.tree_sha256'):
            repack.approved_sources(base, original, root)

    def test_git_fallback_reads_committed_blobs_and_checks_signed_tree(self):
        root, original = self.source_fixture()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'config', 'core.autocrlf', 'false'], check=True)
        subprocess.run(['git', '-C', str(root), 'add', 'backend', 'frontend', 'deploy'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.test', 'commit', '-qm', 'fixture'], check=True)
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        original['git_sha'] = commit
        (root / 'backend/app/deployment.py').write_bytes(b'uncommitted, must not be used')
        self.assertEqual(repack.git_source(root, commit)['files'], original['files'])
        self.assertEqual(repack.approved_sources({'source': original}, original, repository=root), [])

    def test_signing_key_requires_exact_0600_and_external_regular_file(self):
        key = self.put(self.root, 'publisher.key')
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)):
            self.assertEqual(repack.signing_key(key, [self.root / 'output']), key.resolve())
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                repack.signing_key(key, [self.root])
        for mode in (0o644, 0o660, 0o400, 0o700, 0o4600):
            with self.subTest(mode=oct(mode)), patch.object(
                    Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | mode)):
                with self.assertRaisesRegex(RuntimeError, '0600'):
                    repack.signing_key(key, [])
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'regular external'):
                repack.signing_key(key, [])
        with self.assertRaisesRegex(RuntimeError, 'regular external'):
            repack.signing_key(self.root / 'missing', [])

    def test_images_link_and_only_cross_filesystem_errors_fall_back_to_copy(self):
        source = self.put(self.root, 'image.part0000')
        target = self.root / 'output/image.part0000'
        repack.reuse_file(source, target, image=True)
        self.assertTrue(os.path.samefile(source, target))
        target.unlink()
        with patch.object(repack.os, 'link', side_effect=OSError(errno.EXDEV, 'cross filesystem')):
            repack.reuse_file(source, target, image=True)
        self.assertFalse(os.path.samefile(source, target))
        self.assertEqual(source.read_bytes(), target.read_bytes())
        target.unlink()
        with patch.object(repack.os, 'link', side_effect=OSError(errno.EACCES, 'denied')):
            with self.assertRaises(OSError):
                repack.reuse_file(source, target, image=True)
        with patch.object(repack.os, 'link') as link:
            repack.reuse_file(source, target)
        link.assert_not_called()

    def test_bootstrap_excludes_images_and_old_metadata_even_when_present(self):
        root = self.root / 'release'
        for name in repack.METADATA | {'images/backend.part0000', 'README.md', 'cli/ccheck.pyz',
                                       'verify/build-gates.json', 'verify/deployment-tooling.json'}:
            self.put(root, name)
        repack.bootstrap(root)
        with tarfile.open(root / 'bootstrap.tar.gz') as archive:
            self.assertEqual(set(archive.getnames()), {'README.md', 'cli/ccheck.pyz',
                                                      'verify/build-gates.json', 'verify/deployment-tooling.json'})

    def test_validation_report_requires_tool_and_network_tests_for_exact_source(self):
        path = self.root / 'validation.json'
        valid = {'passed': True, 'tool_tests': {'passed': True},
                 'network_integration': {'passed': True}, 'source_tree': 'a' * 64}
        repack.write_json(path, valid)
        self.assertEqual(repack.validation_report(path, {'tree_sha256': 'a' * 64}), valid)
        for updates in ({'passed': 1}, {'tool_tests': True}, {'tool_tests': {'passed': False}},
                        {'network_integration': None}, {'network_integration': True},
                        {'network_integration': {}}, {'network_integration': {'passed': False}},
                        {'network_integration': {'passed': 1}},
                        {'source_tree': 'b' * 64}, {'source_tree': None}):
            repack.write_json(path, dict(valid, **updates))
            with self.subTest(updates=updates), self.assertRaisesRegex(RuntimeError, 'validation report'):
                repack.validation_report(path, {'tree_sha256': 'a' * 64})
        del valid['network_integration']
        repack.write_json(path, valid)
        with self.assertRaisesRegex(RuntimeError, 'network_integration.passed=true'):
            repack.validation_report(path, {'tree_sha256': 'a' * 64})

    def test_cli_requires_validation_report(self):
        arguments = []
        for name in ('base-release', 'trusted-key', 'source', 'output', 'signing-key', 'yaml-package'):
            arguments.extend(['--' + name, 'fixture'])
        arguments.extend(['--version', 'v0.1.0-rc.15'])
        with patch.object(repack, 'repack') as operation, patch('sys.stderr'), self.assertRaises(SystemExit):
            repack.main(arguments)
        operation.assert_not_called()

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL 3 required for real signed repack')
    def test_signed_repack_preserves_base_and_runtime_identities_and_builds_small_patch(self):
        source_root, _ = self.source_fixture()
        implementation = Path(repack.__file__).resolve().parent
        for name in ('build_cli.py', 'ccheck', 'install.sh', 'site-rtx5880.example.json',
                     'INSTALL-RTX5880.template.md'):
            shutil.copy2(implementation / name, source_root / 'deploy/release' / name)
        self.put(source_root, 'deploy/release/cli/ccheck/__init__.py', b'')
        self.put(source_root, 'deploy/release/cli/ccheck/__main__.py', b'def main():\n    return 0\n')
        original = self.freeze(source_root)
        old_source = self.root / 'base-source'
        shutil.copytree(source_root, old_source)
        self.put(source_root, 'deploy/release/cli/ccheck/__main__.py', b'def main():\n    return 1\n')
        source = self.freeze(source_root, commit='b' * 40)
        yaml = self.root / 'yaml'
        self.put(yaml, '__init__.py', b'# vendoring fixture\n')
        base_root = self.root / 'rc14'
        for name in ('images/backend.tar.gz.part0000', 'core.whl', 'core-policy.json',
                     'models/model-manifest.json', 'runtime/qwen/Dockerfile', 'locks/backend.lock',
                     'verify/build-gates.json', 'cli/ccheck.pyz', 'bootstrap.tar.gz',
                     'checksums.sha256', 'README.md'):
            self.put(base_root, name)
        private = self.root / 'private.pem'
        public = self.root / 'trusted.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private)],
                       check=True, capture_output=True)
        private.chmod(0o600)
        subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)],
                       check=True, capture_output=True)
        files = {path.relative_to(base_root).as_posix(): digest(path)
                 for path in base_root.rglob('*') if path.is_file()}
        base = {'format': 1, 'version': 'v0.1.0-rc.14', 'tag': 'release-v0.1.0-rc.14',
                'source': {key: original[key] for key in ('git_sha', 'tree_sha256', 'worktree_snapshot')},
                'files': files, 'images': {'backend': {'id': 'sha256:' + 'a' * 64,
                    'archive_parts': ['images/backend.tar.gz.part0000']}},
                'core_wheel': {'file': 'core.whl', 'sha256': files['core.whl']},
                'profiles': {'ada-48g-fp8': 'requires hardware acceptance'},
                'schema_epoch': 'original-schema', 'runtime_recipe': {'directory': 'runtime/qwen'},
                'validation': {'historical': True}}
        repack.write_json(base_root / 'release.json', base)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(private), '-rawin', '-in',
                        str(base_root / 'release.json'), '-out', str(base_root / 'release.sig')],
                       check=True, capture_output=True)
        before = {path.relative_to(base_root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in base_root.rglob('*') if path.is_file()}
        report = self.root / 'validation.json'
        repack.write_json(report, {'passed': True, 'tool_tests': {'passed': True},
                                  'network_integration': {'passed': True},
                                  'source_tree': source['tree_sha256']})
        args = SimpleNamespace(base_release=base_root, trusted_key=public, source=source_root,
                               base_source=old_source, output=self.root / 'rc15', signing_key=private,
                               yaml_package=yaml, validation_report=report, version='v0.1.0-rc.15')
        # Windows cannot represent POSIX 0600; test that policy separately above.
        actual_key = repack.signing_key
        with patch.object(repack.subprocess, 'run', wraps=subprocess.run) as commands, \
                patch.object(repack, 'signing_key', side_effect=(
                lambda path, roots: path.resolve()) if os.name == 'nt' else actual_key):
            result = repack.repack(args)
        self.assertTrue(all(call.args[0][0] in {'openssl', sys.executable} for call in commands.call_args_list))
        self.assertEqual(before, {path.relative_to(base_root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in base_root.rglob('*') if path.is_file()})
        for name in ('images', 'core_wheel', 'profiles', 'runtime_recipe', 'schema_epoch'):
            self.assertEqual(result[name], base[name])
        self.assertEqual(result['source']['git_sha'], 'b' * 40)
        self.assertEqual(digest(args.output / 'publisher.pem'), digest(public))
        self.assertEqual(result['distribution'], {
            'kind': 'deployment-patch', 'base_version': base['version'],
            'base_release_sha256': digest(base_root / 'release.json'),
            'reused_files': sorted(name for name in result['files'] if name.startswith('images/'))})
        self.assertFalse(result['validation']['gpu']['tested_this_release'])
        self.assertEqual(result['validation']['inherited_metadata'], base['validation'])
        self.assertEqual((args.output / 'verify/deployment-tooling.json').read_bytes(), report.read_bytes())
        self.assertTrue(os.path.samefile(base_root / 'images/backend.tar.gz.part0000',
                                        args.output / 'images/backend.tar.gz.part0000'))
        self.assertEqual(set(path.name for path in (self.root / 'rc15-patch').iterdir()), set(repack.PATCH_FILES))
        with tarfile.open(args.output / 'bootstrap.tar.gz') as archive:
            self.assertFalse(set(archive.getnames()) & repack.METADATA)
            self.assertFalse(any(name.startswith('images/') for name in archive.getnames()))
            self.assertIn('cli/ccheck.pyz', archive.getnames())
        with zipfile.ZipFile(args.output / 'cli/ccheck.pyz') as archive:
            self.assertIn(b'return 1', archive.read('ccheck/__main__.py'))
        guide = (args.output / 'INSTALL-RTX5880.md').read_text(encoding='utf-8')
        self.assertIn(args.version, guide)
        self.assertIn(digest(args.output / 'publisher.pem'), guide)
        self.assertEqual(set(result['artifact_sizes']), set(result['files']))
        for name, size in result['artifact_sizes'].items():
            self.assertEqual(size, (args.output / name).stat().st_size)
        expected_checksums = ''.join(checksum + '  ' + name + '\n'
                                     for name, checksum in sorted(result['files'].items())
                                     if name != 'checksums.sha256')
        self.assertEqual((args.output / 'checksums.sha256').read_text(), expected_checksums)
        verify_release(args.output, public)
        for name in files:
            if name not in repack.REPLACED:
                self.assertEqual(digest(args.output / name), files[name])
        alternate_key = self.root / 'alternate-private.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(alternate_key)],
                       check=True, capture_output=True)
        alternate_key.chmod(0o600)
        wrong_identity = SimpleNamespace(**vars(args))
        wrong_identity.output = self.root / 'wrong-publisher-output'
        wrong_identity.signing_key = alternate_key
        with patch.object(repack, 'signing_key', return_value=alternate_key), \
                self.assertRaisesRegex(RuntimeError, 'publisher identity must match'):
            repack.repack(wrong_identity)
        self.assertEqual(set(path.name for path in wrong_identity.output.iterdir()), {'publisher.pem'})
        self.assertFalse(wrong_identity.output.with_name(wrong_identity.output.name + '-patch').exists())
        self.assertEqual(before, {path.relative_to(base_root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in base_root.rglob('*') if path.is_file()})
        (args.output / 'README.md').write_text('tampered')
        with self.assertRaisesRegex(DeploymentError, 'checksum'):
            verify_release(args.output, public)
        with patch.object(repack, 'signing_key', return_value=private), self.assertRaisesRegex(RuntimeError, 'must be new'):
            repack.repack(args)
        args.output = self.root / 'tampered-base-output'
        (base_root / 'models/model-manifest.json').write_text('tampered baseline')
        with patch.object(repack, 'signing_key', return_value=private), self.assertRaisesRegex(DeploymentError, 'checksum'):
            repack.repack(args)
        self.assertFalse(args.output.exists())


if __name__ == '__main__':
    unittest.main()
