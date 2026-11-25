"""Pure planning for auxiliary JSON migration; never reads/writes files or a DB.

``users`` are decoded auth user records, ``limits`` is quota/limits.json, and
``usage_by_user_id`` maps usage filename stems to decoded objects. Output rows
match db_quota.quota_policies and quota_daily_usage, NOT db_models.Quotas (which
cannot express inheritance or unlimited limits). Resolve policies using the
imported User.is_admin; do not materialize an administrator's effective limit.

Legacy usage day keys already denote UTC days; importing must not reinterpret
those keys in the host timezone. Legacy reservations are not identifiable from
these counters. Take a quiescent snapshot after draining analysis work before any
future apply: this planner cannot distinguish pending reservations from success.

SystemConfig.value accepts arbitrary JSON, but legacy mail passwords and LLM
keys are plaintext. No SystemConfig rows or source values are returned: every
system_config record remains quarantined pending a separate encrypted-secret
migration and runtime integration. AuditLog conversion is deliberately deferred:
its integer ID cannot represent the source filename identity, and request_id has
no unique constraint. A future converter/apply must preserve source filename plus
original request_id, use a durable unique source key, and redact free-text detail.
Counts/flags are safe to report; callers must not log decoded source payloads.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any
from uuid import UUID


class AuxiliaryConversionError(ValueError):
    """Invalid or ambiguous input; messages deliberately omit source values."""


def _object(value: Any, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise AuxiliaryConversionError(f"{what}: expected JSON object")
    return value


def _uuid(value: Any) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise AuxiliaryConversionError("missing or invalid user UUID") from None


def _user_index(users: Iterable[Mapping]) -> tuple[dict[str, UUID], set[UUID]]:
    by_name: dict[str, UUID] = {}
    ids: set[UUID] = set()
    for user in users:
        user = _object(user, "user")
        uid = _uuid(user.get("user_id"))
        name = user.get("username")
        if not isinstance(name, str) or not name:
            raise AuxiliaryConversionError("missing or invalid username")
        if name in by_name or uid in ids:
            raise AuxiliaryConversionError("duplicate user identity")
        by_name[name] = uid
        ids.add(uid)
    return by_name, ids


def _limit(value: Any, *, inherit: bool) -> tuple[str, int | None]:
    if value is None:
        return ("inherit" if inherit else "unlimited"), None
    # quota.py resolves persisted values with int(); reject broken values instead
    # of silently turning corrupt limits into a production unlimited policy.
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError):
        raise AuxiliaryConversionError("invalid daily limit") from None
    if parsed < 0:
        return "unlimited", None
    if parsed > 2_147_483_647:
        raise AuxiliaryConversionError("daily limit exceeds target integer range")
    return "limited", parsed


def _policy_rows(limits: Mapping | None, by_name: Mapping[str, UUID]) -> list[dict]:
    limits = {} if limits is None else _object(limits, "quota limits")
    mode, daily = _limit(limits.get("default_daily"), inherit=False)
    rows = [{
        "scope_key": "global", "user_id": None,
        "mode": mode, "daily_limit": daily, "enabled": True,
        "quota_enabled": bool(limits.get("quota_enabled", False)),
        "email_notify": bool(limits.get("email_notify_enabled", False)),
    }]
    overrides = _object(limits.get("users", {}), "quota user overrides")
    for name, cfg in overrides.items():
        # No case folding, whitespace trimming, UUID/name fallback or invented IDs.
        if name not in by_name:
            raise AuxiliaryConversionError("quota override has no exact username match")
        cfg = _object(cfg, "quota user override")
        mode, daily = _limit(cfg.get("daily_limit"), inherit=True)
        uid = by_name[name]
        rows.append({
            "scope_key": str(uid), "user_id": uid,
            "mode": mode, "daily_limit": daily,
            "enabled": bool(cfg.get("analysis_enabled", True)),
            "quota_enabled": None,
            "email_notify": bool(cfg.get("email_notify", True)),
        })
    return rows[:1] + sorted(rows[1:], key=lambda row: row["scope_key"])


def _usage_rows(usage: Mapping, known_ids: set[UUID]) -> list[dict]:
    usage = _object(usage, "usage files")
    rows = []
    seen_ids: set[UUID] = set()
    for source_id, payload in usage.items():
        uid = _uuid(source_id)
        if uid not in known_ids:
            raise AuxiliaryConversionError("usage has no matching user UUID")
        if uid in seen_ids:
            raise AuxiliaryConversionError("duplicate usage user UUID")
        seen_ids.add(uid)
        for key, entry in _object(payload, "user usage").items():
            try:
                day = date.fromisoformat(key)
                if day.isoformat() != key:
                    raise ValueError
            except (ValueError, TypeError):
                raise AuxiliaryConversionError("usage key must be a UTC YYYY-MM-DD date") from None
            entry = _object(entry, "daily usage")
            try:
                # Same nonnegative normalization as quota.today_analysis_count;
                # login and other counters are intentionally not quota usage.
                used = max(0, int(entry.get("analysis", 0)))
            except (ValueError, TypeError, OverflowError):
                raise AuxiliaryConversionError("invalid analysis usage count") from None
            if used > 2_147_483_647:
                raise AuxiliaryConversionError("analysis usage exceeds target integer range")
            rows.append({"user_id": uid, "date": day, "used": used, "reserved": 0})
    return sorted(rows, key=lambda row: (str(row["user_id"]), row["date"]))


def convert_auxiliary(
    *, users: Iterable[Mapping], limits: Mapping | None = None,
    usage_by_user_id: Mapping | None = None,
    system_configs: Mapping | None = None, admin_ops: Mapping | None = None,
) -> dict:
    """Return target quota rows and explicit deferred/quarantine counts.

    Inputs are decoded snapshot records, never paths. Keys for system_configs and
    admin_ops are source filenames; values are intentionally not examined/copied.
    No source dictionaries are mutated or returned. Errors fail the whole plan
    without echoing user identifiers, timestamps, config keys or secret values.
    This is a conversion plan only: it has no apply, SQL, runtime or CLI path.
    """
    by_name, ids = _user_index(users)
    configs = {} if system_configs is None else _object(system_configs, "system configs")
    audits = {} if admin_ops is None else _object(admin_ops, "admin operations")
    return {
        "quota_policies": _policy_rows(limits, by_name),
        "quota_daily_usage": _usage_rows({} if usage_by_user_id is None else usage_by_user_id, ids),
        "system_configs": [],
        "audit_logs": [],
        "quarantine": {"system_config": True, "system_config_records": len(configs)},
        "deferred": {"admin_ops": True, "admin_ops_records": len(audits)},
    }
