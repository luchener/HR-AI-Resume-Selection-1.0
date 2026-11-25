"""Close the change-log state machine and timestamp invariants."""
from alembic import op
import sqlalchemy as sa

revision = "0019_change_log_state_machine"
down_revision = "0018_change_log_compensation"
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

      IF NEW.state IS DISTINCT FROM OLD.state
         AND NOT (
           (OLD.state = 'pending' AND NEW.state = 'in_flight')
           OR (OLD.state = 'in_flight' AND NEW.state IN ('pending', 'delivered', 'dead'))
           OR (OLD.state = 'dead' AND NEW.state = 'compensated')
         ) THEN
        RAISE EXCEPTION 'illegal change-log state transition';
      END IF;

      IF OLD.state IN ('delivered', 'compensated')
         AND (NEW.delivered_at IS DISTINCT FROM OLD.delivered_at
              OR NEW.compensated_at IS DISTINCT FROM OLD.compensated_at
              OR NEW.compensated_by IS DISTINCT FROM OLD.compensated_by
              OR NEW.available_at IS DISTINCT FROM OLD.available_at
              OR NEW.last_error_code IS DISTINCT FROM OLD.last_error_code
              OR NEW.updated_at IS DISTINCT FROM OLD.updated_at) THEN
        RAISE EXCEPTION 'terminal change-log rows are immutable';
      END IF;

      IF OLD.state = 'dead' AND NEW.state = 'dead'
         AND (NEW.available_at IS DISTINCT FROM OLD.available_at
              OR NEW.last_error_code IS DISTINCT FROM OLD.last_error_code
              OR NEW.updated_at IS DISTINCT FROM OLD.updated_at) THEN
        RAISE EXCEPTION 'dead change-log rows are immutable until compensation';
      END IF;

      IF OLD.state = 'dead' AND NEW.state = 'compensated'
         AND (NEW.available_at IS DISTINCT FROM OLD.available_at
              OR NEW.last_error_code IS DISTINCT FROM OLD.last_error_code) THEN
        RAISE EXCEPTION 'compensation cannot rewrite dead change-log history';
      END IF;

      IF (OLD.state = 'pending' AND NEW.state = 'in_flight')
         OR (OLD.state = 'in_flight' AND NEW.state = 'in_flight') THEN
        IF NEW.attempts <> OLD.attempts + 1
           OR (OLD.state = 'in_flight' AND (
             NEW.claimed_at IS NOT DISTINCT FROM OLD.claimed_at
             OR NEW.lease_until IS NOT DISTINCT FROM OLD.lease_until
           )) THEN
          RAISE EXCEPTION 'attempts may only increment once on a claim';
        END IF;
      ELSIF NEW.attempts IS DISTINCT FROM OLD.attempts THEN
        RAISE EXCEPTION 'attempts may only increment once on a claim';
      END IF;
      IF NEW.attempts > 100 THEN
        RAISE EXCEPTION 'change-log attempts cannot exceed the bound';
      END IF;

      IF NEW.state = 'in_flight' THEN
        IF NEW.claimed_by IS NULL OR NEW.claimed_at IS NULL OR NEW.lease_until IS NULL THEN
          RAISE EXCEPTION 'in-flight change-log rows require a lease';
        END IF;
      ELSIF NEW.claimed_by IS NOT NULL OR NEW.claimed_at IS NOT NULL OR NEW.lease_until IS NOT NULL THEN
        RAISE EXCEPTION 'lease fields require the in-flight state';
      END IF;

      IF NEW.state = 'pending' AND NEW.available_at IS NULL THEN
        RAISE EXCEPTION 'pending change-log rows require available_at';
      END IF;
      IF NEW.state = 'delivered' THEN
        IF NEW.delivered_at IS NULL THEN
          RAISE EXCEPTION 'delivered change-log rows require delivered_at';
        END IF;
      ELSIF NEW.delivered_at IS NOT NULL THEN
        RAISE EXCEPTION 'delivered_at requires the delivered state';
      END IF;

      IF NEW.state = 'compensated' THEN
        IF NEW.compensated_at IS NULL OR NEW.compensated_by IS NULL THEN
          RAISE EXCEPTION 'compensated change-log rows require operator metadata';
        END IF;
      ELSIF NEW.compensated_at IS NOT NULL OR NEW.compensated_by IS NOT NULL THEN
        RAISE EXCEPTION 'compensation metadata requires the compensated state';
      END IF;

      RETURN NEW;
    END $$;
"""


LEGACY_TRIGGER_FUNCTION = """
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


def upgrade():
    op.execute(sa.text(TRIGGER_FUNCTION))


def downgrade():
    op.execute(sa.text(LEGACY_TRIGGER_FUNCTION))
