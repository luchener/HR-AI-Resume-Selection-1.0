"""Independent imported-audit projection checks; never returns source values."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import select
from db_json_compare import json_equal
from db_check_schema import metadata


def reconcile_audits(session, report):
    known = {UUID(str(row['user_id'])) for _, row in report['records']['users']}
    expected = {}
    for _, key, wrapper in report.get('legacy_records', []):
        if not key.startswith('admin_ops/'):
            continue
        item = wrapper['value']
        actor = str(item.get('operator_id') or '')
        try:
            linked = UUID(actor)
        except ValueError:
            linked = None
        when = datetime.fromisoformat(str(item['created_at']).replace('Z','+00:00'))
        if when.tzinfo is None:
            raise ValueError('Audit timestamp requires timezone')
        expected[key] = dict(actor_identity=actor or None, actor_user_id=linked if linked in known else None,
                             action=str(item.get('op') or ''), target_type='legacy_user',
                             target_id=str(item.get('target_username') or '') or None,
                             request_id=str(item.get('request_id') or '') or None,
                             metadata={'legacy_detail_quarantined':True}, created_at=when, ip_address=None, user_agent=None)
    rows = session.execute(select(metadata().tables['audit_logs'])).mappings().all()
    actual = {row['source_key']:row for row in rows if row['source_key'] is not None}
    mismatches = {}
    def record(field,count):
        if count:
            mismatches['audit_logs.'+field] = count
    record('missing_rows',len(expected.keys()-actual.keys()))
    record('extra_rows',len(actual.keys()-expected.keys()))
    record('unexpected_runtime_rows',sum(row['source_key'] is None for row in rows))
    for field in ('actor_identity','actor_user_id','action','target_type','target_id','request_id','metadata','created_at','ip_address','user_agent'):
        record(field,sum(not json_equal(expected[key][field], actual[key][field]) for key in expected.keys() & actual.keys()))
    return {'checked_fields':len(expected)*10+3,'mismatch_count':sum(mismatches.values()),'mismatches':mismatches}
