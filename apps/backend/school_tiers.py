# -*- coding: utf-8 -*-
"""院校层次名单：把"这所学校是不是 985/211"从模型自报改成**服务端名单判定**。

为什么需要它：模型输出 `school_tier` 的词表是 `985/211/一本/二本/专科/其他/未提供`，
**没有"双一流"**。JD 写"双一流院校毕业"时，一所真·双一流但非 211 的学校（宁波大学、
南京邮电大学那批）只能被标成"一本"，而旧逻辑把"一本"读成"明确不是 985/211/双一流"
→ 判 not_met → 扣 10 分 + 封顶 69。名单把这条链路换成确定性事实。

判定口径（只认正证）：
- 名单查到 985 → `985`；查到 211 → `211`；
- **查不到 → 空字符串**，绝不据此判"不达标"（可能是新增双一流院校、分校、或名称写法不同）。

数据来源与校验见 `school_tiers.json` 的 `_meta`。
"""
import json
import os
import threading

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "school_tiers.json")

_LOCK = threading.Lock()
_CACHE: dict | None = None
_BY_NAME: dict[str, frozenset[str]] = {}
_ALIASES: dict[str, str] = {}


def _normalize(name: str) -> str:
    """归一院校名：去空白与间隔符、全角括号转半角、去掉尾部的"（…）"备注。"""
    value = str(name or "").strip()
    if not value:
        return ""
    value = value.replace("（", "(").replace("）", ")")
    for char in (" ", "\t", "　", "·", "．", "."):
        value = value.replace(char, "")
    return value


def _load() -> None:
    global _CACHE, _BY_NAME, _ALIASES
    if _CACHE is not None:
        return
    with _LOCK:
        if _CACHE is not None:
            return
        payload: dict = {}
        try:
            with open(DATA_PATH, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:  # 文件缺失/损坏：退化成"没有任何正证"，判定照旧走人工复核
            payload = {}
        by_name: dict[str, frozenset[str]] = {}
        for entry in payload.get("schools", []):
            if not isinstance(entry, dict):
                continue
            name = _normalize(entry.get("name"))
            tags = entry.get("tags")
            if name and isinstance(tags, list):
                by_name[name] = frozenset(str(tag) for tag in tags)
        aliases = {
            _normalize(key): _normalize(value)
            for key, value in (payload.get("aliases") or {}).items()
            if _normalize(key) and _normalize(value)
        }
        _CACHE = payload
        _BY_NAME = by_name
        _ALIASES = aliases


def lookup(school_name: str) -> frozenset[str]:
    """返回该校已知的层次标签集合（`{'985'}` / `{'211'}` / 空集表示名单里没有）。"""
    _load()
    name = _normalize(school_name)
    if not name:
        return frozenset()
    direct = _BY_NAME.get(name)
    if direct is not None:
        return direct
    aliased = _ALIASES.get(name)
    if aliased:
        return _BY_NAME.get(aliased, frozenset())
    # 名称带后缀（"某某大学（威海）"/"某某大学医学院"）：按唯一前缀命中，命中多所即放弃
    matches = [tags for key, tags in _BY_NAME.items() if len(name) >= 4 and key.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    return frozenset()


def tier_label(school_name: str) -> str:
    """返回 `985` / `211` / `双一流` / 空字符串（空字符串＝**无法证明**，不是"不是"）。"""
    tags = lookup(school_name)
    if "985" in tags:
        return "985"
    if "211" in tags:
        return "211"
    if "双一流" in tags:
        return "双一流"
    return ""


def is_known(school_name: str) -> bool:
    return bool(lookup(school_name))


def stats() -> dict:
    """名单规模（供日志/诊断使用）。"""
    _load()
    counts = {"total": len(_BY_NAME), "aliases": len(_ALIASES)}
    counts["985"] = sum(1 for tags in _BY_NAME.values() if "985" in tags)
    counts["211"] = sum(1 for tags in _BY_NAME.values() if "211" in tags)
    counts["dual_first_class"] = sum(1 for tags in _BY_NAME.values() if "双一流" in tags)
    return counts
