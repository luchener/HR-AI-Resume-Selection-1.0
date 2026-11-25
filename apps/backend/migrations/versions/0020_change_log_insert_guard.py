"""Require change-log rows to enter the stream in their initial state."""
from alembic import op
import sqlalchemy as sa

revision = "0020_change_log_insert_guard"
down_revision = "0019_change_log_state_machine"
branch_labels = None
depends_on = None


INSERT_GUARD_FUNCTION = """
    CREATE OR REPLACE FUNCTION change_log_guard_insert() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.state IS DISTINCT FROM 'pending'
         OR NEW.attempts IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION 'change-log rows must be inserted pending with zero attempts';
      END IF;

      IF NEW.available_at IS NULL
         OR NEW.claimed_by IS NOT NULL
         OR NEW.claimed_at IS NOT NULL
         OR NEW.lease_until IS NOT NULL
         OR NEW.delivered_at IS NOT NULL
         OR NEW.compensated_at IS NOT NULL
         OR NEW.compensated_by IS NOT NULL
         OR NEW.last_error_code IS NOT NULL THEN
        RAISE EXCEPTION 'change-log rows cannot have delivery metadata on insert';
      END IF;

      RETURN NEW;
    END $$;
"""


def upgrade():
    op.execute(sa.text(INSERT_GUARD_FUNCTION))
    op.execute(sa.text("""
        CREATE TRIGGER change_log_guard_insert_trigger
        BEFORE INSERT ON change_log
        FOR EACH ROW EXECUTE FUNCTION change_log_guard_insert()
    """))


def downgrade():
    op.execute(sa.text("DROP TRIGGER change_log_guard_insert_trigger ON change_log"))
    op.execute(sa.text("DROP FUNCTION change_log_guard_insert()"))
