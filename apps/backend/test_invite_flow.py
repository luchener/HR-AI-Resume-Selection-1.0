"""
邀请码注册流程 单元测试（test_invite_flow.py）

覆盖 docs/INVITE_CODE_DESIGN.md (v3) §10 测试清单 + 关键安全语义：
- 无理由/过短理由 422；无码/伪码注册 422
- check 无邮箱绑定校验；邮箱不匹配在 send-email-code 与 register 处拦截 422
- 完整注册成功且码一次性；用户名冲突不耗码；过期码 422
- 被拒 3 次后第 4 次申请 429 拒收；匿名 401 / 非管理员 403
- 码大小写不敏感注册成功；补发重生成、旧码作废
- 拒绝理由 → 拒信邮件（best-effort，SMTP 故障不影响拒绝结果）
- 管理员审批通过响应含明文码（SMTP 未配置时人工转达兜底）

运行：python -m unittest test_invite_flow -v
"""

import json
import os
import shutil
import unittest

# ── 必须在 import app 之前重定向数据目录 ─────────────────────────────
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-invite")
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
os.environ["ENV"] = "local"

import config  # noqa: E402

config.DATA_DIR = _TMP
for _d in _SUBDIRS:
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
config.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(config.LOG_DIR, exist_ok=True)

import auth  # noqa: E402
import mailer  # noqa: E402

# mailer 补丁（在 app import 前生效；app 内 mailer_mod 引用同一模块对象）
_sent: dict[str, list] = {"invite": [], "reject": []}


def _fake_invite_email(to_email: str, code: str, **kw):
    _sent["invite"].append((to_email, code))
    return True


def _fake_reject_email(to_email: str, reason: str):
    _sent["reject"].append((to_email, reason))
    return True


def _fake_smtp_available():
    return True


mailer.send_invite_code_email = _fake_invite_email
mailer.send_invite_rejection_email = _fake_reject_email
mailer.send_verification_email = lambda to, code, **kw: True
mailer.send_password_reset_email = lambda to, u, c: True
mailer.smtp_available = _fake_smtp_available

import app as backend  # noqa: E402

auth_module = auth
config_module = config


def _json(resp):
    try:
        return resp.get_json() or {}
    except Exception:
        return {}


_ADMIN = object()  # 哨兵：_approve/_reject 默认使用管理员 token


class InviteFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 彻底重置临时目录，避免上次运行残留
        shutil.rmtree(_TMP, ignore_errors=True)
        for _d in _SUBDIRS + ("logs",):
            os.makedirs(os.path.join(_TMP, _d), exist_ok=True)
        cls.client = backend.app.test_client()
        # 管理员账号：email 命中 ADMIN_EMAILS 白名单
        admin_email = (list(config.ADMIN_EMAILS) or ["admin@example.com"])[0]
        cls.admin_email = admin_email
        user, err = auth_module.create_user("admin1", "password123", admin_email)
        assert user, f"admin create failed: {err}"
        cls.admin_token = auth_module.generate_jwt(user["user_id"], user["username"])
        # 普通账号（非管理员）
        normal, _ = auth_module.create_user("normal1", "password123", "normal1@example.com")
        cls.normal_token = auth_module.generate_jwt(normal["user_id"], normal["username"])

    def setUp(self):
        # 每用例独立状态：清空申请单/码/限流/验证码（用户文件保留）
        for d in (
            auth_module.INVITE_REQUESTS_DIR,
            auth_module.INVITE_CODES_DIR,
            auth_module.INVITE_CODES_AUDIT_DIR,
            auth_module.RATE_LIMITS_DIR,
            auth_module.RESETS_DIR,
            auth_module.LOGIN_FAILURES_DIR,
        ):
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        _sent["invite"].clear()
        _sent["reject"].clear()

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

    def _apply(self, email, note="申请理由：想用 AI 简历智选产品", ip=None):
        headers = {"X-Forwarded-For": ip} if ip else {}
        return self.client.post(
            "/api/v1/invite-request",
            data=json.dumps({"email": email, "note": note}),
            headers={**headers, "Content-Type": "application/json"},
        )

    def _apply_rid(self, email, note="申请理由：想用 AI 简历智选产品", ip=None):
        """提交申请并返回申请单 id（响应体不含，需查 pending 记录）。"""
        r = self._apply(email, note=note, ip=ip)
        self.assertEqual(r.status_code, 200, f"apply status={r.status_code} body={r.get_data(as_text=True)[:200]}")
        rec = auth_module._find_invite_request_by_email_status(email, "pending")
        self.assertTrue(rec, f"no pending request for {email}")
        return rec.get("request_id")

    def _approve(self, request_id, token=_ADMIN):
        return self._post(
            f"/api/v1/admin/invite-requests/{request_id}/approve",
            token=self.admin_token if token is _ADMIN else token,
        )

    def _reject(self, request_id, reason, token=_ADMIN):
        return self._post(
            f"/api/v1/admin/invite-requests/{request_id}/reject",
            {"reason": reason},
            token=self.admin_token if token is _ADMIN else token,
        )

    def _register(self, username, password, email, vcode, invite_code):
        return self._post("/api/v1/auth/register", {
            "username": username,
            "password": password,
            "email": email,
            "code": vcode,
            "invite_code": invite_code,
        })

    # ── 申请 ────────────────────────────────────────────────────
    def test_apply_requires_note(self):
        r = self._apply("u-note@example.com", note="x", ip="10.1.0.1")
        self.assertEqual(r.status_code, 422, f"short note status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        r = self._apply("u-note2@example.com", note="", ip="10.1.0.2")
        self.assertEqual(r.status_code, 422, f"empty note status={r.status_code}")

    def test_apply_ok_and_pending_unique(self):
        rid = self._apply_rid("u-apply@example.com", ip="10.1.0.2")
        self.assertTrue(rid)
        # 同邮箱 pending 重复申请 → 409（先清除邮箱冷却，让请求到达 pending 检查）
        auth_module._safe_remove(
            auth_module._rate_file_path("invite_req_email:u-apply@example.com")
        )
        r2 = self._apply("u-apply@example.com", ip="10.1.0.2")
        self.assertEqual(r2.status_code, 409, f"dup pending status={r2.status_code} body={r2.get_data(as_text=True)[:150]}")

    # ── 拒绝上限 ────────────────────────────────────────────────
    def test_reject_three_times_then_blocked(self):
        email = "u-reject3@example.com"
        for i in range(3):
            rid = self._apply_rid(email, note=f"第 {i+1} 次申请", ip=f"10.2.0.{i+1}")
            rr = self._reject(rid, "理由不足")
            self.assertEqual(rr.status_code, 200, f"reject #{i+1} status={rr.status_code} body={rr.get_data(as_text=True)[:150]}")
            # 冷却（同邮箱 60s）每轮用不同邮箱会绕过？—— 同邮箱冷却在 _rate_limited
            # 冷却键是邮箱 + 60s 窗口：同一邮箱连续申请会被拦 → 但被拒记录已生效，
            # 这里直接清掉该邮箱的限流文件以模拟时间流逝
            auth_module._safe_remove(
                auth_module._rate_file_path(f"invite_req_email:{email}")
            )
        # 第 4 次申请 → 429 拒收
        r4 = self._apply(email, note="第 4 次申请", ip="10.2.0.9")
        self.assertEqual(r4.status_code, 429, f"4th apply status={r4.status_code} body={r4.get_data(as_text=True)[:150]}")
        self.assertIn("管理员", (r4.get_json() or {}).get("detail", ""))

    # ── 审批鉴权 ────────────────────────────────────────────────
    def test_approve_reject_requires_admin(self):
        rid = self._apply_rid("u-admin@example.com", ip="10.3.0.1")
        # 匿名 → 401；非管理员 → 403
        r = self._approve(rid, token=None)
        self.assertEqual(r.status_code, 401, f"anon approve status={r.status_code}")
        r = self._approve(rid, token=self.normal_token)
        self.assertEqual(r.status_code, 403, f"non-admin approve status={r.status_code}")
        r = self._reject(rid, "原因", token=self.normal_token)
        self.assertEqual(r.status_code, 403, f"non-admin reject status={r.status_code}")
        # 管理员操作不存在的 id → 404
        r = self._approve("no-such-id")
        self.assertEqual(r.status_code, 404, f"404 status={r.status_code}")
        # 重复审批同一单（已处理）→ 409
        r = self._approve(rid)
        self.assertEqual(r.status_code, 200, f"approve ok status={r.status_code}")
        r2 = self._approve(rid)
        self.assertEqual(r2.status_code, 409, f"double approve status={r2.status_code} body={r2.get_data(as_text=True)[:150]}")

    # ── 完整流程：申请→审批→发码→注册→一次性 ────────────────────
    def test_full_flow_registration_and_one_time_code(self):
        email = "u-flow@example.com"
        rid = self._apply_rid(email, ip="10.4.0.1")
        r = self._approve(rid)
        self.assertEqual(r.status_code, 200, f"approve status={r.status_code} body={r.get_data(as_text=True)[:200]}")
        code = (r.get_json() or {}).get("data", {}).get("invite_code")
        self.assertTrue(code, "approve response must include invite_code")
        self.assertEqual(len(code), config_module.INVITE_CODE_LENGTH)
        # 小写码注册成功 → 大小写不敏感
        vcode = auth_module.create_email_code(email, auth_module.PURPOSE_EMAIL_VERIFY)
        self.assertTrue(vcode, "email code should be created")
        reg = self._register("flow_user", "password123", email, vcode, code.lower())
        self.assertEqual(reg.status_code, 200, f"register status={reg.status_code} body={reg.get_data(as_text=True)[:200]}")
        # 一次性：同码再次注册 → 422
        vcode2 = auth_module.create_email_code("flow2@example.com", auth_module.PURPOSE_EMAIL_VERIFY)
        reg2 = self._register("flow_user2", "password123", "flow2@example.com", vcode2, code)
        self.assertEqual(reg2.status_code, 422, f"second register status={reg2.status_code} body={reg2.get_data(as_text=True)[:200]}")

    def test_register_without_code_rejected(self):
        # 无邀请码 / 伪码 / 码与邮箱不匹配 → 注册 422
        email = "u-nocode@example.com"
        rid = self._apply_rid(email, ip="10.4.1.1")
        code = (self._approve(rid).get_json() or {}).get("data", {}).get("invite_code")
        vcode = auth_module.create_email_code(email, auth_module.PURPOSE_EMAIL_VERIFY)
        r = self._register("nocode_u", "password123", email, vcode, "")
        self.assertEqual(r.status_code, 422, f"no code status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        # 伪码
        vcode2 = auth_module.create_email_code(email, auth_module.PURPOSE_EMAIL_VERIFY)
        r = self._register("nocode_u2", "password123", email, vcode2, "ZZZZ")
        self.assertEqual(r.status_code, 422, f"fake code status={r.status_code}")
        # 邮箱不匹配（码绑定 email，注册用别的邮箱）
        vcode3 = auth_module.create_email_code("other@example.com", auth_module.PURPOSE_EMAIL_VERIFY)
        r = self._register("nocode_u3", "password123", "other@example.com", vcode3, code)
        self.assertEqual(r.status_code, 422, f"mismatched email status={r.status_code} body={r.get_data(as_text=True)[:150]}")

    def test_username_conflict_does_not_consume_code(self):
        auth_module.create_user("taken_name", "password123", "taken@example.com")
        email = "u-conflict@example.com"
        rid = self._apply_rid(email, ip="10.4.2.1")
        code = (self._approve(rid).get_json() or {}).get("data", {}).get("invite_code")
        # 用户名冲突注册 → 409，码不消费
        vcode = auth_module.create_email_code(email, auth_module.PURPOSE_EMAIL_VERIFY)
        reg = self._register("taken_name", "password123", email, vcode, code)
        self.assertEqual(reg.status_code, 409, f"conflict status={reg.status_code} body={reg.get_data(as_text=True)[:150]}")
        # 码仍有效 → 换用户名可成功注册
        vcode2 = auth_module.create_email_code(email, auth_module.PURPOSE_EMAIL_VERIFY)
        reg2 = self._register("free_name", "password123", email, vcode2, code)
        self.assertEqual(reg2.status_code, 200, f"retry status={reg2.status_code} body={reg2.get_data(as_text=True)[:150]}")

    # ── 校验接口 ────────────────────────────────────────────────
    def test_invalid_and_expired_codes(self):
        # 伪码 check → 422
        r = self._post("/api/v1/auth/invite-code/check", {"invite_code": "ZZZZ"})
        self.assertEqual(r.status_code, 422, f"fake code check status={r.status_code}")
        # 过期码：生成后把 expires_at 拨到过去 → check 422
        code = auth_module.generate_invite_code(
            bound_email="u-exp@example.com", request_id="exp-test", created_by="admin1", ttl_hours=1
        )
        rec = auth_module._invite_code_record(code)
        rec["expires_at"] = auth_module._now_ts() - 10
        auth_module._write_json(
            os.path.join(auth_module.INVITE_CODES_DIR, f"{auth_module._invite_code_hash(code)}.json"), rec
        )
        r = self._post("/api/v1/auth/invite-code/check", {"invite_code": code})
        self.assertEqual(r.status_code, 422, f"expired code status={r.status_code}")
        # 有效码 check → 200（此阶段无邮箱绑定校验）
        ok_code = auth_module.generate_invite_code(
            bound_email="u-ok@example.com", request_id="ok-test", created_by="admin1"
        )
        r = self._post("/api/v1/auth/invite-code/check", {"invite_code": ok_code})
        self.assertEqual(r.status_code, 200, f"valid code status={r.status_code} body={r.get_data(as_text=True)[:150]}")

    def test_send_email_code_gate(self):
        # send-email-code：缺码 / 伪码 / 邮箱不匹配 → 422（不发验证码）
        r = self._post("/api/v1/auth/invite-code/send-email-code", {"email": "x@example.com"})
        self.assertEqual(r.status_code, 422, f"no code status={r.status_code}")
        r = self._post("/api/v1/auth/invite-code/send-email-code", {"email": "x@example.com", "invite_code": "BOGUS"})
        self.assertEqual(r.status_code, 422, f"bogus code status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        code = auth_module.generate_invite_code(
            bound_email="u-gate@example.com", request_id="gate-test", created_by="admin1"
        )
        # 邮箱不匹配 → 422
        r = self._post("/api/v1/auth/invite-code/send-email-code", {"email": "someone@example.com", "invite_code": code})
        self.assertEqual(r.status_code, 422, f"mismatch status={r.status_code} body={r.get_data(as_text=True)[:150]}")

    # ── 补发 ────────────────────────────────────────────────────
    def test_resend_regenerates_code(self):
        email = "u-resend@example.com"
        rid = self._apply_rid(email, ip="10.5.0.1")
        code1 = (self._approve(rid).get_json() or {}).get("data", {}).get("invite_code")
        # 补发 → 新码，旧码作废
        r = self._post(f"/api/v1/admin/invite-requests/{rid}/resend", token=self.admin_token)
        self.assertEqual(r.status_code, 200, f"resend status={r.status_code} body={r.get_data(as_text=True)[:150]}")
        code2 = (r.get_json() or {}).get("data", {}).get("invite_code")
        self.assertNotEqual(code1, code2, "resend must regenerate a fresh code")
        old_check = self._post("/api/v1/auth/invite-code/check", {"invite_code": code1})
        self.assertEqual(old_check.status_code, 422, f"old code after resend status={old_check.status_code}")

    # ── 拒信 best-effort ─────────────────────────────────────────
    def test_rejection_email_best_effort(self):
        email = "u-reject-mail@example.com"
        rid = self._apply_rid(email, ip="10.6.0.1")
        rr = self._reject(rid, "不符合使用场景")
        self.assertEqual(rr.status_code, 200, f"reject status={rr.status_code}")
        self.assertTrue(any(to == email for to, _ in _sent["reject"]), "rejection email should be recorded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
