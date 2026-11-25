"""Explicit PostgreSQL adapter for the remaining administrator read paths.

The service is deliberately opt-in.  It owns every transaction, re-checks the
administrator from the database, never reads legacy files, and keeps source
text out of list/audit responses.  Physical originals are represented by the
durable ``original_gc`` queue; an external collector removes them after the
database commit.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_audit import append_audit
from db_models import Analysis, Archive, AuditLog, Resume, User
from db_original_gc import original_gc
from db_repository import _uuid
from db_source_scope import has_unfinished_task_for_source
from resume_names import CANDIDATE_NAME_PLACEHOLDERS, resolve_candidate_name
from resume_sanitize import strip_residue_lines


class AdminServiceError(Exception):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail, self.status = detail, status


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _page(value, name, default, maximum):
    try:
        parsed = int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        raise AdminServiceError(f"Invalid {name}") from None
    if parsed < 1 or parsed > maximum:
        raise AdminServiceError(f"Invalid {name}")
    return parsed


def _like(value: str) -> str:
    # ``literal_like`` is intentionally not imported from a web-facing module:
    # this adapter accepts one bounded literal and escapes SQL wildcards here.
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _safe_original(row):
    meta = row.get("legacy_payload") if isinstance(row.get("legacy_payload"), dict) else {}
    original = meta.get("original") if isinstance(meta.get("original"), dict) else {}
    if not row.get("object_key") and not original:
        return {"available": False}
    ext = str(original.get("ext") or (str(row.get("object_key") or "").rsplit(".", 1)[-1] if row.get("object_key") else ""))
    return {"available": bool(row.get("object_key")), "ext": ext[:8],
            "name": str(original.get("name") or "")[:120],
            "bytes": int(original.get("bytes") or 0) if isinstance(original.get("bytes"), int) else 0,
            "sha256": str(original.get("sha256") or "")[:64],
            "stored_at": original.get("stored_at"), "renderable": ext == "pdf",
            "page_count": int(original.get("page_count") or 0) if isinstance(original.get("page_count"), int) else 0}


class AdminService:
    MAX_PAGE_SIZE = 200

    def __init__(self, session_factory, *, admin_emails=(), original_storage_id=None):
        if not callable(session_factory):
            raise TypeError("session_factory must create a fresh Session")
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError("admin_emails must be an iterable")
        self.sessions = session_factory
        self.admin_emails = frozenset(str(x).strip().lower() for x in admin_emails if str(x).strip())
        self.original_storage_id = UUID(str(original_storage_id)) if original_storage_id else None

    def _actor(self, session, actor_id, password_version):
        try:
            identity = _uuid(actor_id, "user_id")
        except (TypeError, ValueError):
            raise AdminServiceError("Invalid or revoked token", 401) from None
        row = session.execute(sa.select(User.__table__).where(User.id == identity).with_for_update()).mappings().one_or_none()
        if row is None or row["status"] != "active" or row["deleted_at"] is not None or type(password_version) is not int or row["password_version"] != password_version:
            raise AdminServiceError("Invalid or revoked token", 401)
        super_admin = bool(row["email"] and row["email"].strip().lower() in self.admin_emails)
        if not super_admin:
            raise AdminServiceError("仅超级管理员可访问", 403)
        return row, identity

    @staticmethod
    def _candidate_name(row):
        processed = row.get("processed_data") if isinstance(row.get("processed_data"), dict) else {}
        personal = processed.get("personal_data")
        personal_name = personal.get("name") if isinstance(personal, dict) else None
        return resolve_candidate_name(
            row.get("candidate_name"), processed.get("candidate_name"), personal_name,
            text=row.get("raw_content") or "", filename=row.get("source_filename") or "",
            fallback_names=(row.get("archive_name"), row.get("analysis_name")),
        ) or "未命名候选人"

    def _resume_query(self, *, owner_id=None, resume_id=None, keyword=""):
        def has_name(column):
            name = sa.func.lower(sa.func.btrim(column, " \t\r\n\f\v\u3000"))
            return name.not_in(("", *sorted(CANDIDATE_NAME_PLACEHOLDERS)))

        archive_name = sa.select(Archive.candidate_name).where(
            Archive.resume_id == Resume.id, Archive.owner_user_id == Resume.owner_user_id,
            Archive.status.in_(("active", "trashed")), has_name(Archive.candidate_name),
        ).order_by(Archive.status, Archive.created_at.desc(), Archive.id.desc()).limit(1).scalar_subquery()
        result_value = Analysis.analysis_result["candidate_name"]
        result_name = result_value.as_string()
        analysis_name = sa.select(result_name).where(
            Analysis.resume_id == Resume.id, Analysis.owner_user_id == Resume.owner_user_id,
            Analysis.status == "completed", sa.func.jsonb_typeof(result_value) == "string", has_name(result_name),
        ).order_by(Analysis.created_at.desc(), Analysis.id.desc()).limit(1).scalar_subquery()
        statement = sa.select(Resume.__table__, User.username.label("owner_username"), User.email.label("owner_email"),
                              archive_name.label("archive_name"), analysis_name.label("analysis_name"))\
            .select_from(Resume.__table__.join(User.__table__, User.id == Resume.owner_user_id))\
            .where(Resume.deleted_at.is_(None))
        if owner_id:
            statement = statement.where(Resume.owner_user_id == owner_id)
        if resume_id:
            statement = statement.where(Resume.id == resume_id)
        if keyword:
            pattern = _like(keyword)
            fields = (
                Resume.raw_content, Resume.candidate_name, Resume.source_filename, archive_name, analysis_name,
                Resume.processed_data["candidate_name"].as_string(),
                Resume.processed_data["personal_data"]["name"].as_string(),
                User.username, User.email,
            )
            filters = [field.ilike(pattern, escape="\\") for field in fields]
            compact = re.sub(r"\s+", "", keyword)
            if re.fullmatch(r"[\u3400-\u9fff·・]{2,18}", compact):
                # PDF glyph spacing and Markdown marks can split an otherwise searchable name.
                compact_text = sa.func.regexp_replace(Resume.raw_content, "[[:blank:]*_\u200b\ufeff]", "", "g")
                filters.append(compact_text.ilike(_like(compact), escape="\\"))
            elif " " in keyword:
                normalized_text = sa.func.regexp_replace(Resume.raw_content, "[[:blank:]]+", " ", "g")
                filters.append(normalized_text.ilike(pattern, escape="\\"))
            statement = statement.where(sa.or_(*filters))
        return statement

    def _summary(self, row, name):
        raw = str(row.get("raw_content") or "")
        cleaned, residue = strip_residue_lines(raw)
        owner = row["owner_user_id"]
        return {"resume_id": str(row["id"]), "user_id": str(owner),
                "owner_username": str(row.get("owner_username") or ""), "owner_email": str(row.get("owner_email") or ""),
                "candidate_name": name,
                "chars": len(cleaned), "content_type": row.get("content_type") or "md",
                "created_at": _iso(row.get("created_at")), "residue_lines_removed": residue,
                "content_suspect": any(ord(ch) < 32 and ch not in "\n\r\t" for ch in raw),
                "original_available": bool(row.get("object_key"))}

    def list_resumes(self, actor_id, *, password_version, keyword="", owner_user_id=None, page=1, size=20):
        page = _page(page, "page", 1, 1_000_000)
        size = _page(size, "size", 20, self.MAX_PAGE_SIZE)
        owner = None
        if owner_user_id:
            try:
                owner = _uuid(owner_user_id, "user_id")
            except (TypeError, ValueError):
                raise AdminServiceError("Invalid user_id") from None
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            needle = " ".join(keyword.split()) if isinstance(keyword, str) else ""
            matches = self._resume_query(owner_id=owner, keyword=needle).with_only_columns(
                Resume.id, Resume.created_at,
            ).cte("matching_resumes")
            start = (page - 1) * size
            page_ids = sa.select(matches.c.id).order_by(
                matches.c.created_at.desc(), matches.c.id.desc(),
            ).offset(start).limit(size).subquery()
            page_rows = self._resume_query().join(page_ids, Resume.id == page_ids.c.id).subquery()
            totals = sa.select(sa.func.count().label("total")).select_from(matches).subquery()
            # One snapshot for the total and the page, including an empty or out-of-range page.
            rows = session.execute(sa.select(page_rows, totals.c.total).select_from(
                totals.outerjoin(page_rows, sa.true()),
            ).order_by(page_rows.c.created_at.desc(), page_rows.c.id.desc())).mappings().all()
            items = [self._summary(row, self._candidate_name(row)) for row in rows if row["id"] is not None]
            return {"items": items, "total": rows[0]["total"], "page": page, "size": size}

    def get_resume(self, actor_id, resume_id, *, password_version, request_id):
        try:
            identity = _uuid(resume_id, "resume_id")
        except (TypeError, ValueError):
            raise AdminServiceError("Invalid UUID", 400) from None
        with self.sessions() as session, session.begin():
            actor, owner = self._actor(session, actor_id, password_version)
            row = session.execute(self._resume_query(resume_id=identity)).mappings().one_or_none()
            if row is None:
                raise AdminServiceError("Resume not found", 404)
            summary = self._summary(row, self._candidate_name(row))
            raw = str(row.get("raw_content") or "")
            cleaned, residue = strip_residue_lines(raw)
            archived = session.scalar(sa.select(sa.func.count()).select_from(Archive.__table__).where(
                Archive.resume_id == identity, Archive.owner_user_id == row["owner_user_id"], Archive.status.in_(("active", "trashed")))) or 0
            append_audit(session, actor_user_id=owner, action="resume.view", target_type="resume",
                         target_id=str(identity), request_id=request_id)
            summary.update(content=cleaned, chars=len(cleaned), residue_lines_removed=residue,
                           archived_count=int(archived), original=_safe_original(row))
            return summary

    def delete_resume(self, actor_id, resume_id, *, password_version, request_id):
        try:
            identity = _uuid(resume_id, "resume_id")
        except (TypeError, ValueError):
            raise AdminServiceError("Invalid UUID", 400) from None
        with self.sessions() as session, session.begin():
            _actor, actor = self._actor(session, actor_id, password_version)
            row = session.execute(sa.select(Resume.__table__).where(Resume.id == identity, Resume.deleted_at.is_(None)).with_for_update()).mappings().one_or_none()
            if row is None:
                raise AdminServiceError("Resume not found", 404)
            if has_unfinished_task_for_source(
                session, row["owner_user_id"], "resume", identity
            ):
                raise AdminServiceError("Finish or recover unfinished analysis tasks before deleting sources", 409)
            archives = session.execute(sa.select(Archive.__table__).where(
                Archive.owner_user_id == row["owner_user_id"], Archive.resume_id == identity,
                Archive.status.in_(("active", "trashed"))).with_for_update()).mappings().all()
            trashed = 0
            for archive in archives:
                if archive["status"] == "active":
                    session.execute(sa.update(Archive).where(Archive.id == archive["id"]).values(
                        status="trashed", updated_at=sa.func.now()))
                    trashed += 1
            key = row.get("object_key")
            if key:
                if self.original_storage_id is None:
                    raise AdminServiceError("Original storage is not configured", 503)
                session.execute(pg_insert(original_gc).values(storage_id=self.original_storage_id,
                    object_key=key, owner_user_id=row["owner_user_id"]).on_conflict_do_nothing())
            session.execute(sa.update(Resume).where(Resume.id == identity).values(
                raw_content="", processed_data={}, legacy_payload={}, object_key=None, content_sha256=None,
                source_filename=None, candidate_name=None, deleted_at=sa.func.now(), updated_at=sa.func.now()))
            append_audit(session, actor_user_id=actor, action="resume.delete", target_type="resume",
                         target_id=str(identity), request_id=request_id)
            return {"message": "简历已删除", "resume_id": str(identity), "archived_to_trash": trashed}

    def list_ops(self, actor_id, *, password_version, op="", keyword="", page=1, size=50, _max_size=200):
        page = _page(page, "page", 1, 1_000_000); size = _page(size, "size", 50, _max_size)
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            stmt = sa.select(AuditLog.__table__, User.username.label("operator_name"))\
                .select_from(AuditLog.__table__.outerjoin(User.__table__, User.id == AuditLog.actor_user_id))
            if op:
                action = op.replace("_", ".")
                stmt = stmt.where(sa.or_(AuditLog.action == action, AuditLog.action == op))
            clean = keyword.strip() if isinstance(keyword, str) else ""
            if clean:
                pattern = _like(clean)
                stmt = stmt.where(sa.or_(AuditLog.action.ilike(pattern, escape="\\"), AuditLog.target_id.ilike(pattern, escape="\\"),
                    User.username.ilike(pattern, escape="\\"), sa.cast(AuditLog.metadata_json, sa.Text).ilike(pattern, escape="\\")))
            rows = session.execute(stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())).mappings().all()
            items = []
            for row in rows:
                metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
                action = str(row.get("action") or "")
                item = {"created_at": _iso(row.get("created_at")), "op": action.replace(".", "_"),
                        "operator_name": str(row.get("operator_name") or metadata.get("actor_name") or "system"),
                        "target_username": str(metadata.get("target_name") or row.get("target_id") or ""),
                        "detail": f"{action} target={row.get('target_id') or ''}", "request_id": row.get("request_id") or ""}
                items.append(item)
            total = len(items); start = (page - 1) * size
            return {"items": items[start:start + size], "total": total, "page": page, "size": size}

    def export_ops(self, actor_id, *, password_version, op="", keyword=""):
        # Export is intentionally not paginated; take the same filtered snapshot
        # in one transaction and return every matching audit row.
        return self.list_ops(actor_id, password_version=password_version, op=op, keyword=keyword,
                             page=1, size=1_000_000, _max_size=1_000_000)

    def _usage_rows(self, session, owner):
        login = sa.select(sa.cast(sa.func.date_trunc("day", sa.func.timezone("UTC", AuditLog.created_at)), sa.Date).label("day"),
                          sa.func.count().label("count")).where(AuditLog.actor_user_id == owner, AuditLog.action == "user.login").group_by("day").subquery()
        from db_task_artifacts import artifacts
        attempts = sa.union_all(sa.select(Analysis.created_at).where(Analysis.owner_user_id == owner),
            sa.select(artifacts.c.created_at).where(artifacts.c.owner_user_id == owner)).subquery()
        analysis = sa.select(sa.cast(sa.func.date_trunc("day", sa.func.timezone("UTC", attempts.c.created_at)), sa.Date).label("day"),
                             sa.func.count().label("count")).group_by("day").subquery()
        rows = {}
        for row in session.execute(sa.select(login.c.day, login.c.count).order_by(login.c.day)).all(): rows.setdefault(row.day, {})["login"] = int(row.count)
        for row in session.execute(sa.select(analysis.c.day, analysis.c.count).order_by(analysis.c.day)).all(): rows.setdefault(row.day, {})["analysis"] = int(row.count)
        return rows

    @staticmethod
    def _usage_shape(rows, granularity, buckets):
        if granularity not in {"day", "month", "year"}: granularity = "day"
        now = datetime.now(timezone.utc)
        labels = []
        if granularity == "day": labels = [(now - timedelta(days=i)).date().isoformat() for i in range(buckets - 1, -1, -1)]
        elif granularity == "month":
            base = now.year * 12 + now.month - 1
            labels = [f"{(base-i)//12:04d}-{(base-i)%12+1:02d}" for i in range(buckets - 1, -1, -1)]
        else: labels = [str(now.year - i) for i in range(buckets - 1, -1, -1)]
        agg = {label: {"login": 0, "analysis": 0} for label in labels}
        for day, value in rows.items():
            key = day.isoformat() if granularity == "day" else (day.isoformat()[:7] if granularity == "month" else str(day.year))
            if key in agg:
                agg[key]["login"] += value.get("login", 0); agg[key]["analysis"] += value.get("analysis", 0)
        login = [agg[x]["login"] for x in labels]; analysis = [agg[x]["analysis"] for x in labels]; total = [a+b for a,b in zip(login, analysis)]
        peak = max(range(len(total)), key=lambda i: total[i]) if total else 0
        present = sorted(day.isoformat() for day in rows)
        return {"granularity": granularity, "labels": labels, "series": {"login": login, "analysis": analysis, "total": total},
                "summary": {"total": sum(total), "login_total": sum(login), "analysis_total": sum(analysis),
                    "avg_per_bucket": round(sum(total)/buckets, 2), "peak_label": labels[peak] if labels else "",
                    "peak_total": total[peak] if total else 0, "active_buckets": sum(x > 0 for x in total),
                    "first_usage": present[0] if present else "", "last_usage": present[-1] if present else ""}}

    def user_usage(self, actor_id, username, *, password_version, granularity="day", buckets=30):
        buckets = _page(buckets, "buckets", 30, 3660)
        if granularity == "month": buckets = min(buckets, 240)
        if granularity == "year": buckets = min(buckets, 10)
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            user = session.execute(sa.select(User.id).where(sa.func.lower(User.username) == username.strip().lower())).scalar_one_or_none()
            if user is None: raise AdminServiceError("用户不存在", 404)
            return self._usage_shape(self._usage_rows(session, user), granularity, buckets)

    def usage_ranking(self, actor_id, *, password_version, limit=20):
        limit = _page(limit, "limit", 20, 200)
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            rows = []
            for user in session.execute(sa.select(User.id, User.username).where(User.deleted_at.is_(None))).mappings().all():
                usage = self._usage_shape(self._usage_rows(session, user["id"]), "year", 10)["summary"]
                if usage["total"]:
                    rows.append({"user_id": str(user["id"]), "username": user["username"], "login": usage["login_total"],
                                 "analysis": usage["analysis_total"], "total": usage["total"], "last_usage": usage["last_usage"]})
            rows.sort(key=lambda x: (x["total"], x["last_usage"]), reverse=True)
            return rows[:limit]
