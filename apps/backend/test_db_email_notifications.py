"""Pure rules and HTTP boundary checks for the opt-in email adapter."""
import time
from unittest import TestCase
from unittest.mock import Mock
from uuid import UUID, uuid4

import jwt

from db_auth import InternalUser
from db_email_notifications import EmailNotificationError, EmailNotificationService, _magic, _safe_snapshot
from db_http_preview import create_preview_app
import email_templates


class EmailRuleTests(TestCase):
    def test_real_image_headers_and_rejections(self):
        self.assertTrue(_magic(b"\x89PNG\r\n\x1a\nrest", "png"))
        self.assertTrue(_magic(b"\xff\xd8\xffrest", "jpg"))
        self.assertTrue(_magic(b"RIFF0000WEBP", "webp"))
        self.assertFalse(_magic(b"<script>", "png"))

    def test_snapshot_is_bounded_and_rejects_missing_image(self):
        identity = uuid4()
        payload = {"subject": " s ", "body": "<script>x</script>", "template_id": "system_update",
                   "images": [{"image_id": str(identity), "width": 5}], "items": ["x"]}
        snapshot = _safe_snapshot(payload, {identity: {"id": identity}})
        self.assertEqual(snapshot["subject"], "s")
        self.assertEqual(snapshot["images"][0]["width"], 60)
        service = EmailNotificationService.__new__(EmailNotificationService)
        html, plain = service._render(snapshot, {identity: {"id": identity, "mime_type": "image/png", "content": b"x"}})
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("s", plain)
        with self.assertRaises(EmailNotificationError):
            _safe_snapshot(payload, {})


class EmailHttpTests(TestCase):
    def setUp(self):
        self.identity = str(uuid4())
        self.secret = "synthetic-email-preview-secret-at-least-48-bytes"
        self.user = InternalUser(user_id=UUID(self.identity), username="alice", email="admin@example.test",
            password_hash="PRIVATE", password_salt="PRIVATE", password_version=3, is_admin=True,
            is_super_admin=True, status="active", deleted_at=None, created_at=None, updated_at=None)
        self.auth = Mock()
        self.auth.find_user_by_id.return_value = self.user
        self.service = Mock()
        self.service.admin_emails = frozenset()
        self.service.preview.return_value = "<html>preview</html>"
        self.service.list_images.return_value = []
        self.service.recipients.return_value = {"total_users": 0, "with_email": 0, "items": []}
        self.service.list_logs.return_value = {"total": 0, "items": []}
        self.app = create_preview_app(Mock(), self.auth, self.secret, email_service=self.service)
        self.client = self.app.test_client()
        now = time.time()
        token = jwt.encode(dict(user_id=self.identity, username="forged", pwd_ver=3, iss="resume-matcher",
                                jti=str(uuid4()), iat=now, exp=now + 3600), self.secret, algorithm="HS256")
        self.headers = {"Authorization": "Bearer " + token}

    def test_preview_and_idempotency_forward_authoritative_actor(self):
        response = self.client.post("/api/v1/admin/notifications/email/preview",
                                    json={"subject": "x", "body": "y"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.service.preview.assert_called_once()
        self.assertEqual(self.service.preview.call_args.kwargs["actor_id"], self.identity)
        self.service.send.return_value = {"email_log_id": str(uuid4())}
        response = self.client.post("/api/v1/admin/notifications/email",
                                    json={"subject": "x", "body": "y"},
                                    headers=self.headers | {"Idempotency-Key": "broadcast-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.send.call_args.kwargs["idempotency_key"], "broadcast-1")

    def test_email_routes_are_closed_without_explicit_service(self):
        closed = create_preview_app(Mock(), self.auth, self.secret).test_client()
        self.assertEqual(closed.get("/api/v1/admin/notifications/email/images", headers=self.headers).status_code, 503)

    def test_non_admin_is_rejected(self):
        self.auth.find_user_by_id.return_value = InternalUser(**{**self.user.__dict__, "is_admin": False,
            "is_super_admin": False, "email": "user@example.test"})
        self.assertEqual(self.client.get("/api/v1/admin/notifications/email/images", headers=self.headers).status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
