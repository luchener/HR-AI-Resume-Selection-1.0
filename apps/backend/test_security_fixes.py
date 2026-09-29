# -*- coding: utf-8 -*-
"""本轮安全修复的回归测试（test_security_fixes.py）。

覆盖：
1. 提权链：白名单邮箱不可分配给普通账号（原 PoC 全链失效）
2. /resumes/improve 纳入配额（修复前可无限白嫖 LLM）
3. 配额原子预占 + 失败回滚
4. X-Forwarded-For 只信任可信跳数（防伪造绕过限流）
5. 岗位批量上传严格校验（防"字符串按字符切分"写出海量文件）
6. _UserLock 嵌套不在内层提前解锁
7. 验证码接口限流
8. 简历/岗位删除接口（级联归档入回收站）
9. 生产环境必须显式配置 ADMIN_EMAILS

运行：python -m unittest test_security_fixes -v
"""
import json
import os
import shutil
import unittest
import uuid
from unittest import mock

# ── 必须在 import app 之前重定向数据目录（否则会写真实 data/）──────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp", "sec-" + uuid.uuid4().hex[:8])
os.environ["ENV"] = "local"
os.makedirs(_TMP, exist_ok=True)

import config as C  # noqa: E402

C.DATA_DIR = _TMP
for _name in [n for n in dir(C) if n.endswith("_DIR") and n not in ("BASE_DIR", "DATA_DIR", "LOG_DIR")]:
    _path = os.path.join(_TMP, _name.lower())
    os.makedirs(_path, exist_ok=True)
    setattr(C, _name, _path)
C.QUOTA_LIMITS_PATH = os.path.join(C.QUOTA_DIR, "limits.json")
C.CAPTCHA_ENABLED = False
C.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(C.LOG_DIR, exist_ok=True)

import app as backend  # noqa: E402
import auth as auth_mod  # noqa: E402
import quota as quota_mod  # noqa: E402
import store as store_mod  # noqa: E402


def _limits(**kw):
    data = {"quota_enabled": False, "default_daily": None, "email_notify_enabled": False, "users": {}}
    data.update(kw)
    with open(C.QUOTA_LIMITS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


class SecurityFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.app.config["TESTING"] = True
        cls.client = backend.app.test_client()
        suf = uuid.uuid4().hex[:6]
        cls.whitelist_email = (sorted(C.ADMIN_EMAILS) or ["boss@example.com"])[0]

        sup, err = auth_mod.create_user("sup_" + suf, "Super1pass", cls.whitelist_email)
        assert sup, err
        cls.super_token = auth_mod.generate_jwt(sup["user_id"], sup["username"])

        adm, err = auth_mod.create_user("adm_" + suf, "Admin1pass", "adm_%s@example.com" % suf)
        assert adm, err
        auth_mod.update_user_admin(adm["username"], True, operator="test")
        cls.admin_token = auth_mod.generate_jwt(adm["user_id"], adm["username"])

        vic, err = auth_mod.create_user("vic_" + suf, "Victim1pass", "vic_%s@example.com" % suf)
        assert vic, err
        cls.victim_user = vic
        cls.victim_token = auth_mod.generate_jwt(vic["user_id"], vic["username"])

        other, err = auth_mod.create_user("oth_" + suf, "Other1pass", "oth_%s@example.com" % suf)
        assert other, err
        cls.other_user = other
        cls.other_token = auth_mod.generate_jwt(other["user_id"], other["username"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def hdr(self, token):
        return {"Authorization": "Bearer " + token}

    # ── 1. 提权链 ────────────────────────────────────────────────────
    def test_whitelist_email_cannot_be_assigned(self):
        """普通管理员把普通用户邮箱改成白名单地址 → 必须 422，且不获得超管身份。"""
        resp = self.client.patch(
            "/api/v1/admin/users/" + self.victim_user["username"],
            json={"email": self.whitelist_email},
            headers=self.hdr(self.admin_token),
        )
        self.assertEqual(resp.status_code, 422, resp.get_data(as_text=True))
        refreshed = auth_mod.find_user_by_username(self.victim_user["username"])
        self.assertNotIn((refreshed.get("email") or "").lower(), C.ADMIN_EMAILS)
        self.assertFalse(auth_mod.is_super_admin(refreshed))

    def test_full_privilege_escalation_chain_blocked(self):
        """原 PoC 全链：重置密码 → 改白名单邮箱 → 登录 → 越权删号。第②步必须失败。"""
        target = auth_mod.create_user(
            "chain_" + uuid.uuid4().hex[:6], "Chain1pass", "chain_%s@example.com" % uuid.uuid4().hex[:6]
        )[0]
        # ① 普通管理员重置密码（这一步仍然允许：普通用户不在保护范围）
        reset = self.client.post(
            "/api/v1/admin/users/%s/reset-password" % target["username"],
            headers=self.hdr(self.admin_token),
        )
        self.assertEqual(reset.status_code, 200)
        temp = reset.get_json()["data"]["temp_password"]
        # ② 改邮箱为白名单 → 被拦
        moved = self.client.patch(
            "/api/v1/admin/users/%s" % target["username"],
            json={"email": self.whitelist_email},
            headers=self.hdr(self.admin_token),
        )
        self.assertEqual(moved.status_code, 422)
        # ③ 用临时密码登录仍然只是普通用户
        login = self.client.post(
            "/api/v1/auth/login", json={"username": target["username"], "password": temp}
        )
        self.assertEqual(login.status_code, 200)
        victim_token = login.get_json()["data"]["token"]
        created = auth_mod.find_user_by_username(target["username"])
        self.assertFalse(auth_mod.is_super_admin(created))
        # ④ 越权删号（仅超管）→ 403
        deleted = self.client.delete(
            "/api/v1/admin/users/" + self.other_user["username"],
            json={"admin_password": temp},
            headers={"Authorization": "Bearer " + victim_token},
        )
        self.assertEqual(deleted.status_code, 403)

    # ── 2/3. 配额 ────────────────────────────────────────────────────
    def test_improve_endpoint_counts_against_quota(self):
        """/resumes/improve 必须走配额：额度用尽 → 429 且不调用 LLM。"""
        resume_id = store_mod.save_resume("张三 Python 工程师", {}, self.victim_user["user_id"])
        job_id = store_mod.save_job(resume_id, "招聘 Python 工程师", {}, self.victim_user["user_id"])
        _limits(quota_enabled=True, users={self.victim_user["username"]: {"daily_limit": 0}})
        try:
            with mock.patch.object(backend.llm, "call_llm", side_effect=AssertionError("LLM 不该被调用")):
                resp = self.client.post(
                    "/api/v1/resumes/improve",
                    json={"resume_id": resume_id, "job_id": job_id},
                    headers=self.hdr(self.victim_token),
                )
            self.assertEqual(resp.status_code, 429, resp.get_data(as_text=True))
        finally:
            _limits()

    def test_improve_endpoint_records_usage(self):
        resume_id = store_mod.save_resume("李四 Java 工程师", {}, self.victim_user["user_id"])
        job_id = store_mod.save_job(resume_id, "招聘 Java 工程师", {}, self.victim_user["user_id"])
        _limits(quota_enabled=True, users={self.victim_user["username"]: {"daily_limit": 5}})
        before = quota_mod.today_analysis_count(self.victim_user["user_id"])
        try:
            with mock.patch.object(backend.llm, "call_llm", return_value="分析文本"):
                resp = self.client.post(
                    "/api/v1/resumes/improve",
                    json={"resume_id": resume_id, "job_id": job_id},
                    headers=self.hdr(self.victim_token),
                )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        finally:
            _limits()
        self.assertEqual(quota_mod.today_analysis_count(self.victim_user["user_id"]), before + 1)

    def test_reserve_is_atomic_and_releasable(self):
        """预占：同一账号连续两次预占，第二次因额度用尽被拒；回滚后可再次预占。"""
        uid = self.victim_user["user_id"]
        _limits(quota_enabled=True, users={self.victim_user["username"]: {"daily_limit": 1}})
        try:
            base_used = quota_mod.today_analysis_count(uid)
            _limits(quota_enabled=True, users={self.victim_user["username"]: {"daily_limit": base_used + 1}})
            self.assertIsNone(quota_mod.reserve_analysis(uid, 1))          # 占满
            self.assertIsNotNone(quota_mod.reserve_analysis(uid, 1))       # 超额被拦
            quota_mod.release_analysis(uid, 1)                             # 回滚
            self.assertIsNone(quota_mod.reserve_analysis(uid, 1))          # 又能占了
            quota_mod.release_analysis(uid, 1)
        finally:
            _limits()

    # ── 4. X-Forwarded-For ──────────────────────────────────────────
    def test_client_ip_uses_rightmost_trusted_hop(self):
        saved = C.TRUSTED_PROXY_COUNT
        try:
            C.TRUSTED_PROXY_COUNT = 1
            with backend.app.test_request_context(
                headers={"X-Forwarded-For": "9.9.9.9, 1.2.3.4"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            ):
                # 客户端伪造的 9.9.9.9 在最左边，nginx 追加的真实地址在右边
                self.assertEqual(backend._client_ip(), "1.2.3.4")

            C.TRUSTED_PROXY_COUNT = 1
            with backend.app.test_request_context(
                headers={"X-Forwarded-For": "not-an-ip"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            ):
                self.assertEqual(backend._client_ip(), "10.0.0.1")  # 畸形输入一律退回

            C.TRUSTED_PROXY_COUNT = 0
            with backend.app.test_request_context(
                headers={"X-Forwarded-For": "9.9.9.9"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            ):
                self.assertEqual(backend._client_ip(), "10.0.0.1")  # 不信任任何代理

            C.TRUSTED_PROXY_COUNT = 2
            with backend.app.test_request_context(
                headers={"X-Forwarded-For": "9.9.9.9, 1.2.3.4, 10.0.0.1"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            ):
                self.assertEqual(backend._client_ip(), "1.2.3.4")
        finally:
            C.TRUSTED_PROXY_COUNT = saved

    def test_spoofed_xff_does_not_bypass_login_rate_limit(self):
        """伪造 XFF 不能换来新的限流身份（限流按真实 IP 哈希）。"""
        C.TRUSTED_PROXY_COUNT = 1
        h1 = auth_mod._ip_hash("1.2.3.4")
        h2 = auth_mod._ip_hash("9.9.9.9")
        self.assertNotEqual(h1, h2)
        with backend.app.test_request_context(
            headers={"X-Forwarded-For": "9.9.9.9, 1.2.3.4"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        ):
            self.assertEqual(auth_mod._ip_hash(backend._client_ip()), h1)

    # ── 5. 上传校验 ──────────────────────────────────────────────────
    def test_job_upload_rejects_non_array(self):
        resume_id = store_mod.save_resume("王五 测试", {}, self.victim_user["user_id"])
        before = len(os.listdir(store_mod.JOBS_DIR))
        resp = self.client.post(
            "/api/v1/jobs/upload",
            json={"resume_id": resume_id, "job_descriptions": "A" * 200},
            headers=self.hdr(self.victim_token),
        )
        self.assertEqual(resp.status_code, 422, resp.get_data(as_text=True))
        # 关键：一个都不许落盘（修复前 200 字符会生成 200 个岗位文件）
        self.assertEqual(len(os.listdir(store_mod.JOBS_DIR)), before)

    def test_job_upload_rejects_bad_items(self):
        resume_id = store_mod.save_resume("赵六 测试", {}, self.victim_user["user_id"])
        before = len(os.listdir(store_mod.JOBS_DIR))
        for payload in (["ok", 123], [""], ["   "], ["x" * 20001], ["a"] * 51):
            resp = self.client.post(
                "/api/v1/jobs/upload",
                json={"resume_id": resume_id, "job_descriptions": payload},
                headers=self.hdr(self.victim_token),
            )
            self.assertEqual(resp.status_code, 422, repr(payload)[:40])
        self.assertEqual(len(os.listdir(store_mod.JOBS_DIR)), before)

    def test_job_upload_accepts_normal_array(self):
        resume_id = store_mod.save_resume("孙七 测试", {}, self.victim_user["user_id"])
        resp = self.client.post(
            "/api/v1/jobs/upload",
            json={"resume_id": resume_id, "job_descriptions": ["后端工程师", " 测试工程师 "]},
            headers=self.hdr(self.victim_token),
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(len(resp.get_json()["job_id"]), 2)

    # ── 6. 锁重入 ────────────────────────────────────────────────────
    def test_nested_user_lock_does_not_release_early(self):
        calls = []

        class FakeFcntl:
            LOCK_EX = 1
            LOCK_UN = 2

            @staticmethod
            def flock(fd, op):
                calls.append(op)

        saved_fcntl = auth_mod.fcntl
        saved_fds = dict(auth_mod._UNIX_LOCK_FDS)
        saved_depth = dict(auth_mod._UNIX_LOCK_DEPTH)
        auth_mod.fcntl = FakeFcntl
        auth_mod._UNIX_LOCK_FDS.clear()
        auth_mod._UNIX_LOCK_DEPTH.clear()
        try:
            with auth_mod._UserLock():
                with auth_mod._UserLock():
                    pass
                # 内层已退出：此时绝不允许放开锁，否则外层临界区形同裸奔
                self.assertEqual(calls.count(FakeFcntl.LOCK_UN), 0)
            self.assertEqual(calls.count(FakeFcntl.LOCK_UN), 1)
            self.assertEqual(calls.count(FakeFcntl.LOCK_EX), 2)
        finally:
            for fd in auth_mod._UNIX_LOCK_FDS.values():
                try:
                    fd.close()
                except Exception:
                    pass
            auth_mod._UNIX_LOCK_FDS.clear()
            auth_mod._UNIX_LOCK_FDS.update(saved_fds)
            auth_mod._UNIX_LOCK_DEPTH.clear()
            auth_mod._UNIX_LOCK_DEPTH.update(saved_depth)
            auth_mod.fcntl = saved_fcntl

    # ── 7. 验证码限流 ────────────────────────────────────────────────
    def test_captcha_endpoint_is_rate_limited(self):
        saved = C.CAPTCHA_ENABLED
        C.CAPTCHA_ENABLED = True
        for name in os.listdir(auth_mod.RATE_LIMITS_DIR):
            try:
                os.remove(os.path.join(auth_mod.RATE_LIMITS_DIR, name))
            except OSError:
                pass
        try:
            codes = [
                self.client.get("/api/v1/auth/captcha", environ_base={"REMOTE_ADDR": "7.7.7.7"}).status_code
                for _ in range(61)
            ]
            self.assertEqual(codes[0], 200)
            self.assertEqual(codes[59], 200)
            self.assertEqual(codes[60], 429)
        finally:
            C.CAPTCHA_ENABLED = saved

    # ── 8. 删除接口 ──────────────────────────────────────────────────
    def test_delete_resume_moves_archives_to_trash(self):
        rid = store_mod.save_resume("周八 删除测试", {}, self.victim_user["user_id"])
        jid = store_mod.save_job(rid, "招聘删除测试岗", {}, self.victim_user["user_id"])
        aid = store_mod.save_archive(
            user_id=self.victim_user["user_id"],
            resume_id=rid,
            job_id=jid,
            candidate_name="周八",
            final_score=80,
            fit_tag="匹配",
            recruitment_recommendation="推荐",
            job_title="删除测试岗",
        )
        resp = self.client.delete("/api/v1/resumes/" + rid, headers=self.hdr(self.victim_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["data"]["archived_to_trash"], 1)
        self.assertFalse(os.path.exists(os.path.join(store_mod.RESUMES_DIR, rid + ".json")))
        archive = store_mod.get_archive(aid, self.victim_user["user_id"])
        self.assertEqual(archive.get("status"), "trashed")
        # 再删一次 → 404
        again = self.client.delete("/api/v1/resumes/" + rid, headers=self.hdr(self.victim_token))
        self.assertEqual(again.status_code, 404)

    def test_delete_job_and_cross_user_isolation(self):
        rid = store_mod.save_resume("吴九 删除测试", {}, self.victim_user["user_id"])
        jid = store_mod.save_job(rid, "招聘跨用户测试", {}, self.victim_user["user_id"])
        # 别人的岗位删不掉
        denied = self.client.delete("/api/v1/jobs/" + jid, headers=self.hdr(self.other_token))
        self.assertEqual(denied.status_code, 404)
        allowed = self.client.delete("/api/v1/jobs/" + jid, headers=self.hdr(self.victim_token))
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(store_mod.JOBS_DIR, jid + ".json")))

    # ── 9. 生产环境配置校验 ──────────────────────────────────────────
    def test_production_requires_explicit_admin_emails(self):
        saved_env = os.environ.get("ADMIN_EMAILS")
        saved_prod = C.ENV
        try:
            C.ENV = "production"
            os.environ.pop("ADMIN_EMAILS", None)
            with self.assertRaises(SystemExit):
                C.check_production()
            os.environ["ADMIN_EMAILS"] = "ops@example.com"
            C.check_production()  # 显式配置后放行
        finally:
            C.ENV = saved_prod
            if saved_env is None:
                os.environ.pop("ADMIN_EMAILS", None)
            else:
                os.environ["ADMIN_EMAILS"] = saved_env


if __name__ == "__main__":
    unittest.main(verbosity=2)
