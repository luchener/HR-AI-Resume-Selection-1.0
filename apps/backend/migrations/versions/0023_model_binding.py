"""Record non-secret model/policy binding once for each claimed batch."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0023_model_binding'
down_revision = '0022_task_events'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analysis_task_inputs', sa.Column('model_binding', JSONB(), nullable=True))
    op.create_check_constraint('ck_task_inputs_model_binding', 'analysis_task_inputs',
        "model_binding IS NULL OR (jsonb_typeof(model_binding) = 'object' AND (model_binding->>'binding_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND (model_binding->>'config_revision') ~ '^[0-9a-f]{64}$' AND (model_binding->>'policy_revision') ~ '^[0-9a-f]{64}$' AND model_binding - 'binding_id' - 'config_revision' - 'policy_revision' = '{}'::jsonb) IS TRUE")
    op.execute("""CREATE FUNCTION model_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF current_user = 'resume_app' AND OLD.model_binding IS NOT NULL
         AND (TG_OP = 'DELETE' OR NEW.model_binding IS DISTINCT FROM OLD.model_binding) THEN
        RAISE EXCEPTION 'model binding is immutable';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER model_binding_immutable_trigger BEFORE UPDATE OR DELETE ON analysis_task_inputs FOR EACH ROW EXECUTE FUNCTION model_binding_immutable()')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM analysis_task_inputs WHERE model_binding IS NOT NULL)')):
        raise ValueError('Reconcile model bindings before downgrade')
    op.execute('DROP TRIGGER model_binding_immutable_trigger ON analysis_task_inputs')
    op.execute('DROP FUNCTION model_binding_immutable()')
    op.drop_constraint('ck_task_inputs_model_binding', 'analysis_task_inputs', type_='check')
    op.drop_column('analysis_task_inputs', 'model_binding')
