"""Opt-in PostgreSQL integration tests against an EMPTY disposable database only.
Run with DATABASE_TEST_URL; refuses databases not named review_test.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import json
import unittest
import uuid
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError
from db_security import migration_engine, normalize_url
from sqlalchemy.engine import make_url
from db_models import Base, User, Resume, LegacyRecord
from db_migrate_json import inventory, apply_import


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable PostgreSQL test URL not configured')
class LiveDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ['DATABASE_TEST_URL']
        from db_test_guard import review_test_url
        review_test_url(cls.url)
        cls.engine = migration_engine(cls.url)
        with cls.engine.begin() as conn:
            if conn.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name <> 'alembic_version'")):
                raise RuntimeError('Test target must be empty')
        spec = importlib.util.spec_from_file_location('frozen', Path(__file__).parent/'migrations/versions/0001_initial_schema.py')
        cls.frozen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.frozen)
        from alembic.config import Config
        from alembic import command
        config = Config(str(Path(__file__).parent / 'alembic.ini'))
        config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        previous = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = cls.url
        try:
            command.upgrade(config, 'head')
        finally:
            if previous is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous

    @classmethod
    def tearDownClass(cls):
        # This suite deliberately leaves synthetic data for inspection. Container
        # cleanup is handled separately using its purpose label and exact name.
        cls.engine.dispose()

    def test_01_constraint_transaction_rollback(self):
        uid = uuid.uuid4()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                conn.execute(User.__table__.insert(), {'id': uid, 'username': 'rollback', 'password_hash': 'hash', 'password_salt': 'salt'})
                conn.execute(Resume.__table__.insert(), {'id': uuid.uuid4(), 'owner_user_id': uuid.uuid4()})
        with self.engine.connect() as conn:
            self.assertIsNone(conn.scalar(select(User.id).where(User.id == uid)))

    def test_02_case_insensitive_uniqueness(self):
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                for name in ['CaseTest', 'casetest']:
                    conn.execute(User.__table__.insert(), {'id': uuid.uuid4(), 'username': name, 'password_hash': 'hash', 'password_salt': 'salt'})

    def test_03_import_replay_and_changed_snapshot(self):
        os.environ['MIGRATION_ADMIN_EMAILS'] = ' Owner@example.test,owner@example.test '
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            uid, rid, jid, aid = [str(uuid.uuid4()) for _ in range(4)]
            records = {
                'users': {'user_id': uid, 'username': 'owner', 'email': 'owner@example.test', 'password_hash': 'hash', 'password_salt': 'salt'},
                'resumes': {'resume_id': rid, 'user_id': uid, 'content': "' OR 1=1; DROP TABLE users; --", 'processed': {'flag': True}},
                'jobs': {'job_id': jid, 'user_id': uid, 'resume_id': str(uuid.uuid4()), 'content': 'JD', 'processed': {}},
                'archives': {'archiveId': aid, 'user_id': uid, 'resume_id': str(uuid.uuid4()), 'job_id': jid, 'source_purged': True, 'custom_tags': ['safe']},
            }
            for table, row in records.items():
                path = root/table/'record.json'
                path.parent.mkdir()
                path.write_text(json.dumps(row), encoding='utf8')
            (root/'quota').mkdir()
            (root/'quota'/'limits.json').write_text(json.dumps({'quota_enabled': True, 'default_daily': 10, 'users': {'owner': {'daily_limit': 2, 'analysis_enabled': False}}}), encoding='utf8')
            (root/'usage').mkdir()
            (root/'usage'/f'{uid}.json').write_text(json.dumps({'2026-10-15': {'analysis': 1}}), encoding='utf8')
            (root/'admin_ops').mkdir()
            (root/'admin_ops'/'event.json').write_text(json.dumps({'op':'user_create', 'operator_id':uid, 'target_username':'owner', 'created_at':'2026-10-15T00:00:00Z', 'detail':'quarantined'}), encoding='utf8')
            from cryptography.fernet import Fernet
            os.environ['DATABASE_CONFIG_KEY'] = Fernet.generate_key().decode()
            (root/'system_config').mkdir()
            (root/'system_config'/'llm.json').write_text(json.dumps({'api_key':'synthetic-secret-no-plaintext'}), encoding='utf8')
            report = inventory(root)
            # Synthetic-only in-memory source evidence for backup roundtrip checks.
            type(self).source_report = report
            result = apply_import(report, self.url)
            self.assertEqual(result['encrypted_system_config'], 1)
            self.assertEqual(result['audit_logs'], 1)
            with self.engine.connect() as conn:
                stored = conn.execute(text('SELECT value::text FROM system_configs')).scalar_one()
                self.assertNotIn('synthetic-secret-no-plaintext', stored)
                legacy = conn.execute(text("SELECT payload::text FROM legacy_records WHERE root_name='system_config'")).scalar_one()
                self.assertNotIn('synthetic-secret-no-plaintext', legacy)
            self.assertEqual(result['quota_policies'], 2)
            self.assertEqual(result['quota_daily_usage'], 1)
            self.assertEqual(result['users'], 1)
            with self.engine.connect() as conn:
                evaluation_before = conn.execute(text("SELECT payload FROM legacy_records WHERE root_name='__migration__' AND record_key='evaluation_time'")).scalar_one()
                from datetime import datetime
                self.assertIsNotNone(datetime.fromisoformat(evaluation_before['evaluated_at']).tzinfo)
            self.assertEqual(apply_import(inventory(root), self.url), {'unchanged_snapshot': 1})
            with self.engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT payload FROM legacy_records WHERE root_name='__migration__' AND record_key='evaluation_time'")).scalar_one(), evaluation_before)
            with self.engine.connect() as conn:
                self.assertEqual(conn.scalar(select(User.is_super_admin)), True)
                self.assertEqual(conn.scalar(select(Resume.raw_content)), records['resumes']['content'])
            from db_reconcile import reconcile
            from sqlalchemy.orm import Session
            with Session(self.engine) as session:
                summary = reconcile(session,report,admin_emails=['owner@example.test'])
                self.assertEqual(summary['mismatch_count'],0)
            with self.engine.begin() as conn:
                conn.execute(text('UPDATE users SET password_version=2'))
            with Session(self.engine) as session:
                drift = reconcile(session,report,admin_emails=['owner@example.test'])
                self.assertEqual(drift['mismatches'],{'users.password_version':1})
                self.assertNotIn('hash',str(drift))
            with self.engine.begin() as conn:
                conn.execute(text('UPDATE users SET password_version=1'))
            from db_reconcile_usage import reconcile_usage
            from db_reconcile_policy import reconcile_policies
            from db_reconcile_audit import reconcile_audits
            from db_reconcile_config import reconcile_configs
            from db_reconcile_legacy import reconcile_legacy
            from db_secrets import ConfigCipher
            cipher = ConfigCipher.from_env(key_id='migration-v1')
            config_check = lambda session, report: reconcile_configs(session, report, cipher=cipher)
            for checker in (reconcile_usage, reconcile_policies, reconcile_audits, config_check, reconcile_legacy):
                with Session(self.engine) as session:
                    self.assertEqual(checker(session,report)['mismatch_count'],0)
            from unittest.mock import patch
            from contextlib import redirect_stdout
            import io
            from db_reconcile import main as reconcile_main
            output = io.StringIO()
            with patch.dict(os.environ, {'DATABASE_RECONCILE_URL': self.url}), patch('sys.argv', ['db_reconcile.py', str(root)]), redirect_stdout(output):
                self.assertEqual(reconcile_main(),0)
            self.assertEqual(json.loads(output.getvalue())['mismatch_count'],0)
            self.assertNotIn('synthetic-secret',output.getvalue())
            auxiliary_mutations = [
                (reconcile_usage,'UPDATE quota_daily_usage SET used=99','UPDATE quota_daily_usage SET used=1','quota_daily_usage.used'),
                (reconcile_policies,"UPDATE quota_policies SET enabled=false WHERE scope_key='global'","UPDATE quota_policies SET enabled=true WHERE scope_key='global'",'quota_policies.enabled'),
                (reconcile_audits,"UPDATE audit_logs SET action='changed'","UPDATE audit_logs SET action='user_create'",'audit_logs.action'),
                (reconcile_audits,"UPDATE audit_logs SET ip_address='127.0.0.1'","UPDATE audit_logs SET ip_address=NULL",'audit_logs.ip_address'),
                (lambda session, report: reconcile(session,report,admin_emails=['owner@example.test']),
                 "UPDATE resumes SET processed_data=jsonb_build_object('flag',1)",
                 "UPDATE resumes SET processed_data=jsonb_build_object('flag',true)",'resumes.processed_data'),
            ]
            for checker, mutate, restore, field in auxiliary_mutations:
                with self.engine.begin() as conn:
                    conn.execute(text(mutate))
                try:
                    with Session(self.engine) as session:
                        self.assertEqual(checker(session,report)['mismatches'],{field:1})
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(text(restore))
            from db_models import SystemConfig
            with self.engine.connect() as conn:
                original_config = conn.execute(select(SystemConfig.__table__)).mappings().one()
                marker = conn.execute(text("SELECT payload FROM legacy_records WHERE root_name='system_config'")).scalar_one()
            altered = cipher.encrypt({'system_config/llm.json':{'api_key':'changed-synthetic-secret'}})
            with self.engine.begin() as conn:
                conn.execute(SystemConfig.__table__.update().values(value=altered))
                conn.execute(text("UPDATE legacy_records SET payload=CAST(:payload AS jsonb) WHERE root_name='system_config'"), {'payload':json.dumps({'value':{'api_key':'synthetic-secret-no-plaintext'}})})
            try:
                with Session(self.engine) as session:
                    self.assertEqual(config_check(session,report)['mismatches'],{'system_configs.changed_documents':1})
                    self.assertEqual(reconcile_legacy(session,report)['mismatches'],{'legacy_records.changed_payloads':1})
                output = io.StringIO()
                with patch.dict(os.environ, {'DATABASE_RECONCILE_URL': self.url}), patch('sys.argv', ['db_reconcile.py', str(root)]), redirect_stdout(output):
                    self.assertEqual(reconcile_main(),1)
                self.assertNotIn('synthetic-secret',output.getvalue())
            finally:
                with self.engine.begin() as conn:
                    conn.execute(SystemConfig.__table__.update().values(value=original_config['value'],updated_at=original_config['updated_at']))
                    conn.execute(text("UPDATE legacy_records SET payload=CAST(:payload AS jsonb) WHERE root_name='system_config'"), {'payload':json.dumps(marker)})
            self.assertEqual(apply_import(inventory(root), self.url), {'unchanged_snapshot':1})
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE resumes SET legacy_payload=jsonb_set(legacy_payload,'{processed,flag}','1'::jsonb)"))
            try:
                with self.assertRaisesRegex(ValueError,'Target content differs'):
                    apply_import(inventory(root),self.url)
            finally:
                with self.engine.begin() as conn:
                    conn.execute(text("UPDATE resumes SET legacy_payload=jsonb_set(legacy_payload,'{processed,flag}','true'::jsonb)"))
            self.assertEqual(apply_import(inventory(root),self.url),{'unchanged_snapshot':1})
            # Copies in legacy_payload remain intact while authoritative fields drift.
            mutations = [
                ('UPDATE users SET password_version=2', 'UPDATE users SET password_version=1'),
                ('UPDATE quota_daily_usage SET used=99', 'UPDATE quota_daily_usage SET used=1'),
                ("UPDATE audit_logs SET action='changed'", "UPDATE audit_logs SET action='user_create'"),
                ("INSERT INTO system_configs (key,value) VALUES ('unexpected','{}')", "DELETE FROM system_configs WHERE key='unexpected'"),
            ]
            for mutate, restore in mutations:
                with self.engine.begin() as conn:
                    conn.execute(text(mutate))
                with self.assertRaisesRegex(ValueError, 'replay certification blocked'):
                    apply_import(inventory(root), self.url)
                with self.engine.begin() as conn:
                    conn.execute(text(restore))
                self.assertEqual(apply_import(inventory(root), self.url), {'unchanged_snapshot': 1})
            with self.engine.begin() as conn:
                config_row = dict(conn.execute(text('SELECT * FROM system_configs')).mappings().one())
                encrypted = config_row['value']
                conn.execute(text('DELETE FROM system_configs'))
            with self.assertRaisesRegex(ValueError, 'configuration missing'):
                apply_import(inventory(root), self.url)
            from db_models import SystemConfig
            with self.engine.begin() as conn:
                conn.execute(SystemConfig.__table__.insert().values(**config_row))
            self.assertEqual(apply_import(inventory(root), self.url), {'unchanged_snapshot': 1})
            path = root/'users'/'record.json'
            records['users']['password_version'] = 2
            path.write_text(json.dumps(records['users']), encoding='utf8')
            with self.assertRaises(ValueError):
                apply_import(inventory(root), self.url)


if __name__ == '__main__':
    unittest.main()
