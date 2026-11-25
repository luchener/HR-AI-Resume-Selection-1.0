"""Immutable Markdown improvement outputs, committed with task/quota settlement."""
import sqlalchemy as sa
from db_models import Base
from db_repository import _uuid
from db_task_lifecycle import _require_tx, _task, record_batch, _RECOVERY_FENCES
from db_task_inputs import task_inputs

metadata = sa.MetaData()
MAX_MARKDOWN_BYTES = 262144
IMPROVEMENT_FAILURE_CODES = frozenset({
    'improvement_failed', 'improvement_truncated', 'improvement_timeout', 'invalid_improvement',
})


def validate_markdown(value):
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 and char not in '\t\r\n' for char in value)
            or len(value.encode('utf-8')) > MAX_MARKDOWN_BYTES):
        raise ValueError('Bounded Markdown result required')
    return value

for name in ('analysis_tasks', 'users', 'resumes', 'jobs'):
    Base.metadata.tables[name].to_metadata(metadata)
artifacts = sa.Table('analysis_task_artifacts', metadata,
    sa.Column('task_id', sa.Uuid(), primary_key=True),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(24), nullable=False),
    sa.Column('markdown', sa.Text()), sa.Column('error_code', sa.String(64)),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_task_owner'),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id', 'job_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id', 'analysis_tasks.job_id'], ondelete='RESTRICT', name='fk_artifact_task_job'),
    sa.ForeignKeyConstraint(['resume_id', 'owner_user_id'], ['resumes.id', 'resumes.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_resume_owner'),
    sa.ForeignKeyConstraint(['job_id', 'owner_user_id'], ['jobs.id', 'jobs.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_job_owner'),
    sa.CheckConstraint("(status = 'completed' AND markdown IS NOT NULL AND length(btrim(markdown)) > 0 AND octet_length(markdown) <= 262144 AND error_code IS NULL) OR (status = 'failed' AND markdown IS NULL AND error_code IS NOT NULL)", name='ck_task_artifact_result'))


def write_improvement(conn, *, task_id, owner_user_id, worker_token, resume_id, markdown=None, error_code=None):
    _require_tx(conn)
    if error_code is not None and (markdown is not None or error_code not in IMPROVEMENT_FAILURE_CODES):
        raise ValueError('Invalid improvement failure code')
    task_id, owner, resume_id, token = map(_uuid, (task_id, owner_user_id, resume_id, worker_token))
    task = _task(conn, task_id, owner)
    if task is None or task['kind'] != 'improvement' or task['total_candidates'] != 1:
        raise ValueError('Improvement task required')
    if task['status'] != 'running' or task['error_code'] in _RECOVERY_FENCES:
        raise ValueError('Improvement task is terminal or fenced')
    inputs = conn.execute(sa.select(task_inputs).where(task_inputs.c.task_id == task_id,
        task_inputs.c.owner_user_id == owner, task_inputs.c.worker_token == token,
        task_inputs.c.lease_until > sa.func.clock_timestamp())).mappings().one_or_none()
    if inputs is None or [item['resume_id'] for item in inputs['snapshot']['resumes']] != [str(resume_id)]:
        raise ValueError('Improvement worker claim or membership mismatch')
    if markdown is not None:
        validate_markdown(markdown)
    status = 'completed' if markdown is not None else 'failed'
    with conn.begin_nested():
        conn.execute(artifacts.insert().values(task_id=task_id, owner_user_id=owner, resume_id=resume_id,
            job_id=task['job_id'], status=status, markdown=markdown,
            error_code=None if markdown is not None else error_code or 'improvement_failed'))
        record_batch(conn, task_id=task_id, owner_user_id=owner, batch_id='improvement',
                     completed=int(markdown is not None), failed=int(markdown is None))
