"""Runtime-only auth state; no legacy tokens, codes or secrets are imported."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
sa.Table('users', metadata, sa.Column('id', sa.Uuid(), primary_key=True))
limits = sa.Table('auth_limits', metadata,
    sa.Column('key', sa.String(64), primary_key=True),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, index=True),
    sa.CheckConstraint("key ~ '^[0-9a-f]{64}$' AND count > 0", name='ck_auth_limits_values'))
challenges = sa.Table('auth_challenges', metadata,
    sa.Column('purpose', sa.String(16), primary_key=True),
    sa.Column('subject', sa.String(64), primary_key=True),
    sa.Column('id', sa.Uuid(), nullable=False, unique=True),
    sa.Column('code_hash', sa.String(64), nullable=False),
    sa.Column('status', sa.String(16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
    sa.Column('password_version', sa.Integer()),
    sa.Column('invite_hash', sa.String(64)),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, index=True),
    sa.CheckConstraint("purpose IN ('register','reset','unfreeze','captcha')", name='ck_auth_challenge_purpose'),
    sa.CheckConstraint("status IN ('pending','sent','failed','used')", name='ck_auth_challenge_status'),
    sa.CheckConstraint("attempts BETWEEN 0 AND 5 AND subject ~ '^[0-9a-f]{64}$' AND code_hash ~ '^[0-9a-f]{64}$'",
                       name='ck_auth_challenge_values'))
invites = sa.Table('auth_invites', metadata,
    sa.Column('code_hash', sa.String(64), primary_key=True),
    sa.Column('email_hash', sa.String(64), nullable=False),
    sa.Column('created_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
    sa.Column('used_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column('revoked_at', sa.DateTime(timezone=True)),
    sa.Column('delivery_pending', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('details', JSONB()),
    sa.Column('request_id', sa.Uuid()),
    sa.CheckConstraint("code_hash ~ '^[0-9a-f]{64}$' AND email_hash ~ '^[0-9a-f]{64}$'", name='ck_auth_invite_values'))
login_state = sa.Table('auth_login_state', metadata,
    sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True),
    sa.Column('failure_times', JSONB(), nullable=False),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("jsonb_typeof(failure_times) = 'array' AND jsonb_array_length(failure_times) <= 64",
                       name='ck_auth_login_times'))
TABLES = (limits, challenges, invites, login_state)
