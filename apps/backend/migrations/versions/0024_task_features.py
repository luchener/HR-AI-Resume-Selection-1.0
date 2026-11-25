"""Persist task kind/search choice and durable Markdown improvement outputs."""
from alembic import op
import sqlalchemy as sa

revision = '0024_task_features'
down_revision = '0023_model_binding'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analysis_tasks', sa.Column('kind', sa.String(24), nullable=False, server_default='screening'))
    op.add_column('analysis_tasks', sa.Column('web_search', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.create_check_constraint('ck_analysis_tasks_kind', 'analysis_tasks', "kind IN ('screening', 'improvement') AND (kind = 'screening' OR (total_candidates = 1 AND web_search = false))")
    op.create_table('analysis_task_artifacts',
        sa.Column('task_id', sa.Uuid(), primary_key=True),
        sa.Column('owner_user_id', sa.Uuid(), nullable=False),
        sa.Column('resume_id', sa.Uuid(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('markdown', sa.Text()), sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['task_id', 'owner_user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_task_owner'),
        sa.ForeignKeyConstraint(['task_id', 'owner_user_id', 'job_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id', 'analysis_tasks.job_id'], ondelete='RESTRICT', name='fk_artifact_task_job'),
        sa.ForeignKeyConstraint(['resume_id', 'owner_user_id'], ['resumes.id', 'resumes.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_resume_owner'),
        sa.ForeignKeyConstraint(['job_id', 'owner_user_id'], ['jobs.id', 'jobs.owner_user_id'], ondelete='RESTRICT', name='fk_artifact_job_owner'),
        sa.CheckConstraint("(status = 'completed' AND markdown IS NOT NULL AND length(btrim(markdown)) > 0 AND octet_length(markdown) <= 262144 AND error_code IS NULL) OR (status = 'failed' AND markdown IS NULL AND error_code IS NOT NULL)", name='ck_task_artifact_result'))
    op.execute('''CREATE FUNCTION protect_task_options() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.kind IS DISTINCT FROM OLD.kind OR NEW.web_search IS DISTINCT FROM OLD.web_search THEN
        RAISE EXCEPTION 'Accepted task options are immutable';
      END IF;
      RETURN NEW;
    END $$''')
    op.execute('CREATE TRIGGER protect_task_options BEFORE UPDATE ON analysis_tasks FOR EACH ROW EXECUTE FUNCTION protect_task_options()')
    op.execute('REVOKE ALL ON FUNCTION protect_task_options() FROM PUBLIC')


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM analysis_task_artifacts) OR EXISTS(SELECT 1 FROM analysis_tasks WHERE kind <> 'screening' OR web_search)")):
        raise ValueError('Export/reconcile task features before downgrade')
    op.execute('DROP TRIGGER protect_task_options ON analysis_tasks')
    op.execute('DROP FUNCTION protect_task_options()')
    op.drop_table('analysis_task_artifacts')
    op.drop_constraint('ck_analysis_tasks_kind', 'analysis_tasks', type_='check')
    op.drop_column('analysis_tasks', 'web_search')
    op.drop_column('analysis_tasks', 'kind')
