import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'cli')]
from build_release import reusable_wheel_provenance, runtime_recipe


class RuntimeProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.row = {'file': 'example.whl', 'distribution': 'example', 'version': '1.0',
                    'sha256': 'a' * 64}
        self.saved = dict(self.row, url='https://files.pythonhosted.org/example.whl')
        self.path = self.root / 'runtime/qwen/wheels.json'
        self.path.parent.mkdir(parents=True)
        self.write([self.saved])

    def write(self, records):
        self.path.write_text(json.dumps(records))
        self.previous = {'_root': str(self.root), 'runtime_recipe': {'directory': 'runtime/qwen'},
            'files': {'runtime/qwen/wheels.json': hashlib.sha256(self.path.read_bytes()).hexdigest()}}

    def test_exact_locked_catalog_reuses_verified_urls(self):
        self.assertEqual(reusable_wheel_provenance(self.previous, [self.row]), [self.saved])

    def test_dependency_hash_version_and_inventory_changes_require_fresh_resolution(self):
        for key in ('sha256', 'version', 'distribution', 'file'):
            with self.subTest(key=key):
                self.assertIsNone(reusable_wheel_provenance(self.previous, [dict(self.row, **{key: 'changed'})]))
        self.assertIsNone(reusable_wheel_provenance(self.previous, []))

    def test_tampered_provenance_is_not_used_or_silently_ignored(self):
        self.path.write_text('[]')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            reusable_wheel_provenance(self.previous, [self.row])

    def test_duplicate_and_insecure_urls_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            reusable_wheel_provenance(self.previous, [self.row, self.row])
        self.write([self.saved, self.saved])
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            reusable_wheel_provenance(self.previous, [self.row])
        for url in ('http://example.com/a', 'https://user:pass@example.com/a',
                    'https://example.com/a?token=private', 'https://example.com/a#fragment'):
            with self.subTest(url=url):
                self.write([dict(self.saved, url=url)])
                with self.assertRaisesRegex(RuntimeError, 'URL'):
                    reusable_wheel_provenance(self.previous, [self.row])

    def test_rebuilds_fresh_entrypoint_without_querying_pypi_for_unchanged_wheels(self):
        context, output = self.root / 'source', self.root / 'output'
        (context / 'locks').mkdir(parents=True)
        (context / 'deploy/release/runtime').mkdir(parents=True)
        output.mkdir()
        (context / 'locks/qwen.lock').write_text('pinned requirements')
        (context / 'locks/qwen.wheels.json').write_text(json.dumps([self.row]))
        (context / 'deploy/release/runtime/qwen_entry.py').write_text('updated entrypoint')
        with patch('build_release.urlopen', side_effect=AssertionError('network forbidden')), \
                patch('build_release.subprocess.check_output', return_value='libc6=2.36\n'), \
                patch('build_release.pinned', return_value='python@sha256:' + 'b' * 64):
            runtime_recipe(context, output, 'verified-image', previous=self.previous)
        self.assertEqual((output / 'runtime/qwen/qwen_entry.py').read_text(), 'updated entrypoint')
        self.assertEqual(json.loads((output / 'runtime/qwen/wheels.json').read_text()), [self.saved])
