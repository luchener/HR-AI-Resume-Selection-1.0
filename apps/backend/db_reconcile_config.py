"""Read-only encrypted configuration reconciliation; no plaintext in reports."""
from sqlalchemy import select
from db_models import SystemConfig
from db_secrets import CONFIG_KEY
from db_json_compare import json_equal


def reconcile_configs(session, report, *, cipher=None):
    expected = {key: wrapper['value'] for _, key, wrapper in report.get('legacy_records', [])
                if key.startswith('system_config/')}
    rows = session.execute(select(SystemConfig.__table__)).mappings().all()
    indexed = {row['key']: row['value'] for row in rows}
    mismatches = {}
    extra_rows = len(indexed.keys() - ({CONFIG_KEY} if expected else set()))
    if extra_rows:
        mismatches['system_configs.extra_rows'] = extra_rows
    if expected and CONFIG_KEY not in indexed:
        mismatches['system_configs.missing_envelope'] = 1
    elif expected:
        if cipher is None:
            raise ValueError('External configuration cipher required')
        actual = cipher.decrypt(indexed[CONFIG_KEY])
        for field, count in (
            ('missing_documents', len(expected.keys()-actual.keys())),
            ('extra_documents', len(actual.keys()-expected.keys())),
            ('changed_documents', sum(not json_equal(expected[key], actual[key]) for key in expected.keys() & actual.keys())),
        ):
            if count:
                mismatches['system_configs.'+field] = count
    return {'checked_fields':len(expected)+2,'mismatch_count':sum(mismatches.values()),'mismatches':mismatches}
