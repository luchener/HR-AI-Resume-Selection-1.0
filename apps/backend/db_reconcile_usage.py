"""Independent UTC usage reconciliation; no import conversion functions reused."""
from datetime import date
from pathlib import PurePosixPath
from uuid import UUID
from sqlalchemy import select
from db_check_schema import metadata


def reconcile_usage(session, report):
    expected = {}
    known_users = {UUID(str(row['user_id'])) for _, row in report['records']['users']}
    for _, key, wrapper in report.get('legacy_records', []):
        path = PurePosixPath(key)
        if len(path.parts) != 2 or path.parts[0] != 'usage' or path.suffix != '.json':
            continue
        uid = UUID(path.stem)
        if uid not in known_users:
            raise ValueError('Usage owner absent from source users')
        payload = wrapper['value']
        if not isinstance(payload, dict):
            raise ValueError('Invalid usage document')
        for day_text, counters in payload.items():
            day = date.fromisoformat(day_text)
            if day.isoformat() != day_text or not isinstance(counters, dict):
                raise ValueError('Invalid UTC usage day')
            identity = (uid, day)
            if identity in expected:
                raise ValueError('Duplicate source usage identity')
            used = max(0, int(counters.get('analysis', 0)))
            if used > 2_147_483_647:
                raise ValueError('Usage exceeds target range')
            expected[identity] = used
    rows = session.execute(select(metadata().tables['quota_daily_usage'])).mappings().all()
    actual = {(row['user_id'], row['date']): row for row in rows}
    mismatches = {}
    def mismatch(field, count=1):
        if count:
            mismatches['quota_daily_usage.'+field] = count
    mismatch('missing_rows', len(expected.keys()-actual.keys()))
    mismatch('extra_rows', len(actual.keys()-expected.keys()))
    mismatch('used', sum(actual[key]['used'] != expected[key] for key in expected.keys() & actual.keys()))
    mismatch('reserved', sum(row['reserved'] != 0 for row in rows))
    return {'checked_fields':len(expected)*2+2, 'mismatch_count':sum(mismatches.values()), 'mismatches':mismatches}
