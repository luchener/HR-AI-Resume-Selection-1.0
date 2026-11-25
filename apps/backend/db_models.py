"""PostgreSQL schema for the staged JSON-to-DB migration.

This module defines the target schema only. The production application continues
using JSON storage until a separately reviewed cutover is approved.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'locked', 'deleted', 'pending')", name="ck_users_status"),
        CheckConstraint("password_version > 0", name="ck_users_password_version"),
        CheckConstraint("frozen_by IS NULL OR frozen_by IN ('auto', 'admin')", name="ck_users_frozen_by"),
        CheckConstraint("deletion_previous_status IS NULL OR deletion_previous_status IN ('active', 'locked', 'pending')",
                        name="ck_users_deletion_previous_status"),
        CheckConstraint("login_consecutive_failures >= 0 AND login_window_failures >= 0", name="ck_users_login_failure_counts"),
        Index("uq_users_username_lower", func.lower(text("username")), unique=True),
        Index("uq_users_email_lower", func.lower(text("email")), unique=True,
              postgresql_where=text("email IS NOT NULL AND email <> ''")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_salt: Mapped[str] = mapped_column(Text, nullable=False)
    password_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_by: Mapped[str | None] = mapped_column(String(16))
    frozen_reason: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    deleted_by: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    deletion_previous_status: Mapped[str | None] = mapped_column(String(16))
    login_consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    login_window_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_resumes_id_owner"),
        CheckConstraint("parse_status IN ('pending', 'parsed', 'failed')", name="ck_resumes_parse_status"),
        Index("ix_resumes_owner_created", "owner_user_id", "created_at"),
        Index("ix_resumes_content_sha256", "content_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    candidate_name: Mapped[str | None] = mapped_column(String(256))
    source_filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="md")
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    legacy_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    processed_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="parsed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_jobs_id_owner"),
        ForeignKeyConstraint(["source_resume_id", "owner_user_id"], ["resumes.id", "resumes.owner_user_id"], name="fk_jobs_source_resume_owner"),
        Index("ix_jobs_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    source_resume_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(512))
    raw_content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    legacy_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    parsed_requirements: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_analysis_tasks_id_owner"),
        UniqueConstraint("request_id", name="uq_analysis_tasks_request_id"),
        CheckConstraint("request_id IS NULL OR request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'", name="ck_analysis_tasks_request_uuid"),
        ForeignKeyConstraint(["job_id", "owner_user_id"], ["jobs.id", "jobs.owner_user_id"], name="fk_analysis_tasks_job_owner"),
        CheckConstraint("status IN ('pending', 'running', 'partial_success', 'completed', 'failed', 'cancelled')", name="ck_analysis_tasks_status"),
        CheckConstraint("kind IN ('screening', 'improvement') AND (kind = 'screening' OR (total_candidates = 1 AND web_search = false))", name="ck_analysis_tasks_kind"),
        CheckConstraint("total_candidates >= 0 AND completed_candidates >= 0 AND failed_candidates >= 0 AND completed_candidates + failed_candidates <= total_candidates", name="ck_analysis_tasks_counts"),
        Index("ix_analysis_tasks_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, server_default="screening")
    web_search: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
    total_candidates: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completed_candidates: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_candidates: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    request_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint("task_id", "resume_id", name="uq_analyses_task_resume"),
        UniqueConstraint("id", "owner_user_id", name="uq_analyses_id_owner"),
        ForeignKeyConstraint(["task_id", "owner_user_id"], ["analysis_tasks.id", "analysis_tasks.owner_user_id"], name="fk_analyses_task_owner"),
        ForeignKeyConstraint(["resume_id", "owner_user_id"], ["resumes.id", "resumes.owner_user_id"], name="fk_analyses_resume_owner"),
        ForeignKeyConstraint(["job_id", "owner_user_id"], ["jobs.id", "jobs.owner_user_id"], name="fk_analyses_job_owner"),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name="ck_analyses_status"),
        CheckConstraint("final_score IS NULL OR (final_score >= 0 AND final_score <= 100)", name="ck_analyses_final_score"),
        Index("ix_analyses_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="SET NULL"))
    resume_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="RESTRICT"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="completed")
    final_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    fit_grade: Mapped[str | None] = mapped_column(String(32))
    fit_tag: Mapped[str | None] = mapped_column(String(64))
    recommendation: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    score_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    scoring_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    evidence_assessments: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    agent_validation: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    analysis_result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    model_name: Mapped[str | None] = mapped_column(String(128))
    model_config_hash: Mapped[str | None] = mapped_column(String(64))
    pipeline_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Archive(Base):
    __tablename__ = "archives"
    __table_args__ = (
        ForeignKeyConstraint(["resume_id", "owner_user_id"], ["resumes.id", "resumes.owner_user_id"], name="fk_archives_resume_owner"),
        ForeignKeyConstraint(["job_id", "owner_user_id"], ["jobs.id", "jobs.owner_user_id"], name="fk_archives_job_owner"),
        ForeignKeyConstraint(["analysis_id", "owner_user_id"], ["analyses.id", "analyses.owner_user_id"], name="fk_archives_analysis_owner"),
        CheckConstraint("status IN ('active', 'trashed', 'deleted')", name="ck_archives_status"),
        Index("ix_archives_owner_status_created", "owner_user_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="SET NULL"))
    candidate_name: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    note: Mapped[str | None] = mapped_column(Text)
    legacy_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Quota(Base):
    __tablename__ = "quotas"
    __table_args__ = (
        UniqueConstraint("user_id", "quota_date", name="uq_quotas_user_date"),
        CheckConstraint("daily_limit >= 0 AND used_count >= 0", name="ck_quotas_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    quota_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    analysis_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SystemConfig(Base):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegacyRecord(Base):
    __tablename__ = "legacy_records"
    __table_args__ = (
        UniqueConstraint("root_name", "record_key", name="uq_legacy_records_root_key"),
        Index("ix_legacy_records_root", "root_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    root_name: Mapped[str] = mapped_column(String(128), nullable=False)
    record_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
