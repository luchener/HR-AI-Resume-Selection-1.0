"""Offline recovery gates with real GnuPG and synthetic data only."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import db_recovery_bundle as bundle


def gpg_binary():
    candidate = shutil.which('gpg')
    if candidate:
        return Path(candidate)
    candidate = Path(r'C:\Program Files\Git\usr\bin\gpg.exe')
    return candidate if candidate.is_file() else None


def fixture(root):
    contents = {
        'database/database.dump': b'PGDMP-synthetic-fixture',
        'source/users/synthetic.json': b'{"synthetic":true}',
        'originals/resume_originals/synthetic.pdf': b'%PDF-synthetic-only',
        'roles/database-init.sh': b'# synthetic role recreation',
        'runtime/recovery-start.json': b'{"mode":"synthetic"}',
        'secrets/application.json': b'{"synthetic_secret":"PRIVATE_TEST_VALUE"}',
    }
    for name, value in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return contents


class RecoveryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.contents = fixture(self.source)

    def descriptor(self):
        return {'format': 1, 'backup_id': str(uuid4()), 'created_at': bundle.utc_now(),
                'recovery_point_utc': '2026-10-16T00:00:00+00:00',
                'files': bundle.inventory(self.source)}

    @contextmanager
    def archive(self, *, descriptor=None, extra=None, omit=None, content_override=None):
        data = io.BytesIO()
        descriptor = descriptor or self.descriptor()
        with tarfile.open(fileobj=data, mode='w') as tar:
            raw = json.dumps(descriptor).encode()
            info = tarfile.TarInfo(bundle.MANIFEST); info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
            for name, value in self.contents.items():
                if name == omit:
                    continue
                value = content_override.get(name, value) if content_override else value
                info = tarfile.TarInfo(name); info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
            if extra is not None:
                tar.addfile(extra, io.BytesIO(b'x' * extra.size) if extra.isfile() else None)
        data.seek(0)
        with tarfile.open(fileobj=data, mode='r:') as tar:
            yield tar

    def test_all_components_are_required(self):
        (self.source / 'database/database.dump').unlink()
        with self.assertRaises(bundle.RecoveryBlocked): bundle.inventory(self.source)

    def test_valid_archive_checks_every_file(self):
        with self.archive() as tar:
            result = bundle.check_archive(tar)
        self.assertEqual(len(result['files']), 6)

    def test_path_traversal_absolute_and_windows_aliases_rejected(self):
        for name in ('../escape', '/etc/passwd', 'a/../../b', 'C:/escape', 'a\\b',
                     'a//b', './a', 'a/NUL.txt', 'a/com1', 'a/b.', 'a/b ', 'a/b\x00'):
            with self.subTest(name=name), self.assertRaises(bundle.RecoveryBlocked): bundle.safe_name(name)

    def test_link_members_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            info = tarfile.TarInfo('source/extra'); info.type = kind; info.linkname = '../../outside'
            with self.subTest(kind=kind), self.archive(extra=info) as tar, self.assertRaises(bundle.RecoveryBlocked):
                bundle.check_archive(tar)

    def test_duplicate_archive_member_rejected(self):
        info = tarfile.TarInfo('database/database.dump'); info.size = len(self.contents[info.name])
        with self.archive(extra=info) as tar, self.assertRaises(bundle.RecoveryBlocked): bundle.check_archive(tar)

    def test_missing_original_rejected(self):
        with self.archive(omit='originals/resume_originals/synthetic.pdf') as tar, self.assertRaises(bundle.RecoveryBlocked):
            bundle.check_archive(tar)

    def test_same_size_corruption_rejected(self):
        name = 'database/database.dump'
        with self.archive(content_override={name: b'x' * len(self.contents[name])}) as tar, self.assertRaises(bundle.RecoveryBlocked):
            bundle.check_archive(tar)

    def test_manifest_count_and_size_limits(self):
        with patch.object(bundle, 'MAX_FILES', 2), self.assertRaises(bundle.RecoveryBlocked): bundle.inventory(self.source)
        with patch.object(bundle, 'MAX_BYTES', 2), self.assertRaises(bundle.RecoveryBlocked): bundle.inventory(self.source)
        with self.archive() as tar, patch.object(bundle, 'MAX_BYTES', 2), self.assertRaises(bundle.RecoveryBlocked):
            bundle.check_archive(tar)

    def test_case_collisions_and_parent_file_collisions_rejected(self):
        for name in ('source/USERS/synthetic.json', 'source/users'):
            descriptor = self.descriptor()
            descriptor['files'][name] = {'bytes': 1, 'sha256': '0' * 64}
            with self.subTest(name=name), self.archive(descriptor=descriptor) as tar, self.assertRaises(bundle.RecoveryBlocked):
                bundle.check_archive(tar)

    def test_source_symlink_rejected(self):
        target = self.source / 'source/link'
        try: target.symlink_to(self.source / 'database/database.dump')
        except OSError: self.skipTest('Host does not permit synthetic symlinks')
        with self.assertRaises(bundle.RecoveryBlocked): bundle.inventory(self.source)

    def test_hardlinked_source_rejected(self):
        os.link(self.source / 'database/database.dump', self.source / 'database/duplicate.dump')
        with self.assertRaises(bundle.RecoveryBlocked): bundle.inventory(self.source)

    def test_timezone_required(self):
        with self.assertRaises(bundle.RecoveryBlocked): bundle.recovery_point('2026-10-16T00:00:00')
        self.assertEqual(bundle.recovery_point('2026-10-16T08:00:00+08:00'), '2026-10-16T00:00:00+00:00')

    def test_error_output_suppressed(self):
        import subprocess
        result = subprocess.CompletedProcess([], 2, stdout=b'PRIVATE', stderr=b'SECRET')
        with patch('subprocess.run', return_value=result), self.assertRaises(bundle.RecoveryBlocked) as failure:
            bundle.captured(['synthetic'])
        self.assertNotIn('PRIVATE', str(failure.exception)); self.assertNotIn('SECRET', str(failure.exception))

    def test_raw_tar_preflight_accepts_valid_archive(self):
        path = self.root / 'archive.tar'
        with self.archive() as tar: path.write_bytes(tar.fileobj.getvalue())
        bundle.preflight_tar(path)

    def test_raw_tar_preflight_rejects_truncation_and_trailing_data(self):
        with self.archive() as tar: raw = tar.fileobj.getvalue()
        for data in (raw[:1024], raw + b'untracked trailing bytes'):
            path = self.root / 'archive.tar'; path.write_bytes(data)
            with self.assertRaises(bundle.RecoveryBlocked): bundle.preflight_tar(path)

    def test_raw_tar_preflight_bounds_pax_headers(self):
        info = tarfile.TarInfo('extended'); info.type = tarfile.XHDTYPE; info.size = 4097
        path = self.root / 'archive.tar'; path.write_bytes(info.tobuf() + bytes(512 * 12))
        with self.assertRaises(bundle.RecoveryBlocked): bundle.preflight_tar(path)

    def test_raw_tar_preflight_rejects_links_before_tarfile_parsing(self):
        info = tarfile.TarInfo('source/extra'); info.type = tarfile.SYMTYPE; info.linkname = '../../outside'
        path = self.root / 'archive.tar'
        with self.archive(extra=info) as tar: path.write_bytes(tar.fileobj.getvalue())
        with self.assertRaises(bundle.RecoveryBlocked): bundle.preflight_tar(path)

    def test_windows_reparse_point_is_rejected_even_without_is_junction(self):
        from types import SimpleNamespace
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=0x400)):
            with self.assertRaises(bundle.RecoveryBlocked): bundle.no_links(self.source)

    def test_filesystem_parent_traversal_cannot_bypass_separation(self):
        with self.assertRaises(bundle.RecoveryBlocked): bundle.no_links(self.root / 'outside/../source')

    def test_cli_rejects_staging_inside_repository(self):
        import runpy
        cli = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/database-encrypted-recovery.py'))
        with self.assertRaises(ValueError):cli['external'](Path(__file__).parent / '.test-tmp/recovery')


@unittest.skipUnless(gpg_binary(), 'GnuPG required for actual OpenPGP tests')
class RecoveryEncryptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.keys = cls.root / 'keys'; cls.keys.mkdir(mode=0o700)
        cls.scratch = cls.root / 'scratch'; cls.scratch.mkdir(mode=0o700)
        cls.gpg = gpg_binary()
        cls.password = b'Synthetic-Recovery-Key-Passphrase9'
        with bundle.keyring(cls.gpg, cls.scratch) as base:
            bundle.captured(base + ['--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                '--quick-generate-key', 'Synthetic Recovery <recovery@example.test>', 'rsa2048', 'encr', '1d'],
                payload=cls.password + b'\n')
            output = bundle.captured(base + ['--with-colons', '--list-keys']).decode()
            cls.recipient = next(line.split(':')[9] for line in output.splitlines() if line.startswith('fpr:'))
            cls.public = cls.keys / 'public.asc'
            cls.private = cls.keys / 'private.asc'
            cls.public.write_bytes(bundle.captured(base + ['--armor', '--export', cls.recipient]))
            cls.private.write_bytes(bundle.captured(base + ['--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                '--armor', '--export-secret-keys', cls.recipient], payload=cls.password + b'\n'))
            cls.private.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix='case-', dir=self.root))
        self.source = self.work / 'input'; self.source.mkdir()
        self.contents = fixture(self.source)
        self.backups = self.work / 'backups'; self.backups.mkdir()
        self.encrypted = self.backups / 'fixture.tar.gpg'
        self.destination = self.work / 'restored'

    def pack(self, **overrides):
        kwargs = dict(gpg=self.gpg, public_key=self.public, recipient=self.recipient,
                      scratch=self.scratch, point='2026-10-16T00:00:00Z')
        kwargs.update(overrides)
        return bundle.pack_bundle(self.source, self.encrypted, **kwargs)

    def restore(self, checksum, **overrides):
        kwargs = dict(expected_sha256=checksum, gpg=self.gpg, private_key=self.private,
                      recipient=self.recipient, passphrase=self.password, scratch=self.scratch)
        kwargs.update(overrides)
        return bundle.restore_bundle(self.encrypted, self.destination, **kwargs)

    def test_actual_openpgp_roundtrip_and_post_restore_corruption(self):
        packed = self.pack()
        self.assertNotIn(b'PRIVATE_TEST_VALUE', self.encrypted.read_bytes())
        result = self.restore(packed['ciphertext_sha256'])
        self.assertEqual(result['files'], len(self.contents))
        self.assertFalse(result['production_acceptance'])
        for name, content in self.contents.items(): self.assertEqual((self.destination / name).read_bytes(), content)
        bundle.verify_restored(self.destination, packed['ciphertext_sha256'], packed['manifest_sha256'])
        (self.destination / 'originals/resume_originals/synthetic.pdf').write_bytes(b'corrupt')
        with self.assertRaises(bundle.RecoveryBlocked):
            bundle.verify_restored(self.destination, packed['ciphertext_sha256'], packed['manifest_sha256'])

    def test_ciphertext_checksum_rejected_before_decrypt(self):
        self.pack()
        with patch.object(bundle, 'captured', side_effect=AssertionError('Unexpected subprocess')):
            with self.assertRaises(bundle.RecoveryBlocked): self.restore('0' * 64)
        self.assertFalse(self.destination.exists())

    def test_tampered_ciphertext_mdc_rejected_even_if_checksum_replaced(self):
        self.pack()
        data = bytearray(self.encrypted.read_bytes()); data[-10] ^= 1; self.encrypted.write_bytes(data)
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(bundle.digest_file(self.encrypted))
        self.assertFalse(self.destination.exists())

    def test_wrong_passphrase_rejected_without_extracting_plaintext(self):
        packed = self.pack()
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(packed['ciphertext_sha256'], passphrase=b'Wrong-synthetic-value')
        self.assertFalse(self.destination.exists())

    def test_wrong_recipient_rejected(self):
        with self.assertRaises(bundle.RecoveryBlocked): self.pack(recipient='0' * 40)
        self.assertFalse(self.encrypted.exists())

    def test_secret_key_export_cannot_be_used_on_backup_machine(self):
        with self.assertRaises(bundle.RecoveryBlocked): self.pack(public_key=self.private)
        self.assertFalse(self.encrypted.exists())

    def test_existing_bundle_and_existing_restore_preserved(self):
        packed = self.pack(); original = self.encrypted.read_bytes()
        with self.assertRaises(bundle.RecoveryBlocked): self.pack()
        self.assertEqual(self.encrypted.read_bytes(), original)
        self.destination.mkdir(); marker = self.destination / 'preserve'; marker.write_bytes(b'keep')
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(packed['ciphertext_sha256'])
        self.assertEqual(marker.read_bytes(), b'keep')

    def test_failed_extraction_leaves_uncertified_destination(self):
        packed = self.pack()
        original = bundle.check_archive
        def interrupted(tar, expected=None, destination=None):
            if destination is not None: raise OSError('Synthetic interrupted disk write')
            return original(tar, expected, destination)
        with patch.object(bundle, 'check_archive', side_effect=interrupted), self.assertRaises(OSError):
            self.restore(packed['ciphertext_sha256'])
        self.assertTrue((self.destination / bundle.INCOMPLETE).exists())
        with self.assertRaises(bundle.RecoveryBlocked):
            bundle.verify_restored(self.destination, packed['ciphertext_sha256'], packed['manifest_sha256'])
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(packed['ciphertext_sha256'])

    def test_source_change_during_copy_prevents_publication(self):
        add = tarfile.TarFile.addfile
        def changed(tar, info, fileobj=None):
            result = add(tar, info, fileobj)
            if info.name == 'database/database.dump':
                (self.source / info.name).write_bytes(b'Synthetic change')
            return result
        with patch.object(tarfile.TarFile, 'addfile', changed), self.assertRaises(bundle.RecoveryBlocked): self.pack()
        self.assertFalse(self.encrypted.exists())

    def test_private_key_must_be_separate_from_backup_directory(self):
        packed = self.pack(); private = self.backups / 'private.asc'; private.write_bytes(self.private.read_bytes())
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(packed['ciphertext_sha256'], private_key=private)

    def test_forged_restore_certificate_rejected_by_independent_manifest_digest(self):
        packed = self.pack(); self.restore(packed['ciphertext_sha256'])
        original = self.destination / 'database/database.dump'; original.write_bytes(b'forged')
        certificate_file = self.destination / bundle.COMPLETE
        certificate = json.loads(certificate_file.read_bytes())
        certificate['manifest']['files']['database/database.dump'] = {
            'bytes': original.stat().st_size, 'sha256': bundle.digest_file(original)}
        certificate_file.write_text(json.dumps(certificate), encoding='utf-8')
        with self.assertRaises(bundle.RecoveryBlocked):
            bundle.verify_restored(self.destination, packed['ciphertext_sha256'], packed['manifest_sha256'])

    def test_unicode_paths_restore_through_bounded_pax_headers(self):
        path = self.source / 'source/候选人.json'; path.write_text('{"synthetic":true}', encoding='utf-8')
        packed = self.pack(); self.restore(packed['ciphertext_sha256'])
        self.assertEqual((self.destination / 'source/候选人.json').read_bytes(), path.read_bytes())

    def test_truncated_ciphertext_rejected_before_destination_creation(self):
        self.pack(); self.encrypted.write_bytes(self.encrypted.read_bytes()[:-128])
        with self.assertRaises(bundle.RecoveryBlocked): self.restore(bundle.digest_file(self.encrypted))
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
