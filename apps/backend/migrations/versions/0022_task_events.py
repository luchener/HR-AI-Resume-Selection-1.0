"""Persist bounded, owner-scoped analysis stage events."""
from alembic import op
import sqlalchemy as sa

revision = '0022_task_events'
down_revision = '0021_task_worker_lease'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('analysis_task_events',
        sa.Column('task_id', sa.Uuid(), primary_key=True),
        sa.Column('sequence', sa.Integer(), primary_key=True),
        sa.Column('owner_user_id', sa.Uuid(), nullable=False),
        sa.Column('resume_id', sa.Uuid(), nullable=False),
        sa.Column('stage', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['task_id', 'owner_user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'],
            ondelete='CASCADE', name='fk_task_events_owner'),
        sa.CheckConstraint('sequence BETWEEN 1 AND 256', name='ck_task_events_sequence'),
        sa.CheckConstraint("stage IN ('planning','retrieving','generating','retrying','web_search','candidate_completed','candidate_failed')", name='ck_task_events_stage'))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM analysis_task_events)')):
        raise ValueError('Export/reconcile task events before downgrade')
    op.drop_table('analysis_task_events')
