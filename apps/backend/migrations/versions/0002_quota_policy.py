"""Frozen opt-in quota policy, daily usage and reservation schema.

Does not replace the legacy quotas table or migrate JSON data.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_quota_policy"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quota_policies",
        sa.Column("scope_key", sa.String(36), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="inherit"),
        sa.Column("daily_limit", sa.Integer),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("quota_enabled", sa.Boolean),
        sa.Column("email_notify", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("(scope_key = 'global' AND user_id IS NULL AND quota_enabled IS NOT NULL) OR "
                           "(user_id IS NOT NULL AND scope_key = CAST(user_id AS VARCHAR) AND quota_enabled IS NULL)",
                           name="ck_quota_policy_scope"),
        sa.CheckConstraint("mode IN ('inherit', 'unlimited', 'limited')", name="ck_quota_policy_mode"),
        sa.CheckConstraint("(mode = 'limited' AND daily_limit IS NOT NULL AND daily_limit >= 0) OR "
                           "(mode <> 'limited' AND daily_limit IS NULL)", name="ck_quota_policy_limit"),
    )
    op.create_table(
        "quota_daily_usage",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("used >= 0 AND reserved >= 0", name="ck_quota_usage_nonnegative"),
    )
    op.create_table(
        "quota_reservations",
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True), unique=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id", "date"], ["quota_daily_usage.user_id", "quota_daily_usage.date"], ondelete="RESTRICT"),
        sa.CheckConstraint("count > 0", name="ck_quota_reservation_count"),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_quota_reservation_status"),
        sa.CheckConstraint("(status = 'pending' AND settled_at IS NULL) OR "
                           "(status <> 'pending' AND settled_at IS NOT NULL)", name="ck_quota_reservation_settled"),
    )
    op.create_index("ix_quota_reservations_pending", "quota_reservations", ["created_at"],
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade():
    op.drop_index("ix_quota_reservations_pending", table_name="quota_reservations")
    op.drop_table("quota_reservations")
    op.drop_table("quota_daily_usage")
    op.drop_table("quota_policies")
