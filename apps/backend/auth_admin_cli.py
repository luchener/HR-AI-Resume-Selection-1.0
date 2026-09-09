"""
管理员命令行工具：登录防爆破 —— 冻结账号查看 / 解冻。

用法（服务器上，项目 apps/backend 目录）：
    python auth_admin_cli.py frozen list
    python auth_admin_cli.py unfreeze <username>

说明：
- 冻结账号默认可由用户本人"正确密码 + 邮箱验证码"自助解冻（见 /api/v1/auth/unfreeze）。
- 本 CLI 用于兜底场景：账号未绑定邮箱 / 自助解冻不可用 / 用户联系管理员处理。
"""
import argparse
import sys

import auth
import config  # noqa: F401  确保 config 先加载 .env 与目录


def cmd_frozen_list(_args) -> int:
    items = auth.list_frozen_users()
    if not items:
        print("[info] 当前没有冻结账号")
        return 0
    print(f"== 冻结账号（{len(items)}）==")
    for item in items:
        print(
            f"  {item.get('username')}  frozen_at={item.get('frozen_at')}  "
            f"consecutive={item.get('consecutive_failures')}  window_failures={item.get('window_failures')}"
        )
    return 0


def cmd_unfreeze(args) -> int:
    if auth.unfreeze_user(args.username, operator="cli"):
        print(f"[info] 账号 {args.username} 已解冻")
        return 0
    print(f"[错误] 账号 {args.username} 不存在", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description="管理员登录安全命令行工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("frozen", help="冻结账号")
    p.add_argument("action", choices=["list"])

    p = sub.add_parser("unfreeze", help="解冻账号")
    p.add_argument("username")

    args = ap.parse_args()
    if args.cmd == "frozen":
        return cmd_frozen_list(args)
    if args.cmd == "unfreeze":
        return cmd_unfreeze(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
