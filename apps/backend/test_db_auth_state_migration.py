"""Offline authentication-state conversion tests; no database or provider access."""
from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from db_auth_state_migration import _plan_digest, convert_auth_state
from db_invite_cipher import InviteCipher


class AuthStateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.legacy = []
        self.users = []
        self.uid = uuid.uuid4()
        self.other = uuid.uuid4()
        self.reviewed = uuid.uuid4()
        self.evaluated = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.hmac_key = b"H" * 32
        self.invite_key = Fernet.generate_key()
        self._put("users", str(self.uid), {"user_id": str(self.uid), "username": "alice",
                                            "password_hash": "hash", "password_salt": "salt"})
        self._put("users", str(self.reviewed), {"user_id": str(self.reviewed), "username": "reviewer",
                                                 "password_hash": "hash", "password_salt": "salt"})

    def _put(self, root: str, name: str, value: dict):
        path = f"{root}/{name}.json"
        if root == "users":
            self.users.append((Path(path), value))
        else:
            self.legacy.append((root, path, {"value": value}))

    def _base(self):
        rid_pending = uuid.uuid4()
        rid_rejected = uuid.uuid4()
        rid_approved = uuid.uuid4()
        old_hash = "a" * 64
        self._put("invite_requests", str(rid_pending), {
            "request_id": str(rid_pending), "email": "pending@example.test", "note": "Need access",
            "status": "pending", "created_at": "2026-01-01T00:00:00Z", "reviewed_by": None,
            "reviewed_at": None, "code_hash": None, "code_sent": False,
        })
        self._put("invite_requests", str(rid_rejected), {
            "request_id": str(rid_rejected), "email": "reject@example.test", "note": "Need access",
            "status": "rejected", "created_at": "2026-01-01T00:00:00Z", "reviewed_by": str(self.reviewed),
            "reviewed_at": "2026-01-01T01:00:00Z", "code_hash": None, "code_sent": False,
            "reject_reason": "No", "rejected_count": 2,
        })
        self._put("invite_requests", str(rid_approved), {
            "request_id": str(rid_approved), "email": "approved@example.test", "note": "Need access",
            "status": "approved", "created_at": "2026-01-01T00:00:00Z", "reviewed_by": str(self.reviewed),
            "reviewed_at": "2026-01-01T01:00:00Z", "code_hash": old_hash, "code_sent": True,
        })
        self._put("invite_codes", old_hash, {
            "code_hash": old_hash, "bound_email": "approved@example.test", "request_id": str(rid_approved),
            "created_at": "2026-01-01T01:00:00Z", "expires_at": self.evaluated.timestamp() + 3600,
            "used": False, "used_by_user_id": None, "revoked": False,
        })
        login_key = hashlib.sha256(b"alice").hexdigest()
        self._put("login_failures", login_key, {
            "username": "alice", "consecutive_failures": 1,
            "last_failure_at": self.evaluated.timestamp() - 100,
            "failures": [{"ts": self.evaluated.timestamp() - 100, "ip_hash": "a" * 24}],
            "frozen": True, "frozen_at": self.evaluated.timestamp() - 90,
            "frozen_by": "admin", "frozen_reason": "review",
        })
        self._put("rate_limits", "invite_req_email_alice@example.test", {
            "start": self.evaluated.timestamp() - 10, "count": 2,
        })
        reset_hash = "b" * 64
        self._put("password_resets", f"reset_password.{reset_hash}", {
            "email": "alice@example.test", "purpose": "reset_password", "code_hash": reset_hash,
            "created_at": self.evaluated.timestamp() - 20, "expires_at": self.evaluated.timestamp() + 20,
            "used": False, "attempts": 0,
        })
        return {"records": {"users": self.users}, "legacy_records": self.legacy,
                "inventory_sha256": "frozen-test-inventory"}

    def test_conversion_preserves_security_state_and_redacts_secrets(self):
        report = self._base()
        plan = convert_auth_state(report, hmac_key=self.hmac_key, invite_key=self.invite_key,
                                 evaluated_at=self.evaluated)
        self.assertEqual(plan["counts"], {"requests": 3, "invite_tombstones": 1, "resign_queue": 1,
                                          "login_state": 1, "rate_limits": 1, "invalidated_challenges": 1})
        self.assertEqual(plan["resign_queue"][0]["old_outcome"], "invalidated_active")
        self.assertTrue(plan["login_state"][0]["frozen"])
        self.assertEqual(plan["login_state"][0]["consecutive_failures"], 1)
        self.assertTrue(plan["invalidated_challenges"][0]["invalidated"])
        rendered = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("approved@example.test", rendered)
        self.assertNotIn("Need access", rendered)
        sealed = plan["requests"][2]["sealed"]
        self.assertEqual(InviteCipher(self.invite_key).decrypt("request", plan["requests"][2]["request_id"], sealed)["email"],
                         "approved@example.test")
        self.assertNotIn(self.hmac_key.decode(), rendered)
        self.assertEqual(_plan_digest(plan), plan["plan_sha256"])
        tampered = dict(plan, counts=dict(plan["counts"], requests=99))
        with self.assertRaisesRegex(ValueError, "digest"):
            _plan_digest(tampered)

    def test_pending_duplicate_email_blocks(self):
        duplicate_id = uuid.uuid4()
        self._put("invite_requests", str(duplicate_id), {
            "request_id": str(duplicate_id), "email": "pending@example.test", "note": "Duplicate",
            "status": "pending", "created_at": "2026-01-01T00:00:00Z", "reviewed_by": None,
            "reviewed_at": None, "code_hash": None, "code_sent": False,
        })
        with self.assertRaisesRegex(ValueError, "duplicate pending"):
            convert_auth_state(self._base(), hmac_key=self.hmac_key, invite_key=self.invite_key,
                               evaluated_at=self.evaluated)

    def test_missing_reviewer_mapping_blocks(self):
        report = self._base()
        report["records"]["users"][1][1]["user_id"] = str(self.other)
        with self.assertRaisesRegex(ValueError, "reviewer mapping"):
            convert_auth_state(report, hmac_key=self.hmac_key, invite_key=self.invite_key,
                               evaluated_at=self.evaluated)

    def test_future_failure_blocks_and_old_codes_never_reactivate(self):
        report = self._base()
        record = next(item for item in report["legacy_records"] if item[0] == "login_failures")
        value = record[2]["value"]
        value["failures"][0]["ts"] = self.evaluated.timestamp() + 1
        value["last_failure_at"] = value["failures"][0]["ts"]
        with self.assertRaisesRegex(ValueError, "future"):
            convert_auth_state(report, hmac_key=self.hmac_key, invite_key=self.invite_key,
                               evaluated_at=self.evaluated)

    def test_used_code_is_tombstone_and_not_resignable(self):
        report = self._base()
        code_record = next(item for item in report["legacy_records"] if item[0] == "invite_codes")
        value = code_record[2]["value"]
        value["used"] = True
        value["used_by_user_id"] = str(self.uid)
        value["used_at"] = self.evaluated.timestamp() - 10
        report["legacy_records"].remove(code_record)
        report["legacy_records"].append(("invite_codes_audit", "invite_codes_audit/" + "a" * 64 + ".json", {"value": value}))
        plan = convert_auth_state(report, hmac_key=self.hmac_key, invite_key=self.invite_key,
                                  evaluated_at=self.evaluated)
        self.assertEqual(plan["resign_queue"], [])
        self.assertEqual(plan["invite_tombstones"][0]["outcome"], "used")


if __name__ == "__main__":
    unittest.main()
