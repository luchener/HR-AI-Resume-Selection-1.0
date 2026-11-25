"""Isolated secret boundary tests; no production app or external database."""
import copy
import io
import json
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from db_models import SystemConfig
from db_secrets import (CONFIG_KEY, ConfigCipher, ConfigSecretError,
                        EncryptedConfigRepository, rotate_envelope)


class CipherTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.cipher = ConfigCipher(self.key, key_id="external-v1")
        self.payload = {"smtp_password": "SECRET-mail-password",
                        "llm": {"api_key": "SECRET-llm-key", "enabled": True},
                        "unknown": [None, 1, "中文"]}

    def test_roundtrip_whole_payload_and_nondeterministic_encryption(self):
        original = copy.deepcopy(self.payload)
        envelope = self.cipher.encrypt(self.payload)
        self.assertEqual(set(envelope), {"version", "key_id", "ciphertext"})
        self.assertEqual(self.cipher.decrypt(envelope), original)
        self.assertEqual(self.payload, original)
        self.assertNotEqual(envelope, self.cipher.encrypt(original))
        for secret in ("SECRET-mail-password", "SECRET-llm-key"):
            self.assertNotIn(secret, json.dumps(envelope))
        self.assertNotIn(self.key.decode(), json.dumps(envelope))
        self.assertNotIn(self.key.decode(), repr(self.cipher))

    def test_environment_key_required_with_no_fallback(self):
        for source in ({}, {"DATABASE_CONFIG_KEY": ""}, {"DATABASE_CONFIG_KEY": "bad-secret"}):
            with self.subTest(source=bool(source)), self.assertRaises(ConfigSecretError):
                ConfigCipher.from_env(key_id="v1", environ=source)
        with patch.dict("os.environ", {"DATABASE_CONFIG_KEY": self.key.decode()}, clear=True):
            cipher = ConfigCipher.from_env(key_id="external-v1")
        self.assertEqual(cipher.decrypt(self.cipher.encrypt(self.payload)), self.payload)

    def test_invalid_keys_and_ids_have_safe_errors(self):
        for key in (None, "", "SECRET-invalid-key", 123, "秘密"):
            with self.subTest(key_type=type(key).__name__), self.assertRaises(ConfigSecretError) as caught:
                ConfigCipher(key, key_id="v1")
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
        for label in (None, "", " ", "x" * 129, 1):
            with self.assertRaises(ConfigSecretError):
                ConfigCipher(self.key, key_id=label)

    def test_wrong_key_plaintext_and_envelope_tampering_rejected(self):
        envelope = self.cipher.encrypt(self.payload)
        wrong = ConfigCipher(Fernet.generate_key(), key_id=self.cipher.key_id)
        with self.assertRaises(ConfigSecretError):
            wrong.decrypt(envelope)
        bad_envelopes = [None, self.payload, {}, {**envelope, "version": 2},
                         {**envelope, "version": True}, {**envelope, "key_id": "other"},
                         {**envelope, "ciphertext": "SECRET-tamper"},
                         {**envelope, "ciphertext": "秘密"},
                         {**envelope, "ciphertext": envelope["ciphertext"][:-8]},
                         {**envelope, "extra": "SECRET-extra"}]
        for bad in bad_envelopes:
            with self.subTest(kind=type(bad).__name__), self.assertRaises(ConfigSecretError) as caught:
                self.cipher.decrypt(bad)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_authenticated_inner_context_and_metadata_required(self):
        protected = {"context": CONFIG_KEY, "version": 1,
                     "key_id": self.cipher.key_id, "payload": self.payload}
        for changes in ({"context": "other"}, {"key_id": "other"}, {"version": True},
                        {"payload": "SECRET-string"}, {"extra": True}):
            token = Fernet(self.key).encrypt(json.dumps({**protected, **changes}).encode()).decode()
            envelope = {"version": 1, "key_id": self.cipher.key_id, "ciphertext": token}
            with self.assertRaises(ConfigSecretError):
                self.cipher.decrypt(envelope)

    def test_invalid_payloads_rejected_without_echoing_data(self):
        for value in (None, [], "SECRET", {"secret": object()}, {"secret": float("nan")}):
            with self.assertRaises(ConfigSecretError) as caught:
                self.cipher.encrypt(value)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_rotation_uses_external_keys_only(self):
        new = ConfigCipher(Fernet.generate_key(), key_id="external-v2")
        old_envelope = self.cipher.encrypt(self.payload)
        original = copy.deepcopy(old_envelope)
        rotated = rotate_envelope(old_envelope, old=self.cipher, new=new)
        self.assertEqual(new.decrypt(rotated), self.payload)
        self.assertEqual(old_envelope, original)
        with self.assertRaises(ConfigSecretError):
            self.cipher.decrypt(rotated)
        with self.assertRaises(ConfigSecretError):
            rotate_envelope(old_envelope, old=new, new=self.cipher)
        with self.assertRaises(ConfigSecretError):
            rotate_envelope(old_envelope, old=self.cipher, new=self.cipher)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", hide_parameters=True)
        # Test-local compatible table, without changing the PostgreSQL model.
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE system_configs (key TEXT PRIMARY KEY, value JSON NOT NULL, "
                "updated_by TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        self.session = Session(self.engine)
        self.cipher = ConfigCipher(Fernet.generate_key(), key_id="v1")
        self.new = ConfigCipher(Fernet.generate_key(), key_id="v2")
        self.repo = EncryptedConfigRepository(self.session, self.cipher)
        self.payload = {"password": "SECRET-database-value", "unknown": {"keep": 1}}
        self.parameters = []
        event.listen(self.engine, "before_cursor_execute", self.record_parameters)

    def record_parameters(self, connection, cursor, statement, parameters, context, many):
        self.parameters.append(parameters)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def raw(self):
        return self.session.execute(select(SystemConfig.value).where(
            SystemConfig.key == CONFIG_KEY)).scalar_one_or_none()

    def test_explicit_caller_transaction_required(self):
        for call in (self.repo.get, lambda: self.repo.put(self.payload),
                     lambda: self.repo.rotate(old=self.cipher, new=self.new)):
            with self.assertRaises(ConfigSecretError):
                call()
        self.assertEqual(self.parameters, [])

    def test_store_envelope_only_get_decrypts_and_upsert_preserves_unknown(self):
        with self.session.begin():
            self.assertIsNone(self.repo.get())
            self.assertIsNone(self.repo.put(self.payload))
            envelope = self.raw()
            self.assertEqual(set(envelope), {"version", "key_id", "ciphertext"})
            self.assertEqual(self.repo.get(), self.payload)
            self.assertIsNone(self.repo.put({**self.payload, "new": True}))
            self.assertEqual(self.repo.get(), {**self.payload, "new": True})
        self.assertNotIn("SECRET-database-value", repr(self.parameters))

    def test_rotation_commits_and_old_key_cannot_read(self):
        with self.session.begin():
            self.repo.put(self.payload)
        with self.session.begin():
            self.assertTrue(self.repo.rotate(old=self.cipher, new=self.new))
            self.assertEqual(EncryptedConfigRepository(self.session, self.new).get(), self.payload)
            with self.assertRaises(ConfigSecretError):
                self.repo.get()
        self.assertNotIn("SECRET-database-value", repr(self.parameters))

    def test_rotation_and_writes_obey_caller_rollback(self):
        with self.session.begin():
            self.repo.put(self.payload)
            original = self.raw()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.session.begin():
                self.repo.rotate(old=self.cipher, new=self.new)
                raise RuntimeError("rollback")
        with self.session.begin():
            self.assertEqual(self.raw(), original)
            self.assertEqual(self.repo.get(), self.payload)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.session.begin():
                self.repo.put({"replacement": True})
                raise RuntimeError("rollback")
        with self.session.begin():
            self.assertEqual(self.repo.get(), self.payload)

    def test_missing_rotation_and_failed_rotation_do_not_write(self):
        with self.session.begin():
            self.assertFalse(self.repo.rotate(old=self.cipher, new=self.new))
            self.repo.put(self.payload)
            original = self.raw()
            with self.assertRaises(ConfigSecretError):
                self.repo.rotate(old=self.new, new=self.cipher)
            self.assertEqual(self.raw(), original)

    def test_no_plaintext_in_logs_stdout_stderr_or_write_results(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            with redirect_stdout(output), redirect_stderr(output), self.session.begin():
                self.assertIsNone(self.repo.put(self.payload))
                self.assertTrue(self.repo.rotate(old=self.cipher, new=self.new))
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertNotIn("SECRET-database-value", output.getvalue())
        self.assertNotIn("SECRET-database-value", repr(self.parameters))


if __name__ == "__main__":
    unittest.main()
