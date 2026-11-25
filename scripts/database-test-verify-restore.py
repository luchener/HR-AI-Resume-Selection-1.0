"""Read-only full restore verification for synthetic review databases only.

Requires DATABASE_TEST_URL pointed at review_test; restore must be review_restore
on the exact same host/port/user. No writes, resets or credential output.
"""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps' / 'backend'))
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db_security import migration_engine
from db_import_attestation import target_attestation, verify_attestation, ATTESTATION_KEY
from db_models import LegacyRecord
from db_secrets import ConfigCipher, EncryptedConfigRepository


def verify_restore(source_url, *, source_report=None):
    from db_test_guard import review_test_url
    url = review_test_url(source_url)
    engines = [migration_engine(url.render_as_string(hide_password=False)),
               migration_engine(url.set(database='review_restore').render_as_string(hide_password=False))]
    summaries = []
    versions = []
    configs = []
    try:
        for engine in engines:
            with Session(engine) as session, session.begin():
                session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                expected = session.scalar(select(LegacyRecord.payload).where(
                    LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == ATTESTATION_KEY))
                verify_attestation(session, expected)
                summaries.append(target_attestation(session))
                versions.append(session.execute(text('SELECT version_num FROM alembic_version')).scalar_one())
                cipher = ConfigCipher.from_env(key_id=os.environ.get('DATABASE_CONFIG_KEY_ID', 'migration-v1'))
                configs.append(EncryptedConfigRepository(session, cipher).get())
                if source_report is not None:
                    from db_reconcile import reconcile, verify_migration_context, evaluation_time
                    from db_reconcile_usage import reconcile_usage
                    from db_reconcile_policy import reconcile_policies
                    from db_reconcile_audit import reconcile_audits
                    from db_reconcile_legacy import reconcile_legacy
                    from db_reconcile_config import reconcile_configs
                    from db_reconcile_empty import reconcile_empty_tables
                    admins = os.environ['MIGRATION_ADMIN_EMAILS'].split(',')
                    hours = os.environ.get('MIGRATION_AUTO_UNFREEZE_HOURS', '0')
                    verify_migration_context(session,source_report,admin_emails=admins,auto_unfreeze_hours=hours)
                    stamp = session.scalar(select(LegacyRecord.payload).where(
                        LegacyRecord.root_name == '__migration__', LegacyRecord.record_key == 'evaluation_time'))
                    results = [reconcile(session,source_report,admin_emails=admins,auto_unfreeze_hours=float(hours),evaluated_at=evaluation_time(stamp['evaluated_at'])),
                               reconcile_configs(session,source_report,cipher=cipher)]
                    results.extend(check(session,source_report) for check in
                                   (reconcile_usage,reconcile_policies,reconcile_audits,reconcile_legacy,reconcile_empty_tables))
                    if any(result['mismatch_count'] for result in results):
                        raise ValueError('Independent restored source reconciliation failed')
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config()
        config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'apps' / 'backend' / 'migrations'))
        head = ScriptDirectory.from_config(config).get_current_head()
        if versions != [head] * 2 or summaries[0] != summaries[1] or configs[0] != configs[1] or configs[0] is None:
            raise ValueError('Restored database differs from source')
        print(f"PASS: restored {len(summaries[0]['tables'])} business tables, version {head}, target attestation and encrypted configuration")
    finally:
        for engine in engines:
            engine.dispose()

if __name__ == '__main__':
    try:
        verify_restore(os.environ['DATABASE_TEST_URL'])
    except Exception as exc:
        print(f'Restore verification blocked ({type(exc).__name__})', file=sys.stderr)
        sys.exit(1)
