"""Isolated encryption boundary for the designated whole system_config payload.

No production wiring, key generation, key persistence, or plaintext fallback.
The caller supplies a Session and owns its transaction. Only ``get`` deliberately
returns decrypted configuration to its caller; writes/rotation return no secrets.
Fernet has no AAD: context and envelope metadata are authenticated inside its
ciphertext instead. Envelope key_id is a public label, never a master key.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db_models import SystemConfig

CONFIG_KEY = "system_config"
KEY_ENV = "DATABASE_CONFIG_KEY"
VERSION = 1


class ConfigSecretError(ValueError):
    """Safe error messages containing no keys, payloads, or ciphertext."""


def _label(key_id):
    if not isinstance(key_id, str) or not key_id.strip() or len(key_id) > 128:
        raise ConfigSecretError("A nonempty public key ID of at most 128 characters is required")
    return key_id


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ConfigSecretError("Configuration must contain valid JSON data") from None


class ConfigCipher:
    """An explicitly supplied external Fernet key; repr never exposes it."""

    def __init__(self, key: str | bytes, *, key_id: str):
        self.key_id = _label(key_id)
        try:
            if not isinstance(key, (str, bytes)) or not key:
                raise ValueError
            self._fernet = Fernet(key)
        except (TypeError, ValueError, UnicodeError):
            raise ConfigSecretError("A valid external DATABASE_CONFIG_KEY is required") from None

    @classmethod
    def from_env(cls, *, key_id: str, environ: Mapping | None = None):
        """Require DATABASE_CONFIG_KEY, with no generated or hardcoded fallback."""
        source = os.environ if environ is None else environ
        return cls(source.get(KEY_ENV), key_id=key_id)

    def encrypt(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ConfigSecretError("Configuration payload must be a JSON object")
        protected = {"context": CONFIG_KEY, "version": VERSION,
                     "key_id": self.key_id, "payload": payload}
        token = self._fernet.encrypt(_json_bytes(protected)).decode("ascii")
        return {"version": VERSION, "key_id": self.key_id, "ciphertext": token}

    def decrypt(self, envelope: dict) -> dict:
        if (not isinstance(envelope, dict)
                or set(envelope) != {"version", "key_id", "ciphertext"}
                or type(envelope["version"]) is not int
                or envelope["version"] != VERSION
                or envelope["key_id"] != self.key_id
                or not isinstance(envelope["ciphertext"], str)):
            raise ConfigSecretError("Invalid encrypted configuration envelope")
        try:
            protected = json.loads(self._fernet.decrypt(envelope["ciphertext"].encode("ascii")))
            if (not isinstance(protected, dict)
                    or set(protected) != {"context", "version", "key_id", "payload"}
                    or protected["context"] != CONFIG_KEY
                    or type(protected["version"]) is not int
                    or protected["version"] != VERSION
                    or protected["key_id"] != self.key_id
                    or not isinstance(protected["payload"], dict)):
                raise ValueError
            return protected["payload"]
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError):
            raise ConfigSecretError("Encrypted configuration could not be authenticated") from None


def rotate_envelope(envelope: dict, *, old: ConfigCipher, new: ConfigCipher) -> dict:
    """Both keys are supplied externally; neither key is written to storage."""
    if old.key_id == new.key_id:
        raise ConfigSecretError("Rotation requires a distinct new public key ID")
    return new.encrypt(old.decrypt(envelope))


class EncryptedConfigRepository:
    """Only the designated system_config row; no generic plaintext write API.

    Call inside ``with session.begin():``. No begin/commit/rollback/close here;
    the caller can compose writes and rotation with other transactional work.
    SQL parameters contain only encrypted envelopes and public metadata.
    """

    def __init__(self, session: Session, cipher: ConfigCipher):
        self.session = session
        self.cipher = cipher
        self.table = SystemConfig.__table__

    def _transaction(self):
        if not self.session.in_transaction():
            raise ConfigSecretError("An explicit caller-owned transaction is required")

    def _row(self, *, lock=False):
        statement = select(self.table.c.value).where(self.table.c.key == CONFIG_KEY)
        if lock:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalar_one_or_none()

    def get(self) -> dict | None:
        self._transaction()
        envelope = self._row()
        return None if envelope is None else self.cipher.decrypt(envelope)

    def put(self, payload: dict) -> None:
        self._transaction()
        envelope = self.cipher.encrypt(payload)
        statement = insert(self.table).values(key=CONFIG_KEY, value=envelope)
        statement = statement.on_conflict_do_update(
            index_elements=[self.table.c.key],
            set_={"value": statement.excluded.value,
                  "updated_at": datetime.now(timezone.utc)},
        )
        self.session.execute(statement)

    def update_document(self, name: str, value: dict | None) -> None:
        """Serialize whole-envelope merge, including the initially absent row.

        None deliberately clears one document; read failures never fallback.
        """
        self._transaction()
        if name not in {'system_config/mail.json', 'system_config/llm.json'}:
            raise ConfigSecretError('Unsupported configuration document')
        from sqlalchemy import text
        self.session.execute(text('SELECT pg_advisory_xact_lock(710413502)'))
        envelope = self._row(lock=True)
        payload = {} if envelope is None else self.cipher.decrypt(envelope)
        if value is None:
            payload.pop(name, None)
        elif isinstance(value, dict):
            payload[name] = value
        else:
            raise ConfigSecretError('Configuration document must be an object')
        self.put(payload)

    def merge_document(self, name: str, payload: dict, merger) -> None:
        """Read, product-validate and replace one document under the same lock."""
        self._transaction()
        if name not in {'system_config/mail.json', 'system_config/llm.json'}:
            raise ConfigSecretError('Unsupported configuration document')
        from sqlalchemy import text
        self.session.execute(text('SELECT pg_advisory_xact_lock(710413502)'))
        envelope = self._row(lock=True)
        documents = {} if envelope is None else self.cipher.decrypt(envelope)
        current = documents.get(name, {})
        if not isinstance(current, dict):
            raise ConfigSecretError('Stored configuration document must be an object')
        merged = merger(current, payload)
        if not isinstance(merged, dict):
            raise ConfigSecretError('Configuration merger must return an object')
        documents[name] = merged
        self.put(documents)

    def rotate(self, *, old: ConfigCipher, new: ConfigCipher) -> bool:
        """Lock/re-encrypt one existing row; caller commits or rolls back.

        The repository cipher is deliberately not changed before commit. Create
        a repository with the new cipher after the caller commits successfully.
        """
        self._transaction()
        envelope = self._row(lock=True)
        if envelope is None:
            return False
        rotated = rotate_envelope(envelope, old=old, new=new)
        self.session.execute(update(self.table).where(self.table.c.key == CONFIG_KEY).values(
            value=rotated, updated_at=datetime.now(timezone.utc)))
        return True
