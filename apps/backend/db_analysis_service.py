"""Atomic result acceptance for the opt-in PostgreSQL analysis path.

The caller supplies an explicit transaction and commits before acknowledging a
callback. LLM calls and other external effects must happen outside this function.
This module is not connected to the production JSON application.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa

from db_models import Analysis
from db_json_compare import json_equal
from db_repository import _uuid
from db_task_lifecycle import _TERMINAL, _require_tx, _task, record_batch, task_batches


def _validate_json(value):
    """Reject coercion, non-finite numbers and PostgreSQL-invalid text."""
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is str:
        if '\x00' not in value and not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            return
    elif type(value) is list:
        for item in value:
            _validate_json(item)
        return
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError('result JSON keys must be strings')
            _validate_json(key)
            _validate_json(item)
        return
    raise ValueError('result must contain valid JSON values and text')


def _projection(result, status):
    if status == 'failed':
        return {}
    score = result.get('final_score')
    if type(score) not in (int, float) or not 0 <= score <= 100 or not math.isfinite(score):
        raise ValueError('completed result requires a finite numeric final_score')
    numeric = Decimal(str(score))
    if not 0 <= numeric <= 100 or numeric != numeric.quantize(Decimal('0.01')):
        raise ValueError('final_score must be between 0 and 100 with at most two decimals')
    values = {'final_score': numeric}
    for source, column, limit in (
        ('fit_grade', 'fit_grade', 32), ('fit_tag', 'fit_tag', 64),
        ('recruitment_recommendation', 'recommendation', 64), ('summary', 'summary', None),
    ):
        value = result.get(source)
        if value is not None and (type(value) is not str or (limit and len(value) > limit)):
            raise ValueError(f'invalid result field: {source}')
        values[column] = value
    for field, expected in (('score_breakdown', dict), ('scoring_meta', dict),
                            ('agent_validation', dict), ('evidence_assessments', list)):
        value = result.get(field, expected())
        if type(value) is not expected:
            raise ValueError(f'invalid result field: {field}')
        values[field] = value
    return values


def _outcomes(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError('outcomes must be a nonempty list or tuple')
    normalized = []
    seen = set()
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {'resume_id', 'status', 'result'}:
            raise ValueError('each outcome requires only resume_id, status and result')
        resume_id = _uuid(value['resume_id'], 'resume_id')
        if resume_id in seen:
            raise ValueError('duplicate resume in result batch')
        seen.add(resume_id)
        if value['status'] not in ('completed', 'failed') or type(value['result']) is not dict:
            raise ValueError('outcome requires completed/failed status and a result object')
        try:
            _validate_json(value['result'])
        except (RecursionError, OverflowError):
            raise ValueError('invalid or excessively nested result JSON') from None
        normalized.append({'resume_id': str(resume_id), 'status': value['status'], 'result': value['result']})
    normalized.sort(key=lambda item: item['resume_id'])
    # Snapshot caller-owned objects so projection, digest and persisted JSON agree.
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf8')
    snapshot = json.loads(encoded)
    for item in snapshot:
        _projection(item['result'], item['status'])
    return snapshot, hashlib.sha256(encoded).hexdigest()


def _check_result_counts(conn, task):
    table = Analysis.__table__
    counts = dict(conn.execute(sa.select(table.c.status, sa.func.count()).where(
        table.c.task_id == task['id'], table.c.owner_user_id == task['owner_user_id']
    ).group_by(table.c.status)).all())
    if (set(counts) - {'completed', 'failed'} or
            counts.get('completed', 0) != task['completed_candidates'] or
            counts.get('failed', 0) != task['failed_candidates']):
        raise ValueError('task result rows and progress are inconsistent; reconcile before accepting results')


def write_analysis_batch(conn, *, task_id: UUID, owner_user_id: UUID, batch_id: str,
                         outcomes, worker_token: UUID | None = None) -> dict:
    """Persist normalized HR reports/failures, progress and quota as one unit.

    Each outcome is {resume_id, status: completed|failed, result: JSON object}.
    Completed results carry the normalized HR report, including final_score;
    failures carry structured error information. The complete report is retained
    in analyses.analysis_result, with searchable fields projected separately.

    An identical batch (regardless of outcome/key order) returns existing rows.
    Changing content under a batch ID or repeating a candidate in a new batch
    is rejected. The nested transaction rolls back ALL this function's writes
    even if the caller catches an error and commits unrelated outer work.
    Count-only legacy batches cannot be reinterpreted as certified results.
    """
    _require_tx(conn)
    if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 128 or '\x00' in batch_id:
        raise ValueError('batch_id must be a nonempty bounded string')
    task_id = _uuid(task_id, 'task_id')
    owner_user_id = _uuid(owner_user_id)
    outcomes, digest = _outcomes(outcomes)
    with conn.begin_nested():
        task = _task(conn, task_id, owner_user_id)
        if task is None:
            raise KeyError(task_id)
        if task['kind'] != 'screening':
            raise ValueError('HR reports require a screening task')
        # Check the recovery fence before looking at old batch certificates.
        if task['error_code'] in {'operator_stopped_recovery', 'worker_lease_expired'}:
            raise ValueError('task was fenced by operator recovery; late results rejected')
        if task['job_id'] is None:
            raise ValueError('analysis result task requires a job')
        from db_task_inputs import task_inputs
        inputs = conn.execute(sa.select(task_inputs).where(
            task_inputs.c.task_id == task_id, task_inputs.c.owner_user_id == owner_user_id
        )).mappings().one_or_none()
        if inputs is not None:
            if worker_token is None or inputs['worker_token'] is None or _uuid(worker_token, 'worker_token') != inputs['worker_token']:
                raise ValueError('result writer does not own the task worker claim')
            lease_live = conn.scalar(sa.select(
                task_inputs.c.lease_until > sa.func.clock_timestamp()
            ).where(
                task_inputs.c.task_id == task_id,
                task_inputs.c.owner_user_id == owner_user_id,
                task_inputs.c.worker_token == _uuid(worker_token, 'worker_token'),
            ))
            # Completed tasks may replay their existing certified batches even
            # after the active lease has been cleared. record_batch rejects new
            # batches on terminal tasks; recovery fences are checked above.
            if task['status'] not in _TERMINAL and not lease_live:
                raise ValueError('result writer lease has expired')
            members = {item['resume_id'] for item in inputs['snapshot']['resumes']}
            if any(item['resume_id'] not in members for item in outcomes):
                raise ValueError('result candidate is not in the accepted task inputs')
        prior = conn.execute(sa.select(task_batches).where(
            task_batches.c.task_id == task_id, task_batches.c.batch_id == batch_id
        )).mappings().one_or_none()
        if prior is not None and prior['result_digest'] is None:
            raise ValueError('count-only batch has no certified result content')
        _check_result_counts(conn, task)
        completed = sum(item['status'] == 'completed' for item in outcomes)
        updated = record_batch(conn, task_id=task_id, owner_user_id=owner_user_id,
                               batch_id=batch_id, completed=completed,
                               failed=len(outcomes) - completed, result_digest=digest)
        table = Analysis.__table__
        if prior is None:
            for item in outcomes:
                # A unique task/resume violation must roll back progress/settlement,
                # never ON CONFLICT away a duplicate and still charge for it.
                conn.execute(table.insert().values(
                    id=uuid4(), task_id=task_id, owner_user_id=owner_user_id,
                    job_id=task['job_id'], resume_id=UUID(item['resume_id']),
                    status=item['status'], analysis_result=item['result'],
                    **_projection(item['result'], item['status'])))
        rows = conn.execute(sa.select(table).where(
            table.c.task_id == task_id, table.c.owner_user_id == owner_user_id,
            table.c.resume_id.in_([UUID(item['resume_id']) for item in outcomes])
        ).order_by(table.c.resume_id)).mappings().all()
        if len(rows) != len(outcomes):
            raise ValueError('accepted batch is missing its result rows')
        if prior is not None and any(
                row['resume_id'] != UUID(item['resume_id']) or row['status'] != item['status'] or
                not json_equal(row['analysis_result'], item['result'])
                for row, item in zip(rows, outcomes)):
            raise ValueError('accepted batch result content is inconsistent')
        return {'task': dict(updated), 'analyses': [dict(row) for row in rows],
                'duplicate': prior is not None}
