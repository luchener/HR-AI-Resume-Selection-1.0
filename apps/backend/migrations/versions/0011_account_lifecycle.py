"""Account freeze metadata and safe deletion/restore state.

Historical state is not guessed. Reimport a frozen source into a fresh target
to project its deletion/freeze metadata; no history or status is rewritten here.
"""
from alembic import op
import sqlalchemy as sa

revision = '0011_account_lifecycle'
down_revision = '0010_task_inputs'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('frozen_at', sa.DateTime(timezone=True)))
    op.add_column('users', sa.Column('frozen_by', sa.String(16)))
    op.add_column('users', sa.Column('frozen_reason', sa.String(512), nullable=False, server_default=''))
    op.add_column('users', sa.Column('deleted_by', sa.String(128), nullable=False, server_default=''))
    op.add_column('users', sa.Column('deletion_previous_status', sa.String(16)))
    op.add_column('users', sa.Column('login_consecutive_failures', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('login_window_failures', sa.Integer(), nullable=False, server_default='0'))
    op.create_check_constraint('ck_users_frozen_by', 'users', "frozen_by IS NULL OR frozen_by IN ('auto', 'admin')")
    op.create_check_constraint('ck_users_deletion_previous_status', 'users',
                               "deletion_previous_status IS NULL OR deletion_previous_status IN ('active', 'locked', 'pending')")
    op.create_check_constraint('ck_users_login_failure_counts', 'users', 'login_consecutive_failures >= 0 AND login_window_failures >= 0')


def downgrade():
    op.drop_constraint('ck_users_login_failure_counts', 'users', type_='check')
    op.drop_constraint('ck_users_deletion_previous_status', 'users', type_='check')
    op.drop_constraint('ck_users_frozen_by', 'users', type_='check')
    for column in ('login_window_failures', 'login_consecutive_failures', 'deletion_previous_status',
                   'deleted_by', 'frozen_reason', 'frozen_by', 'frozen_at'):
        op.drop_column('users', column)
