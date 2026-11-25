"""Contract tests for the newly wired admin/AI preview routes.

These tests use injected services only: no legacy filesystem, provider, or
database is reachable.  PostgreSQL transaction behavior remains covered by the
existing live acceptance suites when a disposable database is supplied.
"""
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import jwt

from db_auth import InternalUser
from db_http_preview import create_preview_app


class AdminAIHTTPTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.owner = str(uuid4())
        self.secret = "admin-ai-preview-secret-with-at-least-48-bytes-for-tests"
        self.user = InternalUser(UUID(self.owner), "alice", "alice@example.com", "hash", "salt", 3,
                                 False, False, "active", None, now, now)
        self.auth = Mock()
        self.auth.find_user_by_id.return_value = self.user
        self.auth.authenticate_user.return_value = self.user.to_public()
        self.store = Mock()
        self.admin = Mock()
        self.admin.list_ops.return_value = {"items": [], "total": 0, "page": 1, "size": 50}
        self.admin.export_ops.return_value = {"items": []}
        self.admin.list_resumes.return_value = {"items": [], "total": 0, "page": 1, "size": 20}
        self.admin.user_usage.return_value = {"granularity": "day", "labels": [], "series": {}, "summary": {}}
        self.admin.usage_ranking.return_value = []
        self.ai = Mock()
        self.ai.normalize.side_effect = lambda value: value
        self.ai.test.return_value = {"ok": True, "provider": "deepseek", "model": "m"}
        self.ai.improve.return_value = {"resume_id": "r", "job_id": "j", "analysis_result": {"final_score": 80}}
        self.app = create_preview_app(self.store, self.auth, self.secret,
                                      admin_emails=[self.user.email], admin_service=self.admin, ai_service=self.ai)
        now_ts = time.time()
        token = jwt.encode({"user_id": self.owner, "username": "alice", "pwd_ver": 3,
                            "iss": "resume-matcher", "jti": str(uuid4()), "iat": now_ts,
                            "exp": now_ts + 3600}, self.secret, algorithm="HS256")
        self.headers = {"Authorization": "Bearer " + token}

    def test_admin_routes_forward_authoritative_identity_and_filters(self):
        client = self.app.test_client()
        response = client.get("/api/v1/admin/ops?op=user_login&keyword=alice&page=2&size=10", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.admin.list_ops.assert_called_once_with(self.owner, password_version=3,
            op="user_login", keyword="alice", page="2", size="10")
        self.assertEqual(client.get("/api/v1/admin/resumes", headers=self.headers).status_code, 200)
        self.assertEqual(client.get("/api/v1/admin/users/usage-ranking?limit=4", headers=self.headers).status_code, 200)

    def test_admin_export_is_attachment_and_anonymous_is_closed(self):
        client = self.app.test_client()
        response = client.get("/api/v1/admin/ops/export?format=json", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertEqual(client.get("/api/v1/admin/ops").status_code, 401)

    def test_ai_routes_validate_and_use_injected_provider(self):
        client = self.app.test_client()
        response = client.post("/api/v1/ai/test", json={"ai_config": {"provider": "deepseek"}}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.ai.test.assert_called_once_with(self.owner, runtime_config={"provider": "deepseek"}, password_version=3)
        response = client.post("/api/v1/resumes/improve", json={"resume_id": str(uuid4()), "job_id": str(uuid4())}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.ai.improve.assert_called_once()

    def test_ai_and_admin_are_opt_in(self):
        client = create_preview_app(self.store, self.auth, self.secret).test_client()
        for path, method in (("/api/v1/admin/ops", "get"), ("/api/v1/admin/resumes", "get"),
                             ("/api/v1/ai/test", "post"), ("/api/v1/resumes/improve", "post")):
            response = getattr(client, method)(path, json={} if method == "post" else None)
            self.assertEqual(response.status_code, 503, path)


if __name__ == "__main__":
    unittest.main()
