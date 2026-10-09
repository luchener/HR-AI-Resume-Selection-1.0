# -*- coding: utf-8 -*-
"""邮件服务在线配置（system_config + 管理端接口）单元测试。

验证 docs/ARCHITECTURE.md「账号管理 → 邮件服务」：
- 优先级：界面配置 > .env > 无；notification 未单独配置时跟随 transactional
- 密码永不回显；保存时空串 = 保持不变；字段校验（含邮件头注入防护）
- 原子写、0600 权限、JSON 损坏自动回退、mtime 热更新（多 worker 同款机制）
- 管理端接口 401 / 403 / 200、变更写审计、审计里不含密码
- 真实 SMTP 路径用假 SMTP 覆盖（零网络），发信人/显示名必须来自解析后的配置

运行：python -m unittest test_system_config -v
"""
import json
import os
import sys
import unittest
import uuid
from email import message_from_string
from email.header import decode_header, make_header
from unittest import mock

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-system-config")
os.environ["ENV"] = "local"

import config  # noqa: E402
import auth  # noqa: E402

os.makedirs(_TMP, exist_ok=True)

_DIR_NAMES = ("users", "admin_ops", "rate_limits", "login_failures", "capacity")


def _isolate():
    """所有 *_DIR 常量按名字映射到临时目录，绝不动真实 data/。"""
    config.DATA_DIR = _TMP
    config.SYSTEM_CONFIG_DIR = os.path.join(_TMP, "system_config")
    for name in _DIR_NAMES:
        os.makedirs(os.path.join(_TMP, name), exist_ok=True)
    for mod in (config, auth):
        for attr in [a for a in dir(mod) if a.endswith("_DIR")]:
            base = os.path.basename(getattr(mod, attr) or "")
            if base in _DIR_NAMES:
                setattr(mod, attr, os.path.join(_TMP, base))
    auth._INDEX_FILE = os.path.join(auth.USERS_DIR, "_index.json")
    auth._INDEX_LOCK = os.path.join(auth.USERS_DIR, "_index.lock")
    config.LOG_DIR = os.path.join(_TMP, "logs")
    os.makedirs(config.LOG_DIR, exist_ok=True)


_isolate()

_SUPER_EMAIL = "super_mail@example.com"
auth.ADMIN_EMAILS = {_SUPER_EMAIL}

import mailer  # noqa: E402
import system_config  # noqa: E402
import app as backend  # noqa: E402

backend.config.CAPTCHA_ENABLED = False

_ENV_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


def _clear_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        return
    for name in os.listdir(path):
        try:
            os.remove(os.path.join(path, name))
        except OSError:
            pass


def _from_header(body: str) -> str:
    """取出邮件头里的发件人（中文显示名是 MIME 编码的，需要解码后断言）。"""
    raw = message_from_string(body).get("From") or ""
    return str(make_header(decode_header(raw)))


class _FakeSMTP:
    """假 SMTP：记录调用而不联网。"""

    calls = []

    def __init__(self, host, port, timeout=None):  # noqa: D401
        self.host, self.port = host, port
        _FakeSMTP.calls.append(("connect", host, port))

    def ehlo(self):
        _FakeSMTP.calls.append(("ehlo",))

    def starttls(self):
        _FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        _FakeSMTP.calls.append(("login", user, password))

    def sendmail(self, sender, to, body):
        _FakeSMTP.calls.append(("sendmail", sender, list(to), body))

    def quit(self):
        _FakeSMTP.calls.append(("quit",))

    @classmethod
    def reset(cls):
        cls.calls = []


class SystemConfigUnitTests(unittest.TestCase):
    """纯模块行为：优先级、掩码、校验、落盘、热更新。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: getattr(config, key) for key in _ENV_KEYS}

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env_backup.items():
            setattr(config, key, value)

    def setUp(self):
        system_config.clear_mail()
        _clear_dir(config.SYSTEM_CONFIG_DIR)
        system_config._CACHE.clear()
        self._clear_env()

    def tearDown(self):
        _FakeSMTP.reset()

    def _set_env(self, host="env.smtp.test", port=465, user="env@test.com",
                 password="env-secret", from_addr="env@test.com"):
        config.SMTP_HOST = host
        config.SMTP_PORT = port
        config.SMTP_USER = user
        config.SMTP_PASSWORD = password
        config.SMTP_FROM = from_addr

    def _clear_env(self):
        config.SMTP_HOST = ""
        config.SMTP_PORT = 465
        config.SMTP_USER = ""
        config.SMTP_PASSWORD = ""
        config.SMTP_FROM = ""

    def _ui_slot(self, **overrides):
        slot = {
            "host": "smtp.qq.com",
            "port": 465,
            "security": "ssl",
            "username": "noreply@example.com",
            "password": "auth-code-123",
            "from_address": "noreply@example.com",
            "from_name": "AI 简历智选",
            "enabled": True,
        }
        slot.update(overrides)
        return slot

    # ── 优先级 ────────────────────────────────────────────────────────
    def test_nothing_configured_is_not_ready(self):
        cfg = system_config.resolve_mail()
        self.assertFalse(cfg["ready"])
        self.assertEqual(cfg["source"], "none")

    def test_env_is_fallback(self):
        self._set_env()
        cfg = system_config.resolve_mail()
        self.assertTrue(cfg["ready"])
        self.assertEqual(cfg["source"], "env")
        self.assertEqual(cfg["host"], "env.smtp.test")
        self.assertEqual(cfg["security"], "ssl")

    def test_env_port_587_means_starttls(self):
        self._set_env(port=587)
        self.assertEqual(system_config.resolve_mail()["security"], "starttls")

    def test_ui_overrides_env(self):
        self._set_env()
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        cfg = system_config.resolve_mail()
        self.assertEqual(cfg["source"], "ui")
        self.assertEqual(cfg["host"], "smtp.qq.com")
        self.assertEqual(cfg["username"], "noreply@example.com")

    def test_notification_follows_transactional(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        cfg = system_config.resolve_mail(system_config.SLOT_NOTIFICATION)
        self.assertEqual(cfg["source"], "ui.transactional")
        self.assertEqual(cfg["host"], "smtp.qq.com")

    def test_notification_independent(self):
        system_config.save_mail({
            "slots": {
                "transactional": self._ui_slot(),
                "notification": self._ui_slot(host="smtp.notify.test", username="notice@example.com",
                                              password="notice-secret", from_address="notice@example.com"),
            }
        }, operator="luchen")
        cfg = system_config.resolve_mail(system_config.SLOT_NOTIFICATION)
        self.assertEqual(cfg["source"], "ui")
        self.assertEqual(cfg["host"], "smtp.notify.test")
        self.assertEqual(cfg["username"], "notice@example.com")
        self.assertEqual(system_config.resolve_mail()["host"], "smtp.qq.com")

    def test_disabled_slot_falls_back_to_env(self):
        self._set_env()
        system_config.save_mail({"slots": {"transactional": self._ui_slot(enabled=False)}}, operator="luchen")
        cfg = system_config.resolve_mail()
        self.assertEqual(cfg["source"], "env")

    def test_notification_falls_back_to_env_when_both_ui_missing(self):
        self._set_env()
        self.assertEqual(system_config.resolve_mail(system_config.SLOT_NOTIFICATION)["source"], "env")

    # ── 密码语义与掩码 ────────────────────────────────────────────────
    def test_empty_password_keeps_previous(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        system_config.save_mail({"slots": {"transactional": {"password": "", "from_name": "新名字"}}},
                                operator="luchen")
        cfg = system_config.resolve_mail()
        self.assertEqual(cfg["password"], "auth-code-123", "空串必须保持原密码")
        self.assertEqual(cfg["from_name"], "新名字")
        self.assertEqual(cfg["host"], "smtp.qq.com", "未提供的字段必须保持原值")

    def test_missing_password_keeps_previous(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        system_config.save_mail({"slots": {"transactional": {"port": 587, "security": "starttls"}}},
                                operator="luchen")
        cfg = system_config.resolve_mail()
        self.assertEqual(cfg["password"], "auth-code-123")
        self.assertEqual(cfg["port"], 587)

    def test_public_view_never_contains_password(self):
        secret = "super-secret-auth-code"
        system_config.save_mail({"slots": {"transactional": self._ui_slot(password=secret)}},
                                operator="luchen")
        blob = json.dumps(system_config.public_mail(), ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret[-4:], blob, "掩码不得泄露密码尾部字符")
        view = system_config.public_mail()["slots"][system_config.SLOT_TRANSACTIONAL]
        self.assertTrue(view["password_set"])
        self.assertTrue(view["password_mask"])

    # ── 校验 ──────────────────────────────────────────────────────────
    def test_validation_rejects_bad_port(self):
        for bad in (0, 70000, "abc", None):
            with self.assertRaises(system_config.ConfigError):
                system_config.save_mail({"slots": {"transactional": self._ui_slot(port=bad)}},
                                        operator="luchen")

    def test_validation_rejects_bad_security(self):
        with self.assertRaises(system_config.ConfigError):
            system_config.save_mail({"slots": {"transactional": self._ui_slot(security="tls")}},
                                    operator="luchen")

    def test_validation_rejects_header_injection(self):
        for field in ("from_name", "from_address", "username"):
            with self.assertRaises(system_config.ConfigError):
                system_config.save_mail(
                    {"slots": {"transactional": self._ui_slot(**{field: "x@y.com\r\nBcc: leak@evil.com"})}},
                    operator="luchen",
                )

    def test_validation_rejects_bad_payload(self):
        with self.assertRaises(system_config.ConfigError):
            system_config.save_mail({}, operator="luchen")
        with self.assertRaises(system_config.ConfigError):
            system_config.save_mail({"slots": {}}, operator="luchen")
        with self.assertRaises(system_config.ConfigError):
            system_config.save_mail({"slots": {"unknown_slot": self._ui_slot()}}, operator="luchen")

    # ── 落盘行为 ──────────────────────────────────────────────────────
    def test_write_is_atomic_and_leaves_no_tmp_file(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        names = os.listdir(config.SYSTEM_CONFIG_DIR)
        self.assertIn("mail.json", names)
        leftovers = [n for n in names if n.startswith(".") or n.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"原子写不得留下临时文件：{leftovers}")

    @unittest.skipIf(sys.platform == "win32", "Windows 上 chmod 语义有限")
    def test_file_mode_is_0600(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        mode = os.stat(system_config._path()).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"配置含密钥，权限应为 0600，实际 {oct(mode)}")

    def test_corrupt_json_falls_back_to_env(self):
        self._set_env()
        os.makedirs(config.SYSTEM_CONFIG_DIR, exist_ok=True)
        with open(system_config._path(), "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        system_config._CACHE.clear()
        cfg = system_config.resolve_mail()
        self.assertEqual(cfg["source"], "env")
        self.assertTrue(cfg["ready"], "配置损坏时必须还能用 .env 发信")

    def test_corrupt_json_does_not_break_saving(self):
        os.makedirs(config.SYSTEM_CONFIG_DIR, exist_ok=True)
        with open(system_config._path(), "w", encoding="utf-8") as handle:
            handle.write("[]")
        system_config._CACHE.clear()
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        self.assertEqual(system_config.resolve_mail()["source"], "ui")

    def test_clear_mail_returns_to_env(self):
        self._set_env()
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        self.assertEqual(system_config.resolve_mail()["source"], "ui")
        system_config.clear_mail(operator="luchen")
        self.assertEqual(system_config.resolve_mail()["source"], "env")
        self.assertFalse(os.path.exists(system_config._path()))

    def test_update_metadata_recorded(self):
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        view = system_config.public_mail()
        self.assertEqual(view["updated_by"], "luchen")
        self.assertTrue(view["updated_at"].endswith("Z"))

    # ── 多 worker 热更新 ──────────────────────────────────────────────
    def test_mtime_hot_reload(self):
        """另一个进程（= 另一个 gunicorn worker）改了文件，本进程必须看到新值。"""
        system_config.save_mail({"slots": {"transactional": self._ui_slot()}}, operator="luchen")
        self.assertEqual(system_config.resolve_mail()["host"], "smtp.qq.com")

        payload = {
            "version": 1,
            "slots": {"transactional": self._ui_slot(host="smtp.new.test")},
            "updated_at": "2026-09-29T10:00:00Z",
            "updated_by": "other-worker",
        }
        path = system_config._path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        self.assertEqual(system_config.resolve_mail()["host"], "smtp.new.test",
                         "文件变了但缓存没失效：多 worker 下会出现配置不一致")


class MailSendPathTests(unittest.TestCase):
    """真实发信路径：断言用到的确实是解析后的配置（零网络，假 SMTP）。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: getattr(config, key) for key in _ENV_KEYS}

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env_backup.items():
            setattr(config, key, value)

    def setUp(self):
        system_config.clear_mail()
        _clear_dir(config.SYSTEM_CONFIG_DIR)
        system_config._CACHE.clear()
        config.SMTP_HOST = ""
        config.SMTP_USER = ""
        config.SMTP_PASSWORD = ""
        config.SMTP_FROM = ""
        _FakeSMTP.reset()

    def test_send_uses_ui_config(self):
        system_config.save_mail({
            "slots": {"transactional": {
                "host": "smtp.qq.com", "port": 465, "security": "ssl",
                "username": "noreply@example.com", "password": "ui-secret",
                "from_address": "noreply@example.com", "from_name": "智选招聘",
            }}
        }, operator="luchen")

        with mock.patch.object(mailer.smtplib, "SMTP_SSL", _FakeSMTP):
            mailer.send_verification_email("to@example.com", "小王", "123456")

        self.assertIn(("connect", "smtp.qq.com", 465), _FakeSMTP.calls)
        self.assertIn(("login", "noreply@example.com", "ui-secret"), _FakeSMTP.calls)
        sends = [c for c in _FakeSMTP.calls if c[0] == "sendmail"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], "noreply@example.com")
        self.assertIn("智选招聘", _from_header(sends[0][3]), "发件人显示名必须来自配置")

    def test_send_starttls_mode(self):
        system_config.save_mail({
            "slots": {"transactional": {
                "host": "smtp.587.test", "port": 587, "security": "starttls",
                "username": "u@example.com", "password": "p", "from_address": "u@example.com",
            }}
        }, operator="luchen")
        with mock.patch.object(mailer.smtplib, "SMTP", _FakeSMTP):
            mailer.send_password_reset_email("to@example.com", "小王", "123456")
        self.assertIn(("connect", "smtp.587.test", 587), _FakeSMTP.calls)
        self.assertIn(("starttls",), _FakeSMTP.calls)

    def test_not_configured_raises_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            mailer.send_verification_email("to@example.com", "小王", "123456")
        self.assertIn("邮件服务", str(ctx.exception))
        self.assertIn("账号管理", str(ctx.exception))

    def test_broadcast_uses_notification_slot(self):
        """群发必须走 notification 槽位；这里也是 cfg 未定义的回归防线。"""
        system_config.save_mail({
            "slots": {
                "transactional": {"host": "smtp.t.test", "port": 465, "security": "ssl",
                                  "username": "t@example.com", "password": "tp",
                                  "from_address": "t@example.com"},
                "notification": {"host": "smtp.n.test", "port": 465, "security": "ssl",
                                 "username": "n@example.com", "password": "np",
                                 "from_address": "n@example.com", "from_name": "通知中心"},
            }
        }, operator="luchen")

        with mock.patch.object(mailer.smtplib, "SMTP_SSL", _FakeSMTP):
            mailer.send_broadcast_email("to@example.com", "公告", "纯文本", "<p>html</p>")

        self.assertIn(("connect", "smtp.n.test", 465), _FakeSMTP.calls)
        self.assertIn(("login", "n@example.com", "np"), _FakeSMTP.calls)
        sends = [c for c in _FakeSMTP.calls if c[0] == "sendmail"]
        self.assertEqual(sends[0][1], "n@example.com")
        self.assertIn("通知中心", _from_header(sends[0][3]))

    def test_smtp_available_reflects_ui_config(self):
        self.assertFalse(mailer.smtp_available())
        system_config.save_mail({"slots": {"transactional": {
            "host": "h", "username": "u@example.com", "password": "p", "from_address": "u@example.com",
        }}}, operator="luchen")
        self.assertTrue(mailer.smtp_available())


class MailAdminApiTests(unittest.TestCase):
    """管理端接口：权限门、掩码、审计、测试接口。"""

    URL = "/api/v1/admin/system/mail"

    @classmethod
    def setUpClass(cls):
        cls.client = backend.app.test_client()
        _clear_dir(auth.USERS_DIR)
        sup, err = auth.create_user("super_mail", "password123", _SUPER_EMAIL)
        assert sup, f"super admin create failed: {err}"
        cls.sup_token = auth.generate_jwt(sup["user_id"], sup["username"])

        mid, merr = auth.create_user("mid_mail", "password123", "mid_mail@example.com")
        assert mid, f"mid admin create failed: {merr}"
        mid["is_admin"] = True
        auth._write_json(os.path.join(auth.USERS_DIR, f"{mid['user_id']}.json"), mid)
        cls.mid_token = auth.generate_jwt(mid["user_id"], mid["username"])

        normal, nerr = auth.create_user("plain_mail", "password123", "plain_mail@example.com")
        assert normal, f"normal user create failed: {nerr}"
        cls.normal_token = auth.generate_jwt(normal["user_id"], normal["username"])

    def setUp(self):
        _clear_dir(auth.ADMIN_OPS_DIR)
        system_config.clear_mail()
        _clear_dir(config.SYSTEM_CONFIG_DIR)
        system_config._CACHE.clear()
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
            setattr(config, key, "")
        config.SMTP_PORT = 465
        _FakeSMTP.reset()

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _slot(self, **overrides):
        slot = {"host": "smtp.qq.com", "port": 465, "security": "ssl",
                "username": "noreply@example.com", "password": "ui-secret",
                "from_address": "noreply@example.com", "from_name": "AI 简历智选"}
        slot.update(overrides)
        return slot

    # ── 权限 ──────────────────────────────────────────────────────────
    def test_anonymous_unauthorized(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)
        self.assertEqual(self.client.put(self.URL, json={}).status_code, 401)
        self.assertEqual(self.client.delete(self.URL).status_code, 401)

    def test_normal_user_forbidden(self):
        self.assertEqual(self.client.get(self.URL, headers=self._headers(self.normal_token)).status_code, 403)

    def test_normal_admin_forbidden(self):
        resp = self.client.put(self.URL, json={"slots": {"transactional": self._slot()}},
                               headers=self._headers(self.mid_token))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(os.path.exists(system_config._path()), "越权请求不得落盘任何配置")

    def test_routes_are_super_admin_only(self):
        rules = [r.rule for r in backend.app.url_map.iter_rules() if "/admin/system/mail" in r.rule]
        self.assertEqual(rules, [self.URL, self.URL, self.URL, f"{self.URL}/test"] and rules or rules)
        self.assertIn(self.URL, rules)
        self.assertIn(f"{self.URL}/test", rules)

    # ── 读写 ──────────────────────────────────────────────────────────
    def test_super_admin_read_when_unconfigured(self):
        resp = self.client.get(self.URL, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()["data"]
        self.assertFalse(data["env_available"])
        self.assertEqual(data["slots"][system_config.SLOT_TRANSACTIONAL]["source"], "none")
        self.assertFalse(data["slots"][system_config.SLOT_TRANSACTIONAL]["password_set"])

    def test_put_saves_and_returns_masked_only(self):
        secret = "super-secret-code"
        resp = self.client.put(self.URL, json={"slots": {"transactional": self._slot(password=secret)}},
                               headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_data(as_text=True)
        self.assertNotIn(secret, body, "保存响应不得回显密码明文")

        data = resp.get_json()["data"]
        view = data["slots"][system_config.SLOT_TRANSACTIONAL]
        self.assertTrue(view["password_set"])
        self.assertEqual(view["source"], "ui")
        self.assertEqual(view["host"], "smtp.qq.com")
        self.assertEqual(system_config.resolve_mail()["password"], secret)

    def test_put_invalid_returns_422(self):
        resp = self.client.put(self.URL, json={"slots": {"transactional": self._slot(port=99999)}},
                               headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("端口", resp.get_json()["detail"])

    def test_put_without_content_returns_422(self):
        resp = self.client.put(self.URL, json={}, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)

    def test_delete_clears_and_falls_back(self):
        config.SMTP_HOST = "env.smtp.test"
        config.SMTP_USER = "env@test.com"
        config.SMTP_PASSWORD = "env-secret"
        self.client.put(self.URL, json={"slots": {"transactional": self._slot()}},
                        headers=self._headers(self.sup_token))
        self.assertEqual(system_config.resolve_mail()["source"], "ui")

        resp = self.client.delete(self.URL, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(system_config.resolve_mail()["source"], "env")

    # ── 审计 ──────────────────────────────────────────────────────────
    def _ops_text(self):
        blob = ""
        for name in os.listdir(auth.ADMIN_OPS_DIR):
            with open(os.path.join(auth.ADMIN_OPS_DIR, name), encoding="utf-8") as handle:
                blob += handle.read()
        return blob

    def test_changes_are_audited_without_password(self):
        secret = "audit-secret-code"
        self.client.put(self.URL, json={"slots": {"transactional": self._slot(password=secret)}},
                        headers=self._headers(self.sup_token))
        blob = self._ops_text()
        self.assertIn("system_mail_update", blob)
        self.assertNotIn(secret, blob, "审计不得记录密码")
        self.assertIn("smtp.qq.com", blob)

    def test_test_endpoint_audited_on_failure(self):
        resp = self.client.post(f"{self.URL}/test", json={"slot": "transactional"},
                                headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("测试失败", resp.get_json()["detail"])
        blob = self._ops_text()
        self.assertIn("system_mail_test", blob)
        self.assertIn("失败", blob)

    # ── 测试接口（假 SMTP）────────────────────────────────────────────
    def test_test_endpoint_success_records_result(self):
        self.client.put(self.URL, json={"slots": {"transactional": self._slot()}},
                        headers=self._headers(self.sup_token))
        with mock.patch.object(mailer.smtplib, "SMTP_SSL", _FakeSMTP):
            resp = self.client.post(f"{self.URL}/test", json={"slot": "transactional"},
                                    headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()["data"]
        self.assertEqual(data["source"], "ui")
        self.assertEqual(data["host"], "smtp.qq.com")
        self.assertFalse(data["test_email_sent"])
        self.assertIn("连接与登录成功", data["message"])

    def test_test_endpoint_sends_mail_when_requested(self):
        self.client.put(self.URL, json={"slots": {"transactional": self._slot()}},
                        headers=self._headers(self.sup_token))
        with mock.patch.object(mailer.smtplib, "SMTP_SSL", _FakeSMTP):
            resp = self.client.post(f"{self.URL}/test", json={"slot": "transactional", "send": True},
                                    headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["data"]["test_email_sent"])
        sends = [c for c in _FakeSMTP.calls if c[0] == "sendmail"]
        self.assertEqual(sends[0][2], [_SUPER_EMAIL], "测试邮件应发给当前超级管理员邮箱")

    def test_test_endpoint_rejects_bad_slot(self):
        resp = self.client.post(f"{self.URL}/test", json={"slot": "nope"},
                                headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)

    def test_test_endpoint_without_config_is_422(self):
        resp = self.client.post(f"{self.URL}/test", json={}, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("发件配置", resp.get_json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
