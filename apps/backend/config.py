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

# 子目录（启动时自动创建）
RESUMES_DIR = os.path.join(DATA_DIR, "resumes")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
ARCHIVES_DIR = os.path.join(DATA_DIR, "archives")
for _d in (DATA_DIR, RESUMES_DIR, JOBS_DIR, ARCHIVES_DIR, LOG_DIR):
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
