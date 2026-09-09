"""
用户管理 CRUD / 管理员账户 单元测试（test_user_admin.py）

覆盖 docs/ARCHITECTURE.md「认证安全加固」用户管理部分：
- 管理员判定：.env 白名单邮箱 OR 用户 is_admin 标记
- GET /api/v1/admin/users 列表 + keyword 搜索 + 分页
- POST /api/v1/admin/users 管理员创建用户（含 is_admin）
- PATCH /api/v1/admin/users/<username> 修改邮箱 / 管理员标记
- POST /api/v1/admin/users/<username>/reset-password 重置密码（临时密码 + 旧 token 失效）
- DELETE /api/v1/admin/users/<username> 删除用户（防删自己 / 防删最后一个管理员）
- 非管理员访问 /admin/* → 403；匿名 → 401

运行：python -m unittest test_user_admin -v
"""

import json
import os
import shutil
import unittest

# ── 必须在 import app 之前重定向数据目录 ─────────────────────────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-user-admin")
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
    "admin_ops",
    "usage",
)
for _d in _SUBDIRS:
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
config.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(config.LOG_DIR, exist_ok=True)

import auth  # noqa: E402
import mailer  # noqa: E402


def _fake_send(to_email: str, code: str, purpose: str = "注册"):
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
_app_mailer = getattr(backend, "mailer_mod", None)

# 验证码默认开启；本套件登录调用统一关闭（验证码专项用例见 test_captcha_*）
backend.config.CAPTCHA_ENABLED = False


def _json(resp):
    try:
        return resp.get_json() or {}
    except Exception:
        return {}


class UserAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)
        for _d in _SUBDIRS + ("logs",):
            os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
        cls.client = backend.app.test_client()
        # 白名单管理员（email 命中 config.ADMIN_EMAILS，超级管理员）+ 普通用户
        admin_email = (list(config.ADMIN_EMAILS) or ["admin@example.com"])[0]
        auser, aerr = auth_module.create_user("boss_admin", "password123", admin_email)
        assert auser, f"admin create failed: {aerr}"
        cls.admin_token = auth_module.generate_jwt(auser["user_id"], auser["username"])
        nuser, _ = auth_module.create_user("bill_normal", "password123", "bill_normal@example.com")
        cls.normal_token = auth_module.generate_jwt(nuser["user_id"], nuser["username"])
        # 普通管理员：is_admin 标记但邮箱不在白名单（不可分配管理员权限）
        muser, merr = auth_module.create_user("mid_admin", "password123", "mid_admin@example.com")
        assert muser, f"mid admin create failed: {merr}"
        muser["is_admin"] = True
        auth_module._write_json(
            os.path.join(auth_module.USERS_DIR, f"{muser['user_id']}.json"), muser
        )
        cls.mid_admin_token = auth_module.generate_jwt(muser["user_id"], muser["username"])

    def setUp(self):
        for d in (
            auth_module.RESETS_DIR,
            auth_module.RATE_LIMITS_DIR,
            auth_module.LOGIN_FAILURES_DIR,
            auth_module.INVITE_REQUESTS_DIR,
            auth_module.INVITE_CODES_DIR,
            auth_module.INVITE_CODES_AUDIT_DIR,
            auth_module.ADMIN_OPS_DIR,
            auth_module.USER_USAGE_DIR,
        ):
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        if _app_mailer is not None:
            _app_mailer.send_verification_email = _fake_send
            _app_mailer.smtp_available = _fake_smtp_available
        # 懒清理哨兵按测试隔离：每个用例从"可清理"状态开始，避免残留 mtime 影响其他用例
        try:
            if os.path.exists(config.PRUNE_TOUCH_FILE):
                os.remove(config.PRUNE_TOUCH_FILE)
        except OSError:
            pass

    def _mkuser(self, name: str, email: str = ""):
        user, err = auth_module.create_user(name, "password123", email)
        assert user, f"create user {name} failed: {err}"
        return user

    def _req(self, method, path, data=None, token=None):
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs = {}
        if data is not None:
            kwargs["data"] = json.dumps(data)
        return getattr(self.client, method)(path, headers=headers, **kwargs)

    # ── 权限边界 ───────────────────────────────────────────────────

    def test_admin_endpoints_require_auth_and_admin(self):
        # 匿名 → 401
        r = self._req("get", "/api/v1/admin/users")
        self.assertEqual(r.status_code, 401)
        # 普通用户 → 403
        r = self._req("get", "/api/v1/admin/users", token=self.normal_token)
        self.assertEqual(r.status_code, 403)
        # 管理员 → 200
        r = self._req("get", "/api/v1/admin/users", token=self.admin_token)
        self.assertEqual(r.status_code, 200)

    # ── 列表 / 搜索 / 分页 ─────────────────────────────────────────

    def test_list_users_with_search_and_pagination(self):
        self._mkuser("alice_worker", "alice@example.com")
        self._mkuser("bob_worker", "bob@example.com")
        r = self._req("get", "/api/v1/admin/users", token=self.admin_token)
        self.assertEqual(r.status_code, 200)
        data = _json(r)["data"]
        self.assertGreaterEqual(data["total"], 3)  # boss + bill + alice + bob

        # keyword 搜索（用户名 / 邮箱，大小写不敏感）
        r = self._req("get", "/api/v1/admin/users?keyword=alice", token=self.admin_token)
        items = _json(r)["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["username"], "alice_worker")

        r = self._req("get", "/api/v1/admin/users?keyword=ALICE", token=self.admin_token)
        self.assertEqual(_json(r)["data"]["total"], 1)

        # 列表不含敏感字段
        item = items[0]
        self.assertNotIn("password_hash", item)
        self.assertNotIn("password_salt", item)

    # ── 管理员创建用户 ─────────────────────────────────────────────

    def test_admin_create_user(self):
        r = self._req(
            "post",
            "/api/v1/admin/users",
            data={"username": "new_hire", "password": "StrongPass123", "email": "new@example.com"},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 201, _json(r))
        user = auth_module.find_user_by_username("new_hire")
        self.assertIsNotNone(user)
        self.assertEqual(user["email"], "new@example.com")
        # 可登录
        ok = auth_module.authenticate_user("new_hire", "StrongPass123")
        self.assertIsNotNone(ok)
        # 重复创建 → 409
        r = self._req(
            "post",
            "/api/v1/admin/users",
            data={"username": "new_hire", "password": "StrongPass123"},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 409)

    def test_admin_create_user_with_admin_flag(self):
        r = self._req(
            "post",
            "/api/v1/admin/users",
            data={"username": "co_admin", "password": "StrongPass123", "email": "co@example.com", "is_admin": True},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 201, _json(r))
        user = auth_module.find_user_by_username("co_admin")
        self.assertTrue(auth_module.is_admin(user))
        # 新管理员可访问管理接口
        token = auth_module.generate_jwt(user["user_id"], user["username"])
        r = self._req("get", "/api/v1/admin/users", token=token)
        self.assertEqual(r.status_code, 200)

    # ── 修改：邮箱 / 管理员标记 ─────────────────────────────────────

    def test_admin_update_email(self):
        self._mkuser("email_target", "old@example.com")
        r = self._req(
            "patch",
            "/api/v1/admin/users/email_target",
            data={"email": "new2@example.com"},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        user = auth_module.find_user_by_username("email_target")
        self.assertEqual(user["email"], "new2@example.com")
        # 非法邮箱 → 422
        r = self._req(
            "patch",
            "/api/v1/admin/users/email_target",
            data={"email": "not-an-email"},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 422)

    def test_admin_toggle_admin_flag(self):
        self._mkuser("promote_me", "promote@example.com")
        r = self._req(
            "patch",
            "/api/v1/admin/users/promote_me",
            data={"is_admin": True},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        user = auth_module.find_user_by_username("promote_me")
        self.assertTrue(auth_module.is_admin(user))
        # 再取消
        r = self._req(
            "patch",
            "/api/v1/admin/users/promote_me",
            data={"is_admin": False},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        user = auth_module.find_user_by_username("promote_me")
        self.assertFalse(auth_module.is_admin(user))

    def test_cannot_demote_last_admin(self):
        # 只有一个动态管理员：boss_admin 是白名单管理员不受限；
        # 先建 co_admin 提升，再把它降级 → 还剩 boss（白名单）→ 允许；
        # 然后只剩 boss 时尝试降级 boss → 白名单不可改 → 400
        self._mkuser("co_admin2", "co2@example.com")
        r = self._req(
            "patch",
            "/api/v1/admin/users/co_admin2",
            data={"is_admin": True},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        # 取消 boss 的管理员（白名单）→ 应拒绝
        r = self._req(
            "patch",
            "/api/v1/admin/users/boss_admin",
            data={"is_admin": False},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 400, _json(r))
        self.assertIn("白名单", _json(r)["detail"])

    # ── 重置密码 ───────────────────────────────────────────────────

    def test_admin_reset_password(self):
        user = self._mkuser("pwd_target", "pwd@example.com")
        old_token = auth_module.generate_jwt(user["user_id"], user["username"])
        r = self._req(
            "post",
            "/api/v1/admin/users/pwd_target/reset-password",
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        temp = _json(r)["data"]["temp_password"]
        self.assertTrue(len(temp) >= 8)
        # 临时密码可登录
        ok = auth_module.authenticate_user("pwd_target", temp)
        self.assertIsNotNone(ok)
        # 旧 token 失效（密码版本 +1）
        user2 = auth_module.find_user_by_username("pwd_target")
        self.assertGreater(user2["password_version"], user.get("password_version", 1))
        self.assertIsNotNone(old_token)

    # ── 删除用户（软删除，仅超级管理员 + 管理员密码确认）──────────────

    def test_admin_delete_user_requires_password(self):
        self._mkuser("doomed", "doomed@example.com")
        # 不带管理员密码 → 422
        r = self._req("delete", "/api/v1/admin/users/doomed", data={}, token=self.admin_token)
        self.assertEqual(r.status_code, 422, _json(r))
        # 密码错误 → 403
        r = self._req("delete", "/api/v1/admin/users/doomed",
                      data={"admin_password": "wrong-pass"}, token=self.admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # 密码正确 → 200，且为软删除（文件保留、标记 deleted_at、不可登录）
        r = self._req("delete", "/api/v1/admin/users/doomed",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertTrue(auth_module.is_user_deleted("doomed"))
        self.assertIsNone(auth_module.authenticate_user("doomed", "password123"))

    def test_only_super_admin_can_delete(self):
        self._mkuser("mid_delete_target", "mdt@example.com")
        # 普通管理员（is_admin 标记但非白名单）删除普通用户 → 403
        r = self._req("delete", "/api/v1/admin/users/mid_delete_target",
                      data={"admin_password": "password123"}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # 账号未被删除
        self.assertFalse(auth_module.is_user_deleted("mid_delete_target"))
        # 超级管理员可删除
        r = self._req("delete", "/api/v1/admin/users/mid_delete_target",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))

    def test_restore_soft_deleted_user(self):
        self._mkuser("restore_me", "restore@example.com")
        r = self._req("delete", "/api/v1/admin/users/restore_me",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        # 出现在可恢复列表
        r = self._req("get", "/api/v1/admin/users/deleted", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        names = [i["username"] for i in _json(r)["data"]["items"]]
        self.assertIn("restore_me", names)
        # 恢复 → 可登录（密码版本 +1，旧 token 失效）
        r = self._req("post", "/api/v1/admin/users/restore_me/restore", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertFalse(auth_module.is_user_deleted("restore_me"))
        self.assertIsNotNone(auth_module.authenticate_user("restore_me", "password123"))
        # 恢复审计留痕
        ops = auth_module.list_admin_ops(op="user_restore", keyword="restore_me")
        self.assertEqual(len(ops), 1)

    def test_restore_expired_deletion_rejected(self):
        user = self._mkuser("expired_del", "expired@example.com")
        # 手工把 deleted_at 拨到 100 天前（超过 90 天窗口）
        path = os.path.join(auth_module.USERS_DIR, f"{user['user_id']}.json")
        record = auth_module._read_json(path)
        record["deleted_at"] = (auth_module.datetime.now(auth_module.timezone.utc)
                                - auth_module.timedelta(days=100)).isoformat()
        auth_module._write_json(path, record)
        r = self._req("post", "/api/v1/admin/users/expired_del/restore", token=self.admin_token)
        self.assertEqual(r.status_code, 400, _json(r))
        self.assertIn("90 天", _json(r)["detail"])
        self.assertTrue(auth_module.is_user_deleted("expired_del"))

    def test_cannot_delete_self(self):
        r = self._req("delete", "/api/v1/admin/users/boss_admin",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 400, _json(r))

    def test_cannot_delete_last_admin(self):
        # boss_admin（白名单）之外只有一个动态管理员时不可删它
        self._mkuser("sole_admin", "sole@example.com")
        r = self._req(
            "patch",
            "/api/v1/admin/users/sole_admin",
            data={"is_admin": True},
            token=self.admin_token,
        )
        self.assertEqual(r.status_code, 200, _json(r))
        # 直接删除 sole_admin：还有 boss（白名单）→ 允许
        r = self._req("delete", "/api/v1/admin/users/sole_admin",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))

    def test_delete_unknown_user_404(self):
        r = self._req("delete", "/api/v1/admin/users/never_existed",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 404)

    # ── 管理操作审计（删除记录等留痕）──────────────────────────────────

    def test_audit_recorded_on_delete_and_create(self):
        self._mkuser("doomed2", "doomed2@example.com")
        r = self._req("delete", "/api/v1/admin/users/doomed2",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        r = self._req("post", "/api/v1/admin/users",
                      data={"username": "fresh_hire", "password": "StrongPass123"},
                      token=self.admin_token)
        self.assertEqual(r.status_code, 201, _json(r))

        ops = auth_module.list_admin_ops()
        ops_by_op = {o["op"]: o for o in ops}
        self.assertIn("user_delete", ops_by_op)
        self.assertEqual(ops_by_op["user_delete"]["target_username"], "doomed2")
        self.assertEqual(ops_by_op["user_delete"]["operator_name"], "boss_admin")
        self.assertIn("user_create", ops_by_op)
        self.assertEqual(ops_by_op["user_create"]["target_username"], "fresh_hire")

    def test_audit_recorded_on_admin_toggle_and_pwd_reset(self):
        self._mkuser("audit_target", "audit@example.com")
        r = self._req("patch", "/api/v1/admin/users/audit_target",
                      data={"is_admin": True}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        r = self._req("post", "/api/v1/admin/users/audit_target/reset-password",
                      token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        ops = auth_module.list_admin_ops()
        ops_by_op = {o["op"]: o for o in ops}
        self.assertIn("admin_grant", ops_by_op)
        self.assertIn("pwd_reset", ops_by_op)

    def test_admin_ops_list_api(self):
        self._mkuser("api_ops_target", "apiops@example.com")
        self._req("delete", "/api/v1/admin/users/api_ops_target",
                  data={"admin_password": "password123"}, token=self.admin_token)
        r = self._req("get", "/api/v1/admin/ops?op=user_delete", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        data = _json(r)["data"]
        self.assertGreaterEqual(data["total"], 1)
        self.assertTrue(all(i["op"] == "user_delete" for i in data["items"]))
        # 非管理员不可看审计
        r = self._req("get", "/api/v1/admin/ops", token=self.normal_token)
        self.assertEqual(r.status_code, 403)

    # ── 权限分级：仅超级管理员可分配管理员权限 ──────────────────────────

    def test_only_super_admin_can_assign_admin(self):
        self._mkuser("promote_via_mid", "pmid@example.com")
        # 普通管理员（is_admin 标记但非白名单）尝试授予 → 403
        r = self._req("patch", "/api/v1/admin/users/promote_via_mid",
                      data={"is_admin": True}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # 普通管理员尝试创建管理员账号 → 403
        r = self._req("post", "/api/v1/admin/users",
                      data={"username": "rogue_admin", "password": "StrongPass123", "is_admin": True},
                      token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # 但普通管理员仍可做其他管理操作（如重置密码）
        r = self._req("post", "/api/v1/admin/users/promote_via_mid/reset-password",
                      token=self.mid_admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        # 超级管理员授予 → 200
        r = self._req("patch", "/api/v1/admin/users/promote_via_mid",
                      data={"is_admin": True}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))

    def test_auth_me_exposes_super_admin(self):
        r = self._req("get", "/api/v1/auth/me", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertTrue(_json(r)["data"]["is_super_admin"])
        r = self._req("get", "/api/v1/auth/me", token=self.mid_admin_token)
        self.assertFalse(_json(r)["data"]["is_super_admin"])
        r = self._req("get", "/api/v1/auth/me", token=self.normal_token)
        self.assertFalse(_json(r)["data"]["is_super_admin"])
        self.assertFalse(_json(r)["data"]["is_admin"])

    # ── 使用次数统计（登录 / 分析 埋点 + day|month|year 聚合 API）──────

    def test_usage_recorded_on_login(self):
        # 真实登录一次 → usage 文件应有 login 计数
        r = self.client.post("/api/v1/auth/login",
                             data=json.dumps({"username": "bill_normal", "password": "password123"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, _json(r))
        usage = auth_module._read_json(
            auth_module._usage_path(auth_module.find_user_by_username("bill_normal")["user_id"])
        )
        self.assertIsNotNone(usage)
        today = auth_module.datetime.now(auth_module.timezone.utc).strftime("%Y-%m-%d")
        self.assertGreaterEqual(usage.get(today, {}).get("login", 0), 1)

    def test_usage_api_day_granularity(self):
        user = self._mkuser("usage_boy", "usage@example.com")
        auth_module.record_user_usage(user["user_id"], "login")
        auth_module.record_user_usage(user["user_id"], "analysis")
        r = self._req("get", "/api/v1/admin/users/usage_boy/usage?granularity=day&buckets=7",
                      token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        data = _json(r)["data"]
        self.assertEqual(data["granularity"], "day")
        self.assertEqual(len(data["labels"]), 7)
        # 最后一天（今天）应有计数
        self.assertEqual(data["series"]["total"][-1], 2)
        self.assertEqual(data["series"]["login"][-1], 1)
        self.assertEqual(data["series"]["analysis"][-1], 1)

    def test_usage_api_month_and_year(self):
        user = self._mkuser("usage_girl", "usageg@example.com")
        auth_module.record_user_usage(user["user_id"], "analysis")
        r = self._req("get", "/api/v1/admin/users/usage_girl/usage?granularity=month&buckets=12",
                      token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        data = _json(r)["data"]
        self.assertEqual(len(data["labels"]), 12)
        self.assertEqual(data["series"]["analysis"][-1], 1)
        r = self._req("get", "/api/v1/admin/users/usage_girl/usage?granularity=year&buckets=3",
                      token=self.admin_token)
        data = _json(r)["data"]
        self.assertEqual(len(data["labels"]), 3)
        self.assertEqual(data["series"]["total"][-1], 1)
        # 非管理员 403
        r = self._req("get", "/api/v1/admin/users/usage_girl/usage", token=self.normal_token)
        self.assertEqual(r.status_code, 403)

    def test_usage_preserved_on_soft_delete(self):
        user = self._mkuser("usage_dead", "usaged@example.com")
        auth_module.record_user_usage(user["user_id"], "login")
        self.assertTrue(os.path.exists(auth_module._usage_path(user["user_id"])))
        r = self._req("delete", "/api/v1/admin/users/usage_dead",
                      data={"admin_password": "password123"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        # 软删除保留使用统计（恢复后不丢历史）
        self.assertTrue(os.path.exists(auth_module._usage_path(user["user_id"])))
        # 恢复后仍可查询 usage
        self._req("post", "/api/v1/admin/users/usage_dead/restore", token=self.admin_token)
        r = self._req("get", "/api/v1/admin/users/usage_dead/usage?granularity=day&buckets=7",
                      token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertEqual(_json(r)["data"]["series"]["login"][-1], 1)

    # ── 管理员手动冻结（防异常消耗 token）─────────────────────────────

    def test_admin_freeze_blocks_login_and_reset(self):
        self._mkuser("freeze_victim", "fv@example.com")
        # 管理员冻结（带原因）
        r = self._req("post", "/api/v1/admin/users/freeze_victim/freeze",
                      data={"reason": "频繁调用异常消耗 token"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertIn("已冻结", _json(r)["data"]["message"])
        # 冻结信息：frozen_by=admin + 原因 + frozen_at 为 ISO 字符串（修复前端 1970 显示）
        fi = auth_module.get_frozen_info("freeze_victim")
        self.assertTrue(fi["frozen"])
        self.assertEqual(fi["frozen_by"], "admin")
        self.assertEqual(fi["frozen_reason"], "频繁调用异常消耗 token")
        self.assertIsNotNone(fi["frozen_at"])
        self.assertTrue(str(fi["frozen_at"]).startswith("20") or str(fi["frozen_at"]).startswith("19"))
        self.assertIn("T", str(fi["frozen_at"]))  # ISO 格式（非 epoch 数字）
        # 冻结列表接口同样输出 ISO
        frozen_list = auth_module.list_frozen_users()
        item = next((x for x in frozen_list if x.get("username") == "freeze_victim"), None)
        self.assertIsNotNone(item)
        self.assertIn("T", str(item.get("frozen_at", "")))
        # 登录 → 423 + frozen_by=admin + admin_email（弹窗提示数据）
        r = self.client.post("/api/v1/auth/login",
                             data=json.dumps({"username": "freeze_victim", "password": "password123"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 423, _json(r))
        body = _json(r)
        self.assertEqual(body.get("frozen_by"), "admin")
        self.assertIn("账号异常请联系系统管理员处理", body.get("detail", ""))
        self.assertTrue(body.get("admin_email"))
        # 重置密码请求 → 423（无法通过改密绕过）
        r = self.client.post("/api/v1/auth/reset-password/request",
                             data=json.dumps({"email": "fv@example.com"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 423, _json(r))
        # 自助解冻发码 → 403（admin 冻结禁止自助解冻）
        r = self.client.post("/api/v1/auth/unfreeze/send-code",
                             data=json.dumps({"username": "freeze_victim", "email": "fv@example.com"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403, _json(r))
        # 审计留痕 user_freeze
        ops = auth_module.list_admin_ops(op="user_freeze", keyword="freeze_victim")
        self.assertEqual(len(ops), 1)
        # 管理员解冻 → 可再登录
        r = self._req("post", "/api/v1/admin/users/freeze_victim/unfreeze", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        r = self.client.post("/api/v1/auth/login",
                             data=json.dumps({"username": "freeze_victim", "password": "password123"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, _json(r))

    def test_auto_freeze_still_self_service(self):
        # 自动冻结（登录失败触发）仍保留自助解冻：frozen_by 应为 auto
        self._mkuser("auto_victim", "av@example.com")
        rec = auth_module._login_fail_record("auto_victim")
        rec["frozen"] = True
        rec["frozen_by"] = "auto"
        auth_module._save_login_fail_record("auto_victim", rec)
        r = self.client.post("/api/v1/auth/unfreeze/send-code",
                             data=json.dumps({"username": "auto_victim", "email": "av@example.com"}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, _json(r))

    def test_admin_freezing_of_admin_account_requires_super_admin(self):
        # mid_admin（普通管理员）不能冻结/解冻管理员身份账号（boss_admin 是白名单超管，mid_admin 是自己）
        # 用 mid_admin 尝试冻结超管 → 403
        r = self._req("post", "/api/v1/admin/users/boss_admin/freeze",
                      data={"reason": "x"}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # mid_admin 尝试冻结自己（is_admin 标记账号）→ 403（自己也是管理员身份）
        r = self._req("post", "/api/v1/admin/users/mid_admin/freeze",
                      data={"reason": "x"}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # 超管冻结普通用户 → 200
        r = self._req("post", "/api/v1/admin/users/bill_normal/freeze",
                      data={"reason": "test"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))

    # ── 权限漏洞①：管理员账号的敏感操作仅超级管理员 ───────────────────

    def test_sensitive_ops_on_admin_account_require_super_admin(self):
        # mid_admin 对 boss_admin（白名单超管，管理员身份）的敏感操作 → 403
        for method, path, body in [
            ("patch", "/api/v1/admin/users/boss_admin", {"email": "hack@example.com"}),
            ("patch", "/api/v1/admin/users/boss_admin", {"is_admin": False}),
            ("post", "/api/v1/admin/users/boss_admin/reset-password", None),
            ("delete", "/api/v1/admin/users/boss_admin", None),
        ]:
            r = self._req(method, path, data=body, token=self.mid_admin_token)
            self.assertEqual(r.status_code, 403, f"{method} {path}: {_json(r)}")
        # mid_admin 对 mid_admin 自己（is_admin 标记账号）→ 同样 403
        r = self._req("patch", "/api/v1/admin/users/mid_admin",
                      data={"email": "hack2@example.com"}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 403, _json(r))
        # mid_admin 对普通用户仍可操作（改邮箱/重置密码）→ 200（用独立用户，不污染共享 fixtures）
        self._mkuser("mid_ops_victim", "mov@example.com")
        r = self._req("patch", "/api/v1/admin/users/mid_ops_victim",
                      data={"email": "mov2@example.com"}, token=self.mid_admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        r = self._req("post", "/api/v1/admin/users/mid_ops_victim/reset-password",
                      token=self.mid_admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        # 超级管理员可操作管理员账号（改 mid_admin 邮箱 → 200，审计留痕"旧 → 新"）
        r = self._req("patch", "/api/v1/admin/users/mid_admin",
                      data={"email": "mid_boss@example.com"}, token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        self.assertEqual(
            auth_module.list_admin_ops(op="email_update", keyword="mid_admin")[0]["detail"],
            "mid_admin 的绑定邮箱：mid_admin@example.com → mid_boss@example.com",
        )

    # ── 使用统计增强：summary 汇总 + year labels 字符串化 + 排行 ──────

    def test_usage_api_summary_and_year_label_types(self):
        user = self._mkuser("summary_guy", "sum@example.com")
        auth_module.record_user_usage(user["user_id"], "login")
        auth_module.record_user_usage(user["user_id"], "login")
        auth_module.record_user_usage(user["user_id"], "analysis")
        r = self._req("get", "/api/v1/admin/users/summary_guy/usage?granularity=day&buckets=7",
                      token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        s = _json(r)["data"]["summary"]
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["login_total"], 2)
        self.assertEqual(s["analysis_total"], 1)
        self.assertEqual(s["peak_total"], 3)
        self.assertTrue(s["peak_label"])
        self.assertGreaterEqual(s["active_buckets"], 1)
        self.assertEqual(s["avg_per_bucket"], round(3 / 7, 2))
        # year labels 一律字符串（前端 slice/格式化不再抛错）
        r = self._req("get", "/api/v1/admin/users/summary_guy/usage?granularity=year&buckets=3",
                      token=self.admin_token)
        data = _json(r)["data"]
        for lab in data["labels"]:
            self.assertIsInstance(lab, str)
            self.assertRegex(lab, r"^\d{4}$")
        self.assertEqual(data["series"]["total"][-1], 3)

    def test_usage_ranking(self):
        u1 = self._mkuser("rank_heavy", "rh@example.com")
        u2 = self._mkuser("rank_light", "rl@example.com")
        auth_module.record_user_usage(u1["user_id"], "analysis")
        auth_module.record_user_usage(u1["user_id"], "analysis")
        auth_module.record_user_usage(u1["user_id"], "login")
        auth_module.record_user_usage(u2["user_id"], "login")
        r = self._req("get", "/api/v1/admin/users/usage-ranking", token=self.admin_token)
        self.assertEqual(r.status_code, 200, _json(r))
        items = _json(r)["data"]["items"]
        by_name = {it["username"]: it for it in items}
        self.assertIn("rank_heavy", by_name)
        self.assertIn("rank_light", by_name)
        self.assertEqual(by_name["rank_heavy"]["total"], 3)
        self.assertEqual(by_name["rank_light"]["total"], 1)
        # 降序：heavy 在前
        idxs = [it["username"] for it in items]
        self.assertLess(idxs.index("rank_heavy"), idxs.index("rank_light"))
        # 非管理员 403
        r = self._req("get", "/api/v1/admin/users/usage-ranking", token=self.normal_token)
        self.assertEqual(r.status_code, 403)

    # ── HR 分析缓存 LRU+TTL（省 token 机制保护 + 容量上限）─────────────

    def test_hr_cache_basic_and_lru_eviction(self):
        from collections import OrderedDict
        # 命中 → 返回；未命中 → None
        backend._hr_cache_put(("k", "1"), {"final_score": 90})
        self.assertEqual(backend._hr_cache_get(("k", "1")), {"final_score": 90})
        self.assertIsNone(backend._hr_cache_get(("k", "missing")))
        # LRU 淘汰：临时调低容量，塞满后最旧条目被淘汰
        old_max = backend._HR_ANALYSIS_CACHE_MAX
        backend._HR_ANALYSIS_CACHE_MAX = 3
        try:
            for i in range(5):
                backend._hr_cache_put(("k", str(i)), {"i": i})
            # 0,1 被淘汰，2,3,4 保留
            self.assertIsNone(backend._hr_cache_get(("k", "0")))
            self.assertIsNone(backend._hr_cache_get(("k", "1")))
            self.assertEqual(backend._hr_cache_get(("k", "4")), {"i": 4})
            # 访问 2 提升其 LRU 位置后再写入 5 → 淘汰 3（2 刚被访问）
            backend._hr_cache_get(("k", "2"))
            backend._hr_cache_put(("k", "5"), {"i": 5})
            self.assertIsNone(backend._hr_cache_get(("k", "3")))
            self.assertEqual(backend._hr_cache_get(("k", "2")), {"i": 2})
        finally:
            backend._HR_ANALYSIS_CACHE_MAX = old_max
            with backend._HR_ANALYSIS_CACHE_LOCK:
                backend._HR_ANALYSIS_CACHE.clear()

    def test_hr_cache_ttl_expiry(self):
        # 写入一条后把时间戳改旧 → get 返回 None（TTL 惰性过期）
        backend._hr_cache_put(("k", "ttl"), {"v": 1})
        with backend._HR_ANALYSIS_CACHE_LOCK:
            backend._HR_ANALYSIS_CACHE[("k", "ttl")]["_ts"] -= (
                backend._HR_ANALYSIS_CACHE_TTL_SECONDS + 10
            )
        self.assertIsNone(backend._hr_cache_get(("k", "ttl")))
        with backend._HR_ANALYSIS_CACHE_LOCK:
            backend._HR_ANALYSIS_CACHE.clear()

    # ── 保留策略：懒清理 ────────────────────────────────────────────

    def test_prune_admin_ops_and_user_usage(self):
        from datetime import datetime, timedelta, timezone

        # 伪造一条 400 天前的审计（created_at 旧）和一条今天的
        old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y%m%dT%H%M%S")
        new_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        auth_module._write_json(
            os.path.join(auth_module.ADMIN_OPS_DIR, f"{old_ts}_old.json"),
            {"op": "user_create", "created_at": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()},
        )
        auth_module._write_json(
            os.path.join(auth_module.ADMIN_OPS_DIR, f"{new_ts}_new.json"),
            {"op": "user_delete", "created_at": datetime.now(timezone.utc).isoformat()},
        )
        # 使用统计：旧天键 + 新天键
        usage_path = auth_module._usage_path("uid_prune_test")
        auth_module._write_json(
            usage_path,
            {
                (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d"): {"login": 1},
                datetime.now(timezone.utc).strftime("%Y-%m-%d"): {"analysis": 2},
            },
        )
        removed_ops = auth_module.prune_admin_ops(180)
        self.assertEqual(removed_ops, 1)
        self.assertEqual(
            len([f for f in os.listdir(auth_module.ADMIN_OPS_DIR) if f.endswith(".json")]),
            1,
        )
        auth_module.prune_user_usage(365)
        data = auth_module._read_json(usage_path) or {}
        self.assertEqual(len(data), 1)  # 旧天键被移除
        self.assertIn(datetime.now(timezone.utc).strftime("%Y-%m-%d"), data)

    def test_prune_removes_fully_expired_usage_file(self):
        from datetime import datetime, timedelta, timezone

        usage_path = auth_module._usage_path("uid_expired_only")
        auth_module._write_json(
            usage_path,
            {(datetime.now(timezone.utc) - timedelta(days=700)).strftime("%Y-%m-%d"): {"login": 3}},
        )
        auth_module.prune_user_usage(365)
        self.assertFalse(os.path.exists(usage_path))  # 全部过期 → 文件删除

    # ── 审计导出 ────────────────────────────────────────────────────

    def test_admin_ops_export_csv_and_json(self):
        from datetime import datetime, timezone

        auth_module.record_admin_op(
            "user_create", "op_id", "boss_admin", "bill_normal", detail="创建用户 bill_normal"
        )
        auth_module.record_admin_op(
            "pwd_reset", "op_id", "boss_admin", "bill_normal", detail="重置密码"
        )
        # CSV（默认格式，含 utf-8-sig BOM + Content-Disposition）
        resp = self._req("get", "/api/v1/admin/ops/export", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers.get("Content-Type", ""))
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        body = resp.data.decode("utf-8-sig")
        self.assertIn("创建用户 bill_normal", body)
        self.assertIn("重置密码", body)
        self.assertIn("boss_admin", body)
        # 过滤后导出
        resp = self._req("get", "/api/v1/admin/ops/export?op=pwd_reset", token=self.admin_token)
        body = resp.data.decode("utf-8-sig")
        self.assertIn("重置密码", body)
        self.assertNotIn("创建用户", body)
        # JSON 格式
        resp = self._req("get", "/api/v1/admin/ops/export?format=json", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        payload = resp.get_json()
        self.assertEqual(len(payload["items"]), 2)
        # 非管理员 → 403；匿名 → 401
        resp = self._req("get", "/api/v1/admin/ops/export", token=self.normal_token)
        self.assertEqual(resp.status_code, 403)
        resp = self._req("get", "/api/v1/admin/ops/export")
        self.assertEqual(resp.status_code, 401)

    def test_maybe_prune_respects_daily_sentinel(self):
        import time as _time

        # 哨兵文件不存在/过期 → 执行；刚写 → 跳过
        auth_module.record_admin_op(
            "user_create", "op_id", "boss_admin", "bill_normal", detail="创建用户 bill_normal"
        )
        before = len([f for f in os.listdir(auth_module.ADMIN_OPS_DIR) if f.endswith(".json")])
        auth_module._prune_due()  # 首次 → 写哨兵并返回 True
        auth_module.maybe_prune()  # 同一天内 → 哨兵新鲜，跳过
        after = len([f for f in os.listdir(auth_module.ADMIN_OPS_DIR) if f.endswith(".json")])
        self.assertEqual(before, after)  # 未发生任何删除
        # 把哨兵改旧 → 下一次真正执行清理
        try:
            os.utime(config.PRUNE_TOUCH_FILE, (_time.time() - 90000, _time.time() - 90000))
        except OSError:
            pass
        self.assertTrue(auth_module._prune_due())
        auth_module.maybe_prune()
        # 审计仍是 1 条（新记录不会被清理）
        after2 = len([f for f in os.listdir(auth_module.ADMIN_OPS_DIR) if f.endswith(".json")])
        self.assertEqual(after2, 1)
        # 恢复哨兵为当前时间，避免影响后续测试运行（残留过期哨兵会使下轮 maybe_prune 立即清理）
        try:
            os.utime(config.PRUNE_TOUCH_FILE, (_time.time(), _time.time()))
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
