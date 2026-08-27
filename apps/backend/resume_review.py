"""Create zero-token resume review markers from existing analysis and raw text."""
from __future__ import annotations

import re
from typing import Any

_MAX_ANNOTATIONS = 18
_MAX_QUOTE_LENGTH = 260


def _terms(text: str) -> list[str]:
    return [item.lower() for item in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#.-]{1,}", str(text or ""))]


def _find_quote(content: str, text: str) -> tuple[int, int] | None:
    if not text:
        return None
    start = content.find(text)
    if start >= 0:
        return start, start + len(text)
    wanted = [term for term in _terms(text) if len(term) > 1]
    if not wanted:
        return None
    offset = 0
    for line in content.splitlines(keepends=True):
        lower = line.lower()
        if sum(term in lower for term in wanted) >= max(1, min(2, len(wanted))):
            clean = line.strip()
            start = content.find(clean, offset)
            if start >= 0:
                return start, start + len(clean)
        offset += len(line)
    return None


def _add_annotation(items: list[dict], content: str, category: str, label: str, reason: str, candidates: list[str], confidence: str = "medium") -> None:
    for candidate in candidates:
        quote = str(candidate or "").strip().strip("-•")
        if not quote:
            continue
        quote = quote[:_MAX_QUOTE_LENGTH]
        span = _find_quote(content, quote)
        if not span:
            continue
        if any(item["start"] == span[0] and item["end"] == span[1] for item in items):
            continue
        items.append({"id": f"ann-{len(items) + 1}", "category": category, "label": label, "quote": content[span[0]:span[1]], "reason": reason, "start": span[0], "end": span[1], "confidence": confidence})
        if len(items) >= _MAX_ANNOTATIONS:
            return


def build_review_markers(content: str, analysis: dict[str, Any], *, candidate_name: str = "候选人") -> dict:
    """Build annotations only from quotes that can be found in the original text."""
    content = str(content or "")
    annotations: list[dict] = []
    _add_annotation(annotations, content, "strength", "匹配亮点", "对应岗位匹配亮点", analysis.get("strengths") or [])
    skill_match = analysis.get("skill_match") if isinstance(analysis.get("skill_match"), dict) else {}
    _add_annotation(annotations, content, "match", "项目匹配点", "对应岗位职责或项目要求", skill_match.get("project_match_points") or [])
    _add_annotation(annotations, content, "match", "技能匹配", "对应岗位技能要求", skill_match.get("hard_skills") or [])
    _add_annotation(annotations, content, "risk", "待核实信息", "需要在面试或材料复核中进一步确认", analysis.get("risk_points") or [], "medium")
    return {"candidate_name": candidate_name or "候选人", "annotations": annotations[:_MAX_ANNOTATIONS], "summary": {"final_score": analysis.get("final_score", 0), "recommendation": analysis.get("recruitment_recommendation", "储备观察"), "highlights": sum(1 for item in annotations if item["category"] in {"strength", "match"}), "risks": sum(1 for item in annotations if item["category"] == "risk")}, "notice": "标记仅用于辅助审阅，原简历内容未被修改。"}
