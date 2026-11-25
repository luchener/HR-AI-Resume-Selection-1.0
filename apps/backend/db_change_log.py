"""Durable, transaction-owned change log for a future shadow/dual-write path.

This module is deliberately opt-in.  Callers must pass an already-open
PostgreSQL connection and append the event in the same transaction as the
business mutation.  Delivery is at-least-once: a downstream consumer must
deduplicate by ``idempotency_key`` before applying an event.

Only bounded, structured JSON is accepted.  Raw exception text, credentials,
request bodies and provider responses must never be written to this table.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Mapping
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

from db_models import Base


metadata = sa.MetaData()
Base.metadata.tables["users"].to_metadata(metadata)

change_log = sa.Table(
    "change_log",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("aggregate_type", sa.String(32), nullable=False),
    sa.Column("aggregate_id", sa.String(128), nullable=False),
    sa.Column("sequence_no", sa.BigInteger(), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("payload", JSONB(), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("claimed_by", sa.String(128)),
    sa.Column("claimed_at", sa.DateTime(timezone=True)),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("delivered_at", sa.DateTime(timezone=True)),
    sa.Column("compensated_at", sa.DateTime(timezone=True)),
    sa.Column("compensated_by", sa.String(128)),
    sa.Column("last_error_code", sa.String(32)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("aggregate_type IN ('user','resume','job','analysis_task','analysis','archive','quota','config','invite')",
                       name="ck_change_log_aggregate_type"),
    sa.CheckConstraint("aggregate_id <> '' AND length(aggregate_id) <= 128", name="ck_change_log_aggregate_id"),
    sa.CheckConstraint("event_type ~ '^[a-z][a-z0-9_.-]{0,63}$'", name="ck_change_log_event_type"),
    sa.CheckConstraint("idempotency_key ~ '^[A-Za-z0-9:_./-]{1,128}$'", name="ck_change_log_idempotency"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_change_log_payload_object"),
    sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_change_log_payload_digest"),
    sa.CheckConstraint("state IN ('pending','in_flight','delivered','dead','compensated')", name="ck_change_log_state"),
    sa.CheckConstraint("(state = 'compensated' AND compensated_at IS NOT NULL AND compensated_by IS NOT NULL) OR "
                       "(state <> 'compensated' AND compensated_at IS NULL AND compensated_by IS NULL)",
                       name="ck_change_log_compensation"),
    sa.CheckConstraint("compensated_by IS NULL OR length(btrim(compensated_by)) BETWEEN 1 AND 128",
                       name="ck_change_log_compensated_by"),
    sa.CheckConstraint("attempts BETWEEN 0 AND 100", name="ck_change_log_attempts"),
    sa.CheckConstraint("last_error_code IS NULL OR last_error_code IN ('temporary','dependency_unavailable','timeout','conflict','unknown')",
                       name="ck_change_log_error_code"),
    sa.UniqueConstraint("aggregate_type", "aggregate_id", "sequence_no", name="uq_change_log_stream_sequence"),
    sa.UniqueConstraint("idempotency_key", name="uq_change_log_idempotency_key"),
    sa.Index("ix_change_log_claim", "state", "available_at", "lease_until", "created_at"),
    sa.Index("ix_change_log_stream", "aggregate_type", "aggregate_id", "sequence_no"),
)

_AGGREGATES = frozenset({"user", "resume", "job", "analysis_task", "analysis", "archive", "quota", "config", "invite"})
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_ERROR_CODES = frozenset({"temporary", "dependency_unavailable", "timeout", "conflict", "unknown"})
_FORBIDDEN_KEYS = frozenset({
    "password", "password_hash", "password_salt", "secret", "secret_key",
    "api_key", "apikey", "smtp_password", "token", "access_token",
    "refresh_token", "jwt", "authorization", "private_key", "passphrase",
    "credential", "credentials", "raw_content", "legacy_payload",
})


def _require_tx(conn) -> None:
    if getattr(getattr(conn, "dialect", None), "name", None) != "postgresql" or not conn.in_transaction():
        raise RuntimeError("change-log operations require an explicit PostgreSQL transaction")


def _uuid(value, label: str) -> UUID:
    try:
        result = value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} requires a UUID") from exc
    if result.int == 0:
        raise ValueError(f"{label} must be nonzero")
    return result


def _bounded_text(value, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _utc_now(now: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if now is None else now
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now requires timezone")
    return value.astimezone(timezone.utc)


def _payload_digest(payload: Mapping) -> tuple[dict, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("change payload must be a JSON object")
    value = deepcopy(dict(payload))
    def walk(value, depth=0):
        if depth > 8:
            raise ValueError("change payload nesting is too deep")
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
                    raise ValueError("change payload contains a restricted field")
                walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)

    walk(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("change payload must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("change payload exceeds 64 KiB")
    return value, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stream_lock(conn, aggregate_type: str, aggregate_id: str) -> None:
    # Transaction-scoped advisory lock closes the max(sequence)+1 race even
    # before the first event exists for a stream.
    conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:stream_key, 0))"),
                 {"stream_key": f"{aggregate_type}:{aggregate_id}"})


def append_change(conn, *, owner_user_id: UUID, aggregate_type: str, aggregate_id: str,
                  event_type: str, idempotency_key: str, payload: Mapping,
                  event_id: UUID | None = None) -> Mapping:
    """Append one ordered event, returning the existing row for a replay.

    The caller must commit the surrounding business mutation and this event
    together.  A replay with the same idempotency key is accepted only when all
    immutable identity and payload fields match.
    """
    _require_tx(conn)
    owner = _uuid(owner_user_id, "owner_user_id")
    if aggregate_type not in _AGGREGATES:
        raise ValueError("unsupported aggregate_type")
    aggregate_id = _bounded_text(aggregate_id, "aggregate_id", re.compile(r"^[^\x00\r\n]{1,128}$"))
    event_type = _bounded_text(event_type, "event_type", _EVENT_RE)
    idempotency_key = _bounded_text(idempotency_key, "idempotency_key", _IDEMPOTENCY_RE)
    payload, payload_sha256 = _payload_digest(payload)
    _stream_lock(conn, aggregate_type, aggregate_id)

    existing = conn.execute(sa.select(change_log).where(change_log.c.idempotency_key == idempotency_key)
                            .with_for_update()).mappings().one_or_none()
    if existing is not None:
        immutable = (existing["owner_user_id"], existing["aggregate_type"], existing["aggregate_id"],
                     existing["event_type"], existing["payload_sha256"])
        if immutable != (owner, aggregate_type, aggregate_id, event_type, payload_sha256):
            raise ValueError("idempotency key belongs to a different change")
        return existing

    sequence = conn.execute(sa.select(sa.func.coalesce(sa.func.max(change_log.c.sequence_no), 0) + 1)
                            .where(change_log.c.aggregate_type == aggregate_type,
                                   change_log.c.aggregate_id == aggregate_id)).scalar_one()
    values = {
        "id": event_id or uuid4(), "owner_user_id": owner,
        "aggregate_type": aggregate_type, "aggregate_id": aggregate_id,
        "sequence_no": int(sequence), "event_type": event_type,
        "idempotency_key": idempotency_key, "payload": payload,
        "payload_sha256": payload_sha256,
    }
    statement = insert(change_log).values(**values).on_conflict_do_nothing(
        constraint="uq_change_log_idempotency_key").returning(*change_log.c)
    row = conn.execute(statement).mappings().one_or_none()
    if row is not None:
        return row
    # A concurrent insert may have won the idempotency race.  PostgreSQL waits
    # for that transaction before this SELECT, so it is safe to read its row.
    row = conn.execute(sa.select(change_log).where(change_log.c.idempotency_key == idempotency_key)
                       .with_for_update()).mappings().one()
    immutable = (row["owner_user_id"], row["aggregate_type"], row["aggregate_id"],
                 row["event_type"], row["payload_sha256"])
    if immutable != (owner, aggregate_type, aggregate_id, event_type, payload_sha256):
        raise ValueError("idempotency key belongs to a different change")
    return row


def claim_changes(conn, *, consumer_id: str, limit: int = 50, lease_seconds: int = 60,
                  now: datetime | None = None) -> list[Mapping]:
    """Claim pending or expired events in commit-scoped leases."""
    _require_tx(conn)
    consumer_id = _bounded_text(consumer_id, "consumer_id", re.compile(r"^[A-Za-z0-9_.:-]{1,128}$"))
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    now = _utc_now(now)
    predecessor = change_log.alias("predecessor")
    # A stream is delivered in sequence order.  A later event must remain
    # pending while any earlier event is still pending or leased.  Without
    # this gate SKIP LOCKED could let a second worker claim sequence N+1
    # while sequence N was being retried, exposing an impossible state to the
    # shadow consumer.  A dead row is an explicit operator-visible fence and
    # continues to block the stream until it is marked compensated after the
    # downstream business state has been reconciled.
    unresolved_predecessor = sa.exists(sa.select(1).select_from(predecessor).where(
        predecessor.c.aggregate_type == change_log.c.aggregate_type,
        predecessor.c.aggregate_id == change_log.c.aggregate_id,
        predecessor.c.sequence_no < change_log.c.sequence_no,
        predecessor.c.state.not_in(("delivered", "compensated")),
    ))
    candidates = conn.execute(sa.select(change_log).where(
        sa.and_(
            change_log.c.attempts < 100,
            ~unresolved_predecessor,
            sa.or_(
                sa.and_(change_log.c.state == "pending", change_log.c.available_at <= now),
                sa.and_(change_log.c.state == "in_flight", change_log.c.lease_until < now),
            ),
        )
    ).order_by(change_log.c.created_at, change_log.c.id).limit(limit)
      .with_for_update(skip_locked=True)).mappings().all()
    claimed = []
    lease_until = now + timedelta(seconds=lease_seconds)
    for row in candidates:
        updated = conn.execute(change_log.update().where(
            change_log.c.id == row["id"],
            sa.or_(change_log.c.state == "pending",
                   sa.and_(change_log.c.state == "in_flight", change_log.c.lease_until < now)),
        ).values(state="in_flight", attempts=change_log.c.attempts + 1,
                 claimed_by=consumer_id, claimed_at=now, lease_until=lease_until,
                 updated_at=now).returning(*change_log.c)).mappings().one()
        claimed.append(updated)
    return claimed


def acknowledge_change(conn, *, event_id: UUID, consumer_id: str,
                       now: datetime | None = None) -> Mapping:
    """Mark a leased event delivered; ownership is checked under row lock."""
    _require_tx(conn)
    consumer_id = _bounded_text(consumer_id, "consumer_id", re.compile(r"^[A-Za-z0-9_.:-]{1,128}$"))
    now = _utc_now(now)
    row = conn.execute(sa.select(change_log).where(
        change_log.c.id == _uuid(event_id, "event_id"),
        change_log.c.state == "in_flight", change_log.c.claimed_by == consumer_id,
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        raise ValueError("event is not leased by this consumer")
    return conn.execute(change_log.update().where(change_log.c.id == row["id"]).values(
        state="delivered", delivered_at=now, claimed_by=None, claimed_at=None,
        lease_until=None, updated_at=now).returning(*change_log.c)).mappings().one()


def fail_change(conn, *, event_id: UUID, consumer_id: str, error_code: str,
                retry_delay_seconds: int = 30, max_attempts: int = 8,
                now: datetime | None = None) -> Mapping:
    """Retry with a fixed code, or fence the event as ``dead``.

    Error text is intentionally discarded.  A dead row keeps the stream
    fenced until an operator reconciles the downstream state and calls
    :func:`compensate_dead_change`.
    """
    _require_tx(conn)
    consumer_id = _bounded_text(consumer_id, "consumer_id", re.compile(r"^[A-Za-z0-9_.:-]{1,128}$"))
    if error_code not in _ERROR_CODES:
        raise ValueError("unsupported error_code")
    if type(retry_delay_seconds) is not int or not 0 <= retry_delay_seconds <= 86400:
        raise ValueError("retry_delay_seconds is out of bounds")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts is out of bounds")
    now = _utc_now(now)
    row = conn.execute(sa.select(change_log).where(
        change_log.c.id == _uuid(event_id, "event_id"),
        change_log.c.state == "in_flight", change_log.c.claimed_by == consumer_id,
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        raise ValueError("event is not leased by this consumer")
    terminal = row["attempts"] >= max_attempts
    return conn.execute(change_log.update().where(change_log.c.id == row["id"]).values(
        state="dead" if terminal else "pending",
        available_at=now if terminal else now + timedelta(seconds=retry_delay_seconds),
        last_error_code=error_code, claimed_by=None, claimed_at=None,
        lease_until=None, updated_at=now).returning(*change_log.c)).mappings().one()


def compensate_dead_change(conn, *, event_id: UUID, operator_id: str,
                           now: datetime | None = None) -> Mapping:
    """Resolve a dead event after its downstream effects were reconciled.

    Compensation is deliberately explicit: only a ``dead`` row can transition
    to ``compensated``, and the terminal transition is recorded with an
    operator identity and UTC timestamp.  The operator must perform any
    domain-specific repair before calling this function; this API only releases
    the ordered stream.
    """
    _require_tx(conn)
    operator_id = _bounded_text(
        operator_id, "operator_id", re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$"))
    now = _utc_now(now)
    row = conn.execute(sa.select(change_log).where(
        change_log.c.id == _uuid(event_id, "event_id"),
        change_log.c.state == "dead",
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        raise ValueError("event is not dead")
    return conn.execute(change_log.update().where(change_log.c.id == row["id"]).values(
        state="compensated", compensated_at=now, compensated_by=operator_id,
        claimed_by=None, claimed_at=None, lease_until=None,
        updated_at=now).returning(*change_log.c)).mappings().one()
