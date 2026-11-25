"""Offline OpenPGP recovery bundles. No network, database writes or cutover.

GnuPG provides encryption/integrity; this module does not implement cryptography.
The ciphertext SHA-256 must be retained separately in a trusted inventory: public
key encryption alone does not authenticate the creator. Only authenticated,
bounded, manifest-checked regular files are eligible for recovery.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
from time import monotonic
from uuid import UUID, uuid4

MANIFEST = '.recovery-manifest.json'
INCOMPLETE = '.recovery-incomplete'
COMPLETE = '.recovery-complete.json'
COMPONENTS = frozenset({'database', 'source', 'originals', 'roles', 'runtime', 'secrets'})
REQUIRED = frozenset({'database/database.dump', 'roles/database-init.sh',
                      'runtime/recovery-start.json', 'secrets/application.json'})
MAX_FILES = 100_000
MAX_BYTES = 20 * 1024**3
MAX_MANIFEST = 32 * 1024**2
CHUNK = 1024**2


class RecoveryBlocked(ValueError):
    """Fixed safe diagnostics; never include paths, credentials or tool output."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def recovery_point(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise RecoveryBlocked('Recovery point must be a timezone-aware timestamp') from None


def safe_name(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RecoveryBlocked('Invalid archive path')
    parts = value.split('/')
    reserved = {'con', 'prn', 'aux', 'nul'} | {f'{p}{i}' for p in ('com', 'lpt') for i in range(1, 10)}
    if (PurePosixPath(value).is_absolute() or '\\' in value or ':' in value
            or any(ord(c) < 32 or c in '<>"|?*' for c in value)
            or any(p in {'', '.', '..'} or p.endswith((' ', '.'))
                   or p.split('.')[0].casefold() in reserved for p in parts)):
        raise RecoveryBlocked('Invalid archive path')
    return value


def no_links(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise RecoveryBlocked('Filesystem paths must not contain parent traversal')
    for part in (path, *path.parents):
        try:
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
        except FileNotFoundError:
            attributes = 0
        if (attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                or part.is_symlink() or getattr(part, 'is_junction', lambda: False)()):
            raise RecoveryBlocked('Symlinked paths are forbidden')
    return path


def fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})', value):
        raise RecoveryBlocked('Full recipient fingerprint is required')
    return value.upper()


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def descriptor_digest(descriptor):
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')


def stable_signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RecoveryBlocked('Source must contain unlinked regular files only')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def preflight_tar(path):
    """Bound raw extension headers before tarfile allocates their payloads."""
    size = Path(path).stat().st_size
    total, count, extended = 0, 0, False
    with Path(path).open('rb') as handle:
        while True:
            header = handle.read(512)
            if len(header) != 512:
                raise RecoveryBlocked('Truncated recovery archive')
            if header == bytes(512):
                if handle.read(512) != bytes(512):
                    raise RecoveryBlocked('Invalid recovery archive terminator')
                while block := handle.read(CHUNK):
                    if any(block):
                        raise RecoveryBlocked('Trailing archive data is forbidden')
                if extended:
                    raise RecoveryBlocked('Incomplete extended archive header')
                return
            try:
                info = tarfile.TarInfo.frombuf(header, 'utf-8', 'surrogateescape')
            except (tarfile.TarError, ValueError, UnicodeError):
                raise RecoveryBlocked('Invalid recovery archive header') from None
            if info.size < 0:
                raise RecoveryBlocked('Invalid recovery archive size')
            if info.type == tarfile.XHDTYPE:
                if extended or info.size > 4096:
                    raise RecoveryBlocked('Oversized or repeated extended header')
                extended = True
            elif info.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                extended = False
                count += 1; total += info.size
                if count > MAX_FILES + 1 or total > MAX_BYTES + MAX_MANIFEST:
                    raise RecoveryBlocked('Archive exceeds recovery limits')
            else:
                raise RecoveryBlocked('Non-regular recovery archive entry')
            padded = ((info.size + 511) // 512) * 512
            if handle.tell() + padded > size:
                raise RecoveryBlocked('Truncated recovery archive member')
            handle.seek(padded, os.SEEK_CUR)


def inventory(root):
    root = no_links(root)
    if not root.is_dir():
        raise RecoveryBlocked('Bundle source must be a directory')
    result, folded, total = {}, set(), 0
    for path in sorted(root.rglob('*')):
        no_links(path)
        if path.is_dir():
            continue
        name = safe_name(path.relative_to(root).as_posix())
        if name.split('/')[0] not in COMPONENTS or name.casefold() in folded:
            raise RecoveryBlocked('Unexpected or conflicting bundle component')
        before = stable_signature(path)
        total += before[2]
        if len(result) >= MAX_FILES or total > MAX_BYTES:
            raise RecoveryBlocked('Bundle exceeds recovery limits')
        digest = digest_file(path)
        if before != stable_signature(path):
            raise RecoveryBlocked('Bundle source changed during inventory')
        folded.add(name.casefold())
        result[name] = {'bytes': before[2], 'sha256': digest}
    if not REQUIRED <= result.keys() or any(not any(n.startswith(c + '/') for n in result) for c in COMPONENTS):
        raise RecoveryBlocked('A required recovery component is missing')
    return result


def captured(args, *, payload=None, output=None, timeout=180):
    try:
        result = subprocess.run(args, input=payload, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RecoveryBlocked('Recovery subprocess unavailable or timed out') from None
    if result.returncode:
        raise RecoveryBlocked('Recovery subprocess rejected the operation')
    return result.stdout


def gpg_path(binary, path):
    """Git's MSYS GnuPG needs /c/... paths; native GnuPG uses native paths."""
    path = Path(path).absolute()
    if os.name == 'nt' and (Path(binary).parent / 'msys-2.0.dll').is_file():
        value = path.as_posix()
        if not re.match(r'^[A-Za-z]:/', value):
            raise RecoveryBlocked('MSYS GnuPG requires a local drive path')
        return '/' + value[0].lower() + value[2:]
    return str(path)


@contextmanager
def keyring(gpg, scratch):
    binary = Path(gpg).absolute()
    conf = binary.with_name('gpgconf' + binary.suffix)
    if not binary.is_file() or not conf.is_file():
        raise RecoveryBlocked('GnuPG and gpgconf executables are required')
    # GnuPG agent sockets also have short Unix-path limits under Git/MSYS.
    with tempfile.TemporaryDirectory(prefix='rk-', dir=scratch) as folder:
        os.chmod(folder, 0o700)
        home = gpg_path(binary, folder)
        base = [str(binary), '--no-options', '--homedir', home, '--batch', '--no-tty']
        try:
            yield base
        finally:
            # Agent caches belong only to this ephemeral keyring, never user's.
            captured([str(conf), '--homedir', home, '--kill', 'gpg-agent'], timeout=30)


def import_key(base, path, expected, *, secret):
    path = no_links(path)
    if not path.is_file() or path.stat().st_size > 4 * CHUNK:
        raise RecoveryBlocked('Invalid key export')
    if secret and os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RecoveryBlocked('Private key export permissions must be restricted')
    captured(base + ['--import'], payload=path.read_bytes())
    output = captured(base + ['--with-colons', '--list-secret-keys' if secret else '--list-keys'])
    primary, following = [], False
    for line in output.decode('utf-8').splitlines():
        fields = line.split(':')
        if fields[0] in {'pub', 'sec'}:
            following = True
        elif following and fields[0] == 'fpr':
            primary.append(fields[9]); following = False
    if primary != [fingerprint(expected)]:
        raise RecoveryBlocked('Imported key differs from the expected recipient')
    if not secret and captured(base + ['--with-colons', '--list-secret-keys']).strip():
        raise RecoveryBlocked('Backup must use a public key export only')


def scratch_directory(path):
    path = no_links(path)
    if not path.is_dir():
        raise RecoveryBlocked('Private scratch directory is required')
    if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RecoveryBlocked('Scratch directory must be private')
    return path


def separate(a, b):
    return a != b and a not in b.parents and b not in a.parents


def pack_bundle(root, destination, *, gpg, public_key, recipient, scratch, point):
    started = monotonic()
    root, destination = no_links(root), no_links(destination)
    scratch = scratch_directory(scratch)
    if not separate(root, destination) or not separate(root, scratch) or destination.exists():
        raise RecoveryBlocked('Source, scratch and new output must be separate')
    if not destination.parent.is_dir():
        raise RecoveryBlocked('Output parent must exist')
    files = inventory(root)
    descriptor = {'format': 1, 'backup_id': str(uuid4()), 'created_at': utc_now(),
                  'recovery_point_utc': recovery_point(point), 'files': files}
    encoded = json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > MAX_MANIFEST:
        raise RecoveryBlocked('Manifest exceeds recovery limits')
    with tempfile.TemporaryDirectory(prefix='recovery-pack-', dir=scratch) as folder:
        os.chmod(folder, 0o700)
        archive = Path(folder) / 'bundle.tar'
        with private_file(archive) as output, tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as tar:
            meta = tarfile.TarInfo(MANIFEST); meta.size = len(encoded); meta.mode = 0o600
            tar.addfile(meta, io.BytesIO(encoded))
            for name, entry in files.items():
                path = root / name
                before = stable_signature(path)
                info = tarfile.TarInfo(name); info.size = entry['bytes']; info.mode = 0o600
                with path.open('rb') as handle:
                    tar.addfile(info, handle)
                if before != stable_signature(path):
                    raise RecoveryBlocked('Bundle source changed during archive creation')
            output.flush(); os.fsync(output.fileno())
        if inventory(root) != files:
            raise RecoveryBlocked('Bundle source changed during archive creation')
        # Validate what actually entered the archive, including mid-copy changes.
        preflight_tar(archive)
        with tarfile.open(archive, 'r:') as tar:
            check_archive(tar, descriptor)
        part = destination.with_name(destination.name + '.part')
        # Failures retain an incomplete encrypted output; no retry overwrites it.
        with private_file(part) as output, keyring(gpg, scratch) as base:
            import_key(base, public_key, recipient, secret=False)
            captured(base + ['--trust-model', 'always', '--recipient', fingerprint(recipient),
                             '--cipher-algo', 'AES256', '--force-mdc', '--compress-algo', 'none',
                             '--output', '-', '--encrypt', gpg_path(gpg, archive)], output=output)
            output.flush(); os.fsync(output.fileno())
        if not part.stat().st_size:
            raise RecoveryBlocked('Encryption produced no ciphertext')
        # Hard-link publication refuses a concurrently created destination.
        os.link(part, destination); part.unlink(); sync_directory(destination.parent)
    return {'backup_id': descriptor['backup_id'], 'ciphertext_sha256': digest_file(destination),
            'manifest_sha256': descriptor_digest(descriptor),
            'ciphertext_bytes': destination.stat().st_size, 'files': len(files),
            'plaintext_bytes': sum(e['bytes'] for e in files.values()),
            'recovery_point_utc': descriptor['recovery_point_utc'],
            'package_seconds': round(monotonic() - started, 3), 'production_acceptance': False}


def read_descriptor(tar):
    member = tar.next()
    if member is None or member.name != MANIFEST or not member.isfile() or member.size > MAX_MANIFEST:
        raise RecoveryBlocked('Authenticated recovery manifest must be first')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        value = json.loads(tar.extractfile(member).read(), object_pairs_hook=unique)
        if (not isinstance(value, dict) or set(value) != {'format', 'backup_id', 'created_at', 'recovery_point_utc', 'files'}
                or type(value['format']) is not int or value['format'] != 1
                or str(UUID(value['backup_id'])) != value['backup_id']):
            raise ValueError
        recovery_point(value['created_at']); recovery_point(value['recovery_point_utc'])
        files, total, folded = value['files'], 0, set()
        if not isinstance(files, dict) or not REQUIRED <= files.keys() or len(files) > MAX_FILES:
            raise ValueError
        for name, entry in files.items():
            safe_name(name)
            if (name.split('/')[0] not in COMPONENTS or name.casefold() in folded
                    or not isinstance(entry, dict) or set(entry) != {'bytes', 'sha256'}
                    or type(entry['bytes']) is not int or entry['bytes'] < 0
                    or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                raise ValueError
            folded.add(name.casefold()); total += entry['bytes']
        if total > MAX_BYTES or any(not any(n.startswith(c + '/') for n in files) for c in COMPONENTS):
            raise ValueError
        # Reject file/parent collisions before writing any destination file.
        for name in folded:
            if any(str(parent) in folded for parent in PurePosixPath(name).parents if str(parent) != '.'):
                raise ValueError
        return value
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        raise RecoveryBlocked('Invalid authenticated recovery manifest') from None


def check_archive(tar, expected=None, destination=None):
    descriptor = read_descriptor(tar)
    if expected is not None and descriptor != expected:
        raise RecoveryBlocked('Archive manifest changed')
    seen = set()
    while (member := tar.next()) is not None:
        name = safe_name(member.name)
        entry = descriptor['files'].get(name)
        if (entry is None or name in seen or not member.isfile() or member.issparse()
                or member.size != entry['bytes']):
            raise RecoveryBlocked('Unexpected archive member')
        seen.add(name)
        if len(seen) > MAX_FILES:
            raise RecoveryBlocked('Archive exceeds recovery limits')
        digest, size = hashlib.sha256(), 0
        handle = tar.extractfile(member)
        target = None
        try:
            if destination is not None:
                path = no_links(destination / name)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = private_file(path)
            while chunk := handle.read(CHUNK):
                size += len(chunk); digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            if target is not None:
                target.flush(); os.fsync(target.fileno())
        finally:
            handle.close()
            if target is not None:
                target.close()
        if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
            raise RecoveryBlocked('Restored file integrity mismatch')
    if seen != descriptor['files'].keys():
        raise RecoveryBlocked('Required archive member is missing')
    return descriptor


def restore_bundle(bundle, destination, *, expected_sha256, gpg, private_key, recipient,
                   passphrase, scratch):
    started = monotonic()
    bundle, destination, private_key = no_links(bundle), no_links(destination), no_links(private_key)
    scratch = scratch_directory(scratch)
    if (not re.fullmatch('[0-9a-f]{64}', expected_sha256 or '') or not bundle.is_file()
            or digest_file(bundle) != expected_sha256):
        raise RecoveryBlocked('Ciphertext differs from trusted backup inventory')
    if (destination.exists() or not destination.parent.is_dir() or not separate(destination, bundle)
            or not separate(destination, scratch) or not separate(destination, private_key)
            or not separate(bundle.parent, private_key)):
        raise RecoveryBlocked('Restore needs a new destination and separately held private key')
    if not isinstance(passphrase, bytes) or not passphrase or b'\n' in passphrase or b'\r' in passphrase:
        raise RecoveryBlocked('A single-line private key passphrase is required')
    with tempfile.TemporaryDirectory(prefix='recovery-decrypt-', dir=scratch) as folder:
        os.chmod(folder, 0o700)
        archive = Path(folder) / 'bundle.tar'
        # Exit status includes OpenPGP integrity validation. Partial plaintext
        # is never extracted if the password/key/MDC check fails.
        with private_file(archive) as output, keyring(gpg, scratch) as base:
            import_key(base, private_key, recipient, secret=True)
            captured(base + ['--max-output', str(MAX_BYTES + MAX_MANIFEST + MAX_FILES * 2048),
                             '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                             '--output', '-', '--decrypt', gpg_path(gpg, bundle)], payload=passphrase + b'\n', output=output)
            output.flush(); os.fsync(output.fileno())
        preflight_tar(archive)
        with tarfile.open(archive, 'r:') as tar:
            descriptor = check_archive(tar)
        destination.mkdir(mode=0o700)
        with private_file(destination / INCOMPLETE) as marker:
            marker.write(b'Incomplete offline restore; do not start applications.\n')
            marker.flush(); os.fsync(marker.fileno())
        with tarfile.open(archive, 'r:') as tar:
            check_archive(tar, descriptor, destination)
        with private_file(destination / COMPLETE) as output:
            output.write(json.dumps({'ciphertext_sha256': expected_sha256, 'manifest': descriptor},
                                    sort_keys=True).encode())
            output.flush(); os.fsync(output.fileno())
        # Complete durability of newly created directories before certification.
        for directory in sorted((p for p in destination.rglob('*') if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            sync_directory(directory)
        sync_directory(destination)
        (destination / INCOMPLETE).unlink(); sync_directory(destination)
        sync_directory(destination.parent)
    return {'backup_id': descriptor['backup_id'], 'files': len(descriptor['files']),
            'manifest_sha256': descriptor_digest(descriptor),
            'plaintext_bytes': sum(e['bytes'] for e in descriptor['files'].values()),
            'recovery_point_utc': descriptor['recovery_point_utc'],
            'decrypt_and_extract_seconds': round(monotonic() - started, 3),
            'production_acceptance': False}


def verify_restored(root, expected_sha256, expected_manifest_sha256):
    root = no_links(root)
    if (root / INCOMPLETE).exists() or not (root / COMPLETE).is_file():
        raise RecoveryBlocked('Restore is incomplete')
    no_links(root / COMPLETE)
    try:
        if (root / COMPLETE).stat().st_size > MAX_MANIFEST + 1024:
            raise ValueError
        certificate = json.loads((root / COMPLETE).read_text(encoding='utf-8'))
        if (not re.fullmatch('[0-9a-f]{64}', expected_sha256 or '')
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest_sha256 or '')
                or certificate['ciphertext_sha256'] != expected_sha256
                or descriptor_digest(certificate['manifest']) != expected_manifest_sha256):
            raise ValueError
        expected = certificate['manifest']['files']
    except (ValueError, KeyError, TypeError):
        raise RecoveryBlocked('Invalid restore certificate') from None
    actual = {}
    for path in root.rglob('*'):
        no_links(path)
        if path.is_dir() or path == root / COMPLETE:
            continue
        stable_signature(path)
        name = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if name not in expected or size != expected[name]['bytes']:
            raise RecoveryBlocked('Recovered files differ from the certified manifest')
        actual[name] = {'bytes': size, 'sha256': digest_file(path)}
    if actual != expected:
        raise RecoveryBlocked('Recovered files differ from the certified manifest')
    return {'verified_files': len(expected), 'production_acceptance': False}
