"""
群发邮件模板渲染（清爽版排版，功能设计定稿 §8）。

设计要点：
- 排版基准 = email-notify-mail.html 清爽版：品牌头 → 正文段落 → 更新列表（模板自带）
  → 图片（位于更新列表下方）→ 蓝色提示块（可选）→ 前往工作台按钮（可选）→ 落款 → 页脚。
- 零第三方依赖：string 拼接 + html.escape，杜绝 XSS / 邮件注入。
- 预览与真实发送共用 render_broadcast_email()，仅图片 src 由调用方回调决定
  （发送用 cid:img-<id>，预览用 data URI），保证"预览即实物"。
- 主题 = 一组 hex 参数，后端正则校验，非法值回退品牌深蓝默认。
"""
import html
import re

# ── 主题参数（全部 hex，校验 ^#[0-9a-fA-F]{6}$）──────────────────────────
THEME_KEYS = (
    "header_bg",      # 品牌头底色
    "header_accent",  # 品牌头副标色
    "accent_bg",      # 蓝色提示块底
    "accent_border",  # 提示块边框
    "accent_text",    # 提示块文字
    "button_bg",      # CTA 按钮底色
    "body_bg",        # 邮件页面底色
)

DEFAULT_THEME = {
    "header_bg": "#17243b",
    "header_accent": "#8fa3c7",
    "accent_bg": "#eef2fb",
    "accent_border": "#b9cbf2",
    "accent_text": "#253249",
    "button_bg": "#263a5e",
    "body_bg": "#f3f6fa",
}

# 预设主题（前端色板与之一致；"brand" 为品牌深蓝默认）
PRESET_THEMES = {
    "brand": DEFAULT_THEME,
    "green": {
        "header_bg": "#1e3a2f",
        "header_accent": "#9fc3b2",
        "accent_bg": "#e6f7ee",
        "accent_border": "#bfe3d0",
        "accent_text": "#2f6b4a",
        "button_bg": "#2e5c49",
        "body_bg": "#f3f6fa",
    },
    "purple": {
        "header_bg": "#3b2a4d",
        "header_accent": "#b7a4cc",
        "accent_bg": "#f0eaf7",
        "accent_border": "#d2c2e6",
        "accent_text": "#4a3560",
        "button_bg": "#5a4080",
        "body_bg": "#f3f6fa",
    },
    "graphite": {
        "header_bg": "#2f3644",
        "header_accent": "#9aa3b3",
        "accent_bg": "#eef1f6",
        "accent_border": "#c3cad6",
        "accent_text": "#39424f",
        "button_bg": "#3d4757",
        "body_bg": "#f3f6fa",
    },
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# ── 模板注册表 ─────────────────────────────────────────────────────────
# has_items=True 的模板，正文段落之后渲染"更新列表"区块（融合要点列表的排版）
TEMPLATES = {
    "system_update": {
        "id": "system_update",
        "name": "系统更新",
        "desc": "版本更新排版",
        "has_items": True,
        "list_title": "本次更新",
        "signoff": "祝工作顺利",
    },
    "notice": {
        "id": "notice",
        "name": "通知公告",
        "desc": "段落式通知",
        "has_items": False,
        "list_title": "",
        "signoff": "祝工作顺利",
    },
    "feature_launch": {
        "id": "feature_launch",
        "name": "新功能上线",
        "desc": "亮点式排版",
        "has_items": True,
        "list_title": "新功能一览",
        "signoff": "祝工作顺利",
    },
    "maintenance": {
        "id": "maintenance",
        "name": "维护通知",
        "desc": "时间+影响排版",
        "has_items": False,
        "list_title": "",
        "signoff": "感谢理解与配合",
    },
}

# 文本长度上限（服务端兜底）
MAX_SUBJECT = 200
MAX_BODY = 20000
MAX_ITEMS = 20
MAX_IMAGES = 3
MAX_ITEM_LEN = 300
MAX_ACCENT = 500
MAX_BUTTON_URL = 500
MAX_BUTTON_TEXT = 30


def sanitize_theme(theme) -> dict:
    """校验并补齐主题参数；非法 hex 或缺失键回退默认。"""
    out = dict(DEFAULT_THEME)
    if not isinstance(theme, dict):
        return out
    for key in THEME_KEYS:
        val = theme.get(key)
        if isinstance(val, str) and _HEX_RE.match(val.strip()):
            out[key] = val.strip().lower()
    return out


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _paragraphs(body: str) -> list[str]:
    """按空行分段；段内换行转 <br>。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", str(body or "")) if b.strip()]
    return [html.escape(b, quote=True).replace("\n", "<br>") for b in blocks]

def render_broadcast_email(
    *,
    subject: str,
    template_id: str,
    body: str,
    images=None,
    items=None,
    accent=None,
    button=None,
    theme=None,
    image_src=None,
) -> str:
    """
    渲染清爽版邮件 HTML。

    images: [{image_id, width}]，渲染在更新列表下方；width 强制 int 60..800。
    image_src(image_id, width) -> str：发送时返回 cid:img-<id>，预览时返回 data URI。
    accent: {"enabled": bool, "text": str}；button: {"enabled": bool, "url": str, "text": str}
    """
    t = sanitize_theme(theme)
    tmpl = TEMPLATES.get(template_id) or TEMPLATES["notice"]
    image_src = image_src or (lambda image_id, width: f"cid:img-{image_id}")

    # ── 正文段落（管理员纯文本，空行分段，全量转义）────────────────
    parts_html = []
    for p in _paragraphs(body):
        parts_html.append(
            f'<div style="color:#435168;font-size:14px;line-height:1.85;margin-top:12px;">{p}</div>'
        )

    # ── 更新列表（模板自带，隶属正文模块；图片渲染在其下方）────────
    if tmpl["has_items"]:
        item_list = [str(v).strip() for v in (items or []) if str(v).strip()]
        if item_list:
            rows = []
            for it in item_list[:MAX_ITEMS]:
                rows.append(
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">'
                    '<tr>'
                    f'<td width="20" valign="top" style="padding-top:4px;">'
                    '<div style="width:15px;height:15px;border-radius:4px;background-color:#1d7f5c;color:#ffffff;'
                    'font-size:10px;font-weight:700;text-align:center;line-height:15px;">&#10003;</div></td>'
                    f'<td style="color:#435168;font-size:13.5px;line-height:1.9;">{_esc(it)}</td>'
                    "</tr>"
                    "</table>"
                )
            list_html = (
                f'<div style="color:#253249;font-size:14px;font-weight:600;margin:18px 0 4px;">'
                f'&#128195; {_esc(tmpl["list_title"])}</div>'
                + "".join(rows)
            )
            parts_html.append(list_html)

    # ── 图片（更新列表之后；CID 内联 / 预览 data URI）───────────────
    img_list = []
    if isinstance(images, list):
        for img in images[:MAX_IMAGES]:
            if not isinstance(img, dict) or not img.get("image_id"):
                continue
            try:
                width = max(60, min(800, int(img.get("width") or 360)))
            except (TypeError, ValueError):
                width = 360
            img_list.append((str(img["image_id"]), width))
    for image_id, width in img_list:
        src = image_src(image_id, width)
        parts_html.append(
            f'<div style="margin:16px 0 2px;">'
            f'<img src="{_esc(src)}" alt="" width="{width}" '
            f'style="width:{width}px;height:auto;max-width:100%;border-radius:6px;display:block;">'
            f"</div>"
        )

    # ── 蓝色提示块（可选）──────────────────────────────────────────
    accent_text = ""
    if isinstance(accent, dict) and accent.get("enabled"):
        accent_text = str(accent.get("text") or "").strip()
    if accent_text:
        parts_html.append(
            f'<div style="background-color:{t["accent_bg"]};border:1px dashed {t["accent_border"]};'
            f'border-radius:10px;padding:14px 17px;margin:20px 0 2px;color:{t["accent_text"]};'
            f'font-size:13.5px;line-height:1.8;">{_esc(accent_text)}</div>'
        )

    # ── 前往工作台按钮（可选）──────────────────────────────────────
    button_url = ""
    if isinstance(button, dict) and button.get("enabled"):
        url = str(button.get("url") or "").strip()
        if _valid_button_url(url):
            button_url = url
    if button_url:
        label = _esc(str(button.get("text") or "前往工作台").strip() or "前往工作台")
        parts_html.append(
            f'<div style="margin:18px 0 2px;"><a href="{_esc(button_url)}" '
            f'style="display:inline-block;background-color:{t["button_bg"]};color:#ffffff;'
            f'font-size:13.5px;font-weight:600;text-decoration:none;border-radius:6px;'
            f'padding:10px 22px;">{label} &#8250;</a></div>'
        )

    # ── 落款 + 页脚 ────────────────────────────────────────────────
    signoff = (
        f'<div style="margin-top:24px;color:#6d7b91;font-size:13px;line-height:1.8;">'
        f'{_esc(tmpl["signoff"])}<br>'
        f'<b style="color:#253249;">AI 简历智选 团队</b></div>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{t['body_bg']};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{t['body_bg']};padding:28px 0;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;border-collapse:collapse;">
        <tr><td style="background-color:{t['header_bg']};border-radius:12px 12px 0 0;padding:24px 32px;">
          <div style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">AI 简历智选</div>
          <div style="color:{t['header_accent']};font-size:12px;margin-top:5px;">智能简历筛选工作台</div>
        </td></tr>
        <tr><td style="background-color:#ffffff;border-radius:0 0 12px 12px;padding:30px 32px 24px;">
          {''.join(parts_html)}
          {signoff}
        </td></tr>
        <tr><td style="padding:14px 32px 0;text-align:center;">
          <div style="color:#9aa5b5;font-size:11px;line-height:1.8;">此邮件由系统自动发送，请勿直接回复。</div>
          <div style="color:#9aa5b5;font-size:11px;">&copy; AI 简历智选 · 智能简历筛选工作台</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _valid_button_url(url: str) -> bool:
    """按钮 URL 白名单：http(s) 绝对地址或站内相对路径。"""
    if url.startswith(("http://", "https://")):
        return len(url) <= MAX_BUTTON_URL
    if url.startswith("/") or url.startswith("./") or url.startswith("../"):
        return len(url) <= MAX_BUTTON_URL
    return False


def render_broadcast_plain(
    *,
    subject: str,
    template_id: str,
    body: str,
    items=None,
    accent=None,
    button=None,
) -> str:
    """同步生成纯文本版本（兼容旧客户端 / 降低垃圾邮件误判）。"""
    tmpl = TEMPLATES.get(template_id) or TEMPLATES["notice"]
    lines = [str(subject or ""), ""]
    lines.append(str(body or "").strip())
    lines.append("")
    if tmpl["has_items"]:
        item_list = [str(v).strip() for v in (items or []) if str(v).strip()]
        if item_list:
            lines.append(f"—— {tmpl['list_title']} ——")
            lines.extend(f"- {it}" for it in item_list[:MAX_ITEMS])
            lines.append("")
    if isinstance(accent, dict) and accent.get("enabled"):
        text = str(accent.get("text") or "").strip()
        if text:
            lines.append(f"提示：{text}")
            lines.append("")
    if isinstance(button, dict) and button.get("enabled"):
        url = str(button.get("url") or "").strip()
        if _valid_button_url(url):
            lines.append(f"前往工作台：{url}")
            lines.append("")
    lines.append(tmpl["signoff"])
    lines.append("AI 简历智选 团队")
    lines.append("")
    lines.append("此邮件由系统自动发送，请勿直接回复。")
    return "\n".join(lines)
