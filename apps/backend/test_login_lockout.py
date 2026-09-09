"""
登录防爆破 / 冻结 / 自助解冻 单元测试（test_login_lockout.py）

覆盖 docs/LOGIN_SECURITY_DESIGN.md (v2) §8 清单：
- 连续错 1/2/3 次均为 401；达到 L1 后冷却期（含正确密码）→ 429 + Retry-After
- 冷却期满后正确密码登录成功 → 记录清零
- 冷却门控在冻结前生效；窗口内第 6 次失败 → 冻结，正确密码也 423
- 自助解冻：密码正确 + 验证码正确 → 解冻成功并返回 JWT；密码错计入并保持冻结
- 邮箱不匹配 / 未绑定邮箱 → 400 + 提示联系管理员
- 管理员 unfreeze 兜底可用；非管理员 403；匿名 401
- 每 IP 超限（含不存在用户名撞库）→ 429；成功登录不消耗 IP 预算

运行：python -m unittest test_login_lockout -v
"""

import json
import os
import shutil
import unittest

# ── 必须在 import app 之前重定向数据目录 ─────────────────────────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-lockout")
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

# 时间注入：失败窗口/冷却语义不变（校验路径），但不做真实等待
config.LOGIN_FAIL_LIMIT = 3
config.LOGIN_LOCK_MINUTES = 5
config.LOGIN_FREEZE_WINDOW_MINUTES = 10
config.LOGIN_FREEZE_THRESHOLD = 6
config.LOGIN_MAX_FAIL_PER_IP_10MIN = 10

import auth  # noqa: E402
import mailer  # noqa: E402

_sent: dict[str, list] = {"verify": []}


def _fake_send(to_email: str, code: str, purpose: str = "注册"):
    _sent.setdefault("verify", []).append((to_email, code, purpose))
    return True


def _fake_smtp_available():
    return True


mailer.send_verification_email = _fake_send
mailer.send_invite_code_email = lambda to, code, **kw: True
mailer.send_password_reset_email = lambda to, u, c: True
mailer.send_invite_rejection_email = lambda to, reason: True
mailer.smtp_available = _fake_smtp_available

import app as backend  # noqa: E402

auth_module = auth
# app 模块对 mailer 的引用（app.mailer_mod）：补丁对象必须是它
_app_mailer = getattr(backend, "mailer_mod", None)

# 验证码默认开启；本套件聚焦防爆破/冻结语义，登录调用统一关闭验证码
# （验证码本身由 test_captcha_* 专项用例覆盖）
backend.config.CAPTCHA_ENABLED = False


def _json(resp):
    try:
        return resp.get_json() or {}
    except Exception:
        return {}


class LoginLockoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 彻底重置临时目录，避免上次运行残留
        shutil.rmtree(_TMP, ignore_errors=True)
        for _d in _SUBDIRS + ("logs",):
            os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
        cls.client = backend.app.test_client()
        # 管理员账号（email 命中白名单）+ 普通账号
        admin_email = (list(config.ADMIN_EMAILS) or ["admin@example.com"])[0]
        auser, aerr = auth_module.create_user("lina_admin", "password123", admin_email)
        assert auser, f"admin create failed: {aerr}"
        cls.admin_token = auth_module.generate_jwt(auser["user_id"], auser["username"])
        nuser, _ = auth_module.create_user("lina_normal", "password123", "lina_normal@example.com")
        cls.normal_token = auth_module.generate_jwt(nuser["user_id"], nuser["username"])

    def setUp(self):
        # 每个用例独立状态：清空验证码/限流/失败记录（用户文件保留）
        for d in (
            auth_module.RESETS_DIR,
            auth_module.RATE_LIMITS_DIR,
            auth_module.LOGIN_FAILURES_DIR,
            auth_module.INVITE_REQUESTS_DIR,
            auth_module.INVITE_CODES_DIR,
            auth_module.INVITE_CODES_AUDIT_DIR,
        ):
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        _sent["verify"].clear()
        # 防御：若其它测试覆盖了 app 可见 mailer 对象，恢复补丁
        if _app_mailer is not None:
            _app_mailer.send_verification_email = _fake_send
            _app_mailer.smtp_available = _fake_smtp_available

    def _mkuser(self, name: str, email: str = ""):
        user, err = auth_module.create_user(name, "password123", email)
        assert user, f"create user {name} failed: {err}"
        return user

    def _post(self, path, data=None, token=None):
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(
            path,
            data=json.dumps(data) if data is not None else None,
            headers=headers,
        )

    def _login(self, username, password, ip=None):
        headers = {"X-Forwarded-For": ip} if ip else {}
        return self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": username, "password": password}),
            headers={**headers, "Content-Type": "application/json"},
        )

    def _clear_ip(self, ip: str) -> None:
        auth_module._safe_remove(
            auth_module._rate_file_path(f"login_ip:{auth_module._ip_hash(ip)}")
        )

    def _freeze(self, username: str) -> None:
        """直接落账 6 次窗口失败（绕过冷却门控）使账号冻结。"""
        auth_module.clear_login_failures(username)
        for _ in range(config.LOGIN_FREEZE_THRESHOLD):
            auth_module.record_login_failure(username, ip="198.51.100.1")
        self.assertTrue(auth_module.is_user_frozen(username), f"{username} should be frozen")

    def _expire_cooldown(self, username: str) -> None:
        """把 last_failure_at 拨到冷却时长之外，模拟冷却期满。"""
        rec = auth_module._login_fail_record(username)
        rec["last_failure_at"] = auth_module._now_ts() - (config.LOGIN_LOCK_MINUTES * 60 + 10)
        rec["frozen"] = False
        auth_module._save_login_fail_record(username, rec)

    # ── L1 冷却 ────────────────────────────────────────────────
    def test_cooldown_after_3_failures(self):
        self._mkuser("cd_user", "cd@example.com")
        for i in range(3):
            r = self._login("cd_user", "wrong-pass", ip="10.0.0.1")
            self.assertEqual(r.status_code, 401, f"failure #{i+1} status={r.status_code}")
        # 冷却期内（正确密码也拒）→ 429 + Retry-After
        r = self._login("cd_user", "password123", ip="10.0.0.1")
        self.assertEqual(r.status_code, 429, f"cooldown status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        self.assertIn("Retry-After", r.headers)
        self.assertIn("分钟", _json(r).get("detail", ""))
        auth_module.clear_login_failures("cd_user")

    def test_cooldown_expiry_allows_login_and_resets(self):
        self._mkuser("exp_user", "exp@example.com")
        for _ in range(3):
            self._login("exp_user", "wrong-pass", ip="10.0.0.2")
        self._expire_cooldown("exp_user")
        r = self._login("exp_user", "password123", ip="10.0.0.2")
        self.assertEqual(r.status_code, 200, f"after cooldown status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 登录成功 → 失败记录文件被清除
        rec = auth_module._read_json(auth_module._login_fail_path("exp_user"))
        self.assertTrue(rec is None, "fail record should be removed after successful login")

    # ── 冷却门控 + L2 冻结 ─────────────────────────────────────
    def test_cooldown_gates_then_window_freeze(self):
        self._mkuser("fz_user", "fz@example.com")
        ip = "10.0.3.1"
        # 前 3 次连续失败 → 401
        for i in range(3):
            r = self._login("fz_user", "wrong-pass", ip=ip)
            self.assertEqual(r.status_code, 401, f"failure #{i+1} status={r.status_code}")
        # 第 4 次被冷却门控拦下 → 429（未落账）
        r = self._login("fz_user", "wrong-pass", ip=ip)
        self.assertEqual(r.status_code, 429, f"4th blocked by cooldown status={r.status_code}")
        # 每次失败后模拟冷却期满，继续推进窗口计数：
        # 第 4/5 次失败 → 401；第 6 次（窗口阈值）→ 冻结 423
        for step in (4, 5, 6):
            self._expire_cooldown("fz_user")
            r = self._login("fz_user", "wrong-pass", ip=ip)
            if step == 6:
                self.assertEqual(r.status_code, 423, f"freeze at window threshold status={r.status_code} body={r.get_data(as_text=True)[:150]}")
            else:
                self.assertEqual(r.status_code, 401, f"failure #{step} status={r.status_code}")
        # 冻结后正确密码也 423
        r = self._login("fz_user", "password123", ip=ip)
        self.assertEqual(r.status_code, 423, f"frozen correct pw status={r.status_code}")
        auth_module.clear_login_failures("fz_user")

    # ── 自助解冻 ────────────────────────────────────────────────
    def test_self_unfreeze_success(self):
        self._mkuser("uf_user", "uf@example.com")
        self._freeze("uf_user")
        code = auth_module.create_email_code("uf@example.com", auth_module.PURPOSE_UNFREEZE)
        self.assertTrue(code, "should create unfreeze code")
        r = self._post(
            "/api/v1/auth/unfreeze",
            {"username": "uf_user", "password": "password123", "email": "uf@example.com", "code": code},
        )
        self.assertEqual(r.status_code, 200, f"unfreeze status={r.status_code} body={r.get_data(as_text=True)[:200]}")
        body = _json(r)
        self.assertTrue((body.get("data") or {}).get("token"), "unfreeze returns JWT")
        # 解冻后正常登录
        r = self._login("uf_user", "password123", ip="10.0.0.9")
        self.assertEqual(r.status_code, 200, f"login after unfreeze status={r.status_code}")

    def test_self_unfreeze_wrong_password(self):
        self._mkuser("ufpw_user", "ufpw@example.com")
        self._freeze("ufpw_user")
        code = auth_module.create_email_code("ufpw@example.com", auth_module.PURPOSE_UNFREEZE)
        r = self._post(
            "/api/v1/auth/unfreeze",
            {"username": "ufpw_user", "password": "badpass", "email": "ufpw@example.com", "code": code},
        )
        # 密码错 → 401 且保持冻结
        self.assertEqual(r.status_code, 401, f"wrong pw status={r.status_code}")
        self.assertTrue(auth_module.is_user_frozen("ufpw_user"))
        auth_module.clear_login_failures("ufpw_user")

    def test_unfreeze_email_mismatch(self):
        self._mkuser("mm_user", "mm@example.com")
        self._freeze("mm_user")
        code = auth_module.create_email_code("mm@example.com", auth_module.PURPOSE_UNFREEZE)
        r = self._post(
            "/api/v1/auth/unfreeze",
            {"username": "mm_user", "password": "password123", "email": "other@example.com", "code": code},
        )
        self.assertIn(r.status_code, (400, 409), f"mismatch status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        detail = _json(r).get("detail", "")
        self.assertTrue("邮箱" in detail and "管理员" in detail, f"detail={detail}")
        auth_module.clear_login_failures("mm_user")

    def test_unfreeze_no_email_account(self):
        self._mkuser("noemail_u", "")
        self._freeze("noemail_u")
        # 账号未绑定邮箱：即便给了邮箱/验证码也无法匹配 → 提示联系管理员
        r = self._post(
            "/api/v1/auth/unfreeze",
            {"username": "noemail_u", "password": "password123", "email": "x@example.com", "code": "ABCDEF"},
        )
        self.assertIn(r.status_code, (400, 409), f"status={r.status_code}")
        detail = _json(r).get("detail", "")
        self.assertTrue(("邮箱" in detail and "管理员" in detail) or "绑定" in detail, f"detail={detail}")
        auth_module.clear_login_failures("noemail_u")

    # ── 管理员兜底解冻 ──────────────────────────────────────────
    def test_admin_unfreeze(self):
        self._mkuser("noemail_a", "")
        self._freeze("noemail_a")
        # 未登录 → 401
        r = self.client.post("/api/v1/admin/users/noemail_a/unfreeze")
        self.assertEqual(r.status_code, 401, f"anon status={r.status_code}")
        # 非管理员 → 403
        r = self._post("/api/v1/admin/users/noemail_a/unfreeze", token=self.normal_token)
        self.assertEqual(r.status_code, 403, f"non-admin status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 管理员 → 200 且已解冻
        r = self._post("/api/v1/admin/users/noemail_a/unfreeze", token=self.admin_token)
        self.assertEqual(r.status_code, 200, f"admin status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        self.assertFalse(auth_module.is_user_frozen("noemail_a"), "should be unfrozen after admin action")

    # ── 每 IP 限流 ──────────────────────────────────────────────
    def test_ip_rate_limit(self):
        ip = "203.0.113.77"
        self._clear_ip(ip)
        # 10 次失败（不同用户名模拟撞库）各记 1 次 IP → 第 11 次被拦
        last = None
        for i in range(11):
            last = self._login(f"ghost-user-{i}", "wrong", ip=ip)
            if i < 10:
                self.assertEqual(last.status_code, 401, f"failure #{i+1} status={last.status_code}")
        self.assertEqual(last.status_code, 429, f"ip limit status={last.status_code} body={last.get_data(as_text=True)[:150]}")
        # 限流期间正确密码也 429
        r = self._login("someone_else", "password123", ip=ip)
        self.assertEqual(r.status_code, 429, f"limited correct attempt status={r.status_code}")
        self._clear_ip(ip)

    def test_successful_login_does_not_consume_ip_budget(self):
        self._mkuser("ok_user", "ok@example.com")
        ip = "203.0.113.88"
        self._clear_ip(ip)
        for _ in range(8):
            r = self._login("ok_user", "password123", ip=ip)
            self.assertEqual(r.status_code, 200, f"success status={r.status_code} body={r.get_data(as_text=True)[:120]}")
        # 多次成功后 IP 计数仍为 0（只读检查不落账）
        limited, _ = auth_module.ip_login_rate_exceeded(ip)
        self.assertFalse(limited, "successful logins should not count toward IP limit")
        self._clear_ip(ip)

    # ── 自助解冻验证码发送路由（POST /auth/unfreeze/send-code）────────
    def test_unfreeze_send_code_route(self):
        self._mkuser("ufsc_user", "ufsc@example.com")
        # 未知用户 → 404
        r = self._post("/api/v1/auth/unfreeze/send-code", {"username": "nobody", "email": "ufsc@example.com"})
        self.assertEqual(r.status_code, 404, f"unknown user status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 未冻结账号 → 409
        r = self._post("/api/v1/auth/unfreeze/send-code", {"username": "ufsc_user", "email": "ufsc@example.com"})
        self.assertEqual(r.status_code, 409, f"non-frozen status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 冻结但邮箱不匹配 → 422
        self._freeze("ufsc_user")
        r = self._post("/api/v1/auth/unfreeze/send-code", {"username": "ufsc_user", "email": "other@example.com"})
        self.assertEqual(r.status_code, 422, f"mismatch status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 冻结 + 邮箱匹配 → 200 且真实生成解冻验证码
        r = self._post("/api/v1/auth/unfreeze/send-code", {"username": "ufsc_user", "email": "ufsc@example.com"})
        self.assertEqual(r.status_code, 200, f"send ok status={r.status_code} body={r.get_data(as_text=True)[:200]}")
        self.assertTrue(
            any(to == "ufsc@example.com" for to, _code, purpose in _sent["verify"]),
            "unfreeze verification email should be sent",
        )
        # 60s 冷却内再次发送 → 429
        r = self._post("/api/v1/auth/unfreeze/send-code", {"username": "ufsc_user", "email": "ufsc@example.com"})
        self.assertEqual(r.status_code, 429, f"cooldown status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        auth_module.clear_login_failures("ufsc_user")

    # ── 冻结列表 ────────────────────────────────────────────────
    def test_frozen_list(self):
        self._mkuser("fz_list", "fl@example.com")
        self._freeze("fz_list")
        names = [i.get("username") for i in auth_module.list_frozen_users()]
        self.assertIn("fz_list", names)
        auth_module.clear_login_failures("fz_list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
