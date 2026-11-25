"""Deployment boundary checks, plus the real SQL factory in the guarded harness."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cryptography.fernet import Fernet

from db_deployment import business_adapters, load_configuration, wsgi
from db_original_objects import OriginalObjects


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('DB_REVIEW_OBJECTS_ROOT', Path(__file__).resolve().parents[2] / '.test-tmp'))
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        originals = self.root / 'originals'
        originals.mkdir()
        storage_id = str(uuid4())
        OriginalObjects.initialize(originals, storage_id=storage_id)
        self.data = dict(scope='isolated-sql-preview',
            database_url='postgresql+psycopg://resume_app:synthetic@127.0.0.1/resume_sql_preview_test',
            jwt_secret='synthetic-jwt-deployment-secret-at-least-32-bytes',
            auth_secret='synthetic-hmac-deployment-secret-at-least-32-bytes',
            config_key=Fernet.generate_key().decode(), config_key_id='preview-test',
            invite_key=Fernet.generate_key().decode(), admin_emails=['owner@example.test'],
            originals_root=str(originals), storage_id=storage_id)
        self.path = self.root / 'deployment.json'

    def write(self, **changes):
        self.path.write_text(json.dumps(self.data | changes), encoding='utf-8')
        self.path.chmod(0o600)
        return self.path

    def test_valid_explicit_configuration_and_no_connection(self):
        with patch('sqlalchemy.create_engine', side_effect=AssertionError('connection')):
            settings, objects, environment, search = load_configuration(self.write())
        self.assertEqual(objects.storage_id, __import__('uuid').UUID(self.data['storage_id']))
        self.assertEqual(environment, {})
        self.assertEqual(settings.max_workers, 1)
        self.assertNotIn('synthetic', repr(settings))

    def test_refuses_production_database_privileged_role_and_missing_marker(self):
        for change in ({'scope': 'production'},
                       {'database_url': self.data['database_url'].replace('resume_sql_preview_test', 'resume_staging')},
                       {'database_url': self.data['database_url'].replace('resume_app:', 'resume_migrator:')},
                       {'database_url': self.data['database_url'] + '?options=unsafe'},
                       {'storage_id': str(uuid4())}, {'originals_root': 'relative'},
                       {'auth_secret': ''}, {'unexpected': True}):
            with self.subTest(keys=list(change)), self.assertRaises((ValueError, TypeError)):
                load_configuration(self.write(**change))

    def test_duplicate_settings_and_shared_linux_permissions_rejected(self):
        self.write()
        raw = self.path.read_text()
        self.path.write_text(raw[:-1] + ',"scope":"isolated-sql-preview"}')
        with self.assertRaises(ValueError):
            load_configuration(self.path)
        if os.name != 'nt':
            self.write().chmod(0o644)
            with self.assertRaises(ValueError):
                load_configuration(self.path)

    def test_wsgi_startup_never_discloses_configuration_exceptions(self):
        with patch.dict(os.environ, {'SQL_DEPLOYMENT_FILE': str(self.path)}), \
                patch('db_deployment.build', side_effect=ValueError('synthetic-private-dsn')):
            with self.assertRaisesRegex(RuntimeError, '^SQL preview startup checks failed$') as caught:
                wsgi()
        self.assertTrue(caught.exception.__suppress_context__)

    def test_business_rules_keep_blank_secret_and_reject_plain_smtp(self):
        gateway = Mock()
        gateway.validate_base_url.side_effect = lambda url: url
        adapters = business_adapters(gateway)
        merged = adapters.config_merger('llm', {'api_key': 'synthetic-private',
            'base_url': 'https://api.example.test', 'model': 'old'}, {'api_key': '', 'model': 'new'})
        self.assertEqual(merged['api_key'], 'synthetic-private')
        self.assertEqual(merged['model'], 'new')
        with self.assertRaises(ValueError):
            adapters.config_merger('mail', {}, {'slots': {'transactional': {'security': 'plain'}}})

    def test_factory_import_does_not_import_legacy_or_open_database(self):
        code = '''import sys,importlib.abc
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, *args):
  if fullname in {'app','auth','config','system_config','store','llm'}:raise AssertionError(fullname)
sys.meta_path.insert(0,Guard())
import db_deployment
from unittest.mock import Mock
db_deployment.business_adapters(Mock())
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gunicorn_shutdown_retries_until_resources_are_closed(self):
        import gunicorn_sql_preview as hooks
        owned = Mock()
        owned.close.side_effect = [{'closed': False, 'complete': False}, {'closed': True, 'complete': True}]
        worker = Mock(wsgi=Mock(extensions={'db_application': owned}))
        with patch.object(hooks, 'sleep'):
            hooks.worker_exit(Mock(), worker)
        self.assertEqual(owned.close.call_count, 2)
        self.assertIn('"sql_preview_drained": true', worker.log.info.call_args.args[0])


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class DeploymentLiveTests(unittest.TestCase):
    def test_real_factory_login_upload_and_shutdown_without_json_imports(self):
        from db_test_guard import review_test_url
        from db_deployment import build
        from db_auth import AuthService
        from test_db_content_live import docx
        import io
        fixture = DeploymentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # The harness owns review_test; intercept only the outer preview-name gate.
        # Runtime still verifies the real resume_app role and full migrated schema.
        real = review_test_url(os.environ['DATABASE_TEST_URL']).set(username='resume_app', password='review-app-only')
        fixture.write(captcha_enabled=False)
        loader = load_configuration
        def guarded(filename):
            from dataclasses import replace
            settings, objects, env, search = loader(filename)
            return replace(settings, database_url=real.render_as_string(hide_password=False)), objects, env, search
        with patch('db_deployment.load_configuration', side_effect=guarded):
            application = build(fixture.path)
        self.addCleanup(application.close)
        user, error = AuthService(application.sessions).create_user('deploy_' + uuid4().hex[:16],
            'SyntheticPassword9', uuid4().hex + '@example.test')
        self.assertIsNone(error)
        client = application.app.test_client()
        login = client.post('/api/v1/auth/login', json={'username': user['username'], 'password': 'SyntheticPassword9'})
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': 'Bearer ' + login.get_json()['data']['token']}
        self.assertEqual(client.get('/ready').status_code, 200)
        upload = client.post('/api/v1/resumes/upload', headers=headers,
            data={'file': (io.BytesIO(docx()), 'synthetic.docx')})
        self.assertEqual(upload.status_code, 200, upload.get_json())
        # This shared guarded database also contains earlier suites' durable
        # tasks/GC intents. Closing this factory must release owned resources;
        # it must not certify those unrelated pending obligations as drained.
        report = application.close()
        self.assertTrue(report['closed'])
        self.assertFalse(report['originals']['alive'])
        self.assertEqual(report['originals']['pending_local'], 0)
        self.assertEqual(client.get('/ready').status_code, 503)
