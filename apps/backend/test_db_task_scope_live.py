"""Task owner/job linkage constraints on a disposable database."""
import os
import uuid
import unittest
from datetime import date, datetime, timedelta, timezone
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from db_security import migration_engine
from db_check_schema import metadata

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class TaskScopeTests(unittest.TestCase):
    def setUp(self):
        from db_test_guard import review_test_url
        review_test_url(os.environ['DATABASE_TEST_URL'])

    def test_task_lifecycle_partial_success_and_stale_recovery(self):
        from db_task_lifecycle import create_task, start_task, record_batch, recover_stale_tasks
        from db_quota import reserve, EffectiveQuota
        from db_models import AnalysisTask
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False)); self.addCleanup(engine.dispose)
        t = metadata().tables; owner, job, task, req = [uuid.uuid4() for _ in range(4)]
        with engine.begin() as conn:
            conn.execute(t['users'].insert(), {'id':owner,'username':owner.hex,'password_hash':'synthetic','password_salt':'synthetic'})
            conn.execute(t['jobs'].insert(), {'id':job,'owner_user_id':owner})
            create_task(conn, owner_user_id=owner, job_id=job, total_candidates=2, request_id=str(req), task_id=task)
            conn.execute(t['quota_daily_usage'].insert(), {'user_id':owner,'date':date(2026,1,1)})
            reserve(conn, request_id=req, user_id=owner, task_id=task, day=date(2026,1,1), count=2, policy=EffectiveQuota(True,5,False))
            start_task(conn, task_id=task, owner_user_id=owner)
            row = record_batch(conn, task_id=task, owner_user_id=owner, batch_id='batch-1', completed=1, failed=1)
            self.assertEqual(row['status'],'partial_success')
            self.assertIsNotNone(row['finished_at'])
            from db_task_lifecycle import finish_task
            self.assertEqual(finish_task(conn, task_id=task, owner_user_id=owner)['status'], 'partial_success')
            same = record_batch(conn, task_id=task, owner_user_id=owner, batch_id='batch-1', completed=1, failed=1)
            self.assertEqual(same['completed_candidates'],1)
            with self.assertRaises(ValueError):
                record_batch(conn, task_id=task, owner_user_id=owner, batch_id='batch-1', completed=2, failed=0)
        with engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(sa.select(t['quota_daily_usage'].c.used,t['quota_daily_usage'].c.reserved).where(t['quota_daily_usage'].c.user_id==owner)).one()),(1,0))

    def test_stale_recovery_is_tenant_scoped_and_idempotent(self):
        from db_task_lifecycle import create_task, recover_stale_tasks
        from db_quota import reserve, EffectiveQuota
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False)); self.addCleanup(engine.dispose)
        t = metadata().tables; owner, other, job, task, other_task, req, other_req = [uuid.uuid4() for _ in range(7)]
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        with engine.begin() as conn:
            for uid in (owner, other):
                conn.execute(t['users'].insert(), {'id':uid,'username':uid.hex,'password_hash':'synthetic','password_salt':'synthetic'})
                conn.execute(t['quota_daily_usage'].insert(), {'user_id':uid,'date':date(2026,1,1)})
            conn.execute(t['jobs'].insert(), {'id':job,'owner_user_id':owner})
            create_task(conn, owner_user_id=owner, job_id=job, total_candidates=1, request_id=str(req), task_id=task)
            create_task(conn, owner_user_id=other, job_id=None, total_candidates=1, request_id=str(other_req), task_id=other_task)
            conn.execute(t['analysis_tasks'].update().where(t['analysis_tasks'].c.id.in_([task,other_task])).values(created_at=old))
            reserve(conn, request_id=req, user_id=owner, task_id=task, day=date(2026,1,1), count=1, policy=EffectiveQuota(True,5,False))
            reserve(conn, request_id=other_req, user_id=other, task_id=other_task, day=date(2026,1,1), count=1, policy=EffectiveQuota(True,5,False))
            self.assertEqual(recover_stale_tasks(conn, before=datetime.now(timezone.utc)-timedelta(hours=1), owner_user_id=owner, confirmed_stopped_task_ids={task}),1)
        with engine.connect() as conn:
            rows = conn.execute(sa.select(t['analysis_tasks'].c.status).where(t['analysis_tasks'].c.id.in_([task,other_task])).order_by(t['analysis_tasks'].c.id)).all()
            self.assertEqual(sorted(r[0] for r in rows), ['failed','pending'])
            usage = conn.execute(sa.select(t['quota_daily_usage'].c.user_id,t['quota_daily_usage'].c.used,t['quota_daily_usage'].c.reserved)).all()
            self.assertEqual(sorted((str(r[0]),r[1],r[2]) for r in usage if r[0] in (owner,other)), sorted([(str(owner),0,0),(str(other),0,1)]))

    def test_task_reservation_mismatch_fails_before_settlement(self):
        from db_task_lifecycle import create_task, start_task, record_batch
        from db_quota import reserve, EffectiveQuota
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False)); self.addCleanup(engine.dispose)
        t = metadata().tables; owner, job, task, req = [uuid.uuid4() for _ in range(4)]
        with engine.begin() as conn:
            conn.execute(t['users'].insert(), {'id':owner,'username':owner.hex,'password_hash':'synthetic','password_salt':'synthetic'})
            conn.execute(t['jobs'].insert(), {'id':job,'owner_user_id':owner})
            conn.execute(t['quota_daily_usage'].insert(), {'user_id':owner,'date':date(2026,1,1)})
            create_task(conn, owner_user_id=owner, job_id=job, total_candidates=1, request_id=str(req), task_id=task)
            other = uuid.uuid4()
            conn.execute(t['users'].insert(), {'id':other,'username':other.hex,'password_hash':'synthetic','password_salt':'synthetic'})
            conn.execute(t['quota_daily_usage'].insert(), {'user_id':other,'date':date(2026,1,1)})
            reserve(conn, request_id=req, user_id=other, task_id=None, day=date(2026,1,1), count=1, policy=EffectiveQuota(True,5,False))
            with self.assertRaises(ValueError):
                start_task(conn, task_id=task, owner_user_id=owner)
            with self.assertRaises(ValueError):
                record_batch(conn, task_id=task, owner_user_id=owner, batch_id='bad-reservation', completed=1)


    def test_concurrent_request_creation_and_recovery_fencing(self):
        from concurrent.futures import ThreadPoolExecutor
        from db_test_guard import review_test_url
        from db_task_lifecycle import create_task, start_task, record_batch, recover_stale_tasks, finish_task
        from db_quota import reserve, EffectiveQuota
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        engine = migration_engine(url.render_as_string(hide_password=False)); self.addCleanup(engine.dispose)
        t = metadata().tables
        owner, job, req = [uuid.uuid4() for _ in range(3)]
        day = date(2026, 1, 1)
        with engine.begin() as conn:
            conn.execute(t['users'].insert(), dict(id=owner,username=owner.hex,password_hash='synthetic',password_salt='synthetic'))
            conn.execute(t['jobs'].insert(), dict(id=job,owner_user_id=owner))
        def create(_):
            with engine.begin() as conn:
                return create_task(conn, owner_user_id=owner, job_id=job, total_candidates=10,
                                   request_id=str(req).upper())['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(4)))
        self.assertEqual(len(set(ids)), 1)
        task = ids[0]
        with self.assertRaises(ValueError), engine.begin() as conn:
            create_task(conn, owner_user_id=owner, job_id=job, total_candidates=9, request_id=str(req))
        with engine.begin() as conn:
            reserve(conn, request_id=req, user_id=owner, task_id=task, day=day, count=10, policy=EffectiveQuota(True,20,False))
            start_task(conn, task_id=task, owner_user_id=owner)
            row = record_batch(conn, task_id=task, owner_user_id=owner, batch_id='first', completed=2, failed=1)
            self.assertEqual(row['status'], 'running')
            self.assertIsNone(row['finished_at'])
            usage = conn.execute(sa.select(t['quota_daily_usage']).where(t['quota_daily_usage'].c.user_id==owner)).mappings().one()
            self.assertEqual((usage['used'],usage['reserved']), (0,10))
            conn.execute(t['analysis_tasks'].update().where(t['analysis_tasks'].c.id==task).values(created_at=datetime.now(timezone.utc)-timedelta(hours=2)))
        cutoff = datetime.now(timezone.utc)-timedelta(hours=1)
        # Failed recovery transaction preserves running state and reservation.
        with self.assertRaises(RuntimeError), engine.begin() as conn:
            recover_stale_tasks(conn, before=cutoff, owner_user_id=owner, confirmed_stopped_task_ids={task})
            raise RuntimeError('operator transaction aborted')
        with engine.begin() as conn:
            self.assertEqual(recover_stale_tasks(conn, before=cutoff, owner_user_id=owner, confirmed_stopped_task_ids={task}),1)
            self.assertEqual(recover_stale_tasks(conn, before=cutoff, owner_user_id=owner, confirmed_stopped_task_ids={task}),0)
            row = finish_task(conn, task_id=task, owner_user_id=owner)
            self.assertEqual((row['status'],row['completed_candidates'],row['failed_candidates']), ('partial_success',2,8))
            usage = conn.execute(sa.select(t['quota_daily_usage']).where(t['quota_daily_usage'].c.user_id==owner)).mappings().one()
            self.assertEqual((usage['used'],usage['reserved']), (2,0))
        with self.assertRaises(ValueError), engine.begin() as conn:
            record_batch(conn, task_id=task, owner_user_id=owner, batch_id='first', completed=2, failed=1)
        with self.assertRaises(ValueError), engine.begin() as conn:
            start_task(conn, task_id=task, owner_user_id=owner)
        with engine.begin() as conn:
            zero = create_task(conn, owner_user_id=owner, job_id=job, total_candidates=0, request_id=str(uuid.uuid4()))
            self.assertEqual(zero['status'], 'completed')
            self.assertIsNotNone(zero['finished_at'])
            self.assertEqual(finish_task(conn, task_id=zero['id'], owner_user_id=owner)['status'], 'completed')

    def test_task_ownership_and_job_linkage(self):
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(engine.dispose)
        t = metadata().tables
        u1,u2,r,j1,j2,task = [uuid.uuid4() for _ in range(6)]
        with engine.begin() as conn:
            for uid in (u1,u2):
                conn.execute(t['users'].insert(), {'id':uid,'username':uid.hex,'password_hash':'synthetic','password_salt':'synthetic'})
            conn.execute(t['resumes'].insert(), {'id':r,'owner_user_id':u1})
            for jid in (j1,j2):
                conn.execute(t['jobs'].insert(), {'id':jid,'owner_user_id':u1})
            conn.execute(t['analysis_tasks'].insert(), {'id':task,'owner_user_id':u1,'job_id':j1})
            for uid in (u1,u2):
                conn.execute(t['quota_daily_usage'].insert(), {'user_id':uid,'date':date(2026,1,1)})
        for owner, task_id in ((u2,task),(u1,uuid.uuid4())):
            with self.assertRaises(IntegrityError) as caught:
                with engine.begin() as conn:
                    conn.execute(t['quota_reservations'].insert(), {'request_id':uuid.uuid4(),'task_id':task_id,'user_id':owner,'date':date(2026,1,1),'count':1})
            self.assertEqual(caught.exception.orig.sqlstate, '23503')
        with self.assertRaises(IntegrityError) as caught:
            with engine.begin() as conn:
                conn.execute(t['analyses'].insert(), {'id':uuid.uuid4(),'owner_user_id':u1,'resume_id':r,'job_id':j2,'task_id':task})
        self.assertEqual(caught.exception.orig.sqlstate, '23503')
        with engine.begin() as conn:
            conn.execute(t['analyses'].insert(), {'id':uuid.uuid4(),'owner_user_id':u1,'resume_id':r,'job_id':j1,'task_id':task})
            conn.execute(t['quota_reservations'].insert(), {'request_id':uuid.uuid4(),'task_id':task,'user_id':u1,'date':date(2026,1,1),'count':1})

    def test_worker_lease_migration_roundtrip_preserves_legacy_claims_and_schema(self):
        import subprocess
        import sys
        from pathlib import Path
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from db_task_lifecycle import recover_expired_tasks, renew_worker_lease
        from db_task_inputs import task_inputs
        from db_test_guard import review_test_url

        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(engine.dispose)
        owner, task, token = [uuid.uuid4() for _ in range(3)]
        tables = metadata().tables
        with engine.begin() as conn:
            conn.execute(tables['users'].insert(), dict(id=owner, username=owner.hex,
                password_hash='synthetic', password_salt='synthetic'))
            conn.execute(tables['analysis_tasks'].insert(), dict(id=task,
                owner_user_id=owner, status='running', total_candidates=1))
            conn.execute(task_inputs.insert(), dict(task_id=task, owner_user_id=owner,
                password_version=1, request_digest='c' * 64,
                snapshot={'resumes': []}, worker_token=token))
        env = dict(os.environ, DATABASE_URL=url.set(username='resume_migrator',
            password='review-migrator-only').render_as_string(hide_password=False))
        # The synthetic fixture explicitly exports/discards progress before
        # downgrading across the event-log migration. Production cannot do this.
        with engine.begin() as conn:
            conn.execute(sa.text('DELETE FROM analysis_task_events'))
        for action, revision in (('downgrade', '0020_change_log_insert_guard'),
                                 ('upgrade', 'head')):
            result = subprocess.run([sys.executable, '-m', 'alembic', action, revision],
                cwd=Path(__file__).parent, env=env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            columns = {column['name'] for column in sa.inspect(engine).get_columns('analysis_task_inputs')}
            self.assertEqual('lease_until' in columns, action == 'upgrade')
        with engine.begin() as conn:
            conn.execute(sa.text('GRANT SELECT, INSERT ON analysis_task_events, analysis_task_artifacts TO resume_app'))
        with engine.begin() as conn:
            row = conn.execute(sa.select(task_inputs).where(task_inputs.c.task_id == task)).mappings().one()
            self.assertEqual((row['owner_user_id'], row['worker_token'], row['lease_until']),
                             (owner, token, None))
            self.assertEqual(row['snapshot'], {'resumes': []})
            self.assertFalse(renew_worker_lease(conn, task_id=task,
                owner_user_id=owner, worker_token=token))
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=owner), 0)
            self.assertEqual(compare_metadata(MigrationContext.configure(conn,
                opts={'compare_type': True}), metadata()), [])

if __name__ == '__main__':
    unittest.main()
