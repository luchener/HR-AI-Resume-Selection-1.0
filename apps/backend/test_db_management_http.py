"""Opt-in management HTTP contracts, using detached synthetic services only."""
from dataclasses import replace
from datetime import datetime, timezone
from time import time
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

import jwt

from db_accounts import AccountError
from db_auth import InternalUser
from db_config_service import ConfigPermissionError, ConfigValidationError
from db_http_preview import create_preview_app
from db_secrets import ConfigSecretError


class ManagementHTTPTests(TestCase):
    def setUp(self):
        self.secret = 'synthetic-management-http-secret-with-at-least-48-bytes'
        now = datetime.now(timezone.utc)
        self.user = InternalUser(uuid4(), 'owner', 'owner@example.test', 'private-hash', 'private-salt',
                                 1, True, True, 'active', None, now, now)
        self.auth = Mock(spec=['find_user_by_id', 'authenticate_user'])
        self.auth.find_user_by_id.return_value = self.user
        methods = ['change_password', 'list_accounts', 'create_account', 'update_account', 'reset_password',
                   'set_frozen', 'delete_account', 'restore_account', 'list_lifecycle']
        self.accounts = Mock(spec=methods)
        self.configs = Mock(spec=['public_document', 'update'])
        for method in methods:
            getattr(self.accounts, method).return_value = {'accepted': True}
        self.configs.update.return_value = {'configured': True, 'api_key_mask': '********'}
        self.app = create_preview_app(Mock(), self.auth, self.secret, admin_emails=[self.user.email],
            config_service=self.configs, account_service=self.accounts, config_writes=True)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + jwt.encode({'user_id': str(self.user.user_id),
            'username': self.user.username, 'pwd_ver': 1, 'iss': 'resume-matcher', 'jti': str(uuid4()),
            'iat': time(), 'exp': time() + 3600}, self.secret, algorithm='HS256')}

    def test_default_preview_refuses_every_management_write(self):
        client = create_preview_app(Mock(), self.auth, self.secret, config_service=self.configs).test_client()
        for path, method in [('/api/v1/auth/change-password', 'POST'), ('/api/v1/admin/users', 'GET'),
                ('/api/v1/admin/users', 'POST'), ('/api/v1/admin/users/other', 'PATCH'),
                ('/api/v1/admin/users/other/reset-password', 'POST'),
                ('/api/v1/admin/users/other/freeze', 'POST'), ('/api/v1/admin/users/other/unfreeze', 'POST'),
                ('/api/v1/admin/users/other', 'DELETE'), ('/api/v1/admin/users/other/restore', 'POST'),
                ('/api/v1/admin/users/frozen', 'GET'), ('/api/v1/admin/users/deleted', 'GET'),
                ('/api/v1/admin/system/mail', 'PUT'), ('/api/v1/admin/system/llm', 'DELETE')]:
            self.assertEqual(client.open(path, method=method, headers=self.headers).status_code, 503)
        self.assertEqual(self.auth.mock_calls, [])
        self.assertEqual(self.accounts.mock_calls, [])
        self.assertEqual(self.configs.mock_calls, [])

    def test_lifecycle_routes_forward_only_jwt_actor_and_optional_reason(self):
        for suffix, method, payload in [('freeze','POST',{'reason':'review','actor_id':'untrusted'}),
                                        ('','DELETE',{'admin_password':'SyntheticPassword9','actor_id':'untrusted'}),
                                        ('restore','POST',None),('unfreeze','POST',None)]:
            path='/api/v1/admin/users/member'+('/'+suffix if suffix else '')
            response=self.client.open(path,method=method,json=payload,headers=self.headers)
            self.assertEqual(response.status_code,422 if suffix=='freeze' else 200)
        self.accounts.delete_account.assert_called_once()
        self.assertEqual(self.accounts.delete_account.call_args.args,(str(self.user.user_id),'member','SyntheticPassword9'))
        self.assertEqual(self.accounts.restore_account.call_args.kwargs['password_version'],1)
        self.assertEqual(self.client.post('/api/v1/admin/users/member/freeze',json={'reason':'review'},headers=self.headers).status_code,200)
        self.assertEqual(self.accounts.set_frozen.call_args.args,(str(self.user.user_id),'member',True))
        for state in ('frozen','deleted'):
            self.assertEqual(self.client.get('/api/v1/admin/users/'+state,headers=self.headers).status_code,200)
            self.assertEqual(self.accounts.list_lifecycle.call_args.args,(str(self.user.user_id),state))

    def test_password_route_passes_only_authoritative_identity_and_expected_version(self):
        response = self.client.post('/api/v1/auth/change-password', headers=self.headers,
            json={'old_password': 'OldPassword9', 'new_password': 'NewPassword9', 'user_id': str(uuid4()), 'pwd_ver': 100})
        self.assertEqual(response.status_code, 200)
        arguments = self.accounts.change_password.call_args
        self.assertEqual(arguments.args, (str(self.user.user_id), 'OldPassword9', 'NewPassword9'))
        self.assertEqual(arguments.kwargs['password_version'], 1)
        self.assertTrue(arguments.kwargs['request_id'].startswith('db-preview:'))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-DB-Preview'], 'management-local')

    def test_anonymous_or_revoked_never_reach_mutation(self):
        for headers in ({}, self.headers):
            self.auth.find_user_by_id.return_value = replace(self.user, password_version=2)
            response = self.client.post('/api/v1/auth/change-password', json={}, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.accounts.mock_calls, [])

    def test_user_and_admin_roles_cannot_change_server_whitelist(self):
        self.auth.find_user_by_id.return_value = replace(self.user, email='ordinary@test.invalid', is_admin=False, is_super_admin=True)
        self.assertEqual(self.client.get('/api/v1/admin/users', headers=self.headers).status_code, 403)
        self.auth.find_user_by_id.return_value = replace(self.user, email='ordinary@test.invalid', is_admin=True)
        self.assertEqual(self.client.put('/api/v1/admin/system/mail', json={}, headers=self.headers).status_code, 403)
        self.assertEqual(self.accounts.mock_calls, [])
        self.assertEqual(self.configs.mock_calls, [])

    def test_admin_routes_preserve_envelope_and_creation_status(self):
        response = self.client.get('/api/v1/admin/users?page=bad&size=999&keyword=alice', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.accounts.list_accounts.assert_called_once_with(str(self.user.user_id), password_version=1,
                                                           keyword='alice', page=1, size=200)
        for path, method, data, status, name in [('/api/v1/admin/users', 'POST', {'username': 'other'}, 201, 'create_account'),
                ('/api/v1/admin/users/other', 'PATCH', {'email': 'x@test.invalid'}, 200, 'update_account'),
                ('/api/v1/admin/users/other/reset-password', 'POST', None, 200, 'reset_password')]:
            response = self.client.open(path, method=method, json=data, headers=self.headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(set(response.get_json()), {'request_id', 'data'})
            self.assertEqual(getattr(self.accounts, name).call_args.kwargs['password_version'], 1)

    def test_config_update_and_clear_are_explicit_and_use_transaction_result(self):
        for document in ('mail', 'llm'):
            path = '/api/v1/admin/system/' + document
            for method, payload in [('PUT', {'enabled': False}), ('DELETE', None)]:
                response = self.client.open(path, method=method, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()['data'], self.configs.update.return_value)
                self.assertEqual(self.configs.update.call_args.args, (str(self.user.user_id), document, payload))
                self.assertEqual(self.configs.update.call_args.kwargs['password_version'], 1)
        self.configs.public_document.assert_not_called()

    def test_bad_json_and_wrong_methods_do_not_reach_service(self):
        for path in ('/api/v1/auth/change-password', '/api/v1/admin/users'):
            for data in ([], None, 'text'):
                self.assertEqual(self.client.post(path, json=data, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.patch('/api/v1/admin/system/mail', headers=self.headers).status_code, 503)
        self.assertEqual(self.accounts.mock_calls, [])
        self.assertEqual(self.configs.mock_calls, [])

    def test_service_errors_are_fixed_and_secrets_never_reflected(self):
        self.accounts.change_password.side_effect = AccountError('旧密码错误。', 401)
        self.assertEqual(self.client.post('/api/v1/auth/change-password', json={}, headers=self.headers).status_code, 401)
        for exception, status in [(ConfigPermissionError('private'), 403), (ConfigValidationError('private'), 422),
                                   (ConfigSecretError('private'), 503), (RuntimeError('private'), 503)]:
            self.configs.update.side_effect = exception
            response = self.client.put('/api/v1/admin/system/llm', json={}, headers=self.headers)
            self.assertEqual(response.status_code, status)
            self.assertNotIn('private', response.get_data(as_text=True))

    def test_writes_require_explicit_boolean_and_config_service(self):
        for value in ('true', 1, None):
            with self.assertRaises(ValueError):
                create_preview_app(Mock(), self.auth, self.secret, config_writes=value)
        with self.assertRaises(ValueError):
            create_preview_app(Mock(), self.auth, self.secret, config_writes=True)

    def test_secret_masks_preserve_existing_values_and_boolean_strings_are_rejected(self):
        for mask in ('********', '\u2022' * 8):
            response = self.client.put('/api/v1/admin/system/llm', json={'api_key': mask}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.configs.update.call_args.args[2]['api_key'], '')
            response = self.client.put('/api/v1/admin/system/mail',
                json={'slots': {'transactional': {'password': mask}}}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.configs.update.call_args.args[2]['slots']['transactional']['password'], '')
        self.configs.reset_mock()
        for value in ({'enabled': 'false'}, {'api_key': []}, {'timeout': True}, {'timeout': 1.5}):
            self.assertEqual(self.client.put('/api/v1/admin/system/llm', json=value, headers=self.headers).status_code, 422)
        for value in ({'enabled': 1}, {'password': {}}, {'port': False}):
            self.assertEqual(self.client.put('/api/v1/admin/system/mail',
                json={'slots': {'transactional': value}}, headers=self.headers).status_code, 422)
        self.assertEqual(self.configs.mock_calls, [])
