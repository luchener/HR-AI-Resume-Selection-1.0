"""Conservative audit conversion: sensitive free-text details remain quarantined."""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import uuid
import sqlalchemy as sa
from db_check_schema import metadata


def audit_rows(source_records, known_users):
    known = {uuid.UUID(str(value)) for value in known_users}
    rows = []
    for key, record in source_records.items():
        if not key.startswith('admin_ops/') or '..' in PurePosixPath(key).parts:
            raise ValueError('Invalid audit source key')
        actor = str(record.get('operator_id') or '')
        try:
            actor_uuid = uuid.UUID(actor)
        except (ValueError, AttributeError):
            actor_uuid = None
        linked = actor_uuid if actor_uuid in known else None
        action = str(record.get('op') or '')
        if not action or len(action) > 128:
            raise ValueError('Invalid audit action')
        try:
            created = datetime.fromisoformat(str(record['created_at']).replace('Z', '+00:00'))
        except (KeyError, ValueError):
            raise ValueError('Invalid audit timestamp') from None
        if created.tzinfo is None:
            raise ValueError('Audit timestamp must be timezone aware')
        rows.append({'source_key': key, 'actor_identity': actor or None,
                     'actor_user_id': linked, 'action': action, 'target_type': 'legacy_user',
                     'target_id': str(record.get('target_username') or '') or None,
                     'request_id': str(record.get('request_id') or '') or None,
                     'metadata': {'legacy_detail_quarantined': True}, 'created_at': created})
    return rows


def insert_audits(session, source_records, known_users):
    table = metadata().tables['audit_logs']
    rows = audit_rows(source_records, known_users)
    for start in range(0, len(rows), 100):
        session.execute(sa.insert(table), rows[start:start + 100])
    return len(rows)
