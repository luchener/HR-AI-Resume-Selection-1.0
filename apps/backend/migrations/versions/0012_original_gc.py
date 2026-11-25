"""Durable original cleanup, without changing historical source records."""
from alembic import op
import sqlalchemy as sa

revision = '0012_original_gc'
down_revision = '0011_account_lifecycle'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('original_gc',
        sa.Column('storage_id', sa.Uuid(), primary_key=True),
        sa.Column('object_key', sa.Text(), primary_key=True),
        sa.Column('owner_user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("storage_id <> '00000000-0000-0000-0000-000000000000'::uuid", name='ck_original_gc_storage'),
        sa.CheckConstraint("object_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.(pdf|docx)$'",
                           name='ck_original_gc_key'))


def downgrade():
    # Never silently discard outstanding physical deletion obligations.
    bind = op.get_bind()
    if bind.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM original_gc)')):
        raise ValueError('Drain original cleanup before downgrade')
    op.drop_table('original_gc')
