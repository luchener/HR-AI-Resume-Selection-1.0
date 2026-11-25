"""Bounded, tenant-owned stage log; no arbitrary provider/event text is stored."""
import sqlalchemy as sa
from db_task_inputs import metadata, task_inputs
from db_task_lifecycle import _RECOVERY_FENCES, _require_tx, _task

MESSAGES = {
    'planning': '正在提取岗位要求', 'retrieving': '正在查找相关经历',
    'generating': '正在生成分析报告', 'retrying': '正在修正报告',
    'web_search': '正在查询已授权的外部信息',
    'candidate_completed': '候选人分析已提交', 'candidate_failed': '候选人分析失败已提交',
}
task_events = sa.Table('analysis_task_events', metadata,
    sa.Column('task_id', sa.Uuid(), primary_key=True),
    sa.Column('sequence', sa.Integer(), primary_key=True),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('stage', sa.String(32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id'],
        ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='CASCADE', name='fk_task_events_owner'),
    sa.CheckConstraint('sequence BETWEEN 1 AND 256', name='ck_task_events_sequence'),
    sa.CheckConstraint("stage IN ('planning','retrieving','generating','retrying','web_search','candidate_completed','candidate_failed')",
                       name='ck_task_events_stage'))


def append_locked_event(conn, *, task_id, owner_user_id, resume_id, stage):
    """Internal writer: caller already holds task lock and validated claim."""
    _require_tx(conn)
    if stage not in MESSAGES:
        raise ValueError('Unsupported analysis stage')
    sequence = (conn.scalar(sa.select(sa.func.max(task_events.c.sequence)).where(
        task_events.c.task_id == task_id)) or 0) + 1
    if sequence > 256:
        raise ValueError('Task event budget exceeded')
    conn.execute(task_events.insert().values(task_id=task_id, owner_user_id=owner_user_id,
        resume_id=resume_id, sequence=sequence, stage=stage))
    return sequence


def append_worker_event(conn, *, task_id, owner_user_id, worker_token, resume_id, stage):
    _require_tx(conn)
    task = _task(conn, task_id, owner_user_id)
    if task is None or task['status'] != 'running' or task['error_code'] in _RECOVERY_FENCES:
        raise ValueError('Task is not running')
    inputs = conn.execute(sa.select(task_inputs).where(
        task_inputs.c.task_id == task_id, task_inputs.c.owner_user_id == owner_user_id,
        task_inputs.c.worker_token == worker_token,
        task_inputs.c.lease_until > sa.func.clock_timestamp(),
    )).mappings().one_or_none()
    if inputs is None or str(resume_id) not in {item['resume_id'] for item in inputs['snapshot']['resumes']}:
        raise ValueError('Event writer does not own a live claim/input')
    if stage in {'candidate_completed', 'candidate_failed'}:
        raise ValueError('Result stage requires committed results')
    return append_locked_event(conn, task_id=task_id, owner_user_id=owner_user_id,
                               resume_id=resume_id, stage=stage)
