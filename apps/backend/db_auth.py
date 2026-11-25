"""Opt-in database authentication; no HTTP wiring, config import or connection at import.

Inject a SQLAlchemy Session factory (for example sessionmaker(engine)). Each call
owns a fresh session and transaction. Findings return InternalUser credentials for
trusted callers only; create/authenticate return an allowlisted public mapping.
Admin email policy must be supplied explicitly by the composition root. Trusted
callers MUST verify email ownership/registration authorization before create_user:
an email matching that policy confers superadmin authority on subsequent lookup.
This service is not a public registration endpoint. This is PBKDF2 compatibility
code, not a production storage cutover or Argon migration.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db_models import User

_PBKDF2_ITERATIONS = 200_000
_USERNAME = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{2,64}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Exact auth.py format: PBKDF2 uses the ASCII base64 salt *text*."""
    if salt is None:
        salt = base64.b64encode(os.urandom(16)).decode("ascii")
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  salt.encode("ascii"), iterations=_PBKDF2_ITERATIONS)
    return base64.b64encode(derived).decode("ascii"), salt


def verify_password(password: str, hashed_b64: str, salt_b64: str) -> bool:
    try:
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                      salt_b64.encode("ascii"), iterations=_PBKDF2_ITERATIONS)
        return hmac.compare_digest(derived, base64.b64decode(hashed_b64))
    except (binascii.Error, ValueError, TypeError, AttributeError):
        return False


def _password_error(password: str) -> str | None:
    if not isinstance(password, str) or len(password) < 8:
        return "密码长度至少 8 个字符"
    if not re.search(r"[A-Za-z]", password):
        return "密码必须包含至少一个字母"
    if not re.search(r"\d", password):
        return "密码必须包含至少一个数字"
    return None


def _uuid(value: str | UUID) -> UUID:
    if not isinstance(value, (str, UUID)):
        raise ValueError("user_id requires a nonzero UUID")
    try:
        result = UUID(str(value))
    except ValueError as exc:
        raise ValueError("user_id requires a nonzero UUID") from exc
    if not result.int:
        raise ValueError("user_id requires a nonzero UUID")
    return result


@dataclass(frozen=True)
class InternalUser:
    """Detached, typed trusted lookup data. Never send dataclasses.asdict to HTTP."""
    user_id: UUID
    username: str
    email: str
    password_hash: str = field(repr=False)
    password_salt: str = field(repr=False)
    password_version: int
    is_admin: bool
    is_super_admin: bool
    status: str
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def can_authenticate(self) -> bool:
        return self.status == "active" and self.deleted_at is None

    def to_public(self) -> dict:
        """Allowlist authoritative columns; never merge historical legacy_payload."""
        return {"user_id": str(self.user_id), "username": self.username,
                "email": self.email, "password_version": self.password_version,
                "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat()}


class AuthService:
    def __init__(self, session_factory: Callable[[], Session], *, admin_emails: Iterable[str] = ()):
        if not callable(session_factory):
            raise TypeError("session_factory must create a fresh Session")
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError("admin_emails must be an iterable of email addresses")
        self._session_factory = session_factory
        self._admin_emails = frozenset(email.strip().lower() for email in admin_emails if email.strip())
        self._table = User.__table__

    def _internal(self, row: Mapping | None) -> InternalUser | None:
        if row is None:
            return None
        email = (row["email"] or "").strip().lower()
        # Superadmin authority comes only from the explicit external policy.
        super_admin = bool(email and email in self._admin_emails)
        return InternalUser(user_id=row["id"], username=row["username"], email=email,
                            password_hash=row["password_hash"], password_salt=row["password_salt"],
                            password_version=row["password_version"],
                            is_admin=bool(row["is_admin"]) or super_admin,
                            is_super_admin=super_admin, status=row["status"],
                            deleted_at=row["deleted_at"], created_at=row["created_at"],
                            updated_at=row["updated_at"])

    def _find(self, predicate) -> InternalUser | None:
        with self._session_factory() as session, session.begin():
            return self._internal(session.execute(select(self._table).where(predicate)).mappings().one_or_none())

    def find_user_by_id(self, user_id: str | UUID) -> InternalUser | None:
        return self._find(self._table.c.id == _uuid(user_id))

    def find_user_by_username(self, username: str) -> InternalUser | None:
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            return None
        return self._find(func.lower(self._table.c.username) == username.lower())

    def find_user_by_email(self, email: str) -> InternalUser | None:
        if not isinstance(email, str) or not email.strip():
            return None
        return self._find(func.lower(self._table.c.email) == email.strip().lower())

    def create_user(self, username: str, password: str, email: str = "") -> tuple[dict | None, str | None]:
        """Trusted registration after caller verifies email ownership/authorization.

        A supplied email on the external admin allowlist grants superadmin on
        lookup. Email verification is an upstream requirement, not done here.
        Registration cannot accept privilege/status/legacy payload parameters.
        DB unique indexes arbitrate concurrent duplicate username/email requests;
        unrelated integrity and infrastructure failures propagate to the caller.
        """
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            return None, "用户名仅允许字母、数字、下划线、连字符或中文，长度 2-64 字符"
        error = _password_error(password)
        if error:
            return None, error
        if not isinstance(email, str):
            return None, "邮箱格式不正确"
        clean = email.strip().lower()
        if clean and (len(clean) > 320 or not _EMAIL.fullmatch(clean)):
            return None, "邮箱格式不正确"
        hashed, salt = hash_password(password)
        now = datetime.now(timezone.utc)
        values = dict(id=uuid4(), username=username, email=clean or None,
                      password_hash=hashed, password_salt=salt, password_version=1,
                      is_admin=False, is_super_admin=False, status="active",
                      deleted_at=None, created_at=now, updated_at=now, legacy_payload={})
        try:
            with self._session_factory() as session, session.begin():
                row = session.execute(insert(self._table).values(**values).returning(*self._table.c)).mappings().one()
                result = self._internal(row).to_public()
            return result, None
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_users_username_lower":
                return None, f"用户名 '{username}' 已被注册 (大小写不敏感)"
            if constraint == "uq_users_email_lower":
                return None, "该邮箱已被注册"
            raise

    def authenticate_user(self, username: str, password: str) -> dict | None:
        if not isinstance(password, str):
            return None
        user = self.find_user_by_username(username)
        if user is None or not user.can_authenticate:
            # Full work factor on absent/deleted/locked/pending accounts as auth.py.
            hash_password(password)
            return None
        if not verify_password(password, user.password_hash, user.password_salt):
            return None
        return user.to_public()

    def update_password(self, user_id: str | UUID, new_password: str) -> tuple[bool, str | None]:
        identity = _uuid(user_id)
        error = _password_error(new_password)
        if error:
            return False, error
        with self._session_factory() as session, session.begin():
            row = session.execute(select(self._table).where(self._table.c.id == identity)
                                  .with_for_update()).mappings().one_or_none()
            if row is None:
                return False, "用户不存在"
            if row["status"] != "active" or row["deleted_at"] is not None:
                return False, "账户不可用"
            hashed, salt = hash_password(new_password)
            session.execute(update(self._table).where(self._table.c.id == identity).values(
                password_hash=hashed, password_salt=salt,
                password_version=row["password_version"] + 1,
                updated_at=datetime.now(timezone.utc)))
            # legacy_payload remains historical. _internal/to_public never read it.
        return True, None
