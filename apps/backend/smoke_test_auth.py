"""
快速冒烟测试：验证多用户认证 + 数据隔离。
不启动真实服务器，直接用 Flask test client。
用法：python smoke_test_auth.py
"""
import json
import os
import sys
import uuid

# 强制使用临时数据目录，避免污染真实数据（放在工作区内以满足沙箱写权限）
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".smoke-tmp")
os.makedirs(_TMP, exist_ok=True)
os.environ["ENV"] = "local"

import config
config.DATA_DIR = _TMP
config.RESUMES_DIR = os.path.join(_TMP, "resumes")
config.JOBS_DIR = os.path.join(_TMP, "jobs")
config.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(config.LOG_DIR, exist_ok=True)

# 需要重新加载模块（config 已初始化目录）
import importlib
for mod_name in ("store", "auth", "mailer"):
    mod = importlib.import_module(mod_name)
    importlib.reload(mod)

import auth as auth_module
import mailer as mailer_mod

# ── 补丁：测试中不真发邮件，记录发送调用 ──────────────────────────
_sent_reset: list[tuple[str, str, str]] = []
_sent_verify: list[tuple[str, str, str]] = []


def _fake_send_reset_email(to_email: str, username: str, code: str) -> bool:
    _sent_reset.append((to_email, username, code))
    return True


def _fake_send_verification_email(to_email: str, code: str, purpose: str = "注册") -> bool:
    _sent_verify.append((to_email, code, purpose))
    return True


mailer_mod.send_password_reset_email = _fake_send_reset_email
mailer_mod.send_verification_email = _fake_send_verification_email
mailer_mod.smtp_available = lambda: True  # 让测试走"已配置 SMTP"分支

from app import app

client = app.test_client()
passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def post(path, data=None, token=None, headers=None):
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    return client.post(path, data=json.dumps(data) if data is not None else None, headers=h)


def reg_with_code(username, password, email, invite=None):
    """通过验证码走注册路由：生成邀请码（未绑定）→ 发码 → 从记录取明文码 → 注册。"""
    if invite is None:
        invite = auth_module.generate_invite_code(
            bound_email="",
            request_id=f"smoke-{uuid.uuid4()}",
            created_by="smoke-test",
            note="冒烟测试",
        )
    _sent_verify.clear()
    r = post("/api/v1/auth/email-code/send", {"email": email})
    code = _sent_verify[-1][1] if _sent_verify else ""
    return post(
        "/api/v1/auth/register",
        {"username": username, "password": password, "email": email, "code": code, "invite_code": invite},
    )


print("== 1. 免认证接口 ==")
r = client.get("/ping")
check("ping 免认证可访问", r.status_code == 200, f"status={r.status_code}")

print("== 2. 注册 ==")
# 直接调用 auth 建号（绕过路由的验证码，方便后续账号类测试复用）。
# 注册路由的验证码流程在 3.8 单独覆盖。
user, err = auth_module.create_user("alice", "password123", "alice@example.com")
check("alice 建号成功", user is not None, f"err={err}")
alice_token = auth_module.generate_jwt(user["user_id"], user["username"]) if user else ""
check("alice 拿到 token", bool(alice_token))

user2, err2 = auth_module.create_user("alice", "password123")
check("重复注册被拒绝", user2 is None and err2 is not None, f"err={err2}")

user3, err3 = auth_module.create_user("bob", "short")
check("弱密码被拒绝", user3 is None and "密码长度" in (err3 or ""), f"err={err3}")

print("== 3. 登录 ==")
r = post("/api/v1/auth/login", {"username": "alice", "password": "password123"})
check("alice 登录成功", r.status_code == 200, f"status={r.status_code}")
alice_token = (r.get_json() or {}).get("data", {}).get("token", "")

r = post("/api/v1/auth/login", {"username": "alice", "password": "wrongpass"})
check("错误密码被拒绝", r.status_code == 401)

print("== 3.5 用户名大小写不敏感 ==")
r = reg_with_code("Alice", "password123", "alice-case@example.com")
check("Alice(与 alice 大小写不同) 注册被拒 409", r.status_code == 409, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

r = post("/api/v1/auth/login", {"username": "ALICE", "password": "password123"})
check("ALICE 大写登录 alice 账号成功", r.status_code == 200, f"status={r.status_code}")
r = post("/api/v1/auth/login", {"username": "Alice", "password": "password123"})
check("Alice 混合大小写登录成功", r.status_code == 200, f"status={r.status_code}")

print("== 3.6 修改密码 ==")
# 未登录改密码 → 401
r = post("/api/v1/auth/change-password", {"old_password": "password123", "new_password": "newpass456"})
check("未登录改密码被拒 401", r.status_code == 401, f"status={r.status_code}")

# 旧密码错误 → 401
r = post(
    "/api/v1/auth/change-password",
    {"old_password": "wrongold", "new_password": "newpass456"},
    token=alice_token,
)
check("旧密码错误被拒 401", r.status_code == 401, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 新旧密码相同 → 422
r = post(
    "/api/v1/auth/change-password",
    {"old_password": "password123", "new_password": "password123"},
    token=alice_token,
)
check("新旧密码相同被拒 422", r.status_code == 422, f"status={r.status_code}")

# 正常修改密码 → 200，且旧 token 立即失效
r = post(
    "/api/v1/auth/change-password",
    {"old_password": "password123", "new_password": "newpass456"},
    token=alice_token,
)
check("alice 修改密码成功", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:200]}")

r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {alice_token}"})
check("改密后旧 token 失效 401", r.status_code == 401, f"status={r.status_code}")

# 新密码可登录 → 刷新 token
r = post("/api/v1/auth/login", {"username": "alice", "password": "newpass456"})
check("新密码登录成功", r.status_code == 200, f"status={r.status_code}")
alice_token = (r.get_json() or {}).get("data", {}).get("token", "")
check("刷新后拿到新 token", bool(alice_token))

# 改回原密码，方便后续测试用例继续用 password123
r = post(
    "/api/v1/auth/change-password",
    {"old_password": "newpass456", "new_password": "password123"},
    token=alice_token,
)
check("alice 改回原密码成功", r.status_code == 200, f"status={r.status_code}")
r = post("/api/v1/auth/login", {"username": "alice", "password": "password123"})
alice_token = (r.get_json() or {}).get("data", {}).get("token", "")
check("原密码重新登录成功", r.status_code == 200 and bool(alice_token), f"status={r.status_code}")

print("== 3.7 忘记密码重置（6 位验证码）==")
# 注册带邮箱的新用户用于测试（走验证码注册）
r = reg_with_code("carol", "carolpass123", "carol@example.com")
check("carol 验证码注册成功", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 邮箱重复注册被拒（验证码已发但邮箱已占用 → 422 在发码阶段或注册阶段）
r = post("/api/v1/auth/email-code/send", {"email": "CAROL@example.com"})
check("已占用邮箱发码被拒 422（大小写不敏感）", r.status_code == 422, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 申请重置：已注册邮箱 → 200 + 记录邮件
_sent_reset.clear()
r = post("/api/v1/auth/reset-password/request", {"email": "carol@example.com"})
check("申请重置（已注册邮箱）返回 200", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")
check("重置邮件已记录发送", len(_sent_reset) == 1, f"sent={len(_sent_reset)}")

# 未注册邮箱 → 同样 200 同文案（防枚举）
r = post("/api/v1/auth/reset-password/request", {"email": "ghost@example.com"})
check("未注册邮箱返回同一文案 200", r.status_code == 200, f"status={r.status_code}")

# 用错误验证码确认 → 400
r = post("/api/v1/auth/reset-password/confirm", {"email": "carol@example.com", "code": "XXXXXX", "new_password": "carolnewpass1"})
check("错误验证码确认被拒 400", r.status_code == 400, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 用正确验证码确认 → 200
assert _sent_reset, "should have captured reset code"
_carol_code = _sent_reset[0][2]
r = post("/api/v1/auth/reset-password/confirm", {"email": "carol@example.com", "code": _carol_code, "new_password": "carolnewpass1"})
check("正确验证码重置密码成功", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 验证码一次性：重复使用 → 400
r = post("/api/v1/auth/reset-password/confirm", {"email": "carol@example.com", "code": _carol_code, "new_password": "anotherpass1"})
check("验证码一次性（重复使用被拒 400）", r.status_code == 400, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 新密码登录成功；旧密码登录失败
r = post("/api/v1/auth/login", {"username": "carol", "password": "carolnewpass1"})
check("重置后新密码登录成功", r.status_code == 200, f"status={r.status_code}")
r = post("/api/v1/auth/login", {"username": "carol", "password": "carolpass123"})
check("重置后旧密码登录失败 401", r.status_code == 401, f"status={r.status_code}")

print("== 3.8 验证码注册（发码/校验/频率限制）==")
# 注册必需验证码：缺 code → 422
r = post("/api/v1/auth/register", {"username": "dave", "password": "davepass123", "email": "dave@example.com"})
check("注册缺验证码被拒 422", r.status_code == 422, f"status={r.status_code}")

# 错误验证码 → 409/400（需先有有效邀请码，否则 422 缺码——见 3.9 对缺码的覆盖）
inv_dave = auth_module.generate_invite_code(
    bound_email="dave@example.com",
    request_id=f"smoke-3-8-{uuid.uuid4()}",
    created_by="smoke-test",
    note="错误验证码场景",
)
r = post("/api/v1/auth/register", {"username": "dave", "password": "davepass123", "email": "dave@example.com", "code": "BADBAD", "invite_code": inv_dave})
check("注册错误验证码被拒", r.status_code in (400, 409), f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 正确验证码 → 200（自动生成未绑定邀请码）
r = reg_with_code("dave", "davepass123", "dave@example.com")
check("dave 验证码注册成功", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

# 频率限制：60 秒内同邮箱重复发码 → 429
r = post("/api/v1/auth/email-code/send", {"email": "dave@example.com"})
# 该邮箱已注册会先 422；用未注册邮箱测 429（已发过一次的测试邮箱）
r2 = post("/api/v1/auth/email-code/send", {"email": "another@example.com"})
check("首次发码成功 200", r2.status_code == 200, f"status={r2.status_code}")
r3 = post("/api/v1/auth/email-code/send", {"email": "another@example.com"})
check("冷却期内重复发码被拒 429", r3.status_code == 429, f"status={r3.status_code} body={r3.get_data(as_text=True)[:120]}")

print("== 3.9 邀请码注册流程（申请→审批→发码→注册）==")
# ① 无邀请码注册 → 422
_inv_email = "invite-user@example.com"
inv = auth_module.generate_invite_code(
    bound_email=_inv_email,
    request_id=f"smoke3-9-{uuid.uuid4()}",
    created_by="smoke-test",
    note="邀请码流程",
)
_sent_verify.clear()
r = post("/api/v1/auth/email-code/send", {"email": _inv_email})
code_inv = _sent_verify[-1][1] if _sent_verify else ""
r = post(
    "/api/v1/auth/register",
    {"username": "invite_user", "password": "password123", "email": _inv_email, "code": code_inv},
)
check("缺邀请码注册被拒 422", r.status_code == 422, f"status={r.status_code}")
# ② 大小写不敏感的邀请码 → 200
r = post(
    "/api/v1/auth/register",
    {"username": "invite_user", "password": "password123", "email": _inv_email, "code": code_inv, "invite_code": inv.lower()},
)
check("邀请码小写注册成功", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")
# ③ 一次性：同一邀请码再注册 → 422（码已消费）
r = post("/api/v1/auth/invite-code/check", {"invite_code": inv})
check("已消费邀请码校验被拒 422", r.status_code == 422, f"status={r.status_code} body={r.get_data(as_text=True)[:120]}")

print("== 4. 未带 token 访问被拒 ==")
r = client.get("/api/v1/resumes?resume_id=x")
check("无 token 访问 resumes 返回 401", r.status_code == 401, f"status={r.status_code}")

print("== 5. 伪造 token 被拒 ==")
r = client.get("/api/v1/resumes?resume_id=x", headers={"Authorization": "Bearer fake.token.here"})
check("伪造 token 返回 401", r.status_code == 401, f"status={r.status_code}")

print("== 6. alice 上传简历 + JD + 分析 ==")
import io
from test_helpers import build_anonymous_resume_docx
r = client.post(
    "/api/v1/resumes/upload",
    data={"file": (io.BytesIO(build_anonymous_resume_docx()), "alice-resume.docx")},
    content_type="multipart/form-data",
    headers={"Authorization": f"Bearer {alice_token}"},
)
check("alice 上传简历", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:200]}")
alice_resume_id = (r.get_json() or {}).get("resume_id", "")

r = post(
    "/api/v1/jobs/upload",
    {"resume_id": alice_resume_id, "job_descriptions": ["资深后端工程师，5年以上经验，熟悉Python"]},
    token=alice_token,
)
check("alice 上传 JD", r.status_code == 200, f"status={r.status_code} body={r.get_data(as_text=True)[:200]}")
alice_job_id = (r.get_json() or {}).get("job_id", [None])[0]

print("== 7. bob 注册并尝试越权 ==")
bob_user, bob_err = auth_module.create_user("bob", "password456", "bob@example.com")
bob_token = auth_module.generate_jwt(bob_user["user_id"], "bob") if bob_user else ""
check("bob 注册成功", bool(bob_token), f"err={bob_err}")

r = client.get(f"/api/v1/resumes?resume_id={alice_resume_id}", headers={"Authorization": f"Bearer {bob_token}"})
check("bob 读取 alice 简历被拒(404)", r.status_code == 404, f"status={r.status_code}")

r = post(
    "/api/v1/jobs/upload",
    {"resume_id": alice_resume_id, "job_descriptions": ["测试JD"]},
    token=bob_token,
)
check("bob 用 alice 的 resume_id 上传 JD 被拒", r.status_code == 400, f"status={r.status_code}")

r = post(
    "/api/v1/resumes/hr-analysis",
    {"resume_id": alice_resume_id, "job_id": alice_job_id},
    token=bob_token,
)
check("bob 分析 alice 的数据被拒(404)", r.status_code == 404, f"status={r.status_code}")

r = post(
    "/api/v1/resumes/hr-analysis",
    {"resume_id": alice_resume_id, "job_id": alice_job_id},
    token=alice_token,
)
# LLM 未配置时会返回 500/503/422 之一，但不该是 401/404（数据归属校验通过）
check(
    "alice 自己分析通过归属校验（无 Key 时走到 LLM 报错而非越权）",
    r.status_code not in (401, 404),
    f"status={r.status_code} body={r.get_data(as_text=True)[:200]}",
)

print("== 8. auth/me ==")
r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bob_token}"})
me = (r.get_json() or {}).get("data", {})
check("auth/me 返回 bob", me.get("username") == "bob", f"me={me}")

print(f"\n结果：{passed} 通过 / {failed} 失败")
# 清理临时目录
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
