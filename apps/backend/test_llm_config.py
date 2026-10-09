# -*- coding: utf-8 -*-
"""模型配置（system_config + 管理端接口）单元测试。

验证 docs/ARCHITECTURE.md「账号管理 → 模型配置」：
- 优先级：界面配置 > .env > 无；enabled=false 时回退 .env
- API Key 永不回显（只回掩码）；保存时空串 = 保持不变
- base_url 白名单：只允许 https 公网地址；内网/回环/元数据地址一律拒绝（防 SSRF）
- 原子写、0600 权限、JSON 损坏自动回退、mtime 热更新（多 worker 同款机制）
- 管理端接口 401 / 403 / 200、变更写审计、审计里不含密钥
- list_models / test_connection 用假客户端覆盖（零网络），异常里的密钥必须先被抹掉
- 关键回归：改配置后不重启进程，下一个请求就用新配置（这是本功能的立身之本）

运行：python -m unittest test_llm_config -v
（本模块与其它模块各自独立跑，因为它会重定向 *_DIR 到临时目录）
"""
import json
import os
import sys
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-llm-config")
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

_SUPER_EMAIL = "super_llm@example.com"
auth.ADMIN_EMAILS = {_SUPER_EMAIL}

import llm  # noqa: E402
import system_config  # noqa: E402
import app as backend  # noqa: E402

backend.config.CAPTCHA_ENABLED = False

_URL = "/api/v1/admin/system/llm"
_GOOD_KEY = "sk-unit-test-abcdef123456"
_GOOD_URL = "https://api.example.com/v1"
_ENV_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LL_MODEL")


class _FakeOpenAI:
    """假 OpenAI 客户端：记录构造参数，并按需返回或抛错。"""

    built = []
    error = None
    model_ids = ["deepseek-chat", "deepseek-reasoner", "deepseek-chat"]
    reply = "pong"

    def __init__(self, api_key=None, base_url=None, timeout=None):
        _FakeOpenAI.built.append({"api_key": api_key, "base_url": base_url, "timeout": timeout})
        self.models = SimpleNamespace(list=self._list)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _list(self):
        if _FakeOpenAI.error:
            raise _FakeOpenAI.error
        return SimpleNamespace(data=[SimpleNamespace(id=item) for item in _FakeOpenAI.model_ids])

    def _create(self, **_kwargs):
        if _FakeOpenAI.error:
            raise _FakeOpenAI.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_FakeOpenAI.reply))]
        )

    @classmethod
    def reset(cls):
        cls.built = []
        cls.error = None


def _clear_dir(path):
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        try:
            os.remove(os.path.join(path, name))
        except OSError:
            pass


def _ops_text() -> str:
    """把审计文件拼成一段文本，用于断言"审计里不该出现的东西没出现"。"""
    ops_dir = getattr(config, "ADMIN_OPS_DIR", os.path.join(_TMP, "admin_ops"))
    chunks = []
    if os.path.isdir(ops_dir):
        for name in sorted(os.listdir(ops_dir)):
            try:
                with open(os.path.join(ops_dir, name), encoding="utf-8") as fh:
                    chunks.append(fh.read())
            except OSError:
                continue
    return "\n".join(chunks)


def _write_raw(payload) -> str:
    """绕过模块校验直接落盘，用于模拟脏数据/外部写入。"""
    path = system_config._path(system_config.NAME_LLM)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))
    system_config._CACHE.pop(path, None)
    return path


class LlmConfigUnitTests(unittest.TestCase):
    """纯模块行为：优先级、掩码、校验、落盘、热更新。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: getattr(config, key) for key in _ENV_KEYS}

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env_backup.items():
            setattr(config, key, value)

    def setUp(self):
        _clear_dir(os.path.join(_TMP, "system_config"))
        system_config._CACHE.clear()
        os.environ.pop("LLM_ALLOW_PRIVATE_BASE_URL", None)
        for key in _ENV_KEYS:
            setattr(config, key, "")
        config.LLM_BASE_URL = ""
        llm._client = None
        llm._client_fingerprint = ""
        _FakeOpenAI.reset()

    # ── 优先级 ────────────────────────────────────────────────────────────
    def test_resolve_priority_none_env_ui(self):
        resolved = system_config.resolve_llm()
        self.assertFalse(resolved["ready"])
        self.assertEqual(resolved["source"], "none")

        config.LLM_API_KEY = "env-key"
        config.LLM_BASE_URL = "https://env.example.com"
        config.LL_MODEL = "env-model"
        resolved = system_config.resolve_llm()
        self.assertTrue(resolved["ready"])
        self.assertEqual(resolved["source"], "env")
        self.assertEqual(resolved["api_key"], "env-key")

        system_config.save_llm(
            {"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "ui-model"}, "su")
        resolved = system_config.resolve_llm()
        self.assertEqual(resolved["source"], "ui")
        self.assertEqual(resolved["api_key"], _GOOD_KEY)
        self.assertEqual(resolved["model"], "ui-model")

    def test_disabled_ui_config_falls_back_to_env(self):
        config.LLM_API_KEY = "env-key"
        config.LLM_BASE_URL = "https://env.example.com"
        system_config.save_llm(
            {"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "ui-model", "enabled": False}, "su")
        resolved = system_config.resolve_llm()
        self.assertEqual(resolved["source"], "env")
        self.assertEqual(resolved["api_key"], "env-key")

    # ── 掩码与保密 ────────────────────────────────────────────────────────
    def test_public_view_never_exposes_key(self):
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        public = system_config.public_llm()
        self.assertTrue(public["api_key_set"])
        self.assertEqual(public["api_key_mask"], "•" * 8)
        self.assertNotIn(_GOOD_KEY, json.dumps(public, ensure_ascii=False))
        self.assertNotIn(_GOOD_KEY[-6:], json.dumps(public, ensure_ascii=False))

    def test_save_keeps_existing_key_when_blank(self):
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        system_config.save_llm({"api_key": "", "model": "m2"}, "su")
        resolved = system_config.resolve_llm()
        self.assertEqual(resolved["api_key"], _GOOD_KEY)
        self.assertEqual(resolved["model"], "m2")

    def test_first_save_without_key_is_rejected(self):
        with self.assertRaises(system_config.ConfigError):
            system_config.save_llm({"base_url": _GOOD_URL, "model": "m"}, "su")

    # ── base_url 白名单（防 SSRF）─────────────────────────────────────────
    def test_rejects_insecure_schemes_and_userinfo(self):
        for bad in ("http://api.example.com", "ftp://api.example.com",
                    "https://user:pass@api.example.com", "https://", "api.example.com"):
            with self.subTest(base_url=bad):
                with self.assertRaises(system_config.ConfigError):
                    system_config.validate_base_url(bad)

    def test_rejects_internal_and_metadata_addresses(self):
        for bad in ("https://127.0.0.1", "https://localhost/v1", "https://10.0.0.5",
                    "https://192.168.1.10:8080", "https://169.254.169.254/latest/meta-data",
                    "https://[::1]:9000", "https://0.0.0.0"):
            with self.subTest(base_url=bad):
                with self.assertRaises(system_config.ConfigError):
                    system_config.validate_base_url(bad)

    def test_allows_http_private_only_with_dev_flag(self):
        with mock.patch.dict(os.environ, {"LLM_ALLOW_PRIVATE_BASE_URL": "1"}):
            self.assertEqual(
                system_config.validate_base_url("http://127.0.0.1:9002/v1"),
                "http://127.0.0.1:9002/v1")
        with self.assertRaises(system_config.ConfigError):
            system_config.validate_base_url("http://127.0.0.1:9002/v1")

    def test_normalizes_trailing_slash(self):
        self.assertEqual(
            system_config.validate_base_url("https://api.example.com/v1/"),
            "https://api.example.com/v1")

    # ── 字段校验 ─────────────────────────────────────────────────────────
    def test_validates_timeout_model_and_key(self):
        base = {"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}
        for bad_timeout in (0, 3601, "abc", None):
            with self.subTest(timeout=bad_timeout):
                payload = dict(base, timeout=bad_timeout)
                with self.assertRaises(system_config.ConfigError):
                    system_config.save_llm(payload, "su")
        with self.assertRaises(system_config.ConfigError):
            system_config.save_llm(dict(base, model="  "), "su")
        with self.assertRaises(system_config.ConfigError):
            system_config.save_llm(dict(base, api_key="sk-x\r\nInjected: 1"), "su")
        with self.assertRaises(system_config.ConfigError):
            system_config.save_llm(dict(base, api_key="k" * 5000), "su")
        saved = system_config.save_llm(dict(base, timeout=60), "su")
        self.assertEqual(saved["timeout"], 60)

    def test_save_rejects_non_dict(self):
        with self.assertRaises(system_config.ConfigError):
            system_config.save_llm(["nope"], "su")

    # ── 落盘与热更新 ─────────────────────────────────────────────────────
    def test_file_mode_is_0600(self):
        if os.name != "posix":
            self.skipTest("Windows 不支持 POSIX 权限位，线上是 Linux")
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        path = system_config._path(system_config.NAME_LLM)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_corrupt_json_falls_back_without_crash(self):
        _write_raw("{ 不是合法 json")
        resolved = system_config.resolve_llm()
        self.assertFalse(resolved["ready"])
        self.assertEqual(resolved["source"], "none")

    def test_mtime_hot_reload(self):
        """外部直接改文件（模拟另一台 worker 或运维手改），下一次读取就是新值。"""
        _write_raw({"api_key": "key-a", "base_url": _GOOD_URL, "model": "m-a", "enabled": True})
        self.assertEqual(system_config.resolve_llm()["api_key"], "key-a")
        _write_raw({"api_key": "key-b", "base_url": _GOOD_URL, "model": "m-b", "enabled": True})
        self.assertEqual(system_config.resolve_llm()["api_key"], "key-b")

    def test_clear_returns_to_env(self):
        config.LLM_API_KEY = "env-key"
        config.LLM_BASE_URL = "https://env.example.com"
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        self.assertEqual(system_config.resolve_llm()["source"], "ui")
        system_config.clear_llm("su")
        self.assertEqual(system_config.resolve_llm()["source"], "env")
        system_config.clear_llm("su")  # 幂等

    def test_store_models_dedupes_caps_and_stamps(self):
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        public = system_config.store_models(["a", "a", " b ", "", "c" * 300], "su")
        self.assertEqual(public["models"], ["a", "b"])
        self.assertTrue(public["models_fetched_at"])
        self.assertEqual(system_config.public_llm()["models"], ["a", "b"])

    # ── 缓存指纹：配置一变，结果缓存必须失效 ─────────────────────────────
    def test_fingerprint_follows_server_default(self):
        config.LLM_API_KEY = "env-key"
        config.LLM_BASE_URL = "https://env.example.com"
        config.LL_MODEL = "env-model"
        first = llm.model_config_fingerprint(None)
        system_config.save_llm(
            {"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "ui-model"}, "su")
        second = llm.model_config_fingerprint(None)
        self.assertNotEqual(first, second)
        system_config.save_llm({"model": "ui-model-2"}, "su")
        self.assertNotEqual(second, llm.model_config_fingerprint(None))

    # ── list_models / test_connection（假客户端，零网络）──────────────────
    def test_list_models_parses_dedupes_and_passes_credentials(self):
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            result = llm.list_models(api_key="tmp-key", base_url=_GOOD_URL)
        self.assertEqual(result["models"], ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(_FakeOpenAI.built[-1]["api_key"], "tmp-key")
        self.assertEqual(_FakeOpenAI.built[-1]["base_url"], _GOOD_URL)

    def test_test_connection_reports_success(self):
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            result = llm.test_connection(api_key="tmp-key", base_url=_GOOD_URL, model="m")
        self.assertEqual(result["model"], "m")
        self.assertEqual(result["reply_chars"], len("pong"))
        self.assertIn("连接正常", result["message"])

    def test_connection_redacts_key_from_provider_error(self):
        _FakeOpenAI.error = RuntimeError(f"401 invalid api key {_GOOD_KEY}")
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            with self.assertRaises(RuntimeError) as ctx:
                llm.test_connection(api_key=_GOOD_KEY, base_url=_GOOD_URL, model="m")
        message = str(ctx.exception)
        self.assertNotIn(_GOOD_KEY, message)
        self.assertIn("***", message)

    def test_no_outbound_call_when_config_missing(self):
        _FakeOpenAI.error = None
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            with self.assertRaises(RuntimeError):
                llm.list_models()
        self.assertEqual(_FakeOpenAI.built, [])


class LlmAdminApiTests(unittest.TestCase):
    """管理端接口：鉴权、保存、测试、取模型、清空、审计。"""

    @classmethod
    def setUpClass(cls):
        _clear_dir(os.path.join(_TMP, "users"))
        _clear_dir(os.path.join(_TMP, "admin_ops"))
        sup, _ = auth.create_user("super_llm", "password123", _SUPER_EMAIL)
        cls.sup_token = auth.generate_jwt(sup["user_id"], sup["username"])
        normal, _ = auth.create_user(
            "plain_llm", "password123", f"plain_llm_{uuid.uuid4().hex[:6]}@example.com")
        cls.normal_token = auth.generate_jwt(normal["user_id"], normal["username"])
        cls.client = backend.app.test_client()

    def setUp(self):
        _clear_dir(os.path.join(_TMP, "system_config"))
        _clear_dir(os.path.join(_TMP, "admin_ops"))
        system_config._CACHE.clear()
        os.environ.pop("LLM_ALLOW_PRIVATE_BASE_URL", None)
        for key in _ENV_KEYS:
            setattr(config, key, "")
        config.LLM_BASE_URL = ""
        llm._client = None
        llm._client_fingerprint = ""
        _FakeOpenAI.reset()

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token}"} if token else {}

    def test_requires_super_admin(self):
        self.assertEqual(self.client.get(_URL).status_code, 401)
        self.assertEqual(
            self.client.get(_URL, headers=self._headers(self.normal_token)).status_code, 403)
        self.assertEqual(
            self.client.put(_URL, json={"model": "m"},
                            headers=self._headers(self.normal_token)).status_code, 403)
        self.assertEqual(
            self.client.post(f"{_URL}/test", json={},
                             headers=self._headers(self.normal_token)).status_code, 403)
        self.assertEqual(
            self.client.post(f"{_URL}/models", json={},
                             headers=self._headers(self.normal_token)).status_code, 403)
        self.assertEqual(
            self.client.delete(_URL, headers=self._headers(self.normal_token)).status_code, 403)
        self.assertEqual(
            self.client.get(_URL, headers=self._headers(self.sup_token)).status_code, 200)

    def test_put_saves_masks_and_writes_audit_without_key(self):
        resp = self.client.put(_URL, json={
            "api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "deepseek-chat", "timeout": 120,
        }, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        data = body["data"]
        self.assertEqual(data["source"], "ui")
        self.assertTrue(data["ready"])
        self.assertEqual(data["api_key_mask"], "•" * 8)
        self.assertNotIn(_GOOD_KEY, json.dumps(body, ensure_ascii=False))
        # 真密钥确实落盘并生效
        self.assertEqual(system_config.resolve_llm()["api_key"], _GOOD_KEY)
        # 审计：有操作记录，但绝不能出现密钥
        ops = _ops_text()
        self.assertIn("system_llm_update", ops)
        self.assertNotIn(_GOOD_KEY, ops)
        self.assertNotIn(_GOOD_KEY[-6:], ops)

    def test_put_rejects_ssrf_base_url(self):
        resp = self.client.put(_URL, json={
            "api_key": _GOOD_KEY, "base_url": "http://169.254.169.254/v1", "model": "m",
        }, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(system_config.resolve_llm()["ready"])

    def test_test_endpoint_reports_success_and_failure(self):
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            ok = self.client.post(f"{_URL}/test", json={
                "api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "deepseek-chat",
            }, headers=self._headers(self.sup_token))
        self.assertEqual(ok.status_code, 200)
        self.assertIn("连接正常", ok.get_json()["data"]["message"])

        _FakeOpenAI.error = RuntimeError(f"401 auth fails {_GOOD_KEY}")
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            bad = self.client.post(f"{_URL}/test", json={
                "api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "deepseek-chat",
            }, headers=self._headers(self.sup_token))
        self.assertEqual(bad.status_code, 422)
        detail = bad.get_json()["detail"]
        self.assertIn("测试失败", detail)
        self.assertNotIn(_GOOD_KEY, detail)
        self.assertNotIn(_GOOD_KEY, _ops_text())

    def test_models_endpoint_persists_list(self):
        system_config.save_llm({"api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m"}, "su")
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            resp = self.client.post(f"{_URL}/models", json={},
                                    headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()["data"]
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["models"], ["deepseek-chat", "deepseek-reasoner"])
        # 落盘缓存：刷新页面下拉框还在
        self.assertEqual(system_config.public_llm()["models"],
                         ["deepseek-chat", "deepseek-reasoner"])
        self.assertTrue(system_config.public_llm()["models_fetched_at"])

    def test_models_endpoint_blocks_ssrf_before_any_outbound_call(self):
        resp = self.client.post(f"{_URL}/models", json={
            "api_key": _GOOD_KEY, "base_url": "https://10.0.0.5/v1",
        }, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(_FakeOpenAI.built, [])  # 一个出网客户端都没建

    def test_delete_clears_and_falls_back_to_env(self):
        config.LLM_API_KEY = "env-key"
        config.LLM_BASE_URL = "https://env.example.com"
        self.client.put(_URL, json={
            "api_key": _GOOD_KEY, "base_url": _GOOD_URL, "model": "m",
        }, headers=self._headers(self.sup_token))
        resp = self.client.delete(_URL, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["source"], "env")
        self.assertIn("system_llm_clear", _ops_text())

    def test_saved_key_applies_without_restart(self):
        """硬指标：保存后不重启进程，下一个请求就用新配置。"""
        self.client.put(_URL, json={
            "api_key": "key-old-000000", "base_url": _GOOD_URL, "model": "m-old",
        }, headers=self._headers(self.sup_token))
        server = llm._server_settings()
        self.assertEqual(server["api_key"], "key-old-000000")
        self.assertEqual(server["model"], "m-old")

        resp = self.client.put(_URL, json={
            "api_key": "key-new-111111", "base_url": _GOOD_URL, "model": "m-new",
        }, headers=self._headers(self.sup_token))
        self.assertEqual(resp.status_code, 200)

        # 同一进程、没有重启：解析结果与即将使用的客户端都必须是新值
        server = llm._server_settings()
        self.assertEqual(server["api_key"], "key-new-111111")
        self.assertEqual(server["model"], "m-new")
        with mock.patch.object(llm, "OpenAI", _FakeOpenAI):
            llm.call_llm("hi")
        self.assertEqual(_FakeOpenAI.built[-1]["api_key"], "key-new-111111")


if __name__ == "__main__":
    unittest.main(verbosity=2)
