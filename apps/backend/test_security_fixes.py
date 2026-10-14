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
from testsupport import isolated_data_dirs  # noqa: E402

# 整体接管数据目录（root 沿用 _TMP）：只改 config 自己的属性不够，
# auth/store/app 在 import 时各自快照过目录常量，整套跑时会指向别处。
_DIRS = isolated_data_dirs("security_fixes", root=_TMP)
C.CAPTCHA_ENABLED = False


def setUpModule():
    """用例运行时重新接管：前面模块的 tearDownModule 会把路径还原成真实 data/。"""
    _DIRS.reapply()


def tearDownModule():
    """还原目录常量，避免把本模块的临时布局留给后续模块。"""
    _DIRS.restore()


import app as backend  # noqa: E402
import auth as auth_mod  # noqa: E402
import quota as quota_mod  # noqa: E402
import safe_fs  # noqa: E402
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


class MissingDataDirectoryTests(unittest.TestCase):
    """数据目录不存在时，“读用户列表”必须是空结果而不是 500。

    现场：首次部署还没建数据目录、备份恢复时目录被清空、测试隔离指向临时目录……
    os.listdir 直接抛 FileNotFoundError，管理员用户列表页整页报错。
    """

    def _missing(self):
        return os.path.join(_TMP, "no-such-users-dir-" + uuid.uuid4().hex[:8])

    def test_list_user_files_tolerates_missing_directory(self):
        with mock.patch.object(auth_mod, "USERS_DIR", self._missing()):
            self.assertEqual(auth_mod._list_user_files(), [])

    def test_user_listing_survives_missing_directory(self):
        missing = self._missing()
        with mock.patch.object(auth_mod, "USERS_DIR", missing), \
                mock.patch.object(auth_mod, "_INDEX_FILE", os.path.join(missing, "_index.json")):
            self.assertEqual(auth_mod.count_users(), 0)
            self.assertEqual(auth_mod.list_users(), [])


class SafeListdirTests(unittest.TestCase):
    """目录缺失/无权限时必须返回空结果：读列表的调用点全后端有 20 多处。"""

    def _missing(self):
        return os.path.join(_TMP, "no-such-dir-" + uuid.uuid4().hex[:8])

    def test_missing_directory_returns_empty(self):
        missing = self._missing()

        self.assertEqual(safe_fs.listdir(missing), [])
        self.assertEqual(safe_fs.list_files(missing, suffix=".json"), [])
        self.assertFalse(safe_fs.isdir(missing))

    def test_existing_directory_still_lists_sorted(self):
        directory = os.path.join(_TMP, "listing-" + uuid.uuid4().hex[:8])
        os.makedirs(directory, exist_ok=True)
        for name in ("b.json", "a.json", "c.txt"):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write("{}")

        self.assertEqual(safe_fs.list_files(directory, suffix=".json"), ["a.json", "b.json"])
        self.assertEqual(sorted(safe_fs.listdir(directory)), ["a.json", "b.json", "c.txt"])

    def test_file_instead_of_directory_returns_empty(self):
        """路径指向文件时也不能抛异常。"""
        target = os.path.join(_TMP, "not-a-dir-" + uuid.uuid4().hex[:8])
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")

        self.assertEqual(safe_fs.listdir(target), [])

    def test_no_backend_module_lists_directories_unsafely(self):
        """回归防线：后世新增代码不许绕过 safe_fs 直接裸用 os.listdir。"""
        backend_dir = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for name in sorted(os.listdir(backend_dir)):
            if not name.endswith(".py") or name.startswith("test_") or name in {"safe_fs.py", "testsupport.py"}:
                continue
            with open(os.path.join(backend_dir, name), "r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if "os.listdir(" in line and "return os.listdir" not in line:
                        offenders.append(f"{name}:{number}")

        self.assertEqual(offenders, [], f"这些地方仍在裸用 os.listdir：{offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
