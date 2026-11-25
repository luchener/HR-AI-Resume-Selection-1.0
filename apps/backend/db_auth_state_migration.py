"""Offline conversion plan for the legacy JSON authentication state.

The converter is deliberately detached from SQLAlchemy and the application
configuration.  It reads one frozen :func:`db_migrate_json.inventory` report,
validates every authentication record, and emits a redacted plan.  Old codes,
email challenges, and legacy sessions are never made usable: invite codes are
represented by tombstones and approved requests are put on an explicit
re-sign queue.  Email addresses and request contents are HMACed/encrypted in
the plan; secrets and plaintext credentials are never returned.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from db_invite_cipher import InviteCipher


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PURPOSES = {"email_verify", "reset_password", "unfreeze"}
_RECORD_ROOTS = {
    "invite_requests", "invite_codes", "invite_codes_audit", "login_failures",
    "rate_limits", "password_resets",
}


def _fail(message: str) -> None:
    # Do not interpolate source values into validation errors.  A malformed
    # source may contain an email, token, or operator note.
    raise ValueError(message)


def _keyed(secret: bytes, namespace: str, value: str) -> str:
    return hmac.new(secret, (namespace + "\0" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def _validate_keys(hmac_key: bytes, invite_key: bytes) -> InviteCipher:
    if type(hmac_key) is not bytes or len(hmac_key) < 32:
        _fail("auth HMAC key must be at least 32 bytes")
    if type(invite_key) is not bytes:
        _fail("invite encryption key must be bytes")
    try:
        return InviteCipher(invite_key)
    except ValueError:
        _fail("invalid invite encryption key")


def _uuid(value: Any, *, zero_ok: bool = False) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail("invalid UUID in authentication state")
    if not zero_ok and not result.int:
        _fail("zero UUID in authentication state")
    return result


def _dt(value: Any, *, required: bool = True) -> datetime | None:
    if value is None or value == "":
        if required:
            _fail("missing authentication timestamp")
        return None
    if not isinstance(value, str):
        _fail("invalid authentication timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("invalid authentication timestamp")
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _epoch(value: Any, *, evaluated_at: datetime, required: bool = True, allow_future: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            _fail("missing authentication epoch")
        return None
    if type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) < 0:
        _fail("invalid authentication epoch")
    result = datetime.fromtimestamp(float(value) / 1000 if float(value) > 10**12 else float(value), timezone.utc)
    if result > evaluated_at and not allow_future:
        _fail("future authentication timestamp")
    return result


def _event_time(value: Any, *, evaluated_at: datetime, required: bool = True) -> datetime | None:
    """Parse the two timestamp formats used by the old JSON stores."""
    if isinstance(value, str):
        result = _dt(value, required=required)
        if result and result > evaluated_at:
            _fail("future authentication timestamp")
        return result
    return _epoch(value, evaluated_at=evaluated_at, required=required)


def _email(value: Any) -> str:
    if type(value) is not str or len(value) > 320:
        _fail("invalid authentication email")
    result = value.strip().lower()
    if not _EMAIL.fullmatch(result) or "\x00" in result or any(0xD800 <= ord(c) <= 0xDFFF for c in result):
        _fail("invalid authentication email")
    return result


def _record_map(report: Mapping[str, Any], root: str) -> list[tuple[str, dict]]:
    result: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for item_root, key, wrapped in report.get("legacy_records", []):
        if item_root != root:
            continue
        if key in seen:
            _fail("duplicate authentication source record")
        seen.add(key)
        if not isinstance(wrapped, dict) or set(wrapped) != {"value"} or not isinstance(wrapped["value"], dict):
            _fail("invalid authentication source record")
        result.append((key, wrapped["value"]))
    return result


def _path_stem(key: str, root: str) -> str:
    prefix = root + "/"
    if not key.startswith(prefix) or not key.endswith(".json"):
        _fail("invalid authentication source path")
    stem = key[len(prefix):-5]
    if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
        _fail("invalid authentication source path")
    return stem


def _user_index(report: Mapping[str, Any]) -> tuple[dict[str, uuid.UUID], set[uuid.UUID]]:
    names: dict[str, uuid.UUID] = {}
    ids: set[uuid.UUID] = set()
    for path, row in report.get("records", {}).get("users", []):
        if not isinstance(row, dict):
            _fail("invalid user source record")
        uid = _uuid(row.get("user_id"))
        username = row.get("username")
        if type(username) is not str or not username.strip():
            _fail("invalid user source identity")
        name = username.strip().lower()
        if name in names or uid in ids:
            _fail("duplicate user source identity")
        names[name], ids = uid, ids | {uid}
    return names, ids


def _resolve_reviewer(value: Any, known_ids: set[uuid.UUID], reviewer_map: Mapping[str, Any]) -> str:
    if value is None or value == "":
        _fail("missing authentication reviewer")
    raw = str(value)
    try:
        candidate = uuid.UUID(raw)
    except ValueError:
        candidate = None
    if candidate is not None and candidate in known_ids:
        return str(candidate)
    mapped = reviewer_map.get(raw)
    if mapped is None:
        _fail("authentication reviewer mapping required")
    resolved = _uuid(mapped)
    if resolved not in known_ids:
        _fail("authentication reviewer mapping is unknown")
    return str(resolved)


def _validate_request(path: str, row: dict, *, cipher: InviteCipher, hmac_key: bytes,
                      known_ids: set[uuid.UUID], reviewer_map: Mapping[str, Any], evaluated_at: datetime) -> dict:
    identity = _uuid(row.get("request_id"))
    if str(identity) != _path_stem(path, "invite_requests"):
        _fail("invite request identity does not match source path")
    email = _email(row.get("email"))
    status = row.get("status")
    if status not in {"pending", "approved", "rejected"}:
        _fail("invalid invite request status")
    note = row.get("note", "")
    if type(note) is not str or len(note) > 200 or "\x00" in note:
        _fail("invalid invite request note")
    created = _dt(row.get("created_at"))
    if created > evaluated_at:
        _fail("future invite request timestamp")
    reviewed_at = _dt(row.get("reviewed_at"), required=False)
    if reviewed_at and reviewed_at > evaluated_at:
        _fail("future invite review timestamp")
    reviewed_by = row.get("reviewed_by")
    sent = row.get("code_sent", False)
    if type(sent) is not bool:
        _fail("invalid invite delivery state")
    if status == "pending":
        if (reviewed_at is not None or reviewed_by not in (None, "") or row.get("code_hash") not in (None, "")
                or sent):
            _fail("pending invite request has review state")
    elif status == "rejected":
        if sent:
            _fail("rejected invite request has delivery state")
        if reviewed_at is None:
            _fail("reviewed invite request is missing review time")
        reviewed_by = _resolve_reviewer(reviewed_by, known_ids, reviewer_map)
    else:
        if reviewed_at is None:
            _fail("reviewed invite request is missing review time")
        reviewed_by = _resolve_reviewer(reviewed_by, known_ids, reviewer_map)
    code_hash = row.get("code_hash")
    if code_hash not in (None, "") and (type(code_hash) is not str or not _HEX64.fullmatch(code_hash.lower())):
        _fail("invalid legacy invite code hash")
    if status == "approved" and not code_hash:
        _fail("approved invite request is missing invite code")
    if status != "approved" and code_hash not in (None, ""):
        _fail("non-approved invite request has invite code")
    reason = row.get("reject_reason")
    if reason is not None and (type(reason) is not str or len(reason) > 500 or "\x00" in reason):
        _fail("invalid invite rejection reason")
    # The encrypted payload is intentionally the only place where the source
    # email and request text survive the plan.
    sealed = cipher.encrypt("request", identity, {"email": email, "note": note, "reject_reason": reason or ""})
    return {
        "request_id": str(identity),
        "email_hash": _keyed(hmac_key, "email", email),
        "sealed": sealed,
        "status": status,
        "created_at": created.isoformat(),
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        "reviewed_by": reviewed_by,
        "old_invite_hash": code_hash.lower() if code_hash else None,
        "old_code_sent": sent,
        "resign_required": status == "approved",
    }


def _validate_code(path: str, row: dict, *, root: str, known_ids: set[uuid.UUID], evaluated_at: datetime,
                   hmac_key: bytes) -> dict:
    code_hash = _path_stem(path, root).lower()
    if not _HEX64.fullmatch(code_hash) or str(row.get("code_hash", "")).lower() != code_hash:
        _fail("legacy invite code hash does not match source path")
    for field in ("used",):
        if type(row.get(field, False)) is not bool:
            _fail("invalid legacy invite code state")
    bound = row.get("bound_email", "")
    if bound:
        bound = _email(bound)
    else:
        bound = ""
    created = _dt(row.get("created_at"))
    if created > evaluated_at:
        _fail("future legacy invite timestamp")
    expires = _epoch(row.get("expires_at"), evaluated_at=evaluated_at, allow_future=True)
    used_by = row.get("used_by_user_id")
    if used_by not in (None, ""):
        used_by = _uuid(used_by)
        if used_by not in known_ids:
            _fail("legacy invite consumer is unknown")
        used_by = str(used_by)
    used_at = _event_time(row.get("used_at"), evaluated_at=evaluated_at, required=False)
    if row.get("used") and (used_by is None or used_at is None):
        _fail("used invite is missing consumption state")
    if not row.get("used") and (used_by is not None or used_at is not None):
        _fail("unused invite has consumption state")
    request_id = row.get("request_id")
    if request_id not in (None, ""):
        request_id = str(_uuid(request_id))
    revoked_at = _event_time(row.get("revoked_at"), evaluated_at=evaluated_at, required=False)
    revoked = row.get("revoked", False)
    if type(revoked) is not bool:
        _fail("invalid legacy invite revocation state")
    if root == "invite_codes" and row.get("used"):
        _fail("used invite remains in active source directory")
    if root == "invite_codes_audit" and not (row.get("used") or revoked or revoked_at):
        _fail("audit invite has no terminal state")
    if row.get("used"):
        outcome = "used"
    elif revoked or revoked_at:
        outcome = "revoked"
    elif expires <= evaluated_at:
        outcome = "expired"
    else:
        outcome = "invalidated_active"
    return {
        "old_code_hash": code_hash,
        "email_hash": _keyed(hmac_key, "email", bound) if bound else None,
        "request_id": request_id,
        "outcome": outcome,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "used_by_user_id": used_by,
        "used_at": used_at.isoformat() if used_at else None,
        "revoked_at": revoked_at.isoformat() if revoked_at else None,
    }


def _validate_login(path: str, row: dict, *, usernames: Mapping[str, uuid.UUID], evaluated_at: datetime) -> dict:
    stem = _path_stem(path, "login_failures").lower()
    candidates = [name for name in usernames if hashlib.sha256(name.encode("utf-8")).hexdigest() == stem]
    if len(candidates) != 1:
        _fail("login failure record has no unique user mapping")
    username = candidates[0]
    embedded = row.get("username")
    if embedded not in (None, "") and (type(embedded) is not str or embedded.strip().lower() != username):
        _fail("login failure username does not match source key")
    if type(row.get("frozen", False)) is not bool:
        _fail("invalid login freeze flag")
    frozen_by = row.get("frozen_by", "auto")
    if frozen_by not in {"auto", "admin"}:
        _fail("invalid login freeze origin")
    reason = row.get("frozen_reason", "")
    if type(reason) is not str or len(reason) > 512 or "\x00" in reason:
        _fail("invalid login freeze reason")
    consecutive = row.get("consecutive_failures", 0)
    if type(consecutive) is not int or consecutive < 0 or consecutive > 2_147_483_647:
        _fail("invalid login failure count")
    failures = row.get("failures", [])
    if type(failures) is not list or len(failures) > 64:
        _fail("invalid login failure sequence")
    converted = []
    for failure in failures:
        if type(failure) is not dict:
            _fail("invalid login failure event")
        at = _epoch(failure.get("ts"), evaluated_at=evaluated_at)
        ip_hash = failure.get("ip_hash", "")
        if (type(ip_hash) is not str or len(ip_hash) > 128 or "\x00" in ip_hash or
                (ip_hash != "unknown" and not re.fullmatch(r"[0-9a-fA-F]{24,128}", ip_hash))):
            _fail("invalid login failure source")
        converted.append({"at": at.isoformat(), "ip_hash": ip_hash})
    frozen_at = _epoch(row.get("frozen_at"), evaluated_at=evaluated_at, required=False)
    if row.get("frozen") and frozen_at is None:
        _fail("frozen login state is missing timestamp")
    if not row.get("frozen") and frozen_at is not None:
        _fail("unfrozen login state has freeze timestamp")
    last = _epoch(row.get("last_failure_at"), evaluated_at=evaluated_at, required=False)
    if converted:
        latest = max(item["at"] for item in converted)
        if last is None or last.isoformat() != latest:
            _fail("login failure sequence and last failure disagree")
    elif last is not None:
        _fail("login state has last failure without sequence")
    return {
        "user_id": str(usernames[username]),
        "failure_times": converted,
        "last_failure_at": last.isoformat() if last else None,
        "consecutive_failures": consecutive,
        "frozen": row.get("frozen", False),
        "frozen_by": frozen_by if row.get("frozen") else None,
        "frozen_at": frozen_at.isoformat() if frozen_at else None,
        "frozen_reason": reason if row.get("frozen") else "",
    }


def _validate_rate(path: str, row: dict, *, evaluated_at: datetime, hmac_key: bytes) -> dict:
    source_key = _path_stem(path, "rate_limits")
    start = row.get("start")
    if type(start) not in (int, float) or not math.isfinite(float(start)) or float(start) < 0:
        _fail("invalid rate-limit start")
    start_at = datetime.fromtimestamp(float(start), timezone.utc)
    if start_at > evaluated_at:
        _fail("future rate-limit timestamp")
    count = row.get("count")
    if type(count) is not int or count <= 0 or count > 2_147_483_647:
        _fail("invalid rate-limit count")
    return {"source_key_hash": _keyed(hmac_key, "rate-limit-key", source_key),
            "started_at": start_at.isoformat(), "count": count}


def _validate_reset(path: str, row: dict, *, evaluated_at: datetime, hmac_key: bytes) -> dict:
    stem = _path_stem(path, "password_resets")
    pieces = stem.split(".")
    if len(pieces) != 2 or pieces[0] not in _PURPOSES or not _HEX64.fullmatch(pieces[1].lower()):
        _fail("invalid password challenge source path")
    if str(row.get("purpose")) != pieces[0] or str(row.get("code_hash", "")).lower() != pieces[1].lower():
        _fail("password challenge does not match source path")
    email = _email(row.get("email"))
    for name in ("created_at", "expires_at"):
        value = row.get(name)
        if type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) < 0:
            _fail("invalid password challenge timestamp")
        if name == "created_at" and datetime.fromtimestamp(float(value), timezone.utc) > evaluated_at:
            _fail("future password challenge timestamp")
    if type(row.get("used", False)) is not bool:
        _fail("invalid password challenge state")
    attempts = row.get("attempts", 0)
    if type(attempts) is not int or not 0 <= attempts <= 5:
        _fail("invalid password challenge attempts")
    return {"purpose": pieces[0], "old_code_hash": pieces[1].lower(),
            "email_hash": _keyed(hmac_key, "email", email), "invalidated": True}


def convert_auth_state(report: Mapping[str, Any], *, hmac_key: bytes, invite_key: bytes,
                       reviewer_map: Mapping[str, Any] | None = None,
                       evaluated_at: datetime | None = None) -> dict[str, Any]:
    """Validate a frozen inventory and return a redacted offline conversion plan.

    ``reviewer_map`` explicitly maps legacy operator identifiers to current
    user UUIDs.  There is no implicit "map to the first admin" fallback.
    """
    cipher = _validate_keys(hmac_key, invite_key)
    reviewer_map = reviewer_map or {}
    if not isinstance(reviewer_map, Mapping):
        _fail("reviewer mapping must be an object")
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    usernames, known_ids = _user_index(report)
    requests = [_validate_request(path, row, cipher=cipher, hmac_key=hmac_key,
                                  known_ids=known_ids, reviewer_map=reviewer_map, evaluated_at=evaluated_at)
                for path, row in _record_map(report, "invite_requests")]
    pending_emails: set[str] = set()
    for item in requests:
        if item["status"] == "pending":
            if item["email_hash"] in pending_emails:
                _fail("duplicate pending invite email")
            pending_emails.add(item["email_hash"])

    codes = []
    code_by_hash: dict[str, dict] = {}
    for root in ("invite_codes", "invite_codes_audit"):
        for path, row in _record_map(report, root):
            converted = _validate_code(path, row, root=root, known_ids=known_ids, evaluated_at=evaluated_at, hmac_key=hmac_key)
            if converted["old_code_hash"] in code_by_hash:
                _fail("duplicate legacy invite code")
            code_by_hash[converted["old_code_hash"]] = converted
            codes.append(converted)
    request_by_id = {item["request_id"]: item for item in requests}
    resign = []
    for item in requests:
        old = item["old_invite_hash"]
        if item["status"] == "approved":
            if old not in code_by_hash:
                _fail("approved invite is missing source code record")
            code = code_by_hash[old]
            if code["outcome"] == "used":
                item["resign_required"] = False
            else:
                resign.append({"request_id": item["request_id"], "email_hash": item["email_hash"],
                               "sealed": item["sealed"], "old_code_hash": old, "old_outcome": code["outcome"]})
        elif old:
            _fail("non-approved invite request references a code")
    for code in codes:
        if code["request_id"] and code["request_id"] not in request_by_id:
            _fail("legacy invite references unknown request")

    login = [_validate_login(path, row, usernames=usernames, evaluated_at=evaluated_at)
             for path, row in _record_map(report, "login_failures")]
    login_users = [item["user_id"] for item in login]
    if len(login_users) != len(set(login_users)):
        _fail("duplicate login state for user")
    rates = [_validate_rate(path, row, evaluated_at=evaluated_at, hmac_key=hmac_key)
             for path, row in _record_map(report, "rate_limits")]
    rate_keys = [item["source_key_hash"] for item in rates]
    if len(rate_keys) != len(set(rate_keys)):
        _fail("duplicate rate-limit state")
    resets = [_validate_reset(path, row, evaluated_at=evaluated_at, hmac_key=hmac_key)
              for path, row in _record_map(report, "password_resets")]
    reset_keys = [(item["purpose"], item["old_code_hash"]) for item in resets]
    if len(reset_keys) != len(set(reset_keys)):
        _fail("duplicate password challenge")

    rejection_counts: dict[str, int] = {}
    for item in requests:
        if item["status"] == "rejected":
            rejection_counts[item["email_hash"]] = rejection_counts.get(item["email_hash"], 0) + 1
    source_counts = {root: len(_record_map(report, root)) for root in sorted(_RECORD_ROOTS)}
    plan: dict[str, Any] = {
        "schema_version": 1,
        "evaluated_at": evaluated_at.isoformat(),
        "source_inventory_sha256": report.get("inventory_sha256"),
        "policy": {"old_invites": "invalidated", "old_challenges": "invalidated", "old_sessions": "rejected"},
        "requests": requests,
        "invite_tombstones": sorted(codes, key=lambda item: item["old_code_hash"]),
        "resign_queue": sorted(resign, key=lambda item: item["request_id"]),
        "login_state": sorted(login, key=lambda item: item["user_id"]),
        "freeze_projection": [item for item in login if item["frozen"]],
        "rate_limits": sorted(rates, key=lambda item: item["source_key_hash"]),
        "invalidated_challenges": sorted(resets, key=lambda item: (item["purpose"], item["old_code_hash"])),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "source_counts": source_counts,
        "counts": {"requests": len(requests), "invite_tombstones": len(codes), "resign_queue": len(resign),
                   "login_state": len(login), "rate_limits": len(rates), "invalidated_challenges": len(resets)},
    }
    # The digest is over the redacted, deterministic payload and contains no key
    # material.  It is used to bind the plan to an evidence record.
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


def _plan_digest(plan: Mapping[str, Any]) -> str:
    body = dict(plan)
    digest = body.pop("plan_sha256", None)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if type(digest) is not str or not hmac.compare_digest(digest, calculated):
        _fail("authentication conversion plan digest mismatch")
    return calculated


def _plan_datetime(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    return _dt(value)


def apply_auth_state_plan(session, plan: Mapping[str, Any], *, invite_key: bytes) -> dict[str, int]:
    """Apply a validated conversion plan to a disposable SQL target.

    The caller owns the transaction.  The explicit invitation key re-authenticates
    every sealed request before any row is written. Pending/rejected requests and login
    failure state enter their runtime tables.  Approved requests are kept in a
    restricted migration ledger until an operator explicitly issues a new
    invitation.  Legacy codes/challenges, rate windows, and the re-sign queue
    are ledger records only, so no old credential becomes consumable.
    """
    if not hasattr(session, "execute") or not session.in_transaction():
        raise ValueError("auth-state apply requires an open caller transaction")
    try:
        cipher = InviteCipher(invite_key)
    except ValueError:
        _fail("explicit invitation encryption key is required")
    if plan.get("schema_version") != 1:
        _fail("unsupported authentication conversion plan")
    _plan_digest(plan)
    requests_plan = plan.get("requests")
    if type(requests_plan) is not list:
        _fail("invalid authentication request plan")
    for item in requests_plan:
        if type(item) is not dict or item.get("status") not in {"pending", "approved", "rejected"}:
            _fail("invalid authentication request plan")
    from sqlalchemy import insert, select, text, update
    from db_invite_state import requests as invite_requests
    from db_models import LegacyRecord, User
    from db_public_auth_state import login_state

    session.execute(text("SELECT pg_advisory_xact_lock(710413507)"))
    existing = session.scalar(select(LegacyRecord.id).where(LegacyRecord.root_name == "__auth_state_migration__").limit(1))
    if existing is not None:
        _fail("authentication state conversion already applied")
    request_ids: set[str] = set()
    runtime_requests = []
    for item in requests_plan:
        request_id = str(_uuid(item.get("request_id")))
        if request_id in request_ids:
            _fail("duplicate authentication request in plan")
        request_ids.add(request_id)
        if type(item.get("email_hash")) is not str or not _HEX64.fullmatch(item["email_hash"]):
            _fail("invalid request email hash")
        if type(item.get("sealed")) is not dict:
            _fail("missing encrypted request payload")
        try:
            cipher.decrypt("request", str(_uuid(item.get("request_id"))), item["sealed"])
        except ValueError:
            _fail("authentication request envelope failed verification")
        if item["status"] == "approved":
            continue  # restricted re-sign ledger below
        runtime_requests.append({
            "id": uuid.UUID(request_id), "email_hash": item["email_hash"], "sealed": item["sealed"],
            "status": item["status"], "created_at": _plan_datetime(item.get("created_at")),
            "reviewed_at": _plan_datetime(item.get("reviewed_at"), nullable=True),
            "reviewed_by": uuid.UUID(item["reviewed_by"]) if item.get("reviewed_by") else None,
            "invite_hash": None, "code_sent": False,
        })
    if runtime_requests:
        session.execute(insert(invite_requests), runtime_requests)

    login_rows = []
    seen_users: set[uuid.UUID] = set()
    for item in plan.get("login_state", []):
        if type(item) is not dict:
            _fail("invalid login state plan")
        user_id = _uuid(item.get("user_id"))
        if user_id in seen_users:
            _fail("duplicate login state in plan")
        seen_users.add(user_id)
        failure_times = item.get("failure_times")
        if type(failure_times) is not list:
            _fail("invalid login failure sequence plan")
        last = item.get("last_failure_at")
        if not failure_times:
            if last is not None:
                _fail("login state has last failure without events")
        elif last is None:
            _fail("login state is missing last failure")
        consecutive = item.get("consecutive_failures")
        if type(consecutive) is not int or consecutive < 0 or consecutive > 2_147_483_647:
            _fail("invalid login consecutive failure count")
        frozen = item.get("frozen")
        if type(frozen) is not bool:
            _fail("invalid login freeze flag")
        frozen_at = _plan_datetime(item.get("frozen_at"), nullable=True)
        frozen_by = item.get("frozen_by")
        frozen_reason = item.get("frozen_reason", "")
        if frozen:
            if frozen_at is None or frozen_by not in {"auto", "admin"}:
                _fail("invalid frozen login projection")
            if type(frozen_reason) is not str or len(frozen_reason) > 512 or "\x00" in frozen_reason:
                _fail("invalid frozen login reason")
        elif frozen_at is not None or frozen_by is not None or frozen_reason not in ("", None):
            _fail("unfrozen login state has freeze projection")

        # The login-failure record is also the source of truth for the user
        # security projection.  Lock the user row before changing either
        # runtime table so a concurrent login cannot observe a half-applied
        # conversion.  Deleted users retain status=deleted; their historical
        # pre-delete state is represented by deletion_previous_status.
        user = session.execute(select(User.__table__).where(User.id == user_id)
                               .with_for_update()).mappings().one_or_none()
        if user is None:
            _fail("authentication state user is missing")
        values = {
            "login_consecutive_failures": consecutive,
            "login_window_failures": len(failure_times),
        }
        if frozen:
            values.update(frozen_at=frozen_at, frozen_by=frozen_by,
                          frozen_reason=frozen_reason)
            if user["status"] == "deleted":
                values["deletion_previous_status"] = "locked"
            else:
                values["status"] = "locked"
        elif user["status"] == "locked" and user["frozen_by"] == "auto":
            # An explicitly unfrozen source record may clear an automatic
            # lock.  Never clear an administrator lock here.
            values.update(status="active", frozen_at=None, frozen_by=None, frozen_reason="")
        session.execute(update(User.__table__).where(User.id == user_id).values(**values))
        if failure_times:
            login_rows.append({"user_id": user_id, "failure_times": failure_times,
                               "last_failure_at": _plan_datetime(last)})
    if login_rows:
        session.execute(insert(login_state), login_rows)

    ledger: list[tuple[str, dict]] = [
        ("plan", {"plan_sha256": plan["plan_sha256"], "source_inventory_sha256": plan.get("source_inventory_sha256"),
                  "schema_version": plan["schema_version"]}),
        ("rejection_counts", plan.get("rejection_counts", {})),
        ("rate_limits", {"items": plan.get("rate_limits", [])}),
        ("invalidated_challenges", {"items": plan.get("invalidated_challenges", [])}),
    ]
    for item in plan.get("invite_tombstones", []):
        if type(item) is not dict or not _HEX64.fullmatch(str(item.get("old_code_hash", ""))):
            _fail("invalid invite tombstone")
        ledger.append(("invite_tombstone/" + item["old_code_hash"], item))
    for item in plan.get("resign_queue", []):
        if type(item) is not dict or not item.get("request_id") or type(item.get("sealed")) is not dict:
            _fail("invalid re-sign queue item")
        request_id = str(_uuid(item["request_id"]))
        try:
            cipher.decrypt("request", request_id, item["sealed"])
        except ValueError:
            _fail("re-sign envelope failed verification")
        ledger.append(("resign/" + request_id, item))
    for root, payload in ledger:
        session.execute(insert(LegacyRecord).values(root_name="__auth_state_migration__", record_key=root,
                                                   payload=payload))
    return {"runtime_requests": len(runtime_requests), "login_state": len(login_rows),
            "ledger_records": len(ledger), "approved_re_sign": len(plan.get("resign_queue", []))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--hmac-key-file", type=Path, required=True)
    parser.add_argument("--invite-key-file", type=Path, required=True)
    parser.add_argument("--reviewer-map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from db_migrate_json import inventory, validate_inventory
        report = inventory(args.data_dir.resolve())
        validate_inventory(report)
        hmac_key = args.hmac_key_file.read_bytes()
        invite_key = args.invite_key_file.read_bytes()
        reviewer_map = json.loads(args.reviewer_map.read_text(encoding="utf-8")) if args.reviewer_map else {}
        plan = convert_auth_state(report, hmac_key=hmac_key, invite_key=invite_key, reviewer_map=reviewer_map)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(args.output.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, args.output)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        print("Authentication state conversion blocked; inspect the protected frozen snapshot.", file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "plan_sha256": plan["plan_sha256"], "counts": plan["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
