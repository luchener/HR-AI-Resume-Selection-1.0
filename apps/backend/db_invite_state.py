"""Encrypted invite applications; runtime schema independent of frozen migrations."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
sa.Table('users', metadata, sa.Column('id', sa.Uuid(), primary_key=True))
requests = sa.Table('invite_requests', metadata,
    sa.Column('id', sa.Uuid(), primary_key=True),
    sa.Column('email_hash', sa.String(64), nullable=False, index=True),
    sa.Column('sealed', JSONB(), nullable=False),
    sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column('reviewed_at', sa.DateTime(timezone=True)),
    sa.Column('reviewed_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
    sa.Column('invite_hash', sa.String(64)),
    sa.Column('code_sent', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.CheckConstraint("email_hash ~ '^[0-9a-f]{64}$' AND status IN ('pending','approved','rejected')",
                       name='ck_invite_request_values'),
    sa.CheckConstraint("invite_hash IS NULL OR invite_hash ~ '^[0-9a-f]{64}$'", name='ck_invite_request_hash'),
    sa.CheckConstraint("(status='pending' AND reviewed_at IS NULL AND reviewed_by IS NULL AND invite_hash IS NULL AND NOT code_sent) OR "
                       "(status='rejected' AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL AND invite_hash IS NULL AND NOT code_sent) OR "
                       "(status='approved' AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL AND invite_hash IS NOT NULL)",
                       name='ck_invite_request_lifecycle'))
sa.Index('uq_invite_request_pending_email', requests.c.email_hash, unique=True,
         postgresql_where=sa.text("status='pending'"))
