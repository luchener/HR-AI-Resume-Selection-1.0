# -*- coding: utf-8 -*-
"""测试隔离：把「数据目录」整体搬到临时目录，测试结束后**原样恢复**。

要解决的坑（2026-10-09 定位）：
多个测试模块在 setUp 里直接改写 store/auth/config 的模块级目录常量，tearDown 删掉临时
目录却不恢复原值——这些常量就永久指向一个**已删除**的目录。于是后续任一模块调用
auth.create_user(email=...) 都会在 os.listdir(USERS_DIR) 上抛 FileNotFoundError。

症状：全量 discover 跑出 53 failures + 24 errors，而同样这些模块**单独跑全部 OK**。

做法：不逐个硬编码变量名（那样新增模块必然漏），而是扫描已加载模块中**值是指向
DATA_DIR 内部路径**的字符串属性，整体换成临时目录下的同构路径：
    config.RESUMES_DIR → <tmp>/resumes
    store.RESUMES_DIR  → <tmp>/resumes   （from config import 的模块级快照）
    auth.USERS_DIR     → <tmp>/users
    auth._INDEX_FILE   → <tmp>/users/_index.json
    config.DATA_DIR    → <tmp>
restore 时逐个写回原值。用 addCleanup 注册，断言失败也能保证恢复。

用法：
    from testsupport import isolated_data_dirs

    def setUp(self):
        self.dirs = isolated_data_dirs("smoke")
        self.addCleanup(self.dirs.restore)
"""
import atexit
import os
import shutil
import sys
import uuid

# 数据目录的规范布局：名字 → 相对 DATA_DIR 的路径。
# 必须按**名字**重定向，而不是看当前值在不在真实 DATA_DIR 里：
# 别的测试模块可能已经把 auth.USERS_DIR 改到它自己的临时目录（甚至已删除），
# 那种“已跑偏”的值同样必须被接管，否则隔离就是漏的。
# 新增数据目录时同步这里；test_testsupport.py 会对照 config.py 校验这张表有没有漏。
_CANONICAL_RELPATHS = {
    "DATA_DIR": "",
    "RESUMES_DIR": "resumes",
    "JOBS_DIR": "jobs",
    "ARCHIVES_DIR": "archives",
    "ARCHIVE_INDEX_PATH": os.path.join("archives", "_index.json"),
    "RESUME_ORIGINALS_DIR": "resume_originals",
    "USERS_DIR": "users",
    "_INDEX_FILE": os.path.join("users", "_index.json"),
    "_INDEX_LOCK": os.path.join("users", "_index.lock"),
    "RESETS_DIR": "password_resets",
    "LOGIN_FAILURES_DIR": "login_failures",
    "RATE_LIMITS_DIR": "rate_limits",
    "INVITE_REQUESTS_DIR": "invite_requests",
    "INVITE_CODES_DIR": "invite_codes",
    "INVITE_CODES_AUDIT_DIR": "invite_codes_audit",
    "ADMIN_OPS_DIR": "admin_ops",
    "USER_USAGE_DIR": "usage",
    "ANNOUNCEMENTS_DIR": "announcements",
    "ANNOUNCEMENT_READS_DIR": "announcement_reads",
    "EMAIL_IMAGES_DIR": "email_images",
    "EMAIL_LOGS_DIR": "email_logs",
    "SYSTEM_CONFIG_DIR": "system_config",
    "QUOTA_DIR": "quota",
    "QUOTA_LIMITS_PATH": os.path.join("quota", "limits.json"),
    "PRUNE_TOUCH_FILE": ".last_prune",
    # retention 模块自己的扫描哨兵文件（不在 config 里，但同样指向 data/）
    "_SENTINEL": ".last_retention",
}
# 这些名字虽然像路径，但绝不能重定向（会破坏应用根目录/日志/环境文件语义）
_NEVER_RELOCATE = frozenset({"BASE_DIR", "LOG_DIR", "ENV_FILE", "_BACKEND_DIR"})

_SKIP_ATTRS = frozenset({
    "__all__", "__builtins__", "__cached__", "__doc__", "__file__", "__loader__",
    "__name__", "__package__", "__path__", "__spec__",
})
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_project_module(module) -> bool:
    """只认本项目（apps/backend）下的模块。

    绝不能扫描第三方库：openai 等把懒代理挂在模块上，读它们的属性会触发副作用
    （实测 isinstance() 会评估 openai 的 _ModuleClient 代理，直接抛 OpenAIError）。
    """
    try:
        path = vars(module).get("__file__")
    except TypeError:  # 没有 __dict__ 的对象
        return False
    if not isinstance(path, str) or not path:
        return False
    return os.path.dirname(os.path.abspath(path)) == _BACKEND_DIR


def _real_data_dir() -> str:
    """真实数据目录。从 config.py 的位置推导，不受任何已被改写的运行时值影响。"""
    import config

    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config.__file__)), "data"))


def _relative_inside(path: str, root: str) -> str | None:
    """path 位于 root 内部时返回相对路径（root 本身返回 ''），否则 None。"""
    try:
        candidate = os.path.normpath(os.path.abspath(path))
    except (TypeError, ValueError):
        return None
    if candidate == root:
        return ""
    if candidate.startswith(root + os.sep):
        return candidate[len(root) + 1:]
    return None


class IsolatedDataDirs:
    """把数据目录重定向到临时目录；restore() 恢复所有被改写的模块级路径。"""

    def __init__(self, prefix: str = "test", root: str | None = None):
        self.prefix = prefix
        # root 允许调用方指定（沿用该模块原有的 _TMP 常量，测试体里的路径断言不用改）
        self.root = root or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".test-tmp",
            f"{prefix}_{uuid.uuid4().hex[:8]}",
        )
        self._saved: list[tuple[object, str, str]] = []
        self._relocated = 0
        # 模块级使用（导入时就隔离、没有 addCleanup）时也保证进程退出不留临时目录
        atexit.register(shutil.rmtree, self.root, ignore_errors=True)

    @property
    def relocated(self) -> int:
        """被改写的路径属性数量（便于在测试里断言隔离确实生效）。"""
        return self._relocated

    def apply(self) -> "IsolatedDataDirs":
        real = _real_data_dir()
        os.makedirs(self.root, exist_ok=True)
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith("_") or "." in module_name:
                continue
            if not _is_project_module(module):
                continue
            try:
                attributes = list(vars(module).items())
            except TypeError:
                continue
            for attr, value in attributes:
                # type() 而不是 isinstance()：代理对象的 isinstance 会触发 __class__
                if attr in _SKIP_ATTRS or attr in _NEVER_RELOCATE:
                    continue
                if type(value) is not str or not value:
                    continue
                rel = _CANONICAL_RELPATHS.get(attr)
                if rel is None:
                    # 兜底：表里没有的新路径常量，只要当前值在真实 DATA_DIR 内部也一起隔离
                    rel = _relative_inside(value, real)
                if rel is None:
                    continue
                new_path = os.path.join(self.root, rel) if rel else self.root
                self._ensure_writable(new_path, self._looks_like_file(value))
                setattr(module, attr, new_path)
                self._saved.append((module, attr, value))
                self._relocated += 1
        return self

    def reapply(self) -> "IsolatedDataDirs":
        """在 setUpModule 里重新接管一次（模块级隔离必须这么用）。

        discover 会先把所有模块 import 完再跑用例，而各模块的 tearDownModule 是在自己
        那批用例结束后就还原（把路径指回真实 data/）。所以模块级隔离如果只在 import 时
        生效，跑到本模块时路径早就被前一个模块的 tearDownModule 还原成真实目录了。
        """
        return self.apply().ensure_dirs()

    def ensure_dirs(self) -> "IsolatedDataDirs":
        """（重新）建齐规范布局里的所有目录。

        setUpClass 里 rmtree 过临时目录之后调用，避免应用需要的目录缺失。
        """
        for rel in _CANONICAL_RELPATHS.values():
            if not rel:
                continue
            # 按名字判断是文件还是目录：目录型要建自身，文件型要建父目录
            self._ensure_writable(os.path.join(self.root, rel), self._looks_like_file(rel))
        os.makedirs(self.root, exist_ok=True)
        return self

    def restore(self) -> None:
        """恢复被改写的路径。

        表里的规范名字一律恢复到**真实 DATA_DIR** 下的对应位置，而不是恢复到
        apply() 时的旧值——那个旧值本身可能就是上一个模块留下的临时目录，
        照原样还回去等于把污染继续传下去。
        """
        real = _real_data_dir()
        for module, attr, value in reversed(self._saved):
            if attr in _CANONICAL_RELPATHS:
                rel = _CANONICAL_RELPATHS[attr]
                target = os.path.join(real, rel) if rel else real
            else:
                target = value
            try:
                setattr(module, attr, target)
            except Exception:  # pragma: no cover - 恢复失败不应掩盖测试本身的失败
                pass
        self._saved = []
        shutil.rmtree(self.root, ignore_errors=True)
        # 默认根目录是 .test-tmp/<prefix>_<uuid>/，跑完顺手把空的 .test-tmp 收掉，别留残余
        parent = os.path.dirname(self.root)
        if os.path.basename(parent) == ".test-tmp":
            try:
                os.rmdir(parent)
            except OSError:
                pass

    def __enter__(self) -> "IsolatedDataDirs":
        return self

    def __exit__(self, *exc_info) -> None:
        self.restore()

    @staticmethod
    def _looks_like_file(path: str) -> bool:
        """判断路径是文件还是目录。

        不能只看 os.path.isfile：像 config.QUOTA_LIMITS_PATH（quota/limits.json）这种
        **尚未创建**的文件会因此被当成目录建出来，之后 open() 直接 PermissionError。
        """
        if os.path.isfile(path):
            return True
        if os.path.isdir(path):
            return False
        return "." in os.path.basename(path)

    @staticmethod
    def _ensure_writable(path: str, is_file: bool) -> None:
        """文件型路径建父目录，目录型路径建自身——保证后续读写不会因目录缺失失败。"""
        target = os.path.dirname(path) if is_file else path
        if target:
            os.makedirs(target, exist_ok=True)


def snapshot_globals(*pairs) -> list[tuple[object, str, object]]:
    """快照若干模块属性，配合 restore_globals 使用。

    pairs 形如 ("config", "LOGIN_FAIL_LIMIT") 或 (mailer, "send_verification_email")，
    前者按模块名 import，后者直接用模块对象。用于测试在 import/setUpModule 里改过的
    标量或函数（验证码开关、限流阈值、邮件发送桩…），避免泄漏给后面的测试模块。
    """
    import importlib

    saved = []
    for target, attr in pairs:
        module = target if not isinstance(target, str) else importlib.import_module(target)
        if hasattr(module, attr):
            saved.append((module, attr, getattr(module, attr)))
    return saved


def restore_globals(saved) -> None:
    for module, attr, value in saved:
        try:
            setattr(module, attr, value)
        except Exception:  # pragma: no cover
            pass


def restore_admin_whitelist() -> None:
    """把被测试替换掉的管理员白名单恢复成 config 里的规范值。

    test_admin_resume_library / test_llm_config / test_system_config 都在 import 时
    直接执行 `auth.ADMIN_EMAILS = {自己的超级管理员邮箱}`，且从不还原。整套跑时白名单
    就永久变成了别人的邮箱，于是 test_user_admin 里那个 .env 白名单管理员不再被认作
    管理员，所有 /admin/* 请求 403（单跑却正常）。在 tearDownModule 里调用本函数即可。

    恢复到 config.ADMIN_EMAILS（而不是“进本模块时的旧值”）：旧值本身可能已被前一个
    模块污染，照原样还回去只是把 403 传给下一个模块。
    """
    import auth
    import config

    auth.ADMIN_EMAILS = list(config.ADMIN_EMAILS)


def isolated_data_dirs(prefix: str = "test", root: str | None = None) -> IsolatedDataDirs:
    """建好数据目录并接管所有数据路径。

    - 在 setUp 里用：`self.dirs = isolated_data_dirs("x"); self.addCleanup(self.dirs.restore)`
    - 在模块级用（模块在 import 时就改过 config.DATA_DIR）：`_DIRS = isolated_data_dirs("x", root=_TMP)`，
      并在 `tearDownModule()` 里调用 `_DIRS.restore()`。
    """
    dirs = IsolatedDataDirs(prefix, root=root).apply()
    return dirs.ensure_dirs()


# ── 真实 data/ 写保护哨兵 ────────────────────────────────────────────────
# 2026-10-10 的现场：test_quota 自己手写了一套“只覆盖 USERS_DIR”的隔离，漏掉
# auth._INDEX_FILE / auth.PRUNE_TOUCH_FILE / retention._SENTINEL，于是整套跑时
# users/_index.json、.last_prune、.last_retention 一直被写进**真实 data/**。
# 单跑发现不了，只能靠“跑完对比一次”才发现——所以把它自动化：
# 任何测试模块只要绕过了隔离引擎往真实 DATA_DIR 写东西，套件退出时就会喊出来。
_MAX_TRACKED_FILES = 20000


def _snapshot_real_data() -> dict:
    """记录真实 DATA_DIR 下的文件指纹（太大就退化成只记数量）。"""
    real = _real_data_dir()
    snapshot = {"count": 0, "files": None}
    if not os.path.isdir(real):
        return snapshot
    files = {}
    for root_dir, _dirs, names in os.walk(real):
        for name in names:
            path = os.path.join(root_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            snapshot["count"] += 1
            if snapshot["count"] > _MAX_TRACKED_FILES:
                snapshot["files"] = None
                snapshot["count"] -= 1
                continue
            if snapshot["files"] is not None:
                files[os.path.relpath(path, real)] = stat.st_size
    if snapshot["files"] is not None:
        snapshot["files"] = files
    return snapshot


_REAL_DATA_AT_IMPORT = _snapshot_real_data()


def _isolation_tripwire() -> None:
    """套件退出时校验真实 data/ 是否被动过（只在真被写时输出）。"""
    after = _snapshot_real_data()
    before = _REAL_DATA_AT_IMPORT
    if before["files"] is None or after["files"] is None:
        if after["count"] != before["count"]:
            print(
                f"\n!!! 测试写保护：真实 data/ 的文件数从 {before['count']} 变成 {after['count']}，"
                "说明有测试模块绕过了 testsupport 隔离（请用 isolated_data_dirs 接管路径）。"
            )
        return
    added = sorted(set(after["files"]) - set(before["files"]))
    removed = sorted(set(before["files"]) - set(after["files"]))
    changed = sorted(
        name for name in set(before["files"]) & set(after["files"])
        if before["files"][name] != after["files"][name]
    )
    if not (added or removed or changed):
        return
    print("\n!!! 测试写保护：本次运行改动了真实 data/ —— 有测试模块绕过了 testsupport 隔离。")
    for label, items in (("新增", added), ("删除", removed), ("改动", changed)):
        if items:
            shown = ", ".join(items[:8]) + (" ..." if len(items) > 8 else "")
            print(f"    {label} {len(items)} 个：{shown}")
    print("    修法：在该模块用 isolated_data_dirs(prefix=...) + setUpModule 里 reapply()。")


atexit.register(_isolation_tripwire)
