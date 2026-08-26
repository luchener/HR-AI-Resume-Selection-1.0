"""
管理员紧急重置密码工具（忘记密码的兜底方案）。

适用场景：
- 用户未绑定邮箱（旧账号），无法走邮件验证码重置
- SMTP 未配置 / 邮件服务故障

用法（服务器上，项目 apps/backend 目录）：
    python reset_password_cli.py <用户名>
    python reset_password_cli.py --new-password <新密码> <用户名>

说明：
- 管理员线下核验用户身份后，直接为用户重置密码（不经邮箱验证码）。
- 默认自动生成一个合规的临时密码并打印；也可用 --new-password 指定。
- 重置后该用户所有旧会话 token 立即失效（password_version +1），并需立即登录修改。
"""
import argparse
import secrets
import sys

import auth
import config  # noqa: F401  确保 config 先加载 .env 与目录


def _gen_temp_password() -> str:
    """生成合规临时密码（≥8 位，含字母和数字）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10)) + "9"


def main():
    ap = argparse.ArgumentParser(description="管理员紧急重置用户密码")
    ap.add_argument("username", help="要重置密码的用户名（大小写不敏感）")
    ap.add_argument("--new-password", "-p", help="指定的新密码（需≥8位含字母数字）")
    args = ap.parse_args()

    user = auth.find_user_by_username(args.username)
    if not user:
        print(f"[错误] 用户 '{args.username}' 不存在。", file=sys.stderr)
        sys.exit(1)

    new_password = args.new_password or _gen_temp_password()
    ok, err = auth.update_password(user["user_id"], new_password)
    if not ok:
        print(f"[错误] 重置失败: {err}", file=sys.stderr)
        sys.exit(1)

    print("=" * 56)
    print(f"用户:      {user['username']}")
    print(f"临时密码:  {new_password}")
    print("")
    print("· 旧会话已全部失效（密码版本已 +1）")
    print("· 请通过可信渠道把临时密码告知用户本人，并提醒登录后立即修改")
    print("=" * 56)


if __name__ == "__main__":
    main()