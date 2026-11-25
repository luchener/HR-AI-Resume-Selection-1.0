"""Encrypted application workflow and revocable delivery-fenced invitations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0014_invite_workflow'
down_revision = '0013_public_auth'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('auth_invites', sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column('auth_invites', sa.Column('revoked_at', sa.DateTime(timezone=True)))
    op.add_column('auth_invites', sa.Column('delivery_pending', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('auth_invites', sa.Column('details', JSONB()))
    op.add_column('auth_invites', sa.Column('request_id', sa.Uuid()))
    op.create_table('invite_requests',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('email_hash', sa.String(64), nullable=False),
        sa.Column('sealed', JSONB(), nullable=False), sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('reviewed_at', sa.DateTime(timezone=True)),
        sa.Column('reviewed_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('invite_hash', sa.String(64)), sa.Column('code_sent', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("email_hash ~ '^[0-9a-f]{64}$' AND status IN ('pending','approved','rejected')", name='ck_invite_request_values'),
        sa.CheckConstraint("invite_hash IS NULL OR invite_hash ~ '^[0-9a-f]{64}$'", name='ck_invite_request_hash'),
        sa.CheckConstraint("(status='pending' AND reviewed_at IS NULL AND reviewed_by IS NULL AND invite_hash IS NULL AND NOT code_sent) OR "
            "(status='rejected' AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL AND invite_hash IS NULL AND NOT code_sent) OR "
            "(status='approved' AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL AND invite_hash IS NOT NULL)",
            name='ck_invite_request_lifecycle'))
    op.create_index('ix_invite_requests_email_hash', 'invite_requests', ['email_hash'])
    op.create_index('uq_invite_request_pending_email', 'invite_requests', ['email_hash'], unique=True,
                    postgresql_where=sa.text("status='pending'"))


def downgrade():
    bind = op.get_bind()
    if (bind.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM invite_requests)')) or
            bind.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM auth_invites WHERE details IS NOT NULL OR '
                                'revoked_at IS NOT NULL OR delivery_pending OR request_id IS NOT NULL)'))):
        raise ValueError('Reconcile invitation workflow before downgrade')
    op.drop_table('invite_requests')
    for column in ('request_id', 'details', 'delivery_pending', 'revoked_at', 'created_at'):
        op.drop_column('auth_invites', column)
