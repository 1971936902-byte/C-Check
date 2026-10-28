import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import shutil
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import remote_terminal
from ccheck.core import DeploymentError


class RemoteTerminalSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.site = {'instance': 'terminal-test', 'data_dir': str(root / 'application'),
                     'terminal': {'enabled': True}}

    def test_disabled_defaults_do_not_require_runtime_or_modify_legacy_site(self):
        site = {'instance': 'legacy-test'}
        self.assertEqual(remote_terminal.settings(site), {'enabled': False})
        self.assertEqual(site, {'instance': 'legacy-test'})

    def test_default_terminal_storage_is_independent_and_private_bind(self):
        before = copy.deepcopy(self.site)
        settings = remote_terminal.settings(self.site)
        self.assertEqual(settings['bind'], '127.0.0.1')
        self.assertEqual(settings['port'], 8803)
        self.assertEqual(settings['image_policy'], 'online')
        self.assertEqual(settings['public_url'], 'https://localhost:8803/guacamole/')
        self.assertNotEqual(settings['data_dir'], self.site['data_dir'])
        self.assertEqual(self.site, before)

    def test_lan_binding_requires_explicit_certificate_files(self):
        self.site['terminal'].update(bind='192.168.1.10', public_url='https://192.168.1.10:8843/guacamole/', port=8843)
        with self.assertRaisesRegex(DeploymentError, 'TLS'):
            remote_terminal.settings(self.site)
        root = Path(self.site['data_dir']).parent
        self.site['terminal']['tls'] = {'cert_file': str(root / 'tls.crt'), 'key_file': str(root / 'tls.key')}
        self.assertEqual(remote_terminal.settings(self.site)['port'], 8843)

    def test_terminal_cannot_store_state_in_application_or_its_parent(self):
        application = Path(self.site['data_dir'])
        for path in (application, application / 'terminal', application.parent):
            with self.subTest(path=path):
                self.site['terminal']['data_dir'] = str(path)
                with self.assertRaises(DeploymentError):
                    remote_terminal.settings(self.site)

    def test_invalid_config_and_network_overlap_are_rejected(self):
        for change in ({'port': True}, {'port': 443}, {'bind': 'not-an-ip'},
                       {'image_policy': 'latest'}, {'public_url': 'http://localhost:8803/guacamole/'},
                       {'public_url': 'https://user:pass@localhost/guacamole/'},
                       {'public_url': 'https://localhost/not-guacamole'}, {'unknown': 'value'},
                       {'public_url': 'https://bad$host:8803/guacamole/'},
                       {'subnets': {'data': '172.30.10.0/26'}},
                       {'subnets': {'data': '172.31.240.0/25', 'ssh': '172.31.240.0/26'}}):
            with self.subTest(change=change):
                candidate = copy.deepcopy(self.site)
                candidate['terminal'].update(change)
                with self.assertRaises(DeploymentError):
                    remote_terminal.settings(candidate)

    def test_target_configuration_does_not_accept_stored_passwords(self):
        target = {'name': 'Linux server', 'hostname': '10.0.0.10', 'port': 22,
                  'username': 'operator', 'host-key': 'ssh-ed25519 AAAA'}
        self.site['terminal']['connections'] = [target]
        self.assertEqual(remote_terminal.settings(self.site)['connections'], [target])
        for changes in ({'password': 'never-store'}, {'port': True}, {'host-key': ''}, {'username': ''}):
            with self.subTest(changes=changes):
                self.site['terminal']['connections'] = [dict(target, **changes)]
                with self.assertRaises(DeploymentError):
                    remote_terminal.settings(self.site)

    def test_runtime_resources_are_explicit_and_hash_bound(self):
        hashes = remote_terminal.hashes()
        self.assertEqual(set(hashes), set(remote_terminal.RESOURCES))
        self.assertTrue(all(len(value) == 64 for value in hashes.values()))
        with self.assertRaises(DeploymentError):
            remote_terminal.resource('../secret')

    def test_combined_installer_contains_terminal_and_assistant_without_source_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            yaml = root / 'yaml'
            yaml.mkdir()
            (yaml / '__init__.py').write_text('# pure Python fixture\n')
            output = root / 'ccheck.pyz'
            subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'build_cli.py'),
                '--output', str(output), '--yaml-package', str(yaml), '--with-assistant', '--with-terminal'],
                check=True, capture_output=True, timeout=60)
            with zipfile.ZipFile(output) as archive:
                self.assertIn('ccheck/assistant_runtime/vllm_control.py', archive.namelist())
                self.assertIn('ccheck/terminal_runtime/compose.yaml', archive.namelist())
            program = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                       'from ccheck.remote_terminal import hashes;print(json.dumps(hashes()))')
            result = subprocess.check_output([sys.executable, '-B', '-c', program, str(output)], cwd=root, timeout=30)
            self.assertEqual(json.loads(result), remote_terminal.hashes())

    def test_managed_and_manual_links_cannot_conflict(self):
        self.site['web'] = {'terminal_config': '/operator/terminal.json'}
        with self.assertRaisesRegex(DeploymentError, 'not both'):
            remote_terminal.settings(self.site)

    def test_project_names_are_independent_and_stable(self):
        config = remote_terminal.settings(self.site)
        first = remote_terminal.project_name(self.site, config)
        self.assertEqual(first, remote_terminal.project_name(self.site, config))
        self.assertNotEqual(first, remote_terminal.project_name(dict(self.site, instance='another-test'), config))
        self.assertNotEqual(first, remote_terminal.project_name(self.site, dict(config, data_dir='/other/terminal')))
        self.assertLessEqual(len(remote_terminal.project_name(dict(self.site, instance='a' * 36), config)), 63)

    def test_runtime_map_rejects_extra_paths_or_non_hash_values(self):
        mapping = remote_terminal.hashes()
        self.assertEqual(len(remote_terminal.package_id(mapping)), 64)
        for bad in (None, dict(mapping, **{'../outside': '0' * 64}), dict(mapping, **{'terminal.py': 'wrong'})):
            with self.assertRaises(DeploymentError):
                remote_terminal.package_id(bad)

    def test_public_link_update_preserves_live_bind_inode_and_contains_no_secrets(self):
        root = Path(self.site['data_dir'])
        (root / 'state').mkdir(parents=True)
        (root / 'config').mkdir()
        (root / 'state/owner.json').write_text(json.dumps({'instance': self.site['instance']}))
        remote_terminal.prepare_public_config(self.site)
        target = root / 'config/terminal-public.json'
        first = target.stat().st_ino
        self.assertEqual(json.loads(target.read_text()), {'url': 'https://localhost:8803/guacamole/'})
        self.site['terminal'] = {'enabled': False}
        remote_terminal.prepare_public_config(self.site)
        self.assertEqual(target.stat().st_ino, first)
        self.assertEqual(json.loads(target.read_text()), {'url': ''})

    def test_public_link_does_not_create_unowned_application(self):
        remote_terminal.prepare_public_config(self.site)
        self.assertFalse(Path(self.site['data_dir']).exists())

    def operation_fixture(self):
        config = remote_terminal.settings(self.site)
        record = {'instance': self.site['instance'], 'project': remote_terminal.project_name(self.site, config),
            'settings': config, 'stage': 'ready'}
        module = Mock()
        @contextmanager
        def locked(*args, **kwargs):
            yield Path(config['data_dir']), record
        return config, record, module, locked

    def test_stop_does_not_call_down_or_reset_state_when_configuration_changed(self):
        config, record, module, locked = self.operation_fixture()
        self.site['terminal']['port'] = 8804
        with patch.object(remote_terminal, 'locked_root', locked), \
                patch.object(remote_terminal, 'cached_runtime', return_value=module), \
                patch.object(remote_terminal, 'validate_state'):
            remote_terminal.operate(self.site, 'stop')
        module.compose.assert_called_once_with('stop', '--timeout', '30')
        self.assertEqual(record['stage'], 'ready')

    def test_start_rejects_unapplied_configuration_before_starting_containers(self):
        config, record, module, locked = self.operation_fixture()
        self.site['terminal']['port'] = 8804
        with patch.object(remote_terminal, 'locked_root', locked), \
                patch.object(remote_terminal, 'cached_runtime', return_value=module), \
                patch.object(remote_terminal, 'validate_state'), \
                self.assertRaisesRegex(DeploymentError, 'configuration changed'):
            remote_terminal.operate(self.site, 'start')
        module.compose.assert_not_called()

    def test_start_retry_is_idempotent_and_journaled(self):
        config, record, module, locked = self.operation_fixture()
        record['stage'] = 'starting'
        with patch.object(remote_terminal, 'locked_root', locked), \
                patch.object(remote_terminal, 'cached_runtime', return_value=module), \
                patch.object(remote_terminal, 'validate_state'), \
                patch.object(remote_terminal, 'configure_library'), \
                patch.object(remote_terminal, 'atomic_json') as journal, \
                patch.object(remote_terminal, 'start_runtime') as start:
            remote_terminal.operate(self.site, 'start')
        start.assert_called_once_with(module, record)
        self.assertEqual(record['stage'], 'ready')
        self.assertEqual(journal.call_count, 2)

    def test_start_checks_offline_images_before_compose(self):
        root = Path(self.site['data_dir']).parent
        lock = root / 'lock.json'
        lock.write_text(json.dumps({'GUACD_IMAGE': {'image': 'pinned'}}))
        module = Mock(LOCK=lock)
        module.runtime_image.side_effect = ValueError('Offline terminal image is missing')
        with self.assertRaisesRegex(ValueError, 'Offline'):
            remote_terminal.start_runtime(module, {'settings': remote_terminal.settings(self.site)})
        module.compose.assert_not_called()

    def test_interrupted_first_image_download_can_resume_without_resetting_credentials(self):
        config, record, module, locked = self.operation_fixture()
        record.update(stage='installing', publisher='publisher-sha', runtime=remote_terminal.package_id(remote_terminal.hashes()))
        root = Path(config['data_dir'])
        root.mkdir(parents=True)
        module.STATE = root / 'state'
        lock_file = root / 'images.lock.json'
        lock_file.write_bytes(remote_terminal.resource('images.lock.json'))
        module.LOCK = lock_file
        release_root = root / 'fixture'
        release_root.mkdir()
        for name in ('release.json', 'release.sig', 'publisher.pem'):
            (release_root / name).write_bytes(b'fixture')
        release = {'_publisher': 'publisher-sha', '_sha256': 'a' * 64, 'terminal_runtime': remote_terminal.hashes()}
        with patch.object(remote_terminal, 'locked_root', locked), \
                patch.object(remote_terminal, 'verify_release', return_value=release), \
                patch.object(remote_terminal, 'cached_runtime', return_value=module), \
                patch.object(remote_terminal, 'configure_library'), \
                patch.object(remote_terminal, 'validate_state') as validate, \
                patch.object(remote_terminal, 'prepare_public_config'):
            remote_terminal.install(self.site, release_root, release_root / 'publisher.pem')
        module.initialize.assert_called_once()
        # No pre-initialization state adoption: validation only follows initialization.
        validate.assert_called_once()

    @unittest.skipUnless(os.name == 'posix' and shutil.which('openssl'), 'Linux/OpenSSL trust verification')
    def test_cached_runtime_is_signed_and_tampering_is_rejected_without_original_release(self):
        root = Path(self.site['data_dir']).parent / 'private-terminal'
        root.mkdir(mode=0o700)
        private = root / 'test.key'
        public = root / 'test.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
        mapping = remote_terminal.hashes()
        manifest = json.dumps({'terminal_runtime': mapping}).encode()
        release_sha = remote_terminal.hashlib.sha256(manifest).hexdigest()
        cache = root / 'releases' / release_sha
        cache.mkdir(parents=True, mode=0o700)
        (cache / 'release.json').write_bytes(manifest)
        (cache / 'publisher.pem').write_bytes(public.read_bytes())
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(private), '-rawin', '-in',
            str(cache / 'release.json'), '-out', str(cache / 'release.sig')], check=True, capture_output=True)
        identifier = remote_terminal.package_id(mapping)
        runtime = root / 'runtime' / identifier
        runtime.mkdir(parents=True, mode=0o700)
        for name in remote_terminal.RESOURCES:
            (runtime / name).write_bytes(remote_terminal.resource(name))
        record = {'release_sha256': release_sha, 'publisher': remote_terminal.digest(public),
            'runtime': identifier, 'project': 'terminal-test'}
        module = remote_terminal.cached_runtime(root, record)
        self.assertEqual(module.PROJECT, 'terminal-test')
        self.assertEqual(module.STATE, root / 'state')
        (runtime / 'nginx.conf').write_text('tampered runtime')
        with self.assertRaisesRegex(DeploymentError, 'runtime was modified'):
            remote_terminal.cached_runtime(root, record)
        (runtime / 'nginx.conf').write_bytes(remote_terminal.resource('nginx.conf'))
        (cache / 'release.sig').write_bytes(b'0' * 64)
        with self.assertRaises(DeploymentError):
            remote_terminal.cached_runtime(root, record)

    @unittest.skipUnless(os.name == 'posix', 'Linux ownership checks')
    def test_unowned_directory_and_foreign_owner_are_not_adopted(self):
        config = remote_terminal.settings(self.site)
        root = Path(config['data_dir'])
        root.mkdir(mode=0o700)
        (root / 'existing-data').write_text('must remain untouched')
        with self.assertRaisesRegex(DeploymentError, 'unowned'):
            with remote_terminal.locked_root(self.site, config):
                self.fail('unowned directory was accepted')
        self.assertEqual((root / 'existing-data').read_text(), 'must remain untouched')
        (root / 'owner.json').write_text(json.dumps({'instance': 'foreign-instance', 'project': 'foreign-project'}))
        (root / 'owner.json').chmod(0o600)
        with self.assertRaisesRegex(DeploymentError, 'ownership'):
            with remote_terminal.locked_root(self.site, config):
                self.fail('foreign owner was accepted')
