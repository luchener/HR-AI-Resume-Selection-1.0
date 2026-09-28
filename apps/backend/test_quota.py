# -*- coding: utf-8 -*-
"""每日使用次数配额 测试（unittest，零 SMTP 依赖，mock 邮件发送）。"""
import json
import os
import shutil
import uuid
import unittest
from unittest import mock

import app as backend
import config as backend_config
import quota as quota_mod
import auth as auth_mod


class QuotaApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.app.config["TESTING"] = True
        backend_config.CAPTCHA_ENABLED = False
        _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp")
        os.makedirs(_base, exist_ok=True)
        cls._tmp = os.path.join(_base, "quota-" + uuid.uuid4().hex[:10])
        os.makedirs(cls._tmp, exist_ok=True)
        # 全量隔离：auth/config 里的路径都是 import 期快照，必须两边同时覆盖，
        # 否则测试会在真实 data/ 下创建 quotaadmin*/quotauser* 账号（历史遗留污染）。
        cls._old = {"backend_config": {}, "auth_mod": {}}
        cls._old["backend_config"]["QUOTA_LIMITS_PATH"] = backend_config.QUOTA_LIMITS_PATH
        backend_config.QUOTA_LIMITS_PATH = os.path.join(cls._tmp, "limits.json")
        _dirs = {
            "USER_USAGE_DIR": "usage",
            "RATE_LIMITS_DIR": "rate_limits",
            "USERS_DIR": "users",
            "RESETS_DIR": "resets",
            "ADMIN_OPS_DIR": "admin_ops",
        }
        for _key, _sub in _dirs.items():
            _path = os.path.join(cls._tmp, _sub)
            if hasattr(backend_config, _key):
                cls._old["backend_config"][_key] = getattr(backend_config, _key)
                setattr(backend_config, _key, _path)
            if hasattr(auth_mod, _key):
                cls._old["auth_mod"][_key] = getattr(auth_mod, _key)
                setattr(auth_mod, _key, _path)
            os.makedirs(_path, exist_ok=True)
        os.makedirs(os.path.dirname(backend_config.QUOTA_LIMITS_PATH), exist_ok=True)
        cls.client = backend.app.test_client()
        suf = uuid.uuid4().hex[:6]
        cls.admin_name = "quotaadmin" + suf
        cls.user_name = "quotauser" + suf
        ok, err = auth_mod.create_user(cls.admin_name, "Admin@12345", cls.admin_name + "@test.local")
        assert ok, "create admin failed: %s" % err
        auth_mod.update_user_admin(cls.admin_name, True, operator="test")
        ok, err = auth_mod.create_user(cls.user_name, "User@12345", cls.user_name + "@test.local")
        assert ok, "create user failed: %s" % err
        resp = cls.client.post("/api/v1/auth/login", json={"username": cls.admin_name, "password": "Admin@12345"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cls.admin_token = resp.get_json()["data"]["token"]
        resp = cls.client.post("/api/v1/auth/login", json={"username": cls.user_name, "password": "User@12345"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cls.user_token = resp.get_json()["data"]["token"]
        cls.user_rec = auth_mod.find_user_by_username(cls.user_name)
        cls.admin_rec = auth_mod.find_user_by_username(cls.admin_name)

    @classmethod
    def tearDownClass(cls):
        for key, val in cls._old["backend_config"].items():
            setattr(backend_config, key, val)
        for key, val in cls._old["auth_mod"].items():
            setattr(auth_mod, key, val)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # 每个测试独立：重置配额文件 + 该用户 usage，避免跨用例污染
        if os.path.exists(backend_config.QUOTA_LIMITS_PATH):
            os.remove(backend_config.QUOTA_LIMITS_PATH)
        up = os.path.join(backend_config.USER_USAGE_DIR, f"{self.user_rec['user_id']}.json")
        if os.path.exists(up):
            os.remove(up)

    def _h(self, token):
        return {"Authorization": "Bearer " + token}

    def _enable_global(self, default_daily=3, email=False):
        resp = self.client.put("/api/v1/admin/quota/settings", json={
            "quota_enabled": True, "default_daily": default_daily, "email_notify_enabled": email,
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def _succeed_analyses(self, user_id, n):
        for _ in range(n):
            auth_mod.record_user_usage(user_id, "analysis")

    # ── 默认不限（零影响上线）────────────────────────────
    def test_default_unlimited(self):
        self.assertIsNone(quota_mod.check_analysis_quota(self.user_rec["user_id"], 3))
        data = quota_mod.get_my_quota(self.user_rec["user_id"])
        self.assertTrue(data["unlimited"])

    # ── 全局限额拦截 ──────────────────────────────────
    def test_global_limit_blocks(self):
        self._enable_global(default_daily=3)
        uid = self.user_rec["user_id"]
        self.assertIsNone(quota_mod.check_analysis_quota(uid, 3))
        # 批量不足明细：used=2 → remaining=1
        self._succeed_analyses(uid, 2)
        err = quota_mod.check_analysis_quota(uid, 2)
        self.assertEqual(err[1], 429)
        self.assertIn("剩余 1", err[0])
        # 用尽：used=3
        self._succeed_analyses(uid, 1)
        err = quota_mod.check_analysis_quota(uid, 1)
        self.assertIsNotNone(err)
        self.assertEqual(err[1], 429)
        self.assertIn("已用完", err[0])

    # ── 管理员豁免额度（但仍受禁用开关约束）──────────────
    def test_admin_exempt_from_limit(self):
        self._enable_global(default_daily=1)
        aid = self.admin_rec["user_id"]
        self._succeed_analyses(aid, 5)
        self.assertIsNone(quota_mod.check_analysis_quota(aid, 3))
        # 但管理员仍可被账号级禁用
        quota_mod.set_user_quota(self.admin_name, {"analysis_enabled": False})
        err = quota_mod.check_analysis_quota(aid, 1)
        self.assertEqual(err[1], 403)
        self.assertIn("已被禁用", err[0])

    # ── 逐账号覆盖 / 0 禁用 / -1 不限 ──────────────────
    def test_per_user_override_and_bounds(self):
        self._enable_global(default_daily=10)
        uid = self.user_rec["user_id"]
        quota_mod.set_user_quota(self.user_name, {"daily_limit": 1})
        self.assertIsNone(quota_mod.check_analysis_quota(uid, 1))
        # 校验不消耗次数：记满 1 次后再拦截
        self._succeed_analyses(uid, 1)
        err = quota_mod.check_analysis_quota(uid, 1)
        self.assertIsNotNone(err)
        self.assertEqual(err[1], 429)
        # 0 = 禁用（剩余 0 起步）
        quota_mod.set_user_quota(self.user_name, {"daily_limit": 0})
        self.assertIsNotNone(quota_mod.check_analysis_quota(uid, 1))
        # -1 = 不限
        quota_mod.set_user_quota(self.user_name, {"daily_limit": -1})
        self.assertIsNone(quota_mod.check_analysis_quota(uid, 3))

    # ── 全局限额开关：关 = 全部不限（含账号级配置）─────────
    def test_global_switch_off_overrides_everything(self):
        self._enable_global(default_daily=1)
        quota_mod.set_user_quota(self.user_name, {"daily_limit": 1, "analysis_enabled": True})
        resp = self.client.put("/api/v1/admin/quota/settings", json={"quota_enabled": False}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        # 账号级限额失效
        self._succeed_analyses(self.user_rec["user_id"], 5)
        self.assertIsNone(quota_mod.check_analysis_quota(self.user_rec["user_id"], 3))
        # 但账号禁用开关仍生效
        quota_mod.set_user_quota(self.user_name, {"analysis_enabled": False})
        err = quota_mod.check_analysis_quota(self.user_rec["user_id"], 1)
        self.assertEqual(err[1], 403)

    # ── 管理端接口 + 审计 ──────────────────────────────
    def test_admin_settings_api_and_audit(self):
        resp = self.client.put("/api/v1/admin/quota/settings", json={"default_daily": "abc"}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422)
        resp = self.client.put("/api/v1/admin/quota/settings", json={"default_daily": 5000}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422)
        self._enable_global(default_daily=5, email=True)
        resp = self.client.get("/api/v1/admin/quota/settings", headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()["data"]
        self.assertTrue(d["quota_enabled"])
        self.assertEqual(d["default_daily"], 5)
        self.assertTrue(d["email_notify_enabled"])
        # 设置单账号
        resp = self.client.put("/api/v1/admin/quota/users/%s" % self.user_name, json={"daily_limit": 2, "analysis_enabled": True}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        target = next(u for u in resp.get_json()["data"] if u["username"] == self.user_name) if isinstance(resp.get_json()["data"], list) else resp.get_json()["data"]
        self.assertEqual(target["daily_limit"], 2)
        # 不存在用户
        resp = self.client.put("/api/v1/admin/quota/users/nobody" + uuid.uuid4().hex[:4], json={"daily_limit": 1}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 404)
        # 重置
        resp = self.client.delete("/api/v1/admin/quota/users/%s" % self.user_name, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/v1/admin/quota/settings", headers=self._h(self.admin_token))
        self.assertFalse(any(u["username"] == self.user_name for u in resp.get_json()["data"]["users"]))
        # 审计
        ops = auth_mod.list_admin_ops(op="quota_setting_update", page=1, size=20)
        self.assertTrue(any("enabled=True" in (o.get("detail") or "") for o in ops))
        ops = auth_mod.list_admin_ops(op="quota_user_update", page=1, size=20)
        self.assertTrue(any(self.user_name in (o.get("target_username") or "") for o in ops))

    # ── 用户端 me ────────────────────────────────────
    def test_me_endpoint(self):
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["data"]["unlimited"])
        self._enable_global(default_daily=3)
        quota_mod.set_user_quota(self.user_name, {"daily_limit": 3})
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.user_token))
        d = resp.get_json()["data"]
        self.assertFalse(d["unlimited"])
        self.assertEqual(d["daily_limit"], 3)
        self.assertEqual(d["remaining"], 3)
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.admin_token))
        self.assertTrue(resp.get_json()["data"]["is_admin"])

    # ── hr_analysis 入口：额度拦截先于 AI 调用 ───────────
    def test_hr_analysis_blocked_by_quota(self):
        self._enable_global(default_daily=0)
        resp = self.client.post("/api/v1/resumes/hr-analysis", json={"resume_id": "r-missing", "job_id": "j-missing"}, headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 429, resp.get_data(as_text=True))
        self.assertIn("已用完", resp.get_json()["detail"])

    # ── 邮件提醒：有邮箱 + 开关 → 每天一封；无邮箱跳过 ─────
    def test_email_notify_dedup_and_no_email(self):
        self._enable_global(default_daily=0, email=True)
        with mock.patch("mailer.send_broadcast_email", return_value=None) as send:
            err = quota_mod.check_analysis_quota(self.user_rec["user_id"], 1)
            self.assertEqual(err[1], 429)
            send.assert_called_once()
            # 去重：再次拦截不再发
            quota_mod.check_analysis_quota(self.user_rec["user_id"], 1)
            send.assert_called_once()
        # 无邮箱账号：建一个不带邮箱的用户
        nope = "quotanomail" + uuid.uuid4().hex[:6]
        ok, err = auth_mod.create_user(nope, "User@12345", "")
        assert ok, err
        rec = auth_mod.find_user_by_username(nope)
        with mock.patch("mailer.send_broadcast_email", return_value=None) as send2:
            err = quota_mod.check_analysis_quota(rec["user_id"], 1)
            self.assertEqual(err[1], 429)
            send2.assert_not_called()

    # ── 权限：普通用户不可访问管理接口 ───────────────────
    def test_quota_admin_requires_admin(self):
        resp = self.client.get("/api/v1/admin/quota/settings", headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 403)


    # ── 回归：逐账号 -1「显式不限」必须覆盖全局默认 ──────────
    def test_per_account_unlimited_overrides_global_default(self):
        self._enable_global(default_daily=3)
        uid = self.user_rec["user_id"]
        self._succeed_analyses(uid, 10)
        # 未覆盖：受全局默认 3 次约束（已用 10 → 拦截）
        err = quota_mod.check_analysis_quota(uid, 1)
        self.assertIsNotNone(err)
        self.assertEqual(err[1], 429)
        # 显式 -1：真正不限（历史 bug：-1 被折叠成 None → 回退全局默认）
        quota_mod.set_user_quota(self.user_name, {"daily_limit": -1})
        self.assertIsNone(quota_mod.check_analysis_quota(uid, 3))
        cfg = quota_mod._load()["users"][self.user_name]
        self.assertEqual(cfg["daily_limit"], -1)
        # me 视图同步
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.user_token))
        self.assertTrue(resp.get_json()["data"]["unlimited"])

    # ── 回归：管理员显式配置限额后必须生效（未配置则豁免）──────
    def test_admin_explicit_limit_applies(self):
        self._enable_global(default_daily=1)
        aid = self.admin_rec["user_id"]
        admin_usage = os.path.join(backend_config.USER_USAGE_DIR, "%s.json" % aid)
        if os.path.exists(admin_usage):
            os.remove(admin_usage)
        # 未显式配置：管理员豁免（全局默认 1 不适用）
        self._succeed_analyses(aid, 5)
        self.assertIsNone(quota_mod.check_analysis_quota(aid, 3))
        # 显式配置 2 次：管理员同样受约束（清空历史计数后从 0 起算）
        os.remove(admin_usage)
        quota_mod.set_user_quota(self.admin_name, {"daily_limit": 2})
        self.assertIsNone(quota_mod.check_analysis_quota(aid, 1))
        err = quota_mod.check_analysis_quota(aid, 5)
        self.assertIsNotNone(err)
        self.assertEqual(err[1], 429)
        # me 视图：管理员 + 显式限额 → 不再 unlimited
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.admin_token))
        d = resp.get_json()["data"]
        self.assertFalse(d["unlimited"])
        self.assertEqual(d["daily_limit"], 2)

    # ── 回归：/quota/me 必须反映账号禁用（不受全局开关/管理员影响）──
    def test_me_reflects_disabled_regardless_of_global_switch(self):
        # 全局配额开关保持关闭（默认值）
        quota_mod.set_user_quota(self.user_name, {"analysis_enabled": False})
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["data"]["analysis_enabled"])
        # 管理员被禁用时同样要反映（前端据此弹窗，而不是静默失败）
        quota_mod.set_user_quota(self.admin_name, {"analysis_enabled": False})
        resp = self.client.get("/api/v1/quota/me", headers=self._h(self.admin_token))
        self.assertFalse(resp.get_json()["data"]["analysis_enabled"])
        # hr-analysis 入口：被禁用 → 403 且可读原因
        resp = self.client.post("/api/v1/resumes/hr-analysis", json={"resume_id": "r-missing", "job_id": "j-missing"}, headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertIn("已被禁用", resp.get_json()["detail"])

    # ── 回归：管理端明细带 raw_daily_limit（编辑弹窗回显用）────
    def test_settings_exposes_raw_daily_limit(self):
        self._enable_global(default_daily=7)
        quota_mod.set_user_quota(self.user_name, {"analysis_enabled": True})
        resp = self.client.get("/api/v1/admin/quota/settings", headers=self._h(self.admin_token))
        row = next(u for u in resp.get_json()["data"]["users"] if u["username"] == self.user_name)
        self.assertIsNone(row["raw_daily_limit"])
        self.assertFalse(row["custom"])
        self.assertEqual(row["daily_limit"], 7)
        quota_mod.set_user_quota(self.user_name, {"daily_limit": -1})
        resp = self.client.get("/api/v1/admin/quota/settings", headers=self._h(self.admin_token))
        row = next(u for u in resp.get_json()["data"]["users"] if u["username"] == self.user_name)
        self.assertEqual(row["raw_daily_limit"], -1)
        self.assertTrue(row["custom"])
        self.assertEqual(row["daily_limit"], -1)
    # ── 回归：缺少 Content-Type（浏览器 text/plain）必须报错而不是静默成功 ──
    def test_missing_json_content_type_is_rejected(self):
        url = "/api/v1/admin/quota/users/%s" % self.user_name
        # 模拟前端漏写 Content-Type：fetch(string) 发的是 text/plain
        resp = self.client.put(url, data=json.dumps({"daily_limit": 2, "analysis_enabled": False}),
                               content_type="text/plain;charset=UTF-8", headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422, resp.get_data(as_text=True))
        # 关键：不能留下"只有 updated_at"的空配置（历史 bug 的痕迹）
        cfg = quota_mod._load()["users"].get(self.user_name) or {}
        self.assertNotIn("daily_limit", cfg)
        self.assertNotIn("analysis_enabled", cfg)
        # 全局设置同理
        resp = self.client.put("/api/v1/admin/quota/settings", data=json.dumps({"quota_enabled": True}),
                               content_type="text/plain;charset=UTF-8", headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(quota_mod._load()["quota_enabled"])
        # 带正确 Content-Type 的同一请求必须真正落盘
        resp = self.client.put(url, json={"daily_limit": 2, "analysis_enabled": False}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        cfg = quota_mod._load()["users"][self.user_name]
        self.assertEqual(cfg["daily_limit"], 2)
        self.assertFalse(cfg["analysis_enabled"])
        # 空 payload 直接调用也要报错
        with self.assertRaises(ValueError):
            quota_mod.set_user_quota(self.user_name, {})
        with self.assertRaises(ValueError):
            quota_mod.update_settings({})
        # 单个字段也算合法（页面上的即时开关就只发一个字段）
        resp = self.client.put(url, json={"analysis_enabled": True}, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(quota_mod._load()["users"][self.user_name]["analysis_enabled"])
if __name__ == "__main__":
    unittest.main()
