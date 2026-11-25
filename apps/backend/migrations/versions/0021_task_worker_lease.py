"""Add expiring heartbeats for running analysis workers.

Lease expiry fences stale workers and settles committed progress. Recovery never
redispatches a task, because a provider call may have completed while its worker
was disconnected.
"""
from alembic import op
import sqlalchemy as sa

revision = '0021_task_worker_lease'
down_revision = '0020_change_log_insert_guard'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analysis_task_inputs',
                  sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_task_inputs_lease_until', 'analysis_task_inputs',
                    ['lease_until', 'task_id'],
                    postgresql_where=sa.text('lease_until IS NOT NULL'))


def downgrade():
    op.drop_index('ix_task_inputs_lease_until', table_name='analysis_task_inputs')
    op.drop_column('analysis_task_inputs', 'lease_until')
