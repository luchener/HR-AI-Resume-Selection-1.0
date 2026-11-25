"""Add the opt-in transaction-owned change log for shadow delivery."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0015_change_log'
down_revision = '0014_invite_workflow'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'change_log',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('owner_user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('aggregate_type', sa.String(32), nullable=False),
        sa.Column('aggregate_id', sa.String(128), nullable=False),
        sa.Column('sequence_no', sa.BigInteger(), nullable=False),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('payload', JSONB(), nullable=False),
        sa.Column('payload_sha256', sa.String(64), nullable=False),
        sa.Column('state', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('claimed_by', sa.String(128)),
        sa.Column('claimed_at', sa.DateTime(timezone=True)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('delivered_at', sa.DateTime(timezone=True)),
        sa.Column('last_error_code', sa.String(32)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("aggregate_type IN ('user','resume','job','analysis_task','analysis','archive','quota','config','invite')", name='ck_change_log_aggregate_type'),
        sa.CheckConstraint("aggregate_id <> '' AND length(aggregate_id) <= 128", name='ck_change_log_aggregate_id'),
        sa.CheckConstraint("event_type ~ '^[a-z][a-z0-9_.-]{0,63}$'", name='ck_change_log_event_type'),
        sa.CheckConstraint("idempotency_key ~ '^[A-Za-z0-9:_./-]{1,128}$'", name='ck_change_log_idempotency'),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name='ck_change_log_payload_object'),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name='ck_change_log_payload_digest'),
        sa.CheckConstraint("state IN ('pending','in_flight','delivered','dead')", name='ck_change_log_state'),
        sa.CheckConstraint("attempts BETWEEN 0 AND 100", name='ck_change_log_attempts'),
        sa.CheckConstraint("last_error_code IS NULL OR last_error_code IN ('temporary','dependency_unavailable','timeout','conflict','unknown')", name='ck_change_log_error_code'),
        sa.UniqueConstraint('aggregate_type', 'aggregate_id', 'sequence_no', name='uq_change_log_stream_sequence'),
        sa.UniqueConstraint('idempotency_key', name='uq_change_log_idempotency_key'),
    )
    op.create_index('ix_change_log_claim', 'change_log', ['state', 'available_at', 'lease_until', 'created_at'])
    op.create_index('ix_change_log_stream', 'change_log', ['aggregate_type', 'aggregate_id', 'sequence_no'])
    op.execute(sa.text("""
        CREATE FUNCTION change_log_guard_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
             OR NEW.aggregate_type IS DISTINCT FROM OLD.aggregate_type
             OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
             OR NEW.sequence_no IS DISTINCT FROM OLD.sequence_no
             OR NEW.event_type IS DISTINCT FROM OLD.event_type
             OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
             OR NEW.payload IS DISTINCT FROM OLD.payload
             OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'change-log immutable fields cannot change';
          END IF;
          IF OLD.state IN ('delivered', 'dead') AND NEW.state IS DISTINCT FROM OLD.state THEN
            RAISE EXCEPTION 'terminal change-log state cannot change';
          END IF;
          IF NEW.attempts < OLD.attempts OR NEW.attempts > 100 THEN
            RAISE EXCEPTION 'change-log attempts cannot decrease or exceed the bound';
          END IF;
          IF NEW.state = 'in_flight' AND (NEW.claimed_by IS NULL OR NEW.lease_until IS NULL) THEN
            RAISE EXCEPTION 'in-flight change-log rows require a lease';
          END IF;
          IF NEW.state = 'delivered' AND NEW.delivered_at IS NULL THEN
            RAISE EXCEPTION 'delivered change-log rows require delivered_at';
          END IF;
          RETURN NEW;
        END $$;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER change_log_guard_update_trigger
        BEFORE UPDATE ON change_log
        FOR EACH ROW EXECUTE FUNCTION change_log_guard_update()
    """))


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM change_log WHERE state IN ('pending','in_flight'))")):
        raise ValueError('Drain pending change-log events before downgrade')
    op.execute(sa.text('DROP TRIGGER change_log_guard_update_trigger ON change_log'))
    op.execute(sa.text('DROP FUNCTION change_log_guard_update()'))
    op.drop_index('ix_change_log_stream', table_name='change_log')
    op.drop_index('ix_change_log_claim', table_name='change_log')
    op.drop_table('change_log')
