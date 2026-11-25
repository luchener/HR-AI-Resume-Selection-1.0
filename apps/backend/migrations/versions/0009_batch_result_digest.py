"""Bind accepted batches to their immutable result content.

Historical count-only batches remain NULL; their result identity is not inferred.
Downgrade discards certificates; re-upgrade cannot certify those old batches.
"""
from alembic import op
import sqlalchemy as sa

revision = '0009_batch_result_digest'
down_revision = '0008_task_request_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analysis_task_batches', sa.Column('result_digest', sa.String(64)))
    op.create_check_constraint('ck_task_batch_result_digest', 'analysis_task_batches',
                               "result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'")


def downgrade():
    op.drop_constraint('ck_task_batch_result_digest', 'analysis_task_batches', type_='check')
    op.drop_column('analysis_task_batches', 'result_digest')
