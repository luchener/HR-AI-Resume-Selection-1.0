"""
JWT 认证 + 用户管理。

设计要点：
- 无数据库依赖，用户信息存 JSON 文件（data/users/），用户名索引 O(1) 查重。
- 注册：用户名 + 密码 → 创建 user 记录，返回 JWT。
- 登录：用户名 + 密码 → 校验，返回 JWT。
- 用户名大小写不敏感：admin / Admin / ADMIN 视为同一账号。
- 密码用 PBKDF2-SHA256 + 随机盐（标准库，零额外依赖）。
- JWT 用 PyJWT（HS256，7 天过期），密钥来自 .env 的 JWT_SECRET_KEY。
- 改密/删号吊销通过"密码版本号 + 用户存在性"实现（无状态 JWT 的正确吊销方式）。
- 文件锁用 fcntl（Unix）；Windows 本地开发自动降级为进程内锁。
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import fcntl  # Unix（服务器）
except ImportError:  # Windows（本地开发/测试）
    fcntl = None  # type: ignore[assignment]

import jwt
from flask import Flask, g, jsonify, request

import config
from config import DATA_DIR

logger = logging.getLogger(__name__)


def _request_id(service: str = "auth") -> str:
    """与 app._request_id 格式一致：服务段:uuid。"""
    return f"{service}:{uuid.uuid4()}"


# ── JWT 配置 ────────────────────────────────────────────────────────────
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 天（账号本身永久保存，过期仅需重新登录）

# 免认证路径白名单（精确匹配 path，不含 query）
_PUBLIC_PATHS = {
    "/ping",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/email-code/send",
    "/api/v1/auth/reset-password/request",
    "/api/v1/auth/reset-password/confirm",
}


# ═══════════════════════════════════════════════════════════════════════
# 用户存储（JSON 文件，data/users/<user_id>.json）
# ═══════════════════════════════════════════════════════════════════════

USERS_DIR = os.path.join(DATA_DIR, "users")
os.makedirs(USERS_DIR, exist_ok=True)

# 密码重置 Token 存储（只存哈希，防文件泄露被直接利用）
RESETS_DIR = os.path.join(DATA_DIR, "password_resets")
os.makedirs(RESETS_DIR, exist_ok=True)

# 用户名索引文件：username_lower → user_id，O(1) 查重/查找
_INDEX_FILE = os.path.join(USERS_DIR, "_index.json")
_INDEX_LOCK = os.path.join(USERS_DIR, "_index.lock")


def _write_json(path: str, data: dict) -> None:
    """原子写：临时文件 + fsync + os.replace，避免半写文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    """读取 JSON 文件，容错处理损坏/缺失文件。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read %s: %s", path, e)
        return None


# ── 用户名索引（O(1) 查重 & 查找）──────────────────────────────────────

def _read_index() -> dict[str, str]:
    """读取用户名索引 {username_lower: user_id}。"""
    data = _read_json(_INDEX_FILE)
    if isinstance(data, dict):
        return data
    return {}


def _write_index(index: dict[str, str]) -> None:
    _write_json(_INDEX_FILE, index)


def _rebuild_index() -> dict[str, str]:
    """从用户文件重建索引（首次运行迁移或索引损坏时使用）。"""
    index: dict[str, str] = {}
    if not os.path.isdir(USERS_DIR):
        return index
    for filename in os.listdir(USERS_DIR):
        if not filename.endswith(".json") or filename.startswith("_"):
            continue
        user = _read_json(os.path.join(USERS_DIR, filename))
        if user and user.get("username"):
            index[user["username"].lower()] = user["user_id"]
    _write_index(index)
    return index


# ── 文件锁（防注册/删除竞态）───────────────────────────────────────────

_LOCK = threading.Lock()


class _UserLock:
    """跨进程文件锁（Unix）/ 进程内锁（Windows 降级）。"""

    def __enter__(self):
        if fcntl is not None:
            # 锁文件可能与用户目录一起被重定向到新路径（如测试环境），先确保父目录存在
            os.makedirs(os.path.dirname(_INDEX_LOCK), exist_ok=True)
            self._fd = open(_INDEX_LOCK, "w")
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        else:
            self._fd = None
            _LOCK.acquire()
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._fd.close()
        else:
            _LOCK.release()


# ═══════════════════════════════════════════════════════════════════════
# 密码哈希（PBKDF2-SHA256，标准库实现，零额外依赖）
# ═══════════════════════════════════════════════════════════════════════

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """返回 (hashed_b64, salt_b64)。salt 为 None 时随机生成。"""
    if salt is None:
        salt = base64.b64encode(os.urandom(16)).decode("ascii")
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        iterations=_PBKDF2_ITERATIONS,
    )
    hashed = base64.b64encode(dk).decode("ascii")
    return hashed, salt


def verify_password(password: str, hashed_b64: str, salt_b64: str) -> bool:
    """常量时间比较，防止时序侧信道；非法哈希容错返回 False。"""
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt_b64.encode("ascii"),
            iterations=_PBKDF2_ITERATIONS,
        )
        return hmac.compare_digest(dk, base64.b64decode(hashed_b64))
    except (binascii.Error, ValueError):
        return False


# ═══════════════════════════════════════════════════════════════════════
# JWT 工具
# ═══════════════════════════════════════════════════════════════════════

def _jwt_secret() -> str:
    secret = config.JWT_SECRET_KEY
    if not secret:
        raise RuntimeError(
            "JWT_SECRET_KEY is not configured. "
            "Set it in .env or environment variables before starting."
        )
    return secret


def generate_jwt(user_id: str, username: str) -> str:
    """
    生成 JWT（HS256，7 天过期）。
    payload 带 pwd_ver（密码版本号）：改密后旧 token 自动失效。
    """
    user = _read_json(os.path.join(USERS_DIR, f"{user_id}.json"))
    pwd_ver = (user or {}).get("password_version", 1)
    now = time.time()
    payload: dict[str, Any] = {
        "user_id": user_id,
        "username": username,
        "pwd_ver": pwd_ver,
        "iss": "resume-matcher",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + JWT_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


class _JwtError(Exception):
    """JWT 解码失败，携带面向用户的安全错误信息。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def decode_jwt(token: str) -> dict:
    """
    解码并校验 JWT。成功返回 payload dict，失败抛 _JwtError。
    """
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["user_id", "username", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise _JwtError("Token 已过期，请重新登录") from e
    except jwt.InvalidTokenError as e:
        raise _JwtError(f"Token 无效: {e}") from e

    if not isinstance(payload, dict):
        raise _JwtError("JWT payload is not an object")
    user_id = payload.get("user_id")
    if not user_id or not payload.get("username"):
        raise _JwtError("JWT missing required claims")

    # 无状态吊销：用户被删除 → token 失效；密码被修改 → 旧 token 失效
    user = _read_json(os.path.join(USERS_DIR, f"{user_id}.json"))
    if user is None:
        raise _JwtError("账户不存在或已被删除，请重新登录")
    if payload.get("pwd_ver", 1) != user.get("password_version", 1):
        raise _JwtError("密码已修改，请重新登录")

    return payload


# ═══════════════════════════════════════════════════════════════════════
# 用户 CRUD
# ═══════════════════════════════════════════════════════════════════════

def _validate_password_strength(password: str) -> Optional[str]:
    """密码强度校验。返回 None 表示通过，否则返回错误信息。"""
    if len(password) < 8:
        return "密码长度至少 8 个字符"
    if not re.search(r"[A-Za-z]", password):
        return "密码必须包含至少一个字母"
    if not re.search(r"\d", password):
        return "密码必须包含至少一个数字"
    return None


def create_user(
    username: str, password: str, email: str = ""
) -> tuple[Optional[dict], Optional[str]]:
    """
    创建用户。成功返回 (user_record, None)，失败返回 (None, error_message)。
    用户名大小写不敏感：admin / Admin / ADMIN 视为同名（保留首次注册大小写显示）。
    密码校验：至少 8 字符，包含字母和数字。
    email 用于密码重置：需为合法邮箱且全局唯一（大小写不敏感）。可选传空。
    """
    if not re.match(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{2,64}$", username):
        return None, "用户名仅允许字母、数字、下划线、连字符或中文，长度 2-64 字符"

    pwd_err = _validate_password_strength(password)
    if pwd_err:
        return None, pwd_err

    email_clean = email.strip().lower() if email else ""
    if email_clean and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        return None, "邮箱格式不正确"

    username_lower = username.lower()

    # 文件锁保护整个「查重 → 写入」流程，消除并发注册竞态
    with _UserLock():
        index = _read_index()
        if not index:
            index = _rebuild_index()  # 首次运行自动重建

        if username_lower in index:
            return None, f"用户名 '{username}' 已被注册 (大小写不敏感)"

        # 邮箱查重（大小写不敏感），扫描用户文件
        if email_clean:
            for filename in os.listdir(USERS_DIR):
                if not filename.endswith(".json") or filename.startswith("_"):
                    continue
                existing = _read_json(os.path.join(USERS_DIR, filename))
                if existing and (existing.get("email") or "").lower() == email_clean:
                    return None, "该邮箱已被注册"

        user_id = str(uuid.uuid4())
        hashed, salt = hash_password(password)
        record: dict[str, Any] = {
            "user_id": user_id,
            "username": username,
            "email": email_clean,
            "password_hash": hashed,
            "password_salt": salt,
            "password_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(os.path.join(USERS_DIR, f"{user_id}.json"), record)

        index[username_lower] = user_id
        _write_index(index)

        return record, None


def find_user_by_username(username: str) -> Optional[dict]:
    """按用户名查找用户（大小写不敏感），通过索引 O(1) 定位。"""
    index = _read_index()
    if not index:
        index = _rebuild_index()
    user_id = index.get(username.lower())
    if not user_id:
        return None
    return _read_json(os.path.join(USERS_DIR, f"{user_id}.json"))


def find_user_by_email(email: str) -> Optional[dict]:
    """按邮箱查找用户（大小写不敏感）。用于密码重置。"""
    email_clean = email.strip().lower()
    if not email_clean:
        return None
    if not os.path.isdir(USERS_DIR):
        return None
    for filename in os.listdir(USERS_DIR):
        if not filename.endswith(".json") or filename.startswith("_"):
            continue
        user = _read_json(os.path.join(USERS_DIR, filename))
        if user and (user.get("email") or "").lower() == email_clean:
            return user
    return None


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """用户名密码校验，成功返回用户记录，失败返回 None。"""
    user = find_user_by_username(username)
    if not user:
        # 用户不存在也跑一遍完整哈希，保持时间特征一致，防用户名枚举
        hash_password(password)
        return None
    if not verify_password(password, user["password_hash"], user["password_salt"]):
        return None
    return user


def update_password(user_id: str, new_password: str) -> tuple[bool, Optional[str]]:
    """修改用户密码：密码版本 +1，使该用户所有旧 token 立即失效。"""
    pwd_err = _validate_password_strength(new_password)
    if pwd_err:
        return False, pwd_err

    path = os.path.join(USERS_DIR, f"{user_id}.json")
    user = _read_json(path)
    if not user:
        return False, "用户不存在"

    hashed, salt = hash_password(new_password)
    user["password_hash"] = hashed
    user["password_salt"] = salt
    user["password_version"] = user.get("password_version", 1) + 1
    user["password_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, user)

    return True, None


def delete_user(user_id: str) -> bool:
    """删除用户（其所有 token 因用户不存在而自动失效）。"""
    path = os.path.join(USERS_DIR, f"{user_id}.json")
    user = _read_json(path)
    if not user:
        return False

    with _UserLock():
        if os.path.exists(path):
            os.remove(path)
        index = _read_index()
        username_lower = user.get("username", "").lower()
        index.pop(username_lower, None)
        _write_index(index)

    return True


# ═══════════════════════════════════════════════════════════════════════
# 邮件验证码（注册绑定邮箱 + 忘记密码重置）—— 统一机制，按邮箱存储
# ═══════════════════════════════════════════════════════════════════════

# 验证码字符集（全大写字母+数字，排除易混淆 0O1lI），6 位（36^6 ≈ 21.8 亿组合）
# 统一大写：邮件里显示大写，用户填写大小写均接受（校验时统一转大写）
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 同一邮箱申请验证码的最短间隔（秒）
_CODE_SEND_MIN_INTERVAL = 60
# 同一验证码最多允许的错误尝试次数（超过则作废，防爆破）
_CODE_MAX_ATTEMPTS = 5

# 用途常量
PURPOSE_EMAIL_VERIFY = "email_verify"      # 注册：绑定邮箱
PURPOSE_RESET_PASSWORD = "reset_password"  # 重置：忘记密码


def create_email_code(
    email: str, purpose: str, ttl_seconds: int | None = None
) -> str:
    """
    为指定邮箱生成一次性 6 位验证码（下单前需先通过频率限制）。
    成功返回明文码（用于发邮件）；若该邮箱在冷却期内，返回空字符串。
    磁盘只存 SHA-256 哈希。
    """
    ttl = ttl_seconds if ttl_seconds else config.RESET_TOKEN_TTL_SECONDS
    now = datetime.now(timezone.utc).timestamp()
    email_clean = email.strip().lower()

    with _UserLock():
        # 频率限制：同邮箱最近一次申请距今必须大于最小间隔
        if _recent_code_for_email(email_clean, purpose) is not None:
            return ""

        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        record = {
            "email": email_clean,
            "purpose": purpose,
            "code_hash": code_hash,
            "created_at": now,
            "expires_at": now + ttl,
            "used": False,
            "attempts": 0,
        }
        _write_json(
            os.path.join(RESETS_DIR, f"{purpose}.{code_hash}.json"), record
        )
        _prune_email_codes()
        return code


def _recent_code_for_email(email: str, purpose: str) -> Optional[dict]:
    """返回该邮箱+用途最近一条仍有效的验证码（用于频率限制）；无则 None。"""
    now = datetime.now(timezone.utc).timestamp()
    newest: Optional[dict] = None
    newest_created = 0.0
    for filename in os.listdir(RESETS_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            rec = _read_json(os.path.join(RESETS_DIR, filename))
            if not rec or rec.get("purpose") != purpose:
                continue
            if (rec.get("email") or "").lower() != email:
                continue
            if rec.get("created_at", 0) > newest_created:
                newest_created = rec.get("created_at", 0)
                newest = rec
        except OSError:
            continue
    return newest


def can_request_code(email: str, purpose: str) -> bool:
    """同邮箱+用途是否处于申请冷却期（用于返回友好提示）。"""
    recent = _recent_code_for_email(email, purpose)
    if recent is None:
        return True
    now = datetime.now(timezone.utc).timestamp()
    return (now - recent.get("created_at", 0)) >= _CODE_SEND_MIN_INTERVAL


def verify_email_code_once(email: str, purpose: str, code: str) -> bool:
    """消费式校验验证码。成功删除记录返回 True；失败累计 attempts，超限作废。"""
    if not code or not email:
        return False
    email_clean = email.strip().lower()
    code_hash = hashlib.sha256(code.strip().upper().encode("ascii")).hexdigest()
    path = os.path.join(RESETS_DIR, f"{purpose}.{code_hash}.json")

    with _UserLock():
        rec = _read_json(path)
        if not rec or (rec.get("email") or "").lower() != email_clean:
            return False
        if rec.get("used"):
            return False
        if rec.get("expires_at", 0) < datetime.now(timezone.utc).timestamp():
            _safe_remove(path)
            return False
        attempts = int(rec.get("attempts", 0))
        if attempts >= _CODE_MAX_ATTEMPTS:
            _safe_remove(path)
            return False
        # 路径即哈希，能读到此文件即说明码匹配 → 消费成功
        rec["used"] = True
        _write_json(path, rec)
        # 消费后删除（防重放）
        _safe_remove(path)
        return True


def register_with_email_code(
    username: str, password: str, email: str, code: str
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """
    带邮箱验证码注册。返回 (user_record, None, None) 成功。
    失败返回 (None, error_message, field)，field 用于前端定位：'email_code'/'username'/'password'。
    """
    # 先消费式校验邮箱验证码，失败则不建号
    if not verify_email_code_once(email, PURPOSE_EMAIL_VERIFY, code):
        return None, "邮箱验证码无效或已过期，请重新发送。", "email_code"

    user, err = create_user(username, password, email)
    if err:
        # 建号失败（如用户名已存在）；验证码已消费，前端需重新发码
        return None, err, "username"
    return user, None, None


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_email_codes() -> None:
    """删除所有过期/已用的验证码记录。"""
    now = datetime.now(timezone.utc).timestamp()
    for filename in os.listdir(RESETS_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            rec = _read_json(os.path.join(RESETS_DIR, filename))
            if not rec:
                _safe_remove(os.path.join(RESETS_DIR, filename))
                continue
            if rec.get("expires_at", 0) < now or rec.get("used"):
                _safe_remove(os.path.join(RESETS_DIR, filename))
        except OSError:
            continue


def reset_password_with_code(email: str, code: str, new_password: str) -> tuple[bool, Optional[str]]:
    """
    用邮箱验证码重置密码：校验验证码 → 更新密码（password_version+1 → 吊销旧 token）。
    """
    pwd_err = _validate_password_strength(new_password)
    if pwd_err:
        return False, pwd_err

    user = find_user_by_email(email)
    if not user or not user.get("email"):
        return False, "该邮箱未绑定账号，请先注册或使用管理员重置工具。"

    if not verify_email_code_once(email, PURPOSE_RESET_PASSWORD, code):
        return False, "验证码无效或已过期，请重新申请。"

    hashed, salt = hash_password(new_password)
    user["password_hash"] = hashed
    user["password_salt"] = salt
    user["password_version"] = user.get("password_version", 1) + 1
    user["password_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(os.path.join(USERS_DIR, f"{user['user_id']}.json"), user)
    return True, None


# ═══════════════════════════════════════════════════════════════════════
# Flask 认证中间件
# ═══════════════════════════════════════════════════════════════════════

class _AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def require_auth() -> None:
    """Flask before_request 中间件：校验 JWT，注入 g.auth_user。"""
    if request.method == "OPTIONS":
        return  # CORS 预检放行（无 Authorization 头，避免误判 401）
    path = request.path
    if path in _PUBLIC_PATHS:
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _AuthError("请先登录。", 401)

    token = auth_header[len("Bearer "):].strip()
    try:
        payload = decode_jwt(token)
    except _JwtError as e:
        raise _AuthError(e.message, 401) from e

    g.auth_user = {
        "user_id": payload["user_id"],
        "username": payload["username"],
    }


def _handle_auth_error(e: _AuthError):
    """统一认证错误响应，带 WWW-Authenticate header。"""
    from flask import Response as _Response

    resp = _Response(
        json.dumps({"detail": e.message, "request_id": _request_id("auth")}, ensure_ascii=False),
        status=e.status,
        mimetype="application/json",
    )
    if e.status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="resume-matcher"'
    return resp


def init_auth(app: Flask) -> None:
    """
    在 Flask app 上注册认证中间件和错误处理器（集中注册，避免遗漏）。
    app.py 若已用 before_request + errorhandler 等价注册，则无需重复调用。
    """
    app.before_request(require_auth)
    app.register_error_handler(_AuthError, _handle_auth_error)