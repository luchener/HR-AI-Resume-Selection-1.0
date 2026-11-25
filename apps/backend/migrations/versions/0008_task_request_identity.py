"""Canonical globally unique task request identities.

Invalid UUIDs or duplicate canonical identities intentionally abort upgrade;
operators must reconcile them rather than silently discard/merge tasks.
"""
from alembic import op
import sqlalchemy as sa

revision = '0008_task_request_identity'
down_revision = '0007_task_batches'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text('UPDATE analysis_tasks SET request_id = request_id::uuid::text WHERE request_id IS NOT NULL'))
    op.create_unique_constraint('uq_analysis_tasks_request_id', 'analysis_tasks', ['request_id'])
    op.create_check_constraint('ck_analysis_tasks_request_uuid', 'analysis_tasks',
                              "request_id IS NULL OR request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'")


def downgrade():
    op.drop_constraint('ck_analysis_tasks_request_uuid', 'analysis_tasks', type_='check')
    op.drop_constraint('uq_analysis_tasks_request_id', 'analysis_tasks', type_='unique')
