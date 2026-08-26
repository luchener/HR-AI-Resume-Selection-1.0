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
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import config

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


def smtp_available() -> bool:
    """是否已配置可用的 SMTP 服务。"""
    return config.smtp_configured()


def send_password_reset_email(to_email: str, username: str, code: str) -> bool:
    """
    发送密码重置邮件，正文内含一次性验证码（6 位）。
    成功返回 True，失败抛异常（由调用方决定如何响应）。
    """
    if not config.smtp_configured():
        raise RuntimeError("邮件服务未配置：请在 .env 中填写 SMTP_HOST/USER/PASSWORD")

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
    if not config.smtp_configured():
        raise RuntimeError("邮件服务未配置：请在 .env 中填写 SMTP_HOST/USER/PASSWORD")

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


def _send(to_email: str, subject: str, plain_body: str, html_body: str) -> None:
    """发送 multipart/alternative 邮件（纯文本 + HTML）。"""
    if not config.smtp_configured():
        raise RuntimeError("邮件服务未配置：请在 .env 中填写 SMTP_HOST/USER/PASSWORD")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("AI 简历智选", "utf-8")), config.SMTP_FROM))
    msg["To"] = to_email
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
            server.starttls()
        try:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM, [to_email], msg.as_string())
            logger.info("email sent to %s (subject=%s)", to_email, subject)
        finally:
            server.quit()
    except Exception:
        logger.exception("send email failed for %s", to_email)
        raise