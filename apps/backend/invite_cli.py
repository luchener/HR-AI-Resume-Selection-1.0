"""
管理员命令行工具：邀请码（申请单审批 / 手动发码 / 补发 / 作废）。

用法（服务器上，项目 apps/backend 目录）：
    python invite_cli.py requests list [--status pending]
    python invite_cli.py requests approve <request_id>       # 通过并尝试发码邮件
    python invite_cli.py requests reject <request_id> [--reason "…"]
    python invite_cli.py gen [--count N] [--email x@y.com] [--note "…"] [--hours 24]
    python invite_cli.py codes list [--status active|used|expired|all]
    python invite_cli.py codes revoke <CODE>
    python invite_cli.py resend <request_id>

说明：
- 与网页后台共用同一套 auth.py 函数，可作为网页故障/服务器离线时的兜底。
- gen 生成时明文只在终端打印一次（服务器不落盘明文）。
"""
import argparse
import sys

import auth
import config  # noqa: F401  确保 config 先加载 .env 与目录
import mailer


def _print_request(rec: dict) -> None:
    print(
        f"  {rec.get('request_id')}  {rec.get('status'):9s}  "
        f"email={rec.get('email')}  created={rec.get('created_at')}"
    )
    if rec.get("note"):
        print(f"    note: {rec.get('note')}")
    if rec.get("reject_reason"):
        print(f"    reject_reason: {rec.get('reject_reason')}")


def cmd_requests_list(args) -> int:
    status = args.status
    records = auth.list_invite_requests(status)
    if not records:
        print(f"[info] 没有 {status} 状态的申请单")
        return 0
    print(f"== {status} 申请单（{len(records)}）==")
    for rec in records:
        _print_request(rec)
    return 0


def cmd_requests_approve(args) -> int:
    req, code, status = auth.approve_invite_request(args.request_id, "cli")
    if status != 200:
        print(f"[错误] {req}", file=sys.stderr)
        return 1
    email = (req.get("email") or "").strip()
    print(f"[info] 已通过审批：email={email}")
    if mailer.smtp_available():
        try:
            mailer.send_invite_code_email(email, code, expires_hours=config.INVITE_CODE_TTL_HOURS)
            auth.mark_invite_request_sent(args.request_id)
            print(f"[info] 邀请码已发送至 {email}")
        except Exception as exc:
            print(f"[错误] 审批已通过，但邮件发送失败：{exc}", file=sys.stderr)
            print(f"[info] 邀请码（请通过可信渠道转达）：{code}")
            return 2
    else:
        print(f"[警告] SMTP 未配置，无法发邮件。邀请码（请手动转达）：{code}", file=sys.stderr)
    return 0


def cmd_requests_reject(args) -> int:
    reason = args.reason or ""
    req, error, status = auth.reject_invite_request(args.request_id, "cli", reason)
    if status != 200:
        print(f"[错误] {error}", file=sys.stderr)
        return 1
    email = (req.get("email") or "").strip()
    print(f"[info] 已拒绝：email={email}")
    if reason and mailer.smtp_available():
        try:
            mailer.send_invite_rejection_email(email, reason)
            print(f"[info] 拒信邮件已发送至 {email}")
        except Exception as exc:
            print(f"[警告] 拒信发送失败：{exc}", file=sys.stderr)
    return 0


def cmd_gen(args) -> int:
    import uuid

    count = max(1, min(args.count, 20))
    email = (args.email or "").strip().lower() or ""
    hours = args.hours or config.INVITE_CODE_TTL_HOURS
    codes = []
    for _i in range(count):
        code = auth.generate_invite_code(
            bound_email=email,
            request_id=f"manual-{uuid.uuid4()}",
            created_by="cli",
            note=args.note or "",
            ttl_hours=hours,
        )
        codes.append(code)
    print("=" * 44)
    for code in codes:
        print(f"  邀请码: {code}")
    print("=" * 44)
    if email:
        print(f"· 绑定邮箱: {email}")
    else:
        print("· 未绑定邮箱（任何邮箱可用，注意仅应急使用）")
    print(f"· 有效期: {hours} 小时 | 一次性")
    print("· 明文仅本次显示，服务器不保存明文")
    return 0


def cmd_codes_list(args) -> int:
    status = args.status
    records = auth.list_invite_codes()
    now_ts = auth._now_ts()
    shown = 0
    for rec in records:
        used = bool(rec.get("used"))
        expired = (not used) and float(rec.get("expires_at", 0)) < now_ts
        rec_status = "used" if used else ("expired" if expired else "active")
        if status != "all" and rec_status != status:
            continue
        shown += 1
        print(
            f"  {rec_status:7s}  hash={str(rec.get('code_hash'))[:12]}  "
            f"email={rec.get('bound_email') or '(未绑定)'}  "
            f"used_by={rec.get('used_by_username') or '-'}  "
            f"expires={rec.get('expires_at')}"
        )
    print(f"== 共 {shown} 条（status={status}）==")
    return 0


def cmd_codes_revoke(args) -> int:
    if auth.revoke_invite_code(args.code):
        print(f"[info] 邀请码 {args.code} 已作废")
        return 0
    print(f"[错误] 邀请码 {args.code} 不存在/已使用/已作废", file=sys.stderr)
    return 1


def cmd_resend(args) -> int:
    req, code, status = auth.resend_invite_request(args.request_id)
    if status != 200:
        print(f"[错误] {req}", file=sys.stderr)
        return 1
    email = (req.get("email") or "").strip()
    if not mailer.smtp_available():
        print("[错误] SMTP 未配置，无法补发邮件", file=sys.stderr)
        return 1
    try:
        mailer.send_invite_code_email(email, code, expires_hours=config.INVITE_CODE_TTL_HOURS)
        auth.mark_invite_request_sent(args.request_id)
        print(f"[info] 新邀请码已发送至 {email}（旧码已作废）")
    except Exception as exc:
        print(f"[错误] 补发失败：{exc}", file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="管理员邀请码命令行工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("requests", help="申请单管理")
    p.add_argument("action", choices=["list", "approve", "reject"])
    p.add_argument("request_id", nargs="?", help="approve/reject 需要")
    p.add_argument("--status", default="pending", help="list 的状态过滤")
    p.add_argument("--reason", default="", help="reject 的拒绝理由")

    p = sub.add_parser("gen", help="手动生成邀请码")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--email", default="", help="绑定邮箱（留空则不绑定，仅应急）")
    p.add_argument("--note", default="")
    p.add_argument("--hours", type=int, default=0, help="有效期小时数（默认读配置 24）")

    p = sub.add_parser("codes", help="邀请码总览")
    p.add_argument("action", choices=["list", "revoke"])
    p.add_argument("code", nargs="?")
    p.add_argument("--status", default="all")

    p = sub.add_parser("resend", help="补发邀请码邮件")
    p.add_argument("request_id")

    args = ap.parse_args()

    if args.cmd == "requests":
        if args.action == "list":
            return cmd_requests_list(args)
        if args.action == "approve":
            if not args.request_id:
                print("[错误] approve 需要 request_id", file=sys.stderr)
                return 1
            return cmd_requests_approve(args)
        if args.action == "reject":
            if not args.request_id:
                print("[错误] reject 需要 request_id", file=sys.stderr)
                return 1
            return cmd_requests_reject(args)
    elif args.cmd == "gen":
        return cmd_gen(args)
    elif args.cmd == "codes":
        if args.action == "list":
            return cmd_codes_list(args)
        if args.action == "revoke":
            if not args.code:
                print("[错误] revoke 需要 CODE", file=sys.stderr)
                return 1
            return cmd_codes_revoke(args)
    elif args.cmd == "resend":
        return cmd_resend(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
