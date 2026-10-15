"""Pure, deterministic scoring of evidence assessments; no app/config/LLM imports.

Contract v3:
* Fixed raw dimension budgets (25/25/25/15/10). When both lanes exist, required
  receives 80% and preferred 20%; a sole lane receives the full dimension budget.
  Missing JD dimensions receive zero raw points and are excluded from the
  denominator. job_fit_score, supported_score and score_range normalize the
  applicable budgets to 100; score_breakdown retains the five raw budgets.
* required_components [{id, text}] define equal-weight subconditions;
  supported_components [{id, text, source_quote, kind}] support only matching
  ids with non-placeholder quotes verified against the resume by the caller.
  direct/alias support any component; related supports practice only, never
  proficiency. Manual review retains its status but gains a supported lower
  proportion. Unknown components earn no matching points and imply no absence.
* Each lane uses equal-weight units after deduplication. Only a nonempty explicit
  alternative_group creates an OR unit within a lane and dimension. Ungrouped
  alternatives and preferences remain independent units. Exact normalized
  texts/ids are deduped. Semantic paraphrases and requirement splitting MUST be
  normalized by the caller; this module cannot identify semantic equivalence.
* All scores count only supported resume/JD matching evidence. No inferred or
  provisional points are awarded. score_range is the current calculated score
  (min=max), never an ability or potential-score interval. Unknown coverage is
  reported separately. decision_ready requires assessed inputs, not a potential
  upper bound. It declares neither candidate ability nor evidence authenticity;
  hard-gate decisions and quote provenance verification belong to the caller.
* A quote is necessary, not proof of provenance. Callers must verify quotes
  against the resume. Negative evidence must contain an explicit negative
  phrase in the quoted text; caller-supplied polarity cannot establish mismatch.
* Confidence and any caller/model scores never affect arithmetic. Related
  evidence never proves proficiency. This module applies no gate cap/deduction.

Integer allocation uses exact fractions and largest remainders (dimension order
breaks ties), so all output points are bounded integers and totals reconcile.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from fractions import Fraction

DIMENSION_LIMITS = {
    "hard_requirements": 25,
    "responsibility_overlap": 25,
    "skills_projects": 25,
    "industry_background": 15,
    "evidence_bonus": 10,
}
_CATEGORY_DIMENSION = {
    "education": "hard_requirements", "degree": "hard_requirements",
    "experience": "hard_requirements", "experience_years": "hard_requirements",
    "years": "hard_requirements", "certificate": "hard_requirements",
    "certification": "hard_requirements", "qualification": "hard_requirements",
    "学历": "hard_requirements", "年限": "hard_requirements", "证书": "hard_requirements",
    "responsibility": "responsibility_overlap", "responsibilities": "responsibility_overlap",
    "职责": "responsibility_overlap", "skill": "skills_projects",
    "skills": "skills_projects", "project": "skills_projects", "tool": "skills_projects",
    "技能": "skills_projects", "项目": "skills_projects",
    "industry": "industry_background", "background": "industry_background",
    "行业": "industry_background", "achievement": "evidence_bonus",
    "evidence": "evidence_bonus", "成果": "evidence_bonus",
}
_PLACEHOLDERS = {"", "未提供", "未提及", "简历未体现", "无", "未知", "不详",
                 "none", "null", "n/a", "na", "unknown", "not mentioned", "not provided"}
_NEGATIVE = re.compile(r"未(?:取得|获得|通过|达到|满足|持有|掌握|使用|接触)|不(?:具备|符合|满足|会|熟悉|足)|没有|缺乏|无(?:经验|证书|相关)|\b(?:no|not|never|without|lack|lacks|unable)\b", re.I)
_STATUSES = {"met", "not_met", "manual_review", "not_mentioned"}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().split())


def _real_quote(value):
    quote = _text(value)
    return bool(quote and _norm(quote).strip(" .。:：-—") not in _PLACEHOLDERS
                and any(c.isalnum() for c in quote))


def _quotes(item):
    positive, negative = [], []
    quote = _text(item.get("source_quote"))
    if _real_quote(quote):
        positive.append(quote)
        if _NEGATIVE.search(quote):
            negative.append(quote)
    evidence = item.get("evidence")
    if not isinstance(evidence, list) and isinstance(item.get("source_quotes"), list):
        evidence = item.get("source_quotes")
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, str):
                quote, polarity = entry, ""
            elif isinstance(entry, Mapping):
                quote = _text(entry.get("quote") or entry.get("source_quote"))
                polarity = _norm(entry.get("polarity"))
            else:
                continue
            if _real_quote(quote):
                positive.append(quote)
                # Polarity is metadata, not proof. Only explicit negative language
                # in the quoted resume text may establish a non-match.
                if _NEGATIVE.search(quote):
                    negative.append(quote)
    return sorted(set(positive)), sorted(set(negative))


_PROFICIENCY = re.compile(r"熟练|精通|熟悉|掌握|精深|深入|\b(?:proficiency|proficient|expert|mastery|master|advanced)\b", re.I)
_PRACTICE = re.compile(r"实践|使用|参与|开发|实施|操作|实操|项目|\b(?:practice|usage|use|used|implementation|hands.on|project|experience)\b", re.I)


def _components(item):
    required = {}
    rows = item.get("required_components")
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and _text(row.get("id")):
            cid, text = _text(row.get("id")), _text(row.get("text"))
            required.setdefault(cid, set()).add(text)
    # Conflicting descriptions of an id must not make related clues prove ability.
    required_rows = [{"id": cid, "text": " / ".join(sorted(texts))}
                     for cid, texts in sorted(required.items())]
    accepted = {}
    rows = item.get("supported_components")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        cid, kind = _text(row.get("id")), _norm(row.get("kind"))
        quote = _text(row.get("source_quote"))
        if cid not in required or not _real_quote(quote) or kind not in {"direct", "alias", "related"}:
            continue
        description = cid + " " + " ".join(required[cid])
        if kind == "related" and (_PROFICIENCY.search(description) or not _PRACTICE.search(description)):
            continue
        key = (cid, kind, quote)
        support = {"id": cid, "text": _text(row.get("text")), "source_quote": quote, "kind": kind}
        if key not in accepted or support["text"] < accepted[key]["text"]:
            accepted[key] = support
    supported_rows = [accepted[key] for key in sorted(accepted)]
    return required_rows, supported_rows


def _prepare(item):
    text = _text(item.get("text"))
    category = _norm(item.get("category")) or "uncategorized"
    explicit_dimension = _text(item.get("dimension"))
    dimension = explicit_dimension if explicit_dimension in DIMENSION_LIMITS else _CATEGORY_DIMENSION.get(category)
    if dimension is None:
        dimension = "hard_requirements" if item.get("hard") is True else "skills_projects"
    logic = _norm(item.get("logic"))
    logic = logic if logic in {"required", "preferred", "alternative"} else "required"
    status = _norm(item.get("status"))
    status = status if status in _STATUSES else "manual_review"
    kind = _norm(item.get("evidence_kind")) or "direct"
    quotes, negative = _quotes(item)
    effective, reason = status, "assessed"
    if status == "met" and not quotes:
        effective, reason = "manual_review", "met_without_quote"
    elif status == "met" and kind not in {"direct", "alias"}:
        effective, reason = "manual_review", "related_is_not_proficiency"
    elif status == "not_met" and (not negative or kind != "direct"):
        effective, reason = "manual_review", "not_met_without_direct_counterevidence"
    required_components, supported_components = _components(item)
    supported_ids = {c["id"] for c in supported_components}
    if effective == "met" and required_components and len(supported_ids) < len(required_components):
        effective, reason = "manual_review", "components_not_fully_supported"
    return {
        "required_components": required_components, "supported_components": supported_components,
        "id": _text(item.get("id")), "text": text, "category": category,
        "dimension": dimension, "logic": logic,
        "alternative_group": _text(item.get("alternative_group")),
        "hard": item.get("hard") is True and logic == "required",
        "status": status, "effective_status": effective, "reason": reason,
        "evidence_kind": kind, "source_quotes": quotes,
        "resume_basis": _text(item.get("resume_basis")),
    }


def _deduplicate(items):
    """Connected components: equal id OR normalized text, including id/text bridges."""
    records = [_prepare(i) for i in items if isinstance(i, Mapping)]
    parents = list(range(len(records)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen = {}
    for i, record in enumerate(records):
        for key in (("id", record["id"]), ("text", _norm(record["text"]))):
            if not key[1]:
                continue
            if key in seen:
                parents[root(i)] = root(seen[key])
            seen[key] = i
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(root(i), []).append(record)
    result = []
    for group in groups.values():
        group.sort(key=lambda r: tuple(str(r[k]) for k in sorted(r)))
        chosen = dict(group[0])
        # Conflicting duplicates must not let input order or cherry-picking decide.
        signatures = {(r["dimension"], r["logic"], r["category"], r["hard"],
                       r["alternative_group"], r["effective_status"]) for r in group}
        if len(signatures) > 1:
            chosen["effective_status"] = "manual_review"
            chosen["reason"] = "conflicting_duplicates"
        component_definitions = {tuple((c["id"], c["text"]) for c in r["required_components"]) for r in group}
        if len(component_definitions) == 1:
            supports = {(c["id"], c["kind"], c["source_quote"]): c
                        for r in group for c in r["supported_components"]}
            chosen["supported_components"] = [supports[key] for key in sorted(supports)]
        else:
            chosen["supported_components"] = []
            chosen["effective_status"] = "manual_review"
            chosen["reason"] = "conflicting_component_definitions"
        if not chosen["id"]:
            chosen["id"] = "text_" + hashlib.sha256(_norm(chosen["text"]).encode()).hexdigest()[:16]
        result.append(chosen)
    return sorted(result, key=lambda r: (r["dimension"], r["category"], _norm(r["text"]), r["id"]))


def _round(value):
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def _allocate(values):
    """Round a vector without inventing/discarding points in its sum."""
    floors = [v.numerator // v.denominator for v in values]
    remaining = _round(sum(values, Fraction())) - sum(floors)
    order = sorted(range(len(values)), key=lambda i: (-(values[i] - floors[i]), i))
    for i in order[:remaining]:
        floors[i] += 1
    return floors


def _interval(record):
    status = record["effective_status"]
    if status == "met":
        return Fraction(1), Fraction(1)
    if status == "not_met":
        return Fraction(0), Fraction(0)
    components = record["required_components"]
    if components and record["reason"] not in {"conflicting_duplicates", "conflicting_component_definitions"}:
        supported = {c["id"] for c in record["supported_components"]}
        return Fraction(len(supported), len(components)), Fraction(1)
    return Fraction(0), Fraction(1)


def _units(records):
    units, alternatives = [], {}
    for record in records:
        if record["alternative_group"]:
            alternatives.setdefault(record["alternative_group"], []).append(record)
        else:
            units.append([record])
    return units + list(alternatives.values())


def _lane(records):
    """Supported share and unassessed share; the latter never earns points."""
    units = _units(records)
    if not units:
        return Fraction(0), Fraction(0)
    support, unknown = Fraction(), Fraction()
    for unit in units:
        intervals = [_interval(r) for r in unit]
        low, high = max(x[0] for x in intervals), max(x[1] for x in intervals)
        support += low
        unknown += high - low
    return support / len(units), unknown / len(units)


def score_evidence(items) -> dict:
    """Return legacy five-dimensional scores plus a JSON-safe explanatory ledger.

    Accept an iterable of assessment mappings (None means no assessments). Other
    top-level values raise TypeError rather than silently fabricating a score.
    Unknown/malformed individual assessments are retained as manual review.
    """
    if items is None:
        items = []
    if isinstance(items, (str, bytes, Mapping)):
        raise TypeError("items must be an iterable of evidence assessment mappings")
    records = _deduplicate(items)
    dimensions, raw_supported, raw_unknown = {}, [], []
    for dimension, maximum in DIMENSION_LIMITS.items():
        selected = [r for r in records if r["dimension"] == dimension]
        required = [r for r in selected if r["logic"] != "preferred"]
        preferred = [r for r in selected if r["logic"] == "preferred"]
        req_low, req_unknown = _lane(required)
        pref_low, pref_unknown = _lane(preferred)
        req_weight = Fraction(4, 5) if preferred and required else Fraction(bool(required))
        pref_weight = Fraction(1, 5) if preferred and required else Fraction(bool(preferred))
        low = maximum * (req_weight * req_low + pref_weight * pref_low)
        unknown = maximum * (req_weight * req_unknown + pref_weight * pref_unknown)
        raw_supported.append(low)
        raw_unknown.append(unknown)
        dimensions[dimension] = {
            "maximum": maximum, "applicable": bool(selected),
            "required_weight": float(maximum * req_weight),
            "preferred_weight": float(maximum * pref_weight),
            "unknown_ratio": float(unknown / maximum),
            "unassessed_lanes": [name for name, rows in (("required", required), ("preferred", preferred)) if not rows],
        }
    points = _allocate(raw_supported)
    breakdown = dict(zip(DIMENSION_LIMITS, points))
    applicable_max = sum(DIMENSION_LIMITS[d] for d in dimensions if dimensions[d]["applicable"])
    scale = Fraction(100, applicable_max) if applicable_max else Fraction(0)
    normalized_points = _allocate([v * scale for v in raw_supported])
    for i, dim in enumerate(DIMENSION_LIMITS):
        dimensions[dim].update(score=points[i], supported_score=points[i],
                               confirmable_max=points[i], score_range={"min": points[i], "max": points[i]})
    ledger = []
    for record in records:
        dim = record["dimension"]
        preferred = record["logic"] == "preferred"
        lane_weight = Fraction(str(dimensions[dim]["preferred_weight" if preferred else "required_weight"]))
        peers = [r for r in records if r["dimension"] == dim and (r["logic"] == "preferred") == preferred]
        units = _units(peers)
        weight = lane_weight / len(units)
        low, high = _interval(record)
        ledger.append({**record, "dimensions": [dim], "lane_weight": float(lane_weight),
                       "weight": float(weight),
                       "supported_ratio": float(low), "unknown_ratio": float(high - low),
                       "score": _round(weight * low),
                       "confirmable_max": _round(weight * low),
                       "additive": False,
                       "score_semantics": "rounded_unit_support_not_independently_sumable; dimension_ledger_is_authoritative",
                       "aggregation": "explicit_group_or_then_equal_mean" if record["alternative_group"] else "equal_mean"})
    # Review state is independent of current-score arithmetic and future potential.
    unresolved = any(r["effective_status"] in {"manual_review", "not_mentioned"} for r in records)
    assessed = any(r["effective_status"] in {"met", "not_met"} or _interval(r)[0] > 0 for r in records)
    incomplete = not assessed
    provisional = unresolved or incomplete  # Compatibility: review pending, never extra points.
    supported_score = sum(normalized_points)
    base_score = supported_score
    decision_ready = not incomplete and not unresolved
    return {
        "score_breakdown": breakdown,
        "job_fit_score": base_score,
        "scoring_meta": {
            "version": "evidence_v3", "score_source": "server_deterministic_evidence",
            "applicable_max": applicable_max, "raw_points": sum(points),
            "raw_supported_points": sum(points),
            "raw_score_range": {"min": sum(points), "max": sum(points)},
            "normalized_breakdown": dict(zip(DIMENSION_LIMITS, normalized_points)),
            "score_semantics": "resume_jd_evidence_match",
            "inferred_points": 0,
            "base_score": base_score, "supported_score": supported_score,
            "provisional_points": 0,
            "score_range": {"min": supported_score, "max": supported_score},
            "score_range_semantics": "current_calculated_match_score_not_ability_interval",
            "ability_claim": False, "authenticity_claim": False,
            "incomplete": incomplete, "analysis_incomplete": incomplete,
            "provisional": provisional, "decision_ready": decision_ready,
            "decision_thresholds": [60, 80],
            "evidence_coverage": float(Fraction(sum(1 for r in records if r["effective_status"] in {"met", "not_met"}), len(records))) if records else 0.0,
            "unknown_ratio": float(sum(raw_unknown, Fraction()) / applicable_max) if applicable_max else 0.0,
            "normalization": "applicable_dimensions_to_100_both_lanes_80_20_sole_lane_100_raw_breakdown_fixed_budgets",
            "component_contract": "matching_required_id_and_caller_verified_quote; direct_alias_any; related_practice_only; equal_component_weights",
            "aggregation": "lane_equal_mean_explicit_alternative_group_or_only",
            "semantic_deduplication": "caller_normalizes_paraphrases_and_requirement_splitting",
            "rounding": "exact_fraction_largest_remainder_dimension_order",
            "dimensions": dimensions, "items": ledger,
            "confirmed_noncompliance": [r["id"] for r in records if r["effective_status"] == "not_met" and r["logic"] != "preferred"],
            "hard_gate_deduction": 0, "gate_cap_applied": None,
            "decision_note": "Current resume/JD text-evidence match only; no ability or authenticity claim. Unknown means no matching points, not noncompliance. Pending review is not an automatic rejection; explicit hard-gate rules belong to the caller.",
            "decision_ready_semantics": "all_assessments_resolved_for_text_evidence_match_only",
        },
    }
