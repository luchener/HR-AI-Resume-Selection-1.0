"""Tables deliberately unpopulated by the current JSON importer must remain empty.

Historical analyses remain in restricted source payloads; zero here is not proof
that runtime analysis migration is complete. Intended only before target cutover.
"""
from sqlalchemy import select, func
from db_check_schema import metadata

EXPECTED_EMPTY = ('analysis_tasks', 'analyses', 'quotas', 'quota_reservations',
                  'job_requirements', 'evidence_assessments', 'analysis_task_batches',
                  'analysis_task_inputs', 'analysis_task_artifacts', 'original_gc', 'auth_limits', 'auth_challenges',
                  'auth_invites', 'auth_login_state', 'invite_requests')


def reconcile_empty_tables(session, report):
    tables = metadata().tables
    mismatches = {}
    for name in EXPECTED_EMPTY:
        count = session.scalar(select(func.count()).select_from(tables[name]))
        if count:
            mismatches[name+'.unexpected_rows'] = count
    return {'checked_fields':len(EXPECTED_EMPTY), 'mismatch_count':sum(mismatches.values()), 'mismatches':mismatches}
