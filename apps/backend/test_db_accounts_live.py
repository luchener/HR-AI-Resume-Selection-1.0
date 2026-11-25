"""Real account/auth/config HTTP transactions on guarded disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import socket
from threading import Barrier
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import func, select, update

from db_accounts import AccountError, AccountService
from db_auth import AuthService
from db_check_schema import metadata
from db_config_service import ConfigService
from db_http_preview import create_preview_app
from db_models import User
from db_runtime import build_runtime, verify_runtime
from db_secrets import ConfigCipher, EncryptedConfigRepository
from db_security import migration_engine
from db_test_guard import review_test_url

SHADOW_EVIDENCE = []


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AccountsLiveFixture(TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.bootstrap = migration_engine(url.render_as_string(hide_password=False))
        cls.engine, cls.sessions = build_runtime(url.set(username='resume_app', password='review-app-only')
                                               .render_as_string(hide_password=False))
        verify_runtime(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.bootstrap.dispose()

    def setUp(self):
        self.password = 'SyntheticPassword9'
        auth = AuthService(self.sessions)
        self.owner_email = uuid4().hex + '@example.test'
        self.owner, error = auth.create_user('owner-' + uuid4().hex, self.password, self.owner_email)
        self.assertIsNone(error)
        self.admin, error = auth.create_user('admin-' + uuid4().hex, self.password, uuid4().hex + '@example.test')
        self.assertIsNone(error)
        self.member, error = auth.create_user('member-' + uuid4().hex, self.password, uuid4().hex + '@example.test')
        self.assertIsNone(error)
        with self.bootstrap.begin() as conn:
            conn.execute(update(User).where(User.id == self.admin['user_id']).values(is_admin=True))
        self.accounts = AccountService(self.sessions, admin_emails=[self.owner_email])
        self.cipher = ConfigCipher(Fernet.generate_key(), key_id='accounts-' + uuid4().hex)
        # Each class runs serially; global config is cleared only in this synthetic DB.
        with self.bootstrap.begin() as conn:
            conn.execute(metadata().tables['system_configs'].delete())
        import system_config
        synthetic_dns = patch(
            'system_config.socket.getaddrinfo',
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                           '', ('93.184.216.34', 443))])
        synthetic_dns.start()
        self.addCleanup(synthetic_dns.stop)
        self.merger = lambda document, current, incoming: (system_config.merge_llm(current, incoming)
            if document == 'llm' else system_config.merge_mail(current, incoming))
        self.configs = ConfigService(self.sessions, self.cipher, validator=lambda d, p: p,
                                    merger=self.merger, admin_emails=[self.owner_email])
        self.app = create_preview_app(Mock(), self.accounts, 'synthetic-account-jwt-secret-with-at-least-48-bytes',
            admin_emails=[self.owner_email], account_service=self.accounts, config_service=self.configs, config_writes=True)
        self.client = self.app.test_client()
        self.owner_headers = self.login(self.owner)
        self.admin_headers = self.login(self.admin)
        self.member_headers = self.login(self.member)

    def login(self, user, password=None):
        response = self.client.post('/api/v1/auth/login', json={'username': user['username'], 'password': password or self.password})
        self.assertEqual(response.status_code, 200)
        return {'Authorization': 'Bearer ' + response.get_json()['data']['token']}

    def audit(self, request_id):
        table = metadata().tables['audit_logs']
        with self.bootstrap.connect() as conn:
            return conn.execute(select(table).where(table.c.request_id == request_id)).mappings().all()


class AccountsLiveTests(AccountsLiveFixture):
    def test_own_password_change_revokes_both_sessions_and_never_audits_secrets(self):
        second = self.login(self.member)
        response = self.client.post('/api/v1/auth/change-password', headers=self.member_headers,
            json={'old_password': self.password, 'new_password': 'ChangedPassword9', 'user_id': self.owner['user_id']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data'], {'message': '密码修改成功，请重新登录。', 'require_relogin': True})
        for header in (second, self.member_headers):
            self.assertEqual(self.client.get('/api/v1/auth/me', headers=header).status_code, 401)
        self.assertIsNone(self.accounts.authenticate_user(self.member['username'], self.password))
        self.assertIsNotNone(self.accounts.authenticate_user(self.owner['username'], self.password))
        events = self.audit(response.get_json()['request_id'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'user.password_change')
        self.assertNotIn(self.password, str(events))
        self.assertNotIn('ChangedPassword9', str(events))

    def test_wrong_old_password_and_weak_password_leave_version_unchanged(self):
        for old, new, status in [('WrongPassword9', 'ChangedPassword9', 401), (self.password, 'weak', 422),
                                 (self.password, self.password, 422), ([], 'ChangedPassword9', 422)]:
            response = self.client.post('/api/v1/auth/change-password', headers=self.member_headers,
                                       json={'old_password': old, 'new_password': new})
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.accounts.find_user_by_id(self.member['user_id']).password_version, 1)

    def test_password_and_admin_create_audit_failures_roll_back_entire_mutation(self):
        with patch('db_accounts.append_audit', side_effect=RuntimeError('synthetic audit failure')):
            response = self.client.post('/api/v1/auth/change-password', headers=self.member_headers,
                json={'old_password': self.password, 'new_password': 'ChangedPassword9'})
            self.assertEqual(response.status_code, 503)
            name = 'rollback-' + uuid4().hex
            response = self.client.post('/api/v1/admin/users', headers=self.owner_headers,
                                       json={'username': name, 'password': self.password, 'is_admin': True})
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.accounts.find_user_by_id(self.member['user_id']).password_version, 1)
        self.assertIsNone(self.accounts.find_user_by_username(name))
        self.assertIsNotNone(self.accounts.authenticate_user(self.member['username'], self.password))

    def test_admin_create_rejects_privilege_and_whitelist_assignment(self):
        for headers, data, status in [(self.member_headers, {}, 403),
                (self.admin_headers, {'is_admin': True}, 403), (self.owner_headers, {'email': self.owner_email}, 403),
                (self.owner_headers, {'is_admin': 'false'}, 422)]:
            name = 'created-' + uuid4().hex
            response = self.client.post('/api/v1/admin/users', headers=headers,
                                       json={'username': name, 'password': self.password, **data})
            self.assertEqual(response.status_code, status)
            self.assertIsNone(self.accounts.find_user_by_username(name))
        response = self.client.post('/api/v1/admin/users', headers=self.owner_headers,
            json={'username': 'created-' + uuid4().hex, 'password': self.password, 'is_admin': True})
        self.assertEqual(response.status_code, 201)
        user = response.get_json()['data']['user']
        self.assertTrue(user['is_admin'])
        self.assertNotIn('password', response.get_data(as_text=True))
        self.assertEqual(len(self.audit(response.get_json()['request_id'])), 1)
        self.assertEqual(self.client.post('/api/v1/admin/users', headers=self.owner_headers,
            json={'username': user['username'].upper(), 'password': self.password}).status_code, 409)

    def test_email_and_role_changes_are_one_transaction_and_revoke_old_token(self):
        path = '/api/v1/admin/users/' + self.member['username']
        new_email = uuid4().hex + '@example.test'
        with patch('db_accounts.append_audit', side_effect=RuntimeError('synthetic audit failure')):
            self.assertEqual(self.client.patch(path, headers=self.owner_headers,
                json={'email': new_email, 'is_admin': True}).status_code, 503)
        current = self.accounts.find_user_by_id(self.member['user_id'])
        self.assertEqual(current.email, self.member['email'])
        self.assertFalse(current.is_admin)
        response = self.client.patch(path, headers=self.owner_headers, json={'email': new_email, 'is_admin': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/v1/auth/me', headers=self.member_headers).status_code, 401)
        current = self.accounts.find_user_by_id(self.member['user_id'])
        self.assertEqual(current.password_version, 2)
        self.assertEqual(current.email, new_email)
        self.assertTrue(current.is_admin)

    def test_ordinary_admin_cannot_edit_or_reset_admin_or_assign_whitelist(self):
        for user in (self.admin, self.owner):
            path = '/api/v1/admin/users/' + user['username']
            self.assertEqual(self.client.patch(path, headers=self.admin_headers,
                                              json={'email': 'other@example.test'}).status_code, 403)
            self.assertEqual(self.client.post(path + '/reset-password', headers=self.admin_headers).status_code, 403)
        path = '/api/v1/admin/users/' + self.member['username']
        self.assertEqual(self.client.patch(path, headers=self.admin_headers, json={'email': self.owner_email}).status_code, 403)
        self.assertEqual(self.client.patch(path, headers=self.admin_headers, json={'is_admin': False}).status_code, 403)
        self.assertEqual(self.client.patch('/api/v1/admin/users/' + self.owner['username'],
            headers=self.owner_headers, json={'is_admin': False}).status_code, 400)

    def test_reset_password_returns_once_revokes_token_and_preserves_audit(self):
        response = self.client.post('/api/v1/admin/users/' + self.member['username'] + '/reset-password', headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        temporary = response.get_json()['data']['temp_password']
        self.assertGreaterEqual(len(temporary), 12)
        self.assertTrue(any(c.isalpha() for c in temporary) and any(c.isdigit() for c in temporary))
        self.assertIsNone(self.accounts.authenticate_user(self.member['username'], self.password))
        self.assertIsNotNone(self.accounts.authenticate_user(self.member['username'], temporary))
        self.assertEqual(self.client.get('/api/v1/auth/me', headers=self.member_headers).status_code, 401)
        events = self.audit(response.get_json()['request_id'])
        self.assertEqual(len(events), 1)
        self.assertNotIn(temporary, str(events))
        listing = self.client.get('/api/v1/admin/users?keyword=' + self.member['username'], headers=self.owner_headers)
        self.assertIsNotNone(listing.get_json()['data']['items'][0]['password_updated_at'])
        self.assertNotIn(temporary, listing.get_data(as_text=True))

    def test_list_users_search_is_literal_case_insensitive_and_never_leaks_legacy(self):
        with self.bootstrap.begin() as conn:
            conn.execute(update(User).where(User.id == self.member['user_id']).values(legacy_payload={'token': 'PRIVATE', 'is_admin': True}))
        response = self.client.get('/api/v1/admin/users?keyword=' + self.member['username'].upper(), headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual(data['total'], 1)
        self.assertFalse(data['items'][0]['is_admin'])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertNotIn('password_hash', response.get_data(as_text=True))
        response = self.client.get('/api/v1/admin/users?keyword=%25', headers=self.admin_headers)
        self.assertEqual(response.get_json()['data']['total'], 0)

    def test_actor_revocation_after_http_gate_is_rechecked_under_transaction_lock(self):
        original = self.accounts.reset_password
        def revoke_then_reset(*args, **kwargs):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == self.admin['user_id']).values(is_admin=False))
            return original(*args, **kwargs)
        with patch.object(self.accounts, 'reset_password', side_effect=revoke_then_reset):
            response = self.client.post('/api/v1/admin/users/' + self.member['username'] + '/reset-password', headers=self.admin_headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.accounts.find_user_by_id(self.member['user_id']).password_version, 1)

    def test_concurrent_duplicate_creation_commits_exactly_one_account_and_audit(self):
        barrier = Barrier(2)
        name = 'race-' + uuid4().hex
        def create():
            barrier.wait(timeout=10)
            try:
                self.accounts.create_account(self.owner['user_id'], {'username': name, 'password': self.password},
                    password_version=1, request_id='race-' + uuid4().hex)
                return 201
            except AccountError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(results), [201, 409])
        account = self.accounts.find_user_by_username(name)
        table = metadata().tables['audit_logs']
        with self.bootstrap.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(table).where(table.c.target_id == str(account.user_id))), 1)

    def test_concurrent_old_password_callbacks_accept_only_one_and_audit_once(self):
        barrier = Barrier(2)
        def change(index):
            barrier.wait(timeout=10)
            try:
                self.accounts.change_password(self.member['user_id'], self.password, 'ChangedPassword' + str(index),
                    password_version=1, request_id='change-' + uuid4().hex)
                return 200
            except AccountError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, range(2)))
        self.assertEqual(sorted(results), [200, 401])
        self.assertEqual(self.accounts.find_user_by_id(self.member['user_id']).password_version, 2)

    def test_config_put_merge_blank_secret_masked_response_and_clear(self):
        path = '/api/v1/admin/system/llm'
        with patch('system_config._read', side_effect=AssertionError('filesystem read forbidden')), \
             patch('system_config._write', side_effect=AssertionError('filesystem write forbidden')):
            response = self.client.put(path, headers=self.owner_headers,
                json={'api_key': 'SyntheticModelSecret', 'base_url': 'https://api.example.test/v1', 'model': 'm1'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('SyntheticModelSecret', response.get_data(as_text=True))
            response = self.client.put(path, headers=self.owner_headers, json={'api_key': '', 'model': 'm2'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['data']['model'], 'm2')
            self.assertEqual(self.client.put(path, headers=self.admin_headers, json={'model': 'denied'}).status_code, 403)
            with self.sessions() as session, session.begin():
                document = EncryptedConfigRepository(session, self.cipher).get()['system_config/llm.json']
                self.assertEqual(document['api_key'], 'SyntheticModelSecret')
            response = self.client.put(path, headers=self.owner_headers, json={'api_key': '********', 'model': 'm3'})
            self.assertEqual(response.status_code, 200)
            with self.sessions() as session, session.begin():
                self.assertEqual(EncryptedConfigRepository(session, self.cipher).get()['system_config/llm.json']['api_key'], 'SyntheticModelSecret')
            response = self.client.delete(path, headers=self.owner_headers)
            cleared = response.get_json()['data']
            self.assertEqual(cleared['source'], 'none')
            self.assertFalse(cleared['ready'])
            self.assertEqual(cleared['updated_by'], '')

    def test_config_validation_storage_and_audit_errors_do_not_partially_commit(self):
        path = '/api/v1/admin/system/mail'
        incoming = {'slots': {'transactional': {'host': 'smtp.example.test', 'password': 'SyntheticSMTPSecret'}}}
        self.assertEqual(self.client.put(path, json=incoming, headers=self.owner_headers).status_code, 200)
        with patch('db_config_service.append_audit', side_effect=RuntimeError('private audit error')):
            response = self.client.put(path, json={'slots': {'transactional': {'password': 'MustRollback'}}}, headers=self.owner_headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private audit error', response.get_data(as_text=True))
            self.assertEqual(self.client.delete(path, headers=self.owner_headers).status_code, 503)
        self.assertEqual(self.client.put(path, json={'slots': {'transactional': {'port': 0}}}, headers=self.owner_headers).status_code, 422)
        with self.sessions() as session, session.begin():
            document = EncryptedConfigRepository(session, self.cipher).get()['system_config/mail.json']
            self.assertEqual(document['slots']['transactional']['password'], 'SyntheticSMTPSecret')
        # Authentication/decryption failures are outages, never validation errors or env fallback.
        self.configs.cipher = ConfigCipher(Fernet.generate_key(), key_id=self.cipher.key_id)
        response = self.client.put(path, json=incoming, headers=self.owner_headers)
        self.assertEqual(response.status_code, 503)

    def test_config_reply_is_built_before_commit_and_failure_rolls_back(self):
        with patch('db_config_service.public_config_document', side_effect=RuntimeError('response failed')):
            self.assertEqual(self.client.put('/api/v1/admin/system/llm', headers=self.owner_headers,
                json={'api_key': 'SyntheticSecret', 'base_url': 'https://api.example.test', 'model': 'm'}).status_code, 503)
        with self.sessions() as session, session.begin():
            self.assertIsNone(EncryptedConfigRepository(session, self.cipher).get())

    def test_config_rejects_private_url_and_type_confusion_without_persisting(self):
        path = '/api/v1/admin/system/llm'
        base = {'api_key': 'SyntheticSecret', 'base_url': 'https://api.example.test', 'model': 'm'}
        for value in ({'base_url': 'http://127.0.0.1:8000'}, {'base_url': 'https://169.254.169.254'},
                      {'enabled': 'false'}, {'timeout': True}, {'api_key': []}):
            response = self.client.put(path, headers=self.owner_headers, json={**base, **value})
            self.assertEqual(response.status_code, 422)
        with self.sessions() as session, session.begin():
            self.assertIsNone(EncryptedConfigRepository(session, self.cipher).get())

    def test_focused_legacy_json_http_shadow_matches_and_detects_deliberate_drift(self):
        import ast
        from pathlib import Path
        import uuid
        from flask import Flask, g, jsonify, request
        from db_http_shadow import compare_responses
        from testsupport import isolated_data_dirs
        directories = isolated_data_dirs('account-shadow')
        self.addCleanup(directories.restore)
        import auth as legacy_auth
        import config
        directories.reapply()
        for user in (self.owner, self.admin, self.member):
            record = self.accounts.find_user_by_id(user['user_id'])
            value = dict(record.to_public(), password_hash=record.password_hash, password_salt=record.password_salt,
                         is_admin=record.is_admin)
            legacy_auth._write_json(os.path.join(legacy_auth.USERS_DIR, user['user_id'] + '.json'), value)
        legacy_auth._rebuild_index()
        # Compile the actual legacy handler bodies without importing app startup,
        # retention, notification workers, model clients or production directories.
        module = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8'))
        names = {'auth_me', 'admin_list_users', 'change_password', '_request_id'}
        selected = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(selected), len(names))
        legacy_app = Flask('legacy-account-shadow')
        namespace = {'app': legacy_app, 'auth_mod': legacy_auth, 'g': g, 'request': request,
                     'jsonify': jsonify, 'uuid': uuid}
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'legacy-shadow-handlers', 'exec'), namespace)
        legacy_auth.init_auth(legacy_app)
        expected = legacy_app.test_client()
        count = 0
        with patch.object(legacy_auth, 'ADMIN_EMAILS', [self.owner_email]), \
             patch.object(config, 'JWT_SECRET_KEY', 'synthetic-account-jwt-secret-with-at-least-48-bytes'):
            for headers in (self.owner_headers, self.admin_headers, self.member_headers):
                left = expected.get('/api/v1/auth/me', headers=headers)
                right = self.client.get('/api/v1/auth/me', headers=headers)
                self.assertTrue(compare_responses(left.status_code, left.get_json(), right.status_code, right.get_json())['equal'])
                count += 1
            for keyword in (self.member['username'], self.member['username'].upper(), '%25'):
                path = '/api/v1/admin/users?keyword=' + keyword
                left = expected.get(path, headers=self.owner_headers)
                right = self.client.get(path, headers=self.owner_headers)
                self.assertTrue(compare_responses(left.status_code, left.get_json(), right.status_code, right.get_json())['equal'])
                count += 1
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == self.member['user_id']).values(email='drift@example.test'))
            left = expected.get('/api/v1/auth/me', headers=self.member_headers)
            right = self.client.get('/api/v1/auth/me', headers=self.member_headers)
            self.assertFalse(compare_responses(left.status_code, left.get_json(), right.status_code, right.get_json())['equal'])
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == self.member['user_id']).values(email=self.member['email']))
            for body in ({'old_password': 'WrongPassword9', 'new_password': 'ChangedPassword9'},
                         {'old_password': self.password, 'new_password': 'weak'},
                         {'old_password': self.password, 'new_password': 'ChangedPassword9'}):
                left = expected.post('/api/v1/auth/change-password', json=body, headers=self.member_headers)
                right = self.client.post('/api/v1/auth/change-password', json=body, headers=self.member_headers)
                self.assertTrue(compare_responses(left.status_code, left.get_json(), right.status_code, right.get_json())['equal'])
                count += 1
        SHADOW_EVIDENCE.append({'synthetic_cases': count, 'differences': 0, 'deliberate_drift_rejected': True,
                               'scope': 'selected_legacy_handlers_and_json_auth_not_full_app'})
