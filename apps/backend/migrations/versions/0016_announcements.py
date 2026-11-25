"""Persist announcement schedules, soft deletion and per-user dismissals."""
from alembic import op
import sqlalchemy as sa

revision = '0016_announcements'
down_revision = '0015_change_log'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('announcements',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('title', sa.String(100), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('start_mode', sa.String(8), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='active'),
        sa.Column('created_by_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_by_name', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('cancelled_at', sa.DateTime(timezone=True)),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("id <> '00000000-0000-0000-0000-000000000000'::uuid", name='ck_announcement_id'),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 100 AND length(btrim(content)) BETWEEN 1 AND 5000",
                           name='ck_announcement_text'),
        sa.CheckConstraint("start_mode IN ('now','at','delay') AND end_at > start_at", name='ck_announcement_schedule'),
        sa.CheckConstraint("(status='active' AND cancelled_at IS NULL) OR (status='cancelled' AND cancelled_at IS NOT NULL)",
                           name='ck_announcement_status'))
    op.create_index('ix_announcements_start', 'announcements', ['start_at', 'id'])
    op.create_table('announcement_dismissals',
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('announcement_id', sa.Uuid(), sa.ForeignKey('announcements.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_announcement_dismissals_announcement', 'announcement_dismissals', ['announcement_id'])


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM announcements) OR EXISTS (SELECT 1 FROM announcement_dismissals)')):
        raise ValueError('Reconcile announcements and dismissals before downgrade')
    op.drop_table('announcement_dismissals')
    op.drop_table('announcements')
