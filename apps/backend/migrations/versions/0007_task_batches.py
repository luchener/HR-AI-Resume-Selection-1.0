"""Persist task batch idempotency records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0007_task_batches'
down_revision = '0006_task_scope'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'analysis_task_batches',
        sa.Column('task_id', UUID(as_uuid=True), nullable=False),
        sa.Column('owner_user_id', UUID(as_uuid=True), nullable=False),
        sa.Column('batch_id', sa.String(128), nullable=False),
        sa.Column('completed', sa.Integer(), nullable=False),
        sa.Column('failed', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('task_id', 'batch_id'),
        sa.ForeignKeyConstraint(['task_id', 'owner_user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='CASCADE'),
        sa.CheckConstraint('completed >= 0 AND failed >= 0 AND completed + failed > 0', name='ck_task_batch_positive'),
    )


def downgrade():
    op.drop_table('analysis_task_batches')
