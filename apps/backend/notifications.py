"""
通知与邮件群发核心逻辑（公告弹窗 + 邮件通知）。

- 公告：data/announcements/<id>.json，状态机 scheduled/active/expired/cancelled。
  投放时间 = 开始时间 + 有效期（分钟/小时/天）；惰性状态判定（读时计算），
  规避 gunicorn 多 worker 常驻定时器问题；前端轮询拉取。
- 用户关闭记录：data/announcement_reads/<user_id>.json（dismissed 列表）。
- 邮件图片：data/email_images/<image_id>.<ext>，CID 内联进邮件，不依赖公网图床。
- 邮件群发：逐封同步发送 + 结果汇总，落盘 data/email_logs/<id>.json。
"""
import base64
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import config
import email_templates
import mailer
from store import _read_json as _store_read_json, _write_json as _store_write_json

# 文本上限（与 email_templates 一致，路由层复用）
MAX_TITLE = 100
MAX_CONTENT = 5000
MAX_SELECTED = 200


# ── 时间/JSON 工具 ────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value) -> datetime:
    """解析 ISO 时间（兼容 Z 后缀与带偏移），统一转 UTC。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("时间不能为空。")
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atomic_replace(tmp: str, path: str) -> None:
    """
    os.replace 的 Windows 容错包装。

    Windows 上杀毒软件 / 索引器会瞬间持有刚写出的 .tmp 或目标文件句柄，os.replace 随之抛
    PermissionError(WinError 5)，在高频写接口上表现为随机 500（真实出现过：
    /api/v1/archives/<id>/tags 返回 500、登录失败计数写不进去）。短暂退避重试即可绕开。
    """
    for _attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (_attempt + 1))
    os.replace(tmp, path)  # 最后一次仍失败就抛出，让调用方看到真实错误


def _write_json(path: str, data: dict) -> None:
    _store_write_json(path, data)


def _read_json(path: str):
    return _store_read_json(path)


# ── 公告 ──────────────────────────────────────────────────────────────
_TIME_UNITS = {"minute": 1, "hour": 60, "day": 1440}


def compute_schedule(
    start_mode: str,
    delay_value,
    delay_unit: str,
    duration_value,
    duration_unit: str,
    start_at: str = "",
) -> tuple[str, str]:
    """
    计算投放窗口 (start_at, end_at) ISO（UTC）。
    start_mode: now（立即）/ at（指定时刻）/ delay（延迟 N 分钟/小时/天）。
    """
    now = _now()
    if start_mode == "at":
        dt = _parse_iso(start_at)
        if dt < now:
            dt = now  # 已过期 → 立即生效
    elif start_mode == "delay":
        unit_min = _TIME_UNITS.get(delay_unit)
        if not unit_min:
            raise ValueError("延迟时间单位无效。")
        try:
            delay = max(0, int(delay_value or 0))
        except (TypeError, ValueError):
            raise ValueError("延迟时间必须为数字。")
        dt = now + timedelta(minutes=delay * unit_min)
    else:
        dt = now

    unit_min = _TIME_UNITS.get(duration_unit)
    if not unit_min:
        raise ValueError("有效期单位无效。")
    try:
        dur = max(1, int(duration_value or 0))
    except (TypeError, ValueError):
        raise ValueError("有效期必须为数字。")
    end = dt + timedelta(minutes=dur * unit_min)
    return dt.isoformat(), end.isoformat()


def _announcement_path(announcement_id: str) -> str:
    return os.path.join(config.ANNOUNCEMENTS_DIR, f"{announcement_id}.json")


def effective_status(rec: dict, now: datetime | None = None) -> str:
    """惰性状态：scheduled → active → expired；cancelled 恒定。"""
    st = rec.get("status")
    if st == "cancelled":
        return "cancelled"
    if st != "active":
        return st or "scheduled"
    now = now or _now()
    try:
        start = _parse_iso(rec.get("start_at"))
        end = _parse_iso(rec.get("end_at"))
    except ValueError:
        return "scheduled"
    if now < start:
        return "scheduled"
    if now >= end:
        return "expired"
    return "active"


def create_announcement(
    *,
    title: str,
    content: str,
    start_mode: str,
    delay_value=None,
    delay_unit: str = "minute",
    duration_value=None,
    duration_unit: str = "day",
    start_at: str = "",
    operator_id: str = "",
    operator_name: str = "",
) -> dict:
    """创建公告。start_mode: now / at / delay。返回完整记录。"""
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise ValueError("公告标题不能为空。")
    if len(title) > MAX_TITLE:
        raise ValueError(f"公告标题不能超过 {MAX_TITLE} 字。")
    if not content:
        raise ValueError("公告内容不能为空。")
    if len(content) > MAX_CONTENT:
        raise ValueError(f"公告内容不能超过 {MAX_CONTENT} 字。")

    start, end = compute_schedule(
        start_mode, delay_value, delay_unit, duration_value, duration_unit, start_at
    )
    announcement_id = str(uuid.uuid4())
    rec = {
        "announcement_id": announcement_id,
        "title": title,
        "content": content,
        "start_mode": start_mode,
        "start_at": start,
        "end_at": end,
        "status": "active",
        "created_by_id": operator_id or "",
        "created_by_name": operator_name or "",
        "created_at": _now_iso(),
    }
    _write_json(_announcement_path(announcement_id), rec)
    return rec


def get_announcement(announcement_id: str) -> dict | None:
    rec = _read_json(_announcement_path(announcement_id))
    return rec


def _list_all_announcements() -> list[dict]:
    """全部公告（含已删除标记），按 start_at 倒序。内部用。"""
    out = []
    if os.path.isdir(config.ANNOUNCEMENTS_DIR):
        for name in os.listdir(config.ANNOUNCEMENTS_DIR):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            rec = _read_json(os.path.join(config.ANNOUNCEMENTS_DIR, name))
            if not rec:
                continue
            rec["status"] = effective_status(rec)
            out.append(rec)
    out.sort(key=lambda r: str(r.get("start_at") or ""), reverse=True)
    return out


def list_announcements(page: int = 1, size: int = 20) -> dict:
    """公告列表分页（固定窗口）：过滤已删除记录，返回 {total, items}。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = max(1, min(100, int(size)))
    except (TypeError, ValueError):
        size = 20
    all_items = [r for r in _list_all_announcements() if not r.get("deleted_at")]
    total = len(all_items)
    start = (page - 1) * size
    return {"total": total, "items": all_items[start:start + size]}


def cancel_announcement(announcement_id: str) -> bool:
    """撤回公告（仅 active 可撤回；scheduled/expired 也允许标记取消）。"""
    rec = get_announcement(announcement_id)
    if not rec:
        return False
    rec["status"] = "cancelled"
    rec["cancelled_at"] = _now_iso()
    _write_json(_announcement_path(announcement_id), rec)
    return True


def delete_announcement(announcement_id: str) -> bool:
    """删除公告（软删除）：记录保留在磁盘，列表不再展示、用户端不再弹出。
    删除时间与操作者由路由侧写入审计记录（announcement_delete）。"""
    rec = get_announcement(announcement_id)
    if not rec:
        return False
    if rec.get("deleted_at"):
        return True
    rec["deleted_at"] = _now_iso()
    _write_json(_announcement_path(announcement_id), rec)
    return True


# ── 用户端：未读公告 / 关闭 ──────────────────────────────────────────
def _reads_path(user_id: str) -> str:
    return os.path.join(config.ANNOUNCEMENT_READS_DIR, f"{user_id}.json")


def _dismissed_ids(user_id: str) -> set[str]:
    rec = _read_json(_reads_path(user_id)) or {}
    return set(rec.get("dismissed") or [])


def user_active_announcements(user_id: str) -> list[dict]:
    """当前用户可弹出的公告（投放期内 且 未被该用户关闭且未删除），按 start_at 倒序。"""
    dismissed = _dismissed_ids(user_id)
    out = []
    for rec in _list_all_announcements():
        if rec.get("deleted_at"):
            continue
        if rec.get("status") != "active":
            continue
        if rec.get("announcement_id") in dismissed:
            continue
        out.append({
            "announcement_id": rec.get("announcement_id"),
            "title": rec.get("title"),
            "content": rec.get("content"),
            "created_at": rec.get("created_at"),
            "start_at": rec.get("start_at"),
            "end_at": rec.get("end_at"),
        })
    return out


def dismiss_announcement(user_id: str, announcement_id: str) -> bool:
    """标记公告已关闭（持久化，不再重复弹出）。"""
    rec = get_announcement(announcement_id)
    if not rec:
        return False
    data = _read_json(_reads_path(user_id)) or {}
    dismissed = list(data.get("dismissed") or [])
    if announcement_id not in dismissed:
        dismissed.append(announcement_id)
    data["dismissed"] = dismissed
    data["updated_at"] = _now_iso()
    _write_json(_reads_path(user_id), data)
    return True


# ── 邮件图片（CID 内联）──────────────────────────────────────────────
ALLOWED_IMAGE_EXTS = ("png", "jpg", "jpeg", "webp")
MAX_IMAGE_BYTES = 2 * 1024 * 1024

_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def _ext_from_filename(filename: str) -> str | None:
    name = (filename or "").lower().strip()
    for ext in ALLOWED_IMAGE_EXTS:
        if name.endswith("." + ext):
            return ext
    return None


def _detect_magic(data: bytes, ext: str) -> bool:
    """按文件头校验真实类型，防伪装/脚本。"""
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in ("jpg", "jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _image_path(image_id: str) -> str:
    # 文件名 <image_id>.<ext>；从目录中按 id 前缀定位
    if not image_id or not image_id.replace("-", "").isalnum() or "-" not in image_id:
        return ""
    if os.path.isdir(config.EMAIL_IMAGES_DIR):
        for name in os.listdir(config.EMAIL_IMAGES_DIR):
            stem, dot, _ext = name.rpartition(".")
            if dot and stem == image_id:
                return os.path.join(config.EMAIL_IMAGES_DIR, name)
    return ""


def save_email_image(data: bytes, filename: str) -> dict:
    """校验并保存邮件图片，返回 {image_id, name, ext, size}。"""
    ext = _ext_from_filename(filename)
    if not ext:
        raise ValueError("仅支持 PNG / JPG / WebP 图片。")
    if not data:
        raise ValueError("图片内容为空。")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片不能超过 2MB。")
    if not _detect_magic(data, ext):
        raise ValueError("文件内容与扩展名不符，仅接受真实图片。")
    image_id = str(uuid.uuid4())
    path = os.path.join(config.EMAIL_IMAGES_DIR, f"{image_id}.{ext}")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    _atomic_replace(tmp, path)
    return {"image_id": image_id, "name": filename, "ext": ext, "size": len(data)}


def get_email_image_bytes(image_id: str) -> bytes | None:
    path = _image_path(image_id)
    if not path:
        return None
    with open(path, "rb") as fh:
        return fh.read()


def image_data_uri(image_id: str) -> str:
    """预览用：读图片转 data URI。"""
    path = _image_path(image_id)
    if not path:
        return ""
    ext = path.rpartition(".")[2].lower()
    mime = _EXT_MIME.get(ext, "image/png")
    with open(path, "rb") as fh:
        return "data:{0};base64,{1}".format(mime, base64.b64encode(fh.read()).decode("ascii"))


def list_email_images() -> list[dict]:
    """已上传图片列表（新 → 旧）。"""
    out = []
    if os.path.isdir(config.EMAIL_IMAGES_DIR):
        for name in os.listdir(config.EMAIL_IMAGES_DIR):
            if not name.endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            image_id, _ext = name.rsplit(".", 1)
            path = os.path.join(config.EMAIL_IMAGES_DIR, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            out.append({"image_id": image_id, "name": name, "ext": _ext, "size": size})
    out.sort(key=lambda m: m["name"], reverse=True)
    return out


def delete_email_image(image_id: str) -> bool:
    """删除上传图片（正文引用由前端确认后一并移除）。"""
    path = _image_path(image_id)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ── 邮件群发 ──────────────────────────────────────────────────────────
def resolve_recipients(target: str, user_ids) -> list[dict]:
    """收集收件人（仅含已绑定邮箱的用户）。target: all / selected。"""
    # auth 延迟导入，避免循环依赖
    import auth as _auth_mod

    if target == "selected":
        ids = (user_ids or [])[:MAX_SELECTED]
        seen = set()
        users = []
        for uid in ids:
            if not uid or uid in seen:
                continue
            seen.add(uid)
            u = _auth_mod.find_user_by_id(uid)
            if u and (u.get("email") or "").strip():
                users.append(u)
        return users

    return [
        u for u in _auth_mod.list_users(keyword="", page=1, size=10**9)
        if (u.get("email") or "").strip()
    ]


def render_preview_email(data: dict, image_src) -> str:
    """
    渲染邮件预览 HTML（纯渲染，不校验 SMTP）。

    与 send_notification_email 共用 email_templates 渲染逻辑，仅图片 src 由
    调用方回调提供（预览用 data URI，发送用 cid:）。返回 HTML 字符串。
    """
    subject = (data.get("subject") or "").strip()
    if not subject:
        raise ValueError("邮件主题不能为空。")
    if len(subject) > email_templates.MAX_SUBJECT:
        raise ValueError(f"邮件主题不能超过 {email_templates.MAX_SUBJECT} 字。")
    body = (data.get("body") or "").strip()
    if not body:
        raise ValueError("邮件正文不能为空。")
    if len(body) > email_templates.MAX_BODY:
        raise ValueError("邮件正文过长。")
    template_id = data.get("template_id") or "notice"
    template_id = template_id if template_id in email_templates.TEMPLATES else "notice"

    img_list = []
    for item in (data.get("images") or [])[:email_templates.MAX_IMAGES]:
        if not isinstance(item, dict) or not item.get("image_id"):
            continue
        iid = str(item["image_id"])
        if not _image_path(iid):
            continue
        try:
            width = max(60, min(800, int(item.get("width") or 360)))
        except (TypeError, ValueError):
            width = 360
        img_list.append({"image_id": iid, "width": width})

    items = [str(v).strip()[:email_templates.MAX_ITEM_LEN] for v in (data.get("items") or []) if str(v).strip()]
    accent = data.get("accent") if isinstance(data.get("accent"), dict) else {}
    accent = {
        "enabled": bool(accent.get("enabled")),
        "text": str(accent.get("text") or "").strip()[:email_templates.MAX_ACCENT],
    }
    button = data.get("button") if isinstance(data.get("button"), dict) else {}
    button = {
        "enabled": bool(button.get("enabled")),
        "url": str(button.get("url") or "").strip(),
        "text": str(button.get("text") or "前往工作台").strip()[:email_templates.MAX_BUTTON_TEXT],
    }

    return email_templates.render_broadcast_email(
        subject=subject,
        template_id=template_id,
        body=body,
        images=img_list,
        items=items,
        accent=accent,
        button=button,
        theme=data.get("theme"),
        image_src=image_src,
    )



def send_notification_email(
    *,
    subject: str,
    template_id: str,
    body: str,
    images=None,
    items=None,
    accent=None,
    button=None,
    theme=None,
    target: str = "all",
    user_ids=None,
    operator_id: str = "",
    operator_name: str = "",
) -> dict:
    """
    渲染并群发通知邮件，返回结果汇总（sent/skipped/failed + 日志 id）。
    SMTP 未配置时抛 RuntimeError（由路由转 503）。
    """
    if not config.smtp_configured():
        raise RuntimeError("邮件服务未配置：请在 .env 中填写 SMTP_HOST/USER/PASSWORD")

    subject = (subject or "").strip()
    if not subject:
        raise ValueError("邮件主题不能为空。")
    if len(subject) > email_templates.MAX_SUBJECT:
        raise ValueError(f"邮件主题不能超过 {email_templates.MAX_SUBJECT} 字。")
    body = (body or "").strip()
    if not body:
        raise ValueError("邮件正文不能为空。")
    if len(body) > email_templates.MAX_BODY:
        raise ValueError("邮件正文过长。")
    template_id = template_id if template_id in email_templates.TEMPLATES else "notice"

    # 图片入参规范化：校验存在 + 宽度
    img_list = []
    for item in (images or [])[:email_templates.MAX_IMAGES]:
        if not isinstance(item, dict) or not item.get("image_id"):
            continue
        iid = str(item["image_id"])
        if not _image_path(iid):
            continue
        try:
            width = max(60, min(800, int(item.get("width") or 360)))
        except (TypeError, ValueError):
            width = 360
        img_list.append({"image_id": iid, "width": width})

    # 校验并裁剪文本字段
    items = [str(v).strip()[:email_templates.MAX_ITEM_LEN] for v in (items or []) if str(v).strip()]
    if isinstance(accent, dict):
        accent = {
            "enabled": bool(accent.get("enabled")),
            "text": str(accent.get("text") or "").strip()[:email_templates.MAX_ACCENT],
        }
    else:
        accent = {"enabled": False, "text": ""}
    if isinstance(button, dict):
        button = {
            "enabled": bool(button.get("enabled")),
            "url": str(button.get("url") or "").strip(),
            "text": str(button.get("text") or "前往工作台").strip()[:email_templates.MAX_BUTTON_TEXT],
        }
    else:
        button = {"enabled": False, "url": "", "text": "前往工作台"}

    # 渲染（发送用 CID src；预览由路由传入 data URI 回调）
    def _cid_src(image_id: str, width: int) -> str:
        return f"cid:img-{image_id}"

    html = email_templates.render_broadcast_email(
        subject=subject,
        template_id=template_id,
        body=body,
        images=img_list,
        items=items,
        accent=accent,
        button=button,
        theme=theme,
        image_src=_cid_src,
    )
    plain = email_templates.render_broadcast_plain(
        subject=subject,
        template_id=template_id,
        body=body,
        items=items,
        accent=accent,
        button=button,
    )

    # 预读图片内容（只在有图时）
    image_parts = []
    for img in img_list:
        raw = get_email_image_bytes(img["image_id"])
        if not raw:
            continue
        ext = _image_path(img["image_id"]).rpartition(".")[2].lower()
        image_parts.append({
            "image_id": img["image_id"],
            "content": raw,
            "mime_subtype": "jpeg" if ext == "jpg" else ext,
        })

    recipients = resolve_recipients(target, user_ids)
    sent = skipped = failed = 0
    errors = []
    for u in recipients:
        email = (u.get("email") or "").strip()
        if not email:
            skipped += 1
            continue
        try:
            mailer.send_broadcast_email(email, subject, plain, html, image_parts)
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 逐封失败不让整体中断
            failed += 1
            errors.append({"email": email, "error": str(exc)[:200]})

    log_id = str(uuid.uuid4())
    # 快照本轮邮件的完整内容参数（历史查看用；图片仅存引用，被删图片渲染时跳过）
    compose_snapshot = {
        "subject": subject,
        "template_id": template_id,
        "body": body,
        "items": items,
        "images": img_list,
        "accent": accent,
        "button": button,
        "theme": theme if isinstance(theme, dict) else None,
        "target": target,
        "user_ids": (user_ids or [])[:MAX_SELECTED],
    }
    log_rec = {
        "email_log_id": log_id,
        "subject": subject,
        "template_id": template_id,
        "target": target,
        "target_count": len(recipients),
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "created_by_id": operator_id or "",
        "created_by_name": operator_name or "",
        "created_at": _now_iso(),
        "compose_snapshot": compose_snapshot,
    }
    _write_json(os.path.join(config.EMAIL_LOGS_DIR, f"{log_id}.json"), log_rec)

    return {
        "email_log_id": log_id,
        "subject": subject,
        "target": target,
        "target_count": len(recipients),
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "created_at": log_rec["created_at"],
    }


def list_email_logs(limit: int = 50) -> list[dict]:
    """邮件发送历史（新 → 旧）。"""
    out = []
    if os.path.isdir(config.EMAIL_LOGS_DIR):
        for name in os.listdir(config.EMAIL_LOGS_DIR):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            rec = _read_json(os.path.join(config.EMAIL_LOGS_DIR, name))
            if rec:
                out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out[: max(1, min(200, limit))]


def get_email_log_detail(log_id: str) -> dict | None:
    """历史邮件详情：日志记录 + 按快照重新渲染的 HTML（图片 data URI 内联，被删图片跳过）。"""
    if not log_id or not log_id.replace("-", "").isalnum():
        return None
    rec = _read_json(os.path.join(config.EMAIL_LOGS_DIR, f"{log_id}.json"))
    if not rec:
        return None
    snap = rec.get("compose_snapshot") or {}
    images = []
    for img in (snap.get("images") or []):
        if isinstance(img, dict) and img.get("image_id") and _image_path(str(img["image_id"])):
            images.append({"image_id": str(img["image_id"]), "width": img.get("width") or 360})
    data = {
        "subject": snap.get("subject") or rec.get("subject") or "",
        "template_id": snap.get("template_id") or rec.get("template_id") or "notice",
        "body": snap.get("body") or "",
        "items": snap.get("items") or [],
        "images": images,
        "accent": {
            "enabled": bool((snap.get("accent") or {}).get("enabled")),
            "text": (snap.get("accent") or {}).get("text") or "",
        },
        "button": {
            "enabled": bool((snap.get("button") or {}).get("enabled")),
            "url": (snap.get("button") or {}).get("url") or "",
            "text": (snap.get("button") or {}).get("text") or "",
        },
        "theme": snap.get("theme") if isinstance(snap.get("theme"), dict) else None,
    }

    def _src(image_id: str, width: int) -> str:
        return image_data_uri(image_id)

    try:
        html = render_preview_email(data, image_src=_src)
    except ValueError:
        html = ""
    return {"record": rec, "html": html}
