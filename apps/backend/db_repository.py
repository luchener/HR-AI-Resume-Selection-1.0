"""Opt-in SQL repositories; importing this module never opens a connection.

The caller injects a Session and owns begin/commit/rollback. Results are database
column mappings, not legacy HTTP response shapes. No filesystem side effects,
password hashing, login policy, or production storage switching happen here.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Numeric, case, delete, exists, func, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ClauseElement

from db_models import Archive, Job, Resume, User
from db_security import bounded_page, literal_like


def _uuid(value, label="owner_user_id") -> UUID:
    if not isinstance(value, (str, UUID)) or not str(value).strip():
        raise ValueError(f"{label} requires a nonempty UUID")
    try:
        result = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} requires a nonempty UUID") from exc
    if result.int == 0:
        raise ValueError(f"{label} requires a nonzero UUID")
    return result


class _Repository:
    model = None
    owner_key = "owner_user_id"
    writable = frozenset()
    search_fields = ()
    sort_fields = ("created_at", "updated_at")
    reference_fields = ()

    def __init__(self, session: Session):
        self.session = session
        self.table = self.model.__table__

    def _scope(self, owner_user_id):
        return self.table.c[self.owner_key] == _uuid(owner_user_id)

    def _get_statement(self, owner_user_id, record_id, *, lock=False):
        statement = select(self.table).where(
            self._scope(owner_user_id), self.table.c.id == _uuid(record_id, "record_id")
        )
        return statement.with_for_update() if lock else statement

    def get(self, owner_user_id, record_id):
        """Return a row (including deletion state), or None for missing/foreign rows."""
        return self.session.execute(self._get_statement(owner_user_id, record_id)).mappings().one_or_none()

    def _list_statement(self, owner_user_id, *, page=1, size=20, search=None,
                        sort="created_at", descending=True):
        scope = self._scope(owner_user_id)
        offset, limit = bounded_page(page, size)
        if sort not in self.sort_fields:
            raise ValueError("Unsupported sort field")
        if not isinstance(descending, bool):
            raise ValueError("descending requires a boolean")
        statement = select(self.table).where(scope)
        if search is not None:
            pattern = literal_like(search)
            if not self.search_fields:
                raise ValueError("Search is unsupported")
            from sqlalchemy import or_
            statement = statement.where(or_(*(
                self.table.c[field].ilike(pattern, escape="\\") for field in self.search_fields
            )))
        column = self.table.c[sort]
        return statement.order_by(column.desc() if descending else column.asc(), self.table.c.id.asc()).offset(offset).limit(limit)

    def list(self, owner_user_id, **options):
        statement = self._list_statement(owner_user_id, **options)
        if "deleted_at" in self.table.c:
            statement = statement.where(self.table.c.deleted_at.is_(None))
        return self.session.execute(statement).mappings().all()

    def _values(self, values):
        if not isinstance(values, Mapping):
            raise ValueError("Values must be a mapping")
        unknown = set(values) - self.writable
        if unknown:
            raise ValueError("Unsupported or immutable fields: " + ", ".join(sorted(map(str, unknown))))
        if any(isinstance(value, ClauseElement) or hasattr(value, "__clause_element__") for value in values.values()):
            raise ValueError("Values must be bound data, not SQL expressions")
        result = deepcopy(dict(values))
        for key in self.reference_fields:
            if key in result and result[key] is not None:
                result[key] = _uuid(result[key], key)
        if "legacy_payload" in result and not isinstance(result["legacy_payload"], dict):
            raise ValueError("legacy_payload requires an object")
        return result

    def insert(self, owner_user_id, values, *, record_id=None):
        owner = _uuid(owner_user_id)
        values = self._values(values)
        values["id"] = _uuid(record_id, "record_id") if record_id is not None else uuid4()
        values[self.owner_key] = owner
        return self.session.execute(insert(self.table).values(**values).returning(*self.table.c)).mappings().one()

    def _locked(self, owner_user_id, record_id):
        return self.session.execute(self._get_statement(owner_user_id, record_id, lock=True)).mappings().one_or_none()

    def _update_locked(self, owner_user_id, record_id, current, values):
        if "legacy_payload" in values:
            # Preserve unknown migration fields and avoid in-place JSON mutation.
            values["legacy_payload"] = {**deepcopy(current["legacy_payload"] or {}), **values["legacy_payload"]}
        values["updated_at"] = datetime.now(timezone.utc)
        statement = update(self.table).where(
            self._scope(owner_user_id), self.table.c.id == _uuid(record_id, "record_id")
        ).values(**values).returning(*self.table.c)
        return self.session.execute(statement).mappings().one_or_none()

    def update(self, owner_user_id, record_id, values):
        self._scope(owner_user_id)  # Reject absent ownership before any session use.
        values = self._values(values)
        current = self._locked(owner_user_id, record_id)
        if current is None:
            return None
        return self._update_locked(owner_user_id, record_id, current, values)

    def delete(self, owner_user_id, record_id):
        """Hard delete a guarded row; the caller handles FK policy and object storage."""
        current = self._locked(owner_user_id, record_id)
        if current is None:
            return False
        statement = delete(self.table).where(
            self._scope(owner_user_id), self.table.c.id == _uuid(record_id, "record_id")
        ).returning(self.table.c.id)
        return self.session.execute(statement).scalar_one_or_none() is not None


class UserRepository(_Repository):
    model = User
    owner_key = "id"
    writable = frozenset({"username", "email", "password_hash", "password_salt", "password_version", "legacy_payload"})
    search_fields = ("username", "email")
    sort_fields = ("created_at", "updated_at", "username")

    def insert(self, owner_user_id, values, *, record_id=None):
        owner = _uuid(owner_user_id)
        if record_id is not None and _uuid(record_id, "record_id") != owner:
            raise ValueError("User ID must match owner")
        return super().insert(owner, values, record_id=owner)

    def get_by_username(self, username):
        """Authentication identity lookup only; caller verifies preexisting hashes.

        This deliberately has no owner parameter: usernames resolve the identity
        before authentication. It never joins or returns tenant resources.
        """
        if not isinstance(username, str) or not username.strip() or len(username) > 64:
            raise ValueError("A nonempty username of at most 64 characters is required")
        statement = select(self.table).where(func.lower(self.table.c.username) == func.lower(username))
        return self.session.execute(statement).mappings().one_or_none()


class ResumeRepository(_Repository):
    model = Resume
    writable = frozenset({"candidate_name", "source_filename", "content_type", "content_sha256", "object_key",
                          "raw_content", "legacy_payload", "processed_data", "parse_status", "retention_until"})
    search_fields = ("candidate_name", "source_filename")
    sort_fields = ("created_at", "updated_at", "candidate_name")


class JobRepository(_Repository):
    model = Job
    writable = frozenset({"source_resume_id", "title", "company_name", "raw_content", "legacy_payload", "parsed_requirements"})
    search_fields = ("title", "company_name")
    sort_fields = ("created_at", "updated_at", "title", "company_name")
    reference_fields = ("source_resume_id",)


class ArchiveRepository(_Repository):
    model = Archive
    writable = frozenset({"resume_id", "job_id", "analysis_id", "candidate_name", "category", "tags", "note", "legacy_payload", "payload"})
    search_fields = ("candidate_name",)
    sort_fields = ("created_at", "updated_at", "candidate_name", "category")
    reference_fields = ("resume_id", "job_id", "analysis_id")

    def list(self, owner_user_id, *, status="active", page=1, size=20, search=None,
             sort="score", descending=True, job_title=None, category=None, tag=None):
        # Even status/filter errors cannot create an ownerless query.
        self._scope(owner_user_id)
        if status not in {"active", "trashed"}:
            raise ValueError("Unsupported archive status")
        statement = self._list_statement(owner_user_id, page=page, size=size, search=search,
                                         sort="created_at" if sort == "score" else sort, descending=descending)
        statement = statement.where(self.table.c.status == status)
        if job_title is not None:
            statement = statement.where(self.table.c.payload["job_title"].astext.ilike(literal_like(job_title), escape="\\"))
        if category is not None:
            column = func.coalesce(func.nullif(self.table.c.category, ""), self.table.c.payload["job_title"].astext)
            statement = statement.where(column.ilike(literal_like(category), escape="\\"))
        if tag is not None:
            if not isinstance(tag, str) or len(tag) > 200:
                raise ValueError("Invalid tag")
            elements = func.jsonb_array_elements_text(self.table.c.tags).table_valued("value")
            statement = statement.where(exists(select(1).select_from(elements).where(func.lower(elements.c.value) == func.lower(tag))))
        if sort == "score":
            score_json = self.table.c.payload["final_score"]
            score = case((func.jsonb_typeof(score_json) == "number", score_json.astext.cast(Numeric)), else_=0)
            statement = statement.order_by(None).order_by(score.desc() if descending else score.asc(), self.table.c.id.asc())
        return self.session.execute(statement).mappings().all()

    def update(self, owner_user_id, record_id, values):
        self._scope(owner_user_id)
        values = self._values(values)
        current = self._locked(owner_user_id, record_id)
        if current is None or current["status"] != "active":
            return None
        return self._update_locked(owner_user_id, record_id, current, values)

    def _transition(self, owner_user_id, record_id, source, target):
        current = self._locked(owner_user_id, record_id)
        if current is None or current["status"] != source:
            return False
        legacy = deepcopy(current["legacy_payload"] or {})
        legacy.update(status=target, trashed_at=datetime.now(timezone.utc).isoformat() if target == "trashed" else None)
        return self._update_locked(owner_user_id, record_id, current, {"status": target, "legacy_payload": legacy}) is not None

    def soft_delete(self, owner_user_id, record_id):
        return self._transition(owner_user_id, record_id, "active", "trashed")

    def restore(self, owner_user_id, record_id):
        return self._transition(owner_user_id, record_id, "trashed", "active")

    def delete(self, owner_user_id, record_id):
        """Permanent deletion is allowed only after moving an archive to trash."""
        current = self._locked(owner_user_id, record_id)
        if current is None or current["status"] != "trashed":
            return False
        statement = delete(self.table).where(self._scope(owner_user_id),
            self.table.c.id == _uuid(record_id, "record_id"), self.table.c.status == "trashed").returning(self.table.c.id)
        return self.session.execute(statement).scalar_one_or_none() is not None
