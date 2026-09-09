"""
登录四位数字验证码 单元测试（test_captcha.py）

覆盖：
- GET /api/v1/auth/captcha 返回 captcha_id + 4 位数字 code（公开，免认证）
- 登录必须携带验证码；缺失 → 422
- 验证码错误 / 已使用（一次性）→ 422
- 正确验证码可正常登录
- 验证码过期（TTL 重置为过去）→ 422
- 验证码失败不触发账号防爆破冻结（不落失败记录）
- 未启用（CAPTCHA_ENABLED=False）时接口 404，登录不校验

运行：python -m unittest test_captcha -v（需独立进程，与 test_* 套件隔离 DATA_DIR）
"""

import json
import os
import shutil
import unittest

# ── 必须在 import app 之前重定向数据目录 ─────────────────────────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-captcha")
os.environ["ENV"] = "local"

import config  # noqa: E402

config.DATA_DIR = _TMP
_SUBDIRS = (
    "users",
    "password_resets",
    "resumes",
    "jobs",
    "archives",
    "invite_requests",
    "invite_codes",
    "invite_codes_audit",
    "login_failures",
    "rate_limits",
)
for _d in _SUBDIRS:
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
config.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(config.LOG_DIR, exist_ok=True)
# 本套件专门验证验证码：强制开启
config.CAPTCHA_ENABLED = True

import auth  # noqa: E402
import mailer  # noqa: E402


def _fake_send(to_email: str, code: str, purpose: str = "注册"):
    return True


mailer.send_verification_email = _fake_send
mailer.send_invite_code_email = lambda to, code, **kw: True
mailer.send_password_reset_email = lambda to, u, c: True
mailer.send_invite_rejection_email = lambda to, reason: True
mailer.smtp_available = lambda: True

import app as backend  # noqa: E402

auth_module = auth
backend.config.CAPTCHA_ENABLED = True


def _json(resp):
    try:
        return resp.get_json() or {}
    except Exception:
        return {}


class CaptchaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)
        for _d in _SUBDIRS + ("logs",):
            os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
        cls.client = backend.app.test_client()
        user, err = auth_module.create_user("cap_user", "password123", "cap_user@example.com")
        assert user, f"create user failed: {err}"

    def setUp(self):
        # 清空验证码/限流目录，保证每个用例独立
        for d in (auth_module.RATE_LIMITS_DIR, auth_module.LOGIN_FAILURES_DIR):
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass

    def _login(self, captcha_id="", captcha_code=""):
        payload = {"username": "cap_user", "password": "password123"}
        if captcha_id:
            payload["captcha_id"] = captcha_id
        if captcha_code:
            payload["captcha_code"] = captcha_code
        return self.client.post(
            "/api/v1/auth/login",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    def test_captcha_endpoint_public_and_shape(self):
        r = self.client.get("/api/v1/auth/captcha")
        self.assertEqual(r.status_code, 200, _json(r))
        data = _json(r)["data"]
        self.assertTrue(data.get("captcha_id"))
        code = data.get("code", "")
        self.assertEqual(len(code), 4)
        self.assertTrue(code.isdigit())

    def test_login_requires_captcha(self):
        r = self._login()
        self.assertEqual(r.status_code, 422, _json(r))
        self.assertIn("验证码", _json(r)["detail"])

    def test_login_with_wrong_captcha(self):
        r = self.client.get("/api/v1/auth/captcha")
        captcha_id = _json(r)["data"]["captcha_id"]
        r = self._login(captcha_id=captcha_id, captcha_code="0000")
        self.assertEqual(r.status_code, 422, _json(r))
        self.assertIn("验证码", _json(r)["detail"])

    def test_captcha_is_one_time(self):
        r = self.client.get("/api/v1/auth/captcha")
        data = _json(r)["data"]
        # 正确验证码 → 登录成功
        r = self._login(captcha_id=data["captcha_id"], captcha_code=data["code"])
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertIn("token", _json(r)["data"])
        # 同一验证码二次使用 → 拒绝（一次性销毁）
        r = self._login(captcha_id=data["captcha_id"], captcha_code=data["code"])
        self.assertEqual(r.status_code, 422, _json(r))

    def test_expired_captcha_rejected(self):
        r = self.client.get("/api/v1/auth/captcha")
        data = _json(r)["data"]
        path = auth_module._captcha_file(data["captcha_id"])
        record = auth_module._read_json(path)
        record["expires_at"] = auth_module.time.time() - 10  # 已过期
        auth_module._write_json(path, record)
        r = self._login(captcha_id=data["captcha_id"], captcha_code=data["code"])
        self.assertEqual(r.status_code, 422, _json(r))

    def test_captcha_failure_does_not_trigger_freeze(self):
        # 连续 3 次带错误验证码的登录：不落账号失败记录 → 不触发冷却/冻结
        for _ in range(3):
            r = self.client.get("/api/v1/auth/captcha")
            captcha_id = _json(r)["data"]["captcha_id"]
            r = self._login(captcha_id=captcha_id, captcha_code="0000")
            self.assertEqual(r.status_code, 422, _json(r))
        rec = auth_module._login_fail_record("cap_user")
        self.assertEqual(rec.get("fail_count", 0), 0)
        self.assertFalse(auth_module.is_user_frozen("cap_user"))

    def test_disabled_captcha_endpoint_404(self):
        previous = backend.config.CAPTCHA_ENABLED
        backend.config.CAPTCHA_ENABLED = False
        try:
            r = self.client.get("/api/v1/auth/captcha")
            self.assertEqual(r.status_code, 404, _json(r))
            # 关闭后登录无需验证码
            r = self._login()
            self.assertEqual(r.status_code, 200, _json(r))
        finally:
            backend.config.CAPTCHA_ENABLED = previous


if __name__ == "__main__":
    unittest.main()