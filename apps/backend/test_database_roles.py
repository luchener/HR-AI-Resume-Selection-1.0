"""Explicit role checks for disposable PostgreSQL review_test only."""
import os
import unittest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from db_security import migration_engine


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class RoleTests(unittest.TestCase):
    def test_staged_table_access_and_denied_operations(self):
        value = os.environ['DATABASE_TEST_URL']
        url = make_url(value)
        if url.query or url.database != 'review_test' or url.host != '127.0.0.1' or url.port != 15439:
            self.fail('Only isolated review_test endpoint allowed')
        app = migration_engine(url.set(username='resume_app', password='review-app-only').render_as_string(hide_password=False))
        reader = migration_engine(url.set(username='resume_readonly', password='review-reader-only').render_as_string(hide_password=False))
        bootstrap = migration_engine(value)
        self.addCleanup(app.dispose)
        self.addCleanup(reader.dispose)
        self.addCleanup(bootstrap.dispose)
        with bootstrap.connect() as conn:
            owners = conn.execute(text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public'")).scalars().all()
            self.assertEqual(owners, ['resume_migrator'])
        from db_runtime import verify_runtime
        self.assertTrue(verify_runtime(app))
        with self.assertRaisesRegex(ValueError, 'Unsafe runtime role'):
            verify_runtime(bootstrap)
        with app.connect() as conn:
            txn = conn.begin()
            for table in ('quota_policies', 'quota_daily_usage', 'quota_reservations', 'job_requirements', 'evidence_assessments', 'system_configs', 'analysis_task_inputs', 'original_gc',
                          'auth_limits', 'auth_challenges', 'auth_invites', 'auth_login_state', 'invite_requests'):
                conn.execute(text(f'SELECT * FROM {table} LIMIT 0'))
                conn.execute(text(f'DELETE FROM {table} WHERE false'))
            conn.execute(text("INSERT INTO audit_logs (action) VALUES ('role_test')"))
            import uuid
            from datetime import date
            from db_check_schema import metadata
            tables = metadata().tables
            uid, rid, jid, aid, req = [uuid.uuid4() for _ in range(5)]
            conn.execute(tables['users'].insert(), {'id':uid,'username':uid.hex,'password_hash':'synthetic','password_salt':'synthetic'})
            conn.execute(tables['resumes'].insert(), {'id':rid,'owner_user_id':uid})
            conn.execute(tables['jobs'].insert(), {'id':jid,'owner_user_id':uid})
            conn.execute(tables['analyses'].insert(), {'id':aid,'owner_user_id':uid,'resume_id':rid,'job_id':jid})
            conn.execute(tables['job_requirements'].insert(), {'id':req,'owner_user_id':uid,'job_id':jid,'snapshot_version':'role-test','requirement_key':'python','text':'Python'})
            conn.execute(tables['evidence_assessments'].insert(), {'id':uuid.uuid4(),'owner_user_id':uid,'job_id':jid,'analysis_id':aid,'requirement_id':req,'status':'not_mentioned','effective_status':'not_mentioned'})
            conn.execute(tables['quota_policies'].insert(), {'scope_key':str(uid),'user_id':uid})
            conn.execute(tables['quota_daily_usage'].insert(), {'user_id':uid,'date':date(2026,1,1)})
            conn.execute(tables['quota_reservations'].insert(), {'request_id':uuid.uuid4(),'user_id':uid,'date':date(2026,1,1),'count':1})
            from db_secrets import ConfigCipher
            from cryptography.fernet import Fernet
            envelope = ConfigCipher(Fernet.generate_key(), key_id='role-test').encrypt({'synthetic': True})
            conn.execute(tables['system_configs'].insert(), {'key':'role-test','value':envelope})
            txn.rollback()
        denied = ['UPDATE audit_logs SET action=action WHERE false',
                  'DELETE FROM audit_logs WHERE false', 'TRUNCATE audit_logs',
                  'SELECT * FROM legacy_records LIMIT 0', 'SELECT * FROM alembic_version',
                  'CREATE TABLE public.forbidden_table (id int)',
                  'CREATE TEMP TABLE forbidden_temp (id int)',
                  "SELECT setval('audit_logs_id_seq', 1)"]
        for sql in denied:
            with self.subTest(sql=sql), self.assertRaises(DBAPIError) as caught:
                with app.begin() as conn:
                    conn.execute(text(sql))
            self.assertEqual(caught.exception.orig.sqlstate, '42501')
        with reader.connect() as conn:
            for view in ('user_status', 'resume_status', 'analysis_status', 'audit_events'):
                conn.execute(text(f'SELECT * FROM reporting.{view} LIMIT 0'))
        for sql in ('SELECT * FROM public.users LIMIT 0', 'SELECT * FROM public.system_configs LIMIT 0',
                    'SELECT * FROM public.original_gc LIMIT 0',
                    'SELECT * FROM public.auth_limits LIMIT 0', 'SELECT * FROM public.auth_challenges LIMIT 0',
                    'SELECT * FROM public.auth_invites LIMIT 0', 'SELECT * FROM public.auth_login_state LIMIT 0',
                    'SELECT * FROM public.invite_requests LIMIT 0',
                    'SELECT * FROM public.analysis_task_inputs LIMIT 0',
                    'SELECT * FROM public.quota_policies LIMIT 0', 'SELECT * FROM public.evidence_assessments LIMIT 0',
                    'DELETE FROM reporting.user_status WHERE false'):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError) as caught:
                with reader.begin() as conn:
                    conn.execute(text(sql))
            self.assertEqual(caught.exception.orig.sqlstate, '42501')

    def test_minimum_privileges(self):
        value = os.environ['DATABASE_TEST_URL']
        if make_url(value).database != 'review_test':
            self.fail('Only review_test allowed')
        engine = migration_engine(value)
        try:
            with engine.connect() as conn:
                for role in ('resume_migrator', 'resume_app', 'resume_readonly'):
                    flags = conn.execute(text('SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname=:role'), {'role': role}).one()
                    self.assertFalse(any(flags))
                self.assertFalse(conn.scalar(text("SELECT has_table_privilege('resume_readonly','public.users','SELECT')")))
                self.assertTrue(conn.scalar(text("SELECT has_table_privilege('resume_readonly','reporting.user_status','SELECT')")))
                self.assertFalse(conn.scalar(text("SELECT has_table_privilege('resume_app','public.audit_logs','DELETE')")))
                self.assertFalse(conn.scalar(text("SELECT has_schema_privilege('resume_app','public','CREATE')")))
                self.assertFalse(conn.scalar(text("SELECT has_table_privilege('resume_app','public.legacy_records','SELECT')")))
            for role, password, sql in [('resume_readonly','review-reader-only','SELECT password_hash FROM users'), ('resume_app','review-app-only','DELETE FROM audit_logs')]:
                role_url = make_url(value).set(username=role, password=password).render_as_string(hide_password=False)
                restricted = migration_engine(role_url)
                try:
                    with self.assertRaises(DBAPIError):
                        with restricted.begin() as conn:
                            conn.execute(text(sql))
                finally:
                    restricted.dispose()
        finally:
            engine.dispose()

if __name__ == '__main__':
    unittest.main()
