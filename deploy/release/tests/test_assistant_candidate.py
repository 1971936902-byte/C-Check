import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'assistant/build_backend_candidate.py'
spec = importlib.util.spec_from_file_location('assistant_candidate_builder', PATH)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AssistantCandidateTests(unittest.TestCase):
    def test_worktree_snapshot_retains_canonical_unchanged_bytes(self):
        from pack_source import snapshot_data
        with patch('pack_source.git', return_value=b'canonical\n') as git, \
                patch('pathlib.Path.read_bytes', return_value=b'working\r\n') as read:
            self.assertEqual(snapshot_data('tracked.py', 'a' * 40, working_tree=True,
                changed=set(), committed_names={'tracked.py'}), b'canonical\n')
            read.assert_not_called()
            git.assert_called_once()

    def test_worktree_snapshot_keeps_changed_and_new_files_exactly(self):
        from pack_source import snapshot_data
        for name in ('modified.py', 'new.py'):
            with self.subTest(name=name), patch('pack_source.git') as git, \
                    patch('pathlib.Path.read_bytes', return_value=b'edited\r\n'):
                self.assertEqual(snapshot_data(name, 'a' * 40, working_tree=True,
                    changed={'modified.py'}, committed_names={'modified.py'}), b'edited\r\n')
                git.assert_not_called()

    def setUp(self):
        self.previous = {'git_sha': 'a' * 40, 'tree_sha256': 'b' * 64,
            'files': {'backend/app/core.py': 'original', 'backend/pyproject.toml': 'dependencies'}}
        self.current = copy.deepcopy(self.previous)
        self.current['files']['backend/app/assistant_gateway.py'] = 'added'
        self.base = {'version': 'v0.1.0-rc.20', 'source': self.previous}
        self.policy = {'compiled_modules': ['app.core']}

    def validate(self):
        builder.validate_sources(self.base, self.current, self.previous, self.policy,
                                 {'compiled_modules': ['app.core']})

    def test_allows_new_assistant_without_native_changes(self):
        self.validate()

    def test_refuses_a3b_baseline(self):
        self.base['version'] = 'v0.1.0-rc.17'
        with self.assertRaisesRegex(RuntimeError, 'baseline'):
            self.validate()

    def test_refuses_unbound_baseline_source(self):
        self.base = dict(self.base, source=dict(self.previous, tree_sha256='c' * 64))
        with self.assertRaisesRegex(RuntimeError, 'source does not match'):
            self.validate()

    def test_refuses_changed_native_or_dependencies(self):
        for name in ('backend/app/core.py', 'backend/pyproject.toml', 'backend/requirements.txt'):
            with self.subTest(name=name):
                self.current = copy.deepcopy(self.previous)
                self.current['files'][name] = 'changed'
                with self.assertRaisesRegex(RuntimeError, 'compilation is required'):
                    self.validate()

    def test_refuses_changed_module_policy(self):
        self.policy['compiled_modules'] = ['app.different']
        with self.assertRaisesRegex(RuntimeError, 'policy changed'):
            self.validate()
