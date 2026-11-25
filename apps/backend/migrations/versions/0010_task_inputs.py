"""Freeze analysis inputs and persist an exclusive worker claim.

Existing tasks are not inferred/backfilled or automatically dispatched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0010_task_inputs'
down_revision = '0009_batch_result_digest'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'analysis_task_inputs',
        sa.Column('task_id', UUID(as_uuid=True), primary_key=True),
        sa.Column('owner_user_id', UUID(as_uuid=True), nullable=False),
        sa.Column('password_version', sa.Integer(), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('snapshot', JSONB(), nullable=False),
        sa.Column('worker_token', UUID(as_uuid=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['task_id', 'owner_user_id'],
                                ['analysis_tasks.id', 'analysis_tasks.owner_user_id'],
                                ondelete='CASCADE', name='fk_task_inputs_owner'),
        sa.CheckConstraint('password_version > 0', name='ck_task_inputs_password_version'),
        sa.CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name='ck_task_inputs_digest'),
        sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name='ck_task_inputs_snapshot'),
    )


def downgrade():
    op.drop_table('analysis_task_inputs')
