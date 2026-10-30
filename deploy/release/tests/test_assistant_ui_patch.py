import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_assistant_ui_patch import BASE_SHA, validate_sources


class AssistantUiPatchTests(unittest.TestCase):
    def baseline(self):
        previous = {'git_sha': 'a' * 40, 'tree_sha256': 'b' * 64,
                    'files': {'frontend/src/views/AssistantView.vue': 'old'}}
        base = {'version': 'v0.1.0-rc.22', '_sha256': BASE_SHA, 'source': previous}
        source = copy.deepcopy(previous)
        source['files']['frontend/src/views/AssistantView.vue'] = 'new'
        return base, source, previous

    def test_only_approved_assistant_frontend_and_publication_files_change(self):
        self.assertEqual(validate_sources(*self.baseline()), ['frontend/src/views/AssistantView.vue'])
        for name in ('backend/app/main.py', 'deploy/release/cli/ccheck/launchers/qwen_gpu.py',
                     'deploy/models/manifest.json', 'frontend/src/views/AdminView.vue'):
            base, source, old = self.baseline()
            source['files'][name] = 'forbidden'
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                validate_sources(base, source, old)

    def test_different_release_or_publisher_manifest_cannot_be_used(self):
        for field, value in (('version', 'v0.1.0-rc.17'), ('version', 'v0.1.0-rc.20'), ('_sha256', '0' * 64)):
            base, source, old = self.baseline()
            base[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                validate_sources(base, source, old)

    def test_source_provenance_and_nonempty_scope_are_required(self):
        base, source, old = self.baseline()
        with self.assertRaises(RuntimeError):
            validate_sources(base, old, old)
        mismatched = copy.deepcopy(old)
        mismatched['git_sha'] = 'c' * 40
        with self.assertRaises(RuntimeError):
            validate_sources(base, source, mismatched)
