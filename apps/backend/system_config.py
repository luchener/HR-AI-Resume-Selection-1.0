"""
运行时系统配置（账号管理 → 邮件服务）。

背景
----
SMTP 配置原先只能改 .env 并重启后端容器。本模块把它搬到
data/system_config/mail.json，管理员在界面上改完即生效，不需要登录服务器。

设计约束
--------
1) 优先级：界面配置 > .env（config.SMTP_*） > 无。读取结果回显 source，
   避免"我明明改了怎么没生效"。
2) 两套发件槽位：transactional（注册验证码/找回密码/邀请码）与
   notification（公告、群发通知）。notification 未单独配置时依次回退
   到 transactional、.env。
3) 多 worker：线上 4 个 gunicorn worker 各持缓存，靠文件 mtime_ns 比对
   失效。保存后所有 worker 自然生效，无需重启、无需跨进程广播。
4) fail-safe：文件缺失、JSON 损坏、字段非法一律回退 .env —— 配置模块
   本身绝不把邮件链路打死。
5) 落盘：明文 + 0600（与 .env 同级，不引入新秘密）。encrypt/decrypt 已留
   挂载点，将来要上 Fernet 只改这两处。

安全约定
--------
- 读取接口永不返回密码，只返回 password_set 与固定掩码；
- 保存时 password 为空串/缺省 = 保持不变（前端拿到的是掩码，回传掩码
  不能覆盖真密码）；
- 发件人显示名/地址会进入邮件头，含 CR/LF 一律拒绝（防邮件头注入）。
"""
import ipaddress
import json
import logging
import os
import socket
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

NAME = "mail"
SLOT_TRANSACTIONAL = "transactional"   # 注册验证码 / 找回密码 / 邀请码
SLOT_NOTIFICATION = "notification"     # 公告通知 / 群发邮件
SLOTS = (SLOT_TRANSACTIONAL, SLOT_NOTIFICATION)
SECURITY_MODES = ("ssl", "starttls", "plain")

_MAX_LEN = {"host": 253, "username": 320, "from_address": 320, "from_name": 100,
            "base_url": 500, "model": 200}
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_MASK = "\u2022" * 8                  # 掩码固定 8 个圆点，不泄露原值长度与尾字符

# name -> (mtime_ns, parsed)。进程内缓存；靠 mtime 失效实现多 worker 热更新。
_CACHE: dict = {}


class ConfigError(ValueError):
    """配置字段非法（由接口层转成 422）。"""


def _dir() -> str:
    """配置目录。注意这里读 config.DATA_DIR 而非模块常量，便于测试隔离。"""
    return os.path.join(config.DATA_DIR, "system_config")


def _path(name: str = NAME) -> str:
    return os.path.join(_dir(), f"{name}.json")


def _ensure_dir() -> str:
    directory = _dir()
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, _DIR_MODE)
    except OSError:  # Windows 上 chmod 语义有限，失败不影响功能
        pass
    return directory


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(name: str = NAME) -> tuple:
    """读取配置。返回 (data, mtime_ns)。文件不存在或损坏时返回 ({}, mtime)。"""
    path = _path(name)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _CACHE.pop(path, None)
        return {}, 0

    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return dict(cached[1]), mtime

    data: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("根节点必须是对象")
        data = loaded
    except Exception as exc:  # noqa: BLE001 —— 配置坏了必须能继续跑
        logger.warning("system_config %s 读取失败，回退 .env：%s", name, exc)
        data = {}

    _CACHE[path] = (mtime, data)
    return dict(data), mtime


def raw(name: str = NAME) -> dict:
    return _read(name)[0]


def _write(name: str, data: dict) -> None:
    """原子写：同目录临时文件 + os.replace；Windows 上杀毒/索引器占用句柄时重试。"""
    directory = _ensure_dir()
    handle_fd, tmp = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp, _FILE_MODE)
        except OSError:
            pass
        last_error = None
        for _ in range(5):
            try:
                os.replace(tmp, _path(name))
                _CACHE.pop(_path(name), None)   # 让本进程立刻看到新值；其它 worker 靠 mtime
                return
            except OSError as exc:  # pragma: no cover - 平台相关
                last_error = exc
                import time as _time
                _time.sleep(0.05)
        raise last_error if last_error else OSError("写入配置失败")
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── 校验 ────────────────────────────────────────────────────────────────────

def _clean_text(value, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > _MAX_LEN[field]:
        raise ConfigError(f"{field} 过长（上限 {_MAX_LEN[field]} 字符）")
    if "\r" in text or "\n" in text:
        raise ConfigError(f"{field} 不能包含换行符")
    return text


def _clean_slot(slot: str, incoming: dict, base: dict) -> dict:
    """校验并合并单个槽位。password 为空 = 保持不变。"""
    if not isinstance(incoming, dict):
        raise ConfigError("槽位内容必须是对象")

    merged = dict(base or {})

    for field in ("host", "username", "from_address", "from_name"):
        if field in incoming:
            merged[field] = _clean_text(incoming.get(field), field)

    if "port" in incoming:
        try:
            port = int(incoming.get("port"))
        except (TypeError, ValueError):
            raise ConfigError("端口必须是 1-65535 的整数")
        if not 1 <= port <= 65535:
            raise ConfigError("端口必须是 1-65535 的整数")
        merged["port"] = port

    if "security" in incoming:
        security = str(incoming.get("security") or "").strip().lower()
        if security not in SECURITY_MODES:
            raise ConfigError("加密方式只支持 ssl / starttls / plain")
        merged["security"] = security

    if "enabled" in incoming:
        merged["enabled"] = bool(incoming.get("enabled"))

    # 密码/授权码：空串或未提供 = 用原值（前端拿到的是掩码，不能回传覆盖）
    password = incoming.get("password")
    if isinstance(password, str) and password.strip():
        if len(password) > 512:
            raise ConfigError("密码/授权码过长")
        merged["password"] = password.strip()

    merged.setdefault("host", "")
    merged.setdefault("port", 465)
    merged.setdefault("security", "ssl")
    merged.setdefault("username", "")
    merged.setdefault("password", "")
    merged.setdefault("from_address", "")
    merged.setdefault("from_name", "AI 简历智选")
    merged.setdefault("enabled", True)
    return merged


# ── 解析（界面 > .env）──────────────────────────────────────────────────────

def _env_slot() -> dict:
    return {
        "host": config.SMTP_HOST or "",
        "port": config.SMTP_PORT,
        "security": "ssl" if config.SMTP_PORT == 465 else "starttls",
        "username": config.SMTP_USER or "",
        "password": config.SMTP_PASSWORD or "",
        "from_address": config.SMTP_FROM or config.SMTP_USER or "",
        "from_name": "AI 简历智选",
        "enabled": True,
    }


def _ready(cfg: dict) -> bool:
    return bool(cfg.get("host") and cfg.get("username") and cfg.get("password"))


def _coerce(slot_cfg: dict) -> dict:
    cfg = dict(slot_cfg)
    cfg["port"] = int(cfg.get("port") or 465)
    cfg["security"] = str(cfg.get("security") or "ssl").lower()
    cfg["from_address"] = cfg.get("from_address") or cfg.get("username") or ""
    cfg["from_name"] = cfg.get("from_name") or "AI 简历智选"
    return cfg


def resolve_mail(slot: str = SLOT_TRANSACTIONAL) -> dict:
    """解析某槽位最终生效的发件配置。

    返回字段：host/port/security/username/password/from_address/from_name/
             ready/source/slot。source 取值：ui / ui.transactional / env / none。
    """
    if slot not in SLOTS:
        slot = SLOT_TRANSACTIONAL

    slots = raw(NAME).get("slots") or {}

    ui = slots.get(slot)
    if isinstance(ui, dict) and ui.get("enabled", True) and ui.get("host"):
        cfg = _coerce(ui)
        cfg.update({"source": "ui", "slot": slot, "ready": True})
        if _ready(cfg):
            return cfg

    if slot == SLOT_NOTIFICATION:
        shared = slots.get(SLOT_TRANSACTIONAL)
        if isinstance(shared, dict) and shared.get("enabled", True) and shared.get("host"):
            cfg = _coerce(shared)
            cfg.update({"source": "ui.transactional", "slot": slot, "ready": True})
            if _ready(cfg):
                return cfg

    env = _env_slot()
    if _ready(env):
        env.update({"source": "env", "slot": slot, "ready": True})
        return env

    env.update({"source": "none", "slot": slot, "ready": False})
    return env


# ── 对外视图与写入 ──────────────────────────────────────────────────────────

def _slot_view(slot: str) -> dict:
    cfg = resolve_mail(slot)
    return {
        "slot": slot,
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 465),
        "security": cfg.get("security", "ssl"),
        "username": cfg.get("username", ""),
        "password_set": bool(cfg.get("password")),
        "password_mask": _MASK if cfg.get("password") else "",
        "from_address": cfg.get("from_address", ""),
        "from_name": cfg.get("from_name", ""),
        "enabled": bool(cfg.get("enabled", True)),
        "source": cfg.get("source", "none"),
        "ready": _ready(cfg),
    }


def public_mail() -> dict:
    """给管理界面的只读视图。任何情况下都不含密码明文。"""
    data = raw(NAME)
    return {
        "slots": {slot: _slot_view(slot) for slot in SLOTS},
        "env_available": _ready(_env_slot()),
        "updated_at": data.get("updated_at", ""),
        "updated_by": data.get("updated_by", ""),
    }


def save_mail(payload: dict, operator: str = "") -> dict:
    """保存界面配置。只写传入的槽位；password 留空表示沿用旧值。"""
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是对象")

    incoming = payload.get("slots")
    if not isinstance(incoming, dict) or not incoming:
        raise ConfigError("没有需要保存的内容")

    data = raw(NAME)
    slots = dict(data.get("slots") or {})

    cleaned = {}
    for slot in SLOTS:
        if slot not in incoming:
            continue
        cleaned[slot] = _clean_slot(slot, incoming.get(slot) or {}, slots.get(slot) or {})
    if not cleaned:
        raise ConfigError("槽位名称不合法")

    slots.update(cleaned)
    data.update({
        "version": 1,
        "slots": slots,
        "updated_at": _now_iso(),
        "updated_by": operator or "",
    })
    _write(NAME, data)
    return public_mail()


def clear_mail(operator: str = "") -> dict:
    """清空界面配置，立即回退到 .env 兜底。"""
    path = _path(NAME)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"清空配置失败：{exc}")
    _CACHE.pop(path, None)
    if operator:
        logger.info("mail config cleared by %s", operator)
    return public_mail()


# ── 加密挂载点（当前明文 + 0600，将来上 Fernet 只改这两处）─────────────────

def encrypt(value: str) -> str:  # pragma: no cover - 预留
    return value


def decrypt(value: str) -> str:  # pragma: no cover - 预留
    return value

# ── 模型配置（账号管理 → 模型配置）──────────────────────────────────────────
#
# 定位是"服务端这一层"：用户在自己浏览器里带的 Key 优先，界面配置次之，.env 最后。
# 所有值都在调用时解析（绝不快照进模块常量），保存后下一个请求立即生效。
# 读取接口永不返回 API Key；base_url 走白名单校验，防止被当成 SSRF 跳板。

NAME_LLM = "llm"
_LLM_MAX = {"base_url": 500, "model": 200, "api_key": 4096}
_LLM_TIMEOUT_RANGE = (1, 3600)
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}


def _allow_private_base_url() -> bool:
    """
    是否允许 base_url 指向内网/回环（仅供本地联调）。

    生产保持关闭：自定义接口地址本身就是 SSRF 面，默认只放行公网 https。
    本地要连自建模型服务时，显式设置 LLM_ALLOW_PRIVATE_BASE_URL=1。
    """
    flag = str(os.environ.get("LLM_ALLOW_PRIVATE_BASE_URL") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _is_private_host(host: str) -> bool:
    """主机是否指向内网/回环/保留地址。域名先解析成 IP 再判断。"""
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return True
    if host in _METADATA_HOSTS:
        return True

    candidates = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            # 解析不了就放行：这种地址本身也连不上，交给真正出网时报错更准确。
            # DNS 重绑定是无法在此根除的 TOCTOU，靠"调用前再校验一次"收敛。
            logger.warning("base_url 主机 %s 解析失败，放行到出网时再报错", host)
            return False
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue

    if not candidates:
        return False
    for ip in candidates:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def validate_base_url(value: str) -> str:
    """校验并归一化 base_url；非法抛 ConfigError（接口层转 422）。"""
    text = _clean_text(value, "base_url")
    if not text:
        raise ConfigError("接口地址不能为空")

    parsed = urlparse(text)
    if parsed.scheme not in ("https", "http"):
        raise ConfigError("接口地址必须以 https:// 开头")
    if parsed.username or parsed.password:
        raise ConfigError("接口地址不能包含用户名或密码")
    if not parsed.hostname:
        raise ConfigError("接口地址缺少主机名")
    if parsed.scheme == "http" and not _allow_private_base_url():
        raise ConfigError("接口地址必须使用 https://")
    if _is_private_host(parsed.hostname) and not _allow_private_base_url():
        raise ConfigError("接口地址不能指向内网 / 回环 / 保留地址")
    return text.rstrip("/")


def _env_llm() -> dict:
    """服务器 .env 里那一层。"""
    return {
        "api_key": config.LLM_API_KEY or "",
        "base_url": config.LLM_BASE_URL or "",
        "model": config.LL_MODEL or "",
        "timeout": 300,
        "enabled": True,
    }


def _coerce_llm(data: dict) -> dict:
    cfg = dict(data or {})
    cfg["api_key"] = str(cfg.get("api_key") or "")
    cfg["base_url"] = str(cfg.get("base_url") or config.LLM_BASE_URL or "")
    cfg["model"] = str(cfg.get("model") or config.LL_MODEL or "deepseek-chat")
    try:
        cfg["timeout"] = int(cfg.get("timeout") or 300)
    except (TypeError, ValueError):
        cfg["timeout"] = 300
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def _llm_ready(cfg: dict) -> bool:
    return bool(cfg.get("api_key") and cfg.get("base_url"))


def resolve_llm() -> dict:
    """
    解析服务端最终生效的模型配置。

    返回 api_key/base_url/model/timeout/ready/source（source ∈ ui|env|none）。
    用户请求里自带的 Key 优先级更高，本函数只负责服务端这一层。
    """
    data = raw(NAME_LLM)
    if data and data.get("enabled", True):
        cfg = _coerce_llm(data)
        if _llm_ready(cfg):
            cfg.update({"source": "ui", "ready": True})
            return cfg

    env = _coerce_llm(_env_llm())
    if _llm_ready(env):
        env.update({"source": "env", "ready": True})
        return env

    env.update({"source": "none", "ready": False})
    return env


def public_llm() -> dict:
    """给管理界面的只读视图。任何情况下都不含 API Key 明文。"""
    data = raw(NAME_LLM)
    cfg = resolve_llm()
    models = data.get("models") if isinstance(data.get("models"), list) else []
    return {
        "api_key_set": bool(cfg.get("api_key")),
        "api_key_mask": _MASK if cfg.get("api_key") else "",
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "timeout": cfg.get("timeout", 300),
        "enabled": bool(cfg.get("enabled", True)),
        "source": cfg.get("source", "none"),
        "ready": bool(cfg.get("ready")),
        "env_available": _llm_ready(_coerce_llm(_env_llm())),
        "models": [str(item) for item in models][:200],
        "models_fetched_at": str(data.get("models_fetched_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "updated_by": str(data.get("updated_by") or ""),
    }


def save_llm(payload: dict, operator: str = "") -> dict:
    """保存界面配置。api_key 为空串/缺省 = 保持原值（前端拿到的是掩码）。"""
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是对象")

    merged = dict(raw(NAME_LLM))

    if "base_url" in payload:
        merged["base_url"] = validate_base_url(payload.get("base_url"))
    if "model" in payload:
        model = _clean_text(payload.get("model"), "model")
        if not model:
            raise ConfigError("默认模型不能为空")
        merged["model"] = model
    if "timeout" in payload:
        try:
            timeout = int(payload.get("timeout"))
        except (TypeError, ValueError):
            raise ConfigError("超时必须是 1-3600 的整数")
        if not _LLM_TIMEOUT_RANGE[0] <= timeout <= _LLM_TIMEOUT_RANGE[1]:
            raise ConfigError("超时必须是 1-3600 的整数")
        merged["timeout"] = timeout
    if "enabled" in payload:
        merged["enabled"] = bool(payload.get("enabled"))

    api_key = payload.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        text = api_key.strip()
        if len(text) > _LLM_MAX["api_key"]:
            raise ConfigError("API Key 过长")
        if "\r" in text or "\n" in text:
            raise ConfigError("API Key 不能包含换行符")
        merged["api_key"] = text

    if not merged.get("api_key"):
        raise ConfigError("尚未配置 API Key，请先填写")
    if not merged.get("base_url"):
        raise ConfigError("尚未配置接口地址，请先填写")
    if not merged.get("model"):
        merged["model"] = config.LL_MODEL or "deepseek-chat"

    merged.update({
        "version": 1,
        "updated_at": _now_iso(),
        "updated_by": operator or "",
    })
    _write(NAME_LLM, merged)
    return public_llm()


def store_models(models, operator: str = "") -> dict:
    """缓存「获取模型列表」的结果，刷新页面后下拉框还能用。"""
    data = dict(raw(NAME_LLM))
    cleaned = []
    for item in models or []:
        text = str(item or "").strip()
        if text and len(text) <= 200 and text not in cleaned:
            cleaned.append(text)
    data["models"] = cleaned[:200]
    data["models_fetched_at"] = _now_iso()
    if operator:
        data["updated_by"] = operator
    _write(NAME_LLM, data)
    return public_llm()


def clear_llm(operator: str = "") -> dict:
    """清空界面配置，立即回退到 .env。"""
    path = _path(NAME_LLM)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"清空配置失败：{exc}")
    _CACHE.pop(path, None)
    if operator:
        logger.info("llm config cleared by %s", operator)
    return public_llm()

