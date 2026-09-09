"""
极简配置。用 os.getenv + python-dotenv，不要 pydantic-settings。
所有默认值都保证：没有 .env 也能跑（零配置）。
"""
import os

from dotenv import load_dotenv

# .env is stored beside this module. Resolve it explicitly so running from the
# repository root does not silently ignore apps/backend/.env.


def _strip_quotes(value):
    """
    去掉环境变量值两端的引号。
    python-dotenv 加载 .env 文件时会自动去引号，但 docker-compose 的 env_file
    是直接注入环境变量、不经 dotenv 处理，会保留引号（如 ENV="production" 实际值
    是带引号的字符串）。这里统一兼容两种来源。
    """
    if value is None:
        return value
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()

# 项目根目录（apps/backend/），数据/日志用绝对路径，宝塔下不会散落
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_FILE)
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = _strip_quotes(os.getenv("LOG_DIR")) or os.path.join(BASE_DIR, "logs")

# 运行环境：local / production
ENV = _strip_quotes(os.getenv("ENV", "local")).lower()

# 生产环境禁止请求体携带自定义模型配置/API Key；本地可显式开启用于开发调试。
ALLOW_CUSTOM_AI_CONFIG = _strip_quotes(
    os.getenv("ALLOW_CUSTOM_AI_CONFIG", "false")
).lower() in {"1", "true", "yes", "on"}

# LLM 配置（核心）
LLM_API_KEY = _strip_quotes(os.getenv("LLM_API_KEY", ""))
LLM_BASE_URL = _strip_quotes(os.getenv("LLM_BASE_URL", "https://api.deepseek.com"))
# 兼容 LLM_MODEL（标准命名）与 LL_MODEL（历史命名）。
# 推荐新配置用 LLM_MODEL，老 .env 用 LL_MODEL 也能跑。
LL_MODEL = (
    _strip_quotes(os.getenv("LLM_MODEL"))
    or _strip_quotes(os.getenv("LL_MODEL"))
    or "deepseek-v4-flash"
)

# JWT 签名密钥（多用户认证）。production 下不能是默认值。
JWT_SECRET_KEY = _strip_quotes(
    os.getenv("JWT_SECRET_KEY", "change-me-jwt-secret")
)

# Session 密钥（保留以备未来 Flask-Session 扩展；当前未使用）
SESSION_SECRET_KEY = _strip_quotes(os.getenv("SESSION_SECRET_KEY", "change-me"))

# 后端端口
try:
    BACKEND_PORT = int(_strip_quotes(os.getenv("BACKEND_PORT", "9001")))
except (TypeError, ValueError):
    BACKEND_PORT = 9001

# CORS 来源（逗号分隔）。同源反代部署留空即可。
_origins_raw = _strip_quotes(os.getenv("ALLOWED_ORIGINS", "")) or ""
if _origins_raw.strip():
    ALLOWED_ORIGINS = [s.strip() for s in _origins_raw.split(",") if s.strip()]
else:
    # 默认放行本地开发端口
    ALLOWED_ORIGINS = [
        f"http://localhost:{p}" for p in (3000, 3001, 3002)
    ] + [f"http://127.0.0.1:{p}" for p in (3000, 3001, 3002)]

# ── SMTP 邮件配置（忘记密码重置）───────────────────────────────────
# 未配置时重置接口返回 503，提示管理员配置邮件服务。
SMTP_HOST = _strip_quotes(os.getenv("SMTP_HOST", ""))
SMTP_PORT = 465
try:
    SMTP_PORT = int(_strip_quotes(os.getenv("SMTP_PORT", "465")))
except (TypeError, ValueError):
    SMTP_PORT = 465
SMTP_USER = _strip_quotes(os.getenv("SMTP_USER", ""))
SMTP_PASSWORD = _strip_quotes(os.getenv("SMTP_PASSWORD", ""))
SMTP_FROM = _strip_quotes(os.getenv("SMTP_FROM", "")) or SMTP_USER

def smtp_configured() -> bool:
    """是否已配置可用的 SMTP 服务。"""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

# 密码重置 Token 有效期（秒），默认 30 分钟
RESET_TOKEN_TTL_SECONDS = 1800
try:
    RESET_TOKEN_TTL_SECONDS = int(
        _strip_quotes(os.getenv("RESET_TOKEN_TTL_SECONDS", "1800"))
    )
except (TypeError, ValueError):
    RESET_TOKEN_TTL_SECONDS = 1800


# ── 账号安全：管理员白名单 ───────────────────────────────────────
# 逗号分隔的邮箱列表。JWT 用户 email 落在白名单内 → 管理员（可审批邀请码、解冻账号）。
# 默认 luchenstudio@163.com（项目联系人邮箱）。
def _email_list(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


ADMIN_EMAILS = _email_list(
    _strip_quotes(os.getenv("ADMIN_EMAILS", "luchenstudio@163.com")) or ""
)


# ── 邀请码注册机制 ───────────────────────────────────────────────
# 邀请码长度（默认 4，字母数字混合、不区分大小写）
INVITE_CODE_LENGTH = 4
try:
    INVITE_CODE_LENGTH = max(
        4, int(_strip_quotes(os.getenv("INVITE_CODE_LENGTH", "4")))
    )
except (TypeError, ValueError):
    INVITE_CODE_LENGTH = 4
# 邀请码有效期（小时），默认 24
INVITE_CODE_TTL_HOURS = 24
try:
    INVITE_CODE_TTL_HOURS = max(
        1, int(_strip_quotes(os.getenv("INVITE_CODE_TTL_HOURS", "24")))
    )
except (TypeError, ValueError):
    INVITE_CODE_TTL_HOURS = 24
# 申请理由必填开关
INVITE_REQUEST_NOTE_REQUIRED = True
# 同一邮箱被拒达到该次数后拒收新申请
INVITE_REQUEST_MAX_REJECTIONS = 3
try:
    INVITE_REQUEST_MAX_REJECTIONS = max(
        1,
        int(
            _strip_quotes(os.getenv("INVITE_REQUEST_MAX_REJECTIONS", "3"))
        ),
    )
except (TypeError, ValueError):
    INVITE_REQUEST_MAX_REJECTIONS = 3
# 每 IP 每日最多申请数
INVITE_REQUEST_MAX_PER_IP_DAY = 3
try:
    INVITE_REQUEST_MAX_PER_IP_DAY = max(
        1,
        int(_strip_quotes(os.getenv("INVITE_REQUEST_MAX_PER_IP_DAY", "3"))),
    )
except (TypeError, ValueError):
    INVITE_REQUEST_MAX_PER_IP_DAY = 3


# ── 登录验证码（四位随机数字，一次性，TTL 秒）────────────────────────
CAPTCHA_ENABLED = (
    _strip_quotes(os.getenv("CAPTCHA_ENABLED", "on")).lower()
    not in ("off", "0", "false", "no")
)
CAPTCHA_TTL_SECONDS = 300
try:
    CAPTCHA_TTL_SECONDS = max(
        30, int(_strip_quotes(os.getenv("CAPTCHA_TTL_SECONDS", "300")))
    )
except (TypeError, ValueError):
    CAPTCHA_TTL_SECONDS = 300


# ── 登录防爆破（冷却 / 冻结 / 每 IP 上限）────────────────────────
LOGIN_FAIL_LIMIT = 3
try:
    LOGIN_FAIL_LIMIT = max(
        1, int(_strip_quotes(os.getenv("LOGIN_FAIL_LIMIT", "3")))
    )
except (TypeError, ValueError):
    LOGIN_FAIL_LIMIT = 3
LOGIN_LOCK_MINUTES = 5
try:
    LOGIN_LOCK_MINUTES = max(
        1, int(_strip_quotes(os.getenv("LOGIN_LOCK_MINUTES", "5")))
    )
except (TypeError, ValueError):
    LOGIN_LOCK_MINUTES = 5
LOGIN_FREEZE_WINDOW_MINUTES = 10
try:
    LOGIN_FREEZE_WINDOW_MINUTES = max(
        1, int(_strip_quotes(os.getenv("LOGIN_FREEZE_WINDOW_MINUTES", "10")))
    )
except (TypeError, ValueError):
    LOGIN_FREEZE_WINDOW_MINUTES = 10
LOGIN_FREEZE_THRESHOLD = 6
try:
    LOGIN_FREEZE_THRESHOLD = max(
        1, int(_strip_quotes(os.getenv("LOGIN_FREEZE_THRESHOLD", "6")))
    )
except (TypeError, ValueError):
    LOGIN_FREEZE_THRESHOLD = 6
LOGIN_MAX_FAIL_PER_IP_10MIN = 10
try:
    LOGIN_MAX_FAIL_PER_IP_10MIN = max(
        1,
        int(_strip_quotes(os.getenv("LOGIN_MAX_FAIL_PER_IP_10MIN", "10"))),
    )
except (TypeError, ValueError):
    LOGIN_MAX_FAIL_PER_IP_10MIN = 10
# 每 IP 上限触发后的冷却时长（分钟）
LOGIN_IP_LOCK_MINUTES = 10
try:
    LOGIN_IP_LOCK_MINUTES = max(
        1, int(_strip_quotes(os.getenv("LOGIN_IP_LOCK_MINUTES", "10")))
    )
except (TypeError, ValueError):
    LOGIN_IP_LOCK_MINUTES = 10
# 自助解冻开关（on：正确密码+邮箱验证码解冻；off：退回仅管理员解冻）
LOGIN_UNFREEZE_VIA_EMAIL = (
    _strip_quotes(os.getenv("LOGIN_UNFREEZE_VIA_EMAIL", "on")).lower()
    not in ("off", "0", "false", "no")
)
# 多 IP 分布式冻结检测（选配）
LOGIN_DISTRIBUTED_IP_DETECT = (
    _strip_quotes(os.getenv("LOGIN_DISTRIBUTED_IP_DETECT", "off")).lower()
    in ("on", "1", "true", "yes")
)
# 自动解冻小时数（0=不自动，需自助或管理员）
LOGIN_FREEZE_AUTO_UNFREEZE_HOURS = 0
try:
    LOGIN_FREEZE_AUTO_UNFREEZE_HOURS = max(
        0,
        int(
            _strip_quotes(
                os.getenv("LOGIN_FREEZE_AUTO_UNFREEZE_HOURS", "0")
            )
        ),
    )
except (TypeError, ValueError):
    LOGIN_FREEZE_AUTO_UNFREEZE_HOURS = 0


# 子目录（启动时自动创建）
RESUMES_DIR = os.path.join(DATA_DIR, "resumes")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
ARCHIVES_DIR = os.path.join(DATA_DIR, "archives")
INVITE_REQUESTS_DIR = os.path.join(DATA_DIR, "invite_requests")
INVITE_CODES_DIR = os.path.join(DATA_DIR, "invite_codes")
INVITE_CODES_AUDIT_DIR = os.path.join(DATA_DIR, "invite_codes_audit")
LOGIN_FAILURES_DIR = os.path.join(DATA_DIR, "login_failures")
RATE_LIMITS_DIR = os.path.join(DATA_DIR, "rate_limits")
ADMIN_OPS_DIR = os.path.join(DATA_DIR, "admin_ops")      # 管理操作审计（删除/创建/改权限/重置密码）
USER_USAGE_DIR = os.path.join(DATA_DIR, "usage")          # 用户使用次数（按天聚合）

# 保留策略（天）：审计记录 / 用户使用统计明细，超过的天数在懒清理时移除
ADMIN_OPS_RETENTION_DAYS = 180
try:
    ADMIN_OPS_RETENTION_DAYS = max(
        1,
        int(_strip_quotes(os.getenv("ADMIN_OPS_RETENTION_DAYS", "180"))),
    )
except (TypeError, ValueError):
    ADMIN_OPS_RETENTION_DAYS = 180
USER_USAGE_RETENTION_DAYS = 365
try:
    USER_USAGE_RETENTION_DAYS = max(
        1,
        int(_strip_quotes(os.getenv("USER_USAGE_RETENTION_DAYS", "365"))),
    )
except (TypeError, ValueError):
    USER_USAGE_RETENTION_DAYS = 365
PRUNE_TOUCH_FILE = os.path.join(DATA_DIR, ".last_prune")  # 懒清理哨兵：每日最多清理一次
for _d in (
    DATA_DIR,
    RESUMES_DIR,
    JOBS_DIR,
    ARCHIVES_DIR,
    INVITE_REQUESTS_DIR,
    INVITE_CODES_DIR,
    INVITE_CODES_AUDIT_DIR,
    LOGIN_FAILURES_DIR,
    RATE_LIMITS_DIR,
    ADMIN_OPS_DIR,
    USER_USAGE_DIR,
    LOG_DIR,
):
    os.makedirs(_d, exist_ok=True)


def check_production():
    """
    生产环境启动校验。
    - SESSION_SECRET_KEY 和 JWT_SECRET_KEY 不能是默认值。
    - LLM_API_KEY 由用户在前端按请求提供，不作为硬性启动条件。
    """
    if ENV == "production":
        if not SESSION_SECRET_KEY or SESSION_SECRET_KEY == "change-me":
            raise SystemExit(
                "[config] ENV=production 时 SESSION_SECRET_KEY 必须改成随机字符串。\n"
                "生成：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        if not JWT_SECRET_KEY or JWT_SECRET_KEY == "change-me-jwt-secret":
            raise SystemExit(
                "[config] ENV=production 时 JWT_SECRET_KEY 必须改成随机字符串。\n"
                "生成：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
