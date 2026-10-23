from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError
from ccheck.fetch import asset_prefix, cached_asset


class PatchFetchTests(unittest.TestCase):
    def test_cache_only_reuses_explicit_matching_image_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            name = 'images/frontend.tar.gz.part0000'
            manifest = {'files': {name: 'a'*64}, 'artifact_sizes': {name: 7},
                        'distribution': {'reused_files': [name]}}
            previous = {'_root': temporary, 'files': {name: 'a'*64}, 'artifact_sizes': {name: 7}}
            self.assertEqual(cached_asset(manifest, name, previous), Path(temporary)/name)
            self.assertIsNone(cached_asset(manifest, name, None))
            previous['files'][name] = 'b'*64
            self.assertIsNone(cached_asset(manifest, name, previous))
            previous['files'][name] = 'a'*64
            previous['artifact_sizes'][name] = 8
            self.assertIsNone(cached_asset(manifest, name, previous))
            self.assertIsNone(cached_asset({'distribution': {}}, 'other', previous))

    def test_unchanged_images_are_retrieved_from_signed_base_version(self):
        manifest = {'version': 'v0.1.0-rc.15', 'distribution': {'kind': 'deployment-patch',
                    'base_version': 'v0.1.0-rc.14', 'reused_files': ['images/qwen.tar.gz.part0000']}}
        self.assertEqual(asset_prefix(manifest, 'images/qwen.tar.gz.part0000', 'https://example.com/download'),
                         'https://example.com/download/release-v0.1.0-rc.14/')
        self.assertEqual(asset_prefix(manifest, 'cli/ccheck.pyz', 'https://example.com/download'),
                         'https://example.com/download/release-v0.1.0-rc.15/')

    def test_unsafe_base_version_or_non_image_reuse_is_rejected(self):
        for version, name, kind in [('v0.1.0-rc.15', 'images/x', 'deployment-patch'),
                                    ('../v14', 'images/x', 'deployment-patch'),
                                    ('v0.1.0-rc.14', 'cli/ccheck.pyz', 'deployment-patch'),
                                    ('v0.1.0-rc.14', 'images/x', 'unknown')]:
            manifest = {'version': 'v0.1.0-rc.15', 'distribution': {'kind': kind,
                        'base_version': version, 'reused_files': [name]}}
            with self.assertRaises(DeploymentError):
                asset_prefix(manifest, name, 'https://example.com/download')

    def test_old_full_release_fetch_keeps_original_location(self):
        self.assertEqual(asset_prefix({'version': 'v0.1.0-rc.14'}, 'images/x', 'https://example.com'),
                         'https://example.com/release-v0.1.0-rc.14/')
