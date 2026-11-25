"""Local HTTP/SSE/worker and drain acceptance against the guarded real database."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from time import time
from unittest.mock import Mock, patch
from uuid import uuid4

import jwt
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_analysis_service import write_analysis_batch
from db_analysis_workflow import AnalysisWorkflow, WorkflowError
from db_auth import AuthService
from db_drain import DrainTracker, DrainingError
from db_http_preview import create_preview_app
from db_models import AnalysisTask, Job, Resume
from db_quota import quota_daily_usage, quota_policies, quota_reservations
from db_runtime import build_runtime
from db_security import migration_engine
from db_store import Store
from db_task_inputs import task_inputs
from db_task_lifecycle import recover_expired_tasks, recover_stale_tasks
from db_test_guard import review_test_url


def report(score=82):
    return {'final_score': score, 'fit_tag': '较匹配', 'fit_grade': 'B',
            'summary': '合成项目经验', 'recruitment_recommendation': '优先面试',
            'score_breakdown': {'skills_projects': 20},
            'agent_validation': {'passed': True}, 'evidence_assessments': []}


class DrainTests(unittest.TestCase):
    def test_refuses_new_work_and_waits_for_each_activity_kind(self):
        tracker = DrainTracker()
        closes = [tracker.enter(kind) for kind in ('requests', 'streams', 'workers')]
        self.assertFalse(tracker.wait(0))
        tracker.begin()
        with self.assertRaises(DrainingError):
            tracker.enter('workers')
        for close in closes:
            self.assertFalse(tracker.wait(0))
            close()
            close()
        self.assertTrue(tracker.wait(0))
        self.assertEqual(tracker.status(), {'draining': True, 'requests': 0, 'streams': 0, 'workers': 0})

    def test_activity_exception_releases_and_bounds_are_checked(self):
        tracker = DrainTracker()
        with self.assertRaises(RuntimeError), tracker.activity('workers'):
            raise RuntimeError('interrupted')
        self.assertEqual(tracker.status()['workers'], 0)
        with self.assertRaises(ValueError):
            tracker.enter('unknown')
        for timeout in (-1, 61):
            with self.assertRaises(ValueError):
                tracker.wait(timeout)


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AnalysisWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.admin = migration_engine(url.render_as_string(hide_password=False))
        cls.engine, cls.sessions = build_runtime(url.set(
            username='resume_app', password='review-app-only').render_as_string(hide_password=False))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.admin.dispose()

    def setUp(self):
        self.auth = AuthService(self.sessions)
        self.user, error = self.auth.create_user('wf-' + uuid4().hex, 'SyntheticPassword9')
        self.assertIsNone(error)
        self.owner = self.user['user_id']
        self.store = Store(session_factory=self.sessions)
        self.resumes = [self.store.save_resume('合成简历 ' + str(index), {}, self.owner) for index in range(4)]
        self.job = self.store.save_job(None, '合成岗位', {}, self.owner)
        with self.admin.begin() as conn:
            conn.execute(insert(quota_policies).values(scope_key='global', mode='limited', daily_limit=100,
                quota_enabled=True).on_conflict_do_update(index_elements=['scope_key'],
                set_={'quota_enabled': True, 'mode': 'limited', 'daily_limit': 100}))
            conn.execute(quota_policies.insert().values(scope_key=self.owner, user_id=self.owner,
                                                       mode='limited', daily_limit=3, email_notify=False))
        self.release = Event()
        self.started = Event()
        self.analyzer = Mock(return_value=report())
        self.workflow = AnalysisWorkflow(self.sessions, self.analyzer, max_workers=4, max_pending=8)
        self.secret = 'isolated-workflow-jwt-secret-48-bytes-for-synthetic-tests'
        self.app = create_preview_app(self.store, self.auth, self.secret, analysis_workflow=self.workflow)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + jwt.encode({
            'user_id': self.owner, 'username': self.user['username'], 'pwd_ver': 1,
            'iss': 'resume-matcher', 'jti': str(uuid4()), 'iat': time(), 'exp': time() + 3600,
        }, self.secret, algorithm='HS256'), 'Idempotency-Key': str(uuid4())}

    def tearDown(self):
        self.release.set()
        self.workflow.close()
        with self.admin.begin() as conn:
            ids = conn.execute(sa.select(AnalysisTask.id).where(AnalysisTask.owner_user_id == self.owner)).scalars().all()
            if ids:
                recover_stale_tasks(conn, before=datetime.now(timezone.utc) + timedelta(minutes=1),
                                    confirmed_stopped_task_ids=ids)

    def submit(self, resumes=None, key=None):
        return self.workflow.submit(owner_user_id=self.owner, password_version=1,
            request_id=key or self.headers['Idempotency-Key'], job_id=self.job,
            resume_ids=resumes or self.resumes[:1])

    def snapshot(self, task):
        return self.workflow.snapshot(task_id=task, owner_user_id=self.owner, password_version=1)

    def usage(self):
        with self.admin.connect() as conn:
            rows = conn.execute(sa.select(quota_daily_usage).where(quota_daily_usage.c.user_id == self.owner)).mappings().all()
            return sum(row['used'] for row in rows), sum(row['reserved'] for row in rows)

    def blocking_analyzer(self, **kwargs):
        self.started.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError('test analyzer release timeout')
        return report()

    def post(self, client=None, resumes=None, streaming=False, headers=None):
        return (client or self.client).post('/api/v1/resumes/hr-analysis' + ('?stream=true' if streaming else ''),
            json={'resume_ids': resumes or self.resumes[:1], 'job_id': self.job},
            headers=headers or self.headers, buffered=not streaming)

    def test_http_result_is_durable_and_default_preview_stays_read_only(self):
        response = self.post(resumes=self.resumes[:3])
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual((data['status'], len(data['analyses'])), ('completed', 3))
        self.assertEqual(self.usage(), (3, 0))
        self.assertTrue(all(item['hr_analysis'] == report() for item in data['analyses']))
        self.assertEqual(self.snapshot(data['task_id']), data)
        self.assertEqual(response.headers['X-DB-Preview'], 'analysis-local')
        self.assertNotIn('worker_token', response.get_data(as_text=True))
        read_only = create_preview_app(self.store, self.auth, self.secret).test_client()
        self.assertEqual(self.post(client=read_only).status_code, 503)

    def test_worker_observes_committed_inputs_without_holding_db_transaction(self):
        observed = []
        def inspect(**kwargs):
            with self.admin.connect() as conn:
                inputs = conn.execute(sa.select(task_inputs).where(task_inputs.c.owner_user_id == self.owner)).mappings().one()
                observed.append(inputs['worker_token'] is not None)
                self.assertEqual(conn.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE usename='resume_app' AND state='idle in transaction'")), 0)
            return report()
        self.analyzer.side_effect = inspect
        task, future = self.submit()
        self.assertTrue(future.result(timeout=10))
        self.assertEqual(observed, [True])
        self.assertEqual(self.snapshot(task)['status'], 'completed')

    def test_worker_heartbeats_extend_a_blocked_provider_call_lease(self):
        self.workflow.close()
        self.workflow = AnalysisWorkflow(
            self.sessions, self.analyzer, max_workers=2, max_pending=4,
            worker_lease_seconds=6, heartbeat_seconds=1,
            recovery_interval_seconds=1,
        )
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        with self.admin.connect() as conn:
            first = conn.scalar(sa.select(task_inputs.c.lease_until).where(
                task_inputs.c.task_id == task))
        self.assertIsNotNone(first)
        Event().wait(1.3)
        with self.admin.connect() as conn:
            renewed = conn.scalar(sa.select(task_inputs.c.lease_until).where(
                task_inputs.c.task_id == task))
        self.assertGreater(renewed, first)
        self.assertFalse(future.done())
        self.release.set()
        self.assertTrue(future.result(timeout=10))
        self.assertEqual(self.snapshot(task)['status'], 'completed')

    def test_expired_worker_lease_is_fenced_settled_and_never_redispatched(self):
        self.workflow.close()
        self.workflow = AnalysisWorkflow(
            self.sessions, self.analyzer, max_workers=2, max_pending=4,
            worker_lease_seconds=30, heartbeat_seconds=10,
            recovery_interval_seconds=1,
        )
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        with self.admin.begin() as conn:
            conn.execute(task_inputs.update().where(
                task_inputs.c.task_id == task,
            ).values(lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")))
        deadline = time() + 8
        while time() < deadline:
            with self.admin.connect() as conn:
                state = conn.execute(sa.select(
                    AnalysisTask.status, AnalysisTask.error_code,
                ).where(AnalysisTask.id == task)).one()
            if state.error_code == 'worker_lease_expired':
                break
            Event().wait(0.05)
        self.assertEqual((state.status, state.error_code),
                         ('failed', 'worker_lease_expired'))
        with self.admin.connect() as conn:
            row = conn.execute(sa.select(
                AnalysisTask.status, AnalysisTask.error_code,
                task_inputs.c.lease_until,
            ).join(task_inputs, task_inputs.c.task_id == AnalysisTask.id).where(
                AnalysisTask.id == task,
            )).one()
        self.assertEqual((row.status, row.error_code, row.lease_until),
                         ('failed', 'worker_lease_expired', None))
        self.assertEqual(self.usage(), (0, 0))
        same, replay = self.submit()
        self.assertEqual(same, task)
        self.assertFalse(replay.result(timeout=10))
        self.assertEqual(self.analyzer.call_count, 1)
        self.release.set()
        with self.assertRaisesRegex(ValueError, 'fenced by operator recovery'):
            future.result(timeout=10)
        self.assertEqual(self.usage(), (0, 0))

    def test_request_replay_uses_original_snapshot_and_does_not_dispatch_again(self):
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        with self.admin.begin() as conn:
            conn.execute(Resume.__table__.update().where(Resume.id == self.resumes[0]).values(raw_content='changed after acceptance'))
        same, duplicate = self.submit()
        self.assertEqual(same, task)
        self.assertFalse(duplicate.result(timeout=10))
        self.release.set()
        future.result(timeout=10)
        self.assertEqual(self.analyzer.call_count, 1)
        self.assertEqual(self.analyzer.call_args.kwargs['resume']['content'], '合成简历 0')
        replay = self.post()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['data']['task_id'], task)
        self.assertEqual(self.usage(), (1, 0))

    def test_expired_claim_cannot_start_the_next_provider_call_before_reaping(self):
        def expire_after_write(conn, **kwargs):
            accepted = write_analysis_batch(conn, **kwargs)
            conn.execute(task_inputs.update().where(
                task_inputs.c.task_id == kwargs['task_id'],
            ).values(lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")))
            return accepted
        with patch('db_analysis_workflow.write_analysis_batch', side_effect=expire_after_write):
            task, future = self.submit(resumes=self.resumes[:2])
            self.assertFalse(future.result(timeout=10))
        self.assertEqual(self.analyzer.call_count, 1)
        self.assertEqual(self.snapshot(task)['status'], 'running')
        self.assertEqual(self.usage(), (0, 2))
        with self.admin.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 1)
        self.assertEqual(self.snapshot(task)['status'], 'partial_success')
        self.assertEqual(self.usage(), (1, 0))

    def test_same_request_with_different_candidates_is_conflict(self):
        task, future = self.submit()
        future.result(timeout=10)
        with self.assertRaises(WorkflowError) as caught:
            self.submit(resumes=self.resumes[1:2])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.post(resumes=self.resumes[1:2]).status_code, 409)
        self.assertEqual(self.usage(), (1, 0))

    def test_claim_survives_failed_result_write_and_is_not_retried(self):
        with patch('db_analysis_workflow.write_analysis_batch', side_effect=RuntimeError('interrupted commit')):
            task, future = self.submit()
            with self.assertRaises(RuntimeError):
                future.result(timeout=10)
        other_process = AnalysisWorkflow(self.sessions, Mock(return_value=report()))
        try:
            same, duplicate = other_process.submit(owner_user_id=self.owner, password_version=1,
                request_id=self.headers['Idempotency-Key'], job_id=self.job, resume_ids=self.resumes[:1])
            self.assertEqual(task, same)
            self.assertFalse(duplicate.result(timeout=10))
            other_process.analyzer.assert_not_called()
            self.assertEqual(self.usage(), (0, 1))
        finally:
            other_process.close()

    def test_bounded_admission_rejects_before_creating_second_task(self):
        self.workflow.close()
        self.workflow = AnalysisWorkflow(self.sessions, self.analyzer, max_workers=1, max_pending=1)
        self.app = create_preview_app(self.store, self.auth, self.secret, analysis_workflow=self.workflow)
        self.client = self.app.test_client()
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        try:
            rejected = self.post(streaming=True, headers={**self.headers, 'Idempotency-Key': str(uuid4())})
            self.assertEqual(rejected.status_code, 503)
            rejected.close()
            self.assertEqual(self.workflow.tracker.status()['streams'], 0)
            self.assertEqual(self.usage(), (0, 1))
            with self.admin.connect() as conn:
                self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(AnalysisTask.__table__).where(AnalysisTask.owner_user_id == self.owner)), 1)
        finally:
            self.release.set()
        future.result(timeout=10)

    def test_legacy_task_without_inputs_cannot_be_adopted(self):
        from db_task_lifecycle import create_task
        from db_quota import reserve, EffectiveQuota
        with self.engine.begin() as conn:
            task = create_task(conn, owner_user_id=self.owner, job_id=self.job,
                               total_candidates=1, request_id=self.headers['Idempotency-Key'])
            reserve(conn, request_id=self.headers['Idempotency-Key'], user_id=self.owner,
                    task_id=task['id'], count=1, policy=EffectiveQuota(True, 3, False))
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.usage(), (0, 1))
        self.analyzer.assert_not_called()

    def test_concurrent_submissions_infer_each_candidate_once(self):
        barrier = Barrier(4)
        def attempt():
            barrier.wait(timeout=10)
            return self.submit(resumes=self.resumes[:3])
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(lambda _: attempt(), range(4)))
        for task, future in accepted:
            future.result(timeout=10)
        self.assertEqual(len({task for task, _ in accepted}), 1)
        self.assertEqual(self.analyzer.call_count, 3)
        self.assertEqual(self.usage(), (3, 0))

    def test_partial_and_all_failure_are_settled_without_provider_details(self):
        self.analyzer.side_effect = [report(), RuntimeError('PRIVATE_PROVIDER_KEY'), {'final_score': True}]
        response = self.post(resumes=self.resumes[:3])
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual((data['status'], data['completed_candidates'], data['failed_candidates']), ('partial_success', 1, 2))
        self.assertEqual({item['error_code'] for item in data['analyses'] if item['status'] == 'failed'},
                         {'analysis_unavailable', 'invalid_analysis_result'})
        self.assertNotIn('PRIVATE_PROVIDER_KEY', response.get_data(as_text=True))
        self.assertEqual(self.usage(), (1, 0))

    def test_all_failures_return_error_with_persisted_state(self):
        self.analyzer.side_effect = RuntimeError('PRIVATE_SMTP_PASSWORD')
        response = self.post(resumes=self.resumes[:3])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['data']['status'], 'failed')
        self.assertEqual(self.usage(), (0, 0))
        self.assertNotIn('PRIVATE_SMTP_PASSWORD', response.get_data(as_text=True))

    def test_http_infrastructure_failure_is_generic_and_leaves_reserved_task(self):
        with patch('db_analysis_workflow.write_analysis_batch', side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_DATABASE_URL', response.get_data(as_text=True))
        self.assertEqual(self.usage(), (0, 1))
        self.assertEqual(self.workflow.tracker.status()['requests'], 0)

    def test_input_errors_create_no_task_or_reservation(self):
        for payload, headers in (
            ({'job_id': self.job, 'resume_ids': []}, self.headers),
            ({'job_id': self.job, 'resume_ids': self.resumes}, self.headers),
            ({'job_id': self.job, 'resume_ids': self.resumes[:1] * 2}, self.headers),
            ({'job_id': self.job, 'resume_id': 'invalid'}, self.headers),
            ({'job_id': self.job, 'resume_id': self.resumes[0]}, {'Authorization': self.headers['Authorization']}),
            ({'job_id': self.job, 'resume_id': self.resumes[0], 'ai_config': {'api_key': 'rejected'}}, self.headers),
        ):
            response = self.client.post('/api/v1/resumes/hr-analysis', json=payload, headers=headers)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.usage(), (0, 0))
        self.analyzer.assert_not_called()
        with self.admin.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(AnalysisTask.__table__).where(AnalysisTask.owner_user_id == self.owner)), 0)

    def test_foreign_and_deleted_resources_fail_before_reservation(self):
        foreign = str(uuid4())
        for job, resumes in ((foreign, self.resumes[:1]), (self.job, [foreign])):
            response = self.client.post('/api/v1/resumes/hr-analysis', json={'job_id': job, 'resume_ids': resumes}, headers=self.headers)
            self.assertEqual(response.status_code, 404)
        with self.admin.begin() as conn:
            conn.execute(Job.__table__.update().where(Job.id == self.job).values(deleted_at=datetime.now(timezone.utc)))
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.usage(), (0, 0))
        self.analyzer.assert_not_called()

    def test_quota_and_disabled_analysis_fail_without_dispatch(self):
        with self.admin.begin() as conn:
            conn.execute(quota_policies.update().where(quota_policies.c.scope_key == self.owner).values(daily_limit=0))
        self.assertEqual(self.post().status_code, 429)
        with self.admin.begin() as conn:
            conn.execute(quota_policies.update().where(quota_policies.c.scope_key == self.owner).values(enabled=False))
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.usage(), (0, 0))
        self.analyzer.assert_not_called()

    def test_task_reads_are_tenant_scoped_and_password_revoked(self):
        task, future = self.submit()
        future.result(timeout=10)
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/' + task, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/' + str(uuid4()), headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/bad', headers=self.headers).status_code, 400)
        other, _ = self.auth.create_user('other-' + uuid4().hex, 'SyntheticPassword9')
        with self.assertRaises(WorkflowError) as caught:
            self.workflow.snapshot(task_id=task, owner_user_id=other['user_id'], password_version=1)
        self.assertEqual(caught.exception.status_code, 404)
        with self.admin.begin() as conn:
            from db_models import User
            conn.execute(User.__table__.update().where(User.id == self.owner).values(password_version=2))
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/' + task, headers=self.headers).status_code, 401)
        with self.assertRaises(WorkflowError):
            self.snapshot(task)

    def test_worker_claim_prevents_restart_redispatch_and_requires_membership(self):
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        other_process = AnalysisWorkflow(self.sessions, Mock(return_value=report()))
        try:
            _, duplicate = other_process.submit(owner_user_id=self.owner, password_version=1,
                request_id=self.headers['Idempotency-Key'], job_id=self.job, resume_ids=self.resumes[:1])
            self.assertFalse(duplicate.result(timeout=10))
            other_process.analyzer.assert_not_called()
            with self.admin.connect() as conn:
                inputs = conn.execute(sa.select(task_inputs).where(task_inputs.c.task_id == task)).mappings().one()
            for token, resume, message in ((None, self.resumes[0], 'worker claim'),
                                          (uuid4(), self.resumes[0], 'worker claim'),
                                          (inputs['worker_token'], self.resumes[3], 'accepted task inputs')):
                with self.engine.begin() as conn, self.assertRaisesRegex(ValueError, message):
                    write_analysis_batch(conn, task_id=task, owner_user_id=self.owner,
                        batch_id='foreign-callback', outcomes=[{'resume_id': resume, 'status': 'completed', 'result': report()}],
                        worker_token=token)
        finally:
            other_process.close()
            self.release.set()
        future.result(timeout=10)
        self.assertEqual(self.analyzer.call_count, 1)

    def test_sse_completion_follows_commit_and_disconnect_keeps_worker_tracked(self):
        self.analyzer.side_effect = self.blocking_analyzer
        response = self.post(streaming=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.started.wait(timeout=10))
        first = json.loads(next(response.response).decode().split('data: ')[1])
        self.assertEqual(first['status'], 'starting')
        task = first['task_id']
        self.assertEqual(self.workflow.tracker.status()['streams'], 1)
        self.release.set()
        events = [json.loads(chunk.decode().split('data: ')[1]) for chunk in response.response]
        self.assertEqual(events[-1]['status'], 'completed')
        self.assertEqual(events[-1]['result']['data'], self.snapshot(task))
        self.assertEqual(self.usage(), (1, 0))
        response.close()
        self.assertEqual(self.workflow.tracker.status()['streams'], 0)

    def test_sse_disconnect_does_not_release_active_worker_or_quota(self):
        self.analyzer.side_effect = self.blocking_analyzer
        response = self.post(streaming=True)
        first = json.loads(next(response.response).decode().split('data: ')[1])
        self.assertTrue(self.started.wait(timeout=10))
        response.close()
        self.workflow.tracker.begin()
        self.assertFalse(self.workflow.tracker.wait(0))
        self.assertEqual(self.workflow.tracker.status(), {'draining': True, 'requests': 0, 'streams': 0, 'workers': 1})
        self.assertEqual(self.usage(), (0, 1))
        self.release.set()
        self.assertTrue(self.workflow.tracker.wait(10))
        self.assertEqual(self.snapshot(first['task_id'])['status'], 'completed')
        self.assertEqual(self.usage(), (1, 0))

    def test_drain_waits_for_long_request_and_refuses_new_admission(self):
        previous_pending = self.workflow.drain_report()['pending_reservations']
        self.analyzer.side_effect = self.blocking_analyzer
        def post():
            with self.app.test_client() as client:
                return self.post(client=client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request_future = pool.submit(post)
            try:
                self.assertTrue(self.started.wait(timeout=10))
                self.assertEqual(self.workflow.tracker.status()['requests'], 1)
                self.workflow.tracker.begin()
                self.assertFalse(self.workflow.tracker.wait(0.01))
                self.assertEqual(self.post(headers={**self.headers, 'Idempotency-Key': str(uuid4())}).status_code, 503)
            finally:
                self.release.set()
            self.assertEqual(request_future.result(timeout=10).status_code, 200)
        self.assertTrue(self.workflow.tracker.wait(10))
        self.assertEqual(self.workflow.tracker.status(), {'draining': True, 'requests': 0, 'streams': 0, 'workers': 0})
        self.assertEqual(self.workflow.drain_report()['pending_reservations'], previous_pending)
        self.assertEqual(self.usage(), (1, 0))

    def test_idle_stream_still_blocks_drain_after_worker_finishes(self):
        response = self.post(streaming=True)
        self.workflow.tracker.begin()
        self.workflow.close()
        self.assertFalse(self.workflow.tracker.wait(0))
        self.assertEqual(self.workflow.tracker.status()['workers'], 0)
        self.assertEqual(self.workflow.tracker.status()['streams'], 1)
        response.close()
        self.assertTrue(self.workflow.tracker.wait(0))

    def test_infrastructure_failure_retains_reservation_and_blocks_drain_certificate(self):
        with patch('db_analysis_workflow.write_analysis_batch', side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
            task, future = self.submit()
            with self.assertRaisesRegex(RuntimeError, 'PRIVATE_DATABASE_URL'):
                future.result(timeout=10)
        self.workflow.tracker.begin()
        self.assertTrue(self.workflow.tracker.wait(10))
        evidence = self.workflow.drain_report()
        self.assertFalse(evidence['complete'])
        self.assertGreaterEqual(evidence['unfinished_tasks'], 1)
        self.assertEqual(self.usage(), (0, 1))
        self.assertEqual(self.snapshot(task)['status'], 'running')

    def test_failed_executor_submission_is_retriable_without_new_reservation(self):
        with patch.object(self.workflow._executor, 'submit', side_effect=RuntimeError('dispatch unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'dispatch unavailable'):
                self.submit()
        self.assertEqual(self.usage(), (0, 1))
        task, future = self.submit()
        self.assertTrue(future.result(timeout=10))
        self.assertEqual(self.snapshot(task)['status'], 'completed')
        self.assertEqual(self.usage(), (1, 0))

    def test_revocation_during_inference_prevents_commit_and_exposes_no_result(self):
        self.analyzer.side_effect = self.blocking_analyzer
        task, future = self.submit()
        self.assertTrue(self.started.wait(timeout=10))
        with self.admin.begin() as conn:
            from db_models import User
            conn.execute(User.__table__.update().where(User.id == self.owner).values(status='locked'))
        self.release.set()
        with self.assertRaises(WorkflowError):
            future.result(timeout=10)
        self.assertEqual(self.usage(), (0, 1))
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/' + task, headers=self.headers).status_code, 401)

    def test_sse_worker_failure_is_generic_and_releases_stream(self):
        with patch('db_analysis_workflow.write_analysis_batch', side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
            response = self.post(streaming=True)
            chunks = b''.join(response.response).decode()
        self.assertIn('"status": "error"', chunks)
        self.assertNotIn('PRIVATE_DATABASE_URL', chunks)
        response.close()
        self.assertEqual(self.workflow.tracker.status()['streams'], 0)


if __name__ == '__main__':
    unittest.main()
