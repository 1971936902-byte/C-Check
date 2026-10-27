import unittest
from unittest.mock import patch
import terminal


class TerminalTests(unittest.TestCase):
    def test_official_guacamole_hash_fixture(self):
        salt = bytes.fromhex('FE24ADC5E11E2B25288D1704ABE67A79E342ECC26064CE69C5B3177795A82264')
        self.assertEqual(terminal.password_hash('guacadmin', salt),
                         'ca458a7d494e3be824f5e1e175a1556c0f8eef2c2d7df3633bec4a29c4411960')

    def test_network_overlap_is_rejected(self):
        for networks, occupied in [(['172.31.240.0/26', '172.31.240.0/25'], []),
                                    (['172.31.240.0/26'], ['172.31.0.0/16']),
                                    (['8.8.8.0/24'], [])]:
            with self.assertRaises(ValueError):
                terminal.validate_networks(networks, occupied)

    def test_disjoint_private_networks(self):
        terminal.validate_networks(['172.31.240.0/26', '172.31.240.64/26'], ['172.18.0.0/16'])

    def test_connection_requires_host_key_and_no_password(self):
        config = {'name': "Test ' host", 'hostname': 'host.docker.internal', 'port': 22,
                  'username': 'vipuser', 'host-key': 'ssh-ed25519 AAAA'}
        sql = terminal.connection_sql(config)
        self.assertIn("'Test '' host'", sql)
        self.assertIn("'host-key', 'host.docker.internal ssh-ed25519 AAAA'", sql)
        self.assertIn('[host.docker.internal]:2222 ssh-ed25519 AAAA', terminal.connection_sql({**config, 'port': 2222}))
        self.assertIn("'enable-sftp', 'false'", sql)
        for value in [{**config, 'host-key': ''}, {**config, 'password': 'secret'},
                      {**config, 'port': 0}, {**config, 'hostname': 'host\nattack'}]:
            with self.assertRaises(ValueError):
                terminal.connection_sql(value)

    def test_operations_are_scoped_to_own_project(self):
        with patch.object(terminal, 'run') as run:
            terminal.compose('stop')
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--project-name') + 1], 'ccheck-terminal')
            self.assertNotIn('--volumes', argv)
            self.assertNotIn('down', argv)


if __name__ == '__main__':
    unittest.main()
