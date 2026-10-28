"""Linux root-only storage initialization regression probe using temporary files."""
import os
from pathlib import Path
import tempfile
import unittest

from prepare_storage import KEYS, prepare


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0, 'requires Linux root')
class StorageProbe(unittest.TestCase):
    def test_repeated_preparation_preserves_files_and_secrets(self):
        with tempfile.TemporaryDirectory(prefix='ccheck-storage-probe-') as temporary:
            root = Path(temporary)
            prepare(root)
            before = {key: (root / 'secrets' / key).read_bytes() for key in KEYS}
            self.assertEqual(len(set(before.values())), 3)
            database = root / 'data/assistant/storage/anythingllm.db'
            database.write_bytes(b'existing database sentinel')
            prepare(root)
            self.assertEqual(database.read_bytes(), b'existing database sentinel')
            for key, value in before.items():
                path = root / 'secrets' / key
                self.assertEqual(path.read_bytes(), value)
                self.assertEqual(path.stat().st_uid, 10001)
                self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix='ccheck-storage-probe-') as temporary:
            root = Path(temporary)
            target = root / 'unrelated'
            target.mkdir()
            (root / 'data').symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                prepare(root)
            self.assertEqual(list(target.iterdir()), [])

    def test_invalid_existing_secret_is_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix='ccheck-storage-probe-') as temporary:
            root = Path(temporary)
            prepare(root)
            key = root / 'secrets' / KEYS[0]
            key.write_bytes(b'bad')
            with self.assertRaises(ValueError):
                prepare(root)
            self.assertEqual(key.read_bytes(), b'bad')

    def test_root_container_identity_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='ccheck-storage-probe-') as temporary:
            with self.assertRaises(ValueError):
                prepare(Path(temporary), uid=0)


if __name__ == '__main__':
    unittest.main()
