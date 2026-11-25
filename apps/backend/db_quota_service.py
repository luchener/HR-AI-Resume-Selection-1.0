"""Transactional PostgreSQL quota settings and read-model service.

The legacy JSON quota module remains authoritative until the adapter is
explicitly injected. Every mutation owns a session transaction, rechecks the
actor and target identity, and writes its audit event before commit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_audit import append_audit
from db_models import User
from db_quota import quota_daily_usage, quota_policies, resolve_policy, utc_day


class QuotaServiceError(Exception):
    status = 503

    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status


class QuotaPermissionError(QuotaServiceError):
    status = 403


class QuotaValidationError(QuotaServiceError):
    status = 422


class QuotaNotFoundError(QuotaServiceError):
    status = 404


MAX_DAILY_LIMIT = 1000


def _uuid(value: str | UUID, label: str) -> UUID:
    try:
        result = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise QuotaValidationError(f"Invalid {label}") from exc
    if not result.int:
        raise QuotaValidationError(f"Invalid {label}")
    return result


def _limit(value):
    if value is None or value == "":
        return None
    if type(value) is bool:
        raise QuotaValidationError("每日限额必须是整数。")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise QuotaValidationError("每日限额必须是整数。") from exc
    if result < -1 or result > MAX_DAILY_LIMIT:
        raise QuotaValidationError(f"每日限额需在 -1 ~ {MAX_DAILY_LIMIT} 之间（-1=不限）。")
    return result


def _bool(payload, key):
    if key not in payload:
        return None
    if type(payload[key]) is not bool:
        raise QuotaValidationError(f"{key} 必须是布尔值。")
    return payload[key]


class QuotaService:
    def __init__(self, session_factory, *, admin_emails=()):
        self.sessions = session_factory
        self.admin_emails = frozenset(str(e).strip().lower() for e in admin_emails if str(e).strip())

    def _actor(self, session, actor_id, password_version, *, require_admin=True):
        actor = _uuid(actor_id, "actor")
        if type(password_version) is not int:
            raise QuotaPermissionError("Invalid or revoked token")
        row = session.execute(sa.select(User).where(User.id == actor).with_for_update()).scalar_one_or_none()
        if (row is None or row.status != "active" or row.deleted_at is not None
                or row.password_version != password_version):
            raise QuotaPermissionError("Invalid or revoked token")
        if require_admin and not (row.is_admin or (row.email or "").lower() in self.admin_emails):
            raise QuotaPermissionError("Administrator required")
        return row

    def _global(self, session):
        return session.execute(sa.select(quota_policies).where(
            quota_policies.c.scope_key == "global")).mappings().one_or_none()

    def _user_policy(self, session, user_id):
        return session.execute(sa.select(quota_policies).where(
            quota_policies.c.scope_key == str(user_id))).mappings().one_or_none()

    @staticmethod
    def _effective(global_row, user_row, *, is_admin):
        global_policy = dict(global_row or {})
        user_policy = dict(user_row or {})
        return resolve_policy(global_policy, user_policy, is_admin=is_admin)

    def _used(self, session, user_id, day):
        row = session.execute(sa.select(quota_daily_usage).where(
            quota_daily_usage.c.user_id == user_id, quota_daily_usage.c.date == day
        )).mappings().one_or_none()
        return int((row or {}).get("used", 0) or 0)

    def _audit(self, session, actor, action, target_id, request_id, fields):
        append_audit(session, actor_user_id=actor.id, action=action,
                     target_type="quota", target_id=str(target_id), request_id=request_id,
                     changed_fields=fields)

    def _policy_values(self, *, user_id=None, mode="inherit", daily_limit=None,
                       enabled=True, quota_enabled=None, email_notify=False):
        return dict(scope_key="global" if user_id is None else str(user_id), user_id=user_id,
                    mode=mode, daily_limit=daily_limit, enabled=enabled,
                    quota_enabled=quota_enabled, email_notify=email_notify)

    def _upsert(self, session, values):
        key = values["scope_key"]
        update_values = {k: values[k] for k in values if k != "scope_key"}
        session.execute(insert(quota_policies).values(**values).on_conflict_do_update(
            index_elements=[quota_policies.c.scope_key], set_=update_values))
        return self._global(session) if key == "global" else self._user_policy(session, values["user_id"])

    def get_settings(self, actor_id, *, password_version, request_id):
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            global_row = self._global(session)
            day = utc_day()
            users = []
            rows = session.execute(sa.select(User, quota_policies).outerjoin(
                quota_policies, quota_policies.c.user_id == User.id
            ).where(User.status != "deleted").order_by(User.username.asc())).all()
            for row in rows:
                user = row[0]
                # A Table selected alongside an ORM entity is flattened by
                # SQLAlchemy; reconstruct a policy mapping from named columns.
                policy = {column.name: row._mapping[column] for column in quota_policies.c}
                if policy["scope_key"] is None:
                    continue
                effective = self._effective(global_row, policy, is_admin=bool(user.is_admin))
                used = self._used(session, user.id, day)
                limit = effective.daily_limit
                users.append({
                    "username": user.username, "is_admin": bool(user.is_admin),
                    "has_email": bool(user.email), "daily_limit": limit,
                    "raw_daily_limit": policy["daily_limit"],
                    "custom": policy["mode"] != "inherit",
                    "analysis_enabled": bool(policy["enabled"]),
                    "email_notify": bool(policy["email_notify"]), "used": used,
                    "remaining": -1 if limit is None else max(0, limit - used),
                    "updated_at": "",
                })
            return {
                "quota_enabled": bool(global_row and global_row["quota_enabled"]),
                "default_daily": None if not global_row or global_row["mode"] != "limited" else global_row["daily_limit"],
                "email_notify_enabled": bool(global_row and global_row["email_notify"]),
                "users": users, "updated_at": "",
            }

    def update_settings(self, actor_id, payload, *, password_version, request_id):
        if not isinstance(payload, dict) or not set(payload) & {"quota_enabled", "default_daily", "email_notify_enabled"}:
            raise QuotaValidationError("全局配额请求体为空或字段不合法。")
        unknown = set(payload) - {"quota_enabled", "default_daily", "email_notify_enabled"}
        if unknown:
            raise QuotaValidationError("全局配额字段不合法。")
        enabled = _bool(payload, "quota_enabled")
        notify = _bool(payload, "email_notify_enabled")
        daily_provided = "default_daily" in payload
        daily = _limit(payload.get("default_daily")) if daily_provided else None
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            current = self._global(session) or {}
            if not daily_provided:
                mode = current.get("mode", "unlimited")
                stored_daily = current.get("daily_limit") if mode == "limited" else None
            else:
                mode = "limited" if daily is not None and daily >= 0 else "unlimited"
                stored_daily = daily if daily is not None and daily >= 0 else None
            values = self._policy_values(
                mode=mode, daily_limit=stored_daily,
                enabled=True, quota_enabled=(enabled if enabled is not None else current.get("quota_enabled", False)),
                email_notify=(notify if notify is not None else current.get("email_notify", False)))
            self._upsert(session, values)
            self._audit(session, actor, "quota.update", "global", request_id,
                        ["daily_limit", "analysis_enabled", "email_notify"])
        return self.get_settings(actor_id, password_version=password_version, request_id=request_id)

    def set_user_quota(self, actor_id, username, payload, *, password_version, request_id):
        if not isinstance(payload, dict) or not set(payload) & {"daily_limit", "analysis_enabled", "email_notify"}:
            raise QuotaValidationError("账号配额请求体为空或字段不合法。")
        unknown = set(payload) - {"daily_limit", "analysis_enabled", "email_notify"}
        if unknown:
            raise QuotaValidationError("账号配额字段不合法。")
        daily_provided = "daily_limit" in payload
        daily = _limit(payload.get("daily_limit")) if daily_provided else None
        enabled = _bool(payload, "analysis_enabled")
        notify = _bool(payload, "email_notify")
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            target = session.execute(sa.select(User).where(sa.func.lower(User.username) == str(username).lower())
                                     .with_for_update()).scalar_one_or_none()
            if target is None or target.status == "deleted":
                raise QuotaNotFoundError("用户不存在。")
            current = self._user_policy(session, target.id) or {}
            if not daily_provided:
                mode = current.get("mode", "inherit")
                stored_daily = current.get("daily_limit") if mode == "limited" else None
            else:
                mode = "inherit" if daily is None else "unlimited" if daily < 0 else "limited"
                stored_daily = daily if daily is not None and daily >= 0 else None
            values = self._policy_values(
                user_id=target.id,
                mode=mode, daily_limit=stored_daily,
                enabled=(enabled if enabled is not None else bool(current.get("enabled", True))),
                email_notify=(notify if notify is not None else bool(current.get("email_notify", True))))
            self._upsert(session, values)
            self._audit(session, actor, "quota.update", target.id, request_id,
                        [key for key in ("daily_limit", "analysis_enabled", "email_notify") if key in payload])
        return self.get_settings(actor_id, password_version=password_version, request_id=request_id)

    def reset_user_quota(self, actor_id, username, *, password_version, request_id):
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            target = session.execute(sa.select(User).where(sa.func.lower(User.username) == str(username).lower())
                                     .with_for_update()).scalar_one_or_none()
            if target is None or target.status == "deleted":
                raise QuotaNotFoundError("用户不存在。")
            deleted = session.execute(sa.delete(quota_policies).where(
                quota_policies.c.scope_key == str(target.id))).rowcount
            if not deleted:
                raise QuotaNotFoundError("该账号未单独配置配额。")
            self._audit(session, actor, "quota.update", target.id, request_id,
                        ["daily_limit", "analysis_enabled", "email_notify"])
        return {"message": "已恢复全局默认。"}

    def get_my_quota(self, actor_id, *, password_version):
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version, require_admin=False)
            global_row = self._global(session)
            user_row = self._user_policy(session, actor.id)
            effective = self._effective(global_row, user_row, is_admin=bool(actor.is_admin))
            if not effective.analysis_enabled:
                return {"analysis_enabled": False, "unlimited": True, "is_admin": bool(actor.is_admin)}
            if not global_row or not global_row["quota_enabled"] or effective.daily_limit is None:
                return {"analysis_enabled": True, "unlimited": True,
                        "quota_enabled": bool(global_row and global_row["quota_enabled"]),
                        "is_admin": bool(actor.is_admin)}
            used = self._used(session, actor.id, utc_day())
            return {"analysis_enabled": True, "unlimited": False,
                    "quota_enabled": True, "daily_limit": effective.daily_limit,
                    "used": used, "remaining": max(0, effective.daily_limit - used),
                    "resets_at": "次日 0 点自动重置", "is_admin": bool(actor.is_admin)}
