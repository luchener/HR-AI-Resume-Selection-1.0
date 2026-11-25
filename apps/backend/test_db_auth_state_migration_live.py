"""Disposable PostgreSQL acceptance for the offline auth-state plan applier."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from unittest import TestCase, skipUnless
from uuid import uuid4

import sqlalchemy as sa
from cryptography.fernet import Fernet

from db_auth_state_migration import apply_auth_state_plan
from db_check_schema import metadata
from db_invite_cipher import InviteCipher
from db_invite_state import requests
from db_models import LegacyRecord, User
from db_public_auth_state import TABLES, login_state
from db_security import migration_engine
from db_test_guard import review_test_url

PLAN_KEY = Fernet.generate_key()


def _sealed(key, identity, email):
    return InviteCipher(key).encrypt("request", identity, {"email": email, "note": "Synthetic", "reject_reason": ""})


def _plan(uid, *, duplicate_tombstone=False):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pending = uuid4()
    approved = uuid4()
    key = PLAN_KEY
    old_hash = "d" * 64
    body = {
        "schema_version": 1, "evaluated_at": now.isoformat(), "source_inventory_sha256": "synthetic-source",
        "policy": {"old_invites": "invalidated", "old_challenges": "invalidated", "old_sessions": "rejected"},
        "requests": [
            {"request_id": str(pending), "email_hash": "a" * 64, "sealed": _sealed(key, pending, "pending@example.test"),
             "status": "pending", "created_at": now.isoformat(), "reviewed_at": None, "reviewed_by": None,
             "old_invite_hash": None, "old_code_sent": False, "resign_required": False},
            {"request_id": str(approved), "email_hash": "b" * 64, "sealed": _sealed(key, approved, "approved@example.test"),
             "status": "approved", "created_at": now.isoformat(), "reviewed_at": now.isoformat(),
             "reviewed_by": str(uid), "old_invite_hash": old_hash, "old_code_sent": True, "resign_required": True},
        ],
        "invite_tombstones": [{"old_code_hash": old_hash, "email_hash": "b" * 64, "request_id": str(approved),
                                "outcome": "invalidated_active", "created_at": now.isoformat(),
                                "expires_at": now.isoformat(), "used_by_user_id": None, "used_at": None,
                                "revoked_at": None}],
        "resign_queue": [{"request_id": str(approved), "email_hash": "b" * 64,
                           "sealed": _sealed(key, approved, "approved@example.test"),
                           "old_code_hash": old_hash, "old_outcome": "invalidated_active"}],
        "login_state": [{"user_id": str(uid), "failure_times": [{"at": now.isoformat(), "ip_hash": "synthetic-ip"}],
                          "last_failure_at": now.isoformat(), "consecutive_failures": 1, "frozen": True,
                          "frozen_by": "admin", "frozen_at": now.isoformat(), "frozen_reason": "synthetic"}],
        "freeze_projection": [], "rate_limits": [{"source_key_hash": "c" * 64, "started_at": now.isoformat(), "count": 2}],
        "invalidated_challenges": [{"purpose": "reset_password", "old_code_hash": "e" * 64,
                                     "email_hash": "a" * 64, "invalidated": True}],
        "rejection_counts": {"a" * 64: 2},
        "source_counts": {"invite_requests": 2, "invite_codes": 1, "invite_codes_audit": 0,
                           "login_failures": 1, "rate_limits": 1, "password_resets": 1},
        "counts": {"requests": 2, "invite_tombstones": 1, "resign_queue": 1, "login_state": 1,
                   "rate_limits": 1, "invalidated_challenges": 1},
    }
    if duplicate_tombstone:
        body["invite_tombstones"].append(dict(body["invite_tombstones"][0]))
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


@skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class AuthStateMigrationLiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ["DATABASE_TEST_URL"])
        cls.engine = migration_engine(url.render_as_string(hide_password=False))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.uid = uuid4()
        with self.engine.begin() as conn:
            conn.execute(User.__table__.insert().values(id=self.uid, username="auth-state-" + self.uid.hex,
                                                        password_hash="synthetic", password_salt="synthetic"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.engine.begin() as conn:
            conn.execute(LegacyRecord.__table__.delete().where(LegacyRecord.root_name == "__auth_state_migration__"))
            conn.execute(requests.delete().where(requests.c.email_hash.in_(["a" * 64, "b" * 64])))
            conn.execute(login_state.delete().where(login_state.c.user_id == self.uid))
            conn.execute(sa.delete(User).where(User.id == self.uid))

    def test_plan_writes_runtime_and_restricted_ledger_atomically(self):
        plan = _plan(self.uid)
        with self.engine.begin() as conn:
            result = apply_auth_state_plan(conn, plan, invite_key=PLAN_KEY)
            self.assertEqual(result, {"runtime_requests": 1, "login_state": 1, "ledger_records": 6, "approved_re_sign": 1})
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(requests).where(requests.c.email_hash == "a" * 64)), 1)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(login_state).where(login_state.c.user_id == self.uid)), 1)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(LegacyRecord).where(
                LegacyRecord.root_name == "__auth_state_migration__")), 6)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(metadata().tables["auth_challenges"])), 0)
            user = conn.execute(sa.select(User.__table__).where(User.id == self.uid)).mappings().one()
            self.assertEqual(user["login_consecutive_failures"], 1)
            self.assertEqual(user["login_window_failures"], 1)
            self.assertEqual(user["status"], "locked")
            self.assertEqual(user["frozen_by"], "admin")
            self.assertEqual(user["frozen_reason"], "synthetic")
            self.assertIsNotNone(user["frozen_at"])

    def test_late_ledger_failure_rolls_back_runtime_rows(self):
        plan = _plan(self.uid, duplicate_tombstone=True)
        with self.assertRaises(sa.exc.IntegrityError):
            with self.engine.begin() as conn:
                apply_auth_state_plan(conn, plan, invite_key=PLAN_KEY)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(requests).where(requests.c.email_hash == "a" * 64)), 0)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(login_state).where(login_state.c.user_id == self.uid)), 0)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(LegacyRecord).where(
                LegacyRecord.root_name == "__auth_state_migration__")), 0)
            user = conn.execute(sa.select(User.__table__).where(User.id == self.uid)).mappings().one()
            self.assertEqual(user["status"], "active")
            self.assertEqual(user["login_consecutive_failures"], 0)
            self.assertEqual(user["login_window_failures"], 0)
            self.assertIsNone(user["frozen_at"])
            self.assertIsNone(user["frozen_by"])

    def test_frozen_deleted_user_preserves_deleted_status_and_previous_state(self):
        plan = _plan(self.uid)
        with self.engine.begin() as conn:
            conn.execute(sa.update(User.__table__).where(User.id == self.uid).values(
                status="deleted", deleted_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                deletion_previous_status="active"))
            apply_auth_state_plan(conn, plan, invite_key=PLAN_KEY)
            user = conn.execute(sa.select(User.__table__).where(User.id == self.uid)).mappings().one()
            self.assertEqual(user["status"], "deleted")
            self.assertEqual(user["deletion_previous_status"], "locked")
            self.assertEqual(user["frozen_by"], "admin")

    def test_admin_freeze_without_failure_events_still_projects_user_state(self):
        plan = _plan(self.uid)
        plan["login_state"][0].update(failure_times=[], last_failure_at=None)
        canonical = json.dumps({k: v for k, v in plan.items() if k != "plan_sha256"},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        plan["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.engine.begin() as conn:
            result = apply_auth_state_plan(conn, plan, invite_key=PLAN_KEY)
            self.assertEqual(result["login_state"], 0)
            user = conn.execute(sa.select(User.__table__).where(User.id == self.uid)).mappings().one()
            self.assertEqual(user["status"], "locked")
            self.assertEqual(user["frozen_by"], "admin")
            self.assertEqual(user["login_consecutive_failures"], 1)
            self.assertEqual(user["login_window_failures"], 0)

    def test_sealed_envelope_must_verify_with_explicit_key_before_writes(self):
        plan = _plan(self.uid)
        plan["requests"][0]["sealed"] = dict(plan["requests"][0]["sealed"], token="tampered")
        canonical = json.dumps({k: v for k, v in plan.items() if k != "plan_sha256"},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        plan["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "envelope"):
            with self.engine.begin() as conn:
                apply_auth_state_plan(conn, plan, invite_key=PLAN_KEY)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(requests).where(
                requests.c.email_hash == "a" * 64)), 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
