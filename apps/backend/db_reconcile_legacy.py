"""Independent restricted historical payload completeness; never prints payloads.

Migration bookkeeping is an explicit allow-list, not a "double underscore" heuristic:
unknown `__*__` roots or unknown keys under known roots are counted as business
differences. Known metadata is bound to this source snapshot, the recomputed
announcement projection and the target attestation; formatted-but-wrong values
(valid SHA-256 shape, wrong digest, wrong counts, empty attestation) are rejected.
"""
import os
import re
from datetime import datetime

from sqlalchemy import select
from db_json_compare import json_equal
from db_models import LegacyRecord
from db_import_attestation import ATTESTATION_KEY, verify_attestation
from db_announcement_import import project_announcement_snapshot

MIGRATION_ROOT = '__migration__'
ANNOUNCEMENT_ROOT = '__announcement_migration__'
MIGRATION_KEYS = frozenset({'source_manifest', 'identity_policy', 'evaluation_time',
                            ATTESTATION_KEY})
ANNOUNCEMENT_KEYS = frozenset({'projection'})
_HEX64 = re.compile(r'^[0-9a-f]{64}$')


def _metadata_target(root, key):
    """True only for explicitly recognised migration metadata rows."""
    if root == MIGRATION_ROOT:
        return key in MIGRATION_KEYS
    if root == ANNOUNCEMENT_ROOT:
        return key in ANNOUNCEMENT_KEYS
    return False


def _hex64(value):
    return isinstance(value, str) and bool(_HEX64.match(value))


def _validate_migration_row(key, payload, report):
    if not isinstance(payload, dict):
        raise ValueError('Corrupted migration metadata')
    if key == 'source_manifest':
        if payload != report.get('manifest'):
            raise ValueError('Source manifest marker does not match this snapshot')
    elif key == 'identity_policy':
        admins = payload.get('admin_emails')
        if not isinstance(admins, list) or any(not isinstance(item, str) for item in admins):
            raise ValueError('Corrupted migration identity policy')
        declared = [item.strip() for item in os.environ.get('MIGRATION_ADMIN_EMAILS', '').split(',')
                    if item.strip()]
        if declared and sorted(admins) != sorted(declared):
            raise ValueError('Identity policy does not match the explicit admin mapping')
        auto = payload.get('auto_unfreeze_hours', '0')
        if not isinstance(auto, str) or not auto.isdigit():
            raise ValueError('Corrupted migration identity policy')
    elif key == 'evaluation_time':
        value = payload.get('evaluated_at')
        if not isinstance(value, str):
            raise ValueError('Corrupted migration evaluation time')
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Corrupted migration evaluation time') from None
        if parsed.tzinfo is None:
            raise ValueError('Corrupted migration evaluation time')


def _validate_announcement(payload, report):
    plan = project_announcement_snapshot(report)
    expected = {'projection_sha256': plan['projection_sha256'],
                'inventory_sha256': plan['inventory_sha256'],
                'announcements': len(plan['announcements']),
                'dismissals': len(plan['dismissals'])}
    if payload is None:
        if expected['announcements'] or expected['dismissals']:
            raise ValueError('Missing announcement projection metadata')
        return
    if not isinstance(payload, dict):
        raise ValueError('Corrupted announcement projection metadata')
    if (not _hex64(payload.get('projection_sha256')) or not _hex64(payload.get('inventory_sha256'))
            or type(payload.get('announcements')) is not int
            or type(payload.get('dismissals')) is not int
            or payload['announcements'] < 0 or payload['dismissals'] < 0):
        raise ValueError('Corrupted announcement projection metadata')
    for key in ('projection_sha256', 'inventory_sha256', 'announcements', 'dismissals'):
        if payload[key] != expected[key]:
            raise ValueError('Announcement projection metadata does not match this snapshot')


def reconcile_legacy(session, report):
    expected = {}
    for root, key, wrapper in report.get('legacy_records', []):
        if isinstance(root, str) and root.startswith('__') and root.endswith('__'):
            raise ValueError('Source collides with reserved migration namespace')
        expected[(root, key)] = ({'encrypted_in':'system_configs','source_key':key}
                                 if root == 'system_config' else wrapper)
    for table, records in report['records'].items():
        for path, payload in records:
            expected[(table, f'{table}/{path.name}')] = payload
    rows = session.execute(select(LegacyRecord.__table__)).mappings().all()
    actual = {}
    metadata = {}
    for row in rows:
        root, key, payload = row['root_name'], row['record_key'], row['payload']
        if _metadata_target(root, key):
            metadata[(root, key)] = payload
        else:
            # Unknown reserved namespaces and unknown keys are business rows, so
            # they surface as extra_rows instead of being silently ignored.
            actual[(root, key)] = payload
    # Semantic binding of known metadata (format checks alone are insufficient).
    for (root, key), payload in metadata.items():
        if root == MIGRATION_ROOT and key != ATTESTATION_KEY:
            _validate_migration_row(key, payload, report)
    attestation = metadata.get((MIGRATION_ROOT, ATTESTATION_KEY))
    if attestation is None:
        raise ValueError('Missing target attestation metadata')
    verify_attestation(session, attestation)
    _validate_announcement(metadata.get((ANNOUNCEMENT_ROOT, 'projection')), report)
    mismatches = {}
    for field, count in (
        ('missing_rows', len(expected.keys()-actual.keys())),
        ('extra_rows', len(actual.keys()-expected.keys())),
        ('changed_payloads', sum(not json_equal(expected[key], actual[key]) for key in expected.keys() & actual.keys())),
    ):
        if count:
            mismatches['legacy_records.'+field] = count
    return {'checked_fields': len(expected)+2, 'mismatch_count': sum(mismatches.values()),
            'mismatches': mismatches, 'metadata_rows': len(metadata)}
