"""HTTP contract tests for the explicitly injected SQL quota adapter."""
from dataclasses import replace
from time import time
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

import jwt

from db_auth import InternalUser
from db_http_preview import create_preview_app
from db_quota_service import QuotaPermissionError, QuotaValidationError


class QuotaHTTPTests(TestCase):
    def setUp(self):
        self.secret = "synthetic-quota-http-secret-with-at-least-48-bytes"
        self.user = InternalUser(uuid4(), "owner", "owner@example.test", "hash", "salt",
                                 1, True, True, "active", None, None, None)
        self.auth = Mock(spec=["find_user_by_id", "authenticate_user"])
        self.auth.find_user_by_id.return_value = self.user
        self.quota = Mock(spec=["get_settings", "update_settings", "set_user_quota",
                                "reset_user_quota", "get_my_quota"])
        self.quota.get_settings.return_value = {"quota_enabled": False, "users": []}
        self.quota.update_settings.return_value = self.quota.get_settings.return_value
        self.quota.set_user_quota.return_value = self.quota.get_settings.return_value
        self.quota.reset_user_quota.return_value = {"message": "已恢复全局默认。"}
        self.quota.get_my_quota.return_value = {"analysis_enabled": True, "unlimited": True}
        self.app = create_preview_app(Mock(), self.auth, self.secret,
                                      admin_emails=[self.user.email], quota_service=self.quota)
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + jwt.encode({
            "user_id": str(self.user.user_id), "username": self.user.username,
            "pwd_ver": 1, "iss": "resume-matcher", "jti": str(uuid4()),
            "iat": time(), "exp": time() + 3600,
        }, self.secret, algorithm="HS256")}

    def test_routes_forward_authoritative_actor_and_envelopes(self):
        response = self.client.get("/api/v1/quota/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], self.quota.get_my_quota.return_value)
        self.quota.get_my_quota.assert_called_once_with(str(self.user.user_id), password_version=1)

        response = self.client.get("/api/v1/admin/quota/settings", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.quota.get_settings.assert_called_once()
        self.assertEqual(self.quota.get_settings.call_args.kwargs["password_version"], 1)
        self.assertTrue(self.quota.get_settings.call_args.kwargs["request_id"].startswith("db-preview:"))

        for method, path, payload, name in [
            ("put", "/api/v1/admin/quota/settings", {"quota_enabled": True}, "update_settings"),
            ("put", "/api/v1/admin/quota/users/member", {"daily_limit": 3}, "set_user_quota"),
            ("delete", "/api/v1/admin/quota/users/member", None, "reset_user_quota"),
        ]:
            response = getattr(self.client, method)(path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.get_json()), {"request_id", "data"})
            self.assertEqual(getattr(self.quota, name).call_args.kwargs["password_version"], 1)

    def test_default_preview_and_non_admin_are_blocked_without_service_calls(self):
        default = create_preview_app(Mock(), self.auth, self.secret).test_client()
        for path, method in [("/api/v1/quota/me", "get"),
                             ("/api/v1/admin/quota/settings", "get"),
                             ("/api/v1/admin/quota/settings", "put")]:
            self.assertEqual(getattr(default, method)(path, headers=self.headers).status_code, 503)
        self.auth.find_user_by_id.return_value = replace(self.user, is_admin=False,
                                                          is_super_admin=False, email="ordinary@test.invalid")
        self.assertEqual(self.client.get("/api/v1/admin/quota/settings", headers=self.headers).status_code, 403)
        self.assertEqual(self.quota.get_settings.call_count, 0)

    def test_service_errors_are_fixed_and_secret_free(self):
        self.quota.update_settings.side_effect = QuotaValidationError("secret details", 422)
        response = self.client.put("/api/v1/admin/quota/settings", json={"quota_enabled": True}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret details", response.get_data(as_text=True))
        self.quota.update_settings.side_effect = QuotaPermissionError("secret details")
        response = self.client.put("/api/v1/admin/quota/settings", json={"quota_enabled": True}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("secret details", response.get_data(as_text=True))


if __name__ == "__main__":
    import unittest
    unittest.main()
