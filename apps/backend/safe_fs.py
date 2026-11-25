"""安全文件系统操作：目录缺失/无权限时返回空结果，而不是抛异常。

为什么单独一个模块：数据目录的“读列表”调用点有 20 多处（用户、审计、用量、
邀请、公告、邮件、归档、原始简历、保留策略……）。os.listdir 在目录缺失、
被清空或权限不足时抛 OSError；只要有一处没守，对应页面就是整页 500。而且这些
场景偏偏都发生在最紧张的时刻：首次部署还没建目录、恢复备份时清空了目录、
运维手动挪走过 data/。

语义：**缺目录 = 没有内容，不是异常。**
"""
from __future__ import annotations

import os
import re


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def safe_component(value: str, *, max_length: int = 128) -> str:
    """Return a single safe path component, or empty for traversal/device names.

    Resource identifiers are generated as UUIDs or simple test IDs.  Path
    separators, dots, control characters, ADS colons and overlong values are
    never needed and must not reach os.path.join on Windows.
    """
    text = str(value or "")
    device_name = text.split(".", 1)[0].upper()
    if (
        not text
        or len(text) > max_length
        or not _SAFE_COMPONENT.fullmatch(text)
        or device_name in _WINDOWS_RESERVED_NAMES
    ):
        return ""
    return text


def listdir(directory: str) -> list[str]:
    """列目录；目录不存在、不是目录、无权限 → 返回空列表。"""
    try:
        return os.listdir(directory)
    except OSError:
        return []


def list_files(directory: str, *, suffix: str = "") -> list[str]:
    """列目录并按后缀过滤（后缀为空则不过滤），始终保持排序后的稳定结果。"""
    names = listdir(directory)
    if suffix:
        names = [name for name in names if name.endswith(suffix)]
    return sorted(names)


def isdir(directory: str) -> bool:
    """目录存在性判断，任何异常都视为不存在。"""
    try:
        return os.path.isdir(directory)
    except OSError:
        return False
