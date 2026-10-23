import copy
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError
from ccheck.engine import Engine, project_name, render_compose
from ccheck import networks
from ccheck.lifecycle import assert_no_pending_network_migration, begin_network_migration, finish_network_migration, recover_network


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = {'instance': 'network-test', 'data_dir': temporary.name, 'gpu': '0',
                     'profile': 'ada-48g-fp8', 'web': {'bind': '127.0.0.1', 'port': 18800,
                     'public_url': 'http://127.0.0.1:18800'}}
        self.engine = Engine(self.site)
        self.project = project_name(self.site)

    def network(self, name='data', cidr=None):
        return {'Id': 'a' * 64, 'Name': self.project + '_' + name, 'Driver': 'bridge',
                'Internal': name != 'edge', 'IPAM': {'Driver': 'default', 'Config': [
                    {'Subnet': cidr or networks.DEFAULT_SUBNETS[name]}]}, 'Containers': {},
                'Labels': {'com.docker.compose.project': self.project, 'com.docker.compose.network': name}}

    def test_fixed_recipe_matches_review_and_preserves_internal_networks(self):
        images = {name: 'sha256:' + 'a' * 64 for name in
                  ['backend', 'mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'frontend']}
        document = render_compose(self.site, images, 10001, 10001)
        self.assertEqual(document['networks'], networks.specifications(self.site))
        self.assertEqual([document['networks'][name]['ipam']['config'][0]['subnet']
                          for name in networks.DEFAULT_SUBNETS], list(networks.DEFAULT_SUBNETS.values()))
        self.assertTrue(document['networks']['data']['internal'])
        self.assertTrue(document['networks']['inference']['internal'])
        self.assertNotIn('internal', document['networks']['edge'])

    def test_invalid_subnets_are_rejected(self):
        for value in [{}, {'data': 'all'}, {**networks.DEFAULT_SUBNETS, 'edge': '172.30.10.1/26'},
                      {**networks.DEFAULT_SUBNETS, 'edge': '172.30.10.0/26'},
                      {**networks.DEFAULT_SUBNETS, 'edge': '8.8.8.0/24'},
                      {**networks.DEFAULT_SUBNETS, 'edge': '0.0.0.0/16'},
                      {**networks.DEFAULT_SUBNETS, 'edge': '::/64'}]:
            with self.subTest(value=value), self.assertRaises(DeploymentError):
                networks.subnets({'network_subnets': value})

    def test_other_docker_network_overlap_is_rejected(self):
        row = self.network(cidr='172.30.10.0/24')
        row.update(Name='another-project', Labels={})
        with patch('ccheck.networks.inventory', return_value=[row]), self.assertRaisesRegex(DeploymentError, 'another Docker'):
            networks.preflight(self.site)

    def test_route_overlap_rejected_but_default_route_allowed(self):
        with patch('ccheck.networks.inventory', return_value=[]), patch('ccheck.networks.run', return_value='[{"dst":"default"}]'):
            self.assertFalse(networks.preflight(self.site)['conflicts'])
        with patch('ccheck.networks.inventory', return_value=[]), \
                patch('ccheck.networks.run', return_value='[{"dst":"172.30.0.0/16","dev":"vpn0"}]'), \
                self.assertRaisesRegex(DeploymentError, 'LAN/VPN'):
            networks.preflight(self.site)

    def test_only_owned_bridge_route_is_ignored(self):
        row = self.network()
        route = {'dst': row['IPAM']['Config'][0]['Subnet'], 'dev': 'br-' + row['Id'][:12]}
        with patch('ccheck.networks.inventory', return_value=[row]), patch('ccheck.networks.run', return_value=json.dumps([route])):
            networks.preflight(self.site)
        route['dst'] = '172.30.10.1/32'
        route['table'] = 'local'
        with patch('ccheck.networks.inventory', return_value=[row]), patch('ccheck.networks.run', return_value=json.dumps([route])):
            networks.preflight(self.site)
        route['dev'] = 'eth0'
        with patch('ccheck.networks.inventory', return_value=[row]), patch('ccheck.networks.run', return_value=json.dumps([route])), \
                self.assertRaises(DeploymentError):
            networks.preflight(self.site)

    def test_policy_route_tables_are_included(self):
        with patch('ccheck.networks.inventory', return_value=[]), \
                patch('ccheck.networks.run', return_value='[{"dst":"172.30.10.0/24","dev":"vpn0","table":100}]') as run, \
                self.assertRaises(DeploymentError):
            networks.preflight(self.site)
        self.assertEqual(run.call_args.args[0], ['ip', '-j', '-4', 'route', 'show', 'table', 'all'])

    def test_namespace_collision_and_foreign_endpoint_rejected(self):
        row = self.network()
        row['Labels'] = {}
        with self.assertRaisesRegex(DeploymentError, 'another owner'):
            networks.assert_scope(self.engine, [row])
        row = self.network()
        row['Containers'] = {'foreign-id': {}}
        with patch('ccheck.networks.run', return_value='[{"Config":{"Labels":{}}}]'), \
                self.assertRaisesRegex(DeploymentError, 'foreign endpoint'):
            networks.assert_scope(self.engine, [row])

    def test_teardown_is_scoped_and_never_deletes_volumes(self):
        with patch('ccheck.networks.inventory', return_value=[self.network()]), patch.object(self.engine, 'compose') as compose:
            networks.teardown(self.engine)
        compose.assert_called_once_with('down', '--timeout', '120')

    def test_existing_auto_network_requires_recreate(self):
        row = self.network(cidr='172.19.0.0/16')
        document = {'networks': networks.specifications(self.site)}
        with patch('ccheck.networks.inventory', return_value=[row]):
            self.assertTrue(networks.needs_recreate(self.engine, document))
        with patch('ccheck.networks.inventory', return_value=[self.network()]):
            self.assertFalse(networks.needs_recreate(self.engine, document))

    def test_actual_subnet_internal_driver_and_ipam_drift_fail(self):
        expected = networks.specifications(self.site)['data']
        for key, value in [('Driver', 'overlay'), ('Internal', False), ('EnableIPv6', True),
                           ('IPAM', {'Driver': 'other', 'Config': [{'Subnet': networks.DEFAULT_SUBNETS['data']}]})]:
            row = self.network()
            row[key] = value
            self.assertFalse(networks.matches(row, expected))

    def test_running_networks_must_all_exist(self):
        self.engine.file.parent.mkdir()
        self.engine.file.write_text(json.dumps({'networks': networks.specifications(self.site)}))
        rows = [self.network(name) for name in networks.DEFAULT_SUBNETS]
        with patch('ccheck.networks.inventory', return_value=rows):
            self.assertTrue(networks.verify(self.engine)['verified'])
        with patch('ccheck.networks.inventory', return_value=rows[:1]), self.assertRaises(DeploymentError):
            networks.verify(self.engine)

    def test_container_cannot_join_extra_network_or_wrong_subnet(self):
        document = {'networks': networks.specifications(self.site)}
        actual = {self.project + '_data': {'IPAddress': '172.30.10.2'}}
        inspection = {'NetworkSettings': {'Networks': actual}}
        networks.verify_container(self.engine, {'networks': ['data']}, inspection, document)
        actual[self.project + '_data']['IPAddress'] = '192.168.1.2'
        with self.assertRaises(DeploymentError):
            networks.verify_container(self.engine, {'networks': ['data']}, inspection, document)
        actual['other'] = {'IPAddress': '172.30.10.3'}
        with self.assertRaises(DeploymentError):
            networks.verify_container(self.engine, {'networks': ['data']}, inspection, document)

    def test_journal_blocks_restart_until_completed_and_preserves_previous_recipe(self):
        document = {'name': self.project, 'networks': {'data': {}, 'inference': {}, 'edge': {}}}
        begin_network_migration(self.engine, self.site, document, {'version': 'v0.1.0-rc.14'})
        with self.assertRaisesRegex(DeploymentError, 'network-recover'):
            assert_no_pending_network_migration(self.engine.root)
        value = json.loads((self.engine.root / 'state/network-migration.json').read_text())
        self.assertEqual(value['previous_compose'], document)
        finish_network_migration(self.engine.root)
        assert_no_pending_network_migration(self.engine.root)

    def test_wrong_previous_namespace_cannot_create_migration_journal(self):
        with self.assertRaises(DeploymentError):
            begin_network_migration(self.engine, self.site, {'name': 'other'}, None)
        self.assertFalse((self.engine.root / 'state/network-migration.json').exists())

    def committed_fixture(self):
        images = {name: 'sha256:' + 'a' * 64 for name in
                  ['backend', 'mysql', 'redis', 'qdrant', 'qwen', 'embedding', 'frontend']}
        document = render_compose(self.site, images, 10001, 10001)
        begin_network_migration(self.engine, self.site, document, {'release_sha256': 'old'}, 'new')
        self.engine.file.write_text(json.dumps(document))
        (self.engine.root / 'config').mkdir()
        (self.engine.root / 'config/site.json').write_text(json.dumps(self.site))
        (self.engine.root / 'state/deployment.json').write_text('{}')
        (self.engine.root / 'data/control').mkdir(parents=True)
        return {'release_sha256': 'new', 'images': images, 'uid': 10001, 'gid': 10001,
                'release_root': '/new-release', 'publisher': 'fingerprint'}

    def test_commit_window_interruption_finalizes_without_downgrade_or_teardown(self):
        state = self.committed_fixture()
        with patch('ccheck.lifecycle.installation_lock', return_value=contextlib.nullcontext()), \
                patch('ccheck.lifecycle.installed', return_value=state), \
                patch('ccheck.core.load_site', return_value=self.site), \
                patch('ccheck.lifecycle.verify_release'), patch('ccheck.lifecycle.start_stack') as start, \
                patch('ccheck.lifecycle.verify') as verify, patch('ccheck.lifecycle.networks.teardown') as down:
            recover_network(self.site)
        self.assertEqual(json.loads((self.engine.root / 'state/network-migration.json').read_text())['status'], 'completed')
        down.assert_not_called()
        start.assert_called_once()
        verify.assert_called_once_with(self.site)

    def test_failed_commit_finalization_retains_pending_journal(self):
        state = self.committed_fixture()
        with patch('ccheck.lifecycle.installation_lock', return_value=contextlib.nullcontext()), \
                patch('ccheck.lifecycle.installed', return_value=state), \
                patch('ccheck.core.load_site', return_value=self.site), patch('ccheck.lifecycle.verify_release'), \
                patch('ccheck.lifecycle.start_stack'), \
                patch('ccheck.lifecycle.verify', side_effect=DeploymentError('not ready')), self.assertRaises(DeploymentError):
            recover_network(self.site)
        self.assertEqual(json.loads((self.engine.root / 'state/network-migration.json').read_text())['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
