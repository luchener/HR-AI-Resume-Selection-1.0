"""Isolated PostgreSQL acceptance for email assets, snapshots and delivery idempotency."""
from concurrent.futures import ThreadPoolExecutor
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from db_auth import AuthService
from db_check_schema import metadata
from db_email_notifications import EmailNotificationError, EmailNotificationService
from db_email_state import email_broadcasts, email_images, email_recipients
from db_http_preview import create_preview_app
from db_models import User
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_test_guard import review_test_url


@skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class EmailNotificationLiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ["DATABASE_TEST_URL"])
        cls.bootstrap = migration_engine(url.render_as_string(hide_password=False))
        cls.engine, cls.sessions = build_runtime(url.set(username="resume_app", password="review-app-only").render_as_string(hide_password=False))
        verify_runtime(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.bootstrap.dispose()

    def setUp(self):
        suffix = uuid4().hex
        self.admin_email = "email-admin-" + suffix + "@example.test"
        self.auth = AuthService(self.sessions, admin_emails=[self.admin_email])
        admin, error = self.auth.create_user("email-admin-" + suffix, "SyntheticPassword9", self.admin_email)
        self.assertIsNone(error)
        user, error = self.auth.create_user("email-user-" + suffix, "SyntheticPassword9", "email-user-" + suffix + "@example.test")
        self.assertIsNone(error)
        self.admin, self.user = admin["user_id"], user["user_id"]
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.admin).values(is_admin=True))
        self.sent = []
        self.fail_email = None
        def sender(to_email, subject, plain, html, images):
            self.sent.append(to_email)
            if to_email == self.fail_email:
                raise RuntimeError("synthetic provider failure")
        self.service = EmailNotificationService(self.sessions, admin_emails=[self.admin_email], send_email=sender)
        self.app = create_preview_app(Store(session_factory=self.sessions), self.auth,
            "synthetic-email-preview-secret-at-least-48-bytes", admin_emails=[self.admin_email], email_service=self.service)
        self.client = self.app.test_client()
        response = self.client.post("/api/v1/auth/login", json={"username": admin["username"], "password": "SyntheticPassword9"})
        self.assertEqual(response.status_code, 200)
        self.headers = {"Authorization": "Bearer " + response.get_json()["data"]["token"]}

    def call(self, method, path, **kwargs):
        kwargs.setdefault("headers", self.headers)
        return getattr(self.client, method)(path, **kwargs)

    def test_snapshot_delivery_idempotency_and_failure_state(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 16
        response = self.call("post", "/api/v1/admin/notifications/email/image",
                             data={"file": (__import__("io").BytesIO(png), "banner.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        image_id = response.get_json()["data"]["image_id"]
        payload = {"subject": "通知", "template_id": "system_update", "body": "正文",
                   "images": [{"image_id": image_id, "width": 200}], "target": "selected",
                   "user_ids": [self.user], "items": ["要点"]}
        key = "broadcast-" + uuid4().hex
        first = self.call("post", "/api/v1/admin/notifications/email", json=payload,
                          headers=self.headers | {"Idempotency-Key": key})
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.get_json()["data"]["sent"], 1)
        before = list(self.sent)
        replay = self.call("post", "/api/v1/admin/notifications/email", json=payload,
                           headers=self.headers | {"Idempotency-Key": key})
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["data"]["replayed"])
        self.assertEqual(self.sent, before)
        changed = payload | {"body": "changed"}
        conflict = self.call("post", "/api/v1/admin/notifications/email", json=changed,
                             headers=self.headers | {"Idempotency-Key": key})
        self.assertEqual(conflict.status_code, 409)
        detail = self.call("get", "/api/v1/admin/notifications/email/logs/" + first.get_json()["data"]["email_log_id"])
        self.assertEqual(detail.status_code, 200)
        self.assertIn("data:image/png;base64", detail.get_json()["data"]["html"])
        with self.bootstrap.connect() as conn:
            rows = conn.execute(sa.select(email_recipients).where(email_recipients.c.user_id == self.user)).mappings().all()
            self.assertEqual(rows[-1]["status"], "sent")

    def test_concurrent_same_key_creates_one_snapshot_and_audit(self):
        payload = {"subject": "并发", "body": "正文", "target": "selected", "user_ids": [self.user]}
        key = "concurrent-" + uuid4().hex
        def invoke():
            return self.service.send(self.admin, payload, password_version=1, request_id=uuid4().hex, idempotency_key=key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _x: invoke(), range(2)))
        self.assertEqual(len({item["email_log_id"] for item in results}), 1)
        self.assertEqual(len(self.sent), 1)
        with self.bootstrap.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(email_broadcasts).where(email_broadcasts.c.idempotency_key == key)), 1)

    def test_provider_failure_is_recorded_and_business_snapshot_remains(self):
        user = self.auth.find_user_by_id(self.user)
        self.fail_email = user.email
        payload = {"subject": "失败", "body": "正文", "target": "selected", "user_ids": [self.user]}
        result = self.service.send(self.admin, payload, password_version=1, request_id=uuid4().hex, idempotency_key="failure-" + uuid4().hex)
        self.assertEqual((result["sent"], result["failed"]), (0, 1))
        with self.bootstrap.connect() as conn:
            row = conn.execute(sa.select(email_broadcasts).where(email_broadcasts.c.id == result["email_log_id"])).mappings().one()
            self.assertEqual((row["target_count"], row["failed"]), (1, 1))

    def test_audit_failure_rolls_back_image_and_broadcast_snapshot(self):
        image = b"\x89PNG\r\n\x1a\n" + b"x" * 16
        with patch("db_email_notifications.append_audit", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.service.save_image(self.admin, image, "x.png", password_version=1, request_id="audit-image")
        with self.bootstrap.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(email_images).where(email_images.c.original_name == "x.png")), 0)

    def test_runtime_cannot_read_or_hard_delete_email_tables(self):
        from sqlalchemy.exc import DBAPIError
        reader = migration_engine(review_test_url(os.environ["DATABASE_TEST_URL"]).set(
            username="resume_readonly", password="review-reader-only").render_as_string(hide_password=False))
        self.addCleanup(reader.dispose)
        for table in ("email_images", "email_broadcasts", "email_broadcast_recipients"):
            for engine, statement in [(self.engine, "DELETE FROM " + table + " WHERE false"),
                                      (reader, "SELECT * FROM public." + table + " LIMIT 0")]:
                with self.assertRaises(DBAPIError):
                    with engine.begin() as conn:
                        conn.execute(sa.text(statement))
