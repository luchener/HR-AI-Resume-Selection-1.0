"""Opt-in legacy-shaped SQL store. Importing this module performs no I/O.

Inject an engine OR a zero-argument session factory; each public operation owns
one session and transaction. This is not wired into the application. Authorization
for explicitly cross-tenant admin_* methods belongs to their caller.
Compatibility lists and searches remain unbounded, like the JSON store.
Original-file/object deletion, retention, normalization,
analysis/auth/quota/config storage and all other legacy functions are deliberately
not implemented here. Database FK restrictions on source deletion propagate.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from db_models import Archive, Resume
from db_repository import ArchiveRepository, JobRepository, ResumeRepository, _uuid


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _record(row, kind):
    if row is None:
        return None
    result = deepcopy(row.get("legacy_payload") or {})
    result.update({f"{kind}_id": str(row["id"]), "user_id": str(row["owner_user_id"]),
                   "created_at": result.get("created_at") or _iso(row["created_at"])})
    if kind == "resume":
        result.update(content=row["raw_content"], content_type=row["content_type"],
                      processed=deepcopy(row["processed_data"]))
    elif kind == "job":
        result.update(content=row["raw_content"], processed=deepcopy(row["parsed_requirements"]))
        result["resume_id"] = result.get("resume_id") or (str(row["source_resume_id"]) if row["source_resume_id"] else None)
    else:
        result.update(deepcopy(row.get("payload") or {}))
        for key in ("resume_id", "job_id"):
            result[key] = result.get(key) or (str(row[key]) if row[key] else None)
        result.update(candidate_name=row["candidate_name"], category=row["category"],
                      custom_tags=deepcopy(row["tags"]), status=row["status"],
                      trashed_at=result.get("trashed_at"))
        for key, default in (("final_score", 0), ("fit_tag", ""),
                             ("recruitment_recommendation", ""), ("job_title", ""),
                             ("analysis_snapshot", {}), ("analysis", {})):
            result.setdefault(key, deepcopy(default))
    return result


def _resume_view(rec):
    if rec is None:
        return None
    p = rec.get("processed") or {}
    processed = {key: p.get(key) or [] for key in (
        "experiences", "projects", "skills", "research_work", "achievements", "education", "extracted_keywords")}
    processed.update(personal_data=p.get("personal_data") or {}, processed_at=p.get("processed_at"))
    return {"resume_id": rec["resume_id"], "raw_resume": {
        "id": 0, "content": rec.get("content", ""), "content_type": rec.get("content_type", "md"),
        "created_at": rec.get("created_at")}, "processed_resume": processed}


def _job_view(rec):
    if rec is None:
        return None
    p = rec.get("processed") or {}
    processed = {key: p.get(key) for key in (
        "job_title", "date_posted", "employment_type", "compensation_and_benfits", "processed_at")}
    processed.update({key: p.get(key) or {} for key in (
        "company_profile", "location", "qualifications", "application_info")})
    processed.update({key: p.get(key) or [] for key in ("key_responsibilities", "extracted_keywords")})
    processed["job_summary"] = p.get("job_summary") or ""
    return {"job_id": rec["job_id"], "raw_job": {"id": 0, "resume_id": rec.get("resume_id"),
        "content": rec.get("content", ""), "created_at": rec.get("created_at")}, "processed_job": processed}


class Store:
    def __init__(self, engine=None, *, session_factory=None):
        if (engine is None) == (session_factory is None):
            raise ValueError("Inject exactly one engine or session_factory")
        self._sessions = session_factory if session_factory is not None else sessionmaker(bind=engine)

    @contextmanager
    def _operation(self, user_id):
        owner = _uuid(user_id)  # No empty-owner bypass, even before opening a session.
        with self._sessions() as session:
            with session.begin():
                yield session, owner

    def save_resume(self, content: str, processed: dict, user_id: str, content_type: str = "md") -> str:
        with self._operation(user_id) as (session, owner):
            row = ResumeRepository(session).insert(owner, {"raw_content": content,
                "processed_data": processed, "content_type": content_type})
            return str(row["id"])

    def _get(self, kind, record_id, user_id, view=None):
        repository = {"resume": ResumeRepository, "job": JobRepository, "archive": ArchiveRepository}[kind]
        with self._operation(user_id) as (session, owner):
            row = repository(session).get(owner, record_id)
            rec = _record(row, kind) if row is not None and (kind == 'archive' or row['deleted_at'] is None) else None
            return view(rec) if view else rec

    def get_resume(self, resume_id: str, user_id: str):
        return self._get("resume", resume_id, user_id)

    def get_resume_view(self, resume_id: str, user_id: str):
        return self._get("resume", resume_id, user_id, _resume_view)

    def list_resumes(self, user_id: str) -> list[dict]:
        with self._operation(user_id) as (session, owner):
            rows = session.execute(select(Resume.__table__).where(
                Resume.owner_user_id == owner, Resume.deleted_at.is_(None)).order_by(Resume.id)).mappings().all()
            return [_record(row, "resume") for row in rows]

    def admin_list_resumes(self) -> list[dict]:
        """Explicit cross-tenant operation; caller must authorize an administrator."""
        with self._sessions() as session:
            with session.begin():
                rows = session.execute(select(Resume.__table__).where(
                    Resume.deleted_at.is_(None)).order_by(Resume.id)).mappings().all()
                return [_record(row, "resume") for row in rows]

    def admin_get_resume(self, resume_id: str):
        """Explicit cross-tenant lookup; caller must authorize an administrator."""
        record_id = _uuid(resume_id, "record_id")
        with self._sessions() as session:
            with session.begin():
                row = session.execute(select(Resume.__table__).where(
                    Resume.id == record_id, Resume.deleted_at.is_(None))).mappings().one_or_none()
                return _record(row, "resume")

    def admin_name_by_resume_id(self) -> dict:
        """Cross-tenant archive names for an authorized administrator's resume list."""
        with self._sessions() as session:
            with session.begin():
                rows = session.execute(select(Archive.__table__).where(
                    Archive.status.in_(("active", "trashed"))).order_by(
                    Archive.owner_user_id, Archive.status, Archive.created_at, Archive.id)).mappings().all()
                names = {}
                for row in rows:
                    rec = _record(row, "archive")
                    resume_id = str(rec.get("resume_id") or "")
                    name = str(rec.get("candidate_name") or "").strip()
                    if resume_id and name and resume_id not in names:
                        names[resume_id] = name[:40]
                return names

    def delete_resume(self, resume_id: str, user_id: str) -> bool:
        """Delete the SQL row only; object storage lifecycle is not implemented."""
        with self._operation(user_id) as (session, owner):
            return ResumeRepository(session).delete(owner, resume_id)

    def set_resume_original(self, resume_id: str, meta: dict, user_id: str) -> bool:
        with self._operation(user_id) as (session, owner):
            return ResumeRepository(session).update(owner, resume_id,
                {"legacy_payload": {"original": deepcopy(dict(meta or {}))}}) is not None

    def save_job(self, resume_id: str, content: str, processed: dict, user_id: str) -> str:
        with self._operation(user_id) as (session, owner):
            row = JobRepository(session).insert(owner, {"source_resume_id": resume_id,
                "raw_content": content, "parsed_requirements": processed,
                "title": processed.get("job_title"), "legacy_payload": {"resume_id": str(resume_id)}})
            return str(row["id"])

    def get_job(self, job_id: str, user_id: str):
        return self._get("job", job_id, user_id)

    def get_job_view(self, job_id: str, user_id: str):
        return self._get("job", job_id, user_id, _job_view)

    def delete_job(self, job_id: str, user_id: str) -> bool:
        with self._operation(user_id) as (session, owner):
            return JobRepository(session).delete(owner, job_id)

    def save_archive(self, user_id: str, resume_id: str, job_id: str, candidate_name: str,
                     final_score: int, fit_tag: str, recruitment_recommendation: str,
                     job_title: str, custom_tags: list[str] | None = None,
                     analysis_snapshot: dict | None = None, category: str = "",
                     full_analysis: dict | None = None) -> str:
        with self._operation(user_id) as (session, owner):
            aid = uuid4()
            payload = {"resume_id": str(resume_id), "job_id": str(job_id),
                "final_score": final_score, "fit_tag": fit_tag,
                "recruitment_recommendation": recruitment_recommendation, "job_title": job_title,
                "analysis_snapshot": deepcopy(analysis_snapshot or {}), "analysis": deepcopy(full_analysis or {})}
            ArchiveRepository(session).insert(owner, {"resume_id": resume_id, "job_id": job_id,
                "candidate_name": candidate_name, "category": category or job_title,
                "tags": custom_tags or [], "payload": payload}, record_id=aid)
            return str(aid)

    def get_archive(self, archive_id: str, user_id: str):
        return self._get("archive", archive_id, user_id)

    def list_archives(self, user_id: str, status: str = "active") -> list[dict]:
        with self._operation(user_id) as (session, owner):
            if status not in {"active", "trashed"}:
                raise ValueError("Unsupported archive status")
            records = self._archive_records(session, owner, (status,))
            return sorted(records, key=lambda rec: rec.get("final_score", 0) or 0, reverse=True)

    @staticmethod
    def _archive_records(session, owner, statuses):
        rows = session.execute(select(Archive.__table__).where(
            Archive.owner_user_id == owner,
            Archive.status == statuses[0] if len(statuses) == 1 else Archive.status.in_(statuses)).order_by(
            Archive.status, Archive.created_at, Archive.id)).mappings().all()
        return [_record(row, "archive") for row in rows]

    def archives_referencing(self, user_id: str, *, resume_id: str = "", job_id: str = "") -> list[dict]:
        """Match either source, including trashed archives and retained source IDs."""
        with self._operation(user_id) as (session, owner):
            if not resume_id and not job_id:
                return []
            records = self._archive_records(session, owner, ("active", "trashed"))
            hits = []
            for status in ("active", "trashed"):
                bucket = sorted((rec for rec in records if rec["status"] == status),
                                key=lambda rec: rec.get("final_score", 0) or 0, reverse=True)
                hits.extend(rec for rec in bucket if
                            (resume_id and rec.get("resume_id") == str(resume_id)) or
                            (job_id and rec.get("job_id") == str(job_id)))
            return hits

    def find_existing_archive(self, user_id: str, resume_id: str, job_id: str) -> str | None:
        """Return the first matching active archive, then a matching trashed archive."""
        with self._operation(user_id) as (session, owner):
            records = self._archive_records(session, owner, ("active", "trashed"))
            for status in ("active", "trashed"):
                for rec in records:
                    if (rec["status"] == status and rec.get("resume_id") == str(resume_id)
                            and rec.get("job_id") == str(job_id)):
                        return rec["archive_id"]
            return None

    def query_archives(self, user_id: str, name_keyword: str = "", job_title: str = "",
                       tag: str = "", category: str = "", sort: str = "score") -> list[dict]:
        """Legacy literal, case-insensitive filters over all active owner records."""
        archives = self.list_archives(user_id, status="active")
        if name_keyword:
            archives = [a for a in archives if name_keyword.lower() in (a.get("candidate_name") or "").lower()]
        if job_title:
            archives = [a for a in archives if job_title.lower() in (a.get("job_title") or "").lower()]
        if category:
            archives = [a for a in archives if category.lower() in
                        (a.get("category") or a.get("job_title") or "").lower()]
        if tag:
            archives = [a for a in archives if tag.lower() in [t.lower() for t in (a.get("custom_tags") or [])]]
        if sort == "created":
            archives.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        else:
            archives.sort(key=lambda a: a.get("final_score", 0) or 0, reverse=True)
        return archives

    def get_distinct_job_titles(self, user_id: str) -> list[str]:
        return sorted({title for rec in self.list_archives(user_id) if
                       (title := (rec.get("job_title") or "").strip())})

    def get_distinct_categories(self, user_id: str) -> list[str]:
        return sorted({category for rec in self.list_archives(user_id) if
                       (category := (rec.get("category") or rec.get("job_title") or "").strip())})

    def empty_trash(self, user_id: str) -> int:
        """Delete only this owner's trashed archive rows in one transaction."""
        with self._operation(user_id) as (session, owner):
            rows = session.execute(delete(Archive.__table__).where(
                Archive.owner_user_id == owner, Archive.status == "trashed").returning(Archive.id)).all()
            return len(rows)

    def update_archive_tags(self, archive_id: str, user_id: str, custom_tags: list[str]) -> bool:
        with self._operation(user_id) as (session, owner):
            return ArchiveRepository(session).update(owner, archive_id, {"tags": custom_tags}) is not None

    def update_archive_category(self, archive_id: str, user_id: str, category: str) -> bool:
        with self._operation(user_id) as (session, owner):
            repo = ArchiveRepository(session)
            # The lock remains held through update; category fallback sees the same snapshot.
            current = repo._locked(owner, archive_id)
            if current is None or current["status"] != "active":
                return False
            value = category or _record(current, "archive").get("job_title", "")
            return repo.update(owner, archive_id, {"category": value}) is not None

    def soft_delete_archive(self, archive_id: str, user_id: str) -> bool:
        with self._operation(user_id) as (session, owner):
            return ArchiveRepository(session).soft_delete(owner, archive_id)

    def restore_archive(self, archive_id: str, user_id: str) -> bool:
        with self._operation(user_id) as (session, owner):
            return ArchiveRepository(session).restore(owner, archive_id)

    def permanent_delete_archive(self, archive_id: str, user_id: str) -> bool:
        with self._operation(user_id) as (session, owner):
            return ArchiveRepository(session).delete(owner, archive_id)
