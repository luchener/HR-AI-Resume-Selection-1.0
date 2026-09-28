"""
管理员账号归属迁移工具：把系统最高管理员（超级管理员）切换到指定账号。

管理员判定机制（见 docs/ARCHITECTURE.md）：
- 管理员（is_admin）        = .env ADMIN_EMAILS 白名单邮箱 或 用户记录 is_admin 标记
- 超级管理员（is_super_admin）= 仅 .env ADMIN_EMAILS 白名单邮箱（默认 luchenstudio@163.com）
即：哪个账号绑定了 luchenstudio@163.com，哪个账号就是超级管理员。

本工具用于部署环境（账号数据在服务器 apps/backend/data/ 里）：
- status                   查看全部账号与管理员身份
- rename <旧> <新>          把账号改名（保留邮箱/密码/绑定），例：admin -> luchen
- make-owner <用户名>       让指定账号绑定白名单邮箱成为超级管理员，并自动取消其他
                           白名单邮箱账号的最高权限（清空其邮箱绑定）
- unbind-email <用户名>     取消指定账号的白名单邮箱绑定（取消其最高权限）

典型用法（服务器 apps/backend 目录）：
    python admin_owner_cli.py status                      # 先看现状
    # 场景A：把管理员账号 admin 直接改名为 luchen（仍绑定白名单邮箱 -> luchen 即超管，admin 账号消失）
    python admin_owner_cli.py rename admin luchen
    # 场景B：luchen 账号已存在 -> 绑邮箱成为超管，再取消 admin 的最高权限
    python admin_owner_cli.py make-owner luchen
    python admin_owner_cli.py unbind-email admin
    # luchen 账号还不存在时：先用邀请码注册（鸡生蛋），再执行上面命令
    python invite_cli.py gen --email luchenstudio@163.com --note "管理员首号注册"

安全说明：
- 所有写操作走 auth.py 的文件锁 + 原子写 + 用户名索引重建（与网页后台同一套存储）。
- make-owner / unbind-email 带"最后一个超级管理员"防锁死保护（不会把系统锁死到无超管）。
- 每次变更写入 data/admin_ops/ 审计，操作人记 cli。
"""
import argparse
import os
import re
import sys

import auth
import config  # noqa: F401  确保 config 先加载 .env 与目录


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{2,64}$")


def _all_user_records():
    """遍历 data/users/ 下全部用户记录（含软删除，用于权限盘点）。"""
    out = []
    if not os.path.isdir(auth.USERS_DIR):
        return out
    for filename in sorted(os.listdir(auth.USERS_DIR)):
        if not filename.endswith(".json") or filename in ("_index.json", "_index.lock"):
            continue
        rec = auth._read_json(os.path.join(auth.USERS_DIR, filename))
        if rec:
            out.append(rec)
    return out


def _whitelist_admins(records, exclude_user_id=None):
    """返回绑定 .env 白名单邮箱（即超级管理员）的账号。"""
    return [
        r
        for r in records
        if r.get("user_id") != exclude_user_id
        and auth.is_admin_email(r.get("email") or "")
    ]


def _print_account(u: dict) -> None:
    email = u.get("email") or ""
    flags = []
    if auth.is_admin_email(email):
        flags.append("超级管理员(白名单邮箱)")
    if u.get("is_admin"):
        flags.append("管理员(is_admin标记)")
    if u.get("deleted_at"):
        flags.append("已软删除")
    status = "；".join(flags) if flags else "普通用户"
    frozen = auth.is_user_frozen(u.get("username") or "")
    print(
        f"  {str(u.get('username') or ''):<20} email={email or '(未绑定)':<26} "
        f"frozen={frozen}  {status}"
    )


def cmd_status(_args) -> int:
    active = auth.list_users(keyword="", page=1, size=10**9)
    deleted = auth.list_deleted_users()
    print(f"== 账号状态（活跃 {len(active)}，软删除 {len(deleted)}）==")
    for u in sorted(active, key=lambda x: str(x.get("created_at") or "")):
        _print_account(u)
    for u in deleted:
        _print_account(u)
    print(f"管理员白名单（ADMIN_EMAILS）：{', '.join(auth.ADMIN_EMAILS) or '(空)'}")
    return 0


def cmd_rename(args) -> int:
    old = (args.old or "").strip()
    new = (args.new or "").strip()
    if not old or not new:
        print("[错误] 新旧用户名都不能为空", file=sys.stderr)
        return 1
    if old.lower() == new.lower():
        print("[错误] 新旧用户名相同（用户名大小写不敏感）", file=sys.stderr)
        return 1
    if not _USERNAME_RE.match(new):
        print("[错误] 新用户名仅允许字母、数字、下划线、连字符或中文，长度 2-64 字符", file=sys.stderr)
        return 1
    user = auth.find_user_by_username(old)
    if not user:
        print(f"[错误] 账号 {old} 不存在", file=sys.stderr)
        return 1
    if auth.find_user_by_username(new):
        print(f"[错误] 用户名 {new} 已存在（用户名大小写不敏感）", file=sys.stderr)
        return 1

    with auth._UserLock():
        user = auth.find_user_by_username(old)
        if not user:
            print(f"[错误] 账号 {old} 不存在", file=sys.stderr)
            return 1
        user["username"] = new
        auth._write_json(os.path.join(auth.USERS_DIR, f"{user['user_id']}.json"), user)
        auth._rebuild_index()

    email = user.get("email") or ""
    print(f"[info] 已改名：{old} -> {new}")
    if auth.is_admin_email(email):
        print(f"[info] 该账号仍绑定白名单邮箱 {email}，即超级管理员 -> 管理员现为 {new}")
    else:
        print(f"[info] 该账号未绑定白名单邮箱（email={email or '(未绑定)'}）")
    auth.record_admin_op(
        "user_rename",
        operator_id="cli",
        operator_name="cli",
        target_username=new,
        detail=f"重命名账号 {old} -> {new}",
    )
    return 0


def cmd_make_owner(args) -> int:
    username = (args.username or "").strip()
    user = auth.find_user_by_username(username)
    if not user:
        print(f"[错误] 账号 {username} 不存在", file=sys.stderr)
        return 1
    owner_email = auth.ADMIN_EMAILS[0] if auth.ADMIN_EMAILS else "luchenstudio@163.com"

    with auth._UserLock():
        records = _all_user_records()
        others = _whitelist_admins(records, exclude_user_id=user["user_id"])
        target = next(
            (r for r in records if r.get("user_id") == user["user_id"]), None
        )
        if not target:
            print(f"[错误] 账号 {username} 不存在", file=sys.stderr)
            return 1
        # 1) 目标账号绑定白名单邮箱 -> 成为超级管理员
        target["email"] = owner_email
        auth._write_json(
            os.path.join(auth.USERS_DIR, f"{target['user_id']}.json"), target
        )
        # 2) 取消其他白名单邮箱账号的最高权限（清空邮箱绑定，保留账号与密码）
        for other in others:
            print(
                f"[info] 取消 {other.get('username')} 的最高权限：清空其邮箱绑定 "
                f"{other.get('email') or ''}"
            )
            other["email"] = ""
            auth._write_json(
                os.path.join(auth.USERS_DIR, f"{other['user_id']}.json"), other
            )
        auth._rebuild_index()

    print(f"[info] 已完成：{username} 绑定 {owner_email}，成为超级管理员（管理员 = {username}）")
    auth.record_admin_op(
        "admin_owner_set",
        operator_id="cli",
        operator_name="cli",
        target_username=username,
        detail=f"设置 {username} 为超级管理员（绑定 {owner_email}），降权 {len(others)} 个原白名单账号",
    )
    return 0


def cmd_unbind_email(args) -> int:
    username = (args.username or "").strip()
    user = auth.find_user_by_username(username)
    if not user:
        print(f"[错误] 账号 {username} 不存在", file=sys.stderr)
        return 1
    email = user.get("email") or ""
    if not email:
        print(f"[info] 账号 {username} 未绑定邮箱，本就无最高权限")
        return 0
    if not auth.is_admin_email(email):
        print(f"[info] 账号 {username} 的邮箱 {email} 不在管理员白名单，无最高权限可取消")
        return 0

    # 防锁死：取消后系统必须仍至少有一个超级管理员
    others = _whitelist_admins(_all_user_records(), exclude_user_id=user["user_id"])
    if not others:
        print(
            "[错误] 这是最后一个白名单邮箱超级管理员账号，取消后系统将没有超级管理员"
            "（防锁死，已中止）。请先 make-owner 指定新管理员再操作。",
            file=sys.stderr,
        )
        return 1

    with auth._UserLock():
        user = auth.find_user_by_username(username)
        if not user:
            print(f"[错误] 账号 {username} 不存在", file=sys.stderr)
            return 1
        user["email"] = ""
        auth._write_json(os.path.join(auth.USERS_DIR, f"{user['user_id']}.json"), user)
        auth._rebuild_index()

    print(f"[info] 已取消 {username} 的最高权限（清空白名单邮箱绑定 {email}）")
    auth.record_admin_op(
        "admin_owner_unbind",
        operator_id="cli",
        operator_name="cli",
        target_username=username,
        detail=f"取消 {username} 的超级管理员权限（清空邮箱绑定）",
    )
    return 0


def main():
    ap = argparse.ArgumentParser(description="管理员账号归属迁移工具（最高管理员切换为 luchen 等）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看全部账号与管理员身份")

    p = sub.add_parser("rename", help="账号改名（保留邮箱/密码/绑定）")
    p.add_argument("old", help="旧用户名")
    p.add_argument("new", help="新用户名")

    p = sub.add_parser("make-owner", help="指定账号绑定白名单邮箱成为超级管理员，并降权其他白名单账号")
    p.add_argument("username")

    p = sub.add_parser("unbind-email", help="取消指定账号的白名单邮箱绑定（取消最高权限）")
    p.add_argument("username")

    args = ap.parse_args()
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "rename":
        return cmd_rename(args)
    if args.cmd == "make-owner":
        return cmd_make_owner(args)
    if args.cmd == "unbind-email":
        return cmd_unbind_email(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
