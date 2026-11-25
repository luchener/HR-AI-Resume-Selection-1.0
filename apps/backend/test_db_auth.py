"""Authentication contracts with mocked sessions and PostgreSQL SQL compilation.

No application/config import, connection, production database or filesystem store.
These tests do not establish live PostgreSQL isolation/locking behavior.
"""
import ast
import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

import db_auth
from db_auth import AuthService, InternalUser


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.factory = MagicMock(return_value=self.session)
        self.service = AuthService(self.factory)
        now = datetime.now(timezone.utc)
        self.row = dict(id=uuid4(), username="Alice", email="alice@example.com",
                        password_hash="hash", password_salt="salt", password_version=7,
                        is_admin=False, is_super_admin=False, status="active", deleted_at=None,
                        created_at=now, updated_at=now,
                        legacy_payload={"username": "stale", "password_hash": "old hash",
                                        "password_version": 1, "is_admin": True, "token": "secret"})

    def returned(self, row):
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        result.mappings.return_value.one.return_value = row
        return result

    def compile(self, statement):
        return statement.compile(dialect=postgresql.dialect())

    def test_exact_legacy_algorithm_without_importing_auth(self):
        # Execute just the two original function definitions, never auth's imports.
        source = ast.parse(Path(__file__).with_name("auth.py").read_text(encoding="utf-8"))
        nodes = [n for n in source.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"hash_password", "verify_password"}]
        import binascii
        import hashlib
        import hmac
        import os
        from typing import Optional
        namespace = dict(base64=base64, binascii=binascii, hashlib=hashlib, hmac=hmac,
                         os=os, Optional=Optional, _PBKDF2_ITERATIONS=200_000)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "legacy_password_only", "exec"), namespace)
        salt = base64.b64encode(bytes(range(16))).decode("ascii")
        for password in ("Password123", "中文密码9λ"):
            expected = namespace["hash_password"](password, salt)
            self.assertEqual(db_auth.hash_password(password, salt), expected)
            self.assertTrue(db_auth.verify_password(password, *expected))
            self.assertTrue(namespace["verify_password"](password, *db_auth.hash_password(password)))
            self.assertFalse(db_auth.verify_password(password + "x", *expected))
        self.assertFalse(db_auth.verify_password("Password123", "!", "not-ascii-λ"))

    def test_create_normalizes_email_and_never_accepts_admin_inputs(self):
        def execute(statement):
            values = self.compile(statement).params
            self.assertIsInstance(values["id"], UUID)
            self.assertEqual(values["email"], "alice@example.com")
            self.assertEqual(values["username"], "Alice")
            self.assertFalse(values["is_admin"])
            self.assertFalse(values["is_super_admin"])
            self.assertTrue(db_auth.verify_password("Password123", values["password_hash"], values["password_salt"]))
            return self.returned({**self.row, **values})
        self.session.execute.side_effect = execute
        public, error = self.service.create_user("Alice", "Password123", " Alice@Example.COM ")
        self.assertIsNone(error)
        self.assertEqual(public["email"], "alice@example.com")
        self.assertEqual(set(public), {"user_id", "username", "email", "password_version", "created_at", "updated_at"})
        self.session.begin.assert_called_once()
        self.session.__exit__.assert_called_once()
        for flag in ("is_admin", "is_super_admin", "status", "legacy_payload"):
            with self.assertRaises(TypeError):
                self.service.create_user("Alice", "Password123", **{flag: True})

    def test_invalid_registration_never_opens_session(self):
        for name, password, email in [("x", "Password123", ""), ("a b", "Password123", ""),
                                     ("Alice", "short1", ""), ("Alice", "12345678", ""),
                                     ("Alice", "abcdefgh", ""), ("Alice", "Password123", "bad")]:
            self.assertIsNotNone(self.service.create_user(name, password, email)[1])
        self.factory.assert_not_called()

    def test_username_validation_boundaries_and_blank_email(self):
        self.session.execute.return_value = self.returned(self.row)
        for username in ("用户", "a_", "ab-9", "a" * 64):
            self.assertIsNone(self.service.create_user(username, "Password123")[1])
            self.assertIsNone(self.compile(self.session.execute.call_args.args[0]).params["email"])
        for username in ("a" * 65, "ab\n", "ab/", " ab"):
            self.assertIsNotNone(self.service.create_user(username, "Password123")[1])

    def test_unique_constraints_rollback_and_unrelated_errors_propagate(self):
        for constraint, text in [("uq_users_username_lower", "用户名"), ("uq_users_email_lower", "邮箱")]:
            exc = IntegrityError("insert", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint)))
            self.session.execute.side_effect = exc
            value, error = self.service.create_user("Alice", "Password123")
            self.assertIsNone(value)
            self.assertIn(text, error)
            self.assertIs(self.session.begin.return_value.__exit__.call_args.args[0], IntegrityError)
        self.session.execute.side_effect = IntegrityError("insert", {}, Exception("unrelated"))
        with self.assertRaises(IntegrityError):
            self.service.create_user("Alice", "Password123")

    def test_find_returns_typed_authoritative_data_and_bound_queries(self):
        self.session.execute.return_value = self.returned(self.row)
        user = self.service.find_user_by_username("ALICE")
        self.assertIsInstance(user, InternalUser)
        self.assertEqual(user.password_version, 7)
        self.assertNotIn("password_hash", repr(user))
        self.assertEqual(user.to_public()["username"], "Alice")
        self.assertNotIn("secret", str(user.to_public()))
        self.assertFalse(user.is_admin)
        compiled = self.compile(self.session.execute.call_args.args[0])
        self.assertIn("lower(users.username)", str(compiled))
        self.assertIn("alice", compiled.params.values())
        self.service.find_user_by_email(" ALICE@Example.com ")
        self.assertIn("alice@example.com", self.compile(self.session.execute.call_args.args[0]).params.values())
        self.service.find_user_by_id(str(self.row["id"]))
        self.assertIn(self.row["id"], self.compile(self.session.execute.call_args.args[0]).params.values())

    def test_explicit_admin_policy_no_persisted_superadmin_authority(self):
        self.session.execute.return_value = self.returned({**self.row, "is_super_admin": True})
        self.assertFalse(self.service.find_user_by_username("Alice").is_super_admin)
        policy = AuthService(self.factory, admin_emails=[" ALICE@EXAMPLE.COM "])
        user = policy.find_user_by_username("Alice")
        self.assertTrue(user.is_super_admin)
        self.assertTrue(user.is_admin)
        self.assertNotIn("is_admin", user.to_public())
        self.session.execute.return_value = self.returned({**self.row, "is_admin": True})
        user = self.service.find_user_by_username("Alice")
        self.assertTrue(user.is_admin)
        self.assertFalse(user.is_super_admin)

    def test_absent_and_unavailable_users_do_full_dummy_work(self):
        for row in (None, {**self.row, "status": "locked"}, {**self.row, "status": "deleted"},
                    {**self.row, "status": "pending"}, {**self.row, "deleted_at": self.row["created_at"]}):
            self.session.execute.return_value = self.returned(row)
            with patch.object(db_auth, "hash_password", wraps=db_auth.hash_password) as dummy:
                self.assertIsNone(self.service.authenticate_user("Alice", "Password123"))
                dummy.assert_called_once_with("Password123")

    def test_authenticate_good_wrong_and_malformed_credentials(self):
        hashed, salt = db_auth.hash_password("Password123")
        self.session.execute.return_value = self.returned({**self.row, "password_hash": hashed, "password_salt": salt})
        public = self.service.authenticate_user("Alice", "Password123")
        self.assertEqual(public["password_version"], 7)
        self.assertNotIn("password_hash", public)
        self.assertIsNone(self.service.authenticate_user("Alice", "WrongPassword123"))
        self.session.execute.return_value = self.returned({**self.row, "password_hash": "malformed"})
        self.assertIsNone(self.service.authenticate_user("Alice", "Password123"))

    def test_password_change_locks_then_updates_authoritative_fields_atomically(self):
        self.session.execute.side_effect = [self.returned(self.row), MagicMock()]
        self.assertEqual(self.service.update_password(self.row["id"], "NewPassword123"), (True, None))
        lock, write = [call.args[0] for call in self.session.execute.call_args_list]
        self.assertIn("FOR UPDATE", str(self.compile(lock)))
        compiled = self.compile(write)
        self.assertEqual(compiled.params["password_version"], 8)
        self.assertTrue(db_auth.verify_password("NewPassword123", compiled.params["password_hash"], compiled.params["password_salt"]))
        self.assertNotIn("legacy_payload", str(compiled))
        self.assertIn(self.row["id"], compiled.params.values())
        self.session.begin.assert_called_once()
        self.assertEqual(self.row["legacy_payload"]["password_version"], 1)

    def test_password_change_missing_locked_deleted_does_not_write(self):
        for row in (None, {**self.row, "status": "locked"}, {**self.row, "deleted_at": self.row["created_at"]}):
            self.session.execute.reset_mock()
            self.session.execute.return_value = self.returned(row)
            self.assertFalse(self.service.update_password(self.row["id"], "NewPassword123")[0])
            self.assertEqual(self.session.execute.call_count, 1)

    def test_password_write_failure_exits_transaction_with_error(self):
        self.session.execute.side_effect = [self.returned(self.row), RuntimeError("write failed")]
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            self.service.update_password(self.row["id"], "NewPassword123")
        self.assertIs(self.session.begin.return_value.__exit__.call_args.args[0], RuntimeError)

    def test_invalid_uuid_and_weak_password_never_open_session(self):
        for value in ("", "invalid", UUID(int=0), None, 12):
            with self.assertRaises(ValueError):
                self.service.find_user_by_id(value)
            with self.assertRaises(ValueError):
                self.service.update_password(value, "Password123")
        self.assertFalse(self.service.update_password(uuid4(), "weak")[0])
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
