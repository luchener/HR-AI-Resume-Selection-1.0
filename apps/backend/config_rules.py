"""Shared pure configuration merge rules; no environment or storage access."""
from datetime import datetime, timezone

SECURITY_MODES = ("ssl", "starttls", "plain")
SLOTS = ("transactional", "notification")
_MAX_LEN = {"host": 253, "username": 320, "from_address": 320, "from_name": 100,
            "base_url": 500, "model": 200}
_LLM_MAX = {"base_url": 500, "model": 200, "api_key": 4096}
_LLM_TIMEOUT_RANGE = (1, 3600)

class ConfigError(ValueError):
    """配置字段非法（由接口层转成 422）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def merge_mail(current: dict, payload: dict, operator: str = "") -> dict:
    """校验并合并完整邮件文档；不读取或写入存储。"""
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是对象")

    incoming = payload.get("slots")
    if not isinstance(incoming, dict) or not incoming:
        raise ConfigError("没有需要保存的内容")

    data = dict(current)
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
    return data


def merge_llm(current: dict, payload: dict, operator: str = "", *, default_model=None, validate_base_url) -> dict:
    """校验并合并完整模型文档；不读取或写入存储。"""
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是对象")

    merged = dict(current)

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
        merged["model"] = default_model or "deepseek-chat"

    merged.update({
        "version": 1,
        "updated_at": _now_iso(),
        "updated_by": operator or "",
    })
    return merged

