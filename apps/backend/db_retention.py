"""Transactional retention cleanup for the isolated PostgreSQL adapter.

The JSON retention command is not used by this module.  A caller supplies the
database session, current UTC instant and the immutable original store.  Source
rows remain as FK tombstones so analyses and archives stay readable; all source
text and migration copies are redacted in the same transaction.  Physical
objects are only removed later by ``ContentService.collect_originals`` from the
durable ``original_gc`` queue.

This module is intentionally unwired.  Production scheduling must first share
the deployment drain tracker and pass an independently reviewed policy.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_audit import append_audit
from db_models import AnalysisTask, Archive, Job, Resume, User
from db_original_gc import original_gc


class RetentionError(ValueError):
    """Fixed, non-sensitive validation or policy failure."""


TERMINAL_TASKS = frozenset({"completed", "partial_success", "failed", "cancelled"})


def _validate_policy(now, days, limit):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise RetentionError("Retention evaluation time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if type(days) is not int or not 1 <= days <= 36500:
        raise RetentionError("Retention days must be between 1 and 36500")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise RetentionError("Retention batch limit must be between 1 and 1000")
    return now, now - timedelta(days=days)


def _expiry_clause(table, *, now, cutoff):
    # Explicit per-record retention wins.  Imported rows without an explicit
    # deadline use the same created-at fallback as the JSON policy.
    if "retention_until" in table.c:
        return sa.or_(
            table.c.retention_until <= now,
            sa.and_(table.c.retention_until.is_(None), table.c.created_at <= cutoff),
        )
    return table.c.created_at <= cutoff


def _archive_payload(row):
    raw_payload = row.get("payload")
    payload = deepcopy({} if raw_payload is None else raw_payload)
    if not isinstance(payload, dict):
        raise RetentionError("Archive payload is not an object")
    payload["source_purged"] = True
    return payload


def _unfinished(session, owner):
    return session.scalar(sa.select(sa.exists().where(
        AnalysisTask.owner_user_id == owner,
        AnalysisTask.status.not_in(TERMINAL_TASKS),
    )))


def _lock_owner(session, owner):
    # Analysis submission and all content writers lock the actor/owner first.
    # Taking the same lock here closes the race between the expiry scan and a
    # task that is accepted immediately after it.
    row = session.execute(sa.select(User.id).where(User.id == owner).with_for_update()).first()
    if row is None:
        raise RetentionError("Retention owner is missing")


def _queue_original(session, originals, source):
    key = source["object_key"]
    if not key:
        meta = (source["legacy_payload"] or {}).get("original")
        if isinstance(meta, dict) and meta.get("storage_key"):
            key = str(meta["storage_key"])
    if not key:
        return False
    try:
        originals._path(key)
    except (AttributeError, ValueError):
        raise RetentionError("Original storage key is invalid") from None
    meta = (source["legacy_payload"] or {}).get("original")
    if isinstance(meta, dict) and meta.get("storage_id") not in (None, str(originals.storage_id)):
        raise RetentionError("Original storage identity mismatch")
    session.execute(insert(original_gc).values(
        storage_id=originals.storage_id, object_key=key, owner_user_id=source["owner_user_id"]
    ).on_conflict_do_nothing())
    return True


def _mark_archives(session, *, owner, resume_id=None, job_id=None, now):
    # Keep the archive row and its analysis snapshot, including trashed or
    # previously deleted rows; the source-purged marker is the durable signal
    # that original text is no longer available.
    predicate = [Archive.owner_user_id == owner]
    if resume_id is not None:
        predicate.append(Archive.resume_id == resume_id)
    if job_id is not None:
        predicate.append(Archive.job_id == job_id)
    rows = session.execute(sa.select(Archive.__table__).where(*predicate)
                           .order_by(Archive.id).with_for_update()).mappings().all()
    for row in rows:
        session.execute(sa.update(Archive).where(Archive.id == row["id"]).values(
            payload=_archive_payload(row), updated_at=now))
    return len(rows)


def _purge_resume(session, originals, row, *, now, cutoff):
    owner = row["owner_user_id"]
    _lock_owner(session, owner)
    row = session.execute(sa.select(Resume.__table__).where(
        Resume.id == row["id"], Resume.deleted_at.is_(None),
        _expiry_clause(Resume.__table__, now=now, cutoff=cutoff)
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        return False, 0, False
    if _unfinished(session, owner):
        return False, 0, False
    queued = _queue_original(session, originals, row)
    marked = _mark_archives(session, owner=owner, resume_id=row["id"], now=now)
    session.execute(sa.update(Resume).where(Resume.id == row["id"]).values(
        raw_content="", processed_data={}, legacy_payload={}, object_key=None,
        content_sha256=None, source_filename=None, candidate_name=None,
        deleted_at=now, updated_at=now))
    append_audit(session, actor_user_id=None, action="retention.resume_purge",
                 target_type="resume", target_id=str(row["id"]), request_id=None,
                 metadata={"archives_marked": marked, "original_queued": int(queued)})
    return True, marked, queued


def _purge_job(session, row, *, now, cutoff):
    owner = row["owner_user_id"]
    _lock_owner(session, owner)
    row = session.execute(sa.select(Job.__table__).where(
        Job.id == row["id"], Job.deleted_at.is_(None),
        _expiry_clause(Job.__table__, now=now, cutoff=cutoff)
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        return False, 0
    if _unfinished(session, owner):
        return False, 0
    marked = _mark_archives(session, owner=owner, job_id=row["id"], now=now)
    session.execute(sa.update(Job).where(Job.id == row["id"]).values(
        raw_content="", parsed_requirements={}, legacy_payload={}, title=None,
        company_name=None, deleted_at=now, updated_at=now))
    append_audit(session, actor_user_id=None, action="retention.job_purge",
                 target_type="job", target_id=str(row["id"]), request_id=None,
                 metadata={"archives_marked": marked})
    return True, marked


def purge_expired(session, originals, *, now, days, limit=100):
    """Redact at most ``limit`` expired source rows in the caller's transaction.

    The caller must already be inside a transaction.  The operation is safe to
    retry: already deleted rows are excluded, archive markers are idempotent,
    and the physical unlink is delegated to the durable GC worker.
    """
    if not hasattr(session, "execute") or not session.in_transaction():
        raise RetentionError("Retention cleanup requires an open transaction")
    if not hasattr(originals, "storage_id") or not hasattr(originals, "_path"):
        raise RetentionError("An explicit original store is required")
    now, cutoff = _validate_policy(now, days, limit)
    total = {"resumes": 0, "jobs": 0, "archives_marked": 0, "originals_queued": 0,
             "skipped_unfinished": 0}

    resume_rows = session.execute(sa.select(Resume.id, Resume.owner_user_id).where(
        Resume.deleted_at.is_(None), _expiry_clause(Resume.__table__, now=now, cutoff=cutoff)
    ).order_by(Resume.created_at, Resume.id).limit(limit)).mappings().all()
    for row in resume_rows:
        changed, marked, queued = _purge_resume(session, originals, row, now=now, cutoff=cutoff)
        if not changed:
            total["skipped_unfinished"] += 1
            continue
        total["resumes"] += 1
        total["archives_marked"] += marked
        if queued:
            total["originals_queued"] += 1

    # ``limit`` bounds rows inspected across both source kinds.  A row skipped
    # because its owner has unfinished work still consumes a scan slot; this
    # prevents a batch from processing more than the declared limit by filling
    # skipped resume slots with jobs.
    remaining = max(0, limit - len(resume_rows))
    if remaining:
        job_rows = session.execute(sa.select(Job.id, Job.owner_user_id).where(
            Job.deleted_at.is_(None), _expiry_clause(Job.__table__, now=now, cutoff=cutoff)
        ).order_by(Job.created_at, Job.id).limit(remaining)).mappings().all()
        for row in job_rows:
            changed, marked = _purge_job(session, row, now=now, cutoff=cutoff)
            if not changed:
                total["skipped_unfinished"] += 1
                continue
            total["jobs"] += 1
            total["archives_marked"] += marked
    return total
