"""Opt-in announcement schema, separate from the frozen initial metadata."""
import sqlalchemy as sa

metadata = sa.MetaData()
sa.Table('users', metadata, sa.Column('id', sa.Uuid(), primary_key=True))

announcements = sa.Table('announcements', metadata,
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
                       name='ck_announcement_status'),
    sa.Index('ix_announcements_start', 'start_at', 'id'),
)

dismissals = sa.Table('announcement_dismissals', metadata,
    sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True),
    sa.Column('announcement_id', sa.Uuid(), sa.ForeignKey('announcements.id', ondelete='RESTRICT'), primary_key=True),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Index('ix_announcement_dismissals_announcement', 'announcement_id'),
)
