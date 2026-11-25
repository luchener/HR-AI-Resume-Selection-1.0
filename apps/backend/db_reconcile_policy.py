"""Independent reconciliation of stored quota policy semantics, not effective limits."""
from uuid import UUID
from sqlalchemy import select
from db_check_schema import metadata


def reconcile_policies(session, report):
    users = {row['username']: UUID(str(row['user_id'])) for _, row in report['records']['users']}
    documents = {key: wrapper['value'] for _, key, wrapper in report.get('legacy_records', [])}
    limits = documents.get('quota/limits.json')
    limits = {} if limits is None else limits
    if not isinstance(limits, dict) or not isinstance(limits.get('users', {}), dict):
        raise ValueError('Invalid quota policy document')
    def mode(value, inherited):
        if value is None:
            return ('inherit' if inherited else 'unlimited'), None
        count = int(value)
        if count > 2_147_483_647:
            raise ValueError('Quota exceeds target range')
        return ('unlimited', None) if count < 0 else ('limited', count)
    global_mode, daily = mode(limits.get('default_daily'), False)
    expected = {'global': dict(user_id=None, mode=global_mode, daily_limit=daily, enabled=True,
                              quota_enabled=bool(limits.get('quota_enabled', False)),
                              email_notify=bool(limits.get('email_notify_enabled', False)))}
    for name, config in limits.get('users', {}).items():
        if name not in users or not isinstance(config, dict):
            raise ValueError('Invalid quota override identity')
        uid = users[name]
        policy_mode, daily = mode(config.get('daily_limit'), True)
        expected[str(uid)] = dict(user_id=uid, mode=policy_mode, daily_limit=daily,
                                 enabled=bool(config.get('analysis_enabled', True)), quota_enabled=None,
                                 email_notify=bool(config.get('email_notify', True)))
    rows = session.execute(select(metadata().tables['quota_policies'])).mappings().all()
    actual = {row['scope_key']: row for row in rows}
    mismatches = {}
    def record(field, count):
        if count:
            mismatches['quota_policies.'+field] = count
    record('missing_rows', len(expected.keys()-actual.keys()))
    record('extra_rows', len(actual.keys()-expected.keys()))
    for field in ('user_id', 'mode', 'daily_limit', 'enabled', 'quota_enabled', 'email_notify'):
        record(field, sum(expected[key][field] != actual[key][field] for key in expected.keys() & actual.keys()))
    return {'checked_fields':len(expected)*6+2, 'mismatch_count':sum(mismatches.values()), 'mismatches':mismatches}
