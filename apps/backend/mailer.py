"""
SMTP 邮件发送（注册邮箱验证码 + 忘记密码重置）。

注意：本模块名用 mailer（不能用 email），因为 Python 标准库有同名 email 包，
import email 会遮蔽标准库导致 smtplib/urllib 崩溃。

无第三方依赖：Python 标准库 smtplib + email.message。
未配置 SMTP 时 smtp_available() 返回 False，上层接口返回 503，避免假死。

邮件格式：multipart/alternative —— 同时携带 HTML（品牌排版 + 验证码大字卡片）
与纯文本（兼容旧客户端、降低垃圾邮件误判概率），客户端按能力自动选择。
HTML 全部使用内联样式 + table 布局，兼容网易/QQ/Gmail 等主流邮箱。
"""
import logging
import smtplib
import time
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import config
import system_config

logger = logging.getLogger(__name__)

# 品牌色
_NAVY = "#17243b"
_NAVY_SOFT = "#263a5e"
_TEXT = "#253249"
_MUTED = "#6d7b91"
_BG = "#f3f6fa"
_BLUE = "#466fd0"
_BLUE_SOFT = "#eef2fb"
_BLUE_BORDER = "#b9cbf2"
_FOOTER = "#9aa5b5"


def smtp_available(slot: str = system_config.SLOT_TRANSACTIONAL) -> bool:
    """是否已配置可用的发件服务（界面配置优先，其次读 .env）。"""
    return bool(system_config.resolve_mail(slot).get("ready"))


def _mail_config(slot: str = system_config.SLOT_TRANSACTIONAL) -> dict:
    """取最终生效的发件配置；未就绪时抛出指向界面的明确错误。"""
    cfg = system_config.resolve_mail(slot)
    if not cfg.get("ready"):
        raise RuntimeError(
            "邮件服务未配置：请在「账号管理 → 邮件服务」中填写发件邮箱；"
            "服务器 .env 里的 SMTP_HOST/SMTP_USER/SMTP_PASSWORD 仍是默认配置。"
        )
    return cfg


def _connect(cfg: dict):
    """按配置建立并登录 SMTP 连接（ssl / starttls / plain）。"""
    host = cfg.get("host") or ""
    port = int(cfg.get("port") or 465)
    mode = (cfg.get("security") or "ssl").lower()

    if mode == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    elif mode == "starttls":
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
    else:
        server = smtplib.SMTP(host, port, timeout=15)

    try:
        server.login(cfg.get("username") or "", cfg.get("password") or "")
    except Exception:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - 关闭失败不影响报错
            pass
        raise
    return server


def send_password_reset_email(to_email: str, username: str, code: str) -> bool:
    """
    发送密码重置邮件，正文内含一次性验证码（6 位）。
    成功返回 True，失败抛异常（由调用方决定如何响应）。
    """
    _mail_config()  # 未配置时在此抛出明确错误

    ttl_minutes = max(1, config.RESET_TOKEN_TTL_SECONDS // 60)
    subject = "【AI 简历智选】密码重置验证码"

    plain = f"""您好，{username}：

您正在重置密码。以下是本次重置用的一次性验证码：

  验证码：{code}

验证码仅可使用一次，{ttl_minutes} 分钟内有效。
如非本人操作，请忽略本邮件，并请不要将验证码告知他人。

—— AI 简历智选（系统自动发送，请勿回复）
"""

    html = _build_html_email(
        subject="密码重置验证码",
        greeting=f"您好，{username}，",
        description="您正在重置密码。请输入下方验证码完成验证：",
        code=code,
        ttl_minutes=ttl_minutes,
    )
    _send(to_email, subject, plain, html)
    return True


def send_verification_email(to_email: str, code: str, purpose: str = "注册") -> bool:
    """
    发送通用邮箱验证码邮件（注册绑定时用）。
    成功返回 True，失败抛异常（由调用方决定如何响应）。
    """
    _mail_config()  # 未配置时在此抛出明确错误

    ttl_minutes = max(1, config.RESET_TOKEN_TTL_SECONDS // 60)
    subject = f"【AI 简历智选】{purpose}邮箱验证码"

    plain = f"""您好：

您正在使用此邮箱进行{purpose}。以下是一次性邮箱验证码：

  验证码：{code}

验证码仅可使用一次，{ttl_minutes} 分钟内有效。
如非本人操作，请忽略本邮件，并请不要将验证码告知他人。

—— AI 简历智选（系统自动发送，请勿回复）
"""

    html = _build_html_email(
        subject=f"{purpose}邮箱验证",
        greeting="您好，",
        description=f"您正在使用此邮箱进行{purpose}。请输入下方验证码完成验证：",
        code=code,
        ttl_minutes=ttl_minutes,
    )
    _send(to_email, subject, plain, html)
    return True


def send_invite_code_email(to_email: str, code: str, expires_hours: int = 24) -> bool:
    """
    发送邀请码邮件（管理员审批通过后，自动发到申请人邮箱）。
    邀请码为短码（默认 4 位），不区分大小写；一次性、限时有效。
    """
    _mail_config()  # 未配置时在此抛出明确错误

    subject = "【AI 简历智选】您的注册邀请码"
    plain = f"""您好：

您的账号申请已通过审批。请使用以下邀请码完成注册：

  邀请码：{code}

使用说明：
· 邀请码仅可使用一次，{expires_hours} 小时内有效（过期需重新申请）；
· 仅限在申请时填写的邮箱注册使用，请勿转借他人；
· 如非本人申请，请忽略本邮件。

—— AI 简历智选（系统自动发送，请勿回复）
"""
    html = _build_invite_email_html(
        code=code,
        expires_hours=expires_hours,
        description="您的账号申请已通过审批，请使用下方邀请码完成注册：",
    )
    _send(to_email, subject, plain, html)
    return True


def send_invite_rejection_email(to_email: str, reason: str) -> bool:
    """
    发送申请被拒通知邮件（管理员填写拒绝理由时触发）。
    """
    _mail_config()  # 未配置时在此抛出明确错误

    subject = "【AI 简历智选】账号申请未通过"
    reason_text = str(reason or "").strip() or "暂未说明具体原因，如有疑问可联系管理员。"
    plain = f"""您好：

很抱歉，您的账号申请未通过审批。

原因：{reason_text}

如需进一步沟通，请回复本邮件或联系管理员。

—— AI 简历智选（系统自动发送，请勿回复）
"""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{_BG};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_BG};padding:28px 0;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;border-collapse:collapse;">
        <tr><td style="background-color:{_NAVY};border-radius:12px 12px 0 0;padding:26px 32px;">
          <div style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">AI 简历智选</div>
          <div style="color:#8fa3c7;font-size:12px;margin-top:5px;">智能简历筛选工作台</div>
        </td></tr>
        <tr><td style="background-color:#ffffff;border-radius:0 0 12px 12px;padding:34px 32px 26px;">
          <div style="color:{_TEXT};font-size:15px;font-weight:600;">您好：</div>
          <div style="color:{_MUTED};font-size:14px;line-height:1.7;margin-top:10px;">很抱歉，您的账号申请未通过审批。</div>
          <div style="background-color:#fff4f2;border:1px solid #f1c4bd;border-radius:10px;padding:16px;margin:18px 0;color:{_TEXT};font-size:14px;line-height:1.7;">{reason_text}</div>
          <div style="color:{_MUTED};font-size:13px;line-height:1.8;">如需进一步沟通，请回复本邮件或联系管理员。</div>
        </td></tr>
        <tr><td style="padding:14px 32px 0;text-align:center;">
          <div style="color:{_FOOTER};font-size:11px;line-height:1.8;">此邮件由系统自动发送，请勿直接回复。</div>
          <div style="color:{_FOOTER};font-size:11px;">© AI 简历智选 · 智能简历筛选工作台</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    _send(to_email, subject, plain, html)
    return True


def _build_invite_email_html(code: str, expires_hours: int, description: str) -> str:
    """邀请码邮件的 HTML 正文（大字卡片 + 使用说明）。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{_BG};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_BG};padding:28px 0;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;border-collapse:collapse;">
        <tr><td style="background-color:{_NAVY};border-radius:12px 12px 0 0;padding:26px 32px;">
          <div style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">AI 简历智选</div>
          <div style="color:#8fa3c7;font-size:12px;margin-top:5px;">智能简历筛选工作台</div>
        </td></tr>
        <tr><td style="background-color:#ffffff;border-radius:0 0 12px 12px;padding:34px 32px 26px;">
          <div style="color:{_TEXT};font-size:15px;font-weight:600;">您好：</div>
          <div style="color:{_MUTED};font-size:14px;line-height:1.7;margin-top:10px;">{description}</div>
          <div style="background-color:{_BLUE_SOFT};border:1px dashed {_BLUE_BORDER};border-radius:10px;padding:18px;text-align:center;margin:22px 0;">
            <div style="font-size:12px;color:{_MUTED};letter-spacing:1px;margin-bottom:8px;">注册邀请码</div>
            <div style="font-size:36px;font-weight:700;letter-spacing:14px;color:{_NAVY};font-family:'Courier New',Consolas,monospace;padding-left:14px;">{code}</div>
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 4px;">
            <tr><td style="color:{_MUTED};font-size:13px;line-height:1.8;">
              · 邀请码<b style="color:{_TEXT}">仅可使用一次</b>，请在 <b style="color:{_TEXT}">{expires_hours} 小时</b>内完成注册<br>
              · 仅限申请时填写的邮箱使用，请勿转借他人<br>
              · 如非本人申请，请忽略本邮件
            </td></tr>
          </table>
        </td></tr>
        <tr><td style="padding:14px 32px 0;text-align:center;">
          <div style="color:{_FOOTER};font-size:11px;line-height:1.8;">此邮件由系统自动发送，请勿直接回复。</div>
          <div style="color:{_FOOTER};font-size:11px;">© AI 简历智选 · 智能简历筛选工作台</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_html_email(subject: str, greeting: str, description: str, code: str, ttl_minutes: int) -> str:
    """构建验证码邮件的 HTML 正文（table 布局 + 内联样式，兼容主流邮箱）。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{_BG};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_BG};padding:28px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;border-collapse:collapse;">
          <!-- 品牌头 -->
          <tr>
            <td style="background-color:{_NAVY};border-radius:12px 12px 0 0;padding:26px 32px;">
              <div style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">AI 简历智选</div>
              <div style="color:#8fa3c7;font-size:12px;margin-top:5px;">智能简历筛选工作台</div>
            </td>
          </tr>
          <!-- 正文卡片 -->
          <tr>
            <td style="background-color:#ffffff;border-radius:0 0 12px 12px;padding:34px 32px 26px;">
              <div style="color:{_TEXT};font-size:15px;font-weight:600;">{greeting}</div>
              <div style="color:{_MUTED};font-size:14px;line-height:1.7;margin-top:10px;">{description}</div>

              <!-- 验证码大字卡片 -->
              <div style="background-color:{_BLUE_SOFT};border:1px dashed {_BLUE_BORDER};border-radius:10px;padding:18px;text-align:center;margin:22px 0;">
                <div style="font-size:12px;color:{_MUTED};letter-spacing:1px;margin-bottom:8px;">{subject}</div>
                <div style="font-size:32px;font-weight:700;letter-spacing:10px;color:{_NAVY};font-family:'Courier New',Consolas,monospace;padding-left:10px;">{code}</div>
              </div>

              <!-- 使用说明 -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 4px;">
                <tr>
                  <td style="color:{_MUTED};font-size:13px;line-height:1.8;">
                    · 验证码<b style="color:{_TEXT}">仅可使用一次</b>，请在 <b style="color:{_TEXT}">{ttl_minutes} 分钟</b>内完成操作<br>
                    · 如非本人操作，请忽略本邮件<br>
                    · 请勿将验证码告知他人，谨防诈骗
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- 页脚 -->
          <tr>
            <td style="padding:14px 32px 0;text-align:center;">
              <div style="color:{_FOOTER};font-size:11px;line-height:1.8;">此邮件由系统自动发送，请勿直接回复。</div>
              <div style="color:{_FOOTER};font-size:11px;">© AI 简历智选 · 智能简历筛选工作台</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def test_smtp(slot: str = system_config.SLOT_TRANSACTIONAL, to_email: str = "") -> dict:
    """
    测试发件配置：连接 + 登录；给了 to_email 则同时发一封测试邮件。

    返回结构化结果供管理界面展示；连接或认证失败直接抛异常（接口层转 422）。
    """
    cfg = system_config.resolve_mail(slot)
    if not cfg.get("ready"):
        raise RuntimeError("当前没有可用的发件配置：请先填写 SMTP 服务器、用户名与密码/授权码。")

    sender = cfg.get("from_address") or cfg.get("username") or ""
    started = time.time()
    server = _connect(cfg)
    sent = False
    try:
        if to_email:
            plain = (
                "这是一封来自「AI 简历智选」的发件配置测试邮件。\n\n"
                f"发件邮箱：{sender}\n"
                f"服务器：{cfg.get('host')}:{cfg.get('port')}（{cfg.get('security')}）\n\n"
                "收到本邮件说明邮件服务配置可用，无需回复。"
            )
            html = (
                '<div style="font:14px/1.7 -apple-system,Segoe UI,Microsoft YaHei,sans-serif;color:#253249">'
                "<p>这是一封来自 <b>AI 简历智选</b> 的发件配置测试邮件。</p>"
                f"<p>发件邮箱：<b>{sender}</b><br>"
                f"服务器：{cfg.get('host')}:{cfg.get('port')}（{cfg.get('security')}）</p>"
                '<p style="color:#6d7b91">收到本邮件说明邮件服务配置可用，无需回复。</p></div>'
            )
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header("【AI 简历智选】发件配置测试", "utf-8")
            msg["From"] = formataddr((str(Header(cfg.get("from_name") or "AI 简历智选", "utf-8")), sender))
            msg["To"] = to_email
            msg.attach(MIMEText(plain, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            server.sendmail(sender, [to_email], msg.as_string())
            sent = True
    finally:
        server.quit()

    return {
        "slot": slot,
        "source": cfg.get("source"),
        "host": cfg.get("host"),
        "port": cfg.get("port"),
        "security": cfg.get("security"),
        "from_address": sender,
        "test_email_sent": sent,
        "seconds": round(time.time() - started, 2),
        "message": "连接与登录成功" + ("，测试邮件已发送" if sent else ""),
    }


def _send(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    slot: str = system_config.SLOT_TRANSACTIONAL,
) -> None:
    """发送 multipart/alternative 邮件（纯文本 + HTML）。"""
    cfg = _mail_config(slot)
    sender = cfg.get("from_address") or cfg.get("username") or ""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(cfg.get("from_name") or "AI 简历智选", "utf-8")), sender))
    msg["To"] = to_email
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        server = _connect(cfg)
        try:
            server.sendmail(sender, [to_email], msg.as_string())
            logger.info("email sent to %s (subject=%s)", to_email, subject)
        finally:
            server.quit()
    except Exception:
        logger.exception("send email failed for %s", to_email)
        raise



def send_broadcast_email(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    images=None,
) -> None:
    """
    发送群发通知邮件（支持 CID 内联图片）。

    images: [{image_id, content(bytes), mime_subtype}] —— 邮件 HTML 中以
    <img src="cid:img-<image_id>"> 引用，此处附加同名 Content-ID 的 MIMEImage，
    不依赖公网图床，QQ/网易/Outlook 等主流客户端均可显示。
    结构：multipart/related → multipart/alternative(纯文本+HTML) + 内联图片。
    """
    cfg = _mail_config(system_config.SLOT_NOTIFICATION)
    sender = cfg.get("from_address") or cfg.get("username") or ""

    outer = MIMEMultipart("related")
    outer["Subject"] = Header(subject, "utf-8")
    outer["From"] = formataddr((str(Header(cfg.get("from_name") or "AI 简历智选", "utf-8")), sender))
    outer["To"] = to_email

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    outer.attach(alt)

    for img in (images or []):
        cid = "img-{0}".format(str(img.get("image_id") or ""))
        if cid == "img-":
            continue
        payload = img.get("content")
        if not payload:
            continue
        subtype = str(img.get("mime_subtype") or "png").lower()
        part = MIMEImage(payload, _subtype=subtype)
        part.add_header("Content-ID", "<{0}>".format(cid))
        part.add_header("Content-Disposition", "inline", filename="{0}.{1}".format(cid, subtype))
        outer.attach(part)

    try:
        server = _connect(cfg)
        try:
            server.sendmail(sender, [to_email], outer.as_string())
            logger.info("broadcast email sent to %s (subject=%s)", to_email, subject)
        finally:
            server.quit()
    except Exception:
        logger.exception("broadcast email failed for %s", to_email)
        raise
