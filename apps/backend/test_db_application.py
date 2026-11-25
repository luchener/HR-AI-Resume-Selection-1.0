"""Application composition and real HTTP acceptance on guarded review_test."""
from dataclasses import fields, replace
import io
import json
import os
from pathlib import Path
import shutil
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from db_application import ApplicationAdapters, ApplicationSettings, create_application, main


def settings(url='postgresql+psycopg://resume_app:synthetic@127.0.0.1:15439/review_test', **changes):
    return ApplicationSettings(database_url=url, jwt_secret='synthetic-jwt-secret-application-at-least-48-bytes',
        auth_secret=b'synthetic-HMAC-secret-application-at-least-48-bytes',
        config_key=Fernet.generate_key(), invite_key=Fernet.generate_key(), **changes)


def adapters():
    return ApplicationAdapters(**{item.name: Mock() for item in fields(ApplicationAdapters)})


class ApplicationBoundaryTests(unittest.TestCase):
    def test_settings_repr_hides_credentials_and_rejects_invalid_policy(self):
        value = settings()
        self.assertNotIn('synthetic', repr(value))
        for change in ({'jwt_secret': 'short'}, {'auth_secret': b'short'},
                       {'admin_emails': 'admin@example.test'}, {'captcha_enabled': 1},
                       {'max_workers': True}, {'max_pending': 0}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                replace(value, **change)

    def test_missing_adapter_cannot_create_an_application(self):
        with self.assertRaises(ValueError):
            replace(adapters(), analyze=None)
        with patch('db_application.build_runtime') as build:
            with self.assertRaises(TypeError):
                create_application(settings(), adapters(), object())
            build.assert_not_called()

    def test_cli_errors_do_not_expose_exception_or_secret(self):
        with patch('importlib.import_module', side_effect=ValueError('PRIVATE_DATABASE_PASSWORD')):
            output = io.StringIO()
            from contextlib import redirect_stdout
            with redirect_stdout(output):
                self.assertEqual(main(['--factory', 'synthetic:build']), 2)
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_cli_binds_loopback_and_closes_server_and_application(self):
        from db_application import SQLApplication
        application = Mock(spec=SQLApplication)
        application.app = Mock()
        application.close.return_value = {'complete': True}
        module, server = Mock(), Mock()
        module.build.return_value = application
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch('werkzeug.serving.make_server', return_value=server) as serve, \
             patch('importlib.import_module', return_value=module):
            self.assertEqual(main(['--factory', 'synthetic:build', '--port', '8011']), 0)
        serve.assert_called_once_with('127.0.0.1', 8011, application.app, threaded=True)
        server.server_close.assert_called_once()
        application.close.assert_called_once()

    def test_busy_recovery_thread_prevents_engine_disposal(self):
        from db_application import SQLApplication
        engine, workflow, gc = Mock(), Mock(), Mock()
        gc.stop.return_value = True
        workflow.tracker.wait.return_value = True
        workflow.close.return_value = False
        application = SQLApplication(Mock(), engine, Mock(), workflow, gc)
        self.assertEqual(application.close(0), {'closed': False, 'complete': False})
        engine.dispose.assert_not_called()


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ApplicationLiveTests(unittest.TestCase):
    def setUp(self):
        from db_test_guard import review_test_url
        from db_original_objects import OriginalObjects
        from test_db_content_live import analysis_report
        import parser
        import system_config
        import socket
        self.release = Event()
        self.started = Event()
        root = Path(os.environ.get('DB_REVIEW_OBJECTS_ROOT', '.test-tmp')) / ('application-' + uuid4().hex)
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.objects = OriginalObjects.initialize(root, storage_id=uuid4())
        url = review_test_url(os.environ['DATABASE_TEST_URL']).set(username='resume_app', password='review-app-only')
        self.config = settings(url.render_as_string(hide_password=False),
            admin_emails=(uuid4().hex + '@example.test',), captcha_enabled=False)
        self.callbacks = replace(adapters(), extract_text=parser.extract_text_from_file,
            summarize_job=parser.summarize_job_locally, analyze=Mock(return_value=analysis_report()),
            config_validator=lambda document, value: value,
            config_merger=lambda document, current, incoming: (
                system_config.merge_llm(current, incoming) if document == 'llm'
                else system_config.merge_mail(current, incoming)),
            validate_base_url=system_config.validate_base_url)
        real_dns = socket.getaddrinfo
        def resolve(host, *args, **kwargs):
            if host == 'api.example.test':
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
            return real_dns(host, *args, **kwargs)
        dns = patch('system_config.socket.getaddrinfo', side_effect=resolve)
        dns.start()
        self.addCleanup(dns.stop)
        self.application = create_application(self.config, self.callbacks, self.objects)
        self.addCleanup(self.finish)
        from db_security import migration_engine
        bootstrap = migration_engine(review_test_url(os.environ['DATABASE_TEST_URL']).render_as_string(hide_password=False))
        try:
            import sqlalchemy as sa
            from db_models import SystemConfig
            with bootstrap.begin() as conn:
                conn.execute(sa.delete(SystemConfig))
        finally:
            bootstrap.dispose()
        from db_auth import AuthService
        self.user, error = AuthService(self.application.sessions).create_user(
            'application-' + uuid4().hex, 'SyntheticPassword9', self.config.admin_emails[0])
        self.assertIsNone(error)
        self.client = self.application.app.test_client()
        # Each independent fixture uses a distinct documentation-only IP.
        # Shared SQL rate-limit state must not couple unrelated test accounts.
        address = '2001:db8:' + ':'.join(UUID(self.user['user_id']).hex[index:index + 4] for index in range(0, 24, 4))
        response = self.client.post('/api/v1/auth/login', json={
            'username': self.user['username'], 'password': 'SyntheticPassword9'},
            environ_base={'REMOTE_ADDR': address})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.headers = {'Authorization': 'Bearer ' + response.get_json()['data']['token']}

    def finish(self):
        self.release.set()
        result = self.application.close()
        self.assertTrue(result['closed'])

    def upload(self):
        from test_db_content_live import docx
        response = self.client.post('/api/v1/resumes/upload', headers=self.headers,
            data={'file': (io.BytesIO(docx()), 'synthetic.docx')})
        self.assertEqual(response.status_code, 200, response.get_json())
        resume = response.get_json()['resume_id']
        response = self.client.post('/api/v1/jobs/upload', headers=self.headers,
            json={'resume_id': resume, 'job_descriptions': ['Synthetic Python engineer']})
        self.assertEqual(response.status_code, 200, response.get_json())
        return resume, response.get_json()['job_id'][0]

    def test_complete_http_flow_persists_across_application_restart(self):
        import sqlalchemy as sa
        from db_models import Analysis, SystemConfig
        resume, job = self.upload()
        headers = dict(self.headers, **{'Idempotency-Key': str(uuid4())})
        body = {'resume_ids': [resume], 'job_id': job}
        response = self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()['data']
        self.assertEqual((result['status'], len(result['analyses'])), ('completed', 1))
        self.assertEqual(self.client.get('/api/v1/analysis-history', headers=self.headers).status_code, 200)
        quota = self.client.get('/api/v1/quota/me', headers=self.headers)
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(self.client.get('/api/v1/admin/users', headers=self.headers).status_code, 200)
        saved = self.client.put('/api/v1/admin/system/llm', headers=self.headers,
            json={'api_key': 'SyntheticStoredProviderSecret', 'base_url': 'https://api.example.test', 'model': 'synthetic'})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertNotIn('SyntheticStoredProviderSecret', saved.get_data(as_text=True))
        with self.application.sessions() as session:
            stored = session.scalar(sa.select(SystemConfig.value).where(SystemConfig.key == 'system_config'))
            self.assertNotIn('SyntheticStoredProviderSecret', json.dumps(stored))
            self.assertEqual(session.scalar(sa.select(sa.func.count()).select_from(Analysis).where(
                Analysis.task_id == result['task_id'])), 1)
        self.assertTrue(self.application.close()['closed'])
        self.assertFalse(self.application.readiness())
        self.application = create_application(self.config, self.callbacks, self.objects)
        self.client = self.application.app.test_client()
        again = self.client.post('/api/v1/resumes/hr-analysis', headers=headers, json=body)
        self.assertEqual(again.status_code, 200, again.get_json())
        self.assertEqual(again.get_json()['data'], result)
        self.assertEqual(self.callbacks.analyze.call_count, 1)
        self.assertTrue(self.application.readiness())
        self.assertEqual(self.client.get('/api/v1/analysis-tasks/' + result['task_id']).status_code, 401)

    def test_shared_drain_waits_for_worker_and_open_sse_response(self):
        from test_db_content_live import analysis_report
        def blocked(**kwargs):
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError('Synthetic analyzer timed out')
            return analysis_report()
        self.callbacks.analyze.side_effect = blocked
        resume, job = self.upload()
        response = self.client.post('/api/v1/resumes/hr-analysis?stream=true', headers={
            **self.headers, 'Idempotency-Key': str(uuid4())}, json={'resume_ids': [resume], 'job_id': job}, buffered=False)
        try:
            next(response.response)
            self.assertTrue(self.started.wait(5))
            self.assertFalse(self.application.close(timeout=0)['closed'])
            self.assertFalse(self.application.readiness())
            self.assertEqual(self.client.get('/api/v1/quota/me', headers=self.headers).status_code, 503)
            self.release.set()
            list(response.response)
        finally:
            self.release.set()
            response.close()
        report = self.application.close()
        self.assertTrue(report['closed'])
        self.assertEqual(self.application.close(), report)
        self.assertFalse(self.application.gc_worker.report()['alive'])

    def test_real_loopback_http_and_authentication(self):
        from werkzeug.serving import make_server
        server = make_server('127.0.0.1', 0, self.application.app, threaded=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            with urlopen(base + '/ping', timeout=5) as response:
                self.assertEqual(response.status, 200)
            with urlopen(base + '/ready', timeout=5) as response:
                self.assertEqual(json.loads(response.read()), {'ready': True})
            request = Request(base + '/api/v1/quota/me', headers=self.headers)
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + '/api/v1/quota/me', timeout=5)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_startup_probe_failure_disposes_engine_without_workers(self):
        engine = Mock()
        with patch('db_application.build_runtime', return_value=(engine, Mock())), \
             patch('db_application.verify_runtime', side_effect=RuntimeError('unavailable')), \
             patch('db_analysis_workflow.AnalysisWorkflow') as worker:
            with self.assertRaises(RuntimeError):
                create_application(self.config, self.callbacks, self.objects)
            engine.dispose.assert_called_once()
            worker.assert_not_called()
