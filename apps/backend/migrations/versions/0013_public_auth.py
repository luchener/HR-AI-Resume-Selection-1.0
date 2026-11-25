"""Runtime auth challenges, bound invitations and cross-process limits."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0013_public_auth'
down_revision = '0012_original_gc'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('auth_limits',
        sa.Column('key', sa.String(64), primary_key=True), sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("key ~ '^[0-9a-f]{64}$' AND count > 0", name='ck_auth_limits_values'))
    op.create_index('ix_auth_limits_expires_at', 'auth_limits', ['expires_at'])
    op.create_table('auth_challenges',
        sa.Column('purpose', sa.String(16), primary_key=True), sa.Column('subject', sa.String(64), primary_key=True),
        sa.Column('id', sa.Uuid(), nullable=False, unique=True), sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('password_version', sa.Integer()), sa.Column('invite_hash', sa.String(64)),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("purpose IN ('register','reset','unfreeze','captcha')", name='ck_auth_challenge_purpose'),
        sa.CheckConstraint("status IN ('pending','sent','failed','used')", name='ck_auth_challenge_status'),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5 AND subject ~ '^[0-9a-f]{64}$' AND code_hash ~ '^[0-9a-f]{64}$'",
                           name='ck_auth_challenge_values'))
    op.create_index('ix_auth_challenges_expires_at', 'auth_challenges', ['expires_at'])
    op.create_table('auth_invites',
        sa.Column('code_hash', sa.String(64), primary_key=True), sa.Column('email_hash', sa.String(64), nullable=False),
        sa.Column('created_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('used_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("code_hash ~ '^[0-9a-f]{64}$' AND email_hash ~ '^[0-9a-f]{64}$'", name='ck_auth_invite_values'))
    op.create_table('auth_login_state',
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('failure_times', JSONB(), nullable=False),
        sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("jsonb_typeof(failure_times) = 'array' AND jsonb_array_length(failure_times) <= 64",
                           name='ck_auth_login_times'))


def downgrade():
    bind = op.get_bind()
    # Expiring state is still security state; never discard it implicitly.
    for table in ('auth_limits', 'auth_challenges', 'auth_invites', 'auth_login_state'):
        if bind.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM ' + table + ')')):
            raise ValueError('Reconcile runtime auth state before downgrade')
    for table in ('auth_login_state', 'auth_invites', 'auth_challenges', 'auth_limits'):
        op.drop_table(table)
