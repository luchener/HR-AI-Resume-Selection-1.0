"""Require explicit operator compensation before a dead event releases its stream."""
from alembic import op
import sqlalchemy as sa

revision = '0018_change_log_compensation'
down_revision = '0017_email_notifications'
branch_labels = None
depends_on = None


TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION change_log_guard_update() RETURNS trigger
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
      IF OLD.state IN ('delivered', 'compensated') AND NEW.state IS DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION 'terminal change-log state cannot change';
      END IF;
      IF OLD.state = 'dead' AND NEW.state NOT IN ('dead', 'compensated') THEN
        RAISE EXCEPTION 'dead change-log rows may only be compensated';
      END IF;
      IF NEW.state = 'compensated' AND OLD.state IS DISTINCT FROM 'dead' THEN
        RAISE EXCEPTION 'only dead change-log rows may be compensated';
      END IF;
      IF OLD.state = 'compensated'
         AND (NEW.compensated_at IS DISTINCT FROM OLD.compensated_at
              OR NEW.compensated_by IS DISTINCT FROM OLD.compensated_by) THEN
        RAISE EXCEPTION 'change-log compensation metadata is immutable';
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
      IF NEW.state = 'compensated'
         AND (NEW.compensated_at IS NULL OR NEW.compensated_by IS NULL) THEN
        RAISE EXCEPTION 'compensated change-log rows require operator metadata';
      END IF;
      IF NEW.state <> 'compensated'
         AND (NEW.compensated_at IS NOT NULL OR NEW.compensated_by IS NOT NULL) THEN
        RAISE EXCEPTION 'compensation metadata requires the compensated state';
      END IF;
      RETURN NEW;
    END $$;
"""


ORIGINAL_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION change_log_guard_update() RETURNS trigger
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
"""


def upgrade():
    op.add_column('change_log', sa.Column('compensated_at', sa.DateTime(timezone=True)))
    op.add_column('change_log', sa.Column('compensated_by', sa.String(128)))
    op.drop_constraint('ck_change_log_state', 'change_log', type_='check')
    op.create_check_constraint(
        'ck_change_log_state', 'change_log',
        "state IN ('pending','in_flight','delivered','dead','compensated')")
    op.create_check_constraint(
        'ck_change_log_compensation', 'change_log',
        "(state = 'compensated' AND compensated_at IS NOT NULL AND compensated_by IS NOT NULL) OR "
        "(state <> 'compensated' AND compensated_at IS NULL AND compensated_by IS NULL)")
    op.create_check_constraint(
        'ck_change_log_compensated_by', 'change_log',
        'compensated_by IS NULL OR length(btrim(compensated_by)) BETWEEN 1 AND 128')
    op.execute(sa.text(TRIGGER_FUNCTION))


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM change_log WHERE state = 'compensated')")):
        raise ValueError('Reconcile compensated change-log events before downgrade')
    op.execute(sa.text(ORIGINAL_TRIGGER_FUNCTION))
    op.drop_constraint('ck_change_log_compensated_by', 'change_log', type_='check')
    op.drop_constraint('ck_change_log_compensation', 'change_log', type_='check')
    op.drop_constraint('ck_change_log_state', 'change_log', type_='check')
    op.create_check_constraint(
        'ck_change_log_state', 'change_log',
        "state IN ('pending','in_flight','delivered','dead')")
    op.drop_column('change_log', 'compensated_by')
    op.drop_column('change_log', 'compensated_at')
