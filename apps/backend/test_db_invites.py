"""Pure encryption, composition and strict input tests without side effects."""
from unittest import TestCase
from unittest.mock import Mock
from cryptography.fernet import Fernet

from db_invite_cipher import InviteCipher
from db_invites import InviteService, _text, _identity, _mask
from db_public_auth import PublicAuthError, PublicAuthService


class InvitePolicyTests(TestCase):
    def test_cipher_rejects_wrong_context_identity_key_and_tampering(self):
        a, b = InviteCipher(Fernet.generate_key()), InviteCipher(Fernet.generate_key())
        payload = dict(email='synthetic@example.test', note='Request reason')
        envelope = a.encrypt('request', 'id', payload)
        self.assertEqual(a.decrypt('request','id',envelope), payload)
        self.assertNotIn('synthetic', str(envelope))
        for cipher, kind, identity, value in [(b,'request','id',envelope), (a,'invite','id',envelope),
                (a,'request','other',envelope), (a,'request','id',dict(envelope,v=True)),
                (a,'request','id',dict(envelope,token='invalid'))]:
            with self.assertRaisesRegex(ValueError, 'authentication failed'):
                cipher.decrypt(kind,identity,value)

    def test_explicit_composition_and_bounded_policy(self):
        auth = PublicAuthService(Mock(), secret=b'A'*32, send_mail=Mock())
        options = dict(cipher=InviteCipher(Fernet.generate_key()), send_mail=Mock())
        for change in [dict(cipher=None), dict(send_mail=None), dict(ttl_hours=True), dict(max_rejections=0), dict(per_ip_day=101)]:
            with self.assertRaises(ValueError):
                InviteService(auth, **(options|change))
        with self.assertRaises(ValueError):
            InviteService(None, **options)

    def test_text_uuid_and_masking_bounds(self):
        for value in [None, [], 'x\0y', 'x\ud800y', 'x'*201]:
            with self.assertRaises(PublicAuthError):
                _text(value, 2, 200)
        for value in ['../private', 0, None, '00000000-0000-0000-0000-000000000000']:
            with self.assertRaises(PublicAuthError):
                _identity(value)
        self.assertEqual(_mask('alice@example.test'), 'a***@example.test')
        self.assertEqual(_mask('a@example.test'), '***@example.test')

    def test_preview_default_denies_and_mismatched_services_rejected(self):
        import test_db_http_preview as previews
        from db_http_preview import create_preview_app
        fixture = previews.PreviewTests()
        fixture.setUp()
        for method, path in [('post','/api/v1/invite-request'),('get','/api/v1/admin/invite-requests'),
                             ('post','/api/v1/admin/invite-codes/generate')]:
            self.assertEqual(getattr(fixture.client,method)(path).status_code, 503)
        with self.assertRaises(ValueError):
            create_preview_app(fixture.store,fixture.auth,fixture.secret,invite_service=Mock())
