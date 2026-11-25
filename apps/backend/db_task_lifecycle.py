"""Transactional analysis-task lifecycle for the opt-in PostgreSQL path.

Callers own the transaction and dispatch only after committing creation/start.
Every operation is tenant-scoped and locks the task before changing it. Quota
settlement is terminal and idempotent; partial success charges completed items.
This module is not imported by the legacy JSON application.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_models import AnalysisTask
from db_quota import quota_reservations, settle_partial

# Batch identities are persisted in the same transaction as result rows/counts.
task_batches = sa.Table(
    'analysis_task_batches', sa.MetaData(),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('batch_id', sa.String(128), nullable=False),
    sa.Column('completed', sa.Integer, nullable=False),
    sa.Column('failed', sa.Integer, nullable=False),
    sa.Column('result_digest', sa.String(64)),
    sa.PrimaryKeyConstraint('task_id', 'batch_id'),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id'],
                            ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='CASCADE'),
    sa.CheckConstraint('completed >= 0 AND failed >= 0 AND completed + failed > 0', name='ck_task_batch_positive'),
    sa.CheckConstraint("result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'", name='ck_task_batch_result_digest'),
)

_TERMINAL = {"completed", "failed", "cancelled", "partial_success"}
_SETTLE_ON_COMPLETE = _TERMINAL
_RECOVERY_FENCES = {"operator_stopped_recovery", "worker_lease_expired"}
WORKER_LEASE_SECONDS = 120
_TRANSITIONS = {
    "pending": {"running", "completed", "cancelled", "failed", "partial_success"},
    "running": {"partial_success", "completed", "failed", "cancelled"},
}


def _require_tx(conn):
    if conn.dialect.name != "postgresql" or not conn.in_transaction():
        raise RuntimeError("task operations require an explicit PostgreSQL transaction")


def _task(conn, task_id: UUID, owner_user_id: UUID, *, lock=True):
    statement = sa.select(AnalysisTask.__table__).where(
        AnalysisTask.id == task_id, AnalysisTask.owner_user_id == owner_user_id)
    if lock:
        statement = statement.with_for_update()
    return conn.execute(statement).mappings().one_or_none()


def _transition(current: str, target: str) -> None:
    if target != current and target not in _TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid task transition: {current} -> {target}")


def _reservation_for_task(conn, task_id, owner_user_id, request_id, *, total_candidates=None, require_pending=False):
    try:
        request_uuid = UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('task request_id must be a UUID')
    # Ownership fields are immutable in the quota API. Do NOT lock this row:
    # settle_partial must acquire usage before reservation, just like reserve.
    row = conn.execute(sa.select(quota_reservations).where(
        quota_reservations.c.request_id == request_uuid)).mappings().one_or_none()
    if row is None or row['task_id'] != task_id or row['user_id'] != owner_user_id:
        raise ValueError('task reservation ownership mismatch')
    if total_candidates is not None and row['count'] != total_candidates:
        raise ValueError('task reservation count mismatch')
    if require_pending and row['status'] != 'pending':
        raise ValueError('task reservation must be pending before dispatch/results')
    return row


def _pending_task_reservation(conn, row):
    if row['total_candidates']:
        if not row['request_id']:
            raise ValueError('nonempty task requires a quota request before dispatch/results')
        _reservation_for_task(conn, row['id'], row['owner_user_id'], row['request_id'],
                              total_candidates=row['total_candidates'], require_pending=True)


def create_task(conn, *, owner_user_id: UUID, job_id: UUID | None, total_candidates: int,
                request_id: str | None = None, task_id: UUID | None = None,
                kind='screening', web_search=False) -> Mapping:
    _require_tx(conn)
    if type(total_candidates) is not int or total_candidates < 0:
        raise ValueError("total_candidates must be a nonnegative integer")
    if kind not in {'screening', 'improvement'} or type(web_search) is not bool or (kind == 'improvement' and (total_candidates != 1 or web_search)):
        raise ValueError('Invalid task kind or search option')
    task_id = task_id or uuid4()
    if request_id is not None:
        try:
            request_id = str(UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('task request_id must be a UUID')
    values = dict(id=task_id, owner_user_id=owner_user_id, job_id=job_id,
                  kind=kind, web_search=web_search,
                  total_candidates=total_candidates, request_id=request_id,
                  status='completed' if total_candidates == 0 else 'pending')
    if total_candidates == 0:
        values['finished_at'] = sa.func.now()
    statement = insert(AnalysisTask.__table__).values(**values)
    if request_id is not None:
        statement = statement.on_conflict_do_nothing(constraint='uq_analysis_tasks_request_id')
    row = conn.execute(statement.returning(AnalysisTask.__table__)).mappings().one_or_none()
    if row is not None:
        return row
    # ON CONFLICT waits for concurrent creators; READ COMMITTED sees their row.
    existing = conn.execute(sa.select(AnalysisTask.__table__).where(
        AnalysisTask.request_id == request_id).with_for_update()).mappings().one()
    if (existing['owner_user_id'], existing['job_id'], existing['total_candidates'], existing['kind'], existing['web_search']) != (owner_user_id, job_id, total_candidates, kind, web_search):
        raise ValueError('request_id already belongs to a different task')
    return existing


def start_task(conn, *, task_id: UUID, owner_user_id: UUID) -> Mapping:
    _require_tx(conn)
    row = _task(conn, task_id, owner_user_id)
    if row is None:
        raise KeyError(task_id)
    if row['status'] in _TERMINAL:
        raise ValueError('task is already terminal')
    _pending_task_reservation(conn, row)
    if row["status"] == "running":
        return row
    _transition(row["status"], "running")
    return conn.execute(AnalysisTask.__table__.update().where(
        AnalysisTask.id == task_id, AnalysisTask.owner_user_id == owner_user_id
    ).values(status="running", started_at=sa.func.now()).returning(AnalysisTask.__table__)).mappings().one()


def record_batch(conn, *, task_id: UUID, owner_user_id: UUID, batch_id: str, completed: int = 0,
                 failed: int = 0, result_digest: str | None = None) -> Mapping:
    """Accept an idempotent result batch under the task lock.

    New result writers should use db_analysis_service.write_analysis_batch,
    which binds counts to result content and rolls back partial writes itself.
    This lower-level API retains count-only batches for lifecycle rehearsals.
    Call this BEFORE persisting result rows, in the SAME transaction. A rejected
    batch must roll back that entire transaction. A duplicate returns the existing
    counts; result rows must also use their own unique identities/on-conflict
    semantics so duplicate delivery never replaces an already accepted result.
    Counts cannot exceed total_candidates. Recovery rejects even old replays.
    """
    _require_tx(conn)
    if result_digest is not None:
        import re
        if not isinstance(result_digest, str) or not re.fullmatch('[0-9a-f]{64}', result_digest):
            raise ValueError('result_digest must be a SHA-256 hex digest')
    if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 128:
        raise ValueError('batch_id must be a nonempty bounded string')
    if type(completed) is not int or type(failed) is not int or completed < 0 or failed < 0 or not (completed or failed):
        raise ValueError("batch must contain a positive count of nonnegative integers")
    row = _task(conn, task_id, owner_user_id)
    if row is None:
        raise KeyError(task_id)
    if row.get('error_code') in _RECOVERY_FENCES:
        raise ValueError('task was fenced by operator recovery; late results rejected')
    prior = conn.execute(sa.select(task_batches).where(
        task_batches.c.task_id == task_id, task_batches.c.batch_id == batch_id
    ).with_for_update()).mappings().one_or_none()
    if prior is not None:
        if (prior['completed'], prior['failed']) != (completed, failed):
            raise ValueError('batch_id already belongs to different result counts')
        if prior['result_digest'] != result_digest:
            raise ValueError('batch_id already belongs to different result content')
        return row
    if row["status"] in _TERMINAL:
        raise ValueError("task is already terminal")
    _pending_task_reservation(conn, row)
    total_completed = row["completed_candidates"] + completed
    total_failed = row["failed_candidates"] + failed
    if total_completed + total_failed > row["total_candidates"]:
        raise ValueError("task result count exceeds total_candidates")
    status = "running"
    if total_completed and total_failed and total_completed + total_failed == row["total_candidates"]:
        status = "partial_success"
    elif total_failed and total_completed + total_failed == row["total_candidates"]:
        status = "failed"
    elif total_completed and total_completed + total_failed == row["total_candidates"]:
        status = "completed"
    _transition(row["status"], status)
    values = dict(completed_candidates=total_completed, failed_candidates=total_failed, status=status)
    if status in _SETTLE_ON_COMPLETE:
        values["finished_at"] = sa.func.now()
    conn.execute(task_batches.insert().values(task_id=task_id, owner_user_id=owner_user_id,
                                              batch_id=batch_id, completed=completed, failed=failed,
                                              result_digest=result_digest))
    updated = conn.execute(AnalysisTask.__table__.update().where(
        AnalysisTask.id == task_id, AnalysisTask.owner_user_id == owner_user_id
    ).values(**values).returning(AnalysisTask.__table__)).mappings().one()
    if status in _TERMINAL:
        from db_task_inputs import task_inputs
        conn.execute(task_inputs.update().where(
            task_inputs.c.task_id == task_id,
            task_inputs.c.owner_user_id == owner_user_id,
        ).values(lease_until=None))
    if status in _SETTLE_ON_COMPLETE and updated["request_id"]:
        _reservation_for_task(conn, task_id, owner_user_id, updated["request_id"], total_candidates=updated['total_candidates'])
        settle_partial(conn, UUID(str(updated["request_id"])), total_completed)
    return updated


def finish_task(conn, *, task_id: UUID, owner_user_id: UUID, succeeded: bool | None = None) -> Mapping:
    """Finish after all batches and settle its linked reservation exactly once."""
    _require_tx(conn)
    row = _task(conn, task_id, owner_user_id)
    if row is None:
        raise KeyError(task_id)
    if row["status"] in _TERMINAL:
        return row
    if row["completed_candidates"] + row["failed_candidates"] != row["total_candidates"]:
        raise ValueError("cannot finish an incomplete task")
    completed = row["completed_candidates"]
    if succeeded is not None and type(succeeded) is not bool:
        raise ValueError('succeeded must be boolean or None')
    derived = 'completed' if not row['failed_candidates'] else ('partial_success' if completed else 'failed')
    if succeeded is not None and succeeded != (derived == 'completed'):
        raise ValueError('succeeded conflicts with recorded task results')
    status = derived
    if row['total_candidates'] == 0:
        status = 'completed'
    _transition(row["status"], status)
    updated = conn.execute(AnalysisTask.__table__.update().where(
        AnalysisTask.id == task_id, AnalysisTask.owner_user_id == owner_user_id
    ).values(status=status, finished_at=sa.func.now()).returning(AnalysisTask.__table__)).mappings().one()
    if row["request_id"] and row['total_candidates']:
        _reservation_for_task(conn, task_id, owner_user_id, row["request_id"], total_candidates=row['total_candidates'])
        settle_partial(conn, UUID(str(row["request_id"])), completed)
    return updated


def renew_worker_lease(conn, *, task_id: UUID, owner_user_id: UUID,
                       worker_token: UUID,
                       lease_seconds: int = WORKER_LEASE_SECONDS) -> bool:
    """Extend a live worker claim; an expired token can never resurrect itself."""
    _require_tx(conn)
    if type(lease_seconds) is not int or not 3 <= lease_seconds <= 3600:
        raise ValueError('worker lease must be an integer from 3 to 3600 seconds')
    task = _task(conn, task_id, owner_user_id)
    if task is None or task['status'] != 'running' or task['error_code'] in _RECOVERY_FENCES:
        return False
    from db_task_inputs import task_inputs
    result = conn.execute(task_inputs.update().where(
        task_inputs.c.task_id == task_id,
        task_inputs.c.owner_user_id == owner_user_id,
        task_inputs.c.worker_token == worker_token,
        task_inputs.c.lease_until > sa.func.clock_timestamp(),
    ).values(lease_until=sa.func.clock_timestamp() + sa.text(
        f"INTERVAL '{lease_seconds} seconds'")))
    return result.rowcount == 1


def recover_expired_tasks(conn, *, limit: int = 100,
                          owner_user_id: UUID | None = None) -> int:
    """Fence expired running workers and settle only committed successes.

    Pending unclaimed tasks remain eligible for idempotent replay. Recovery is
    terminal and never redispatches: an expired lease cannot prove that an
    external provider call did not finish.
    """
    _require_tx(conn)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('recovery limit must be an integer from 1 to 1000')
    if owner_user_id is not None and not isinstance(owner_user_id, UUID):
        try:
            owner_user_id = UUID(str(owner_user_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('recovery owner must be a UUID') from None
    from db_task_inputs import task_inputs

    candidates = conn.execute(sa.select(task_inputs.c.task_id).join(
        AnalysisTask.__table__,
        sa.and_(AnalysisTask.id == task_inputs.c.task_id,
                AnalysisTask.owner_user_id == task_inputs.c.owner_user_id),
    ).where(
        AnalysisTask.status == 'running',
        task_inputs.c.worker_token.is_not(None),
        task_inputs.c.lease_until.is_not(None),
        task_inputs.c.lease_until < sa.func.statement_timestamp(),
        *([AnalysisTask.owner_user_id == owner_user_id] if owner_user_id is not None else []),
    ).order_by(task_inputs.c.lease_until,
               task_inputs.c.task_id).limit(limit)).scalars().all()
    if not candidates:
        return 0

    # Mutators share task -> task_inputs lock order. Recheck the lease after
    # locking, so a heartbeat that won the race remains authoritative.
    rows = conn.execute(sa.select(AnalysisTask.__table__).where(
        AnalysisTask.id.in_(candidates),
        AnalysisTask.status == 'running',
        *([AnalysisTask.owner_user_id == owner_user_id] if owner_user_id is not None else []),
    ).order_by(AnalysisTask.id).with_for_update(skip_locked=True)).mappings().all()
    recovered = 0
    for row in rows:
        input_row = conn.execute(sa.select(
            task_inputs.c.worker_token, task_inputs.c.lease_until,
        ).where(
            task_inputs.c.task_id == row['id'],
            task_inputs.c.owner_user_id == row['owner_user_id'],
        ).with_for_update()).mappings().one_or_none()
        if input_row is None:
            continue
        database_now = conn.scalar(sa.select(sa.func.clock_timestamp()))
        expired = (input_row['worker_token'] is not None
                   and input_row['lease_until'] is not None
                   and input_row['lease_until'] < database_now)
        if not expired:
            continue
        completed = row['completed_candidates']
        remaining = row['total_candidates'] - completed
        status = 'completed' if remaining == 0 else ('partial_success' if completed else 'failed')
        conn.execute(AnalysisTask.__table__.update().where(
            AnalysisTask.id == row['id'],
            AnalysisTask.owner_user_id == row['owner_user_id'],
        ).values(status=status, failed_candidates=remaining,
                 error_code='worker_lease_expired', finished_at=sa.func.now()))
        conn.execute(task_inputs.update().where(
            task_inputs.c.task_id == row['id'],
            task_inputs.c.owner_user_id == row['owner_user_id'],
        ).values(lease_until=None))
        if row['request_id'] and row['total_candidates']:
            _reservation_for_task(conn, row['id'], row['owner_user_id'], row['request_id'],
                                  total_candidates=row['total_candidates'])
            settle_partial(conn, UUID(str(row['request_id'])), completed)
        recovered += 1
    return recovered


def recover_stale_tasks(conn, *, before: datetime, confirmed_stopped_task_ids,
                        owner_user_id: UUID | None = None) -> int:
    """Fence explicitly confirmed stopped workers and charge committed successes.

    NOT an automatic timeout/lease mechanism. The operator must first stop and
    prevent redispatch of the specific workers, then supply their task UUIDs.
    A cutoff alone never authorizes recovery. Task row locks serialize recovery
    with result acceptance; the committed terminal state/error_code rejects late
    batches (including old batch replays) and start attempts. The result writer
    must lock/check this task BEFORE persisting results in the SAME transaction,
    and roll back the whole transaction on rejection; never catch-and-commit.
    External LLM effects are not undone and require operator reconciliation.
    """
    _require_tx(conn)
    if before.tzinfo is None:
        raise ValueError("recovery cutoff requires timezone")
    if not isinstance(confirmed_stopped_task_ids, (set, frozenset, list, tuple)) or not confirmed_stopped_task_ids:
        raise ValueError('explicit operator-confirmed stopped task UUIDs required')
    if any(not isinstance(value, UUID) for value in confirmed_stopped_task_ids):
        raise ValueError('confirmed stopped identities must be UUIDs')
    statement = sa.select(AnalysisTask.__table__).where(
        AnalysisTask.id.in_(confirmed_stopped_task_ids),
        AnalysisTask.status.in_(("pending", "running")),
        AnalysisTask.created_at < before.astimezone(timezone.utc))
    if owner_user_id is not None:
        statement = statement.where(AnalysisTask.owner_user_id == owner_user_id)
    rows = conn.execute(statement.order_by(AnalysisTask.id).with_for_update()).mappings().all()
    from db_task_inputs import task_inputs
    for row in rows:
        completed = row['completed_candidates']
        remaining = row['total_candidates'] - completed
        status = 'completed' if remaining == 0 else ('partial_success' if completed else 'failed')
        conn.execute(AnalysisTask.__table__.update().where(AnalysisTask.id == row["id"]).values(
            status=status, failed_candidates=remaining, error_code='operator_stopped_recovery', finished_at=sa.func.now()))
        conn.execute(task_inputs.update().where(
            task_inputs.c.task_id == row['id'],
            task_inputs.c.owner_user_id == row['owner_user_id'],
        ).values(lease_until=None))
        if row["request_id"] and row['total_candidates']:
            _reservation_for_task(conn, row['id'], row['owner_user_id'], row["request_id"], total_candidates=row['total_candidates'])
            settle_partial(conn, UUID(str(row["request_id"])), completed)
    return len(rows)
