"""Frozen dispatch inputs and a single durable worker claim for DB previews."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from db_models import AnalysisTask

metadata = sa.MetaData()
AnalysisTask.__table__.to_metadata(metadata)
# Copy FK targets for metadata resolution; only task_inputs is merged/created.
from db_models import Base
for name in ('users', 'jobs', 'resumes'):
    Base.metadata.tables[name].to_metadata(metadata)

task_inputs = sa.Table(
    'analysis_task_inputs', metadata,
    sa.Column('task_id', sa.Uuid(), primary_key=True),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('password_version', sa.Integer(), nullable=False),
    sa.Column('request_digest', sa.String(64), nullable=False),
    sa.Column('snapshot', JSONB(), nullable=False),
    sa.Column('worker_token', sa.Uuid()),
    sa.Column('lease_until', sa.DateTime(timezone=True)),
    sa.Column('model_binding', JSONB()),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id'],
                            ['analysis_tasks.id', 'analysis_tasks.owner_user_id'],
                            ondelete='CASCADE', name='fk_task_inputs_owner'),
    sa.CheckConstraint('password_version > 0', name='ck_task_inputs_password_version'),
    sa.CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name='ck_task_inputs_digest'),
    sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name='ck_task_inputs_snapshot'),
    sa.CheckConstraint("model_binding IS NULL OR (jsonb_typeof(model_binding) = 'object' AND (model_binding->>'binding_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND (model_binding->>'config_revision') ~ '^[0-9a-f]{64}$' AND (model_binding->>'policy_revision') ~ '^[0-9a-f]{64}$' AND model_binding - 'binding_id' - 'config_revision' - 'policy_revision' = '{}'::jsonb) IS TRUE", name='ck_task_inputs_model_binding'),
    sa.Index('ix_task_inputs_lease_until', 'lease_until', 'task_id',
             postgresql_where=sa.text('lease_until IS NOT NULL')),
)
