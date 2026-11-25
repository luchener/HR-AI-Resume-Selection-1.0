"""Opt-in PostgreSQL state for administrator email broadcasts.

The tables intentionally keep provider delivery outside the database transaction:
the broadcast and recipient snapshot are committed first, then a sender updates
each durable recipient result.  This makes retries and partial provider failure
observable without pretending SMTP is transactional.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))

email_images = sa.Table(
    "email_images", metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("uploaded_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("original_name", sa.String(512), nullable=False),
    sa.Column("extension", sa.String(8), nullable=False),
    sa.Column("mime_type", sa.String(64), nullable=False),
    sa.Column("content", sa.LargeBinary(), nullable=False),
    sa.Column("size_bytes", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("extension IN ('png','jpg','jpeg','webp')", name="ck_email_images_extension"),
    sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 2097152", name="ck_email_images_size"),
    sa.CheckConstraint("length(btrim(original_name)) BETWEEN 1 AND 512", name="ck_email_images_name"),
    sa.Index("ix_email_images_created", "created_at", "id"),
)

email_broadcasts = sa.Table(
    "email_broadcasts", metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
    sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("created_by_name", sa.String(64), nullable=False),
    sa.Column("subject", sa.String(200), nullable=False),
    sa.Column("template_id", sa.String(64), nullable=False),
    sa.Column("compose_snapshot", JSONB(), nullable=False),
    sa.Column("target", sa.String(16), nullable=False),
    sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("target IN ('all','selected')", name="ck_email_broadcast_target"),
    sa.CheckConstraint("target_count >= 0 AND sent >= 0 AND skipped >= 0 AND failed >= 0 AND sent + skipped + failed <= target_count", name="ck_email_broadcast_counts"),
    sa.Index("ix_email_broadcasts_created", "created_at", "id"),
)

email_recipients = sa.Table(
    "email_broadcast_recipients", metadata,
    sa.Column("broadcast_id", sa.Uuid(), sa.ForeignKey("email_broadcasts.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False),
    sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
    sa.Column("error_message", sa.String(200)),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("sent_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("status IN ('pending','sent','skipped','failed')", name="ck_email_recipient_status"),
    sa.CheckConstraint("attempts BETWEEN 0 AND 10", name="ck_email_recipient_attempts"),
    sa.Index("ix_email_recipients_status", "broadcast_id", "status"),
)

TABLES = (email_images, email_broadcasts, email_recipients)
