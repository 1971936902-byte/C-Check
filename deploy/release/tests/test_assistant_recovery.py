import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck import assistant_recovery
from ccheck.core import DeploymentError
from ccheck.__main__ import main


class AssistantRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'data/control').mkdir(parents=True)
        self.site = {'instance': 'recover-test', 'data_dir': temporary.name,
            'profile': 'ada-48g-fp8', 'performance': {}, 'assistant': {'enabled': True}}
        self.engine = MagicMock()
        self.engine.site, self.engine.root = self.site, self.root
        def maintenance(value):
            marker = self.root / 'data/control/maintenance'
            marker.touch() if value else marker.unlink(missing_ok=True)
        self.engine.maintenance.side_effect = maintenance

    def test_models_must_be_replaced_and_healthy_before_database_release(self):
        assistant_recovery.recover_stack(self.engine, 'release')
        calls = self.engine.mock_calls
        finish = next(i for i, value in enumerate(calls) if value.args and value.args[0] == 'assistant-recovery-finish')
        for expected in (call.compose('stop', '--timeout', '120', 'qwen', 'embedding'),
                         call.wait(['qwen']), call.wait(['embedding']), call.wait(['assistant-gateway', 'anythingllm'])):
            self.assertLess(calls.index(expected), finish)
        self.assertFalse((self.root / 'data/control/maintenance').exists())
        self.assertEqual(json.loads((self.root / 'state/assistant-recovery.json').read_text())['status'], 'completed')

    def test_failed_health_never_releases_and_retry_keeps_identity(self):
        self.engine.wait.side_effect = DeploymentError('unhealthy')
        with self.assertRaises(DeploymentError):
            assistant_recovery.recover_stack(self.engine, 'release')
        path = self.root / 'state/assistant-recovery.json'
        failed = json.loads(path.read_text())
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue((self.root / 'data/control/maintenance').exists())
        self.assertFalse(any(value.args and value.args[0] == 'assistant-recovery-finish' for value in self.engine.management.call_args_list))
        with self.assertRaises(DeploymentError):
            assistant_recovery.assert_no_pending(self.site)
        self.engine.wait.side_effect = None
        assistant_recovery.recover_stack(self.engine, 'release')
        self.assertEqual(json.loads(path.read_text())['token'], failed['token'])
        self.assertFalse((self.root / 'data/control/maintenance').exists())

    def test_busy_review_refusal_does_not_stop_services_or_enable_maintenance(self):
        self.engine.management.side_effect = DeploymentError('review is active')
        with self.assertRaises(DeploymentError):
            assistant_recovery.recover_stack(self.engine, 'release')
        self.engine.compose.assert_not_called()
        self.engine.maintenance.assert_not_called()
        self.assertEqual(json.loads((self.root / 'state/assistant-recovery.json').read_text())['status'], 'blocked')

    def test_update_carries_explicit_assistant_configuration(self):
        (self.root / 'config').mkdir()
        (self.root / 'config/site.json').write_text('{}')
        incoming = dict(self.site, assistant={'enabled': True})
        saved = copy.deepcopy(self.site)
        del saved['assistant']
        with patch('sys.argv', ['ccheck', 'update', '--config', 'incoming.json', '--backup-key', 'backup.key']), \
                patch('ccheck.__main__.load_site', side_effect=[incoming, saved]), \
                patch('ccheck.__main__.lifecycle.update') as update:
            self.assertEqual(main(), 0)
        self.assertEqual(update.call_args.args[0]['assistant'], {'enabled': True})


if __name__ == '__main__':
    unittest.main()
