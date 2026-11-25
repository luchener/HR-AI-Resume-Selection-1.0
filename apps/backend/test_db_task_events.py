import os
import json
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4, UUID

import sqlalchemy as sa
from db_task_events import append_worker_event, task_events
from db_analysis_workflow import WorkflowError


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class TaskEventTests(TestCase):
    def setUp(self):
        from test_db_analysis_workflow import AnalysisWorkflowTests
        self.engine, self.sessions, self.admin = None, None, None
        from db_test_guard import review_test_url
        from db_runtime import build_runtime
        from db_security import migration_engine
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        self.admin = migration_engine(url.render_as_string(hide_password=False))
        self.engine, self.sessions = build_runtime(url.set(username='resume_app', password='review-app-only').render_as_string(hide_password=False))
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.admin.dispose)
        AnalysisWorkflowTests.setUp(self)
        self.addCleanup(lambda: AnalysisWorkflowTests.tearDown(self))

    def submit(self, **kwargs):
        from test_db_analysis_workflow import AnalysisWorkflowTests
        return AnalysisWorkflowTests.submit(self, **kwargs)

    def page(self, task, **kwargs):
        return self.workflow.event_page(task_id=task, owner_user_id=self.owner, password_version=1, **kwargs)

    def event_analyzer(self):
        from test_db_analysis_workflow import report
        class Analyzer:
            def __call__(self, **kwargs):
                return report()
            def analyze_with_events(self, *, resume, job, on_event):
                on_event({'status': 'planning', 'message': 'PRIVATE provider@example.test API_KEY'})
                on_event({'status': 'generating', 'message': 'PRIVATE'})
                return report()
        self.workflow.analyzer = Analyzer()

    def test_durable_order_cursor_and_sse_replay_do_not_repeat_inference(self):
        self.event_analyzer()
        task, future = self.submit()
        future.result(10)
        page = self.page(task, limit=2)
        self.assertEqual([e['id'] for e in page['events']], [1, 2])
        self.assertTrue(page['has_more'])
        rest = self.page(task, after=2)
        self.assertEqual(rest['events'][0]['status'], 'candidate_completed')
        self.assertNotIn('PRIVATE', json.dumps(page))
        path = '/api/v1/analysis-tasks/' + task + '/events'
        response = self.client.get(path + '?stream=true', headers={**self.headers, 'Last-Event-ID': '2'})
        text = response.get_data(as_text=True)
        response.close()
        self.assertIn('id: 3\n', text)
        self.assertNotIn('id: 1\n', text)
        self.assertIn('"status": "completed"', text)
        self.assertEqual(self.workflow.tracker.status()['streams'], 0)
        # A separate SQL workflow/process sees the same committed sequence.
        from db_analysis_workflow import AnalysisWorkflow
        from unittest.mock import Mock
        other = AnalysisWorkflow(self.sessions, Mock())
        try:
            self.assertEqual(other.event_page(task_id=task, owner_user_id=self.owner,
                password_version=1), self.page(task))
        finally:
            other.close()

    def test_events_recheck_owner_identity_and_strict_cursor(self):
        task, future = self.submit()
        future.result(10)
        for after in (True, -1, '1', 257, 2):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.page(task, after=after)
        other, error = self.auth.create_user('events-' + uuid4().hex, 'SyntheticPassword9')
        self.assertIsNone(error)
        with self.assertRaises(WorkflowError) as caught:
            self.workflow.event_page(task_id=task, owner_user_id=other['user_id'], password_version=1)
        self.assertEqual(caught.exception.status_code, 404)
        from db_models import User
        with self.admin.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.owner).values(status='locked'))
        with self.assertRaises(WorkflowError):
            self.page(task)

    def test_wrong_or_expired_worker_cannot_append_and_sequences_serialize(self):
        from test_db_analysis_workflow import report
        def blocked(**kwargs):
            self.started.set()
            self.release.wait(10)
            return report()
        self.workflow.analyzer = blocked
        task, future = self.submit()
        self.assertTrue(self.started.wait(5))
        from db_task_inputs import task_inputs
        with self.admin.connect() as conn:
            token = conn.scalar(sa.select(task_inputs.c.worker_token).where(task_inputs.c.task_id == task))
        args = dict(task_id=UUID(task), owner_user_id=UUID(self.owner), resume_id=UUID(self.resumes[0]), stage='generating')
        with self.assertRaises(ValueError), self.engine.begin() as conn:
            append_worker_event(conn, worker_token=uuid4(), **args)
        def write(_):
            with self.engine.begin() as conn:
                return append_worker_event(conn, worker_token=token, **args)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(write, range(4))), [1, 2, 3, 4])
        with self.admin.begin() as conn:
            conn.execute(task_inputs.update().where(task_inputs.c.task_id == task).values(
                lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")))
        with self.assertRaises(ValueError), self.engine.begin() as conn:
            append_worker_event(conn, worker_token=token, **args)
        self.release.set()
        try:
            future.result(10)
        except ValueError:
            pass

    def test_result_event_failure_rolls_back_result_and_quota(self):
        from db_models import Analysis, AnalysisTask
        with patch('db_task_events.append_locked_event', side_effect=RuntimeError('synthetic event failure')):
            task, future = self.submit()
            with self.assertRaises(RuntimeError):
                future.result(10)
        with self.admin.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(Analysis).where(Analysis.task_id == task)), 0)
            row = conn.execute(sa.select(AnalysisTask.status, AnalysisTask.completed_candidates).where(AnalysisTask.id == task)).one()
            self.assertEqual(tuple(row), ('running', 0))

    def test_runtime_cannot_rewrite_or_delete_event_history(self):
        from sqlalchemy.exc import DBAPIError
        for sql in ('UPDATE analysis_task_events SET stage=stage WHERE false',
                    'DELETE FROM analysis_task_events WHERE false', 'TRUNCATE analysis_task_events'):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError), self.engine.begin() as conn:
                conn.execute(sa.text(sql))

    def test_nonempty_event_history_blocks_destructive_downgrade(self):
        from alembic import command
        from alembic.config import Config
        from pathlib import Path
        task, future = self.submit()
        future.result(10)
        config = Config(str(Path(__file__).parent / 'alembic.ini'))
        config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        from alembic.script import ScriptDirectory
        migration = ScriptDirectory.from_config(config).get_revision('0022_task_events').module
        with self.admin.connect() as conn, patch.object(migration.op, 'get_bind', return_value=conn):
            with self.assertRaisesRegex(ValueError, 'Export/reconcile task events'):
                migration.downgrade()
        self.assertEqual(len(self.page(task)['events']), 1)
