"""Preserve audit actor identity and unique legacy source keys."""
from alembic import op
import sqlalchemy as sa

revision = '0004_audit_source'
down_revision = '0003_requirement_evidence'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('audit_logs', sa.Column('source_key', sa.String(512), nullable=True))
    op.add_column('audit_logs', sa.Column('actor_identity', sa.String(128), nullable=True))
    op.create_unique_constraint('uq_audit_logs_source_key', 'audit_logs', ['source_key'])


def downgrade():
    op.drop_constraint('uq_audit_logs_source_key', 'audit_logs', type_='unique')
    op.drop_column('audit_logs', 'actor_identity')
    op.drop_column('audit_logs', 'source_key')
