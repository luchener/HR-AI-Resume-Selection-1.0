"""Verify/copy original resume files between offline migration snapshots.

No deletion, no overwrite. Destination must be an empty existing directory.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def original_manifest(data_dir: Path) -> list[dict]:
    from db_migrate_json import inventory
    report = inventory(data_dir)
    result = []
    for _, row in report['records']['resumes']:
        meta = row.get('original')
        if not meta:
            continue
        rid = str(uuid.UUID(str(row['resume_id'])))
        ext = str(meta.get('ext', '')).lower().lstrip('.')
        if ext not in {'pdf', 'docx'}:
            raise ValueError('Unsupported original extension')
        relative = Path('resume_originals') / f'{rid}.{ext}'
        path = data_dir / relative
        if path.is_symlink() or (data_dir / 'resume_originals').is_symlink() or not path.is_file():
            raise ValueError('Referenced original missing or symlinked')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        if digest != meta.get('sha256') or size != meta.get('bytes'):
            raise ValueError('Original hash/size mismatch')
        result.append({'path': relative.as_posix(), 'sha256': digest, 'bytes': size})
    return result


def copy_originals(data_dir: Path, destination: Path) -> dict:
    if destination.is_symlink():
        raise ValueError('Destination must not be symlinked')
    data_dir, destination = data_dir.resolve(), destination.resolve()
    if not destination.is_dir() or any(destination.iterdir()):
        raise ValueError('Destination must be an existing empty directory')
    if destination == data_dir or data_dir in destination.parents or destination in data_dir.parents:
        raise ValueError('Source and destination must be separate')
    manifest = original_manifest(data_dir)
    # Failures deliberately leave an uncertified destination for inspection.
    # Never delete partial files automatically or permit overwriting on retry.
    incomplete = destination / '.originals-copy-incomplete'
    with incomplete.open('x', encoding='utf-8') as marker:
        marker.write('Copy in progress; not certified for restoration.\n')
        marker.flush()
        os.fsync(marker.fileno())
    for entry in manifest:
        source = data_dir / entry['path']
        target = destination / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Copied original failed integrity validation')
    if original_manifest(data_dir) != manifest:
        raise ValueError('Source originals changed during copy')
    certificate = {'version': 1, 'manifest': manifest}
    completed = destination / 'originals-manifest.json'
    with completed.open('x', encoding='utf-8') as handle:
        json.dump(certificate, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    # Exact marker created above, only after successful verification/certificate.
    incomplete.unlink()
    return {'verified_copied': len(manifest), 'manifest': manifest}


def verify_original_copy(destination: Path, expected_manifest: list[dict]) -> dict:
    """A certificate alone is insufficient: verify against trusted source manifest."""
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError('Invalid original copy destination')
    if (destination / '.originals-copy-incomplete').exists():
        raise ValueError('Original copy is incomplete')
    certificate_path = destination / 'originals-manifest.json'
    if certificate_path.is_symlink():
        raise ValueError('Symlinked original certificate')
    certificate = json.loads(certificate_path.read_text(encoding='utf-8'))
    if certificate != {'version': 1, 'manifest': expected_manifest}:
        raise ValueError('Original certificate differs from expected manifest')
    allowed = {'originals-manifest.json'}
    for entry in expected_manifest:
        relative = Path(entry['path'])
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != 'resume_originals' or relative.parts[1] in {'.', '..'}:
            raise ValueError('Invalid original manifest path')
        target = destination / relative
        if target.is_symlink() or target.parent.is_symlink() or not target.is_file():
            raise ValueError('Restored original missing or symlinked')
        if target.stat().st_size != entry['bytes'] or hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Restored original integrity mismatch')
        allowed.add(relative.as_posix())
    for path in destination.rglob('*'):
        if path.is_symlink() or (path.is_file() and path.relative_to(destination).as_posix() not in allowed):
            raise ValueError('Unexpected file in original copy')
    return {'verified_originals': len(expected_manifest)}
