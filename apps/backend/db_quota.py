"""Opt-in PostgreSQL quota foundation; no imports from or writes to JSON storage.

Call reserve/settle inside a caller-owned transaction (normally READ COMMITTED).
Commit before dispatching work; retry the whole transaction on serialization or
 deadlock errors. Every writer locks usage before reservations. Reservation IDs
are durable idempotency keys: retries must reuse the same user, UTC day and count.
Pending reservations never expire automatically: a crashed worker holds capacity
until an operator/reconciler proves failure and settles it. A future expiry worker
must use settle's lock order and coordinate with workers to prevent late success.
Email settings are resolved only; notification delivery/dedup and policy loading,
JSON migration, task dispatch, partial batch success and production wiring are out
of scope. One reservation settles all its items together.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Mapping
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID, insert
from sqlalchemy.engine import Connection

# Independent metadata: never mutate db_models.Base. The users stub only resolves
# FKs; create these three tables explicitly, not metadata.create_all() in production.
metadata = sa.MetaData()
_users = sa.Table("users", metadata, sa.Column("id", PGUUID(as_uuid=True), primary_key=True))
quota_policies = sa.Table(
    "quota_policies", metadata,
    sa.Column("scope_key", sa.String(36), primary_key=True),
    sa.Column("user_id", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True),
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
quota_daily_usage = sa.Table(
    "quota_daily_usage", metadata,
    sa.Column("user_id", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("date", sa.Date, primary_key=True),
    sa.Column("used", sa.Integer, nullable=False, server_default="0"),
    sa.Column("reserved", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint("used >= 0 AND reserved >= 0", name="ck_quota_usage_nonnegative"),
)
quota_reservations = sa.Table(
    "quota_reservations", metadata,
    sa.Column("request_id", PGUUID(as_uuid=True), primary_key=True),
    sa.Column("task_id", PGUUID(as_uuid=True), unique=True),
    sa.Column("user_id", PGUUID(as_uuid=True), nullable=False),
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
sa.Index("ix_quota_reservations_pending", quota_reservations.c.created_at,
         postgresql_where=quota_reservations.c.status == "pending")
TABLES = (quota_policies, quota_daily_usage, quota_reservations)


@dataclass(frozen=True)
class EffectiveQuota:
    analysis_enabled: bool
    daily_limit: int | None
    email_notify: bool


def resolve_policy(global_policy: Mapping | None = None, user_policy: Mapping | None = None,
                   *, is_admin: bool = False) -> EffectiveQuota:
    """Pure quota.py semantics; enabled is the independent per-user analysis gate.

    Global email_notify is the master switch; missing user email_notify defaults
    true as in JSON. A persisted user row should set it explicitly. Global enabled
    is reserved and does not disable all users. Missing global quota_enabled is off.
    """
    global_policy = global_policy or {}
    user_policy = user_policy or {}
    enabled = bool(user_policy.get("enabled", True))
    email = bool(global_policy.get("email_notify", False)) and bool(user_policy.get("email_notify", True))
    # Validate even while quota is off, rather than silently accept malformed rows.
    for policy in (global_policy, user_policy):
        mode = policy.get("mode", "inherit")
        limit = policy.get("daily_limit")
        if mode not in ("inherit", "unlimited", "limited"):
            raise ValueError("invalid quota mode")
        if mode == "limited":
            if type(limit) is not int or limit < 0:
                raise ValueError("limited policy requires a nonnegative integer")
        elif limit is not None:
            raise ValueError("only limited policy may specify daily_limit")
    limit = None
    if global_policy.get("quota_enabled", False):
        mode = user_policy.get("mode", "inherit")
        if mode == "limited":
            limit = user_policy["daily_limit"]
        elif mode == "inherit" and not is_admin and global_policy.get("mode") == "limited":
            limit = global_policy["daily_limit"]
    return EffectiveQuota(enabled, limit, email)


class QuotaDenied(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def check_capacity(policy: EffectiveQuota, used: int, reserved: int, count: int) -> None:
    """Pure decision; unlike legacy coercion, reject invalid internal counts."""
    if type(count) is not int or count <= 0:
        raise ValueError("count must be a positive integer")
    if type(used) is not int or type(reserved) is not int or min(used, reserved) < 0:
        raise ValueError("usage must be nonnegative integers")
    if not policy.analysis_enabled:
        raise QuotaDenied(403, "analysis is disabled")
    if policy.daily_limit is not None and used + reserved + count > policy.daily_limit:
        raise QuotaDenied(429, "daily quota exceeded")


def utc_day(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("UTC day requires a timezone-aware datetime")
    return now.astimezone(timezone.utc).date()


def _require_transaction(conn: Connection) -> None:
    if conn.dialect.name != "postgresql" or not conn.in_transaction():
        raise RuntimeError("quota operations require an explicit PostgreSQL transaction")


def _lock_usage(conn: Connection, user_id: UUID, day: date):
    conn.execute(insert(quota_daily_usage).values(user_id=user_id, date=day)
                 .on_conflict_do_nothing(index_elements=["user_id", "date"]))
    return conn.execute(sa.select(quota_daily_usage).where(
        quota_daily_usage.c.user_id == user_id, quota_daily_usage.c.date == day
    ).with_for_update()).mappings().one()


def _reservation(conn: Connection, request_id: UUID):
    return conn.execute(sa.select(quota_reservations).where(
        quota_reservations.c.request_id == request_id
    ).with_for_update()).mappings().one_or_none()


def _same_request(row, user_id, day, count, task_id):
    if (row["user_id"], row["date"], row["count"], row["task_id"]) != (user_id, day, count, task_id):
        raise ValueError("request_id already belongs to a different reservation")


def reserve(conn: Connection, *, request_id: UUID, user_id: UUID, count: int,
            policy: EffectiveQuota, day: date | None = None, task_id: UUID | None = None) -> Mapping:
    """Reserve once, including unlimited usage; caller commits or rolls back.

    Existing IDs return their original terminal/pending state, even if policies
    changed. A failed ID cannot be reused to dispatch fresh work. task_id, when
    supplied, is also globally unique; a different request for it is rejected by
    the database. Task ownership must be validated by the future adapter.
    """
    _require_transaction(conn)
    if type(count) is not int or count <= 0:
        raise ValueError("count must be a positive integer")
    day = day or utc_day()
    usage = _lock_usage(conn, user_id, day)
    row = _reservation(conn, request_id)
    if row is not None:
        _same_request(row, user_id, day, count, task_id)
        return row
    check_capacity(policy, usage["used"], usage["reserved"], count)
    inserted = conn.execute(insert(quota_reservations).values(
        request_id=request_id, task_id=task_id, user_id=user_id, date=day, count=count
    ).on_conflict_do_nothing(index_elements=["request_id"]).returning(quota_reservations)).mappings().one_or_none()
    if inserted is None:  # Same key raced from a different usage row.
        row = _reservation(conn, request_id)
        _same_request(row, user_id, day, count, task_id)
        return row
    conn.execute(quota_daily_usage.update().where(
        quota_daily_usage.c.user_id == user_id, quota_daily_usage.c.date == day
    ).values(reserved=quota_daily_usage.c.reserved + count))
    return inserted


def settle(conn: Connection, request_id: UUID, *, succeeded: bool) -> Mapping:
    """First terminal outcome wins; failure releases, success charges once.

    Read immutable ownership without locking, then lock usage before reservation,
    the same order as reserve. Settle the original day even after UTC midnight.
    A repeated conflicting outcome returns the original state without charging.
    """
    _require_transaction(conn)
    if type(succeeded) is not bool:
        raise ValueError("succeeded must be boolean")
    original = conn.execute(sa.select(quota_reservations).where(
        quota_reservations.c.request_id == request_id)).mappings().one_or_none()
    if original is None:
        raise KeyError(request_id)
    usage = _lock_usage(conn, original["user_id"], original["date"])
    row = _reservation(conn, request_id)
    if row["status"] != "pending":
        return row
    if usage["reserved"] < row["count"]:
        raise RuntimeError("reservation counters are inconsistent")
    conn.execute(quota_daily_usage.update().where(
        quota_daily_usage.c.user_id == row["user_id"], quota_daily_usage.c.date == row["date"]
    ).values(reserved=quota_daily_usage.c.reserved - row["count"],
             used=quota_daily_usage.c.used + (row["count"] if succeeded else 0)))
    return conn.execute(quota_reservations.update().where(
        quota_reservations.c.request_id == request_id
    ).values(status="succeeded" if succeeded else "failed", settled_at=sa.func.now())
      .returning(quota_reservations)).mappings().one()


def settle_partial(conn: Connection, request_id: UUID, succeeded_count: int) -> Mapping:
    """Settle a reservation once, charging only completed items.

    The reservation becomes terminal even when only part succeeds: ``used`` is
    increased by succeeded_count and the complete reservation count is released
    from ``reserved``. Repeated calls return the original terminal row.
    """
    _require_transaction(conn)
    if type(succeeded_count) is not int or succeeded_count < 0:
        raise ValueError("succeeded_count must be a nonnegative integer")
    original = conn.execute(sa.select(quota_reservations).where(
        quota_reservations.c.request_id == request_id)).mappings().one_or_none()
    if original is None:
        raise KeyError(request_id)
    if succeeded_count > original["count"]:
        raise ValueError("succeeded_count exceeds reservation count")
    usage = _lock_usage(conn, original["user_id"], original["date"])
    row = _reservation(conn, request_id)
    if row["status"] != "pending":
        return row
    if usage["reserved"] < row["count"]:
        raise RuntimeError("reservation counters are inconsistent")
    conn.execute(quota_daily_usage.update().where(
        quota_daily_usage.c.user_id == row["user_id"], quota_daily_usage.c.date == row["date"]
    ).values(reserved=quota_daily_usage.c.reserved - row["count"],
             used=quota_daily_usage.c.used + succeeded_count))
    return conn.execute(quota_reservations.update().where(
        quota_reservations.c.request_id == request_id
    ).values(status="succeeded" if succeeded_count == row["count"] else "failed",
             settled_at=sa.func.now()).returning(quota_reservations)).mappings().one()
