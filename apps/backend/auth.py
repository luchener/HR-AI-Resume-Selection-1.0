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
from datetime import datetime, timedelta, timezone
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

# 管理员邮箱白名单（config.ADMIN_EMAILS，默认 luchenstudio@163.com）
ADMIN_EMAILS: list[str] = list(config.ADMIN_EMAILS)
# 对外展示的联系邮箱（冻结/拒收文案提示用）
CONTACT_EMAIL = ADMIN_EMAILS[0] if ADMIN_EMAILS else "luchenstudio@163.com"


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
    "/api/v1/auth/invite-code/check",
    "/api/v1/auth/invite-code/send-email-code",
    "/api/v1/auth/unfreeze",
    "/api/v1/auth/unfreeze/send-code",
    "/api/v1/invite-request",
    "/api/v1/auth/register-config",
    "/api/v1/auth/captcha",
}

# 管理员接口前缀（require_admin 按此识别）
ADMIN_PATH_PREFIX = "/api/v1/admin/"


# ═══════════════════════════════════════════════════════════════════════
# 用户存储（JSON 文件，data/users/<user_id>.json）
# ═══════════════════════════════════════════════════════════════════════

USERS_DIR = os.path.join(DATA_DIR, "users")
os.makedirs(USERS_DIR, exist_ok=True)

# 密码重置 Token 存储（只存哈希，防文件泄露被直接利用）
RESETS_DIR = os.path.join(DATA_DIR, "password_resets")
os.makedirs(RESETS_DIR, exist_ok=True)

# 邀请码 / 申请单 / 登录失败 / IP 限流 存储目录
INVITE_REQUESTS_DIR = config.INVITE_REQUESTS_DIR
INVITE_CODES_DIR = config.INVITE_CODES_DIR
INVITE_CODES_AUDIT_DIR = config.INVITE_CODES_AUDIT_DIR
LOGIN_FAILURES_DIR = config.LOGIN_FAILURES_DIR
RATE_LIMITS_DIR = config.RATE_LIMITS_DIR
ADMIN_OPS_DIR = config.ADMIN_OPS_DIR
USER_USAGE_DIR = config.USER_USAGE_DIR
ADMIN_OPS_RETENTION_DAYS = config.ADMIN_OPS_RETENTION_DAYS
USER_USAGE_RETENTION_DAYS = config.USER_USAGE_RETENTION_DAYS
PRUNE_TOUCH_FILE = config.PRUNE_TOUCH_FILE
for _d in (
    INVITE_REQUESTS_DIR,
    INVITE_CODES_DIR,
    INVITE_CODES_AUDIT_DIR,
    LOGIN_FAILURES_DIR,
    RATE_LIMITS_DIR,
    ADMIN_OPS_DIR,
    USER_USAGE_DIR,
):
    os.makedirs(_d, exist_ok=True)

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

_LOCK = threading.RLock()
# Unix 下按线程缓存锁文件 fd：同线程嵌套 flock 同一 fd 幂等（可重入），
# 不同线程/进程各自独立 fd → 仍互斥。Windows 用 RLock（进程内可重入互斥）。
_UNIX_LOCK_FDS: dict[int, Any] = {}


class _UserLock:
    """跨进程文件锁（Unix）/ 进程内锁（Windows 降级）。支持同线程嵌套（可重入）。"""

    def __enter__(self):
        if fcntl is not None:
            # 锁文件可能与用户目录一起被重定向到新路径（如测试环境），先确保父目录存在
            os.makedirs(os.path.dirname(_INDEX_LOCK), exist_ok=True)
            tid = threading.get_ident()
            fd = _UNIX_LOCK_FDS.get(tid)
            if fd is None:
                fd = open(_INDEX_LOCK, "w")
                _UNIX_LOCK_FDS[tid] = fd
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            _LOCK.acquire()
        return self

    def __exit__(self, *exc):
        if fcntl is not None:
            tid = threading.get_ident()
            fd = _UNIX_LOCK_FDS.get(tid)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
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
    if user.get("deleted_at"):
        raise _JwtError("账户不存在或已被删除，请重新登录")
    if payload.get("pwd_ver", 1) != user.get("password_version", 1):
        raise _JwtError("密码已修改，请重新登录")

    return payload


# ── 登录验证码（四位数字，一次性，TTL 5 分钟）────────────────────────
CAPTCHA_TTL_SECONDS = config.CAPTCHA_TTL_SECONDS
CAPTCHA_ENABLED = config.CAPTCHA_ENABLED


def _captcha_file(captcha_id: str) -> str:
    return os.path.join(RATE_LIMITS_DIR, f"captcha_{captcha_id}.json")


def _purge_stale_captchas() -> None:
    """清理已过期验证码文件（每次生成时顺手做，避免目录膨胀）。"""
    if not os.path.isdir(RATE_LIMITS_DIR):
        return
    now = time.time()
    for filename in os.listdir(RATE_LIMITS_DIR):
        if not filename.startswith("captcha_"):
            continue
        path = os.path.join(RATE_LIMITS_DIR, filename)
        try:
            if now - os.path.getmtime(path) > CAPTCHA_TTL_SECONDS:
                _safe_remove(path)
        except OSError:
            pass


def _hash_captcha(code: str) -> str:
    return hashlib.sha256(f"captcha:{code}".encode("utf-8")).hexdigest()


def new_captcha() -> tuple[str, str]:
    """生成四位数字验证码，返回 (captcha_id, code)。code 明文仅本次响应持有。"""
    _purge_stale_captchas()
    captcha_id = str(uuid.uuid4())
    code = "".join(secrets.choice("0123456789") for _ in range(4))
    _write_json(
        _captcha_file(captcha_id),
        {"code_hash": _hash_captcha(code), "expires_at": time.time() + CAPTCHA_TTL_SECONDS},
    )
    return captcha_id, code


def verify_captcha(captcha_id: str, code: str) -> bool:
    """一次性校验：无论成功失败均销毁，防止重放。"""
    if not captcha_id or not code:
        return False
    path = _captcha_file(captcha_id)
    record = _read_json(path)
    if not record:
        return False
    _safe_remove(path)  # 一次性
    if time.time() > record.get("expires_at", 0):
        return False
    return secrets.compare_digest(record.get("code_hash", ""), _hash_captcha(code))


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
    if user.get("deleted_at"):
        # 软删除账号不允许登录（文件保留，便于恢复）
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


# 软删除恢复窗口（删除后 N 天内可恢复，超过则不可恢复）
DELETED_RESTORE_WINDOW_DAYS = 90


def delete_user(user_id: str, operator: str = "") -> bool:
    """
    软删除用户：保留用户文件与索引，仅标记 deleted_at。
    恢复窗口内可用 restore_user 找回；期间该账号无法登录。
    """
    path = os.path.join(USERS_DIR, f"{user_id}.json")
    user = _read_json(path)
    if not user:
        return False

    with _UserLock():
        user = _read_json(path)
        if not user:
            return False
        user["deleted_at"] = datetime.now(timezone.utc).isoformat()
        user["deleted_by"] = operator or "admin"
        _write_json(path, user)
        # 清理登录失败记录（恢复后从零开始累计）
        username_lower = str(user.get("username") or "").lower()
        _safe_remove(os.path.join(LOGIN_FAILURES_DIR, f"{_login_fail_hash(username_lower)}.json"))
        if username_lower in _read_index():
            pass  # 索引保留，恢复时无需重建

    return True


def list_deleted_users(window_days: int = DELETED_RESTORE_WINDOW_DAYS) -> list[dict]:
    """列出删除时间在恢复窗口内的软删除用户（脱敏），按删除时间倒序。"""
    users = []
    if os.path.isdir(USERS_DIR):
        for filename in os.listdir(USERS_DIR):
            if not filename.endswith(".json") or filename.startswith("_"):
                continue
            user = _read_json(os.path.join(USERS_DIR, filename))
            if not user or not user.get("deleted_at"):
                continue
            try:
                deleted_dt = datetime.fromisoformat(user["deleted_at"])
            except (TypeError, ValueError):
                continue
            if datetime.now(timezone.utc) - deleted_dt > timedelta(days=window_days):
                continue
            users.append({
                "user_id": user.get("user_id"),
                "username": user.get("username"),
                "email": user.get("email") or "",
                "deleted_at": user.get("deleted_at"),
                "deleted_by": user.get("deleted_by") or "",
            })
    users.sort(key=lambda u: str(u.get("deleted_at") or ""), reverse=True)
    return users


def restore_user(username: str, window_days: int = DELETED_RESTORE_WINDOW_DAYS) -> tuple[bool, Optional[str]]:
    """恢复软删除用户。仅删除时间在窗口内可恢复；返回 (success, error)。"""
    user = find_user_by_username(username)
    if not user:
        return False, "用户不存在"
    deleted_at = user.get("deleted_at")
    if not deleted_at:
        return False, "该账号未被删除，无需恢复"
    try:
        deleted_dt = datetime.fromisoformat(str(deleted_at))
    except (TypeError, ValueError):
        return False, "删除记录异常，无法恢复"

    if datetime.now(timezone.utc) - deleted_dt > timedelta(days=window_days):
        return False, "已超过可恢复期限（删除后 90 天内可恢复）"

    with _UserLock():
        user = find_user_by_username(username)
        if not user or not user.get("deleted_at"):
            return False, "账号状态已变化，请刷新后重试"
        user.pop("deleted_at", None)
        user.pop("deleted_by", None)
        # 恢复后旧 token 全部失效，需重新登录
        user["password_version"] = user.get("password_version", 1) + 1
        user["restored_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(os.path.join(USERS_DIR, f"{user['user_id']}.json"), user)

    logger.info("[admin] user restored username=%s", username)
    return True, None


def is_user_deleted(username: str) -> bool:
    """该账号是否处于已删除（软删除）状态。"""
    user = find_user_by_username(username)
    return bool(user and user.get("deleted_at"))


# ═══════════════════════════════════════════════════════════════════════
# 管理员账户 + 用户管理（CRUD；管理员判定 = 白名单邮箱 OR 用户 is_admin 标记）
# ═══════════════════════════════════════════════════════════════════════

def is_admin(user: Optional[dict]) -> bool:
    """
    综合管理员判定：.env 白名单邮箱（ADMIN_EMAILS）或用户记录 is_admin 标记。
    user 可为 g.auth_user（含 email）或完整用户记录。
    """
    if not user:
        return False
    email = (user.get("email") or "").strip().lower()
    if is_admin_email(email):
        return True
    return bool(user.get("is_admin"))


def is_super_admin(user: Optional[dict]) -> bool:
    """
    超级管理员判定：仅 .env 白名单邮箱（ADMIN_EMAILS）命中的账号。
    只有超级管理员可以分配/取消其他账号的管理员权限。
    """
    if not user:
        return False
    email = (user.get("email") or "").strip().lower()
    return is_admin_email(email)


def list_users(
    keyword: str = "",
    page: int = 1,
    size: int = 50,
) -> list[dict]:
    """
    用户列表（脱敏：不含 password_hash / password_salt / token 等）。
    keyword 匹配用户名/邮箱（大小写不敏感，子串包含）。按 created_at 倒序。
    """
    users = []
    if os.path.isdir(USERS_DIR):
        for filename in os.listdir(USERS_DIR):
            if not filename.endswith(".json") or filename.startswith("_"):
                continue
            user = _read_json(os.path.join(USERS_DIR, filename))
            if not user or not user.get("username"):
                continue
            if user.get("deleted_at"):
                continue  # 软删除账号不出现在普通列表，恢复走专用接口
            users.append(user)

    kw = (keyword or "").strip().lower()
    if kw:
        users = [
            u
            for u in users
            if kw in (u.get("username") or "").lower()
            or kw in (u.get("email") or "").lower()
        ]

    users.sort(key=lambda u: str(u.get("created_at") or ""), reverse=True)

    # 分页
    start = max(0, (max(1, page) - 1) * max(1, size))
    page_users = users[start : start + max(1, size)]

    out = []
    for u in page_users:
        out.append(
            {
                "user_id": u.get("user_id"),
                "username": u.get("username"),
                "email": u.get("email") or "",
                "is_admin": is_admin(u),
                "frozen": is_user_frozen(u.get("username") or ""),
                "email_bound": bool(u.get("email")),
                "created_at": u.get("created_at"),
                "password_updated_at": u.get("password_updated_at"),
            }
        )
    return out


def count_users(keyword: str = "") -> int:
    """按相同过滤条件统计用户总数（分页用）。"""
    return len(list_users(keyword=keyword, page=1, size=10**9))


def find_user_by_id(user_id: str) -> Optional[dict]:
    """按 user_id 查找用户。"""
    if not user_id:
        return None
    return _read_json(os.path.join(USERS_DIR, f"{user_id}.json"))


def update_user_admin(username: str, is_admin_flag: bool, operator: str = "") -> tuple[bool, Optional[str]]:
    """
    设置/取消用户的管理员标记（白名单邮箱用户始终有效，不可被取消）。
    返回值 (success, error)。成功时同步刷新 ADMIN_EMAILS 白名单不可变约束。
    """
    user = find_user_by_username(username)
    if not user:
        return False, "用户不存在"

    # 白名单邮箱用户：管理员身份来自 .env，界面不可取消（防锁死）
    if (user.get("email") or "").strip().lower() in ADMIN_EMAILS:
        return False, "该用户由 .env 白名单配置为管理员，请在服务器 .env 中调整"

    with _UserLock():
        user = find_user_by_username(username)
        if not user:
            return False, "用户不存在"
        user["is_admin"] = bool(is_admin_flag)
        _write_json(os.path.join(USERS_DIR, f"{user['user_id']}.json"), user)
        _rebuild_index()  # 索引本身不含 is_admin，仅确保文件状态一致

    logger.info(
        "[admin] user admin flag set username=%s is_admin=%s operator=%s",
        username,
        bool(is_admin_flag),
        operator or "-",
    )
    return True, None


def admin_update_email(username: str, new_email: str, operator: str = "") -> tuple[bool, Optional[str]]:
    """管理员修改用户邮箱（白名单邮箱用户不允许改，防止破坏管理员身份）。"""
    email_clean = (new_email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        return False, "邮箱格式不正确"

    user = find_user_by_username(username)
    if not user:
        return False, "用户不存在"

    # 邮箱唯一（大小写不敏感）
    if user.get("email") or "":
        if (user["email"]).strip().lower() in ADMIN_EMAILS:
            return False, "该用户由 .env 白名单配置为管理员，不可修改邮箱"

    with _UserLock():
        existing = find_user_by_email(email_clean)
        if existing and existing.get("user_id") != user.get("user_id"):
            return False, "该邮箱已被其他用户使用"
        user["email"] = email_clean
        user["email_updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(os.path.join(USERS_DIR, f"{user['user_id']}.json"), user)

    logger.info("[admin] user email updated username=%s operator=%s", username, operator or "-")
    return True, None


def admin_reset_password(username: str, operator: str = "") -> tuple[bool, Optional[str], Optional[str]]:
    """
    管理员重置用户密码。返回 (success, error, temp_password)。
    成功时生成 12 位临时密码，密码版本 +1 使旧 token 全部失效。
    """
    user = find_user_by_username(username)
    if not user:
        return False, "用户不存在", None

    temp = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(12))
    ok, err = update_password(user["user_id"], temp)
    if not ok:
        return False, err, None

    logger.info("[admin] password reset username=%s operator=%s", username, operator or "-")
    return True, None, temp


# ═══════════════════════════════════════════════════════════════════════
# 管理操作审计（删除记录/权限变更/创建/重置密码 等留痕，可查询）
# ═══════════════════════════════════════════════════════════════════════

# 管理操作类型（与前端「操作记录」Tab 过滤联动）
ADMIN_OP_TYPES = (
    "user_create",
    "user_delete",
    "user_restore",
    "admin_grant",
    "admin_revoke",
    "email_update",
    "pwd_reset",
    "user_freeze",
    "user_unfreeze",
)


def record_admin_op(
    op: str,
    operator_id: str,
    operator_name: str,
    target_username: str,
    detail: str = "",
    request_id: str = "",
) -> str:
    """
    追加一条管理操作审计记录（原子写 data/admin_ops/<ts>_<uuid>.json）。
    供删除用户 / 创建用户 / 分配管理员 / 重置密码 等管理动作调用。
    """
    rec = {
        "op": op,
        "operator_id": operator_id or "",
        "operator_name": operator_name or "",
        "target_username": target_username or "",
        "detail": detail or "",
        "request_id": request_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = os.path.join(ADMIN_OPS_DIR, f"{ts}_{uuid.uuid4().hex[:12]}.json")
    with _UserLock():
        _write_json(path, rec)
    logger.info("[admin-op] %s target=%s operator=%s", op, target_username, operator_name or "-")
    return rec["created_at"]


def list_admin_ops(
    op: str = "",
    keyword: str = "",
    page: int = 1,
    size: int = 50,
) -> list[dict]:
    """管理操作审计列表（新→旧）。keyword 匹配操作者/目标用户名，op 精确过滤。"""
    maybe_prune()  # 保留策略：懒清理过期审计
    recs = []
    if os.path.isdir(ADMIN_OPS_DIR):
        for filename in os.listdir(ADMIN_OPS_DIR):
            if not filename.endswith(".json"):
                continue
            rec = _read_json(os.path.join(ADMIN_OPS_DIR, filename))
            if not rec:
                continue
            recs.append(rec)
    kw = (keyword or "").strip().lower()
    if op:
        recs = [r for r in recs if r.get("op") == op]
    if kw:
        recs = [
            r
            for r in recs
            if kw in (r.get("operator_name") or "").lower()
            or kw in (r.get("target_username") or "").lower()
        ]
    recs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    start = max(0, (max(1, page) - 1) * max(1, size))
    return recs[start : start + max(1, size)]


def count_admin_ops(op: str = "", keyword: str = "") -> int:
    return len(list_admin_ops(op=op, keyword=keyword, page=1, size=10**9))


# ── 保留策略：审计 / 使用统计 懒清理（每日最多一次，随查询触发）─────────

def _prune_due() -> bool:
    """懒清理哨兵：距上次清理是否已超过 24h。"""
    try:
        mtime = os.path.getmtime(PRUNE_TOUCH_FILE)
        if time.time() - mtime < 86400:
            return False
    except OSError:
        pass
    try:
        with open(PRUNE_TOUCH_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass
    return True


def prune_admin_ops(days: int) -> int:
    """删除超过 days 天的管理操作审计文件，返回删除条数。"""
    if days <= 0 or not os.path.isdir(ADMIN_OPS_DIR):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    with _UserLock():
        for filename in os.listdir(ADMIN_OPS_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(ADMIN_OPS_DIR, filename)
            rec = _read_json(path)
            created = rec.get("created_at") if rec else ""
            try:
                ts = datetime.fromisoformat(str(created))
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    if removed:
        logger.info("[prune] admin_ops: removed %d records (older than %d days)", removed, days)
    return removed


def prune_user_usage(days: int) -> int:
    """删除超过 days 天的用户使用统计明细（按天键），空文件删除，返回清理的用户文件数。"""
    if days <= 0 or not os.path.isdir(USER_USAGE_DIR):
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    touched = 0
    for filename in os.listdir(USER_USAGE_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(USER_USAGE_DIR, filename)
        with _UserLock():
            data = _read_json(path) or {}
            if not data:
                continue
            fresh = {k: v for k, v in data.items() if not isinstance(k, str) or k >= cutoff}
            if len(fresh) == len(data):
                continue
            if fresh:
                _write_json(path, fresh)
            else:
                try:
                    os.remove(path)
                except OSError:
                    continue
            touched += 1
    if touched:
        logger.info("[prune] user_usage: pruned %d files (older than %d days)", touched, days)
    return touched


def maybe_prune() -> None:
    """懒清理入口：每日最多执行一次，同时清理审计与使用统计。"""
    if not _prune_due():
        return
    try:
        prune_admin_ops(ADMIN_OPS_RETENTION_DAYS)
        prune_user_usage(USER_USAGE_RETENTION_DAYS)
    except Exception as e:  # 清理失败不影响主流程
        logger.warning("[prune] skipped due to error: %s", e)


def delete_user_audit(operator_id: str, operator_name: str, target_username: str, request_id: str = "") -> str:
    """删除用户时的专用审计入口（保留用户名，即使文件已删也可查）。"""
    return record_admin_op(
        "user_delete",
        operator_id,
        operator_name,
        target_username,
        detail=f"删除用户 {target_username}",
        request_id=request_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# 用户使用统计（登录 / HR 分析 按天聚合，可查询 日/月/年）
# ═══════════════════════════════════════════════════════════════════════

def _usage_path(user_id: str) -> str:
    return os.path.join(USER_USAGE_DIR, f"{user_id}.json")


def record_user_usage(user_id: str, action: str) -> None:
    """
    记录一次用户使用行为。action ∈ {"login", "analysis"}。
    存储格式：data/usage/<user_id>.json = {"2026-09-04": {"login": n, "analysis": m}, ...}
    """
    if not user_id:
        return
    action = (action or "").strip()
    if action not in ("login", "analysis"):
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _UserLock():
        data = _read_json(_usage_path(user_id)) or {}
        entry = data.get(day) or {}
        entry[action] = int(entry.get(action, 0)) + 1
        data[day] = entry
        _write_json(_usage_path(user_id), data)


def get_user_usage(user_id: str, granularity: str = "day", buckets: int = 30) -> dict:
    """
    用户使用次数聚合。granularity ∈ {"day", "month", "year"}。
    返回 {
        "granularity": ..., "labels": [str, ...],   # labels 一律字符串（前端展示安全）
        "series": {"login": [...], "analysis": [...], "total": [...]},
        "summary": {total, login_total, analysis_total, avg_per_bucket,
                    peak_label, peak_total, active_buckets, first_usage, last_usage},
    }
    total = login + analysis（展示口径）。
    """
    if granularity not in ("day", "month", "year"):
        granularity = "day"
    buckets = max(1, min(int(buckets or 30), 3660))
    maybe_prune()  # 保留策略：懒清理过期使用统计
    data = _read_json(_usage_path(user_id)) or {}

    now = datetime.now(timezone.utc)
    if granularity == "year":
        labels = [str(now.year - i) for i in range(buckets - 1, -1, -1)]
        agg: dict = {y: {"login": 0, "analysis": 0} for y in labels}
        for day_str, entry in data.items():
            try:
                y = str(int(day_str[:4]))
            except (TypeError, ValueError):
                continue
            if y in agg:
                agg[y]["login"] += int(entry.get("login", 0))
                agg[y]["analysis"] += int(entry.get("analysis", 0))
    elif granularity == "month":
        labels = []
        y, m = now.year, now.month
        for i in range(buckets - 1, -1, -1):
            total = y * 12 + (m - 1) - i
            yy, mm = divmod(total, 12)
            labels.append(f"{yy}-{mm + 1:02d}")
        agg = {lab: {"login": 0, "analysis": 0} for lab in labels}
        for day_str, entry in data.items():
            if len(day_str) >= 7:
                lab = day_str[:7]
                if lab in agg:
                    agg[lab]["login"] += int(entry.get("login", 0))
                    agg[lab]["analysis"] += int(entry.get("analysis", 0))
    else:  # day
        labels = []
        for i in range(buckets - 1, -1, -1):
            d = now - timedelta(days=i)
            labels.append(d.strftime("%Y-%m-%d"))
        agg = {lab: {"login": 0, "analysis": 0} for lab in labels}
        for day_str, entry in data.items():
            if day_str in agg:
                agg[day_str]["login"] += int(entry.get("login", 0))
                agg[day_str]["analysis"] += int(entry.get("analysis", 0))

    login_series = [agg[lab]["login"] for lab in labels]
    analysis_series = [agg[lab]["analysis"] for lab in labels]
    total_series = [l + a for l, a in zip(login_series, analysis_series)]
    series = {
        "login": login_series,
        "analysis": analysis_series,
        "total": total_series,
    }

    # 汇总统计：区间合计 / 分项 / 均值 / 峰值 / 活跃期数 / 首次·最近使用
    login_total = sum(login_series)
    analysis_total = sum(analysis_series)
    total_all = login_total + analysis_total
    peak_idx = max(range(len(total_series)), key=lambda i: total_series[i], default=-1)
    sorted_days = sorted(data.keys())
    summary = {
        "total": total_all,
        "login_total": login_total,
        "analysis_total": analysis_total,
        "avg_per_bucket": round(total_all / buckets, 2) if buckets else 0,
        "peak_label": labels[peak_idx] if peak_idx >= 0 else "",
        "peak_total": total_series[peak_idx] if peak_idx >= 0 else 0,
        "active_buckets": sum(1 for v in total_series if v > 0),
        "first_usage": sorted_days[0] if sorted_days else "",
        "last_usage": sorted_days[-1] if sorted_days else "",
    }
    return {"granularity": granularity, "labels": labels, "series": series, "summary": summary}


def usage_ranking(limit: int = 20) -> list[dict]:
    """
    全用户使用排行：按最近使用总量（登录+分析）降序，供管理员发现高消耗账号。
    返回 [{username, user_id, total, login, analysis, last_usage}]（只含已统计到的用户）。
    """
    limit = max(1, min(int(limit or 20), 200))
    maybe_prune()  # 保留策略：懒清理过期使用统计
    if not os.path.isdir(USER_USAGE_DIR):
        return []
    rows: list[dict] = []
    for filename in os.listdir(USER_USAGE_DIR):
        if not filename.endswith(".json"):
            continue
        user_id = filename[:-5]
        data = _read_json(os.path.join(USER_USAGE_DIR, filename)) or {}
        if not data:
            continue
        login_total = 0
        analysis_total = 0
        for _day, entry in data.items():
            login_total += int(entry.get("login", 0) or 0)
            analysis_total += int(entry.get("analysis", 0) or 0)
        rows.append(
            {
                "user_id": user_id,
                "username": _username_by_id(user_id),
                "login": login_total,
                "analysis": analysis_total,
                "total": login_total + analysis_total,
                "last_usage": sorted(data.keys())[-1],
            }
        )
    rows.sort(key=lambda r: (r["total"], r["last_usage"] or ""), reverse=True)
    return rows[:limit]


def _username_by_id(user_id: str) -> str:
    """通过用户 ID 反查用户名（尽力而为，找不到返回空串）。"""
    if not user_id:
        return ""
    for filename in os.listdir(USERS_DIR):
        if not filename.endswith(".json") or filename.startswith("_"):
            continue
        try:
            rec = _read_json(os.path.join(USERS_DIR, filename))
        except OSError:
            continue
        if rec and rec.get("user_id") == user_id:
            return rec.get("username") or ""
    return ""


def delete_user_usage(user_id: str) -> None:
    """删除用户时一并清理其使用统计数据。"""
    if not user_id:
        return
    with _UserLock():
        path = _usage_path(user_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Failed to remove usage file %s", path)


# ═══════════════════════════════════════════════════════════════════════
# 登录防爆破：冷却 → 冻结 → 管理员/邮箱自助解冻 + 每 IP 限流
# ═══════════════════════════════════════════════════════════════════════

def _login_fail_hash(username_lower: str) -> str:
    """登录失败记录文件名：sha256(username_lower)（磁盘不存明文用户名标识）。"""
    return hashlib.sha256(username_lower.encode("utf-8")).hexdigest()


def _login_fail_path(username_lower: str) -> str:
    return os.path.join(LOGIN_FAILURES_DIR, f"{_login_fail_hash(username_lower)}.json")


def _login_fail_record(username_lower: str) -> dict:
    rec = _read_json(_login_fail_path(username_lower)) or {}
    rec.setdefault("username", username_lower)
    rec.setdefault("consecutive_failures", 0)
    rec.setdefault("last_failure_at", 0)
    rec.setdefault("failures", [])
    rec.setdefault("frozen", False)
    rec.setdefault("frozen_at", None)
    rec.setdefault("frozen_by", "auto")      # auto=系统自动冻结 / admin=管理员手动冻结
    rec.setdefault("frozen_reason", "")       # 管理员冻结时可填原因
    # 惰性剪枝：剔除窗口外失败条目
    now = _now_ts()
    window = config.LOGIN_FREEZE_WINDOW_MINUTES * 60
    kept = [f for f in rec["failures"] if now - float(f.get("ts", 0)) < window]
    if len(kept) != len(rec["failures"]):
        rec["failures"] = kept
    return rec


def _save_login_fail_record(username_lower: str, rec: dict) -> None:
    _write_json(_login_fail_path(username_lower), rec)


def login_policy_check(username: str) -> tuple[str, float, str]:
    """
    登录前策略检查（不校验密码）。返回 (action, retry_after_seconds, frozen?)
    action ∈ {ok, cooldown, frozen}。
    """
    username_lower = str(username or "").lower()
    rec = _login_fail_record(username_lower)
    now = _now_ts()

    # 自动解冻（配置 >0 小时）：到期自动解除（仅系统自动冻结的账号；管理员冻结须人工解冻）
    if (
        rec.get("frozen")
        and rec.get("frozen_by") != "admin"
        and config.LOGIN_FREEZE_AUTO_UNFREEZE_HOURS > 0
        and rec.get("frozen_at")
        and now - float(rec.get("frozen_at", 0))
        > config.LOGIN_FREEZE_AUTO_UNFREEZE_HOURS * 3600
    ):
        rec["frozen"] = False
        rec["frozen_at"] = None
        rec["frozen_by"] = "auto"
        rec["frozen_reason"] = ""
        rec["consecutive_failures"] = 0
        rec["failures"] = []
        _save_login_fail_record(username_lower, rec)

    if rec.get("frozen"):
        return "frozen", 0.0, "frozen"

    # L1 冷却：连续失败 ≥ 阈值且距上次失败 < 冷却时长
    consecutive = int(rec.get("consecutive_failures", 0))
    last_failure = float(rec.get("last_failure_at", 0) or 0)
    lock_seconds = config.LOGIN_LOCK_MINUTES * 60
    if (
        consecutive >= config.LOGIN_FAIL_LIMIT
        and now - last_failure < lock_seconds
    ):
        retry = max(1.0, lock_seconds - (now - last_failure))
        return "cooldown", retry, ""
    return "ok", 0.0, ""


def record_login_failure(username: str, ip: str = "") -> tuple[bool, bool]:
    """
    记录一次登录失败。返回 (frozen_now, reached_cooldown_after_this_failure)。
    调用方（路由）按返回值决定响应码（423 或 401/429）。
    """
    username_lower = str(username or "").lower()
    now = _now_ts()
    with _UserLock():
        rec = _login_fail_record(username_lower)
        if rec.get("frozen"):
            return True, False  # 已冻结，不再计数
        rec["consecutive_failures"] = int(rec.get("consecutive_failures", 0)) + 1
        rec["last_failure_at"] = now
        failures = [f for f in rec.get("failures", []) if now - float(f.get("ts", 0)) < config.LOGIN_FREEZE_WINDOW_MINUTES * 60]
        failures.append({"ts": now, "ip_hash": _ip_hash(ip)})
        # 窗口条目上限（防文件膨胀）
        rec["failures"] = failures[-config.LOGIN_FREEZE_THRESHOLD * 2:]
        frozen = len(rec["failures"]) >= config.LOGIN_FREEZE_THRESHOLD
        if frozen:
            rec["frozen"] = True
            rec["frozen_at"] = now
            logger.warning(
                "[auth] account frozen username=%s window_failures=%d ip=%s",
                username_lower,
                len(rec["failures"]),
                _ip_hash(ip),
            )
        _save_login_fail_record(username_lower, rec)
        return frozen, not frozen


def clear_login_failures(username: str) -> None:
    """登录成功 / 自助解冻成功后清零失败记录。"""
    username_lower = str(username or "").lower()
    _safe_remove(_login_fail_path(username_lower))


def freeze_user(
    username: str,
    operator: str = "",
    reason: str = "",
) -> tuple[bool, Optional[str]]:
    """
    管理员手动冻结用户（防异常消耗 token 等场景）。
    与自动冻结（登录失败触发）以 frozen_by=admin / auto 区分：
    - 冻结后拒绝登录（423）与拒绝重置密码；
    - admin 冻结的账号禁止自助解冻（只能管理员解冻）。
    返回 (success, error)。
    """
    username_lower = str(username or "").lower()
    if not find_user_by_username(username_lower):
        return False, "用户不存在"
    now = _now_ts()
    with _UserLock():
        rec = _login_fail_record(username_lower)
        rec["frozen"] = True
        rec["frozen_at"] = now
        rec["frozen_by"] = "admin"
        rec["frozen_reason"] = str(reason or "").strip()
        # 保留失败计数供管理员查看，不清空
        _save_login_fail_record(username_lower, rec)
    logger.warning(
        "[auth] account frozen by admin username=%s operator=%s reason=%s",
        username_lower,
        operator or "-",
        rec["frozen_reason"] or "-",
    )
    return True, None


def get_frozen_info(username: str) -> dict:
    """冻结信息（未冻结也返回结构，frozen=False）。供登录 423 响应/管理员视图使用。"""
    username_lower = str(username or "").lower()
    rec = _login_fail_record(username_lower)
    return {
        "frozen": bool(rec.get("frozen")),
        "frozen_by": rec.get("frozen_by") or "auto",
        "frozen_reason": rec.get("frozen_reason") or "",
        "frozen_at": _epoch_to_iso(rec.get("frozen_at")),  # 输出 ISO（内部仍为 epoch 秒）
        "consecutive_failures": int(rec.get("consecutive_failures", 0)),
        "window_failures": len(rec.get("failures") or []),
    }


def _epoch_to_iso(value):
    """兼容：frozen_at 内部为 epoch 秒（float），对外统一输出 ISO 字符串（修复前端 1970 显示）。"""
    if value is None or value == "":
        return None
    try:
        as_float = float(value)
        if as_float > 10**12:  # 毫秒级时间戳兜底
            return datetime.fromtimestamp(as_float / 1000, timezone.utc).isoformat()
        return datetime.fromtimestamp(as_float, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def list_frozen_users() -> list[dict]:
    """列出所有冻结账号（管理员视图）。"""
    if not os.path.isdir(LOGIN_FAILURES_DIR):
        return []
    out = []
    for filename in os.listdir(LOGIN_FAILURES_DIR):
        if not filename.endswith(".json"):
            continue
        rec = _read_json(os.path.join(LOGIN_FAILURES_DIR, filename))
        if rec and rec.get("frozen"):
            out.append(
                {
                    "username": rec.get("username") or "",
                    "frozen_at": _epoch_to_iso(rec.get("frozen_at")),  # 输出 ISO
                    "frozen_by": rec.get("frozen_by") or "auto",
                    "frozen_reason": rec.get("frozen_reason") or "",
                    "consecutive_failures": int(rec.get("consecutive_failures", 0)),
                    "window_failures": len(rec.get("failures") or []),
                }
            )
    out.sort(key=lambda r: r.get("frozen_at") or "", reverse=True)
    return out


def unfreeze_user(username: str, operator: str = "") -> bool:
    """解冻（管理员或自助解冻共用）：清 frozen + 清失败记录。"""
    username_lower = str(username or "").lower()
    with _UserLock():
        rec = _login_fail_record(username_lower)
        if not rec.get("frozen"):
            # 无失败记录文件也视为已解冻（幂等）
            _safe_remove(_login_fail_path(username_lower))
            return True
        rec["frozen"] = False
        rec["frozen_at"] = None
        rec["frozen_by"] = "auto"
        rec["frozen_reason"] = ""
        rec["consecutive_failures"] = 0
        rec["failures"] = []
        _save_login_fail_record(username_lower, rec)
    logger.info("[auth] account unfrozen username=%s operator=%s", username_lower, operator or "self")
    return True


def unfreeze_by_email(username: str, email: str, code: str) -> tuple[bool, Optional[str]]:
    """
    冻结账号自助解冻：正确密码已在路由层校验通过（email 为账号绑定邮箱）。
    本函数负责：账号确实冻结 + 该邮箱确实属于该账号 + 邮箱验证码消费校验。
    返回 (ok, error_message)。
    """
    user = find_user_by_username(username)
    if not user:
        return False, "账号不存在。"
    user_email = str(user.get("email") or "").strip().lower()
    email_clean = str(email or "").strip().lower()
    if not user_email or user_email != email_clean:
        return False, "该邮箱与本账号不匹配，无法自助解冻，请联系管理员。"
    # 校验该账号确实冻结（未冻结则无意义）
    username_lower = str(username or "").lower()
    rec = _read_json(_login_fail_path(username_lower)) or {}
    if not rec.get("frozen"):
        return False, "该账号未被冻结。"
    # 管理员手动冻结：禁止自助解冻，只能联系管理员
    if rec.get("frozen_by") == "admin":
        return False, "该账号已被系统管理员冻结，请联系管理员解冻。"
    # 邮箱验证码消费校验
    if not verify_email_code_once(email_clean, PURPOSE_UNFREEZE, code):
        return False, "验证码无效或已过期，请重新发送。"
    unfreeze_user(username, operator="self:email")
    return True, None


def request_unfreeze_code(username: str, email: str) -> tuple[bool, Optional[str], int]:
    """
    自助解冻第一步：为冻结账号的绑定邮箱发送邮箱验证码。
    - 该账号必须处于冻结状态
    - email 必须与该账号绑定邮箱一致
    返回 (sent_ok, error_message, status)。
    """
    user = find_user_by_username(username)
    if not user:
        return False, "账号不存在。", 404
    user_email = str(user.get("email") or "").strip().lower()
    email_clean = str(email or "").strip().lower()
    if not user_email or user_email != email_clean:
        return False, "该邮箱与本账号不匹配，无法自助解冻，请联系管理员。", 422
    username_lower = str(username or "").lower()
    rec = _read_json(_login_fail_path(username_lower)) or {}
    if not rec.get("frozen"):
        return False, "该账号未被冻结。", 409
    # 管理员手动冻结：禁止自助解冻（不发验证码）
    if rec.get("frozen_by") == "admin":
        return False, "该账号已被系统管理员冻结，请联系管理员解冻。", 403
    return True, None, 200


def is_user_frozen(username: str) -> bool:
    """该账号当前是否处于冻结状态（仅查询，不触发自动解冻逻辑）。"""
    username_lower = str(username or "").lower()
    rec = _read_json(_login_fail_path(username_lower)) or {}
    return bool(rec.get("frozen"))


def ip_login_rate_exceeded(ip: str = "") -> tuple[bool, float]:
    """
    每 IP 登录失败限流·只读检查（不计数）。返回 (受限?, 剩余秒)。
    计数只在真实失败发生时由 record_ip_login_failure 落账 ——
    这样成功登录 / 冷却拦截不会消耗 IP 预算（设计 v2 §2.3）。
    """
    ip_hash = _ip_hash(ip)
    return _rate_exceeded(
        f"login_ip:{ip_hash}",
        max_count=config.LOGIN_MAX_FAIL_PER_IP_10MIN,
        window=config.LOGIN_FREEZE_WINDOW_MINUTES * 60,
    )


def record_ip_login_failure(ip: str = "") -> None:
    """记录一次该 IP 的登录失败（仅实际失败时调用；10 分钟窗口滚动计数）。"""
    ip_hash = _ip_hash(ip)
    _rate_limited(
        f"login_ip:{ip_hash}",
        max_count=1 << 30,  # 只计数不拦停；拦截判断交给 ip_login_rate_exceeded
        window=config.LOGIN_FREEZE_WINDOW_MINUTES * 60,
    )


def login_failure_rate_limited(ip: str = "") -> tuple[bool, float]:
    """[deprecated] 旧名称兼容：等同 ip_login_rate_exceeded（仅检查、不计数）。"""
    return ip_login_rate_exceeded(ip)


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
PURPOSE_UNFREEZE = "unfreeze"              # 自助解冻：冻结账号邮箱验证


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
    username: str,
    password: str,
    email: str,
    code: str,
    invite_code: str = "",
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """
    带邮箱验证码 + 邀请码注册。返回 (user_record, None, None) 成功。
    失败返回 (None, error_message, field)，field 用于前端定位：'email_code'/'username'/'password'/'invite_code'。

    校验顺序（关键设计）：
    ① 非消费校验邀请码（无效/过期/已用/与邮箱不匹配 → 失败，不扣码）；
    ② 消费式校验邮箱验证码（现状逻辑，失败不扣邀请码）；
    ③ 文件锁内原子完成：复检邀请码 → 建号 → 消费邀请码（回填 + 归档 + 删除）。
    只有建号成功才消费邀请码 —— 任何中间失败用户都不会损失码。
    """
    email_clean = str(email or "").strip().lower()
    # ① 邀请码（强绑定申请邮箱；大小写不敏感）
    if not invite_code:
        return None, "请填写邀请码。", "invite_code"
    check = validate_invite_code(invite_code, email=email_clean)
    if check is not None:
        return None, "邀请码无效或不可用，请联系管理员获取新的邀请码。", "invite_code"

    # ② 邮箱验证码（一次性消费；失败不扣邀请码）
    if not verify_email_code_once(email_clean, PURPOSE_EMAIL_VERIFY, code):
        return None, "邮箱验证码无效或已过期，请重新发送。", "email_code"

    # ③ 锁内：复检 → 建号 → 消费邀请码（原子）
    with _UserLock():
        recheck = validate_invite_code(invite_code, email=email_clean)
        if recheck is not None:
            return None, "邀请码无效或不可用，请联系管理员获取新的邀请码。", "invite_code"
        user, err = create_user(username, password, email_clean)
        if err:
            # 建号失败（用户名冲突等）：不消费邀请码，用户可换名重试
            return None, err, "username"
        consume_invite_code(invite_code, user.get("user_id", ""), user.get("username", ""))
    return user, None, None


# ═══════════════════════════════════════════════════════════════════════
# 邀请码（注册邀请）—— 申请 → 审批 → 发码邮件 → 注册前置校验
# ═══════════════════════════════════════════════════════════════════════

# 安全字母表（排除易混淆 0O1lI）；邀请码不区分大小写，落盘一律大写
_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def normalize_invite_code(code: str) -> str:
    """规范化邀请码：去空白/连字符/下划线，统一大写（大小写不敏感）。"""
    return re.sub(r"[\s\-_]", "", str(code or "")).upper()


def _invite_code_hash(code: str) -> str:
    """规范化后的邀请码 → sha256 十六进制（磁盘零明文，文件名即哈希）。"""
    return hashlib.sha256(normalize_invite_code(code).encode("ascii")).hexdigest()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def generate_invite_code(
    bound_email: str,
    request_id: str,
    created_by: str,
    note: str = "",
    ttl_hours: int | None = None,
) -> str:
    """生成一次性邀请码（明文返回用于发邮件；落盘只有哈希）。"""
    length = config.INVITE_CODE_LENGTH
    code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))
    code_hash = _invite_code_hash(code)
    now = _now_ts()
    ttl_h = ttl_hours or config.INVITE_CODE_TTL_HOURS
    record = {
        "code_hash": code_hash,
        "bound_email": str(bound_email or "").strip().lower(),
        "request_id": request_id,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": now + ttl_h * 3600,
        "used": False,
        "used_by_user_id": None,
        "used_by_username": None,
        "used_at": None,
        "note": note,
    }
    _write_json(os.path.join(INVITE_CODES_DIR, f"{code_hash}.json"), record)
    return code


def _invite_code_record(code: str) -> Optional[dict]:
    """按明文码读取记录（内部函数：路由层不可用明文探测结果做文案区分）。"""
    path = os.path.join(INVITE_CODES_DIR, f"{_invite_code_hash(code)}.json")
    return _read_json(path)


def validate_invite_code(code: str, email: str = "") -> Optional[str]:
    """
    非消费校验邀请码。返回 None=有效；否则返回统一错误信息（不区分原因，防枚举）。
    email 非空时强校验绑定邮箱：码有效但邮箱不匹配 → 同样返回统一文案
    （不向调用方暴露"码存在"，统一由路由层映射为"邀请码无效或不可用"）。
    注：绑定邮箱不匹配的信息单独返回给用户是合理的——码持有者即申请邮箱主人。
    这里为简化安全面，把不匹配也归入无效文案；如需更友好提示可单独返回。
    """
    if not code:
        return "empty"
    rec = _invite_code_record(code)
    if rec is None:
        return "invalid"
    if rec.get("used"):
        return "used"
    if float(rec.get("expires_at", 0)) < _now_ts():
        return "expired"
    if email:
        email_clean = str(email or "").strip().lower()
        bound = str(rec.get("bound_email") or "").lower()
        if bound and bound != email_clean:
            return "email_mismatch"
    return None


def consume_invite_code(code: str, user_id: str, username: str) -> bool:
    """消费邀请码（调用方须已持锁）：回填使用者 → 归档到 audit → 删除活跃文件。"""
    rec = _invite_code_record(code)
    if rec is None:
        return False
    rec["used"] = True
    rec["used_by_user_id"] = user_id
    rec["used_by_username"] = username
    rec["used_at"] = datetime.now(timezone.utc).isoformat()
    code_hash = _invite_code_hash(code)
    # 归档留存（审计链），再删活跃文件 —— 一次性语义
    _write_json(os.path.join(INVITE_CODES_AUDIT_DIR, f"{code_hash}.json"), rec)
    _safe_remove(os.path.join(INVITE_CODES_DIR, f"{code_hash}.json"))
    return True


def create_invite_request(
    email: str, note: str, ip: str = ""
) -> tuple[Optional[dict], Optional[str], int]:
    """
    提交邀请码申请。返回 (request, None, 200) 成功；
    失败返回 (None, error_message, status)。
    - note 必填（2–200 字符，可配置）
    - 同邮箱最多 1 条 pending
    - 同邮箱被拒次数 ≥ 上限 → 拒收（429）
    - 同邮箱 60s 冷却、每 IP 每日上限（见 _rate_limited）
    """
    email_clean = str(email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        return None, "邮箱格式不正确。", 422
    if config.INVITE_REQUEST_NOTE_REQUIRED:
        note_clean = str(note or "").strip()
        if len(note_clean) < 2 or len(note_clean) > 200:
            return None, "请填写申请理由（2–200 字符）。", 422
    else:
        note_clean = str(note or "").strip()[:200]

    # 被拒次数上限（拒收）
    if count_rejected_requests(email_clean) >= config.INVITE_REQUEST_MAX_REJECTIONS:
        return None, (
            "该邮箱的申请已多次未通过，暂不再接收新申请，"
            "如有需要请联系管理员。"
        ), 429

    ip_hash = _ip_hash(ip)
    with _UserLock():
        # 同邮箱冷却（60s）
        limited_email, _retry = _rate_limited(
            f"invite_req_email:{email_clean}", max_count=1, window=60
        )
        if limited_email:
            return None, "申请过于频繁，请稍后再试。", 429
        # 每 IP 每日上限
        limited_ip, _retry = _rate_limited(
            f"invite_req_ip:{ip_hash}",
            max_count=config.INVITE_REQUEST_MAX_PER_IP_DAY,
            window=86400,
        )
        if limited_ip:
            return None, "今日申请次数已达上限，请明天再试或联系管理员。", 429
        # 同邮箱 pending 唯一
        existing = _find_invite_request_by_email_status(email_clean, "pending")
        if existing:
            return None, "该邮箱已有申请在审批中，请耐心等待。", 409
        request_id = str(uuid.uuid4())
        record = {
            "request_id": request_id,
            "email": email_clean,
            "note": note_clean,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ip_hash": ip_hash,
            "reviewed_by": None,
            "reviewed_at": None,
            "code_hash": None,
            "code_sent": False,
            "sent_at": None,
            "reject_reason": None,
        }
        _write_json(os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json"), record)
        return record, None, 200


def _find_invite_request_by_email_status(email: str, status: str) -> Optional[dict]:
    """按邮箱 + 状态查找申请单（pending 唯一性检查用）。"""
    email_clean = str(email or "").strip().lower()
    if not os.path.isdir(INVITE_REQUESTS_DIR):
        return None
    for filename in os.listdir(INVITE_REQUESTS_DIR):
        if not filename.endswith(".json"):
            continue
        rec = _read_json(os.path.join(INVITE_REQUESTS_DIR, filename))
        if not rec:
            continue
        if (rec.get("email") or "").lower() == email_clean and rec.get("status") == status:
            return rec
    return None


def get_invite_request(request_id: str) -> Optional[dict]:
    path = os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json")
    return _read_json(path)


def list_invite_requests(status: str = "pending") -> list[dict]:
    """列出指定状态申请单（新→旧）。脱敏由路由层做。"""
    if not os.path.isdir(INVITE_REQUESTS_DIR):
        return []
    out = []
    for filename in os.listdir(INVITE_REQUESTS_DIR):
        if not filename.endswith(".json"):
            continue
        rec = _read_json(os.path.join(INVITE_REQUESTS_DIR, filename))
        if rec and (not status or rec.get("status") == status):
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def count_rejected_requests(email: str) -> int:
    """统计该邮箱历史被拒申请数（驱动拒收黑名单）。"""
    email_clean = str(email or "").strip().lower()
    if not os.path.isdir(INVITE_REQUESTS_DIR):
        return 0
    count = 0
    for filename in os.listdir(INVITE_REQUESTS_DIR):
        if not filename.endswith(".json"):
            continue
        rec = _read_json(os.path.join(INVITE_REQUESTS_DIR, filename))
        if rec and (rec.get("email") or "").lower() == email_clean and rec.get("status") == "rejected":
            count += 1
    return count


def approve_invite_request(request_id: str, admin_user_id: str) -> tuple[Optional[str], Optional[str], int]:
    """
    审批通过：锁内复检 pending → 生成码（绑定申请邮箱）→ 回填申请单。
    返回 (request, None, 200)；SMTP 发送由路由层完成（失败走补发）。
    返回码明文仅此一次（发邮件/一次性回显用）。
    """
    with _UserLock():
        req = get_invite_request(request_id)
        if not req:
            return None, "申请单不存在。", 404
        if req.get("status") != "pending":
            return None, "该申请已被处理，请刷新列表。", 409
        email = (req.get("email") or "").strip().lower()
        note = str(req.get("note") or "")
        code = generate_invite_code(
            bound_email=email,
            request_id=request_id,
            created_by=admin_user_id,
            note=note,
        )
        code_hash = _invite_code_hash(code)
        req["status"] = "approved"
        req["reviewed_by"] = admin_user_id
        req["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        req["code_hash"] = code_hash
        _write_json(os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json"), req)
        return req, code, 200


def reject_invite_request(
    request_id: str, admin_user_id: str, reason: str = ""
) -> tuple[Optional[dict], Optional[str], int]:
    """拒绝申请：记录原因；reason 非空 → 路由层发拒信邮件。"""
    with _UserLock():
        req = get_invite_request(request_id)
        if not req:
            return None, "申请单不存在。", 404
        if req.get("status") != "pending":
            return None, "该申请已被处理，请刷新列表。", 409
        req["status"] = "rejected"
        req["reviewed_by"] = admin_user_id
        req["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        req["reject_reason"] = str(reason or "").strip()[:500]
        _write_json(os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json"), req)
        return req, None, 200


def mark_invite_request_sent(request_id: str) -> None:
    """标记发码邮件已发送（审批/补发成功后调用）。"""
    req = get_invite_request(request_id)
    if not req:
        return
    req["code_sent"] = True
    req["sent_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json"), req)


def resend_invite_request(request_id: str) -> tuple[Optional[str], Optional[str], int]:
    """补发：返回已批准申请单绑定的邀请码明文（重新读取码文件仍可用则重发）。"""
    req = get_invite_request(request_id)
    if not req:
        return None, "申请单不存在。", 404
    if req.get("status") != "approved" or not req.get("code_hash"):
        return None, "该申请未通过审批，无法补发邀请码。", 409
    code_hash = req.get("code_hash")
    audit_path = os.path.join(INVITE_CODES_AUDIT_DIR, f"{code_hash}.json")
    active_path = os.path.join(INVITE_CODES_DIR, f"{code_hash}.json")
    rec = _read_json(active_path) or _read_json(audit_path)
    if not rec or rec.get("used"):
        return None, "该邀请码已被使用或不存在，请重新审批发放。", 409
    # 明文已不可得（只存哈希）→ 补发需要明文，见下：
    # 方案：approve 时明文只出现一次；补发场景需要重新生成码。
    # 因此这里走"重新生成 + 覆盖"：原码作废，生成新码并更新申请单。
    return _regenerate_code_for_request(req, "resend")


def _regenerate_code_for_request(
    req: dict, action: str
) -> tuple[Optional[str], Optional[str], int]:
    """重新生成绑定码（补发场景：原码明文丢失，无法重发同一码）。"""
    request_id = req.get("request_id") or ""
    email = (req.get("email") or "").strip().lower()
    with _UserLock():
        req2 = get_invite_request(request_id)
        if not req2:
            return None, "申请单不存在。", 404
        # 原码归档（防复活）
        old_hash = req2.get("code_hash")
        if old_hash:
            old_active = os.path.join(INVITE_CODES_DIR, f"{old_hash}.json")
            old_rec = _read_json(old_active)
            if old_rec and not old_rec.get("used"):
                _safe_remove(old_active)  # 旧码作废
        code = generate_invite_code(
            bound_email=email,
            request_id=request_id,
            created_by=str(req2.get("reviewed_by") or ""),
            note=str(req2.get("note") or ""),
        )
        req2["code_hash"] = _invite_code_hash(code)
        req2["code_sent"] = False
        _write_json(os.path.join(INVITE_REQUESTS_DIR, f"{request_id}.json"), req2)
        return req2, code, 200


def list_invite_codes(only_used: bool | None = None) -> list[dict]:
    """邀请码总览（路由层脱敏：不回明文）。含活跃 + 已消费（audit 归档）。"""
    out = []
    for directory in (INVITE_CODES_DIR, INVITE_CODES_AUDIT_DIR):
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith(".json"):
                continue
            rec = _read_json(os.path.join(directory, filename))
            if rec:
                out.append(rec)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def revoke_invite_code(code: str) -> bool:
    """作废未使用的邀请码（删除活跃文件；audit 归档保留审计）。

    入参可为明文邀请码或完整 64 位哈希（管理端列表返回完整哈希，前端作废时
    直接回传，避免截断哈希再次哈希导致定位失败）。
    """
    raw = (code or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        code_hash = raw.lower()
    else:
        code_hash = _invite_code_hash(raw)
    active_path = os.path.join(INVITE_CODES_DIR, f"{code_hash}.json")
    rec = _read_json(active_path)
    if rec is None or rec.get("used"):
        return False
    # 归档并标记作废
    rec["revoked"] = True
    rec["revoked_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(os.path.join(INVITE_CODES_AUDIT_DIR, f"{code_hash}.json"), rec)
    _safe_remove(active_path)
    return True


def is_admin_email(email: str) -> bool:
    """用户 email 是否在管理员白名单（大小写不敏感）。"""
    return str(email or "").strip().lower() in ADMIN_EMAILS


# ═══════════════════════════════════════════════════════════════════════
# IP 限流（文件计数，跨 gunicorn worker 有效）—— 邀请码校验 / 登录失败等
# ═══════════════════════════════════════════════════════════════════════

def _ip_hash(ip: str) -> str:
    if not ip:
        return "unknown"
    # 加盐哈希避免 IP 明文落盘；盐固定于模块（非机密，仅为防明文）
    return hashlib.sha256(f"rl:{ip}".encode("utf-8")).hexdigest()[:24]


def _rate_file_path(key: str) -> str:
    safe_key = re.sub(r"[^0-9A-Za-z_@.\-]", "_", key)
    return os.path.join(RATE_LIMITS_DIR, f"{safe_key}.json")


def _rate_exceeded(
    key: str, max_count: int, window: float, now: float | None = None
) -> tuple[bool, float]:
    """
    只读频率检查：当前窗口计数是否已超上限。返回 (受限?, 剩余秒数)。
    不写计数文件 —— 与 _rate_limited（读+写）区分，供"失败才落账"的
    登录 IP 限流使用（成功请求不应消耗 IP 预算）。
    """
    now = _now_ts() if now is None else now
    path = _rate_file_path(key)
    with _UserLock():
        rec = _read_json(path) or {}
        start = float(rec.get("start", now))
        count = int(rec.get("count", 0))
        if now - start >= window:
            return False, 0.0
        limited = count >= max_count
        retry = max(0.0, window - (now - start)) if limited else 0.0
    return limited, retry


def _rate_limited(
    key: str, max_count: int, window: float, now: float | None = None
) -> tuple[bool, float]:
    """
    固定窗口文件计数限流。返回 (受限?, 剩余秒数)。
    key 形如 login_ip:<ip_hash> / invite_check_ip:<ip_hash>。
    """
    now = _now_ts() if now is None else now
    path = _rate_file_path(key)
    with _UserLock():
        rec = _read_json(path) or {}
        start = float(rec.get("start", now))
        count = int(rec.get("count", 0))
        if now - start >= window:
            start = now
            count = 0
        count += 1
        _write_json(path, {"start": start, "count": count})
        limited = count > max_count
        retry = max(0.0, window - (now - start)) if limited else 0.0
    return limited, retry


def _prune_rate_files(max_age_seconds: float = 86400) -> None:
    """惰性清理过期限流文件（低频调用即可）。"""
    if not os.path.isdir(RATE_LIMITS_DIR):
        return
    now = _now_ts()
    try:
        for filename in os.listdir(RATE_LIMITS_DIR):
            path = os.path.join(RATE_LIMITS_DIR, filename)
            try:
                rec = _read_json(path)
                start = float((rec or {}).get("start", 0))
                if now - start > max_age_seconds:
                    os.remove(path)
            except OSError:
                continue
    except OSError:
        pass


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

    user = _read_json(os.path.join(USERS_DIR, f"{payload['user_id']}.json"))
    g.auth_user = {
        "user_id": payload["user_id"],
        "username": payload["username"],
        "email": (user or {}).get("email") or "",
        "is_admin": is_admin(user),
    }

    # 管理员接口：认证通过后做管理员鉴权（白名单邮箱 OR is_admin 标记）
    if path.startswith(ADMIN_PATH_PREFIX):
        if not g.auth_user.get("is_admin"):
            logger.warning(
                "[auth] admin access denied path=%s user=%s email=%s",
                path,
                g.auth_user.get("username"),
                (g.auth_user.get("email") or "") or "-",
            )
            raise _AuthError("无权限执行该操作。", 403)


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