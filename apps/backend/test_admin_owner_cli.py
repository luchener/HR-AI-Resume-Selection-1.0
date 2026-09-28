"""
admin_owner_cli.py 单元测试：管理员归属迁移（改名 / 绑定白名单邮箱 / 取消最高权限）。

覆盖 docs/ARCHITECTURE.md 的管理员判定机制：
- 超级管理员 = 绑定 .env ADMIN_EMAILS 白名单邮箱的账号（默认 luchenstudio@163.com）
- 工具目标：让 luchen 成为管理员、取消 admin 的最高权限

运行：python -m unittest test_admin_owner_cli -v
"""

import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout

# ── 必须在 import auth / admin_owner_cli 之前重定向数据目录 ────────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-owner-cli")
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
import admin_owner_cli  # noqa: E402

OWNER_EMAIL = (list(config.ADMIN_EMAILS) or ["luchenstudio@163.com"])[0]


class _Args:
    """简化 argparse Namespace。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class OwnerCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)
        for _d in _SUBDIRS + ("logs",):
            os.makedirs(os.path.join(_TMP, _d), exist_ok=True)

    def setUp(self):
        # 每个用例重建干净数据：白名单超管 admin + 普通用户 user_plain
        for f in os.listdir(auth.USERS_DIR):
            if f.endswith(".json"):
                os.remove(os.path.join(auth.USERS_DIR, f))
        for f in os.listdir(auth.ADMIN_OPS_DIR):
            try:
                os.remove(os.path.join(auth.ADMIN_OPS_DIR, f))
            except OSError:
                pass
        u1, e1 = auth.create_user("admin", "password123", OWNER_EMAIL)
        assert u1, f"admin create failed: {e1}"
        u2, e2 = auth.create_user("user_plain", "password123", "plain@example.com")
        assert u2, f"plain user create failed: {e2}"

    # ── status ───────────────────────────────────────────────────────
    def test_status_lists_admin_flag(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_status(_Args())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("admin", text)
        self.assertIn("超级管理员", text)
        self.assertIn(OWNER_EMAIL, text)
        self.assertIn("user_plain", text)

    # ── rename ───────────────────────────────────────────────────────
    def test_rename_admin_to_luchen(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_rename(_Args(old="admin", new="luchen"))
        self.assertEqual(rc, 0, err.getvalue())
        # 旧名消失、新名可查，email 保留 -> luchen 仍是超级管理员
        self.assertIsNone(auth.find_user_by_username("admin"))
        luchen = auth.find_user_by_username("luchen")
        self.assertIsNotNone(luchen)
        self.assertEqual((luchen.get("email") or "").lower(), OWNER_EMAIL)
        self.assertTrue(auth.is_super_admin(luchen))
        self.assertTrue(auth.is_admin(luchen))
        # 用户名索引同步
        with open(os.path.join(auth.USERS_DIR, "_index.json"), encoding="utf-8") as f:
            idx = json.load(f)
        self.assertIn("luchen", idx)
        self.assertNotIn("admin", idx)

    def test_rename_case_insensitive_same_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_rename(_Args(old="admin", new="Admin"))
        self.assertEqual(rc, 1)
        self.assertIn("相同", err.getvalue())
        self.assertIsNotNone(auth.find_user_by_username("admin"))

    def test_rename_conflict_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_rename(_Args(old="admin", new="user_plain"))
        self.assertEqual(rc, 1)
        self.assertIn("已存在", err.getvalue())
        self.assertIsNotNone(auth.find_user_by_username("admin"))

    def test_rename_missing_user_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_rename(_Args(old="nobody", new="luchen"))
        self.assertEqual(rc, 1)
        self.assertIn("不存在", err.getvalue())

    def test_rename_invalid_new_name_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_rename(_Args(old="admin", new="a"))
        self.assertEqual(rc, 1)

    # ── make-owner ───────────────────────────────────────────────────
    def test_make_owner_binds_email_and_demotes_others(self):
        # admin 当前绑定白名单邮箱（超管）；把 user_plain 提升为新的超管
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_make_owner(_Args(username="user_plain"))
        self.assertEqual(rc, 0, err.getvalue())
        up = auth.find_user_by_username("user_plain")
        self.assertEqual((up.get("email") or "").lower(), OWNER_EMAIL)
        self.assertTrue(auth.is_super_admin(up))
        # admin 被降权：邮箱绑定清空 -> 不再是超级管理员
        admin = auth.find_user_by_username("admin")
        self.assertNotEqual((admin.get("email") or "").lower(), OWNER_EMAIL)
        self.assertFalse(auth.is_super_admin(admin))
        # 密码保留可登录
        self.assertIsNotNone(auth.authenticate_user("admin", "password123"))

    def test_make_owner_missing_user_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_make_owner(_Args(username="nobody"))
        self.assertEqual(rc, 1)

    # ── unbind-email ─────────────────────────────────────────────────
    def test_unbind_last_admin_protected(self):
        # admin 是唯一白名单管理员 -> 防锁死拒绝
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_unbind_email(_Args(username="admin"))
        self.assertEqual(rc, 1)
        self.assertIn("防锁死", err.getvalue())
        self.assertTrue(auth.is_super_admin(auth.find_user_by_username("admin")))

    def test_unbind_after_make_owner(self):
        # 先 make-owner user_plain，再 unbind admin -> 成功降权
        admin_owner_cli.cmd_make_owner(_Args(username="user_plain"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_unbind_email(_Args(username="admin"))
        self.assertEqual(rc, 0, err.getvalue())
        admin = auth.find_user_by_username("admin")
        self.assertFalse(auth.is_super_admin(admin))
        # user_plain 仍为超管
        self.assertTrue(auth.is_super_admin(auth.find_user_by_username("user_plain")))

    def test_unbind_non_whitelist_is_noop(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = admin_owner_cli.cmd_unbind_email(_Args(username="user_plain"))
        self.assertEqual(rc, 0)
        self.assertIn("无最高权限可取消", out.getvalue())

    # ── 审计 ─────────────────────────────────────────────────────────
    def test_rename_records_admin_op(self):
        admin_owner_cli.cmd_rename(_Args(old="admin", new="luchen"))
        ops = auth.list_admin_ops()
        self.assertTrue(any(o.get("op") == "user_rename" and o.get("target_username") == "luchen" for o in ops))

    def test_make_owner_records_admin_op(self):
        admin_owner_cli.cmd_make_owner(_Args(username="user_plain"))
        ops = auth.list_admin_ops()
        self.assertTrue(any(o.get("op") == "admin_owner_set" and o.get("target_username") == "user_plain" for o in ops))


if __name__ == "__main__":
    unittest.main()
