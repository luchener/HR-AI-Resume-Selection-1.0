"""Search egress boundaries and durable improvement/quota acceptance."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from threading import Event
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa

from db_search_provider import SQLSearchProvider
from db_screening import SQLResumeImprover, SQLScreeningAnalyzer

MARKDOWN = '# Synthetic Candidate\n## Experience\nPython API delivery and SQL transactions.'


class SearchBoundaryTests(unittest.TestCase):
    def client(self, body, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, stream=httpx.ByteStream(body), **kwargs)))

    def test_custom_search_pins_transport_bounds_results_and_does_not_log_query(self):
        provider = SQLSearchProvider({'endpoint': 'https://search.example.test/api', 'api_key': 'synthetic-search-key'})
        body = json.dumps({'results': [{'title': 'Public company', 'url': 'https://example.test/info', 'snippet': 'public'},
                                      {'url': 'http://127.0.0.1/private'}, {'url': 'javascript:bad'}]}).encode()
        from types import SimpleNamespace
        with patch('db_search_provider.resolve_https', return_value=SimpleNamespace(url=provider.endpoint)), \
                patch('db_search_provider.http_client', side_effect=lambda *a, **k: self.client(body)):
            result = provider(['Synthetic Company'])
        self.assertEqual(result[0], [{'title': 'Public company', 'url': 'https://example.test/info', 'snippet': 'public'}])

    def test_custom_failure_never_uses_public_fallback(self):
        provider = SQLSearchProvider({'endpoint': 'https://search.example.test/api', 'api_key': 'synthetic'})
        with patch.object(provider, '_fetch', side_effect=ValueError('private endpoint unavailable')) as fetch:
            self.assertEqual(provider(['Synthetic Company']), [None])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args, ('https://search.example.test/api',))

    def test_public_html_parsing_and_bounded_fallback(self):
        provider = SQLSearchProvider()
        html = '<a class="result__a" href="https://example.test/company">Company</a>'
        with patch.object(provider, '_fetch', side_effect=[ValueError(), html]) as fetch:
            results = provider(['Synthetic Company'])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(results[0][0]['url'], 'https://example.test/company')
        for queries in (['x'] * 4, ['x' * 201], [''], [1]):
            with self.assertRaises(ValueError):
                provider(queries)

    def test_transport_rejects_large_compressed_and_custom_redirect_responses(self):
        from types import SimpleNamespace
        provider = SQLSearchProvider({'endpoint': 'https://search.example.test/api'})
        for status, body, headers in [(200, b'x' * 524289, {}), (200, b'x', {'Content-Encoding': 'gzip'}),
                                      (302, b'', {'Location': 'https://other.example.test'})]:
            with self.subTest(status=status, headers=headers), \
                    patch('db_search_provider.resolve_https', return_value=SimpleNamespace(url=provider.endpoint)), \
                    patch('db_search_provider.http_client', side_effect=lambda *a, **k:
                        httpx.Client(transport=httpx.MockTransport(lambda request:
                            httpx.Response(status, stream=httpx.ByteStream(body), headers=headers)))):
                self.assertEqual(provider(['Synthetic Company']), [None])

    def test_improver_uses_original_prompt_and_task_model_snapshot(self):
        gateway = Mock()
        gateway.model_snapshot.return_value = ({'api_key': 'synthetic-old'}, 'a' * 64)
        gateway.complete.return_value = MARKDOWN
        worker, binding = SQLResumeImprover(gateway).prepare_task()
        self.assertEqual(worker(resume={'content': 'Synthetic Python resume'}, job={'content': 'Python role'}), MARKDOWN)
        self.assertFalse(gateway.complete.call_args.kwargs['expect_json'])
        self.assertEqual(gateway.complete.call_args.kwargs['runtime_config']['api_key'], 'synthetic-old')
        self.assertNotIn('synthetic-old', json.dumps(binding))


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class FeatureLiveTests(unittest.TestCase):
    def setUp(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.setUp(self)
        from db_security import migration_engine
        from db_test_guard import review_test_url
        self.admin = migration_engine(review_test_url(os.environ['DATABASE_TEST_URL']).render_as_string(hide_password=False))
        self.addCleanup(self.admin.dispose)
        self.config = replace(self.config, admin_emails=())
        from db_quota import quota_policies
        with self.admin.begin() as conn:
            conn.execute(quota_policies.insert().values(scope_key=self.user['user_id'],
                user_id=UUID(self.user['user_id']), mode='limited', daily_limit=3,
                enabled=True, email_notify=False))

    def finish(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.finish(self)

    def upload(self):
        from test_db_application import ApplicationLiveTests
        return ApplicationLiveTests.upload(self)

    def compose(self, *, analyzer=None, improver=None):
        from db_application import create_application
        from test_db_content_live import analysis_report
        self.application.close()
        self.application = create_application(self.config, replace(self.callbacks,
            analyze=analyzer if analyzer is not None else Mock(return_value=analysis_report()),
            improver=improver if improver is not None else Mock(return_value=MARKDOWN)), self.objects)
        self.client = self.application.app.test_client()

    def usage(self):
        from db_quota import quota_daily_usage
        with self.admin.connect() as conn:
            rows = conn.execute(sa.select(quota_daily_usage.c.used, quota_daily_usage.c.reserved).where(
                quota_daily_usage.c.user_id == UUID(self.user['user_id']))).all()
        return tuple(map(sum, zip(*rows))) if rows else (0, 0)

    def test_http_improvement_replay_is_markdown_and_charges_once(self):
        callback = Mock(return_value=MARKDOWN)
        analyzer = Mock(side_effect=AssertionError('screening path used'))
        self.compose(improver=callback, analyzer=analyzer)
        resume, job = self.upload()
        key = str(uuid4())
        headers = self.headers | {'Idempotency-Key': key}
        payload = {'resume_id': resume, 'job_id': job}
        response = self.client.post('/api/v1/resumes/improve?stream=true', headers=headers, json=payload)
        self.assertEqual(response.status_code, 200)
        events = [json.loads(frame[6:]) for frame in response.get_data(as_text=True).strip().split('\n\n')]
        snapshot = events[-1]['result']['data']
        self.assertEqual(snapshot['improvement']['analysis_result'], MARKDOWN)
        self.assertEqual(snapshot['kind'], 'improvement')
        self.assertEqual(snapshot['analyses'], [])
        replay = self.client.post('/api/v1/resumes/improve', headers=headers, json=payload)
        self.assertEqual(replay.get_json()['data']['task_id'], snapshot['task_id'])
        self.assertEqual(callback.call_count, 1)
        analyzer.assert_not_called()
        self.assertEqual(self.usage(), (1, 0))
        from db_models import Analysis
        from db_task_artifacts import artifacts
        with self.admin.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(Analysis).where(Analysis.task_id == UUID(snapshot['task_id']))), 0)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(artifacts).where(artifacts.c.task_id == UUID(snapshot['task_id']))), 1)
        conflict = self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=payload)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.usage(), (1, 0))

    def test_improvement_failure_and_invalid_markdown_release_quota(self):
        for value in ('', {'final_score': 82}):
            with self.subTest(value=value):
                self.compose(improver=Mock(return_value=value))
                resume, job = self.upload()
                response = self.client.post('/api/v1/resumes/improve', headers=self.headers | {'Idempotency-Key': str(uuid4())},
                    json={'resume_id': resume, 'job_id': job})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.get_json()['data']['status'], 'failed')
                self.assertEqual(self.usage(), (0, 0))

    def test_search_choice_is_immutable_and_bound_to_idempotency(self):
        from test_db_content_live import analysis_report
        gateway, search, calls = Mock(), Mock(return_value=[[{'title': 'Synthetic', 'url': 'https://example.test', 'snippet': 'public'}]]), []
        gateway.model_snapshot.return_value = ({'api_key': 'synthetic'}, 'b' * 64)
        analyzer = SQLScreeningAnalyzer(gateway, search_provider=search)
        def agent(**kwargs):
            calls.append(kwargs['agent_config']['web_search'])
            if kwargs['agent_config']['web_search']:
                kwargs['search_provider'](['Synthetic Company'])
            return analysis_report()
        self.compose(analyzer=analyzer)
        resume, job = self.upload()
        key = str(uuid4()); headers = self.headers | {'Idempotency-Key': key}
        payload = {'resume_ids': [resume], 'job_id': job, 'agent_config': {'web_search': True}}
        with patch('db_screening.run_screening_agent', side_effect=agent):
            result = self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=payload)
            self.assertEqual(result.status_code, 200, result.get_json())
            self.assertTrue(result.get_json()['data']['web_search'])
            self.assertEqual(self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=payload).status_code, 200)
        self.assertEqual(calls, [True]); self.assertEqual(search.call_count, 1)
        payload['agent_config']['web_search'] = False
        self.assertEqual(self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=payload).status_code, 409)
        from db_models import AnalysisTask
        with self.assertRaises(sa.exc.DBAPIError), self.application.engine.begin() as conn:
            conn.execute(sa.update(AnalysisTask).where(AnalysisTask.id == UUID(result.get_json()['data']['task_id'])).values(web_search=False))

    def test_improver_snapshot_and_artifact_permissions_and_downgrade_guard(self):
        gateway = Mock()
        gateway.model_snapshot.return_value = ({'api_key': 'synthetic-secret'}, 'c' * 64)
        gateway.complete.return_value = MARKDOWN
        self.compose(improver=SQLResumeImprover(gateway))
        resume, job = self.upload()
        response = self.client.post('/api/v1/resumes/improve', headers=self.headers | {'Idempotency-Key': str(uuid4())},
            json={'resume_id': resume, 'job_id': job})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(set(response.get_json()['data']['model_binding']), {'binding_id', 'config_revision', 'policy_revision'})
        self.assertNotIn('synthetic-secret', response.get_data(as_text=True))
        for sql in ('UPDATE analysis_task_artifacts SET markdown=markdown WHERE false', 'DELETE FROM analysis_task_artifacts WHERE false'):
            with self.assertRaises(sa.exc.DBAPIError), self.application.engine.begin() as conn:
                conn.execute(sa.text(sql))
        from alembic.script import ScriptDirectory
        from alembic.config import Config
        from pathlib import Path
        config = Config(str(Path(__file__).with_name('alembic.ini')))
        config.set_main_option('script_location', str(Path(__file__).with_name('migrations')))
        migration = ScriptDirectory.from_config(config).get_revision('0024_task_features').module
        with self.admin.connect() as conn, patch.object(migration.op, 'get_bind', return_value=conn):
            with self.assertRaisesRegex(ValueError, 'Export/reconcile task features'):
                migration.downgrade()

    def test_expired_improvement_cannot_publish_or_charge_late_result(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        callback = Mock(side_effect=lambda **kwargs: (entered.set(), release.wait(10), MARKDOWN)[-1])
        self.compose(improver=callback)
        resume, job = self.upload(); key = str(uuid4())
        workflow = self.application.workflow
        task, future = workflow.submit(owner_user_id=self.user['user_id'], password_version=1,
            request_id=key, job_id=job, resume_ids=[resume], kind='improvement')
        self.assertTrue(entered.wait(5))
        from db_task_inputs import task_inputs
        from db_task_lifecycle import recover_expired_tasks
        with self.admin.begin() as conn:
            conn.execute(task_inputs.update().where(task_inputs.c.task_id == UUID(task)).values(lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=UUID(self.user['user_id'])), 1)
        release.set()
        with self.assertRaises(ValueError):
            future.result(10)
        snapshot = workflow.snapshot(task_id=task, owner_user_id=self.user['user_id'], password_version=1)
        self.assertEqual(snapshot['status'], 'failed'); self.assertNotIn('improvement', snapshot)
        self.assertEqual(self.usage(), (0, 0))
        _, replay = workflow.submit(owner_user_id=self.user['user_id'], password_version=1,
            request_id=key, job_id=job, resume_ids=[resume], kind='improvement')
        replay.result(5); self.assertEqual(callback.call_count, 1)
