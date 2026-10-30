import copy
from pathlib import Path
import sys
import os
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_assistant_ui_patch import BASE_SHA, validate_sources, public_dist_permissions


class AssistantUiPatchTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX static-file permissions')
    def test_only_public_dist_is_readable_by_nonroot_nginx(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / 'dist'
            (dist / 'assets').mkdir(parents=True, mode=0o700)
            private = root / 'private'
            private.write_text('not public')
            private.chmod(0o600)
            for name in ('index.html', 'assets/main.js'):
                path = dist / name
                path.write_text('public output')
                path.chmod(0o600)
            public_dist_permissions(dist)
            self.assertEqual((dist / 'assets').stat().st_mode & 0o777, 0o755)
            self.assertEqual((dist / 'index.html').stat().st_mode & 0o777, 0o644)
            self.assertEqual((dist / 'assets/main.js').stat().st_mode & 0o777, 0o644)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

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
