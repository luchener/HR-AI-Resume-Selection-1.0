"""Local immutable object durability/integrity and path boundaries."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from db_original_objects import OriginalObjects, STORE_MARKER


class OriginalObjectTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.objects = OriginalObjects.initialize(self.root, storage_id=uuid4())

    def files(self):
        return [p for p in self.root.iterdir() if p.name != STORE_MARKER]

    def test_unique_immutable_objects_hash_size_and_safe_display_name(self):
        keys = [self.objects.put(b'%PDF synthetic', ext='pdf', filename='../private/hello\r\n.pdf')[0]
                for _ in range(2)]
        self.assertNotEqual(*keys)
        key, meta = self.objects.put(b'%PDF synthetic', ext='pdf', filename='C:\\private\\hello.pdf')
        self.assertEqual(meta['name'], 'hello.pdf')
        self.assertEqual(self.objects.read_verified(key, meta), b'%PDF synthetic')
        for change in ({'bytes': 1}, {'sha256': '0' * 64}, {'ext': 'docx'}, {'bytes': True}, {'storage_id': str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.objects.read_verified(key, {**meta, **change})
        (self.root / key).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.objects.read_verified(key, meta)

    def test_path_traversal_and_unsupported_keys_never_read_or_remove(self):
        for key in ('../secret', '/secret.pdf', 'C:\\secret.pdf', str(uuid4()) + '.exe', '', str(uuid4()) + '.pdf/..'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.objects.remove(key)

    def test_failed_write_keeps_uncertified_object_and_does_not_overwrite(self):
        with patch('db_original_objects.os.fsync', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.objects.put(b'%PDF test', ext='pdf', filename='test.pdf')
        self.assertEqual(len(self.files()), 1)
        self.objects.put(b'%PDF other', ext='pdf', filename='test.pdf')
        self.assertEqual(len(self.files()), 2)

    def test_remove_is_replayable_and_never_removes_other_objects(self):
        key, _ = self.objects.put(b'%PDF test', ext='pdf', filename='test.pdf')
        other, _ = self.objects.put(b'%PDF other', ext='pdf', filename='test.pdf')
        self.objects.remove(key)
        self.objects.remove(key)
        self.assertTrue((self.root / other).is_file())

    def test_hardlink_is_refused(self):
        key, meta = self.objects.put(b'%PDF test', ext='pdf', filename='test.pdf')
        (self.root / 'outside-link').hardlink_to(self.root / key)
        with self.assertRaises(ValueError):
            self.objects.read_verified(key, meta)
        with self.assertRaises(ValueError):
            self.objects.remove(key)

    def test_replaced_root_is_refused(self):
        key = str(uuid4()) + '.pdf'
        moved = self.root.parent / ('original-moved-' + uuid4().hex)
        self.root.rename(moved)
        self.root.mkdir()
        try:
            with self.assertRaises(ValueError):
                self.objects.remove(key)
        finally:
            self.root.rmdir()
            moved.rename(self.root)

    def test_marker_survives_restart_and_wrong_store_or_missing_marker_is_refused(self):
        OriginalObjects(self.root, storage_id=self.objects.storage_id)
        with self.assertRaises(ValueError):
            OriginalObjects(self.root, storage_id=uuid4())
        with self.assertRaises(ValueError):
            OriginalObjects.initialize(self.root, storage_id=self.objects.storage_id)
        (self.root / STORE_MARKER).unlink()
        with self.assertRaises(FileNotFoundError):
            self.objects.remove(str(uuid4()) + '.pdf')
