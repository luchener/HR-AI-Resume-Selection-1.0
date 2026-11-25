"""Unwired superadmin config transaction boundary, with injected product validation.

Validator(document, payload) must enforce existing masking/merge/schema rules and
return a complete normalized document. No HTTP route or environment fallback.
"""
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy import select
from db_models import User
from db_secrets import EncryptedConfigRepository
from db_secrets import ConfigSecretError
from db_audit import append_audit
from db_config_resolved import environment_snapshot, public_config_document


class ConfigPermissionError(PermissionError):
    pass


class ConfigValidationError(ValueError):
    """Only product validation errors; storage/decryption errors stay unavailable."""


class ConfigService:
    def __init__(self, session_factory, cipher, *, validator, admin_emails=(), merger=None, environment=None):
        if not callable(validator):
            raise TypeError('Explicit product configuration validator required')
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError('admin_emails must be an iterable')
        self.sessions = session_factory
        self.cipher = cipher
        self.validator = validator
        if merger is not None and not callable(merger):
            raise TypeError('Configuration merger must be callable')
        self.merger = merger
        self.admin_emails = frozenset(e.strip().lower() for e in admin_emails if e.strip())
        self.environment = environment_snapshot(environment)

    def public_document(self, actor_id, document, *, password_version):
        """Frontend view resolved against an explicit environment snapshot only."""
        if document not in {'mail', 'llm'}:
            raise ValueError('Unsupported configuration document')
        actor = UUID(str(actor_id))
        if not actor.int or type(password_version) is not int:
            raise ConfigPermissionError('Invalid actor identity')
        with self.sessions() as session, session.begin():
            user = session.execute(select(User.email, User.status, User.deleted_at, User.password_version).where(
                User.id == actor).with_for_update()).mappings().one_or_none()
            if (user is None or user['status'] != 'active' or user['deleted_at'] is not None
                    or user['password_version'] != password_version
                    or (user['email'] or '').lower() not in self.admin_emails):
                raise ConfigPermissionError('Active superadministrator required')
            documents = EncryptedConfigRepository(session, self.cipher).get()
            current = (documents or {}).get(f'system_config/{document}.json')
            return public_config_document(document, current, self.environment)

    def update(self, actor_id, document, payload, *, request_id, password_version):
        if document not in {'mail', 'llm'}:
            raise ValueError('Unsupported configuration document')
        actor = UUID(str(actor_id))
        if not actor.int or type(password_version) is not int:
            raise ConfigPermissionError('Invalid actor identity')
        with self.sessions() as session, session.begin():
            user = session.execute(select(User.id, User.username, User.email, User.is_admin, User.status,
                                          User.deleted_at, User.password_version).where(User.id == actor).with_for_update()).mappings().one_or_none()
            if (user is None or user['status'] != 'active' or user['deleted_at'] is not None
                    or user['password_version'] != password_version
                    or (user['email'] or '').lower() not in self.admin_emails):
                raise ConfigPermissionError('Active administrator required')
            repository = EncryptedConfigRepository(session, self.cipher)
            def stamped(value):
                if not isinstance(value, dict):
                    raise ConfigSecretError('Validator must return a complete configuration object')
                return dict(value, updated_at=datetime.now(timezone.utc).isoformat(), updated_by=user['username'])
            if self.merger is not None and payload is not None:
                normalized = None
                def merge(current, incoming):
                    nonlocal normalized
                    try:
                        normalized = stamped(self.merger(document, current, incoming))
                        return normalized
                    except ConfigSecretError:
                        raise
                    except ValueError:
                        raise ConfigValidationError('Invalid product configuration') from None
                repository.merge_document(f'system_config/{document}.json', payload, merge)
            else:
                try:
                    normalized = None if payload is None else stamped(self.validator(document, payload))
                except ConfigSecretError:
                    raise
                except ValueError:
                    raise ConfigValidationError('Invalid product configuration') from None
                if payload is not None and not isinstance(normalized, dict):
                    raise ValueError('Validator must return a complete configuration object')
                repository.update_document(f'system_config/{document}.json', normalized)
            # Form the allowlisted response before committing, under the same lock.
            result = public_config_document(document, normalized, self.environment)
            append_audit(session, actor_user_id=actor, action=f'config.{document}.update',
                         target_type='config', target_id=document, request_id=request_id,
                         changed_fields=[document])
        return result
