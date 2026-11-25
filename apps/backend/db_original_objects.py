"""Explicit, immutable local originals for the isolated SQL adapter.

The root must already exist and be private to this adapter. A failed/ambiguous
database commit leaves an unreferenced object for operator reconciliation; never
delete an upload in an exception handler. Only committed GC entries may delete.
No production configuration, ambient directories or rendering imports.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

MAX_ORIGINAL_BYTES = 30 * 1024 * 1024
_KEY = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:pdf|docx)')
STORE_MARKER = '.original-store-id'


def _plain(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError('Original storage links are forbidden')


class OriginalObjects:
    @classmethod
    def initialize(cls, root, *, storage_id):
        """One-time explicit initialization; refuse populated/existing stores."""
        root = Path(root).absolute()
        for path in (root, *root.parents):
            _plain(path)
        identity = UUID(str(storage_id))
        if not identity.int or not root.is_dir() or any(root.iterdir()):
            raise ValueError('Initialize only an existing empty private originals directory')
        fd = os.open(root / STORE_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write((str(identity) + '\n').encode('ascii'))
            handle.flush()
            os.fsync(handle.fileno())
        objects = cls(root, storage_id=identity)
        objects._sync_root()
        return objects

    def __init__(self, root, *, storage_id):
        self.storage_id = UUID(str(storage_id))
        if not self.storage_id.int:
            raise ValueError('A persistent nonzero originals storage UUID is required')
        root = Path(root).absolute()
        for path in (root, *root.parents):
            _plain(path)
        if not root.is_dir():
            raise ValueError('An existing private originals directory is required')
        self.root = root.resolve(strict=True)
        self._identity = self.root.stat().st_dev, self.root.stat().st_ino
        self._verify_marker()

    def _verify_marker(self):
        marker = self.root / STORE_MARKER
        _plain(marker)
        with marker.open('rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    handle.read(80) != (str(self.storage_id) + '\n').encode('ascii')):
                raise ValueError('Original store identity mismatch')

    def _path(self, key):
        if type(key) is not str or not _KEY.fullmatch(key):
            raise ValueError('Invalid original object key')
        for parent in (self.root, *self.root.parents):
            _plain(parent)
        info = self.root.stat()
        if (info.st_dev, info.st_ino) != self._identity:
            raise ValueError('Original root was replaced')
        self._verify_marker()
        path = self.root / key
        _plain(path)
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Original must be a private regular file')
        return path

    def _sync_root(self):
        # Windows does not offer directory fsync through Python. Production
        # Linux durability still requires a persistent volume and backup tests.
        if os.name != 'nt':
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def put(self, data, *, ext, filename):
        if type(data) is not bytes or not 0 < len(data) <= MAX_ORIGINAL_BYTES or ext not in {'pdf', 'docx'}:
            raise ValueError('Invalid original')
        key = str(uuid4()) + '.' + ext
        path = self._path(key)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self._sync_root()
        name = re.sub(r'[\x00-\x1f<>:"/|?*]', '_', filename.replace('\\', '/').split('/')[-1]).strip('._ ')
        meta = dict(name=(name or 'resume.' + ext)[:120], ext=ext, bytes=len(data), storage_id=str(self.storage_id),
                    sha256=hashlib.sha256(data).hexdigest(), stored_at=datetime.now(timezone.utc).isoformat())
        self.read_verified(key, meta)
        return key, meta

    def read_verified(self, key, meta):
        path = self._path(key)
        with path.open('rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ORIGINAL_BYTES:
                raise ValueError('Invalid original file')
            data = handle.read(MAX_ORIGINAL_BYTES + 1)
        if (type(meta) is not dict or type(meta.get('bytes')) is not int or
                meta.get('storage_id') not in (None, str(self.storage_id)) or
                meta['bytes'] != len(data) or meta.get('ext') != key.rsplit('.', 1)[-1] or
                meta.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise ValueError('Original integrity mismatch')
        return data

    def remove(self, key):
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # Crash after unlink, before DB commit: replay completes it.
        self._sync_root()
