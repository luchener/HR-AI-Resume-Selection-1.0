"""Conservative shared-tail OR practice clues, with original Unicode offsets.

This module never rewrites a JD or scores it. None means the grammar is outside
this deliberately small parser; callers must keep their original requirement.
A result's matched flag only indicates an explicit object + practice action.
Offsets are zero-based, end-exclusive Python string indices in the full resume.
No IO, application configuration, model or third-party dependency is used.
"""
from __future__ import annotations

import re

# Auditable equivalence classes, not inference edges. Agent/API/SQL are NOT aliases.
PRACTICE_ALIASES: tuple[tuple[str, ...], ...] = (
    ("RAG应用", "RAG", "检索增强生成", "检索增强"),
    ("问答系统", "知识问答", "知识问答系统"),
    ("SQL自动生成", "自动生成SQL", "text2sql", "NL2SQL"),
    ("数据分析助手", "分析助手"),
)
_TAIL = re.compile(r"(?P<tail>开发经验|实施经验|使用经验|实践经验)(?:者)?(?:优先|为佳)?[。.!！]?$", re.I)
_AND = re.compile(r"并且|同时|以及|且|和|与|及|\band\b", re.I)
_PREFIX = re.compile(r"^(?:要求|需|需要|应)?\s*(?:具有|具备|拥有|有)\s*")
_INTENT = re.compile(r"求职意向|求职目标|职业目标|希望从事|期望从事|希望参与|希望|期望|未来|拟开发|计划|打算|想要|期望岗位|目标岗位|意向岗位")
_NEGATIVE = re.compile(r"尚未|从未|未曾|未|没|不(?:具备|会|曾|熟悉|使用|涉及|了解|开发|实施|实践|做|负责|参与|实现|构建|搭建)|无(?:相关)?|缺乏|缺少|\b(?:never|not|without|no)\b", re.I)
_ACTIONS = {
    "开发经验": r"开发|研发|构建|搭建|实现|编写|打造",
    "实施经验": r"实施|部署|交付|落地|搭建|上线",
    "使用经验": r"使用|运用|采用|用过|应用于",
    "实践经验": r"开发|研发|构建|搭建|实现|编写|实施|部署|交付|落地|上线|使用|运用|采用|用过|实践",
}
# A sentence/clause never borrows actions from another line, sentence or comma.
_SEGMENTS = re.compile(r"[^。；;！!？?\r\n，,]+")


def _literal_pattern(surface: str) -> re.Pattern:
    left = r"(?<![A-Za-z0-9_])" if surface[0].isascii() and surface[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if surface[-1].isascii() and surface[-1].isalnum() else ""
    return re.compile(left + re.escape(surface) + right, re.I)


def _parse(requirement: str) -> tuple[list[str], str] | None:
    text = requirement.strip()
    tail_match = _TAIL.search(text)
    if not tail_match or _AND.search(text):
        return None
    objects = _PREFIX.sub("", text[:tail_match.start()]).strip()
    # Parenthetical logic, multiple requirements and independently suffixed
    # experience clauses need a richer parser; do not guess their scope.
    if re.search(r"[。；;：:\n\r()（）/]|经验|例如|包括|如：|等", objects):
        return None
    explicit_or = "或" in objects or bool(re.search(r"任一|任意一种|其中之一|之一", objects))
    if not explicit_or:
        return None
    objects = re.sub(r"(?:中的)?(?:任一(?:项|种)?|任意一种|其中之一|之一)$", "", objects).strip()
    if re.search(r"任一|之一", objects):
        return None
    parts = [part.strip() for part in re.split(r"、|，|,|或者|或", objects)]
    if not 2 <= len(parts) <= 12 or any(not part or len(part) > 40 for part in parts):
        return None
    if any(re.search(r"\s{2,}|具备|拥有|具有|要求|必须|至少|熟悉|掌握|精通|负责|能够|优先|开发|实施|使用|实践", part) for part in parts):
        return None
    # Unknown objects are intentionally accepted literally, including Chinese.
    return parts, tail_match.group("tail")


def _surfaces(obj: str) -> tuple[str, ...]:
    for group in PRACTICE_ALIASES:
        if obj.casefold() in {surface.casefold() for surface in group}:
            return group
    return (obj,)


def match_practice_alternatives(requirement: str, resume: str) -> dict | None:
    """Recognize explicit shared-tail OR and return positive/negative citations.

    branches: expanded strings in original order, each with the shared tail.
    matches / negative_matches: branch_index, branch, quote, start, end. Quotes
    contain both object and action, and always equal resume[start:end].
    matched: any branch has positive practice evidence. conflicting: the SAME
    branch also has negative practice evidence. Intent is never evidence.
    Complex/AND/non-practice/ambiguous enumeration requirements return None.
    """
    if not isinstance(requirement, str) or not isinstance(resume, str):
        return None
    parsed = _parse(requirement)
    if parsed is None:
        return None
    objects, tail = parsed
    branches = [obj + tail for obj in objects]
    patterns = [tuple(_literal_pattern(surface) for surface in _surfaces(obj)) for obj in objects]
    action_pattern = re.compile(_ACTIONS[tail])
    positives: list[dict] = []
    negatives: list[dict] = []
    # An intent heading applies to following lines within its paragraph too.
    offset = 0
    intent = False
    for line in resume.splitlines(keepends=True):
        if not line.strip():
            intent = False
        if _INTENT.search(line):
            intent = True
        elif re.match(r"\s*(?:项目经历|项目经验|工作经历|工作经验|实习经历|专业技能|教育经历)\s*[:：]?\s*$", line):
            intent = False
        if not intent:
            # Contrast delimiters keep local negative scope separate.
            for segment in _SEGMENTS.finditer(line):
                for clause in re.finditer(r"(?:(?!但是|不过|然而|但).)+", segment.group()):
                    raw = clause.group()
                    quote = raw.strip()
                    if not quote or _INTENT.search(quote):
                        continue
                    # Mere skill listings or future wishes are not actions.
                    if not action_pattern.search(quote):
                        continue
                    start = offset + segment.start() + clause.start() + len(raw) - len(raw.lstrip())
                    end = start + len(quote)
                    negative = bool(_NEGATIVE.search(re.sub(r"不仅|不但|不只是|不止", "", quote)))
                    for index, aliases in enumerate(patterns):
                        if any(pattern.search(quote) for pattern in aliases):
                            citation = {"branch_index": index, "branch": branches[index],
                                        "quote": quote, "start": start, "end": end}
                            (negatives if negative else positives).append(citation)
        offset += len(line)
    positive_indices = {item["branch_index"] for item in positives}
    negative_indices = {item["branch_index"] for item in negatives}
    return {"branches": branches, "matches": positives, "negative_matches": negatives,
            "matched": bool(positives), "conflicting": bool(positive_indices & negative_indices)}
