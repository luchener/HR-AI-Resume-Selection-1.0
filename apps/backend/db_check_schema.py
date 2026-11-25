"""Compare Alembic head schema with all target metadata, read-only."""
import os
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from db_models import Base
from db_quota import metadata as quota_metadata
from db_evidence import metadata as evidence_metadata
from db_security import migration_engine


def metadata():
    merged = sa.MetaData()
    for source in (Base.metadata, quota_metadata, evidence_metadata):
        for table in source.tables.values():
            if table.name not in merged.tables:
                table.to_metadata(merged)
    audit = merged.tables['audit_logs']
    audit.append_column(sa.Column('source_key', sa.String(512), nullable=True))
    audit.append_column(sa.Column('actor_identity', sa.String(128), nullable=True))
    audit.append_constraint(sa.UniqueConstraint('source_key', name='uq_audit_logs_source_key'))
    analyses = merged.tables['analyses']
    requirements = merged.tables['job_requirements']
    evidence = merged.tables['evidence_assessments']
    analyses.append_constraint(sa.UniqueConstraint('id', 'owner_user_id', 'job_id', name='uq_analysis_id_owner_job'))
    requirements.append_constraint(sa.UniqueConstraint('id', 'owner_user_id', 'job_id', name='uq_requirement_id_owner_job'))
    evidence.append_column(sa.Column('job_id', sa.Uuid(), nullable=False))
    evidence.append_constraint(sa.ForeignKeyConstraint(['analysis_id','owner_user_id','job_id'], ['analyses.id','analyses.owner_user_id','analyses.job_id'], ondelete='RESTRICT', name='fk_evidence_analysis_job'))
    evidence.append_constraint(sa.ForeignKeyConstraint(['requirement_id','owner_user_id','job_id'], ['job_requirements.id','job_requirements.owner_user_id','job_requirements.job_id'], ondelete='RESTRICT', name='fk_evidence_requirement_job'))
    # Request UUID/uniqueness constraints come from AnalysisTask (0008).
    # Do not append duplicate constraints here: merged starts with Base metadata.
    tasks = merged.tables['analysis_tasks']
    tasks.append_constraint(sa.UniqueConstraint('id', 'owner_user_id', 'job_id', name='uq_task_id_owner_job'))
    analyses.append_constraint(sa.ForeignKeyConstraint(['task_id', 'owner_user_id', 'job_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id', 'analysis_tasks.job_id'], ondelete='RESTRICT', name='fk_analysis_task_job'))
    merged.tables['quota_reservations'].append_constraint(sa.ForeignKeyConstraint(['task_id', 'user_id'], ['analysis_tasks.id', 'analysis_tasks.owner_user_id'], ondelete='RESTRICT', name='fk_quota_reservation_task_owner'))
    from db_task_lifecycle import task_batches
    task_batches.to_metadata(merged)
    from db_task_inputs import task_inputs
    task_inputs.to_metadata(merged)
    from db_task_events import task_events
    task_events.to_metadata(merged)
    from db_task_artifacts import artifacts
    artifacts.to_metadata(merged)
    from db_original_gc import original_gc
    original_gc.to_metadata(merged)
    from db_public_auth_state import TABLES
    for table in TABLES:
        table.to_metadata(merged)
    from db_invite_state import requests as invite_requests
    invite_requests.to_metadata(merged)
    from db_change_log import change_log
    change_log.to_metadata(merged)
    from db_announcement_state import announcements, dismissals
    announcements.to_metadata(merged)
    dismissals.to_metadata(merged)
    from db_email_state import email_images, email_broadcasts, email_recipients
    email_images.to_metadata(merged)
    email_broadcasts.to_metadata(merged)
    email_recipients.to_metadata(merged)
    return merged


def main():
    engine = migration_engine(os.environ['DATABASE_URL'])
    try:
        with engine.connect() as conn:
            differences = compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), metadata())
        print(f'Schema differences: {len(differences)}')
        for difference in differences:
            print(difference)
        return int(bool(differences))
    finally:
        engine.dispose()

if __name__ == '__main__':
    raise SystemExit(main())
