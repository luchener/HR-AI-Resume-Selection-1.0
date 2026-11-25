"""Frozen initial PostgreSQL schema; independent of application models.

Revision ID: 0001_initial_schema
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# Frozen metadata: do not replace this with imports from live db_models.
metadata = sa.MetaData()

legacy_records = sa.Table(
    'legacy_records', metadata,
    sa.Column('id', sa.BigInteger(), primary_key=True, nullable=False, autoincrement=True),
    sa.Column('root_name', sa.String(length=128), primary_key=False, nullable=False),
    sa.Column('record_key', sa.String(length=1024), primary_key=False, nullable=False),
    sa.Column('payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('imported_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.UniqueConstraint('root_name', 'record_key', name='uq_legacy_records_root_key'),
)
sa.Index('ix_legacy_records_root', legacy_records.c.root_name, unique=False, _table=legacy_records)

users = sa.Table(
    'users', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('username', sa.String(length=64), primary_key=False, nullable=False),
    sa.Column('email', sa.String(length=320), primary_key=False, nullable=True),
    sa.Column('password_hash', sa.Text(), primary_key=False, nullable=False),
    sa.Column('password_salt', sa.Text(), primary_key=False, nullable=False),
    sa.Column('password_version', sa.Integer(), primary_key=False, nullable=False, server_default='1'),
    sa.Column('is_admin', sa.Boolean(), primary_key=False, nullable=False, server_default=sa.text('false')),
    sa.Column('is_super_admin', sa.Boolean(), primary_key=False, nullable=False, server_default=sa.text('false')),
    sa.Column('status', sa.String(length=16), primary_key=False, nullable=False, server_default='active'),
    sa.Column('deleted_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('last_login_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('legacy_payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.CheckConstraint('password_version > 0', name='ck_users_password_version'),
    sa.CheckConstraint("status IN ('active', 'locked', 'deleted', 'pending')", name='ck_users_status'),
)
sa.Index('uq_users_email_lower', sa.text('lower(email)'), unique=True, _table=users, postgresql_where=sa.text("email IS NOT NULL AND email <> ''"))
sa.Index('uq_users_username_lower', sa.text('lower(username)'), unique=True, _table=users)

audit_logs = sa.Table(
    'audit_logs', metadata,
    sa.Column('id', sa.BigInteger(), primary_key=True, nullable=False, autoincrement=True),
    sa.Column('actor_user_id', UUID(), primary_key=False, nullable=True),
    sa.Column('action', sa.String(length=128), primary_key=False, nullable=False),
    sa.Column('target_type', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('target_id', sa.String(length=128), primary_key=False, nullable=True),
    sa.Column('request_id', sa.String(length=128), primary_key=False, nullable=True),
    sa.Column('ip_address', INET(), primary_key=False, nullable=True),
    sa.Column('user_agent', sa.Text(), primary_key=False, nullable=True),
    sa.Column('metadata', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
)
sa.Index('ix_audit_logs_actor_created', audit_logs.c.actor_user_id, audit_logs.c.created_at, unique=False, _table=audit_logs)

quotas = sa.Table(
    'quotas', metadata,
    sa.Column('id', sa.BigInteger(), primary_key=True, nullable=False, autoincrement=True),
    sa.Column('user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('quota_date', sa.Date(), primary_key=False, nullable=False),
    sa.Column('daily_limit', sa.Integer(), primary_key=False, nullable=False),
    sa.Column('used_count', sa.Integer(), primary_key=False, nullable=False, server_default='0'),
    sa.Column('analysis_enabled', sa.Boolean(), primary_key=False, nullable=False, server_default=sa.text('true')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.CheckConstraint('daily_limit >= 0 AND used_count >= 0', name='ck_quotas_nonnegative'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.UniqueConstraint('user_id', 'quota_date', name='uq_quotas_user_date'),
)

resumes = sa.Table(
    'resumes', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('owner_user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('candidate_name', sa.String(length=256), primary_key=False, nullable=True),
    sa.Column('source_filename', sa.String(length=512), primary_key=False, nullable=True),
    sa.Column('content_type', sa.String(length=32), primary_key=False, nullable=False, server_default='md'),
    sa.Column('content_sha256', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('object_key', sa.Text(), primary_key=False, nullable=True),
    sa.Column('raw_content', sa.Text(), primary_key=False, nullable=False, server_default=''),
    sa.Column('legacy_payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('processed_data', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('parse_status', sa.String(length=16), primary_key=False, nullable=False, server_default='parsed'),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('deleted_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('retention_until', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.CheckConstraint("parse_status IN ('pending', 'parsed', 'failed')", name='ck_resumes_parse_status'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.UniqueConstraint('id', 'owner_user_id', name='uq_resumes_id_owner'),
)
sa.Index('ix_resumes_content_sha256', resumes.c.content_sha256, unique=False, _table=resumes)
sa.Index('ix_resumes_owner_created', resumes.c.owner_user_id, resumes.c.created_at, unique=False, _table=resumes)

system_configs = sa.Table(
    'system_configs', metadata,
    sa.Column('key', sa.String(length=128), primary_key=True, nullable=False),
    sa.Column('value', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('updated_by', UUID(), primary_key=False, nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
)

jobs = sa.Table(
    'jobs', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('owner_user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('source_resume_id', UUID(), primary_key=False, nullable=True),
    sa.Column('title', sa.String(length=512), primary_key=False, nullable=True),
    sa.Column('company_name', sa.String(length=512), primary_key=False, nullable=True),
    sa.Column('raw_content', sa.Text(), primary_key=False, nullable=False, server_default=''),
    sa.Column('legacy_payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('parsed_requirements', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('deleted_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_resume_id'], ['resumes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_resume_id', 'owner_user_id'], ['resumes.id', 'resumes.owner_user_id'], name='fk_jobs_source_resume_owner'),
    sa.UniqueConstraint('id', 'owner_user_id', name='uq_jobs_id_owner'),
)
sa.Index('ix_jobs_owner_created', jobs.c.owner_user_id, jobs.c.created_at, unique=False, _table=jobs)

analysis_tasks = sa.Table(
    'analysis_tasks', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('owner_user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('job_id', UUID(), primary_key=False, nullable=True),
    sa.Column('status', sa.String(length=24), primary_key=False, nullable=False, server_default='pending'),
    sa.Column('total_candidates', sa.Integer(), primary_key=False, nullable=False, server_default='0'),
    sa.Column('completed_candidates', sa.Integer(), primary_key=False, nullable=False, server_default='0'),
    sa.Column('failed_candidates', sa.Integer(), primary_key=False, nullable=False, server_default='0'),
    sa.Column('request_id', sa.String(length=128), primary_key=False, nullable=True),
    sa.Column('error_code', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('error_message', sa.Text(), primary_key=False, nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.CheckConstraint('total_candidates >= 0 AND completed_candidates >= 0 AND failed_candidates >= 0 AND completed_candidates + failed_candidates <= total_candidates', name='ck_analysis_tasks_counts'),
    sa.CheckConstraint("status IN ('pending', 'running', 'partial_success', 'completed', 'failed', 'cancelled')", name='ck_analysis_tasks_status'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['job_id', 'owner_user_id'], ['jobs.id', 'jobs.owner_user_id'], name='fk_analysis_tasks_job_owner'),
    sa.UniqueConstraint('id', 'owner_user_id', name='uq_analysis_tasks_id_owner'),
)
sa.Index('ix_analysis_tasks_owner_created', analysis_tasks.c.owner_user_id, analysis_tasks.c.created_at, unique=False, _table=analysis_tasks)

analyses = sa.Table(
    'analyses', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('task_id', UUID(), primary_key=False, nullable=True),
    sa.Column('resume_id', UUID(), primary_key=False, nullable=False),
    sa.Column('job_id', UUID(), primary_key=False, nullable=False),
    sa.Column('owner_user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('status', sa.String(length=24), primary_key=False, nullable=False, server_default='completed'),
    sa.Column('final_score', sa.Numeric(precision=5, scale=2), primary_key=False, nullable=True),
    sa.Column('fit_grade', sa.String(length=32), primary_key=False, nullable=True),
    sa.Column('fit_tag', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('recommendation', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('summary', sa.Text(), primary_key=False, nullable=True),
    sa.Column('score_breakdown', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('scoring_meta', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('evidence_assessments', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column('agent_validation', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('analysis_result', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('model_name', sa.String(length=128), primary_key=False, nullable=True),
    sa.Column('model_config_hash', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('pipeline_version', sa.String(length=64), primary_key=False, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.CheckConstraint('final_score IS NULL OR (final_score >= 0 AND final_score <= 100)', name='ck_analyses_final_score'),
    sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name='ck_analyses_status'),
    sa.ForeignKeyConstraint(['task_id'], ['analysis_tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id', 'owner_user_id'], ['jobs.id', 'jobs.owner_user_id'], name='fk_analyses_job_owner'),
    sa.ForeignKeyConstraint(['resume_id', 'owner_user_id'], ['resumes.id', 'resumes.owner_user_id'], name='fk_analyses_resume_owner'),
    sa.ForeignKeyConstraint(['task_id', 'owner_user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], name='fk_analyses_task_owner'),
    sa.UniqueConstraint('id', 'owner_user_id', name='uq_analyses_id_owner'),
    sa.UniqueConstraint('task_id', 'resume_id', name='uq_analyses_task_resume'),
)
sa.Index('ix_analyses_owner_created', analyses.c.owner_user_id, analyses.c.created_at, unique=False, _table=analyses)

archives = sa.Table(
    'archives', metadata,
    sa.Column('id', UUID(), primary_key=True, nullable=False),
    sa.Column('owner_user_id', UUID(), primary_key=False, nullable=False),
    sa.Column('resume_id', UUID(), primary_key=False, nullable=True),
    sa.Column('job_id', UUID(), primary_key=False, nullable=True),
    sa.Column('analysis_id', UUID(), primary_key=False, nullable=True),
    sa.Column('candidate_name', sa.String(length=256), primary_key=False, nullable=True),
    sa.Column('category', sa.String(length=128), primary_key=False, nullable=True),
    sa.Column('tags', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column('status', sa.String(length=16), primary_key=False, nullable=False, server_default='active'),
    sa.Column('note', sa.Text(), primary_key=False, nullable=True),
    sa.Column('legacy_payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('payload', JSONB(), primary_key=False, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False, server_default=sa.text('now()')),
    sa.Column('deleted_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.CheckConstraint("status IN ('active', 'trashed', 'deleted')", name='ck_archives_status'),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['analysis_id', 'owner_user_id'], ['analyses.id', 'analyses.owner_user_id'], name='fk_archives_analysis_owner'),
    sa.ForeignKeyConstraint(['job_id', 'owner_user_id'], ['jobs.id', 'jobs.owner_user_id'], name='fk_archives_job_owner'),
    sa.ForeignKeyConstraint(['resume_id', 'owner_user_id'], ['resumes.id', 'resumes.owner_user_id'], name='fk_archives_resume_owner'),
)
sa.Index('ix_archives_owner_status_created', archives.c.owner_user_id, archives.c.status, archives.c.created_at, unique=False, _table=archives)


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
