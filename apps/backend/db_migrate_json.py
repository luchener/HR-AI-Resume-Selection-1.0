"""Inventory and transactionally import the legacy JSON store into PostgreSQL.

Default mode is read-only dry-run. Apply requires --apply and the dedicated
DATABASE_MIGRATION_URL environment variable. It never deletes/modifies JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from db_json_compare import json_equal


TABLE_DIRS = {
    "users": "users",
    "resumes": "resumes",
    "jobs": "jobs",
    "archives": "archives",
}
IGNORED_PREFIXES = ("_", ".")


def strict_source_json(raw: str):
    """Reject ambiguous objects and values PostgreSQL JSONB cannot represent."""
    import math
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError('Nonfinite JSON number')
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite JSON number')
        return parsed
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant, parse_float=finite_float)
    def validate(value):
        if isinstance(value, str):
            if '\x00' in value:
                raise ValueError('JSON contains null character')
            value.encode('utf-8')  # Reject lone Unicode surrogate code points.
        elif isinstance(value, dict):
            for key, child in value.items():
                validate(key)
                validate(child)
        elif isinstance(value, list):
            for child in value:
                validate(child)
    validate(result)
    return result


def snapshot_login_failure(report, username):
    """Read only the already hashed/parsed snapshot; never reopen live files."""
    failure_key = hashlib.sha256(str(username).lower().encode()).hexdigest()
    key = f'login_failures/{failure_key}.json'
    for _, record_key, payload in report['legacy_records']:
        if record_key == key:
            value = payload['value']
            if not isinstance(value, dict):
                raise ValueError('Invalid login failure document')
            if 'frozen' in value and type(value['frozen']) is not bool:
                raise ValueError('Invalid login freeze flag')
            if value.get('frozen_at') is not None:
                import math
                timestamp = value['frozen_at']
                if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                    raise ValueError('Invalid login freeze timestamp')
                try:
                    datetime.fromtimestamp(timestamp / 1000 if timestamp > 10**12 else timestamp, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    raise ValueError('Invalid login freeze timestamp') from None
            if value.get('frozen_by') not in (None, '', 'auto', 'admin'):
                raise ValueError('Invalid login freeze origin')
            reason = value.get('frozen_reason') or ''
            if not isinstance(reason, str) or len(reason) > 512 or '\x00' in reason:
                raise ValueError('Invalid login freeze reason')
            count = value.get('consecutive_failures', 0)
            if type(count) is not int or not 0 <= count <= 2_147_483_647 or not isinstance(value.get('failures', []), list):
                raise ValueError('Invalid login failure counts')
            return value
    return {}


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Invalid source timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError('Invalid source timestamp') from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _uuid(value: Any, fallback: str | None = None) -> uuid.UUID:
    try:
        parsed = uuid.UUID(str(value))
        if not parsed.int:
            raise ValueError('zero UUID is not a valid identity')
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ValueError("missing or invalid UUID; migration never invents replacement identities")


def inventory(data_dir: Path) -> dict[str, Any]:
    loaded: dict[str, list[tuple[Path, dict]]] = {}
    errors: list[str] = []
    manifest = {}
    # Reparse the exact bytes hashed in the full snapshot manifest, so imported
    # payload and checksums cannot refer to different versions of a file.
    loaded = {name: [] for name in TABLE_DIRS}
    legacy = []
    for path in sorted(data_dir.rglob('*')):
        if path.is_symlink():
            errors.append('Symbolic links are not accepted in migration snapshots')
            continue
        if not path.is_file() or path.name.endswith(('.lock', '.tmp')):
            continue
        relative = path.relative_to(data_dir)
        raw = path.read_bytes()
        manifest[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        if path.suffix != '.json':
            continue
        try:
            payload = strict_source_json(raw.decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError):
            errors.append(f'{relative.as_posix()}: invalid JSON')
            continue
        if len(relative.parts) == 2 and relative.parts[0] in TABLE_DIRS and not path.name.startswith(IGNORED_PREFIXES):
            if not isinstance(payload, dict):
                errors.append(f'{relative.as_posix()}: expected JSON object')
            else:
                loaded[relative.parts[0]].append((path, payload))
        else:
            # Wrapper allows arrays, scalars and top-level metadata losslessly.
            legacy.append((relative.parts[0] if len(relative.parts) > 1 else '__root__', relative.as_posix(), {'value': payload}))
    def canonical(value):
        try:
            return str(_uuid(value))
        except ValueError:
            return None  # Validation reports invalid identities separately.
    users = {canonical(row.get('user_id')) for _, row in loaded['users']} - {None}
    resumes = {canonical(row.get('resume_id')) for _, row in loaded['resumes']} - {None}
    jobs = {canonical(row.get('job_id')) for _, row in loaded['jobs']} - {None}
    orphan_resumes = sum(canonical(r.get('user_id')) not in users for _, r in loaded['resumes'])
    orphan_jobs = sum(canonical(r.get('user_id')) not in users for _, r in loaded['jobs'])
    orphan_archives = sum(canonical(r.get('owner_user_id') or r.get('user_id')) not in users for _, r in loaded['archives'])
    digest_material = json.dumps(manifest, sort_keys=True)
    return {
        "manifest": manifest,
        "source_dir": str(data_dir),
        "counts": {table: len(rows) for table, rows in loaded.items()},
        "auxiliary_json_records": len(legacy),
        "invalid_json_files": errors,
        "orphan_owner_references": {
            "resumes": orphan_resumes,
            "jobs": orphan_jobs,
            "archives": orphan_archives,
        },
        "inventory_sha256": hashlib.sha256(digest_material.encode()).hexdigest(),
        "records": loaded,
        "legacy_records": legacy,
        "known_ids": {"users": users, "resumes": resumes, "jobs": jobs},
    }


def _safe_id(row: dict, key: str, path: Path | None = None, default: str | None = None) -> str | None:
    value = row.get(key)
    if value:
        return str(value)
    if path is not None:
        return path.stem
    return default


def _user_record(path: Path, row: dict) -> dict:
    uid = _uuid(row.get("user_id"), f"users/{path.name}")
    email = str(row.get("email") or "").strip().lower() or None
    deleted = bool(row.get("deleted_at"))
    known = {
        "user_id", "username", "email", "password_hash", "password_salt", "password_version",
        "is_admin", "is_super_admin", "created_at", "updated_at", "last_login_at", "deleted_at",
        "deleted_by", "restored_at", "password_updated_at", "frozen_until", "frozen_reason",
    }
    version = row.get('password_version', 1)
    if type(version) is not int or version < 1 or version > 2_147_483_647:
        raise ValueError('Invalid source password version')
    return {
        "id": uid,
        "username": str(row.get("username") or path.stem),
        "email": email,
        "password_hash": str(row.get("password_hash") or ""),
        "password_salt": str(row.get("password_salt") or ""),
        "password_version": version,
        "is_admin": bool(row.get("is_admin")),
        "is_super_admin": bool(row.get("is_super_admin")),
        "status": "deleted" if deleted else ("locked" if row.get("frozen_until") else "active"),
        "deleted_at": _parse_dt(row.get("deleted_at")),
        "frozen_at": None,
        "frozen_by": 'auto' if row.get('frozen_until') else None,
        "frozen_reason": str(row.get('frozen_reason') or '') if row.get('frozen_until') else '',
        "deleted_by": str(row.get('deleted_by') or '') if deleted else '',
        "deletion_previous_status": ('locked' if row.get('frozen_until') else 'active') if deleted else None,
        "login_consecutive_failures": 0,
        "login_window_failures": 0,
        "created_at": _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_dt(row.get("updated_at")) or datetime.now(timezone.utc),
        "last_login_at": _parse_dt(row.get("last_login_at")),
        "legacy_payload": dict(row),
    }


def _resume_record(path: Path, row: dict) -> dict:
    from hashlib import sha256
    content = str(row.get("content") or "")
    processed = row.get("processed") if isinstance(row.get("processed"), dict) else {}
    personal = processed.get("personal_data") if isinstance(processed.get("personal_data"), dict) else {}
    original = row.get("original") if isinstance(row.get("original"), dict) else {}
    return {
        "id": _uuid(row.get("resume_id"), f"resumes/{path.name}"),
        "owner_user_id": _uuid(row.get("user_id")),
        "candidate_name": personal.get("name") or personal.get("姓名"),
        "source_filename": original.get("name") or original.get("filename"),
        "content_type": str(row.get("content_type") or "md"),
        "content_sha256": original.get("sha256") or sha256(content.encode("utf-8")).hexdigest(),
        "object_key": original.get("storage_key"),
        "raw_content": content,
        "legacy_payload": dict(row),
        "processed_data": processed,
        "parse_status": "parsed",
        "created_at": _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_dt(row.get("updated_at")) or _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "deleted_at": _parse_dt(row.get("deleted_at")),
        "retention_until": _parse_dt(original.get("retention_until")),
    }


def _job_record(path: Path, row: dict) -> dict:
    processed = row.get("processed") if isinstance(row.get("processed"), dict) else {}
    return {
        "id": _uuid(row.get("job_id"), f"jobs/{path.name}"),
        "owner_user_id": _uuid(row.get("user_id")),
        "source_resume_id": _uuid(row.get("resume_id")) if row.get("resume_id") else None,
        "title": processed.get("job_title"),
        "company_name": (processed.get("company_profile") or {}).get("company_name") if isinstance(processed.get("company_profile"), dict) else None,
        "raw_content": str(row.get("content") or ""),
        "legacy_payload": dict(row),
        "parsed_requirements": processed,
        "created_at": _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_dt(row.get("updated_at")) or _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "deleted_at": _parse_dt(row.get("deleted_at")),
    }


def archive_identity(row):
    identifiers = {_uuid(row[key]) for key in ('archive_id', 'archiveId', 'id') if row.get(key)}
    if len(identifiers) != 1:
        raise ValueError('Missing or conflicting archive identities')
    return identifiers.pop()


def _archive_record(path: Path, row: dict) -> dict:
    known = {"archive_id", "archiveId", "id", "user_id", "owner_user_id", "resume_id", "job_id", "analysis_id", "candidate_name", "category", "tags", "status", "note", "created_at", "updated_at", "deleted_at", "trashed_at"}
    return {
        "id": archive_identity(row),
        "owner_user_id": _uuid(row.get("owner_user_id") or row.get("user_id")),
        "resume_id": _uuid(row.get("resume_id")) if row.get("resume_id") else None,
        "job_id": _uuid(row.get("job_id")) if row.get("job_id") else None,
        "analysis_id": _uuid(row.get("analysis_id")) if row.get("analysis_id") else None,
        "candidate_name": row.get("candidate_name"),
        "category": row.get("category"),
        "legacy_payload": dict(row),
        "tags": row.get("custom_tags", row.get("tags", [])),
        "status": "trashed" if row.get("trashed_at") else str(row.get("status") or "active"),
        "note": row.get("note"),
        "payload": {k: v for k, v in row.items() if k not in known},
        "created_at": _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_dt(row.get("updated_at")) or _parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
        "deleted_at": _parse_dt(row.get("deleted_at")),
    }


def validate_inventory(report: dict[str, Any]) -> None:
    if report["invalid_json_files"] or any(report["orphan_owner_references"].values()):
        raise ValueError("Invalid JSON or orphan owners; source repair required")
    if any(root == '__migration__' for root, _, _ in report['legacy_records']):
        raise ValueError('Source collides with reserved migration namespace')
    names, emails, user_ids = set(), set(), set()
    for path, row in report["records"]["users"]:
        uid = _uuid(row.get('user_id'))
        if uid in user_ids:
            raise ValueError('Duplicate source user UUID')
        user_ids.add(uid)
        snapshot_login_failure(report, row.get('username'))
        name = str(row.get("username") or "").lower()
        email = str(row.get("email") or "").strip().lower()
        if not name or name in names or (email and email in emails):
            raise ValueError("Missing or duplicate user identity")
        if not row.get("password_hash") or not row.get("password_salt"):
            raise ValueError("User password material missing")
        names.add(name)
        if email:
            emails.add(email)
    from db_models import User, Resume, Job, Archive
    from sqlalchemy import String
    converters = {'users': (_user_record, User), 'resumes': (_resume_record, Resume),
                  'jobs': (_job_record, Job), 'archives': (_archive_record, Archive)}
    for table, (convert, model) in converters.items():
        for path, row in report['records'][table]:
            converted = convert(path, row)
            for column in model.__table__.columns:
                value = converted.get(column.name)
                if value is not None and isinstance(column.type, String):
                    if not isinstance(value, str) or '\x00' in value or (column.type.length and len(value) > column.type.length):
                        raise ValueError('Source text does not fit target column')
            if table == 'archives' and converted['status'] not in {'active', 'trashed'}:
                raise ValueError('Invalid source archive status')
    reference_indexes = {table: {_uuid(row.get(key)): row for path, row in report['records'][table]}
                         for table, key in (('resumes', 'resume_id'), ('jobs', 'job_id'))}
    for table, key in (("resumes", "resume_id"), ("jobs", "job_id"), ("archives", "archive_id")):
        seen = set()
        for path, row in report["records"][table]:
            identifier = archive_identity(row) if table == 'archives' else _uuid(row.get(key))
            if identifier in seen:
                raise ValueError("Duplicate source identifier")
            seen.add(identifier)
            if row.get('owner_user_id') and row.get('user_id') and _uuid(row['owner_user_id']) != _uuid(row['user_id']):
                raise ValueError('Conflicting source owner identities')
            owner = str((row.get('owner_user_id') or row.get('user_id')) if table == 'archives' else row.get('user_id') or '')
            _uuid(owner)
            for target, field in (("resumes", "resume_id"), ("jobs", "job_id")):
                if table == target:
                    continue
                ref = row.get(field)
                if ref:
                    canonical_ref = _uuid(ref)
                    target_row = reference_indexes[target].get(canonical_ref)
                    if target_row is not None and _uuid(target_row.get('owner_user_id') or target_row.get('user_id')) != _uuid(owner):
                        raise ValueError('Cross-owner source reference')


def apply_import(report: dict[str, Any], database_url: str) -> dict[str, int]:
    from sqlalchemy import select, text
    from db_security import migration_engine
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.orm import Session
    from db_models import Archive, Job, Resume, User, LegacyRecord

    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("migration DSN must use PostgreSQL")
    validate_inventory(report)
    from db_originals import original_manifest
    original_manifest(Path(report['source_dir']))
    engine = migration_engine(database_url)
    imported = {name: 0 for name in (*TABLE_DIRS, "legacy_records", "announcements", "announcement_dismissals")}
    try:
        with Session(engine) as session, session.begin():
            # Serialize imports and block stale reimports instead of silently skipping changes.
            session.execute(text("SELECT pg_advisory_xact_lock(710413501)"))
            from db_import_attestation import lock_target, target_attestation, verify_attestation, ATTESTATION_KEY
            lock_target(session)
            source = Path(report["source_dir"])
            if inventory(source)["manifest"] != report["manifest"]:
                raise ValueError("Source changed after inventory; use a frozen snapshot")
            ledger = session.scalar(select(LegacyRecord).where(LegacyRecord.root_name == "__migration__", LegacyRecord.record_key == "source_manifest"))
            if ledger is not None:
                expected_policy = session.scalar(select(LegacyRecord).where(LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == 'identity_policy'))
                current_policy = {'admin_emails': sorted({v.strip().lower() for v in os.environ.get('MIGRATION_ADMIN_EMAILS', '').split(',') if v.strip()}), 'auto_unfreeze_hours': os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0')}
                if ledger.payload != report["manifest"] or expected_policy is None or expected_policy.payload != current_policy:
                    raise ValueError("Target already imported a different snapshot or identity policy; incremental overwrite forbidden")
                for model, key, table in ((User, 'user_id', 'users'), (Resume, 'resume_id', 'resumes'), (Job, 'job_id', 'jobs'), (Archive, 'archive_id', 'archives')):
                    for path, row in report['records'][table]:
                        current = session.get(model, archive_identity(row) if table == 'archives' else _uuid(row.get(key)))
                        if current is None or not json_equal(current.legacy_payload, row):
                            raise ValueError('Target content differs; unchanged snapshot cannot be certified')
                from db_models import SystemConfig
                envelope = session.scalar(select(SystemConfig.value).where(SystemConfig.key == 'system_config'))
                source_configs = {key: payload['value'] for root, key, payload in report['legacy_records'] if root == 'system_config'}
                if source_configs:
                    if envelope is None:
                        raise ValueError('Encrypted target configuration missing')
                    from db_secrets import ConfigCipher
                    decrypted = ConfigCipher.from_env(key_id=os.environ.get('DATABASE_CONFIG_KEY_ID', 'migration-v1')).decrypt(envelope)
                    if not json_equal(decrypted, source_configs):
                        raise ValueError('Encrypted target configuration differs from source')
                elif envelope is not None:
                    raise ValueError('Unexpected target configuration')
                attested = session.scalar(select(LegacyRecord.payload).where(LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == ATTESTATION_KEY))
                verify_attestation(session, attested)
                return {"unchanged_snapshot": 1}
            from db_check_schema import metadata as target_metadata
            for target in target_metadata().sorted_tables:
                if session.execute(select(1).select_from(target).limit(1)).first() is not None:
                    raise ValueError("Initial import requires all target business tables to be empty")
            admins = {v.strip().lower() for v in os.environ.get("MIGRATION_ADMIN_EMAILS", "").split(',') if v.strip()}
            if "MIGRATION_ADMIN_EMAILS" not in os.environ:
                raise ValueError("Set MIGRATION_ADMIN_EMAILS to the source ADMIN_EMAILS explicitly")
            import math
            auto_hours = float(os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0'))
            if not math.isfinite(auto_hours) or auto_hours < 0:
                raise ValueError('Invalid automatic unfreeze policy')
            evaluated_at = datetime.now(timezone.utc)
            user_rows = []
            for path, row in report["records"]["users"]:
                converted = _user_record(path, row)
                converted["is_super_admin"] = bool(converted["email"] in admins)
                converted["is_admin"] = converted["is_super_admin"] or bool(row.get("is_admin"))
                failure = snapshot_login_failure(report, row['username'])
                converted['login_consecutive_failures'] = failure.get('consecutive_failures', 0)
                converted['login_window_failures'] = len(failure.get('failures', []))
                frozen = bool(failure.get('frozen'))
                if frozen and failure.get('frozen_by') != 'admin' and auto_hours > 0 and failure.get('frozen_at'):
                    epoch = float(failure['frozen_at'])
                    age = evaluated_at.timestamp() - (epoch / 1000 if epoch > 10**12 else epoch)
                    if age > auto_hours * 3600:
                        frozen = False
                if frozen:
                    if converted['status'] == 'deleted':
                        converted['deletion_previous_status'] = 'locked'
                    else:
                        converted['status'] = 'locked'
                    timestamp = failure.get('frozen_at')
                    converted['frozen_at'] = (datetime.fromtimestamp(timestamp / 1000 if timestamp > 10**12 else timestamp,
                                                                    timezone.utc) if timestamp is not None else None)
                    converted['frozen_by'] = failure.get('frozen_by') or 'auto'
                    converted['frozen_reason'] = failure.get('frozen_reason') or ''
                user_rows.append(converted)
            for model, rows in ((User, user_rows),):
                if rows:
                    for start in range(0, len(rows), 100):
                        session.execute(insert(model), rows[start:start + 100])
                    imported["users"] = len(rows)

            resume_rows = [_resume_record(path, row) for path, row in report["records"]["resumes"]]
            job_rows = [_job_record(path, row) for path, row in report["records"]["jobs"]]
            archive_rows = [_archive_record(path, row) for path, row in report["records"]["archives"]]
            resume_ids = {r["id"] for r in resume_rows}
            job_ids = {r["id"] for r in job_rows}
            for row in job_rows:
                if row["source_resume_id"] not in resume_ids:
                    row["source_resume_id"] = None
            for row in archive_rows:
                if row["resume_id"] not in resume_ids:
                    row["resume_id"] = None
                if row["job_id"] not in job_ids:
                    row["job_id"] = None
                # Historical analysis lives in payload; no invented analysis FK.
                row["analysis_id"] = None
            for model, rows, name in ((Resume, resume_rows, "resumes"), (Job, job_rows, "jobs"), (Archive, archive_rows, "archives")):
                if rows:
                    for start in range(0, len(rows), 100):
                        session.execute(insert(model), rows[start:start + 100])
                    imported[name] = len(rows)
            # Announcement files are legacy auxiliary data, but their runtime
            # contract is transactional and tenant-sensitive. Project them
            # into 0016 before the generic legacy ledger is written; the
            # projection itself is idempotent and bound to this frozen report.
            from db_announcement_import import apply_announcement_snapshot
            announcement_counts = apply_announcement_snapshot(session, report)
            imported['announcements'] = announcement_counts['announcements']
            imported['announcement_dismissals'] = announcement_counts['dismissals']
            from db_import_auxiliary import convert_auxiliary
            from db_quota import quota_policies, quota_daily_usage
            auxiliary = {key: payload['value'] for root, key, payload in report['legacy_records']}
            usage = {Path(key).stem: value for key, value in auxiliary.items() if key.startswith('usage/') and len(Path(key).parts) == 2}
            converted_aux = convert_auxiliary(users=[r for p, r in report['records']['users']], limits=auxiliary.get('quota/limits.json'), usage_by_user_id=usage)
            from db_audit_import import insert_audits
            imported['audit_logs'] = insert_audits(session, {key: value for key, value in auxiliary.items() if key.startswith('admin_ops/')}, report['known_ids']['users'])
            for name, target in (('quota_policies', quota_policies), ('quota_daily_usage', quota_daily_usage)):
                rows = converted_aux[name]
                for start in range(0, len(rows), 100):
                    session.execute(insert(target), rows[start:start + 100])
                imported[name] = len(rows)
            config_payload = {key: value for key, value in auxiliary.items() if key.startswith('system_config/')}
            config_cipher = None
            if config_payload:
                from db_secrets import ConfigCipher, EncryptedConfigRepository
                config_cipher = ConfigCipher.from_env(key_id=os.environ.get('DATABASE_CONFIG_KEY_ID', 'migration-v1'))
                EncryptedConfigRepository(session, config_cipher).put(config_payload)
                imported['encrypted_system_config'] = len(config_payload)
            for root, key, payload in report["legacy_records"]:
                if root == 'system_config':
                    payload = {'encrypted_in': 'system_configs', 'source_key': key}
                session.execute(text("""INSERT INTO legacy_records (root_name, record_key, payload)
                    VALUES (:root, :key, CAST(:payload AS jsonb))
                    ON CONFLICT (root_name, record_key) DO NOTHING"""),
                    {"root": root, "key": key, "payload": json.dumps(payload, ensure_ascii=False)})
                imported["legacy_records"] += 1
            for table, rows in report["records"].items():
                for path, payload in rows:
                    session.add(LegacyRecord(root_name=table, record_key=f'{table}/{path.name}', payload=payload))
            if inventory(source)['manifest'] != report['manifest']:
                raise ValueError('Source changed during import; transaction rolled back')
            session.add(LegacyRecord(root_name='__migration__', record_key='evaluation_time', payload={'evaluated_at': evaluated_at.isoformat()}))
            session.add(LegacyRecord(root_name='__migration__', record_key='identity_policy', payload={'admin_emails': sorted(admins), 'auto_unfreeze_hours': os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0')}))
            session.add(LegacyRecord(root_name="__migration__", record_key="source_manifest", payload=report["manifest"]))
            session.flush()
            session.add(LegacyRecord(root_name='__migration__', record_key=ATTESTATION_KEY, payload=target_attestation(session)))
        return imported
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, help="Path to legacy apps/backend/data")
    parser.add_argument("--apply", action="store_true", help="Import into PostgreSQL; default is read-only dry run")
    parser.add_argument("--database-url-env", default="DATABASE_MIGRATION_URL", help="Environment variable containing migration DSN")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    if not data_dir.is_dir():
        parser.error("data_dir must be an existing directory")
    report = inventory(data_dir)
    public = {k: v for k, v in report.items() if k not in {"records", "legacy_records", "known_ids", "manifest", "source_dir"}}
    try:
        validate_inventory(report)
        from db_import_auxiliary import convert_auxiliary
        auxiliary = {key: payload['value'] for root, key, payload in report['legacy_records']}
        usage = {Path(key).stem: value for key, value in auxiliary.items() if key.startswith('usage/') and len(Path(key).parts) == 2}
        converted = convert_auxiliary(users=[row for path, row in report['records']['users']], limits=auxiliary.get('quota/limits.json'), usage_by_user_id=usage)
        from db_audit_import import audit_rows
        audits = audit_rows({key: value for key, value in auxiliary.items() if key.startswith('admin_ops/')}, report['known_ids']['users'])
        public['auxiliary_preflight'] = {name: len(converted[name]) for name in ('quota_policies', 'quota_daily_usage')}
        public['auxiliary_preflight']['audit_logs'] = len(audits)
        from db_originals import original_manifest
        originals = original_manifest(data_dir)
        if inventory(data_dir)['manifest'] != report['manifest']:
            raise ValueError('Source changed during offline preflight')
        public['originals_preflight'] = {'verified_files': len(originals), 'verified_bytes': sum(entry['bytes'] for entry in originals)}
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        print(f'Preflight blocked ({type(exc).__name__}); inspect protected source snapshot.', file=sys.stderr)
        return 2
    print(json.dumps(public, ensure_ascii=False, indent=2))
    if report["invalid_json_files"] or any(report["orphan_owner_references"].values()):
        print("Import blocked: inventory has invalid JSON or orphan owner references.", file=sys.stderr)
        return 2
    if not args.apply:
        print("DRY RUN ONLY: no database writes performed. Binary files stay in source volume; this tool does not authorize cutover.")
        return 0
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        print(f"Import blocked: set {args.database_url_env} explicitly.", file=sys.stderr)
        return 2
    try:
        counts = apply_import(report, database_url)
    except Exception as exc:
        # Never print a DSN, bound parameters, resume text or password hashes.
        print(f'Import failed ({type(exc).__name__}); transaction rolled back. Inspect protected diagnostics.', file=sys.stderr)
        return 2
    print(json.dumps({"imported": counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
