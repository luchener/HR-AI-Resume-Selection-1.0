"""
每日使用次数配额核心（按账号限次 + 账号禁用 + 次数用尽通知）。

- 配置：data/quota/limits.json
    {
      "quota_enabled": false,        // 全局配额总开关（关=全部账号不限次）
      "default_daily": 10,           // 全局默认每日限额；None/-1 = 不限
      "email_notify_enabled": false, // 次数用尽邮件提醒总开关
      "users": {                     // 逐账号覆盖
        "zhangsan": {"daily_limit": 3, "analysis_enabled": true, "email_notify": true, "updated_at": "..."}
      },
      "updated_at": "..."
    }
- 已用次数：直接读 auth 的 data/usage/<user_id>.json 当天 analysis 计数（与埋点同口径，
  成功分析才 +1；失败不计数）。日期键与 record_user_usage 一致（UTC 自然日）。
- 校验：hr_analysis 入口调用 check_analysis_quota(user_id, requested_count)。
- 邮件提醒：次数用尽且账号有邮箱且开关开 → 每账号每天最多一封（rate_limits 键去重）。
"""
import os
import uuid
from datetime import datetime, timezone

import config
import mailer
from store import _read_json as _store_read_json, _write_json as _store_write_json

MAX_DAILY_LIMIT = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_key() -> str:
    """与 auth.record_user_usage 同口径（UTC 自然日）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_json(path: str, data: dict) -> None:
    _store_write_json(path, data)


def _read_json(path: str):
    return _store_read_json(path)


def _defaults() -> dict:
    return {
        "quota_enabled": False,
        "default_daily": None,
        "email_notify_enabled": False,
        "users": {},
        "updated_at": "",
    }


def _load() -> dict:
    data = _read_json(config.QUOTA_LIMITS_PATH) or {}
    d = _defaults()
    for key in ("quota_enabled", "default_daily", "email_notify_enabled", "updated_at"):
        if key in data:
            d[key] = data[key]
    users = data.get("users")
    d["users"] = users if isinstance(users, dict) else {}
    return d


def _save(data: dict) -> None:
    data["updated_at"] = _now_iso()
    _write_json(config.QUOTA_LIMITS_PATH, data)


def _user_cfg(username: str, data: dict | None = None) -> dict:
    data = data or _load()
    cfg = data.get("users", {}).get(username) or {}
    return cfg if isinstance(cfg, dict) else {}


def _usage_path(user_id: str) -> str:
    return os.path.join(config.USER_USAGE_DIR, f"{user_id}.json")


def today_analysis_count(user_id: str) -> int:
    """当日已成功完成的分析次数（读 usage，与埋点同口径）。"""
    usage = _read_json(_usage_path(user_id)) or {}
    entry = usage.get(_today_key()) or {}
    try:
        return max(0, int(entry.get("analysis", 0)))
    except (TypeError, ValueError):
        return 0


def _resolve_daily_limit(username: str, data: dict | None = None):
    """逐账号 > 全局默认 > 不限。返回 None 表示不限。"""
    data = data or _load()
    cfg = _user_cfg(username, data)
    if cfg.get("daily_limit") is not None:
        try:
            v = int(cfg.get("daily_limit"))
            return v if v >= 0 else -1
        except (TypeError, ValueError):
            return -1
    dd = data.get("default_daily")
    if dd is None:
        return None
    try:
        v = int(dd)
        return v if v >= 0 else -1
    except (TypeError, ValueError):
        return None


def _effective_limit(username: str, user: dict | None, data: dict | None = None):
    """
    某账号的「生效限额」。
      管理员 + 未显式配置 daily_limit → None（不限；全局默认也不适用于管理员）
      管理员 + 显式配置 daily_limit   → 按配置执行（便于管理员自助验证限额）
      普通账号                        → 逐账号配置 > 全局默认 > None
    返回 None 或 <0 表示不限。
    """
    data = data or _load()
    if user and bool(user.get("is_admin")):
        cfg = _user_cfg(username, data)
        if cfg.get("daily_limit") is None:
            return None
    return _resolve_daily_limit(username, data)


def _analysis_enabled(username: str, data: dict | None = None) -> bool:
    cfg = _user_cfg(username, data)
    return bool(cfg.get("analysis_enabled", True))


def check_analysis_quota(user_id: str, requested_count: int):
    """
    校验本次分析是否允许。返回 None=放行；否则 (detail, status_code)。
    优先级：账号分析开关（独立于配额开关，管理员同样生效）→ 全局限额开关 → 生效限额
    （管理员默认不限，显式配置 daily_limit 后同样受限额约束）→ 额度校验。
    返回 None=放行；否则 (detail, status)；403=账号被禁用，429=额度不足。
    """
    import auth as auth_mod
    requested_count = max(1, int(requested_count or 1))
    data = _load()

    user = auth_mod.find_user_by_id(user_id)
    if not user:
        return None
    username = user.get("username") or ""

    # ① 账号级分析开关：关 = 该账号整体禁用（独立于配额开关，管理员同样生效）
    if not _analysis_enabled(username, data):
        return ("该账号的分析功能已被禁用，请联系管理员。", 403)

    # ② 全局限额开关：关 = 配额全部不生效（回到现状）
    if not bool(data.get("quota_enabled")):
        return None

    # ③④ 额度校验（管理员默认不限；若显式配置了限额则同样生效）
    limit = _effective_limit(username, user, data)
    if limit is None or limit < 0:
        return None
    used = today_analysis_count(user_id)
    remaining = limit - used
    if remaining <= 0:
        _notify_quota_exhausted(username, limit, used)
        return (f"今日分析次数已用完（限额 {limit} 次），明天 0 点重置。", 429)
    if requested_count > remaining:
        return (f"本次需要 {requested_count} 次，今日剩余 {remaining} 次，请减少选择或明天再试。", 429)
    return None


def _notify_quota_exhausted(username: str, limit: int, used: int) -> None:
    """次数用尽邮件提醒：每账号每天最多一封，无邮箱自动跳过。"""
    try:
        import auth as auth_mod
        data = _load()
        if not bool(data.get("email_notify_enabled")):
            return
        cfg = _user_cfg(username, data)
        if not bool(cfg.get("email_notify", True)):
            return
        user = auth_mod.find_user_by_username(username)
        if not user or not user.get("email"):
            return  # 无邮箱账号：仅弹窗，跳过邮件
        dedup_key = f"quota_email_{username}_{_today_key()}"
        dedup_path = os.path.join(config.RATE_LIMITS_DIR, f"{dedup_key}.json")
        if _read_json(dedup_path) is not None:
            return
        subject = "【AI简历智选】今日分析次数已用完"
        plain = (
            f"你好，{username}：\n\n"
            f"你今天的使用次数（限额 {limit} 次）已用完，明天 0 点自动重置。\n"
            f"如需更多次数，请联系管理员调整配额。\n\n"
            f"（本邮件由系统自动发送，请勿回复）"
        )
        html = (
            "<div style='font-family:system-ui,sans-serif;max-width:520px;margin:0 auto'>"
            "<h2 style='color:#1b2a45'>今日分析次数已用完</h2>"
            f"<p>你好，{username}：</p>"
            f"<p>你今天的使用次数（限额 <b>{limit}</b> 次）已用完，明天 0 点自动重置。</p>"
            "<p>如需更多次数，请联系管理员调整配额。</p>"
            "<p style='color:#8a93a6;font-size:12px'>本邮件由系统自动发送，请勿回复。</p>"
            "</div>"
        )
        mailer.send_broadcast_email(user["email"], subject, plain, html)
        # 落去重标记（仅发送成功后才标记）
        _write_json(dedup_path, {"sent": _now_iso()})
    except Exception as exc:
        import logging
        logging.getLogger("quota").warning("quota email notify failed: %s", exc)


def get_settings() -> dict:
    """管理端总览：全局配置 + 逐账号（含今日已用/剩余/来源）。"""
    import auth as auth_mod
    data = _load()
    users_out = []
    for username, rec in sorted(data.get("users", {}).items()):
        u = auth_mod.find_user_by_username(username)
        if not u:
            continue
        limit = _effective_limit(username, u, data)
        used = today_analysis_count(u.get("user_id") or "")
        remaining = -1 if limit is None or limit < 0 else max(0, limit - used)
        raw = rec.get("daily_limit")
        users_out.append({
            "username": username,
            "is_admin": bool(u.get("is_admin")),
            "has_email": bool(u.get("email")),
            "daily_limit": limit,
            "raw_daily_limit": raw,
            "custom": raw is not None,
            "analysis_enabled": bool(rec.get("analysis_enabled", True)),
            "email_notify": bool(rec.get("email_notify", True)),
            "used": used,
            "remaining": remaining,
            "updated_at": rec.get("updated_at", ""),
        })
    return {
        "quota_enabled": bool(data.get("quota_enabled")),
        "default_daily": data.get("default_daily"),
        "email_notify_enabled": bool(data.get("email_notify_enabled")),
        "users": users_out,
        "updated_at": data.get("updated_at", ""),
    }


_SETTINGS_FIELDS = ("quota_enabled", "default_daily", "email_notify_enabled")
_USER_FIELDS = ("daily_limit", "analysis_enabled", "email_notify")


def _require_fields(payload, allowed, what: str) -> dict:
    """
    拒绝空请求体。
    背景：前端 fetch 传字符串 body 时若没显式带 Content-Type: application/json，
    浏览器按规范发 text/plain，Flask 的 get_json(silent=True) 解析失败返回 None，
    接口会"200 但什么都没写"（还会留下一条只有 updated_at 的空配置）。这里硬性拦截。
    """
    if not isinstance(payload, dict) or not any(k in payload for k in allowed):
        raise ValueError(
            f"{what}请求体为空或不是 JSON：需 Content-Type: application/json，"
            f"且至少包含 {' / '.join(allowed)} 中的一项。"
        )
    return payload


def update_settings(payload: dict) -> dict:
    payload = _require_fields(payload, _SETTINGS_FIELDS, "全局配额")
    data = _load()
    if "quota_enabled" in payload:
        data["quota_enabled"] = bool(payload.get("quota_enabled"))
    if "default_daily" in payload:
        data["default_daily"] = _coerce_limit(payload.get("default_daily"))
    if "email_notify_enabled" in payload:
        data["email_notify_enabled"] = bool(payload.get("email_notify_enabled"))
    _save(data)
    return get_settings()


def set_user_quota(username: str, payload: dict) -> dict:
    payload = _require_fields(payload, _USER_FIELDS, "账号配额")
    data = _load()
    cfg = dict(_user_cfg(username, data))
    if "daily_limit" in payload:
        cfg["daily_limit"] = _coerce_limit(payload.get("daily_limit"))
    if "analysis_enabled" in payload:
        cfg["analysis_enabled"] = bool(payload.get("analysis_enabled"))
    if "email_notify" in payload:
        cfg["email_notify"] = bool(payload.get("email_notify"))
    cfg["updated_at"] = _now_iso()
    data["users"][username] = cfg
    _save(data)
    return get_settings()


def reset_user_quota(username: str) -> bool:
    data = _load()
    if username not in data.get("users", {}):
        return False
    del data["users"][username]
    _save(data)
    return True


def _coerce_limit(value):
    """
    解析 daily_limit。
      None / ""  → None（未配置：逐账号=继承全局默认，全局=不限）
      -1         → -1（显式不限，覆盖全局默认）
      0          → 0（禁用该账号分析）
      >=1        → 具体次数
    越界（<-1 或 >MAX_DAILY_LIMIT）抛 ValueError。
    """
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError("每日限额必须是整数。") from None
    if v < -1 or v > MAX_DAILY_LIMIT:
        raise ValueError(f"每日限额需在 -1 ~ {MAX_DAILY_LIMIT} 之间（-1=不限）。")
    return v


def get_my_quota(user_id: str) -> dict:
    """
    用户端 quota/me。与实际拦截口径 check_analysis_quota 完全一致：
      ① 账号禁用（独立于全局开关，管理员同样生效）
      ② 全局配额开关关闭 → 不限额
      ③ 生效限额（管理员未显式配置则不限；显式配置则生效）
    """
    import auth as auth_mod
    user = auth_mod.find_user_by_id(user_id)
    if not user:
        return {"analysis_enabled": True, "unlimited": True}
    username = user.get("username") or ""
    is_admin = bool(user.get("is_admin"))
    data = _load()

    # ① 账号禁用优先于一切（必须与后端实际拦截口径一致，否则前端不会提示）
    if not _analysis_enabled(username, data):
        return {"analysis_enabled": False, "unlimited": True, "is_admin": is_admin}

    # ② 全局配额开关关闭 → 不限额（禁用判断已在 ① 处理）
    if not bool(data.get("quota_enabled")):
        return {"analysis_enabled": True, "unlimited": True, "quota_enabled": False, "is_admin": is_admin}

    # ③ 生效限额（管理员默认不限；显式配置则生效）
    limit = _effective_limit(username, user, data)
    if limit is None or limit < 0:
        return {"analysis_enabled": True, "unlimited": True, "quota_enabled": True, "is_admin": is_admin}
    used = today_analysis_count(user_id)
    return {
        "analysis_enabled": True,
        "unlimited": False,
        "quota_enabled": True,
        "daily_limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "resets_at": "次日 0 点自动重置",
    }
