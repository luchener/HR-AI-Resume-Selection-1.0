"""Tie evidence to the same job as both analysis and requirement."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0005_evidence_job_scope'
down_revision = '0004_audit_source'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('evidence_assessments', sa.Column('job_id', UUID(), nullable=True))
    op.execute('UPDATE evidence_assessments e SET job_id = a.job_id FROM analyses a WHERE a.id = e.analysis_id')
    op.alter_column('evidence_assessments', 'job_id', nullable=False)
    op.create_unique_constraint('uq_analysis_id_owner_job', 'analyses', ['id','owner_user_id','job_id'])
    op.create_unique_constraint('uq_requirement_id_owner_job', 'job_requirements', ['id','owner_user_id','job_id'])
    op.create_foreign_key('fk_evidence_analysis_job', 'evidence_assessments', 'analyses', ['analysis_id','owner_user_id','job_id'], ['id','owner_user_id','job_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_evidence_requirement_job', 'evidence_assessments', 'job_requirements', ['requirement_id','owner_user_id','job_id'], ['id','owner_user_id','job_id'], ondelete='RESTRICT')


def downgrade():
    op.drop_constraint('fk_evidence_requirement_job','evidence_assessments',type_='foreignkey')
    op.drop_constraint('fk_evidence_analysis_job','evidence_assessments',type_='foreignkey')
    op.drop_constraint('uq_requirement_id_owner_job','job_requirements',type_='unique')
    op.drop_constraint('uq_analysis_id_owner_job','analyses',type_='unique')
    op.drop_column('evidence_assessments','job_id')
