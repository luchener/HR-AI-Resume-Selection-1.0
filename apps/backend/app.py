"""
Flask 后端应用。极简化重构，替代旧版 FastAPI + SQLAlchemy + Agent 抽象层。

主要路由：
    GET  /ping                                  健康检查
    POST /api/v1/ai/test                        测试请求级模型配置
    POST /api/v1/resumes/upload                 上传简历（multipart）
    POST /api/v1/resumes/hr-analysis            单人/多人招聘筛选
    POST /api/v1/resumes/improve                分析简历（?stream=true 走 SSE）
    GET  /api/v1/resumes?resume_id=             获取简历
    POST /api/v1/resumes/improved-markdown      提取优化后简历 markdown
    POST /api/v1/jobs/upload                    上传 JD（JSON，手动校验 Content-Type）
    GET  /api/v1/jobs?job_id=                   获取 JD

启动：gunicorn app:app（宝塔/生产）或 python run.py（本地）。
"""
import csv
import io
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import Flask, g, request, jsonify, Response, stream_with_context

import config
from config import ALLOWED_ORIGINS
import store
import auth as auth_mod
import mailer as mailer_mod
import llm
import parser as doc_parser
import screening_agent
import resume_review
import resume_sanitize
from prompts import (
    PROMPT_HR_JUDGE,
    PROMPT_HR_RECRUITMENT_ANALYSIS,
)

# ── 日志（标准库，去掉复杂轮转）──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if config.ENV == "production" else logging.DEBUG,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("resume-matcher")
if not any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
    file_handler = logging.FileHandler(
        os.path.join(config.LOG_DIR, "backend.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s - %(name)s - %(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)
for noisy_logger in ("openai", "httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

# ── 生产环境启动校验 ─────────────────────────────────────────────────
config.check_production()

app = Flask(__name__)

# ── 认证中间件 + 错误处理器（init_auth 一行注册：require_auth + _AuthError 处理）
auth_mod.init_auth(app)

# Leave a little room for multipart headers, then enforce the exact file limit
# after Flask has parsed the uploaded part.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
MAX_RESUME_FILE_SIZE = 30 * 1024 * 1024
_HR_ANALYSIS_VERSION = "screening-agent-v1-employment-timeline"
# HR 分析结果缓存：LRU（最近最久未用淘汰）+ TTL（24h），防内存无界增长，
# 同时保留"同一简历+同一职位+同一模型配置重复分析不调 LLM"的省 token 语义。
_HR_ANALYSIS_CACHE_MAX = 1000
_HR_ANALYSIS_CACHE_TTL_SECONDS = 24 * 3600
_HR_ANALYSIS_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_HR_ANALYSIS_CACHE_LOCK = threading.Lock()


def _hr_cache_get(key: tuple) -> dict | None:
    """取缓存（命中刷新 LRU 位置；过期条目惰性剔除）。"""
    now = time.time()
    with _HR_ANALYSIS_CACHE_LOCK:
        entry = _HR_ANALYSIS_CACHE.get(key)
        if entry is None:
            return None
        if now - entry.get("_ts", 0) > _HR_ANALYSIS_CACHE_TTL_SECONDS:
            _HR_ANALYSIS_CACHE.pop(key, None)
            return None
        _HR_ANALYSIS_CACHE.move_to_end(key)
        return entry.get("value")


def _hr_cache_put(key: tuple, value: dict) -> None:
    """写缓存：超容量淘汰最久未用条目。"""
    now = time.time()
    with _HR_ANALYSIS_CACHE_LOCK:
        if key in _HR_ANALYSIS_CACHE:
            _HR_ANALYSIS_CACHE.pop(key, None)
        _HR_ANALYSIS_CACHE[key] = {"_ts": now, "value": value}
        while len(_HR_ANALYSIS_CACHE) > _HR_ANALYSIS_CACHE_MAX:
            _HR_ANALYSIS_CACHE.popitem(last=False)


@app.errorhandler(413)
def _handle_payload_too_large(_error):
    return _err("Uploaded file exceeds the 30 MB limit", 413, "resumes")


# ── CORS（after_request，简单可靠）──────────────────────────────────
@app.after_request
def _cors(resp: Response) -> Response:
    if request.path.startswith("/api/v1/"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"

    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "*"
    return resp


# ── CORS 预检：Flask 默认对未注册 OPTIONS 返回 405，
#    浏览器在跨域 + Content-Type: application/json 场景下会先发预检，
#    没有这个处理器整个跨域 POST 都会被拦截。
@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        resp = Response()
        origin = request.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            req_methods = request.headers.get("Access-Control-Request-Method")
            resp.headers["Access-Control-Allow-Methods"] = req_methods or "GET,POST,OPTIONS"
            req_headers = request.headers.get("Access-Control-Request-Headers")
            resp.headers["Access-Control-Allow-Headers"] = req_headers or "*"
        return resp


# ── 统一错误类型：替代 _do_improve 的 (jsonify, status) tuple 模式 ────
class ApiError(Exception):
    """业务可主动抛的 HTTP 错误。message / status / service 用于响应构造。"""

    def __init__(self, message: str, status: int = 400, service: str = "api"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.service = service


@app.errorhandler(ApiError)
def _handle_api_error(e: ApiError):
    return _err(e.message, e.status, e.service)


# ── request_id 工具（与旧版格式兼容：服务段:uuid）────────────────────
def is_super_admin():
    me = g.get("auth_user") or {}
    return bool(auth_mod.is_super_admin(me))


def _is_admin_account(user: dict | None) -> bool:
    """该账号是否为管理员身份（白名单邮箱或 is_admin 标记）。权限分级用。"""
    return bool(auth_mod.is_admin(user))


def _ensure_super_admin_for_admin_target(rid: str, target_user: dict | None) -> bool:
    """普通管理员操作管理员身份账号 → 403。返回 True 表示请求应被拒绝（已写响应）。"""
    if not _is_admin_account(target_user):
        return False  # 普通用户，任何管理员可操作
    if is_super_admin():
        return False  # 超级管理员可操作
    raise ApiError("该账号为管理员账号，仅超级管理员（admin）可操作。", 403, "admin")


def _request_id(service: str = "api") -> str:
    return f"{service}:{uuid.uuid4()}"


def _current_user_id() -> str:
    """
    从认证中间件注入的 g.auth_user 获取当前用户 id。
    注意：只能在请求上下文中调用；线程池任务（如批量分析）必须显式传入，
    不能在线程里访问 g。
    """
    auth_user = g.get("auth_user")
    if not auth_user:
        raise ApiError("请先登录。", 401, "auth")
    return auth_user["user_id"]


def _err(detail: str, status: int, service: str = "api"):
    """统一错误响应：{detail, request_id}"""
    return jsonify({"detail": detail, "request_id": _request_id(service)}), status


def _request_ai_config(data: dict, *, required: bool = False) -> dict | None:
    value = data.get("ai_config")
    if value is None:
        if required:
            raise ApiError("请先配置 AI 模型。", 422, "ai")
        return None
    if not config.ALLOW_CUSTOM_AI_CONFIG:
        raise ApiError("生产环境不允许使用自定义 AI Key，请联系管理员配置。", 403, "ai")
    try:
        return llm.normalize_runtime_config(value)
    except ValueError as exc:
        raise ApiError(str(exc), 422, "ai") from exc


def _request_agent_config(data: dict) -> dict:
    """请求级 Agent 增强配置（可选）：web_search（Step 4）。

    非法值静默回退默认，不阻断请求。
    """
    value = data.get("agent_config")
    if not isinstance(value, dict):
        return {}
    web_search = bool(value.get("web_search")) if isinstance(value.get("web_search"), bool) else False
    return {"web_search": web_search}


def _agent_config_fingerprint(agent_config: dict | None) -> str:
    """Agent 增强配置的缓存指纹，防止不同增强等级串缓存。"""
    config = agent_config if isinstance(agent_config, dict) else {}
    return "ws=1" if config.get("web_search") else "ws=0"


# ════════════════════════════════════════════════════════════════════
# 健康检查
# ════════════════════════════════════════════════════════════════════
@app.get("/ping")
def ping():
    return jsonify({"message": "pong", "database": "reachable"})


# ════════════════════════════════════════════════════════════════════════
# 用户认证
# ════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/auth/register")
def register():
    """注册：需有效邀请码 + 注册邮箱验证码。body: {username, password, email, code, invite_code}"""
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    code = (data.get("code") or "").strip()
    invite_code = (data.get("invite_code") or "").strip()
    if not username or not password:
        return jsonify({"detail": "请提供用户名和密码。", "request_id": rid}), 422
    if not email or not code:
        return jsonify({"detail": "请提供邮箱和邮箱验证码。", "request_id": rid}), 422
    if not invite_code:
        return jsonify({"detail": "请填写邀请码。", "request_id": rid}), 422
    # 注册失败按 IP 计数（防撞库/爆破注册接口）
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    user, error, field = auth_mod.register_with_email_code(
        username, password, email, code, invite_code
    )
    if error:
        status = 409 if field != "invite_code" else 422
        return jsonify({"detail": error, "request_id": rid}), status
    token = auth_mod.generate_jwt(user["user_id"], user["username"])
    logger.info(
        "[auth] register ok username=%s email=%s bound_invite=%s",
        user["username"],
        email,
        auth_mod.normalize_invite_code(invite_code),
    )
    return jsonify({
        "request_id": rid,
        "data": {
            "user_id": user["user_id"],
            "username": user["username"],
            "token": token,
        },
    })


@app.post("/api/v1/auth/invite-code/send-email-code")
def invite_code_send_email_code():
    """
    注册 · 发送邮箱验证码（须先通过邀请码校验，且邮箱与码绑定邮箱一致）。
    body: {email, invite_code}
    - 邀请码无效/已用/过期 → 422（统一文案）
    - 邮箱与码绑定邮箱不匹配 → 422（提示仅限申请邮箱）
    - 其余逻辑与原 email-code/send 一致（含冷却/已注册/SMTP 检查）
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    invite_code = (data.get("invite_code") or "").strip()
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"detail": "邮箱格式不正确。", "request_id": rid}), 422
    if not invite_code:
        return jsonify({"detail": "请先填写并验证邀请码。", "request_id": rid}), 422
    # 每 IP 邀请码校验失败限流（10 次/小时）
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    ip_hash = auth_mod._ip_hash(ip)
    limited, _retry = auth_mod._rate_limited(f"invite_check_ip:{ip_hash}", max_count=10, window=3600)
    if limited:
        return jsonify({"detail": "尝试次数过多，请稍后再试。", "request_id": rid}), 429

    check = auth_mod.validate_invite_code(invite_code, email=email)
    if check is not None:
        return jsonify({"detail": "邀请码无效或不可用，请联系管理员获取新的邀请码。", "request_id": rid}), 422
    # 该邮箱已注册
    if auth_mod.find_user_by_email(email):
        return jsonify({"detail": "该邮箱已被注册，请直接登录或使用忘记密码。", "request_id": rid}), 422
    # 频率限制 + SMTP 检查
    if not auth_mod.can_request_code(email, auth_mod.PURPOSE_EMAIL_VERIFY):
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    if not mailer_mod.smtp_available():
        return jsonify({"detail": "邮件服务未配置，请联系管理员处理。", "request_id": rid}), 503
    code = auth_mod.create_email_code(email, auth_mod.PURPOSE_EMAIL_VERIFY)
    if not code:
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    try:
        mailer_mod.send_verification_email(email, code, purpose="注册")
    except Exception as exc:
        logger.error("send register email code failed for %s: %s", email, exc)
        return jsonify({"detail": "邮件发送失败，请稍后重试或联系管理员。", "request_id": rid}), 502
    logger.info("register email code sent to %s (invite ok)", email)
    return jsonify({"detail": "验证码已发送，请查收邮件。", "request_id": rid}), 200


@app.post("/api/v1/auth/email-code/send")
def email_code_send():
    """
    发送邮箱验证码（注册绑定时用）。body: {email}
    - 邮箱已注册 → 422
    - 冷却期内（60 秒）→ 429
    - SMTP 未配置 → 503
    - 成功 → 200

    说明：注册页现在必须先通过邀请码校验，前端走 /invite-code/send-email-code。
    本端点保留给「忘记密码/其他复用」场景（邀请码闸门不适用于重置验证码）。
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"detail": "请提供邮箱。", "request_id": rid}), 422
    email_clean = email.lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        return jsonify({"detail": "邮箱格式不正确。", "request_id": rid}), 422
    # 该邮箱已注册
    if auth_mod.find_user_by_email(email_clean):
        return jsonify({"detail": "该邮箱已被注册，请直接登录或使用忘记密码。", "request_id": rid}), 422
    # 频率限制
    if not auth_mod.can_request_code(email_clean, auth_mod.PURPOSE_EMAIL_VERIFY):
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    if not mailer_mod.smtp_available():
        return jsonify({"detail": "邮件服务未配置，请联系管理员处理。", "request_id": rid}), 503
    code = auth_mod.create_email_code(email_clean, auth_mod.PURPOSE_EMAIL_VERIFY)
    if not code:
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    try:
        mailer_mod.send_verification_email(email_clean, code, purpose="注册")
    except Exception as exc:
        logger.error("send register email code failed for %s: %s", email_clean, exc)
        return jsonify({"detail": "邮件发送失败，请稍后重试或联系管理员。", "request_id": rid}), 502
    logger.info("register email code sent to %s", email_clean)
    return jsonify({"detail": "验证码已发送，请查收邮件。", "request_id": rid}), 200


@app.get("/api/v1/auth/captcha")
def auth_captcha():
    """获取登录验证码（四位随机数字）。返回 captcha_id + 明文 code，一次性使用。"""
    rid = auth_mod._request_id("auth")
    if not config.CAPTCHA_ENABLED:
        return jsonify({"detail": "验证码功能未启用。", "request_id": rid}), 404
    captcha_id, code = auth_mod.new_captcha()
    return jsonify({
        "request_id": rid,
        "data": {"captcha_id": captcha_id, "code": code, "ttl_seconds": auth_mod.CAPTCHA_TTL_SECONDS},
    })


@app.post("/api/v1/auth/login")
def login():
    """登录：含防爆破（冷却/冻结/每 IP 限流）+ 四位数字验证码。"""
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"detail": "请提供用户名和密码。", "request_id": rid}), 422
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""

    # 0. 验证码校验（一次性）：失败不记账（不影响防爆破计数），直接拒绝
    if config.CAPTCHA_ENABLED:
        captcha_id = (data.get("captcha_id") or "").strip()
        captcha_code = (data.get("captcha_code") or "").strip()
        if not captcha_id or not captcha_code:
            return jsonify({"detail": "请填写验证码。", "request_id": rid}), 422
        if not auth_mod.verify_captcha(captcha_id, captcha_code):
            return jsonify({"detail": "验证码错误或已过期，请刷新验证码后重试。", "request_id": rid}), 422

    # 0.5 每 IP 失败上限（防跨用户名撞库）—— 只读检查；计数在失败时才落账
    ip_limited, ip_retry = auth_mod.ip_login_rate_exceeded(ip)
    if ip_limited:
        retry_min = max(1, int(ip_retry // 60) + 1)
        resp = jsonify({"detail": f"尝试次数过多，请在 {retry_min} 分钟后重试。", "request_id": rid}), 429
        resp[0].headers["Retry-After"] = str(int(ip_retry) + 1)
        return resp

    # 1. 账号级策略（冷却/冻结）
    action, retry_after, _ = auth_mod.login_policy_check(username)
    if action == "frozen":
        fi = auth_mod.get_frozen_info(username)
        if fi.get("frozen_by") == "admin":
            # 管理员手动冻结：拒绝登录 + 提示联系管理员（含原因）
            detail = "账号异常请联系系统管理员处理！"
            if fi.get("frozen_reason"):
                detail += f"（原因：{fi['frozen_reason']}）"
            return jsonify({
                "detail": detail,
                "frozen_by": "admin",
                "frozen_reason": fi.get("frozen_reason") or "",
                "admin_email": (list(auth_mod.ADMIN_EMAILS) or [""])[0],
                "request_id": rid,
            }), 423
        return jsonify({
            "detail": "该账号已临时冻结。若你是账号本人，请在登录页使用正确密码 + 邮箱验证码自助解冻；如需帮助请联系管理员。",
            "frozen_by": "auto",
            "request_id": rid,
        }), 423
    if action == "cooldown":
        retry_min = max(1, int(retry_after // 60) + 1)
        resp = jsonify({"detail": f"尝试过于频繁，请在 {retry_min} 分钟后重试。", "request_id": rid}), 429
        resp[0].headers["Retry-After"] = str(int(retry_after) + 1)
        return resp

    # 2. 校验密码
    user = auth_mod.authenticate_user(username, password)
    if not user:
        frozen_now, _ = auth_mod.record_login_failure(username, ip)
        auth_mod.record_ip_login_failure(ip)
        if frozen_now:
            logger.warning("[auth] login froze account username=%s", username)
            return jsonify({
                "detail": "该账号已临时冻结。若你是账号本人，请在登录页使用正确密码 + 邮箱验证码自助解冻；如需帮助请联系管理员。",
                "request_id": rid,
            }), 423
        return jsonify({"detail": "用户名或密码错误。", "request_id": rid}), 401

    # 3. 登录成功：清零失败记录 → 签发 JWT → 记录使用次数
    auth_mod.clear_login_failures(username)
    auth_mod.record_user_usage(user["user_id"], "login")
    token = auth_mod.generate_jwt(user["user_id"], user["username"])
    return jsonify({
        "request_id": rid,
        "data": {
            "user_id": user["user_id"],
            "username": user["username"],
            "token": token,
        },
    })


@app.post("/api/v1/auth/invite-code/check")
def invite_code_check():
    """
    非消费校验邀请码（注册页前置解锁）。body: {invite_code}
    只做存在性/过期/已用校验（不校验邮箱，因为此阶段用户还没填邮箱）。
    不区分失败原因，统一文案，防枚举。
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    invite_code = (data.get("invite_code") or "").strip()
    if not invite_code:
        return jsonify({"detail": "请填写邀请码。", "request_id": rid}), 422
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    ip_hash = auth_mod._ip_hash(ip)
    limited, _retry = auth_mod._rate_limited(f"invite_check_ip:{ip_hash}", max_count=10, window=3600)
    if limited:
        return jsonify({"detail": "尝试次数过多，请稍后再试。", "request_id": rid}), 429
    check = auth_mod.validate_invite_code(invite_code)
    if check is not None:
        return jsonify({"detail": "邀请码无效或不可用，请联系管理员获取新的邀请码。", "request_id": rid}), 422
    return jsonify({"request_id": rid, "data": {"valid": True}}), 200


@app.get("/api/v1/auth/register-config")
def register_config():
    """注册页配置：invite_required（当前固定 True，未来可关）。"""
    return jsonify({
        "request_id": auth_mod._request_id("auth"),
        "data": {
            "invite_required": True,
            "contact_email": auth_mod.CONTACT_EMAIL,
            "invite_code_length": config.INVITE_CODE_LENGTH,
        },
    })


@app.post("/api/v1/invite-request")
def invite_request():
    """提交邀请码申请（公开）。body: {email, note}"""
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    note = (data.get("note") or "").strip()
    if not email or not note:
        return jsonify({"detail": "请填写邮箱和申请理由。", "request_id": rid}), 422
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    request_rec, error, status = auth_mod.create_invite_request(email, note, ip)
    if error:
        return jsonify({"detail": error, "request_id": rid}), status
    logger.info("[invite] request submitted email=%s", email)
    return jsonify({
        "request_id": rid,
        "data": {
            "message": "申请已提交，管理员审批通过后邀请码将发送至你的邮箱。",
            "email": email,
        },
    }), 200


@app.post("/api/v1/auth/unfreeze/send-code")
def unfreeze_send_code():
    """
    自助解冻第一步：为冻结账号的绑定邮箱发送解冻验证码（公开）。
    body: {username, email}
    - 账号不存在 → 404
    - 邮箱与账号绑定邮箱不匹配 / 账号未绑定邮箱 → 422（提示联系管理员）
    - 账号未冻结 → 409
    - 通过后复用邮箱验证码机制（PURPOSE_UNFREEZE，60s 冷却 / 30 分钟有效）
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not username or not email:
        return jsonify({"detail": "请提供用户名和邮箱。", "request_id": rid}), 422
    ok, error, status = auth_mod.request_unfreeze_code(username, email)
    if not ok:
        return jsonify({"detail": error, "request_id": rid}), status
    if not auth_mod.can_request_code(email, auth_mod.PURPOSE_UNFREEZE):
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    if not mailer_mod.smtp_available():
        return jsonify({"detail": "邮件服务未配置，请联系管理员处理。", "request_id": rid}), 503
    code = auth_mod.create_email_code(email, auth_mod.PURPOSE_UNFREEZE)
    if not code:
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
    try:
        mailer_mod.send_verification_email(email, code, purpose="账号解冻")
    except Exception as exc:
        logger.error("send unfreeze code failed for %s: %s", email, exc)
        return jsonify({"detail": "邮件发送失败，请稍后重试或联系管理员。", "request_id": rid}), 502
    logger.info("[auth] unfreeze code sent email=%s username=%s", email, username)
    return jsonify({"detail": "解冻验证码已发送，请查收邮件。", "request_id": rid}), 200


@app.post("/api/v1/auth/unfreeze")
def unfreeze():
    """
    冻结账号自助解冻（公开）。body: {username, password, email, code}
    正确密码 + 绑定邮箱验证码 → 解冻并直接返回新 JWT。
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    code = (data.get("code") or "").strip()
    if not username or not password or not email or not code:
        return jsonify({"detail": "请提供用户名、密码、邮箱和邮箱验证码。", "request_id": rid}), 422

    # 该账号必须处于冻结状态
    action, _, _ = auth_mod.login_policy_check(username)
    if action != "frozen":
        return jsonify({"detail": "该账号未被冻结，无需解冻。", "request_id": rid}), 409

    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    ip_limited, _ip_retry = auth_mod.ip_login_rate_exceeded(ip)
    if ip_limited:
        return jsonify({"detail": "尝试次数过多，请稍后再试。", "request_id": rid}), 429

    # 密码是本人的第一证明（错误也计入窗口失败，防绕过登录限流）
    user = auth_mod.authenticate_user(username, password)
    if not user:
        frozen_now, _ = auth_mod.record_login_failure(username, ip)
        auth_mod.record_ip_login_failure(ip)
        if frozen_now:
            return jsonify({"detail": "自助解冻失败：密码或验证码有误。", "request_id": rid}), 401
        return jsonify({"detail": "密码错误，无法自助解冻。", "request_id": rid}), 401

    ok, error = auth_mod.unfreeze_by_email(username, email, code)
    if not ok:
        # 验证码失败不额外记登录失败（用户已证明密码正确）
        return jsonify({"detail": error, "request_id": rid}), 400

    auth_mod.clear_login_failures(username)
    token = auth_mod.generate_jwt(user["user_id"], user["username"])
    logger.info("[auth] account unfrozen via email username=%s", username)
    # 清除本 IP 的登录失败计数（本人自助解冻成功 → 该 IP 恢复）
    auth_mod._safe_remove(
        auth_mod._rate_file_path(f"login_ip:{auth_mod._ip_hash(ip)}")
    )
    return jsonify({
        "request_id": rid,
        "data": {"message": "账号已解冻，欢迎回来。", "token": token, "user_id": user["user_id"], "username": user["username"]},
    }), 200


@app.get("/api/v1/auth/me")
def auth_me():
    """返回当前登录用户信息（含 is_admin，供前端显示管理入口）。"""
    auth_user = g.get("auth_user")
    if not auth_user:
        return jsonify({"detail": "请先登录。", "request_id": "auth:me"}), 401
    email = auth_user.get("email") or ""
    return jsonify({
        "request_id": f"auth:me:{auth_user['user_id'][:8]}",
        "data": {
            "user_id": auth_user["user_id"],
            "username": auth_user["username"],
            "email": email,
            "is_admin": bool(auth_mod.is_admin(auth_user)),
            "is_super_admin": bool(auth_mod.is_super_admin(auth_user)),
        },
    })


@app.post("/api/v1/auth/change-password")
def change_password():
    """修改当前登录用户的密码。需验证旧密码；成功后旧 token 全部失效。"""
    rid = auth_mod._request_id("auth")
    auth_user = g.get("auth_user")
    if not auth_user:
        return jsonify({"detail": "请先登录。", "request_id": rid}), 401

    data = request.get_json(silent=True) or {}
    old_password = (data.get("old_password") or "")
    new_password = (data.get("new_password") or "")

    if not old_password or not new_password:
        return jsonify({"detail": "请提供旧密码和新密码。", "request_id": rid}), 422
    if old_password == new_password:
        return jsonify({"detail": "新密码不能与旧密码相同。", "request_id": rid}), 422

    # 校验旧密码（用当前登录用户名走一次完整认证，防止 token 持有者越权改密）
    authed = auth_mod.authenticate_user(auth_user["username"], old_password)
    if not authed:
        return jsonify({"detail": "旧密码错误。", "request_id": rid}), 401

    ok, error = auth_mod.update_password(auth_user["user_id"], new_password)
    if not ok:
        return jsonify({"detail": error, "request_id": rid}), 422

    # 密码已变更：旧 token 因 pwd_ver 不匹配全部失效，前端应跳回登录页
    return jsonify({
        "request_id": rid,
        "data": {"message": "密码修改成功，请重新登录。", "require_relogin": True},
    })


@app.post("/api/v1/auth/reset-password/request")
def reset_password_request():
    """
    忘记密码 · 第一步：提交邮箱 → 生成 6 位重置验证码并发邮件。
    为避免用户枚举，邮箱不存在或未配置 SMTP 时返回相同的 200 文案。
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"detail": "请提供注册邮箱。", "request_id": rid}), 422

    generic_msg = "如果该邮箱已注册，重置验证码已发送，请查收。"

    user = auth_mod.find_user_by_email(email)
    if not user or not user.get("email"):
        # 用户不存在 / 该账号未登记邮箱：返回同样文案，防止邮箱枚举
        logger.info("reset request for unknown email=%s", email)
        return jsonify({"detail": generic_msg, "request_id": rid}), 200

    # 管理员冻结：禁止重置密码（防通过改密绕过冻结）
    fi = auth_mod.get_frozen_info(user["username"])
    if fi.get("frozen") and fi.get("frozen_by") == "admin":
        return jsonify({
            "detail": "账号异常请联系系统管理员处理！",
            "admin_email": (list(auth_mod.ADMIN_EMAILS) or [""])[0],
            "request_id": rid,
        }), 423

    if not mailer_mod.smtp_available():
        return jsonify({
            "detail": "邮件服务未配置，请联系管理员处理。",
            "request_id": rid,
        }), 503

    # 频率限制：防止对同一邮箱爆破/骚扰
    if not auth_mod.can_request_code(email, auth_mod.PURPOSE_RESET_PASSWORD):
        return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429

    try:
        code = auth_mod.create_email_code(email, auth_mod.PURPOSE_RESET_PASSWORD)
        if not code:
            return jsonify({"detail": "发送过于频繁，请稍后再试。", "request_id": rid}), 429
        mailer_mod.send_password_reset_email(email, user["username"], code)
    except Exception as exc:
        logger.error("reset request failed for %s: %s", email, exc)
        return jsonify({
            "detail": "重置邮件发送失败，请稍后重试或联系管理员。",
            "request_id": rid,
        }), 502

    logger.info("reset code generation/email done for user_id=%s", user["user_id"])
    return jsonify({"detail": generic_msg, "request_id": rid}), 200


@app.post("/api/v1/auth/reset-password/confirm")
def reset_password_confirm():
    """
    忘记密码 · 第二步：提交邮箱 + 6 位验证码 + 新密码 → 重置成功，旧 token 吊销。
    """
    rid = auth_mod._request_id("auth")
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = (data.get("new_password") or "")
    if not email or not code or not new_password:
        return jsonify({"detail": "请提供邮箱、验证码和新密码。", "request_id": rid}), 422

    # 管理员冻结：禁止通过重置密码改变登录状态
    frozen_user = auth_mod.find_user_by_email(email)
    if frozen_user:
        fi = auth_mod.get_frozen_info(frozen_user["username"])
        if fi.get("frozen") and fi.get("frozen_by") == "admin":
            return jsonify({
                "detail": "账号异常请联系系统管理员处理！",
                "admin_email": (list(auth_mod.ADMIN_EMAILS) or [""])[0],
                "request_id": rid,
            }), 423

    ok, error = auth_mod.reset_password_with_code(email, code, new_password)
    if not ok:
        return jsonify({"detail": error, "request_id": rid}), 400

    logger.info("password reset completed via email code")
    return jsonify({
        "request_id": rid,
        "data": {"message": "密码重置成功，请使用新密码登录。"},
    })


# ════════════════════════════════════════════════════════════════════
# 管理员接口（require_auth 内已做白名单鉴权：路径前缀 /api/v1/admin/）
# —— 邀请码申请审批 / 邀请码管理 / 冻结账号管理
# ════════════════════════════════════════════════════════════════════

def _client_ip() -> str:
    """统一取客户端 IP（兼容反代 X-Forwarded-For）。"""
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )


def _mask_email(email: str) -> str:
    """邮箱脱敏：l***@163.com。"""
    email = str(email or "")
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _admin_user_id() -> str:
    auth_user = g.get("auth_user") or {}
    return auth_user.get("user_id") or ""


def _request_payload_serialize(request_id: str) -> dict:
    return {"request_id": request_id}


@app.get("/api/v1/admin/invite-requests")
def admin_list_invite_requests():
    """申请单列表。query: status=pending|approved|rejected（默认 pending）"""
    rid = _request_id("admin")
    status = (request.args.get("status") or "pending").strip()
    if status not in ("pending", "approved", "rejected"):
        status = "pending"
    records = auth_mod.list_invite_requests(status)
    items = []
    for rec in records:
        items.append(
            {
                "request_id": rec.get("request_id"),
                "email": _mask_email(rec.get("email") or ""),
                "note": rec.get("note") or "",
                "status": rec.get("status"),
                "created_at": rec.get("created_at"),
                "reviewed_at": rec.get("reviewed_at"),
                "reject_reason": rec.get("reject_reason") or "",
                "code_sent": bool(rec.get("code_sent")),
                "rejected_count": auth_mod.count_rejected_requests(rec.get("email") or ""),
            }
        )
    return jsonify({"request_id": rid, "data": {"items": items, "total": len(items)}})


@app.post("/api/v1/admin/invite-requests/<request_id>/approve")
def admin_approve_invite_request(request_id: str):
    """审批通过：生成一次性邀请码（绑定申请邮箱）→ SMTP 自动发送。"""
    rid = _request_id("admin")
    req, code, status = auth_mod.approve_invite_request(request_id, _admin_user_id())
    if status != 200:
        return jsonify({"detail": req or "操作失败。", "request_id": rid}), status
    # SMTP 发送邀请码邮件（失败不撤销审批；管理页可补发）
    email = (req.get("email") or "").strip()
    try:
        if not mailer_mod.smtp_available():
            logger.warning("[invite] approve request=%s SMTP unavailable, code=%s", request_id, code)
            return jsonify({
                "detail": "已生成邀请码，但邮件服务未配置，无法自动发送。请先配置 SMTP 后使用补发功能；或直接复制下方邀请码告知申请人。",
                "request_id": rid,
                "data": {"invite_code": code, "code_sent": False},
            }), 200
        mailer_mod.send_invite_code_email(
            email, code, expires_hours=config.INVITE_CODE_TTL_HOURS
        )
        auth_mod.mark_invite_request_sent(request_id)
    except Exception as exc:
        logger.error("approve send invite email failed for %s: %s", email, exc)
        return jsonify({
            "detail": "审批已通过，但邀请码邮件发送失败。请点击补发，或直接复制下方邀请码告知申请人。",
            "request_id": rid,
            "data": {"invite_code": code, "code_sent": False},
        }), 200
    logger.info("[invite] approved request=%s email=%s by=%s", request_id, email, _admin_user_id())
    return jsonify({
        "request_id": rid,
        "data": {
            "message": "审批已通过，邀请码已发送至申请邮箱。",
            "request_id_info": request_id,
            "invite_code": code,
            "code_sent": True,
        },
    }), 200


@app.post("/api/v1/admin/invite-requests/<request_id>/reject")
def admin_reject_invite_request(request_id: str):
    """拒绝申请：记录原因；reason 非空 → 发拒信邮件。"""
    rid = _request_id("admin")
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    req, error, status = auth_mod.reject_invite_request(request_id, _admin_user_id(), reason)
    if status != 200:
        return jsonify({"detail": error or "操作失败。", "request_id": rid}), status
    email = (req.get("email") or "").strip()
    if reason:
        try:
            if mailer_mod.smtp_available():
                mailer_mod.send_invite_rejection_email(email, reason)
        except Exception as exc:
            # 拒信失败不影响拒绝结果（best-effort），降级返回成功并记日志
            logger.error("reject email failed for %s: %s", email, exc)
    rejected_total = auth_mod.count_rejected_requests(email)
    logger.info("[invite] rejected request=%s email=%s by=%s total_rejected=%d", request_id, email, _admin_user_id(), rejected_total)
    return jsonify({
        "request_id": rid,
        "data": {
            "message": "已拒绝该申请。",
            "rejected_total": rejected_total,
            "max_rejections": config.INVITE_REQUEST_MAX_REJECTIONS,
        },
    }), 200


@app.post("/api/v1/admin/invite-requests/<request_id>/resend")
def admin_resend_invite_request(request_id: str):
    """补发邀请码邮件（原码明文已不可得 → 重新生成并发送，旧码作废）。"""
    rid = _request_id("admin")
    req, code, status = auth_mod.resend_invite_request(request_id)
    if status != 200:
        return jsonify({"detail": req or "操作失败。", "request_id": rid}), status
    email = (req.get("email") or "").strip()
    try:
        if not mailer_mod.smtp_available():
            return jsonify({
                "detail": "邮件服务未配置，无法补发。请先配置 SMTP。",
                "request_id": rid,
            }), 503
        mailer_mod.send_invite_code_email(
            email, code, expires_hours=config.INVITE_CODE_TTL_HOURS
        )
        auth_mod.mark_invite_request_sent(request_id)
    except Exception as exc:
        logger.error("resend invite email failed for %s: %s", email, exc)
        # 补发失败也返回明文码，便于管理员人工转达
        return jsonify({
            "detail": "补发邮件失败，请稍后重试，或直接复制下方邀请码告知申请人。",
            "request_id": rid,
            "data": {"invite_code": code},
        }), 200
    logger.info("[invite] resent request=%s email=%s", request_id, email)
    return jsonify({"request_id": rid, "data": {"message": "邀请码已重新生成并发送至申请邮箱。"}}), 200


@app.get("/api/v1/admin/invite-codes")
def admin_list_invite_codes():
    """邀请码总览（不回明文）。query: status=active|used|expired|all"""
    rid = _request_id("admin")
    status = (request.args.get("status") or "all").strip()
    records = auth_mod.list_invite_codes()
    now_ts = auth_mod._now_ts()
    items = []
    for rec in records:
        used = bool(rec.get("used"))
        expired = (not used) and float(rec.get("expires_at", 0)) < now_ts
        rec_status = "used" if used else ("expired" if expired else "active")
        if status != "all" and rec_status != status:
            continue
        items.append(
            {
                # 完整哈希返回（作废接口需用完整哈希定位；展示截断由前端负责）
                "code_hash": rec.get("code_hash") or "",
                "status": rec_status,
                "bound_email": _mask_email(rec.get("bound_email") or ""),
                "created_at": rec.get("created_at"),
                # expires_at 统一转 ISO 字符串（历史记录为 epoch 秒），避免前端按毫秒误解析为 1970
                "expires_at": _epoch_or_iso(rec.get("expires_at")),
                "used_by_username": rec.get("used_by_username") or None,
                "note": rec.get("note") or "",
                "revoked": bool(rec.get("revoked")),
            }
        )
    return jsonify({"request_id": rid, "data": {"items": items, "total": len(items)}})


def _epoch_or_iso(value):
    """兼容：历史记录 expires_at 为 epoch 秒（float），统一输出为 ISO 字符串。"""
    if value is None:
        return None
    try:
        as_float = float(value)
        if as_float > 10**12:  # 已是毫秒级时间戳
            return datetime.fromtimestamp(as_float / 1000, timezone.utc).isoformat()
        return datetime.fromtimestamp(as_float, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


@app.post("/api/v1/admin/invite-codes/generate")
def admin_generate_invite_codes():
    """
    兜底手动生成邀请码（应急/活动）。body: {count, note, days?, email?}
    email 缺省时不绑定（任何邮箱可用——仅用于管理员应急首号注册等场景）。
    """
    rid = _request_id("admin")
    data = request.get_json(silent=True) or {}
    try:
        count = max(1, min(int(data.get("count") or 1), 20))
    except (TypeError, ValueError):
        count = 1
    note = (data.get("note") or "").strip()[:200]
    email = (data.get("email") or "").strip().lower() or ""
    days = config.INVITE_CODE_TTL_HOURS / 24
    codes = []
    for _i in range(count):
        request_id = f"manual-{uuid.uuid4()}"
        code = auth_mod.generate_invite_code(
            bound_email=email,  # 空 = 不绑定
            request_id=request_id,
            created_by=_admin_user_id(),
            note=note,
            ttl_hours=int(days * 24) if days else config.INVITE_CODE_TTL_HOURS,
        )
        codes.append(code)
    logger.info("[invite] manual generate count=%d by=%s", count, _admin_user_id())
    return jsonify({
        "request_id": rid,
        "data": {
            "codes": codes,  # 明文仅此一次返回
            "message": "已生成邀请码，请立即复制保存（服务器不保存明文）。",
        },
    }), 200


@app.post("/api/v1/admin/invite-codes/<path:code_or_hash>/revoke")
def admin_revoke_invite_code(code_or_hash: str):
    """作废未使用邀请码（入参可为明文码或哈希）。"""
    rid = _request_id("admin")
    code = code_or_hash.strip()
    ok = auth_mod.revoke_invite_code(code)
    if not ok:
        # 尝试按哈希直接删（仅当入参已是哈希且活跃码存在）
        return jsonify({"detail": "邀请码不存在、已使用或已作废。", "request_id": rid}), 404
    return jsonify({"request_id": rid, "data": {"message": "邀请码已作废。"}}), 200


@app.get("/api/v1/admin/users")
def admin_list_users():
    """用户列表（管理员）：支持 ?keyword= 搜索用户名/邮箱，?page=&size= 分页。"""
    rid = _request_id("admin")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(200, max(1, int(request.args.get("size", "50"))))
    except (TypeError, ValueError):
        size = 50
    keyword = (request.args.get("keyword") or "").strip()
    items = auth_mod.list_users(keyword=keyword, page=page, size=size)
    total = auth_mod.count_users(keyword=keyword)
    return jsonify({
        "request_id": rid,
        "data": {"items": items, "total": total, "page": page, "size": size},
    })


@app.post("/api/v1/admin/users")
def admin_create_user():
    """管理员创建用户（绕过邀请码）。body: {username, password, email?, is_admin?}
    is_admin 仅超级管理员（.env 白名单邮箱）可指定；普通管理员创建的用户不带管理员身份。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    is_admin_flag = bool(data.get("is_admin"))
    if not username or not password:
        return jsonify({"detail": "请提供用户名和密码。", "request_id": rid}), 422

    # 权限分级：仅超级管理员可创建管理员账号
    if is_admin_flag and not auth_mod.is_super_admin(me):
        return jsonify({
            "detail": "仅超级管理员（admin 账号）可以创建管理员。",
            "request_id": rid,
        }), 403

    user, error = auth_mod.create_user(username, password, email)
    if error:
        return jsonify({"detail": error, "request_id": rid}), 409 if "已被" in error else 422

    # 设置管理员标记（仅超级管理员路径可达）
    if is_admin_flag:
        ok, err = auth_mod.update_user_admin(username, True, operator=_admin_user_id())
        if not ok:
            # 创建成功但设管理员失败（白名单用户已默认管理员）→ 忽略
            pass
    auth_mod.record_admin_op(
        "user_create",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"创建用户 {username}" + ("（管理员）" if is_admin_flag else ""),
        request_id=rid,
    )
    user = auth_mod.find_user_by_username(username)
    return jsonify({
        "request_id": rid,
        "data": {
            "message": f"用户 {username} 创建成功。",
            "user": {
                "user_id": user.get("user_id") if user else None,
                "username": username,
                "email": (user or {}).get("email") or "",
                "is_admin": auth_mod.is_admin(user),
            },
        },
    }), 201


@app.patch("/api/v1/admin/users/<username>")
def admin_update_user(username: str):
    """修改用户：email（绑定邮箱）/ is_admin（管理员标记）。body 二选一或都要。
    is_admin 变更仅超级管理员可操作；普通管理员只能改邮箱。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    data = request.get_json(silent=True) or {}
    user = auth_mod.find_user_by_username(username)
    if not user:
        return jsonify({"detail": "用户不存在。", "request_id": rid}), 404

    # 权限分级①：is_admin 变更仅超级管理员；管理员身份账号的改邮箱也仅超级管理员
    if "is_admin" in data and not is_super_admin():
        return jsonify({
            "detail": "仅超级管理员（admin 账号）可以分配或取消管理员权限。",
            "request_id": rid,
        }), 403
    if "email" in data:
        _ensure_super_admin_for_admin_target(rid, user)

    # 防锁死：不允许降级最后一个管理员（白名单邮箱管理员除外）
    if "is_admin" in data and not data.get("is_admin"):
        target_email = (user.get("email") or "").strip().lower()
        if target_email not in auth_mod.ADMIN_EMAILS:
            remaining = [
                u
                for u in auth_mod.list_users(page=1, size=10**9)
                if u.get("is_admin") and u.get("username") != username
            ]
            if not remaining:
                return jsonify({
                    "detail": "不能取消最后一个管理员，系统将失去管理入口。",
                    "request_id": rid,
                }), 400

    if "email" in data:
        old_email = (user.get("email") or "").strip() or "（无）"
        new_email = (data.get("email") or "").strip()
        ok, err = auth_mod.admin_update_email(username, new_email, operator=_admin_user_id())
        if not ok:
            return jsonify({"detail": err, "request_id": rid}), 422
        auth_mod.record_admin_op(
            "email_update",
            _admin_user_id(),
            me.get("username") or "",
            username,
            detail=f"{username} 的绑定邮箱：{old_email} → {new_email}",
            request_id=rid,
        )

    if "is_admin" in data:
        target = bool(data.get("is_admin"))
        ok, err = auth_mod.update_user_admin(username, target, operator=_admin_user_id())
        if not ok:
            return jsonify({"detail": err, "request_id": rid}), 400
        auth_mod.record_admin_op(
            "admin_grant" if target else "admin_revoke",
            _admin_user_id(),
            me.get("username") or "",
            username,
            detail=f"{'授予' if target else '取消'}管理员权限",
            request_id=rid,
        )

    user = auth_mod.find_user_by_username(username)
    return jsonify({
        "request_id": rid,
        "data": {
            "message": f"用户 {username} 已更新。",
            "user": {
                "user_id": user.get("user_id"),
                "username": username,
                "email": (user or {}).get("email") or "",
                "is_admin": auth_mod.is_admin(user),
            },
        },
    }), 200


@app.post("/api/v1/admin/users/<username>/reset-password")
def admin_reset_password(username: str):
    """管理员重置用户密码：生成临时密码并返回，用户旧 token 全部失效。
    管理员身份账号仅超级管理员可重置。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    target = auth_mod.find_user_by_username(username)
    if not target:
        return jsonify({"detail": "用户不存在。", "request_id": rid}), 404
    _ensure_super_admin_for_admin_target(rid, target)
    ok, err, temp = auth_mod.admin_reset_password(username, operator=_admin_user_id())
    if not ok:
        return jsonify({"detail": err, "request_id": rid}), 404 if err == "用户不存在" else 422
    auth_mod.record_admin_op(
        "pwd_reset",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"重置密码",
        request_id=rid,
    )
    return jsonify({
        "request_id": rid,
        "data": {
            "message": f"用户 {username} 的密码已重置。",
            "temp_password": temp,  # 明文仅本次响应返回一次
        },
    }), 200


@app.delete("/api/v1/admin/users/<username>")
def admin_delete_user(username: str):
    """删除用户（软删除，90 天内可恢复）。
    权限：仅超级管理员（.env 白名单邮箱）可操作；需校验操作者管理员密码。
    禁止删除自己。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    if (me.get("username") or "").lower() == username.lower():
        return jsonify({"detail": "不能删除当前登录的管理员账号。", "request_id": rid}), 400

    # 权限收紧：仅超级管理员可删除任何账号
    if not auth_mod.is_super_admin(me):
        return jsonify({
            "detail": "仅超级管理员（admin 账号）可以删除账号。",
            "request_id": rid,
        }), 403

    user = auth_mod.find_user_by_username(username)
    if not user:
        return jsonify({"detail": "用户不存在。", "request_id": rid}), 404

    # 删除需验证操作者（超级管理员）密码
    data = request.get_json(silent=True) or {}
    admin_password = (data.get("admin_password") or "").strip()
    if not admin_password:
        return jsonify({"detail": "请填写管理员密码以确认删除操作。", "request_id": rid}), 422
    if not auth_mod.authenticate_user(me.get("username") or "", admin_password):
        return jsonify({"detail": "管理员密码验证失败，删除已取消。", "request_id": rid}), 403

    # 防锁死：不允许删除最后一个管理员（白名单邮箱管理员除外）
    target_email = (user.get("email") or "").strip().lower()
    if auth_mod.is_admin(user) and target_email not in auth_mod.ADMIN_EMAILS:
        remaining = [
            u
            for u in auth_mod.list_users(page=1, size=10**9)
            if u.get("is_admin") and u.get("username") != username
        ]
        if not remaining:
            return jsonify({
                "detail": "不能删除最后一个管理员，系统将失去管理入口。",
                "request_id": rid,
            }), 400

    # 记录删除审计（先留痕再删，用户名即使文件删除后仍可查）
    auth_mod.record_admin_op(
        "user_delete",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"软删除用户 {username}（90 天内可恢复）",
        request_id=rid,
    )
    if not auth_mod.delete_user(user["user_id"], operator=_admin_user_id()):
        return jsonify({"detail": "删除失败，用户不存在。", "request_id": rid}), 404
    logger.info("[admin] user soft-deleted username=%s by=%s", username, _admin_user_id())
    return jsonify({"request_id": rid, "data": {"message": f"用户 {username} 已删除（90 天内可恢复）。"}}), 200


@app.get("/api/v1/admin/users/deleted")
def admin_list_deleted_users():
    """列出可恢复的软删除账号（删除时间在 90 天恢复窗口内），按删除时间倒序。"""
    rid = _request_id("admin")
    items = auth_mod.list_deleted_users()
    return jsonify({"request_id": rid, "data": {"items": items, "total": len(items)}})


@app.post("/api/v1/admin/users/<username>/restore")
def admin_restore_user(username: str):
    """恢复软删除账号（仅删除后 90 天内）。恢复后需重新登录。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    ok, err = auth_mod.restore_user(username)
    if not ok:
        status = 404 if err == "用户不存在" else 400
        return jsonify({"detail": err, "request_id": rid}), status
    auth_mod.record_admin_op(
        "user_restore",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"恢复用户 {username}",
        request_id=rid,
    )
    return jsonify({"request_id": rid, "data": {"message": f"用户 {username} 已恢复。"}}), 200


@app.get("/api/v1/admin/ops")
def admin_list_ops():
    """管理操作记录（删除/创建/权限变更/重置密码 等审计）。op 精确过滤 + keyword 搜索操作者/目标。"""
    rid = _request_id("admin")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(200, max(1, int(request.args.get("size", "50"))))
    except (TypeError, ValueError):
        size = 50
    op = (request.args.get("op") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    items = auth_mod.list_admin_ops(op=op, keyword=keyword, page=page, size=size)
    total = auth_mod.count_admin_ops(op=op, keyword=keyword)
    return jsonify({
        "request_id": rid,
        "data": {"items": items, "total": total, "page": page, "size": size},
    })


@app.get("/api/v1/admin/ops/export")
def admin_ops_export():
    """审计导出：csv（Excel 兼容，utf-8-sig）或 json。遵循当前 op/keyword 过滤，导出全部结果不受分页限制。"""
    rid = _request_id("admin")
    op = (request.args.get("op") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    fmt = (request.args.get("format") or "csv").strip().lower()
    if fmt not in ("csv", "json"):
        fmt = "csv"
    items = auth_mod.list_admin_ops(op=op, keyword=keyword, page=1, size=10**9)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if fmt == "json":
        payload = json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(), "items": items},
            ensure_ascii=False,
        )
        return Response(
            payload,
            status=200,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="admin-ops-{stamp}.json"',
                "Content-Type": "application/json; charset=utf-8",
            },
        )
    # CSV：utf-8-sig BOM，Excel 双击直接打开不乱码
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间(UTC)", "操作类型", "操作者", "目标用户", "详情", "request_id"])
    for rec in items:
        writer.writerow([
            rec.get("created_at", ""),
            rec.get("op", ""),
            rec.get("operator_name", ""),
            rec.get("target_username", ""),
            rec.get("detail", ""),
            rec.get("request_id", ""),
        ])
    data = "\ufeff" + buf.getvalue()
    return Response(
        data,
        status=200,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="admin-ops-{stamp}.csv"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


@app.get("/api/v1/admin/users/<username>/usage")
def admin_user_usage(username: str):
    """用户使用次数（登录 / HR 分析）聚合统计。granularity=day|month|year&buckets=N。"""
    rid = _request_id("admin")
    user = auth_mod.find_user_by_username(username)
    if not user:
        return jsonify({"detail": "用户不存在。", "request_id": rid}), 404
    granularity = (request.args.get("granularity") or "day").strip()
    try:
        buckets = max(1, min(int(request.args.get("buckets", "30")), 3660))
    except (TypeError, ValueError):
        buckets = 30
    if granularity == "month":
        buckets = min(buckets, 240)
    elif granularity == "year":
        buckets = min(buckets, 10)
    result = auth_mod.get_user_usage(user["user_id"], granularity=granularity, buckets=buckets)
    return jsonify({"request_id": rid, "data": {"username": username, **result}})


@app.get("/api/v1/admin/users/usage-ranking")
def admin_usage_ranking():
    """全用户使用排行：按最近使用总量（登录+分析）降序，帮助发现高消耗账号。"""
    rid = _request_id("admin")
    try:
        limit = max(1, min(int(request.args.get("limit", "20")), 200))
    except (TypeError, ValueError):
        limit = 20
    items = auth_mod.usage_ranking(limit=limit)
    return jsonify({"request_id": rid, "data": {"items": items, "total": len(items)}})


@app.get("/api/v1/admin/users/frozen")
def admin_list_frozen_users():
    """冻结账号列表。"""
    rid = _request_id("admin")
    items = auth_mod.list_frozen_users()
    return jsonify({"request_id": rid, "data": {"items": items, "total": len(items)}})


@app.post("/api/v1/admin/users/<username>/freeze")
def admin_freeze_user(username: str):
    """管理员手动冻结用户（防异常消耗 token 等）。
    body: {reason?}。冻结后拒绝登录/重置密码/自助解冻。管理员身份账号仅超级管理员可冻结。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    target = auth_mod.find_user_by_username(username)
    if not target:
        return jsonify({"detail": "用户不存在。", "request_id": rid}), 404
    _ensure_super_admin_for_admin_target(rid, target)

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    ok, err = auth_mod.freeze_user(username, operator=_admin_user_id(), reason=reason)
    if not ok:
        return jsonify({"detail": err, "request_id": rid}), 422
    auth_mod.record_admin_op(
        "user_freeze",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"冻结用户 {username}" + (f"（原因:{reason}）" if reason else ""),
        request_id=rid,
    )
    return jsonify({
        "request_id": rid,
        "data": {"message": f"账号 {username} 已冻结，该用户将无法登录与重置密码。"},
    }), 200


@app.post("/api/v1/admin/users/<username>/unfreeze")
def admin_unfreeze_user(username: str):
    """管理员兜底解冻（管理员身份账号仅超级管理员可解冻）。"""
    rid = _request_id("admin")
    me = g.get("auth_user") or {}
    target = auth_mod.find_user_by_username(username)
    if not target:
        return jsonify({"detail": "解冻失败，用户不存在。", "request_id": rid}), 404
    _ensure_super_admin_for_admin_target(rid, target)

    ok = auth_mod.unfreeze_user(username, operator=f"admin:{_admin_user_id()}")
    if not ok:
        return jsonify({"detail": "解冻失败，用户不存在。", "request_id": rid}), 404
    auth_mod.record_admin_op(
        "user_unfreeze",
        _admin_user_id(),
        me.get("username") or "",
        username,
        detail=f"解冻用户 {username}",
        request_id=rid,
    )
    return jsonify({"request_id": rid, "data": {"message": f"账号 {username} 已解冻。"}}), 200


@app.post("/api/v1/ai/test")
def test_ai_model():
    """Verify a browser-supplied model configuration without persisting it."""
    data = request.get_json(silent=True) or {}
    ai_config = _request_ai_config(data, required=True)
    started = time.perf_counter()
    try:
        llm.call_llm(
            "这是一次连接测试。请只回复 OK。",
            max_tokens=8,
            runtime_config=ai_config,
        )
    except Exception as exc:
        logger.warning("AI model connection test failed: %s", type(exc).__name__)
        raise ApiError(
            "AI 模型连接失败，请检查 API Key、接口地址与模型名称。",
            502,
            "ai",
        ) from exc

    return jsonify(
        {
            "request_id": _request_id("ai"),
            "data": {
                "ok": True,
                "provider": ai_config["provider"],
                "model": ai_config["model"],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            },
        }
    )


# ════════════════════════════════════════════════════════════════════
# 简历接口
# ════════════════════════════════════════════════════════════════════
@app.post("/api/v1/resumes/upload")
def upload_resume():
    """上传 PDF/DOCX 简历，仅在本地解析文本并存储。"""
    rid = _request_id("resumes")

    f = request.files.get("file")
    if not f or not f.filename:
        return _err("No file provided", 400, "resumes")

    file_bytes = f.read()
    if len(file_bytes) > MAX_RESUME_FILE_SIZE:
        return _err("Uploaded file exceeds the 30 MB limit", 413, "resumes")
    filename = f.filename.lower()
    content_type = f.mimetype or f.content_type or ""
    # Windows clients and some security proxies upload Office files as
    # application/octet-stream. Confirm the type using extension and magic bytes.
    if filename.endswith(".pdf") and file_bytes.startswith(b"%PDF"):
        content_type = "application/pdf"
    elif filename.endswith(".docx") and file_bytes.startswith(b"PK"):
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # 1. 提取文本
    try:
        text = doc_parser.extract_text_from_file(file_bytes, content_type)
    except ValueError as e:
        logger.warning(f"resume parse failed: {e}")
        return _err(str(e), 400, "resumes")
    except Exception as e:
        logger.error(f"resume parse error: {e}", exc_info=True)
        return _err(f"File conversion failed: {e}", 400, "resumes")

    # 上传链路不调用 LLM，避免用户等待 60-90 秒；招聘分析阶段一次性处理。
    user_id = _current_user_id()
    resume_id = store.save_resume(content=text, processed={}, user_id=user_id)

    return jsonify(
        {
            "message": "Resume uploaded and processed as MD successfully",
            "request_id": rid,
            "resume_id": resume_id,
            "extracted_characters": len(text),
        }
    )


@app.post("/api/v1/resumes/improve")
def improve_resume():
    """分析简历 vs JD。?stream=true 走 SSE 流式。"""
    rid = _request_id("resumes")
    stream = request.args.get("stream", "false").lower() in ("true", "1", "yes")

    data = request.get_json(silent=True) or {}
    resume_id = data.get("resume_id")
    job_id = data.get("job_id")
    if not resume_id or not job_id:
        return _err("resume_id and job_id are required", 422, "resumes")
    ai_config = _request_ai_config(data)
    user_id = _current_user_id()

    if stream:
        return Response(
            stream_with_context(_improve_stream(resume_id, job_id, rid, user_id, ai_config)),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # 非流式：异常走全局 errorhandler，类型安全
    result = _do_improve(resume_id, job_id, rid, user_id, ai_config)
    return jsonify({"request_id": rid, "data": result})


def _do_improve(
    resume_id: str,
    job_id: str,
    rid: str,
    user_id: str,
    ai_config: dict | None = None,
) -> dict:
    """
    执行分析（核心逻辑，非流式与流式共用）。
    成功返回 dict；失败抛 ApiError，由全局 errorhandler 统一序列化。
    """
    resume = store.get_resume(resume_id, user_id=user_id)
    if not resume:
        raise ApiError(f"Resume not found: {resume_id}", 404, "resumes")
    job = store.get_job(job_id, user_id=user_id)
    if not job:
        raise ApiError(f"Job not found: {job_id}", 404, "resumes")

    try:
        prompt = PROMPT_HR_JUDGE.format(
            Job_Description=job.get("content", ""),
            raw_resume=resume.get("content", ""),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        analysis_result = llm.call_llm(
            prompt,
            expect_json=False,
            runtime_config=ai_config,
        )
    except ApiError:
        raise
    except Exception as e:
        logger.error(f"improve LLM call failed: {e}", exc_info=True)
        raise ApiError(f"Analysis failed: {e}", 500, "resumes") from e

    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "analysis_result": analysis_result,
        "details": "Analysis completed successfully using hr_judge prompt template.",
        "commentary": "The resume has been analyzed against the job description using the hr_judge prompt template.",
    }


def _improve_stream(
    resume_id: str,
    job_id: str,
    rid: str,
    user_id: str,
    ai_config: dict | None = None,
):
    """SSE 生成器。严格照搬旧版事件格式：data: {json}\\n\\n"""
    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        yield sse({"status": "starting", "message": "Analyzing resume and job description..."})

        resume = store.get_resume(resume_id, user_id=user_id)
        if not resume:
            yield sse({"status": "error", "message": f"Resume not found: {resume_id}"})
            return
        job = store.get_job(job_id, user_id=user_id)
        if not job:
            yield sse({"status": "error", "message": f"Job not found: {job_id}"})
            return

        yield sse({"status": "parsing", "message": "Preparing analysis with hr_judge prompt..."})

        prompt = PROMPT_HR_JUDGE.format(
            Job_Description=job.get("content", ""),
            raw_resume=resume.get("content", ""),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        yield sse({"status": "analyzing", "message": "Running analysis with LLM..."})

        analysis_result = llm.call_llm(
            prompt,
            expect_json=False,
            runtime_config=ai_config,
        )

        final_result = {
            "resume_id": resume_id,
            "job_id": job_id,
            "analysis_result": analysis_result,
            "details": "Analysis completed successfully using hr_judge prompt template.",
            "commentary": "The resume has been analyzed against the job description using the hr_judge prompt template.",
        }
        # completed 的 result 双层包装，与非流式响应同构
        yield sse({"status": "completed", "result": {"request_id": rid, "data": final_result}})
    except Exception as e:
        logger.error(f"improve stream failed: {e}", exc_info=True)
        yield sse({"status": "error", "message": str(e)})


_AI_RISK_RULES = {
    "none": (0, 0, "无AI痕迹", "简历真实自然"),
    "light": (5, 10, "轻微AI美化", "轻度AI润色，轻微包装"),
    "medium": (15, 20, "中度AI美化", "中度AI包装，真实性一般"),
    "high": (30, 30, "重度AI/模板生成", "重度AI生成，内容可信度低"),
}


def _as_int(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _short_list(value, limit: int = 3, item_limit: int = 90) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:item_limit] for item in value if str(item).strip()][:limit]


def _as_text(value, default: str = "未提供") -> str:
    text = str(value or "").strip()
    return text[:120] if text else default


def _as_section(value, fields: tuple[str, ...]) -> dict:
    source = value if isinstance(value, dict) else {}
    return {field: _as_text(source.get(field)) for field in fields}


def _normalize_month_label(value, *, allow_present: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return "未提供"
    if allow_present and text.lower() in {"至今", "目前", "在职", "present", "current", "now"}:
        return "至今"

    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*[-./年]\s*(1[0-2]|0?[1-9])(?:\s*月)?(?!\d)",
        text,
    )
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"

    year_match = re.fullmatch(r"\s*((?:19|20)\d{2})\s*年?\s*", text)
    if year_match:
        return year_match.group(1)
    return _as_text(text)


def _month_index(value: str, current: datetime) -> int | None:
    if value == "至今":
        return current.year * 12 + current.month - 1
    match = re.fullmatch(r"((?:19|20)\d{2})-(1[0-2]|0[1-9])", value)
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2)) - 1


def _month_label(index: int) -> str:
    year, zero_based_month = divmod(index, 12)
    return f"{year:04d}-{zero_based_month + 1:02d}"


def _month_duration(months: int) -> str:
    years, remainder = divmod(max(0, months), 12)
    if years and remainder:
        return f"{years} 年 {remainder} 个月"
    if years:
        return f"{years} 年"
    return f"{remainder} 个月"


def _normalize_employment_records(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    current = datetime.now()
    records: list[dict[str, str]] = []
    for item in value[:_EMPLOYMENT_RECORD_LIMIT]:
        if not isinstance(item, dict):
            continue
        company_name = item.get("company_name") or item.get("company")
        job_title = item.get("job_title") or item.get("position") or item.get("title")
        start_value = item.get("start_date") or item.get("start_time")
        end_value = item.get("end_date") or item.get("end_time")
        if not any((company_name, job_title, start_value, end_value)):
            continue

        start_date = _normalize_month_label(start_value)
        end_date = _normalize_month_label(end_value, allow_present=True)
        start_index = _month_index(start_date, current)
        end_index = _month_index(end_date, current)
        duration = "未提供"
        if start_index is not None and end_index is not None and start_index <= end_index:
            duration = _month_duration(end_index - start_index + 1)

        records.append(
            {
                "company_name": _as_text(company_name),
                "job_title": _as_text(job_title),
                "start_date": start_date,
                "end_date": end_date,
                "duration": duration,
            }
        )

    records.sort(
        key=lambda record: _month_index(record["start_date"], current) or -1,
        reverse=True,
    )
    return records


def _extract_employment_records(resume_content: str) -> list[dict[str, str]]:
    """Extract structured employment rows when the model omits them."""
    lines = [
        re.sub(r"[ \t\u3000]+", " ", line).strip()
        for line in str(resume_content or "").replace("\r\n", "\n").split("\n")
    ]
    lines = [line for line in lines if line]
    if not lines:
        return []

    section_start = next(
        (index for index, line in enumerate(lines) if line.rstrip("：:") in {"工作经历", "工作经验", "任职经历"}),
        -1,
    )
    section_end = len(lines)
    if section_start >= 0:
        for index in range(section_start + 1, len(lines)):
            if lines[index].rstrip("：:") in {
                "教育经历", "教育背景", "项目经历", "项目经验", "专业技能",
                "技能特长", "证书与荣誉", "荣誉奖项", "自我评价",
            }:
                section_end = index
                break
        searchable_lines = lines[section_start + 1:section_end]
    else:
        searchable_lines = lines

    extracted: list[dict[str, str]] = []
    for index, line in enumerate(searchable_lines):
        date_match = _EMPLOYMENT_DATE_RANGE_RE.search(line)
        if not date_match:
            continue

        header_candidates: list[tuple[int, str]] = []
        inline_header = line[:date_match.start()].strip(" |-—–~～·")
        if inline_header:
            header_candidates.append((index, inline_header))
        lookback_start = max(0, index - 6)
        header_candidates.extend(
            (candidate_index, searchable_lines[candidate_index])
            for candidate_index in range(index - 1, lookback_start - 1, -1)
        )

        company_name = ""
        job_title = ""
        for _candidate_index, candidate in header_candidates:
            header_match = _EMPLOYMENT_HEADER_RE.fullmatch(candidate)
            if header_match:
                company_name = header_match.group("company").strip()
                job_title = header_match.group("title").strip()
                break

        if not company_name:
            for candidate_index, candidate in header_candidates:
                company_match = _EMPLOYMENT_COMPANY_RE.fullmatch(candidate)
                if not company_match:
                    continue
                company_name = company_match.group("company").strip()
                nearby_lines = searchable_lines[candidate_index + 1:index]
                job_title = next(
                    (
                        nearby.strip()
                        for nearby in nearby_lines
                        if _looks_like_job_title(nearby)
                    ),
                    "未提供",
                )
                break

        if not company_name:
            continue
        extracted.append(
            {
                "company_name": company_name,
                "job_title": job_title or "未提供",
                "start_date": date_match.group("start"),
                "end_date": date_match.group("end"),
            }
        )

    return _normalize_employment_records(extracted)


def _looks_like_job_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.rstrip("：:") in {"内容", "职责", "业绩", "工作内容", "工作职责"}:
        return False
    return bool(re.search(r"工程师|经理|主管|总监|负责人|顾问|专员|助理|实习|设计师|开发|运维|销售|会计|教师", text))


def _merge_employment_records(
    extracted_records: list[dict[str, str]],
    model_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prefer resume text for factual fields, then add model-only periods."""
    merged = [dict(record) for record in extracted_records]
    keyed = {
        (record["start_date"], record["end_date"]): record
        for record in merged
        if record["start_date"] != "未提供" and record["end_date"] != "未提供"
    }
    for model_record in model_records:
        key = (model_record["start_date"], model_record["end_date"])
        existing = keyed.get(key)
        if existing:
            for field in ("company_name", "job_title"):
                if existing[field] == "未提供" and model_record[field] != "未提供":
                    existing[field] = model_record[field]
            continue
        merged.append(dict(model_record))
        if key[0] != "未提供" and key[1] != "未提供":
            keyed[key] = merged[-1]
    return _normalize_employment_records(merged)


def _find_employment_overlaps(records: list[dict[str, str]]) -> list[dict[str, str | int]]:
    """Find overlapping employment periods instead of silently merging them as gaps."""
    current = datetime.now()
    parsed = []
    for record in records:
        start = _month_index(record.get("start_date", ""), current)
        end = _month_index(record.get("end_date", ""), current)
        if start is None or end is None or start > end:
            continue
        parsed.append((start, end, record))
    overlaps = []
    for index, (start, end, record) in enumerate(parsed):
        for other_start, other_end, other in parsed[index + 1:]:
            overlap_start = max(start, other_start)
            overlap_end = min(end, other_end)
            if overlap_start <= overlap_end:
                overlaps.append({
                    "company_name": record.get("company_name", "未提供"),
                    "other_company_name": other.get("company_name", "未提供"),
                    "start_date": _month_label(overlap_start),
                    "end_date": _month_label(overlap_end),
                    "months": overlap_end - overlap_start + 1,
                    "message": f"{record.get('company_name', '未提供')} 与 {other.get('company_name', '未提供')} 任职时间重叠 {_month_duration(overlap_end - overlap_start + 1)}，建议人工核实",
                })
    return overlaps


def _calculate_employment_gaps(records: list[dict[str, str]], gap_note: str = "") -> str:
    """Calculate calendar-month gaps after merging overlapping employment periods."""
    if not records:
        return "未提供"

    current = datetime.now()
    current_index = current.year * 12 + current.month - 1
    intervals: list[tuple[int, int]] = []
    incomplete = False
    for record in records:
        start_index = _month_index(record["start_date"], current)
        end_index = _month_index(record["end_date"], current)
        if (
            start_index is None
            or end_index is None
            or start_index > end_index
            or start_index > current_index
            or end_index > current_index
        ):
            incomplete = True
            continue
        intervals.append((start_index, end_index))

    if incomplete:
        summary = "部分经历缺少精确月份，无法完整核算空窗期"
        if gap_note and gap_note != "未提供":
            summary += f"；简历说明：{gap_note}"
        return summary

    intervals.sort()
    merged: list[list[int]] = []
    for start_index, end_index in intervals:
        if not merged or start_index > merged[-1][1] + 1:
            merged.append([start_index, end_index])
        else:
            merged[-1][1] = max(merged[-1][1], end_index)

    gaps: list[tuple[int, int, int, bool]] = []
    for previous, following in zip(merged, merged[1:]):
        gap_start = previous[1] + 1
        gap_end = following[0] - 1
        if gap_start <= gap_end:
            gaps.append((gap_start, gap_end, gap_end - gap_start + 1, False))

    if merged[-1][1] < current_index:
        gap_start = merged[-1][1] + 1
        gaps.append((gap_start, current_index, current_index - gap_start + 1, True))

    if gaps:
        total_months = sum(gap[2] for gap in gaps)
        details = "；".join(
            f"{_month_label(start)} 至 {_month_label(end)}（{_month_duration(months)}{'，当前' if is_current else ''}）"
            for start, end, months, is_current in gaps
        )
        summary = f"共 {len(gaps)} 段，累计 {_month_duration(total_months)}：{details}"
    else:
        summary = f"无空窗期（任职时间连续，截至 {_month_label(current_index)}）"

    if gap_note and gap_note != "未提供":
        summary += f"；简历说明：{gap_note}"
    return summary


def _list_or_default(value, default: str, limit: int = 3) -> list[str]:
    return _short_list(value, limit) or [default]


def _normalize_skill_labels(value) -> list[str]:
    label_aliases = {
        "[直接匹配]": "[符合要求]",
        "[完全匹配]": "[符合要求]",
        "[可迁移]": "[相关经验]",
        "[部分匹配]": "[相关经验]",
        "[缺失]": "[简历未体现]",
    }
    items = _short_list(value, 5)
    normalized: list[str] = []
    for item in items:
        for old_label, new_label in label_aliases.items():
            if item.startswith(old_label):
                item = new_label + item[len(old_label):]
                break
        normalized.append(item)
    return normalized


def _compact_analysis_text(value: str, max_chars: int) -> str:
    """Remove extraction noise while preserving evidence from both ends."""
    unique_lines: list[str] = []
    seen: set[str] = set()
    for raw_line in str(value or "").replace("\r\n", "\n").split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw_line).strip()
        if not line or line in seen:
            continue
        if _looks_like_extraction_noise(line):
            continue
        seen.add(line)
        unique_lines.append(line)

    compacted = "\n".join(unique_lines)
    if len(compacted) <= max_chars:
        return compacted
    head_size = int(max_chars * 0.78)
    tail_size = max_chars - head_size
    return f"{compacted[:head_size]}\n...[中间重复或超长内容已压缩]...\n{compacted[-tail_size:]}"


_SHORT_SKILL_TOKENS = {
    "AI", "BI", "C", "C#", "C++", "ERP", "Go", "HR", "IT", "IoT",
    "OA", "PMP", "R", "SQL", "UI", "UG", "UX",
}


def _looks_like_extraction_noise(line: str) -> bool:
    """Detect common PDF watermark/font-map fragments without dropping skills."""
    if re.search(r"[\u4e00-\u9fff]", line):
        return False
    if line in _SHORT_SKILL_TOKENS:
        return False
    if len(line) <= 2 and line.isascii():
        return True

    tokens = line.split()
    simplified = [re.sub(r"[^A-Za-z0-9+#.-]", "", token).strip(".-") for token in tokens]
    simplified = [token for token in simplified if token]
    if len(simplified) >= 3:
        short_ratio = sum(len(token) <= 3 for token in simplified) / len(simplified)
        if short_ratio >= 0.75:
            return True

    collapsed = re.sub(r"[^A-Za-z0-9]", "", line)
    if len(collapsed) >= 24:
        digit_count = sum(char.isdigit() for char in collapsed)
        letter_count = sum(char.isalpha() for char in collapsed)
        if digit_count >= 5 and letter_count >= 5 and "@" not in line:
            return True
    return False


_BASIC_SCREENING_FIELDS = (
    "native_place", "age", "gender", "work_location", "salary_expectation",
)
_EDUCATION_HISTORY_FIELDS = (
    "degree", "school_name", "school_tier", "major", "graduation_year",
)
_WORK_HISTORY_FIELDS = (
    "total_years", "relevant_years", "industry_match", "company_background",
    "seniority", "team_size", "stability", "employment_gaps",
    "responsibility_match",
)
_EMPLOYMENT_RECORD_LIMIT = 12
_EMPLOYMENT_DATE_TOKEN = r"(?:19|20)\d{2}\s*[-./年]\s*(?:1[0-2]|0?[1-9])(?:\s*月)?"
_EMPLOYMENT_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_EMPLOYMENT_DATE_TOKEN})\s*(?:-|—|–|~|～|至)\s*"
    rf"(?P<end>至今|目前|在职|present|current|{_EMPLOYMENT_DATE_TOKEN})",
    re.IGNORECASE,
)
_EMPLOYMENT_COMPANY_SUFFIX = r"(?:有限责任公司|股份有限公司|有限公司|集团公司|集团|公司|研究院|事务所|中心|工作室|厂)"
_EMPLOYMENT_HEADER_RE = re.compile(
    rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})\s+(?P<title>.+)"
)
_EMPLOYMENT_COMPANY_RE = re.compile(rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})")

_SCORE_BREAKDOWN_LIMITS = {
    "hard_requirements": 25,
    "responsibility_overlap": 25,
    "skills_projects": 25,
    "industry_background": 15,
    "evidence_bonus": 10,
}


def _normalize_education_history(raw) -> list[dict]:
    """Normalize education history entries, sorted by degree (博士 > 硕士 > 本科 > 专科)."""
    _DEGREE_ORDER = {"博士": 0, "硕士": 1, "本科": 2, "专科": 3, "大专": 3, "其他": 4}
    if not isinstance(raw, list) or not raw:
        return []
    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        degree = str(item.get("degree") or "未提供").strip()
        entry = {
            "degree": degree,
            "school_name": _as_text(item.get("school_name")),
            "school_tier": _as_text(item.get("school_tier")),
            "major": _as_text(item.get("major")),
            "graduation_year": _as_text(item.get("graduation_year")),
        }
        entries.append(entry)
    entries.sort(key=lambda e: _DEGREE_ORDER.get(e["degree"], 99))
    return entries[:8]


def _normalize_score_breakdown(value) -> tuple[dict[str, int], bool]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, int] = {}
    complete = True
    for field, maximum in _SCORE_BREAKDOWN_LIMITS.items():
        try:
            score = int(round(float(source.get(field))))
        except (TypeError, ValueError):
            score = 0
            complete = False
        normalized[field] = max(0, min(maximum, score))
    return normalized, complete


def _normalize_hr_analysis(raw: dict, job_content: str = "", resume_content: str = "") -> dict:
    """Enforce the scoring formula and all allowed deduction/grade ranges."""
    score_breakdown, has_complete_breakdown = _normalize_score_breakdown(raw.get("score_breakdown"))
    base_score = (
        sum(score_breakdown.values())
        if has_complete_breakdown
        else max(0, min(100, _as_int(raw.get("job_fit_score"))))
    )
    risk = str(raw.get("ai_risk") or "none").strip().lower()
    if risk not in _AI_RISK_RULES:
        risk = "none"

    minimum, maximum, risk_level, risk_label = _AI_RISK_RULES[risk]
    proposed = _as_int(raw.get("ai_deduction"), minimum)
    deduction = max(minimum, min(maximum, proposed))
    final_score = max(0, base_score - deduction)

    # 硬门槛确定性扣分（A/B）：requirements_checklist 中的 not_met 为确定性不达标，
    # 在 AI 美化扣分之后再次扣分并封顶——学历层级不达标封顶 D 级，其余硬门槛封顶 C 级。
    raw_checklist = raw.get("requirements_checklist") if isinstance(raw.get("requirements_checklist"), list) else []
    hard_gate_fails = 0
    education_gate_fail = False
    for item in raw_checklist:
        if isinstance(item, dict) and item.get("status") == "not_met":
            hard_gate_fails += 1
            if str(item.get("category")) == "education":
                education_gate_fail = True
    hard_gate_deduction = 0
    if hard_gate_fails:
        hard_gate_deduction = min(30, 10 * hard_gate_fails)  # 每条确定性不达标扣 10 分，上限 30
        final_score = max(0, final_score - hard_gate_deduction)
        if education_gate_fail:
            final_score = min(final_score, 59)   # 学历硬门槛不达标 → 淘汰级封顶
        else:
            final_score = min(final_score, 69)   # 年限/证书硬门槛不达标 → 储备观察封顶

    if final_score >= 90:
        grade = "S级（优质适配）"
    elif final_score >= 80:
        grade = "A级（良好适配）"
    elif final_score >= 70:
        grade = "B级（基本适配）"
    elif final_score >= 60:
        grade = "C级（适配一般）"
    else:
        grade = "D级（不适配）"

    score_recommendation = "优先面试" if final_score >= 80 else "储备观察" if final_score >= 60 else "淘汰"
    requested_recommendation = str(raw.get("recruitment_recommendation") or score_recommendation).strip()
    if requested_recommendation == "酌情考虑":
        requested_recommendation = "储备观察"
    if requested_recommendation not in {"优先面试", "储备观察", "淘汰"}:
        requested_recommendation = score_recommendation
    recommendation_rank = {"淘汰": 0, "储备观察": 1, "优先面试": 2}
    if recommendation_rank[requested_recommendation] > recommendation_rank[score_recommendation]:
        requested_recommendation = score_recommendation

    score_fit_tag = "高匹配" if final_score >= 80 else "部分匹配" if final_score >= 60 else "不匹配"
    requested_fit_tag = str(raw.get("fit_tag") or score_fit_tag).strip()
    if requested_fit_tag not in {"高匹配", "部分匹配", "不匹配"}:
        requested_fit_tag = score_fit_tag
    fit_rank = {"不匹配": 0, "部分匹配": 1, "高匹配": 2}
    if fit_rank[requested_fit_tag] > fit_rank[score_fit_tag]:
        requested_fit_tag = score_fit_tag

    basic_screening = _as_section(raw.get("basic_screening"), _BASIC_SCREENING_FIELDS)
    education_history = _normalize_education_history(raw.get("education_history"))
    raw_work_history = raw.get("work_history") if isinstance(raw.get("work_history"), dict) else {}
    work_history = _as_section(raw_work_history, _WORK_HISTORY_FIELDS)
    model_employment_records = _normalize_employment_records(raw_work_history.get("employment_records"))
    extracted_employment_records = _extract_employment_records(resume_content)
    employment_records = _merge_employment_records(
        extracted_employment_records,
        model_employment_records,
    )
    work_history["employment_records"] = employment_records
    work_history["employment_overlaps"] = _find_employment_overlaps(employment_records)
    if employment_records:
        gap_note = _as_text(raw_work_history.get("employment_gap_notes"))
        work_history["employment_gaps"] = _calculate_employment_gaps(employment_records, gap_note)
    raw_skill_match = raw.get("skill_match") if isinstance(raw.get("skill_match"), dict) else {}
    skill_match = {
        "hard_skills": _normalize_skill_labels(raw_skill_match.get("hard_skills")) or ["[简历未体现] 未找到可确认的岗位核心技能证据。"],
        "project_match_points": _list_or_default(raw_skill_match.get("project_match_points"), "简历未提供可与 JD 直接对应的项目或工作成果。", 5),
        "soft_skills": _list_or_default(raw_skill_match.get("soft_skills"), "简历未提供可确认的软实力证据。", 3),
    }

    risk_points = _short_list(raw.get("risk_points"), 6)
    if not re.search(r"薪资|薪酬|工资|预算|待遇|\d+\s*[kK]", job_content):
        risk_points = [
            item for item in risk_points
            if not re.search(r"薪资|薪酬|工资|预算|待遇", item)
        ]

    summary = str(raw.get("summary") or "未提供综合判定说明。").strip()[:240]
    agent_trace = raw.get("agent_trace") if isinstance(raw.get("agent_trace"), dict) else None
    agent_validation = raw.get("agent_validation") if isinstance(raw.get("agent_validation"), dict) else None
    requirements_checklist = raw_checklist if isinstance(raw_checklist, list) else []
    return {
        "candidate_name": _as_text(raw.get("candidate_name")),
        "final_score": final_score,
        "fit_grade": grade,
        "hard_gate_deduction": hard_gate_deduction,
        "job_fit_score": base_score,
        "job_fit_percentage": base_score,
        "score_breakdown": score_breakdown,
        "ai_risk": risk,
        "ai_risk_level": risk_level,
        "ai_risk_label": risk_label,
        "ai_deduction": deduction,
        "summary": summary,
        "basic_screening": basic_screening,
        "education_history": education_history,
        "work_history": work_history,
        "skill_match": skill_match,
        "certificates": _list_or_default(raw.get("certificates"), "简历未提供证书资质信息。", 5),
        "bonus_items": _list_or_default(raw.get("bonus_items"), "简历未提供明确加分项。", 5),
        "strengths": _list_or_default(raw.get("strengths"), "简历未提供可确认的核心优势证据。", 5),
        "weaknesses": _list_or_default(raw.get("weaknesses"), "简历未提供足够信息，无法确认关键短板。", 5),
        "risk_points": risk_points or ["未发现明显履历风险；关键事实仍建议在面试中核验。"],
        "role_specific_assessment": _list_or_default(raw.get("role_specific_assessment"), "当前岗位无额外专项判断。", 4),
        "requirements_checklist": requirements_checklist,
        "deduction_reasons": _short_list(raw.get("deduction_reasons"), 3),
        "recruitment_recommendation": requested_recommendation,
        "fit_tag": requested_fit_tag,
        "agent_trace": agent_trace,
        "agent_validation": agent_validation,
    }


def _hr_analysis_markdown(result: dict) -> str:
    reasons = result["deduction_reasons"] or ["未发现需要扣分的明显 AI 包装依据"]
    skills = result["skill_match"]
    basic = result["basic_screening"]
    education_history = result.get("education_history") or []
    edu_lines = [f"- 学历：{e['degree']} / {e['school_name']} / {e['school_tier']} / {e['major']} / {e['graduation_year']}" for e in education_history] or ["- 教育经历：未提供"]
    history = result["work_history"]
    employment_records = history.get("employment_records") or []
    employment_lines = [
        f"- {record['company_name']} / {record['job_title']}：{record['start_date']} 至 {record['end_date']}"
        for record in employment_records
    ] or ["- 工作经历明细：未提供"]
    return "\n".join(
        [
            "# HR 招聘分析报告",
            f"1. **简历最终得分 + 适配等级**：{result['final_score']} 分，{result['fit_grade']}",
            f"2. **适配标签**：{result['fit_tag']}；招聘建议：{result['recruitment_recommendation']}",
            f"3. **AI美化风险等级**：{result['ai_risk_level']}（扣 {result['ai_deduction']} 分，{result['ai_risk_label']}）",
            f"4. **核心判定简要说明**：{result['summary']}",
            "## 基础信息",
            f"- 姓名：{result['candidate_name']}；性别：{basic['gender']}；年龄：{basic['age']}",
            f"- 籍贯：{basic['native_place']}；工作所在地：{basic['work_location']}；期望薪资：{basic['salary_expectation']}",
            "## 教育经历",
            *edu_lines,
            "## 工作履历",
            f"- 年限：总计 {history['total_years']}；相关岗位 {history['relevant_years']}；行业匹配 {history['industry_match']}",
            f"- 履历稳定性：{history['stability']}；职责重合度：{history['responsibility_match']}",
            *employment_lines,
            f"- 空窗期：{history['employment_gaps']}",
            "## 技能与项目匹配",
            "- 核心技能：" + "；".join(skills['hard_skills']),
            "- 项目匹配：" + "；".join(skills['project_match_points']),
            "## 优势与短板",
            "- 优势：" + "；".join(result['strengths']),
            "- 短板：" + "；".join(result['weaknesses']),
            "- 证书资质：" + "；".join(result['certificates']),
            "- 岗位专项判断：" + "；".join(result['role_specific_assessment']),
            "## 风险预警",
            "- " + "；".join(result['risk_points']),
            "- AI 风险依据：" + "；".join(reasons),
        ]
    )


def _build_raw_resume_markdown(content: str) -> str:
    """Convert locally extracted resume text into A4 Studio markdown."""
    lines = [line.strip() for line in (content or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    section_names = {
        "个人简介", "个人概况", "职业概况", "自我评价", "工作经验", "工作经历",
        "项目经验", "项目经历", "教育背景", "教育经历", "专业技能", "技能特长",
        "技能", "证书与荣誉", "荣誉奖项", "培训经历", "论文发表", "作品集",
    }
    out = [f"# {lines[0]}"]
    index = 1

    # Common DOCX extraction shape: name -> 联系方式 -> contact line.
    if index < len(lines) and lines[index].rstrip("：:") in {"联系方式", "联系信息"}:
        index += 1
        if index < len(lines):
            out.extend(["", f"> {lines[index]}"])
            index += 1
    elif index < len(lines) and lines[index] not in section_names:
        out.extend(["", f"## {lines[index]}"])
        index += 1

    current_section = False
    for line in lines[index:]:
        normalized = line.rstrip("：:")
        if normalized in section_names:
            out.extend(["", f"## {normalized}"])
            current_section = True
            continue
        if not current_section:
            out.extend(["", line])
            continue
        if line.startswith(("•", "·", "●", "▪", "-", "*")):
            out.append(f"- {line.lstrip('•·●▪-* ').strip()}")
        elif "\t" in line:
            title, meta = (part.strip() for part in line.split("\t", 1))
            out.append(f"### {title} | {meta}" if meta else f"### {title}")
        else:
            out.append(line)

    return "\n".join(out).rstrip() + "\n"


def _resume_studio_markdown(resume: dict) -> str:
    processed = resume.get("processed") or {}
    if any(processed.get(key) for key in ("personal_data", "experiences", "projects", "education", "skills")):
        return _normalize_md_for_a4cv(_build_fallback_markdown(processed))
    return _normalize_md_for_a4cv(_build_raw_resume_markdown(resume.get("content", "")))


def _run_hr_analysis(
    resume_id: str,
    job_id: str,
    user_id: str,
    ai_config: dict | None = None,
    precomputed_requirements: dict | None = None,
    agent_config: dict | None = None,
    on_event=None,
) -> dict:
    config_fingerprint = llm.model_config_fingerprint(ai_config)
    agent_fingerprint = _agent_config_fingerprint(agent_config)
    cache_key = (_HR_ANALYSIS_VERSION, user_id, resume_id, job_id, config_fingerprint, agent_fingerprint)
    cached = _hr_cache_get(cache_key)
    if cached is not None:
        return cached

    resume = store.get_resume(resume_id, user_id=user_id)
    if not resume:
        raise ApiError(f"Resume not found: {resume_id}", 404, "resumes")
    job = store.get_job(job_id, user_id=user_id)
    if not job:
        raise ApiError(f"Job not found: {job_id}", 404, "resumes")

    started = time.perf_counter()
    try:
        raw = screening_agent.run_screening_agent(
            job_content=job.get("content", ""),
            resume_content=resume.get("content", ""),
            current_date=datetime.now().strftime("%Y-%m"),
            runtime_config=ai_config,
            precomputed_requirements=precomputed_requirements,
            agent_config=agent_config,
            on_event=on_event,
        )
        result = _normalize_hr_analysis(
            raw,
            job.get("content", ""),
            resume.get("content", ""),
        )
        _hr_cache_put(cache_key, result)
        logger.info(
            "HR analysis completed: candidates=1 input_chars=%s elapsed_ms=%s",
            len(job.get("content", "")) + len(resume.get("content", "")),
            int((time.perf_counter() - started) * 1000),
        )
        auth_mod.record_user_usage(user_id, "analysis")  # 使用统计埋点
        return result
    except ValueError as e:
        logger.warning("HR model returned invalid JSON after retry: %s", e)
        raise ApiError("AI 分析结果无法解析，请重试。", 502, "resumes") from e
    except Exception as e:
        logger.error(f"HR recruitment analysis failed: {e}", exc_info=True)
        raise ApiError("AI 分析服务暂时不可用，请检查模型配置后重试。", 503, "resumes") from e


def _run_hr_batch_analysis(
    resume_ids: list[str],
    job_id: str,
    user_id: str,
    ai_config: dict | None = None,
    agent_config: dict | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Run isolated candidate analyses concurrently; pre-extract job requirements once to avoid redundant LLM calls."""
    agent_fingerprint = _agent_config_fingerprint(agent_config)
    results: dict[str, dict] = {}
    failures: list[dict] = []
    pending: list[str] = []
    job: dict | None = None
    for resume_id in resume_ids:
        cache_key = (
            _HR_ANALYSIS_VERSION,
            user_id,
            resume_id,
            job_id,
            llm.model_config_fingerprint(ai_config),
            agent_fingerprint,
        )
        cached = _hr_cache_get(cache_key)
        if cached is not None:
            results[resume_id] = cached
            continue
        resume = store.get_resume(resume_id, user_id=user_id)
        if not resume:
            failures.append({"resume_id": resume_id, "detail": f"Resume not found: {resume_id}"})
            continue
        pending.append(resume_id)
        if job is None:
            job = store.get_job(job_id, user_id=user_id)
            if not job:
                for rid in pending:
                    failures.append({"resume_id": rid, "detail": f"Job not found: {job_id}"})
                return results, failures

    if not pending:
        return results, failures

    # Pre-extract requirements once for the shared JD — saves N-1 LLM calls in batch mode
    precomputed_requirements = None
    if job and job.get("content"):
        _req_budget = {"calls": 0}
        precomputed_requirements = screening_agent._extract_requirements(
            job.get("content", ""), ai_config, _req_budget
        )
        logger.info(
            "Batch HR analysis: pre-extracted %s requirements for job %s (LLM calls=%s)",
            len(precomputed_requirements.get("requirements", [])),
            job_id,
            _req_budget["calls"],
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(3, len(pending)), thread_name_prefix="hr-analysis") as executor:
        futures = {
            executor.submit(_run_hr_analysis, resume_id, job_id, user_id, ai_config, precomputed_requirements, agent_config): resume_id
            for resume_id in pending
        }
        for future in as_completed(futures):
            resume_id = futures[future]
            try:
                results[resume_id] = future.result()
            except ApiError as exc:
                failures.append({"resume_id": resume_id, "detail": exc.message})
            except Exception as exc:
                logger.error("Parallel HR analysis failed for %s: %s", resume_id, exc, exc_info=True)
                failures.append({"resume_id": resume_id, "detail": "AI 分析服务暂时不可用，请重试。"})

    logger.info(
        "Parallel HR analysis completed: candidates=%s succeeded=%s elapsed_ms=%s",
        len(pending),
        sum(1 for resume_id in pending if resume_id in results),
        int((time.perf_counter() - started) * 1000),
    )

    return results, failures


def _compare_candidates(analysis_results: list[dict], runtime_config: dict | None = None) -> dict | None:
    """Compare multiple candidates. Returns None for single-candidate analysis."""
    if len(analysis_results) < 2:
        return None

    items = []
    for a in analysis_results:
        if isinstance(a, dict):
            items.append({
                "candidate_name": a.get("candidate_name") or a.get("hr_analysis", {}).get("candidate_name") or "未提供",
                "final_score": a.get("hr_analysis", {}).get("final_score", a.get("final_score", 0)),
                "strengths": a.get("hr_analysis", {}).get("strengths", a.get("strengths", [])),
                "weaknesses": a.get("hr_analysis", {}).get("weaknesses", a.get("weaknesses", [])),
            })

    candidates_json = json.dumps(items, ensure_ascii=False)
    prompt = f"""你是招聘委员会主席。对比以下 {len(items)} 位候选人：
{candidates_json}

重要：每位候选人的"final_score"是系统已计算好的最终得分，排名中每一行的 score 必须与该候选人的 final_score 完全一致，严禁重新打分或自由发挥分数。

只输出 JSON：{{
  "ranking": [
    {{"rank": 1, "name": "...", "score": <该候选人 final_score 原样> , "difference": "与岗位最相关的差异点"}}
  ],
  "pairwise": [
    "张三 vs 李四：张三带过 15 人团队；李四技术更深但无管理经验"
  ],
  "recommendation": "优先面试：张三、李四"
}}"""

    try:
        result = llm.call_llm(prompt, expect_json=True, max_tokens=2000, runtime_config=runtime_config)
    except Exception as exc:  # 对比只是增强能力，失败不应拖垮批量分析响应
        logger.warning("候选人对比调用失败，跳过对比: %s", exc)
        return None
    if isinstance(result, dict):
        ranking = result.get("ranking")
        if isinstance(ranking, list):
            # 权威分数以系统计算的 final_score 为准（LLM 不得改分）：按姓名回填
            score_by_name = {item["candidate_name"]: item["final_score"] for item in items}
            for item in ranking:
                if isinstance(item, dict):
                    item.setdefault("name", "")
                    matched = score_by_name.get(item.get("name"))
                    if matched is None:
                        # 姓名严格不匹配时尝试包含式匹配（去空白），仍找不到则按名字截断兜底
                        norm_name = (item.get("name") or "").replace(" ", "").strip()
                        for cand, score in score_by_name.items():
                            cand_norm = cand.replace(" ", "").strip()
                            if cand_norm and (cand_norm in norm_name or norm_name in cand_norm):
                                matched = score
                                break
                    item["score"] = matched if matched is not None else 0
                    item.setdefault("difference", "")
            # 排序以分数为准（降序），保证排名与报告头/候选人切换条一致
            ranking.sort(key=lambda r: r.get("score") or 0, reverse=True)
            for index, item in enumerate(ranking, start=1):
                item["rank"] = index
            return {
                "ranking": ranking,
                "pairwise": result.get("pairwise", []),
                "recommendation": result.get("recommendation", ""),
            }
    return None


def _candidate_name_from_resume(resume: dict, result: dict) -> str:
    analyzed_name = str(result.get("candidate_name") or "").strip()
    if analyzed_name and analyzed_name not in {"未提供", "未知", "无法识别"}:
        return analyzed_name[:40]

    personal_data = (resume.get("processed") or {}).get("personal_data") or {}
    structured_name = str(personal_data.get("name") or "").strip()
    if not structured_name:
        first_name = str(personal_data.get("firstName") or personal_data.get("first_name") or "").strip()
        last_name = str(personal_data.get("lastName") or personal_data.get("last_name") or "").strip()
        structured_name = f"{last_name}{first_name}" if any("\u4e00" <= char <= "\u9fff" for char in last_name + first_name) else " ".join(filter(None, (first_name, last_name)))
    if structured_name:
        return structured_name[:40]

    for raw_line in str(resume.get("content") or "").splitlines()[:5]:
        line = raw_line.strip().lstrip("#").strip()
        if not line or len(line) > 40:
            continue
        if any(
            token in line
            for token in (
                "@", "：", ":", "·", "|", "，", ",", "。",
                "简历", "联系方式", "工程师", "经理", "候选人",
            )
        ):
            continue
        if sum(char.isdigit() for char in line) > 1:
            continue
        return line
    return "未识别姓名"


def _hr_analysis_payload(resume_id: str, job_id: str, result: dict, user_id: str) -> dict:
    resume = store.get_resume(resume_id, user_id=user_id)
    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "candidate_name": _candidate_name_from_resume(resume or {}, result),
        "hr_analysis": result,
        "analysis_result": _hr_analysis_markdown(result),
        "studio_markdown": _resume_studio_markdown(resume or {}),
    }


@app.post("/api/v1/resumes/hr-analysis")
def hr_analysis():
    """Run one concise, structured LLM call for recruitment screening.

    ?stream=true 走 SSE 流式：实时推送 Agent 各阶段进度，最后输出与非流式同构的结果。
    """
    rid = _request_id("resumes")
    stream = request.args.get("stream", "false").lower() in ("true", "1", "yes")
    data = request.get_json(silent=True) or {}
    resume_ids = data.get("resume_ids")
    if not isinstance(resume_ids, list):
        resume_ids = [data.get("resume_id")] if data.get("resume_id") else []
    resume_ids = list(dict.fromkeys(str(item).strip() for item in resume_ids if str(item).strip()))
    job_id = str(data.get("job_id") or "").strip()
    if not resume_ids or not job_id:
        return _err("resume_id/resume_ids and job_id are required", 422, "resumes")
    if len(resume_ids) > 3:
        return _err("A maximum of 3 resumes can be analyzed at once", 422, "resumes")
    ai_config = _request_ai_config(data)
    agent_config = _request_agent_config(data)
    user_id = _current_user_id()

    if stream:
        return Response(
            stream_with_context(_hr_analysis_stream(resume_ids, job_id, rid, user_id, ai_config, agent_config)),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    if len(resume_ids) == 1:
        result = _run_hr_analysis(resume_ids[0], job_id, user_id, ai_config, agent_config=agent_config)
        payload = _hr_analysis_payload(resume_ids[0], job_id, result, user_id)
    else:
        batch_results, failures = _run_hr_batch_analysis(resume_ids, job_id, user_id, ai_config, agent_config)
        analyses = [
            _hr_analysis_payload(resume_id, job_id, batch_results[resume_id], user_id)
            for resume_id in resume_ids
            if resume_id in batch_results
        ]
        if not analyses:
            raise ApiError("本批次所有简历的 AI 分析均失败，请重试。", 502, "resumes")
        # Cross-candidate comparison
        comparison = _compare_candidates(analyses, ai_config)
        payload = {
            **analyses[0],
            "batch_analyses": analyses,
            "batch_failures": failures,
            "comparison": comparison,
        }

    return jsonify(
        {
            "request_id": rid,
            "data": payload,
        }
    )


def _hr_analysis_stream(resume_ids, job_id, rid, user_id, ai_config, agent_config):
    """SSE 生成器：逐阶段推送进度，最后输出与非流式同构的 completed 结果。

    单简历路径用后台线程 + 线程安全队列实时转发 screening_agent 的 on_event，
    让前端能实时看到“正在检索公司公开信息”等中间步骤（而非分析结束后一次性到达）。
    """
    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        yield sse({"status": "starting", "message": "正在启动招聘筛选 Agent"})

        if len(resume_ids) == 1:
            # 实时流式：on_event 立即入队，主生成器边消费边 yield
            event_queue: "queue.Queue[dict | None]" = queue.Queue()
            result_holder: dict = {}

            def _on_event(event: dict) -> None:
                event_queue.put(event)

            def _worker() -> None:
                try:
                    result_holder["result"] = _run_hr_analysis(
                        resume_ids[0], job_id, user_id, ai_config,
                        agent_config=agent_config, on_event=_on_event,
                    )
                except Exception as exc:  # 传递给主生成器统一抛
                    result_holder["error"] = exc
                finally:
                    event_queue.put(None)  # 结束哨兵

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            while True:
                event = event_queue.get()
                if event is None:
                    break
                yield sse(event)
            worker.join()
            if "error" in result_holder:
                raise result_holder["error"]
            result = result_holder["result"]
            payload = _hr_analysis_payload(resume_ids[0], job_id, result, user_id)
        else:
            # 批量：逐步推送每份候选人的进度与阶段
            for index, resume_id in enumerate(resume_ids, 1):
                yield sse({"status": "candidate", "index": index, "total": len(resume_ids), "message": f"正在分析候选人 {index}/{len(resume_ids)}"})
            batch_results, failures = _run_hr_batch_analysis(resume_ids, job_id, user_id, ai_config, agent_config)
            analyses = [
                _hr_analysis_payload(resume_id, job_id, batch_results[resume_id], user_id)
                for resume_id in resume_ids
                if resume_id in batch_results
            ]
            if not analyses:
                yield sse({"status": "error", "message": "本批次所有简历的 AI 分析均失败，请重试。"})
                return
            comparison = _compare_candidates(analyses, ai_config)
            payload = {
                **analyses[0],
                "batch_analyses": analyses,
                "batch_failures": failures,
                "comparison": comparison,
            }

        yield sse({"status": "completed", "result": {"request_id": rid, "data": payload}})
    except Exception as exc:
        logger.error("HR analysis stream failed: %s", exc, exc_info=True)
        yield sse({"status": "error", "message": "AI 分析服务暂时不可用，请重试。"})


@app.get("/api/v1/resumes")
def get_resume():
    """获取简历 + 结构化数据。"""
    rid = _request_id("resumes")
    resume_id = request.args.get("resume_id")
    if not resume_id:
        return _err("resume_id is required", 422, "resumes")

    view = store.get_resume_view(resume_id, user_id=_current_user_id())
    if not view:
        return _err(f"Resume not found: {resume_id}", 404, "resumes")
    # 过滤 PDF 解析器残留的 ASCII 二进制乱码，避免前端展示乱码
    view["raw_resume"]["content"] = resume_sanitize.sanitize_resume_content(
        view["raw_resume"].get("content", "")
    )
    return jsonify({"request_id": rid, "data": view})


@app.post("/api/v1/resumes/review-markers")
def review_markers():
    """基于已有招聘分析和原简历内容生成零 Token 的简历重点标记。"""
    rid = _request_id("resumes")
    data = request.get_json(silent=True) or {}
    resume_id = str(data.get("resume_id") or "").strip()
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    if not resume_id:
        return _err("resume_id is required", 422, "resumes")
    resume = store.get_resume(resume_id, user_id=_current_user_id())
    if not resume:
        return _err(f"Resume not found: {resume_id}", 404, "resumes")
    clean_content = resume_sanitize.sanitize_resume_content(resume.get("content", ""))
    payload = resume_review.build_review_markers(
        clean_content,
        analysis,
        candidate_name=str(data.get("candidate_name") or "候选人"),
    )
    return jsonify({"request_id": rid, "data": payload})


@app.post("/api/v1/resumes/improved-markdown")
def improved_markdown():
    """
    从分析结果里提取优化后的简历 markdown（给 a4cv 编辑器用）。
    先正则抽 ```md 块；抽不到则从结构化简历拼兜底 markdown。
    """
    rid = _request_id("resumes")
    data = request.get_json(silent=True) or {}
    analysis_result = data.get("analysis_result") or ""
    resume_id = data.get("resume_id")

    # 1. 尝试从分析文本抽代码块
    md, source = _extract_md_block(analysis_result)
    if md:
        md = _normalize_md_for_a4cv(md)  # 加粗小节标题转 ##，让 a4cv 能识别结构
        return jsonify(
            {
                "request_id": rid,
                "data": {
                    "markdown": md,
                    "source": "extracted",
                    "sections_detected": _count_sections(md),
                },
            }
        )

    # 2. 兜底：从结构化简历拼装
    if resume_id:
        resume = store.get_resume(resume_id, user_id=_current_user_id())
        if resume:
            md = _resume_studio_markdown(resume)
            if md:
                return jsonify(
                    {
                        "request_id": rid,
                        "data": {
                            "markdown": md,
                            "source": "fallback",
                            "sections_detected": _count_sections(md),
                        },
                    },
                )

    return jsonify(
        {
            "request_id": rid,
            "data": {"markdown": "", "source": "none", "sections_detected": 0},
        }
    )


# ════════════════════════════════════════════════════════════════════
# 岗位接口
# ════════════════════════════════════════════════════════════════════
@app.post("/api/v1/jobs/upload")
def upload_job():
    """上传 JD（可批量）。手动校验 Content-Type 必须是 application/json。"""
    rid = _request_id("jobs")

    # 手动 Content-Type 校验（照搬旧版）
    ctype = request.headers.get("Content-Type", "")
    if "application/json" not in ctype:
        return _err("Content-Type must be application/json", 400, "jobs")

    data = request.get_json(silent=True) or {}
    resume_id = data.get("resume_id")
    job_descriptions = data.get("job_descriptions") or []

    if not resume_id:
        return _err("resume_id is required", 422, "jobs")
    if not job_descriptions:
        return _err("job_descriptions is required", 422, "jobs")

    # 校验 resume 存在（且属于当前用户）
    user_id = _current_user_id()
    if not store.get_resume(resume_id, user_id=user_id):
        return _err(f"resume corresponding to resume_id: {resume_id} not found", 400, "jobs")

    job_ids = []
    for desc in job_descriptions:
        processed = doc_parser.summarize_job_locally(desc)
        jid = store.save_job(resume_id=resume_id, content=desc, processed=processed, user_id=user_id)
        job_ids.append(jid)
        logger.info(f"Job created: {jid}")

    return jsonify(
        {
            "message": "data successfully processed",
            "request_id": rid,
            "job_id": job_ids,
        }
    )


@app.get("/api/v1/jobs")
def get_job():
    """获取 JD + 结构化数据。"""
    rid = _request_id("jobs")
    job_id = request.args.get("job_id")
    if not job_id:
        return _err("job_id is required", 422, "jobs")

    view = store.get_job_view(job_id, user_id=_current_user_id())
    if not view:
        return _err(f"Job not found: {job_id}", 404, "jobs")
    return jsonify({"request_id": rid, "data": view})


# ════════════════════════════════════════════════════════════════════
# 归档（候选人才库 + 回收站）
# ════════════════════════════════════════════════════════════════════


def _archive_payload(rec: dict, include_analysis: bool = False) -> dict:
    """归档记录对外响应结构（裁剪掉内部字段）。

    include_analysis=True 时附带完整 analysis（供详情页重新生成报告快照），
    列表接口默认不附带以避免响应过大。
    """
    payload = {
        "archive_id": rec["archive_id"],
        "resume_id": rec.get("resume_id"),
        "job_id": rec.get("job_id"),
        "candidate_name": rec.get("candidate_name", ""),
        "final_score": rec.get("final_score", 0) or 0,
        "fit_tag": rec.get("fit_tag", ""),
        "recruitment_recommendation": rec.get("recruitment_recommendation", ""),
        "job_title": rec.get("job_title", ""),
        "category": rec.get("category") or rec.get("job_title", ""),
        "custom_tags": rec.get("custom_tags") or [],
        "analysis_snapshot": rec.get("analysis_snapshot") or {},
        "status": rec.get("status", "active"),
        "trashed_at": rec.get("trashed_at"),
        "created_at": rec.get("created_at"),
    }
    if include_analysis:
        payload["analysis"] = rec.get("analysis") or {}
    return payload


@app.post("/api/v1/archives")
def create_archive():
    """归档一份已分析的简历到候选人才库（幂等：resume_id + job_id 唯一）。"""
    rid = _request_id("archives")
    data = request.get_json(silent=True) or {}
    resume_id = str(data.get("resume_id") or "").strip()
    job_id = str(data.get("job_id") or "").strip()
    if not resume_id or not job_id:
        return _err("resume_id and job_id are required", 422, "archives")

    user_id = _current_user_id()
    resume = store.get_resume(resume_id, user_id=user_id)
    if not resume:
        return _err(f"Resume not found: {resume_id}", 404, "archives")
    job = store.get_job(job_id, user_id=user_id)
    if not job:
        return _err(f"Job not found: {job_id}", 404, "archives")

    # 幂等：同一 (resume_id, job_id) 已归档则直接返回
    existing = store.find_existing_archive(user_id, resume_id, job_id)
    if existing:
        rec = store.get_archive(existing, user_id=user_id)
        return jsonify(
            {
                "request_id": rid,
                "message": "该候选人已归档",
                "data": _archive_payload(rec),
            }
        )

    # 从已存储的分析结果提取展示字段
    hr = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    job_title = ""
    p = job.get("processed") or {}
    if isinstance(p, dict):
        job_title = str(p.get("job_title") or "").strip()
    if not job_title:
        # 兜底：从 JD 原始文本首行提取
        first_line = (job.get("content") or "").strip().splitlines()
        if first_line:
            job_title = first_line[0].strip()

    candidate_name = str(data.get("candidate_name") or hr.get("candidate_name") or "").strip()
    if not candidate_name:
        # 兜底：从简历原文第一行取姓名
        first_line = (resume.get("content") or "").strip().splitlines()
        candidate_name = first_line[0].strip() if first_line else "未提供"

    try:
        final_score = int(float(hr.get("final_score", data.get("final_score", 0) or 0)))
    except (TypeError, ValueError):
        final_score = 0

    custom_tags = data.get("custom_tags")
    if not isinstance(custom_tags, list):
        custom_tags = []
    custom_tags = [str(t).strip() for t in custom_tags if str(t).strip()]

    # 岗位分类：HR 预设/自定义优先，空则回退 JD 提取的 job_title
    category = str(data.get("category") or "").strip()

    snapshot = {
        "candidate_name": candidate_name,
        "final_score": final_score,
        "fit_tag": str(hr.get("fit_tag") or ""),
        "recruitment_recommendation": str(hr.get("recruitment_recommendation") or ""),
        "job_fit_percentage": hr.get("job_fit_percentage"),
        "summary": str(hr.get("summary") or ""),
        "ai_risk_label": str(hr.get("ai_risk_label") or ""),
        "relevant_years": (hr.get("work_history") or {}).get("relevant_years"),
        "analysis_result_md": str(data.get("analysis_result") or ""),
    }

    aid = store.save_archive(
        user_id=user_id,
        resume_id=resume_id,
        job_id=job_id,
        candidate_name=candidate_name,
        final_score=final_score,
        fit_tag=str(hr.get("fit_tag") or ""),
        recruitment_recommendation=str(hr.get("recruitment_recommendation") or ""),
        job_title=job_title,
        custom_tags=custom_tags,
        analysis_snapshot=snapshot,
        category=category,
        full_analysis=hr if isinstance(hr, dict) else {},
    )
    logger.info(f"Archive created: {aid} by {user_id}")
    rec = store.get_archive(aid, user_id=user_id)
    return jsonify({"request_id": rid, "message": "归档成功", "data": _archive_payload(rec)}), 201


@app.get("/api/v1/archives")
def list_archives_api():
    """候选人才库列表（默认仅 active），支持姓名/岗位/标签筛选与排序。"""
    rid = _request_id("archives")
    user_id = _current_user_id()
    name = str(request.args.get("name") or "").strip()
    job_title = str(request.args.get("job_title") or "").strip()
    category = str(request.args.get("category") or "").strip()
    tag = str(request.args.get("tag") or "").strip()
    sort = str(request.args.get("sort") or "score").strip()
    if sort not in ("score", "created"):
        sort = "score"

    records = store.query_archives(
        user_id,
        name_keyword=name,
        job_title=job_title,
        category=category,
        tag=tag,
        sort=sort,
    )
    payloads = [_archive_payload(r) for r in records]

    # 附加聚合信息：去重后的岗位分类（供前端下拉）
    meta = {
        "job_titles": store.get_distinct_job_titles(user_id),
        "categories": store.get_distinct_categories(user_id),
    }
    return jsonify({"request_id": rid, "data": {"archives": payloads, "meta": meta}})


@app.get("/api/v1/archives/trash")
def list_trash_api():
    """回收站列表。"""
    rid = _request_id("archives")
    records = store.list_archives(_current_user_id(), status="trashed")
    return jsonify(
        {
            "request_id": rid,
            "data": {
                "archives": [_archive_payload(r) for r in records],
                "meta": {"job_titles": [], "categories": []},
            },
        }
    )


@app.get("/api/v1/archives/<archive_id>")
def get_archive_api(archive_id: str):
    """归档详情（仅 active 可见）。附完整 analysis 供重新生成快照。"""
    rid = _request_id("archives")
    rec = store.get_archive(archive_id, user_id=_current_user_id())
    if not rec:
        return _err(f"Archive not found: {archive_id}", 404, "archives")
    if rec.get("status") != "active":
        return _err("回收站中的记录不可查看详情", 400, "archives")
    return jsonify({"request_id": rid, "data": _archive_payload(rec, include_analysis=True)})


@app.patch("/api/v1/archives/<archive_id>/tags")
def update_archive_tags_api(archive_id: str):
    """更新归档自定义标签（仅 active）。"""
    rid = _request_id("archives")
    data = request.get_json(silent=True) or {}
    tags = data.get("custom_tags")
    if not isinstance(tags, list):
        return _err("custom_tags must be a list", 422, "archives")
    tags = [str(t).strip() for t in tags if str(t).strip()]

    if not store.update_archive_tags(archive_id, _current_user_id(), tags):
        return _err(f"Archive not found or not editable: {archive_id}", 404, "archives")
    rec = store.get_archive(archive_id, user_id=_current_user_id())
    return jsonify({"request_id": rid, "data": _archive_payload(rec)})


@app.patch("/api/v1/archives/<archive_id>/category")
def update_archive_category_api(archive_id: str):
    """更新岗位分类（仅 active）。预设分类或 HR 自定义字符串。"""
    rid = _request_id("archives")
    data = request.get_json(silent=True) or {}
    category = str(data.get("category") or "").strip()
    if not category:
        return _err("category is required", 422, "archives")

    if not store.update_archive_category(archive_id, _current_user_id(), category):
        return _err(f"Archive not found or not editable: {archive_id}", 404, "archives")
    rec = store.get_archive(archive_id, user_id=_current_user_id())
    return jsonify({"request_id": rid, "data": _archive_payload(rec)})


@app.delete("/api/v1/archives/<archive_id>")
def soft_delete_archive_api(archive_id: str):
    """移入回收站（软删除，可恢复）。"""
    rid = _request_id("archives")
    if not store.soft_delete_archive(archive_id, _current_user_id()):
        return _err(f"Archive not found or not active: {archive_id}", 404, "archives")
    return jsonify({"request_id": rid, "message": "已移入回收站，可在回收站恢复"})


@app.post("/api/v1/archives/<archive_id>/restore")
def restore_archive_api(archive_id: str):
    """从回收站恢复到人才库。"""
    rid = _request_id("archives")
    if not store.restore_archive(archive_id, _current_user_id()):
        return _err(f"Archive not found or not trashed: {archive_id}", 404, "archives")
    return jsonify({"request_id": rid, "message": "已恢复到人才库"})


@app.delete("/api/v1/archives/trash/<archive_id>")
def permanent_delete_archive_api(archive_id: str):
    """回收站内彻底删除单条。"""
    rid = _request_id("archives")
    if not store.permanent_delete_archive(archive_id, _current_user_id()):
        return _err(f"Archive not found or not trashed: {archive_id}", 404, "archives")
    return jsonify({"request_id": rid, "message": "已彻底删除"})


@app.delete("/api/v1/archives/trash")
def empty_trash_api():
    """清空回收站。"""
    rid = _request_id("archives")
    count = store.empty_trash(_current_user_id())
    return jsonify({"request_id": rid, "message": f"已清空回收站（{count} 条）"})


# ════════════════════════════════════════════════════════════════════
# markdown 提取工具（照搬旧版 markdown_extractor.py 逻辑）
# ════════════════════════════════════════════════════════════════════
_CODE_BLOCK_RE = re.compile(r"```(?:md|markdown)?[ \t]*\n([\s\S]+?)\n```", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,2}\s+\S+", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+\S+", re.MULTILINE)
# 识别代码块是否是「真正的简历 markdown」而非其他内容（如代码/JSON）。
# 放宽判断：很多 LLM 生成的简历首行是纯名字（无 # 标题），但有小节标题、
# 加粗、列表或分隔线等 markdown 结构。只要够长且含这些痕迹就认为是简历。
_MD_SIGNATURE_RE = re.compile(
    r"(^#{1,3}\s+\S)"           # 任意层级标题
    r"|(\*\*[^*]+\*\*)"          # 加粗 **xxx**
    r"|(^[-*]\s+\S)"             # 无序列表
    r"|(^---\s*$)",              # 分隔线
    re.MULTILINE,
)


def _extract_md_block(text: str):
    """
    从分析结果抽 ```md 代码块作为优化后的简历。返回 (md, source)。

    启发式评分替代原先的"取最长"：
      - 长度分：capped len / 100
      - 简历关键词分：包含"工作经历"/"教育背景"/"项目经验"/"技能"等核心小节 +5/项
      - 标题/列表/加粗结构 +1/项
    取得分最高的代码块；都不过关返回 None 走 fallback。
    """
    if not text:
        return None, "none"
    matches = _CODE_BLOCK_RE.findall(text)
    if not matches:
        return None, "none"

    # 简历常见中文小节标题；命中一个 +5 分
    resume_section_keywords = (
        "工作经历", "教育背景", "项目经验", "项目经历",
        "技能", "个人信息", "联系方式", "工作业绩",
        "教育经历", "实习经历", "工作项目", "工作项目经验",
    )

    def _score(candidate: str) -> int:
        score = 0
        score += min(len(candidate) // 100, 50)  # 长度分封顶 50
        for kw in resume_section_keywords:
            if kw in candidate:
                score += 5
        # markdown 结构信号
        score += len(_HEADING_RE.findall(candidate))
        score += len(_SECTION_RE.findall(candidate))
        return score

    best = max(matches, key=_score).strip()
    if _score(best) >= 5 and len(best) > 150:
        return best, "extracted"
    return None, "none"


# 用 _ALL_HEADING_RE 统计小节数（覆盖 1-3 级标题，避免漏计 ### 级）
_ALL_HEADING_RE = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)


def _count_sections(md: str) -> int:
    """统计 markdown 中的标题数量（1-3 级），用于返回 sections_detected。"""
    if not md:
        return 0
    return len(_ALL_HEADING_RE.findall(md))


# a4cv 能识别的简历小节标题关键词（与 a4cv looksLikeSectionTitle 对齐）。
# LLM 常把小节标题写成加粗 **工作经历** 而非 ## 工作经历，这里统一转成 ##，
# 让 a4cv 的 normalizeImportedMarkdown 能正确识别结构。
_A4CV_SECTION_KEYWORDS = (
    "自我评价|个人简介|职业概况|个人优势|工作经历|工作经验|项目经历|项目经验|"
    "实习经历|教育经历|教育背景|技能|技能特长|专业技能|证书|证书与荣誉|荣誉奖项|"
    "关键成果|作品集|发表论文|论文发表|社团经历|培训经历"
)
# 匹配独立的加粗小节标题行：**工作经历** 或 **工作经历**
_BOLD_SECTION_RE = re.compile(
    r"^[ \t]*\*{2}\s*(" + _A4CV_SECTION_KEYWORDS + r")\s*[:：]?\*{2}[ \t]*$",
    re.MULTILINE,
)


def _normalize_md_for_a4cv(md: str) -> str:
    """
    把 LLM 生成的简历 markdown 标准化，让 a4cv 编辑器能正确识别结构：
    1. 加粗小节标题转 ## 标题：**工作经历** → ## 工作经历
    2. 首行若是纯名字（无 #），补成 # 姓名（a4cv 靠 # 识别姓名栏）

    a4cv 的 normalizeImportedMarkdown：只要 markdown 含任何 # 标题就走
    normalizeHeadingMarkdown 直接返回，不会自动补 # 姓名。而 LLM 常把姓名
    写成纯文本首行，所以必须在这里补上。
    """
    if not md:
        return md

    def _repl(m):
        return f"## {m.group(1)}"

    md = _BOLD_SECTION_RE.sub(_repl, md)

    # 首行补 # 姓名：若首行不是标题、不是空行、也不是联系方式行，视为姓名
    lines = md.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue  # 跳过前导空行
        if stripped.startswith("#"):
            break  # 首个非空行已是标题，无需补
        # 简易判断：联系方式行（含 @ 或多个数字）不当作姓名
        if "@" in stripped or re.search(r"\d{4,}", stripped):
            break
        lines[i] = f"# {stripped}"
        break

    return "\n".join(lines)


def _build_fallback_markdown(processed: dict) -> str:
    """从结构化简历拼 a4cv 兼容的最小 markdown。"""
    pd = processed.get("personal_data") or {}
    name = pd.get("firstName") or pd.get("name") or "你的姓名"
    title = pd.get("title") or pd.get("position") or ""

    contact_bits = []
    for key in ("email", "phone"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))
    loc = pd.get("location")
    if isinstance(loc, dict):
        city = loc.get("city")
        if city:
            contact_bits.append(str(city))
    for key in ("linkedin", "portfolio"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))

    out = [f"# {name}"]
    if title:
        out.append(f"## {title}")
    if contact_bits:
        out += ["", "> " + " · ".join(contact_bits)]

    experiences = processed.get("experiences") or []
    if experiences:
        out += ["", "## 工作经历"]
        for e in experiences:
            t = e.get("job_title") or e.get("jobTitle") or "职位"
            c = e.get("company") or ""
            sd = e.get("start_date") or e.get("startDate") or ""
            ed = e.get("end_date") or e.get("endDate") or ""
            meta = " · ".join(x for x in (c, f"{sd} - {ed}" if sd else "") if x)
            out.append(f"### {t}" + (f" | {meta}" if meta else ""))
            for b in e.get("description") or []:
                if b:
                    out.append(f"- {b}")

    projects = processed.get("projects") or []
    if projects:
        out += ["", "## 项目经历"]
        for p in projects:
            t = p.get("project_name") or p.get("projectName") or "项目"
            desc = p.get("description") or ""
            out.append(f"### {t}")
            if desc:
                out.append(f"- {desc}")

    education = processed.get("education") or []
    if education:
        out += ["", "## 教育背景"]
        for ed in education:
            school = ed.get("institution") or "学校"
            degree = ed.get("degree") or ""
            out.append(f"### {school}" + (f" | {degree}" if degree else ""))

    skills = processed.get("skills") or []
    if skills:
        out += ["", "## 技能标签"]
        names = []
        for s in skills:
            if isinstance(s, dict):
                names.append(s.get("skill_name") or s.get("skillName") or "")
            else:
                names.append(str(s))
        out.append(" · ".join(filter(None, names)))

    achievements = processed.get("achievements") or []
    if achievements:
        out += ["", "## 证书与荣誉"]
        for a in achievements:
            out.append(f"- {a}")

    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    # 仅本地直接 python app.py 时用；生产请用 gunicorn app:app
    app.run(host="127.0.0.1", port=config.BACKEND_PORT, debug=(config.ENV != "production"))
