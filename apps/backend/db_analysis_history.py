"""Read-only historical analysis queries for the opt-in PostgreSQL adapter.

The legacy application keeps analysis output in process/cache or archive JSON.
This module provides the SQL-side read contract without importing the legacy
application.  It never writes an analysis, quota, task, or archive row.  All
queries are tenant-scoped; administrator listing deliberately omits resume
source text and full result JSON until the detail endpoint is requested.
Cross-tenant detail access requires a superadministrator and a committed audit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import ceil
from typing import Mapping

import sqlalchemy as sa

from db_audit import append_audit
from db_models import Analysis, Job, Resume, User
from db_repository import _uuid
from db_security import literal_like


class AnalysisHistoryError(Exception):
    """Fixed, safe HTTP-facing error for the read-only history adapter."""

    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _page(value, name: str, *, default: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if type(value) not in (str, int) or (isinstance(value, str) and not value.isdecimal()):
        raise AnalysisHistoryError(f"Invalid {name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AnalysisHistoryError(f"Invalid {name}") from None
    if parsed < 1 or parsed > maximum:
        raise AnalysisHistoryError(f"Invalid {name}")
    return parsed


def _optional_uuid(value, name: str):
    if value in (None, ""):
        return None
    try:
        return _uuid(value, name)
    except (TypeError, ValueError):
        raise AnalysisHistoryError(f"Invalid {name}") from None


def _safe_json(value):
    return deepcopy(value) if isinstance(value, (dict, list)) else {}


def _candidate_name(row: Mapping) -> str:
    result = row.get("analysis_result") if isinstance(row.get("analysis_result"), dict) else {}
    return str(row.get("candidate_name") or result.get("candidate_name") or "未提供")[:256]


def _job_title(row: Mapping) -> str:
    parsed = row.get("parsed_requirements") if isinstance(row.get("parsed_requirements"), dict) else {}
    return str(row.get("job_title") or parsed.get("job_title") or "")[:512]


def _summary(row: Mapping, *, include_result: bool = False) -> dict:
    item = {
        "analysis_id": str(row["analysis_id"]),
        "task_id": str(row["task_id"]) if row.get("task_id") else None,
        "resume_id": str(row["resume_id"]),
        "job_id": str(row["job_id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "status": row["status"],
        "candidate_name": _candidate_name(row),
        "job_title": _job_title(row),
        "final_score": float(row["final_score"]) if row.get("final_score") is not None else None,
        "fit_grade": row.get("fit_grade"),
        "fit_tag": row.get("fit_tag"),
        "recruitment_recommendation": row.get("recommendation"),
        "summary": row.get("summary"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }
    if row.get("owner_username") is not None:
        item["owner_username"] = str(row["owner_username"])
    if include_result:
        item["hr_analysis"] = _safe_json(row.get("analysis_result")) if row["status"] == "completed" else None
        item["score_breakdown"] = _safe_json(row.get("score_breakdown"))
        item["scoring_meta"] = _safe_json(row.get("scoring_meta"))
        item["evidence_assessments"] = _safe_json(row.get("evidence_assessments"))
        item["agent_validation"] = _safe_json(row.get("agent_validation"))
        item["model_name"] = row.get("model_name")
        item["pipeline_version"] = row.get("pipeline_version")
    return item


class AnalysisHistoryService:
    """Explicit-session read adapter for completed and failed analyses."""

    MAX_PAGE_SIZE = 100

    def __init__(self, session_factory, *, admin_emails=()):
        if not callable(session_factory):
            raise TypeError("session_factory must create a fresh Session")
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError("admin_emails must be an iterable of email addresses")
        self.sessions = session_factory
        self.admin_emails = frozenset(str(value).strip().lower() for value in admin_emails if str(value).strip())

    def _actor(self, session, actor_id, password_version, *, administrator=False):
        try:
            owner = _uuid(actor_id, "user_id")
        except (TypeError, ValueError):
            raise AnalysisHistoryError("Invalid or revoked token", 401) from None
        row = session.execute(sa.select(User.id, User.email, User.is_admin, User.status,
            User.deleted_at, User.password_version).where(User.id == owner).with_for_update()).mappings().one_or_none()
        if (row is None or row["status"] != "active" or row["deleted_at"] is not None
                or type(password_version) is not int or row["password_version"] != password_version):
            raise AnalysisHistoryError("Invalid or revoked token", 401)
        super_admin = bool(row["email"] and row["email"].strip().lower() in self.admin_emails)
        is_admin = bool(row["is_admin"] or super_admin)
        if administrator and not super_admin:
            raise AnalysisHistoryError("Superadministrator required", 403)
        return row, owner, is_admin, super_admin

    @staticmethod
    def _base_statement(*, owner=None, include_result=False):
        columns = [
            Analysis.id.label("analysis_id"), Analysis.task_id, Analysis.resume_id,
            Analysis.job_id, Analysis.owner_user_id, Analysis.status,
            Analysis.final_score, Analysis.fit_grade, Analysis.fit_tag,
            Analysis.recommendation, Analysis.summary,
            Analysis.created_at, Analysis.updated_at,
            sa.func.coalesce(sa.func.nullif(Resume.candidate_name, ''),
                Analysis.analysis_result['candidate_name'].astext, '未提供').label('candidate_name'),
            sa.func.coalesce(sa.func.nullif(Job.title, ''), Job.parsed_requirements['job_title'].astext, '').label('job_title'),
            User.username.label("owner_username"),
        ]
        if include_result:
            columns.extend([Analysis.score_breakdown, Analysis.scoring_meta,
                Analysis.evidence_assessments, Analysis.agent_validation, Analysis.analysis_result,
                Analysis.model_name, Analysis.pipeline_version])
        statement = sa.select(*columns).select_from(
            Analysis.__table__.join(Resume.__table__, sa.and_(
                Resume.id == Analysis.resume_id, Resume.owner_user_id == Analysis.owner_user_id,
            )).join(Job.__table__, sa.and_(
                Job.id == Analysis.job_id, Job.owner_user_id == Analysis.owner_user_id,
            )).join(User.__table__, User.id == Analysis.owner_user_id)
        )
        if owner is not None:
            statement = statement.where(Analysis.owner_user_id == owner)
        return statement

    def _run_list(self, session, statement, *, page, size):
        # Count and page share a statement snapshot even while another worker
        # commits a result. The outer join preserves totals beyond the last page.
        matches = statement.cte('history_matches')
        selected = sa.select(matches).order_by(matches.c.created_at.desc(), matches.c.analysis_id.desc())\
            .offset((page - 1) * size).limit(size).subquery('history_page')
        totals = sa.select(sa.func.count().label('history_total')).select_from(matches).subquery('history_count')
        rows = session.execute(sa.select(totals.c.history_total, *selected.c)
            .select_from(totals.outerjoin(selected, sa.true()))
            .order_by(selected.c.created_at.desc(), selected.c.analysis_id.desc())).mappings().all()
        total = rows[0]['history_total']
        return {"items": [_summary(row) for row in rows if row['analysis_id'] is not None], "page": page, "size": size,
                "total": int(total), "pages": int(ceil(total / size)) if total else 0}

    def list_user(self, actor_id, *, password_version, resume_id=None, job_id=None,
                  task_id=None, status=None, page=1, size=20):
        page = _page(page, "page", default=1, maximum=1000000)
        size = _page(size, "size", default=20, maximum=self.MAX_PAGE_SIZE)
        resume_id = _optional_uuid(resume_id, "resume_id")
        job_id = _optional_uuid(job_id, "job_id")
        task_id = _optional_uuid(task_id, "task_id")
        if status not in (None, "", "completed", "failed", "cancelled", "running", "pending"):
            raise AnalysisHistoryError("Invalid status")
        with self.sessions() as session, session.begin():
            _actor, owner, _is_admin, _super = self._actor(session, actor_id, password_version)
            statement = self._base_statement(owner=owner)
            if resume_id is not None:
                statement = statement.where(Analysis.resume_id == resume_id)
            if job_id is not None:
                statement = statement.where(Analysis.job_id == job_id)
            if task_id is not None:
                statement = statement.where(Analysis.task_id == task_id)
            if status:
                statement = statement.where(Analysis.status == status)
            return self._run_list(session, statement, page=page, size=size)

    def get_user(self, actor_id, analysis_id, *, password_version):
        identity = _optional_uuid(analysis_id, "analysis_id")
        if identity is None:
            raise AnalysisHistoryError("Invalid analysis_id")
        with self.sessions() as session, session.begin():
            _actor, owner, _is_admin, _super = self._actor(session, actor_id, password_version)
            row = session.execute(self._base_statement(owner=owner, include_result=True).where(Analysis.id == identity)).mappings().one_or_none()
            if row is None:
                raise AnalysisHistoryError("Analysis not found", 404)
            return _summary(row, include_result=True)

    def get_context(self, actor_id, *, password_version, resume_id=None, analysis_id=None,
                    analysis=None):
        """Return tenant-owned text for sanitizing/rendering in the helper routes."""
        resume_identity = _optional_uuid(resume_id, "resume_id")
        analysis_identity = _optional_uuid(analysis_id, "analysis_id")
        if resume_identity is None:
            raise AnalysisHistoryError("resume_id is required")
        with self.sessions() as session, session.begin():
            _actor, owner, _is_admin, _super = self._actor(session, actor_id, password_version)
            resume = session.execute(sa.select(Resume.__table__).where(
                Resume.id == resume_identity, Resume.owner_user_id == owner, Resume.deleted_at.is_(None)
            )).mappings().one_or_none()
            if resume is None:
                raise AnalysisHistoryError("Resume not found", 404)
            result = deepcopy(analysis) if type(analysis) is dict else None
            if analysis_identity is not None:
                row = session.execute(self._base_statement(owner=owner, include_result=True).where(
                    Analysis.id == analysis_identity, Analysis.resume_id == resume_identity
                )).mappings().one_or_none()
                if row is None:
                    raise AnalysisHistoryError("Analysis not found", 404)
                result = _safe_json(row.get("analysis_result")) if row["status"] == "completed" else {}
            return {"resume_id": str(resume_identity), "content": str(resume["raw_content"] or ""),
                    "processed": _safe_json(resume.get("processed_data")),
                    "candidate_name": str(resume.get("candidate_name") or "未提供"),
                    "analysis": result or {}}

    def list_admin(self, actor_id, *, password_version, owner_user_id=None, resume_id=None,
                   job_id=None, status=None, keyword="", page=1, size=20):
        page = _page(page, "page", default=1, maximum=1000000)
        size = _page(size, "size", default=20, maximum=self.MAX_PAGE_SIZE)
        owner_filter = _optional_uuid(owner_user_id, "owner_user_id")
        resume_filter = _optional_uuid(resume_id, "resume_id")
        job_filter = _optional_uuid(job_id, "job_id")
        if status not in (None, "", "completed", "failed", "cancelled", "running", "pending"):
            raise AnalysisHistoryError("Invalid status")
        if not isinstance(keyword, str) or len(keyword) > 200:
            raise AnalysisHistoryError("Invalid keyword")
        with self.sessions() as session, session.begin():
            _actor, _owner, _is_admin, _super = self._actor(session, actor_id, password_version, administrator=True)
            statement = self._base_statement()
            if owner_filter is not None:
                statement = statement.where(Analysis.owner_user_id == owner_filter)
            if resume_filter is not None:
                statement = statement.where(Analysis.resume_id == resume_filter)
            if job_filter is not None:
                statement = statement.where(Analysis.job_id == job_filter)
            if status:
                statement = statement.where(Analysis.status == status)
            clean = keyword.strip()
            if clean:
                pattern = literal_like(clean)
                statement = statement.where(sa.or_(
                    User.username.ilike(pattern, escape='\\'),
                    statement.selected_columns.candidate_name.ilike(pattern, escape='\\'),
                    statement.selected_columns.job_title.ilike(pattern, escape='\\')
                ))
            result = self._run_list(session, statement, page=page, size=size)
            # Admin list is intentionally summary-only.  It exposes owner identity
            # for operations but never returns source text, credentials, or JSON.
            return result

    def get_admin(self, actor_id, analysis_id, *, password_version, request_id):
        identity = _optional_uuid(analysis_id, "analysis_id")
        if identity is None:
            raise AnalysisHistoryError("Invalid analysis_id")
        with self.sessions() as session, session.begin():
            _actor, owner, _is_admin, _super = self._actor(session, actor_id, password_version, administrator=True)
            row = session.execute(self._base_statement(include_result=True).where(Analysis.id == identity)).mappings().one_or_none()
            if row is None:
                raise AnalysisHistoryError("Analysis not found", 404)
            result = _summary(row, include_result=True)
            append_audit(session, actor_user_id=owner, action='analysis.view', target_type='analysis',
                         target_id=str(identity), request_id=request_id)
        return result
