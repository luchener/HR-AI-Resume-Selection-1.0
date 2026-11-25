"""Policy/HTTP gates without database or provider side effects."""
from unittest import TestCase
from unittest.mock import Mock

from db_public_auth import PublicAuthError, PublicAuthService, _email, _password
import test_db_http_preview as previews


class PublicAuthPolicyTests(TestCase):
    def test_requires_explicit_secret_sender_and_bounded_policy(self):
        for options in ({'secret': b'short'}, {'send_mail': None}, {'captcha_enabled': 1},
                        {'max_keys': 0}, {'invite_length': 3}):
            args = dict(secret=b'S' * 32, send_mail=Mock())
            args.update(options)
            with self.assertRaises(ValueError):
                PublicAuthService(Mock(), **args)

    def test_inputs_and_hash_namespaces_cannot_collide_or_use_unkeyed_digest(self):
        for email in (None, [], 'no-at-sign', 'x\0@example.test', 'x\ud800@example.test'):
            with self.subTest(email=repr(email)), self.assertRaises(PublicAuthError):
                _email(email)
        for password in (None, [], 'weak', 'A'*1025):
            with self.assertRaises(PublicAuthError):
                _password(password)
        self.assertEqual(_email(' Alice@Example.Test '), 'alice@example.test')
        self.assertEqual(_password(' Password9 '), ' Password9 ')
        a = PublicAuthService(Mock(), secret=b'A'*32, send_mail=Mock())
        b = PublicAuthService(Mock(), secret=b'B'*32, send_mail=Mock())
        hashes = {a.digest('email', '123456'), a.digest('invite', '123456'), b.digest('email', '123456')}
        self.assertEqual(len(hashes), 3)
        self.assertTrue(all(len(x) == 64 for x in hashes))

    def test_default_preview_refuses_public_routes_and_policy_mismatch(self):
        # Reuse only setup helpers; do not inherit the existing test methods.
        fixture = previews.PreviewTests()
        fixture.setUp()
        for method, path in [('post','/api/v1/auth/register'), ('post','/api/v1/auth/reset-password/request'),
                ('get','/api/v1/auth/captcha'), ('post','/api/v1/auth/unfreeze'),
                ('get','/api/v1/auth/register-config')]:
            self.assertEqual(getattr(fixture.client, method)(path).status_code, 503)
        from db_http_preview import create_preview_app
        service = Mock(admin_emails=frozenset({'owner@example.test'}))
        with self.assertRaises(ValueError):
            create_preview_app(fixture.store, fixture.auth, fixture.secret, public_auth=service)
