"""Independent critical-field reconciliation; no importer conversion reuse.

Returns only mismatch counts and table/field names. Must use a frozen source and
quiescent target; this covers specified fields, not full release certification.
"""
from uuid import UUID
from datetime import datetime, timezone
from hashlib import sha256
from db_json_compare import json_equal
from sqlalchemy import select
from db_models import User, Resume, Job, Archive, LegacyRecord


def source_time(value):
    if value is None or value == '':
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def evaluation_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Timezone-aware migration evaluation time required')
    return parsed


def expected_user_status(item, failures, *, auto_unfreeze_hours, evaluated_at):
    if item.get('deleted_at'):
        return 'deleted'
    status = 'locked' if item.get('frozen_until') else 'active'
    key = 'login_failures/' + sha256(str(item['username']).lower().encode()).hexdigest() + '.json'
    failure = failures.get(key, {})
    frozen = bool(failure.get('frozen'))
    if frozen and failure.get('frozen_by') != 'admin' and auto_unfreeze_hours > 0 and failure.get('frozen_at'):
        if evaluated_at is None or evaluated_at.tzinfo is None:
            raise ValueError('Explicit timezone-aware migration evaluation time required')
        epoch = float(failure['frozen_at'])
        age = evaluated_at.timestamp() - (epoch / 1000 if epoch > 10**12 else epoch)
        if age > auto_unfreeze_hours * 3600:
            frozen = False
    return 'locked' if frozen else status


def expected_lifecycle(item, failures, *, auto_unfreeze_hours, evaluated_at):
    """Infer source state independently, including a freeze hidden by deletion."""
    live = dict(item, deleted_at=None)
    prior = expected_user_status(live, failures, auto_unfreeze_hours=auto_unfreeze_hours, evaluated_at=evaluated_at)
    key = 'login_failures/' + sha256(str(item['username']).lower().encode()).hexdigest() + '.json'
    record = failures.get(key, {})
    keep = bool(record.get('frozen'))
    if keep and record.get('frozen_by') != 'admin' and auto_unfreeze_hours > 0 and record.get('frozen_at'):
        epoch = float(record['frozen_at'])
        keep = evaluated_at.timestamp() - (epoch / 1000 if epoch > 10**12 else epoch) <= auto_unfreeze_hours * 3600
    epoch = record.get('frozen_at')
    return {'deletion_previous_status': prior if item.get('deleted_at') else None,
            'login_consecutive_failures': record.get('consecutive_failures', 0),
            'login_window_failures': len(record.get('failures', [])),
            'deleted_by': str(item.get('deleted_by') or '') if item.get('deleted_at') else '',
            'frozen_at': datetime.fromtimestamp(epoch / 1000 if epoch > 10**12 else epoch, timezone.utc) if keep and epoch is not None else None,
            'frozen_by': (record.get('frozen_by') or 'auto') if keep else ('auto' if item.get('frozen_until') else None),
            'frozen_reason': (record.get('frozen_reason') or '') if keep else
                             (str(item.get('frozen_reason') or '') if item.get('frozen_until') else '')}


def reconcile(session, report, *, admin_emails, auto_unfreeze_hours=0, evaluated_at=None):
    import math
    if not math.isfinite(auto_unfreeze_hours) or auto_unfreeze_hours < 0:
        raise ValueError('Invalid automatic unfreeze policy')
    failures = {key: payload['value'] for _, key, payload in report.get('legacy_records', []) if key.startswith('login_failures/')}
    mismatches = {}
    checked = 0
    admins = {email.strip().lower() for email in admin_emails}
    source_ids = {table: {UUID(str(item[key])) for _, item in report['records'][table]}
                  for table, key in (('resumes', 'resume_id'), ('jobs', 'job_id'))}
    def retained_reference(value, table):
        parsed = UUID(str(value)) if value else None
        return parsed if parsed in source_ids[table] else None
    def check(table, field, actual, expected):
        nonlocal checked
        checked += 1
        if not json_equal(actual, expected):
            key = f'{table}.{field}'
            mismatches[key] = mismatches.get(key,0)+1
    for model, source_name, id_key in ((User,'users','user_id'),(Resume,'resumes','resume_id'),(Job,'jobs','job_id'),(Archive,'archives','archive_id')):
        rows = session.execute(select(model.__table__)).mappings().all()
        indexed = {row['id']:row for row in rows}
        source = report['records'][source_name]
        check(source_name,'count',len(rows),len(source))
        for _, item in source:
            if source_name == 'archives':
                identities = {UUID(str(item[key])) for key in ('archive_id', 'archiveId', 'id') if item.get(key)}
                if len(identities) != 1:
                    raise ValueError('Missing or conflicting archive identities')
                identity = identities.pop()
            else:
                identity = UUID(str(item[id_key]))
            row = indexed.get(identity)
            if row is None:
                check(source_name,'row_exists',False,True)
                continue
            check(source_name,'legacy_payload',row['legacy_payload'],item)
            if source_name=='users':
                email = str(item.get('email') or '').strip().lower() or None
                expected = {'username':str(item['username']),'email':email,
                            'password_hash':str(item['password_hash']),'password_salt':str(item['password_salt']),
                            'password_version':item.get('password_version',1),
                            'is_super_admin':email in admins,
                            'is_admin':email in admins or bool(item.get('is_admin'))}
            else:
                expected = {'owner_user_id':UUID(str((item.get('owner_user_id') or item['user_id']) if source_name == 'archives' else item['user_id']))}
                if source_name in {'resumes','jobs'}:
                    expected['raw_content'] = str(item.get('content') or '')
                    expected['processed_data' if source_name=='resumes' else 'parsed_requirements'] = item.get('processed') if isinstance(item.get('processed'),dict) else {}
                else:
                    expected.update(tags=item.get('custom_tags',item.get('tags',[])),candidate_name=item.get('candidate_name'),category=item.get('category'),
                                    status='trashed' if item.get('trashed_at') else str(item.get('status') or 'active'))
            expected['deleted_at'] = source_time(item.get('deleted_at'))
            for field in ('created_at', 'updated_at'):
                value = item.get(field)
                if not value and source_name != 'users' and field == 'updated_at':
                    value = item.get('created_at')
                if value:
                    expected[field] = source_time(value)
                # Missing source timestamps are generated on import; cannot infer exact values.
            if source_name == 'users':
                expected['last_login_at'] = source_time(item.get('last_login_at'))
                expected['status'] = expected_user_status(item, failures, auto_unfreeze_hours=auto_unfreeze_hours, evaluated_at=evaluated_at)
                expected.update(expected_lifecycle(item, failures, auto_unfreeze_hours=auto_unfreeze_hours, evaluated_at=evaluated_at))
            elif source_name == 'jobs':
                expected['source_resume_id'] = retained_reference(item.get('resume_id'), 'resumes')
                processed = expected['parsed_requirements']
                company = processed.get('company_profile')
                expected['title'] = processed.get('job_title')
                expected['company_name'] = company.get('company_name') if isinstance(company, dict) else None
            elif source_name == 'archives':
                expected['resume_id'] = retained_reference(item.get('resume_id'), 'resumes')
                expected['job_id'] = retained_reference(item.get('job_id'), 'jobs')
                expected['analysis_id'] = None
                expected['note'] = item.get('note')
                projected = {'archive_id', 'archiveId', 'id', 'user_id', 'owner_user_id', 'resume_id',
                             'job_id', 'analysis_id', 'candidate_name', 'category', 'tags', 'status',
                             'note', 'created_at', 'updated_at', 'deleted_at', 'trashed_at'}
                expected['payload'] = {key: value for key, value in item.items() if key not in projected}
            elif source_name == 'resumes':
                original = item.get('original') if isinstance(item.get('original'), dict) else {}
                expected['retention_until'] = source_time(original.get('retention_until'))
                personal = expected['processed_data'].get('personal_data')
                personal = personal if isinstance(personal, dict) else {}
                expected.update(candidate_name=personal.get('name') or personal.get('姓名'),
                                source_filename=original.get('name') or original.get('filename'),
                                content_type=str(item.get('content_type') or 'md'),
                                content_sha256=original.get('sha256') or sha256(expected['raw_content'].encode('utf-8')).hexdigest(),
                                object_key=original.get('storage_key'), parse_status='parsed')
            for field,value in expected.items():
                check(source_name,field,row[field],value)
    return {'checked_fields':checked,'mismatch_count':sum(mismatches.values()),'mismatches':mismatches}


def verify_migration_context(session, report, *, admin_emails, auto_unfreeze_hours):
    """Require the exact source and explicit identity policy certified at import."""
    def ledger(key):
        return session.scalar(select(LegacyRecord.payload).where(
            LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == key))
    source = ledger('source_manifest')
    policy = ledger('identity_policy')
    expected = {'admin_emails': sorted({email.strip().lower() for email in admin_emails if email.strip()}),
                'auto_unfreeze_hours': auto_unfreeze_hours}
    if source is None or source != report['manifest']:
        raise ValueError('Source differs from migration ledger')
    if policy is None or policy != expected:
        raise ValueError('Identity policy differs from migration ledger')


def main():
    import argparse
    import json
    import os
    from pathlib import Path
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from db_migrate_json import inventory, validate_inventory
    from db_security import migration_engine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--evaluated-at', help='Timezone-aware migration evaluation timestamp for automatic freeze expiry')
    args = parser.parse_args()
    engine = None
    try:
        report = inventory(args.snapshot)
        validate_inventory(report)
        if 'MIGRATION_ADMIN_EMAILS' not in os.environ:
            raise ValueError('Explicit identity policy required')
        engine = migration_engine(os.environ['DATABASE_RECONCILE_URL'])
        with Session(engine) as session, session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            verify_migration_context(session, report, admin_emails=os.environ['MIGRATION_ADMIN_EMAILS'].split(','),
                                     auto_unfreeze_hours=os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0'))
            ledger = session.scalar(select(LegacyRecord.payload).where(LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == 'evaluation_time'))
            persisted = evaluation_time(ledger['evaluated_at']) if ledger else None
            explicit = evaluation_time(args.evaluated_at) if args.evaluated_at else None
            if explicit is not None and persisted is not None and explicit != persisted:
                raise ValueError('Evaluation time differs from migration ledger')
            evaluation = persisted or explicit
            result = reconcile(session,report,admin_emails=os.environ['MIGRATION_ADMIN_EMAILS'].split(','),
                               auto_unfreeze_hours=float(os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0')), evaluated_at=evaluation)
            from db_reconcile_usage import reconcile_usage
            from db_reconcile_policy import reconcile_policies
            from db_reconcile_audit import reconcile_audits
            from db_reconcile_config import reconcile_configs
            from db_reconcile_legacy import reconcile_legacy
            from db_reconcile_empty import reconcile_empty_tables
            from db_secrets import ConfigCipher
            has_configs = any(key.startswith('system_config/') for _, key, _ in report['legacy_records'])
            cipher = ConfigCipher.from_env(key_id=os.environ.get('DATABASE_CONFIG_KEY_ID', 'migration-v1')) if has_configs else None
            for auxiliary_check in (reconcile_usage, reconcile_policies, reconcile_audits, reconcile_legacy, reconcile_empty_tables,
                                    lambda session, report: reconcile_configs(session, report, cipher=cipher)):
                auxiliary_result = auxiliary_check(session, report)
                result['checked_fields'] += auxiliary_result['checked_fields']
                result['mismatch_count'] += auxiliary_result['mismatch_count']
                result['mismatches'].update(auxiliary_result['mismatches'])
        if inventory(args.snapshot)['manifest'] != report['manifest']:
            raise ValueError('Source snapshot changed')
        print(json.dumps(result,sort_keys=True))
        return int(bool(result['mismatch_count']))
    except Exception as exc:
        print(f'Reconciliation blocked ({type(exc).__name__})')
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
