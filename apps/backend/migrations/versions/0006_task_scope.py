"""Bind quota reservations to task owner and analyses to task job."""
from alembic import op

revision = '0006_task_scope'
down_revision = '0005_evidence_job_scope'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint('uq_task_id_owner_job', 'analysis_tasks', ['id', 'owner_user_id', 'job_id'])
    op.create_foreign_key('fk_analysis_task_job', 'analyses', 'analysis_tasks', ['task_id', 'owner_user_id', 'job_id'], ['id', 'owner_user_id', 'job_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_quota_reservation_task_owner', 'quota_reservations', 'analysis_tasks', ['task_id', 'user_id'], ['id', 'owner_user_id'], ondelete='RESTRICT')


def downgrade():
    op.drop_constraint('fk_quota_reservation_task_owner', 'quota_reservations', type_='foreignkey')
    op.drop_constraint('fk_analysis_task_job', 'analyses', type_='foreignkey')
    op.drop_constraint('uq_task_id_owner_job', 'analysis_tasks', type_='unique')
