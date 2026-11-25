"""Persist administrator email assets, recipient snapshots and delivery state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0017_email_notifications'
down_revision = '0016_announcements'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'email_images',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('uploaded_by_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('original_name', sa.String(512), nullable=False),
        sa.Column('extension', sa.String(8), nullable=False),
        sa.Column('mime_type', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("extension IN ('png','jpg','jpeg','webp')", name='ck_email_images_extension'),
        sa.CheckConstraint('size_bytes > 0 AND size_bytes <= 2097152', name='ck_email_images_size'),
        sa.CheckConstraint('length(btrim(original_name)) BETWEEN 1 AND 512', name='ck_email_images_name'),
    )
    op.create_index('ix_email_images_created', 'email_images', ['created_at', 'id'])
    op.create_table(
        'email_broadcasts',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('idempotency_key', sa.String(128), nullable=False, unique=True),
        sa.Column('created_by_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_by_name', sa.String(64), nullable=False),
        sa.Column('subject', sa.String(200), nullable=False),
        sa.Column('template_id', sa.String(64), nullable=False),
        sa.Column('compose_snapshot', JSONB(), nullable=False),
        sa.Column('target', sa.String(16), nullable=False),
        sa.Column('target_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('skipped', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("target IN ('all','selected')", name='ck_email_broadcast_target'),
        sa.CheckConstraint('target_count >= 0 AND sent >= 0 AND skipped >= 0 AND failed >= 0 AND sent + skipped + failed <= target_count', name='ck_email_broadcast_counts'),
    )
    op.create_index('ix_email_broadcasts_created', 'email_broadcasts', ['created_at', 'id'])
    op.create_table(
        'email_broadcast_recipients',
        sa.Column('broadcast_id', sa.Uuid(), sa.ForeignKey('email_broadcasts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.String(200)),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending','sent','skipped','failed')", name='ck_email_recipient_status'),
        sa.CheckConstraint('attempts BETWEEN 0 AND 10', name='ck_email_recipient_attempts'),
    )
    op.create_index('ix_email_recipients_status', 'email_broadcast_recipients', ['broadcast_id', 'status'])


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM email_broadcast_recipients WHERE status = 'pending')")):
        raise ValueError('Drain pending email recipients before downgrade')
    op.drop_index('ix_email_recipients_status', table_name='email_broadcast_recipients')
    op.drop_table('email_broadcast_recipients')
    op.drop_index('ix_email_broadcasts_created', table_name='email_broadcasts')
    op.drop_table('email_broadcasts')
    op.drop_index('ix_email_images_created', table_name='email_images')
    op.drop_table('email_images')
