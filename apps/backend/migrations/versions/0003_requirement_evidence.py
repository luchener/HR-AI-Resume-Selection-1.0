"""Versioned requirement/evidence snapshots, four-state checks and tenant FKs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0003_requirement_evidence'
down_revision = '0002_quota_policy'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('job_requirements',
        sa.Column('id', UUID(), primary_key=True),
        sa.Column('job_id', UUID(), nullable=False),
        sa.Column('owner_user_id', UUID(), nullable=False),
        sa.Column('snapshot_version', sa.String(64), nullable=False),
        sa.Column('requirement_key', sa.String(128), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('category', sa.String(64)),
        sa.Column('dimension', sa.String(64)),
        sa.Column('hard_requirement', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('logic', sa.String(8), nullable=False, server_default='AND'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('payload', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("logic IN ('AND','OR')", name='ck_requirements_logic'),
        sa.UniqueConstraint('job_id','snapshot_version','requirement_key', name='uq_requirement_snapshot_key'),
        sa.UniqueConstraint('id','owner_user_id', name='uq_requirement_id_owner'),
        sa.ForeignKeyConstraint(['job_id','owner_user_id'], ['jobs.id','jobs.owner_user_id'], ondelete='RESTRICT', name='fk_requirement_job_owner'),
    )
    op.create_table('evidence_assessments',
        sa.Column('id', UUID(), primary_key=True),
        sa.Column('analysis_id', UUID(), nullable=False),
        sa.Column('requirement_id', UUID(), nullable=False),
        sa.Column('owner_user_id', UUID(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('effective_status', sa.String(24), nullable=False),
        sa.Column('reason', sa.Text()),
        sa.Column('source_quotes', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('payload', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('met','not_met','manual_review','not_mentioned')", name='ck_evidence_status'),
        sa.CheckConstraint("effective_status IN ('met','not_met','manual_review','not_mentioned')", name='ck_evidence_effective_status'),
        sa.UniqueConstraint('analysis_id','requirement_id', name='uq_evidence_analysis_requirement'),
        sa.ForeignKeyConstraint(['analysis_id','owner_user_id'], ['analyses.id','analyses.owner_user_id'], ondelete='RESTRICT', name='fk_evidence_analysis_owner'),
        sa.ForeignKeyConstraint(['requirement_id','owner_user_id'], ['job_requirements.id','job_requirements.owner_user_id'], ondelete='RESTRICT', name='fk_evidence_requirement_owner'),
    )
    op.create_index('ix_evidence_owner_analysis','evidence_assessments',['owner_user_id','analysis_id'])


def downgrade():
    op.drop_table('evidence_assessments')
    op.drop_table('job_requirements')
