"""Existing HR normalization, isolated from Flask and legacy configuration.

Algorithm/helper bodies are copied mechanically from app.py; parity tests guard
against drift. Only config access is replaced with an explicit scoped policy.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re
import deception_check
import evidence_scoring

@dataclass(frozen=True)
class NormalizationPolicy:
    AI_RISK_DEDUCTION_MODE: str = 'readonly'
    DECEPTION_CHECK_MODE: str = 'shadow'
    DECEPTION_SHADOW_VERBOSE: bool = False

    def __post_init__(self):
        if (self.AI_RISK_DEDUCTION_MODE not in {'readonly', 'enforce'}
                or self.DECEPTION_CHECK_MODE not in {'off', 'shadow', 'on'}
                or type(self.DECEPTION_SHADOW_VERBOSE) is not bool):
            raise ValueError('Invalid explicit normalization policy')

_policy = ContextVar('hr_normalization_policy', default=NormalizationPolicy())


logger = logging.getLogger("resume-matcher")


_AI_RISK_RULES = {
    "none": (0, 0, "无AI痕迹", "简历真实自然"),
    "light": (5, 10, "轻微AI美化", "轻度AI润色，轻微包装"),
    "medium": (15, 20, "中度AI美化", "中度AI包装，真实性一般"),
    "high": (30, 30, "重度AI/模板生成", "重度AI生成，内容可信度低"),
}


_GATE_TIER_POLICY = {"A": (10, 30, 59), "B": (8, 24, 69), "C": (5, 15, 79)}


_OVERRIDE_CAPABILITY = 80


_OVERRIDE_CORE_MIN = 20


_UNCAPPABLE_CERT_TOKENS = (
    "注册会计师", "注会", "CPA",
    "一级建造师", "二级建造师", "建造师", "一建", "二建",
    "一级造价工程师", "二级造价工程师", "造价工程师", "一造", "二造",
    "注册消防工程师", "消防工程师", "一消", "二消",
    "执业医师", "执业药师", "执业护士", "注册护士",
    "教师资格", "教资", "法律职业资格", "司法考试", "法考", "律师执业",
    "注册建筑师", "注册结构工程师", "注册岩土", "注册电气工程师", "注册公用设备工程师",
    "环境影响评价工程师",
)


_MITIGABLE_CERT_EXCEPTIONS = (
    "CISP", "注册信息安全", "CISSP", "PMP", "ACCA", "CFA", "软考", "计算机等级",
)


_UNCAPPABLE_GATE_KEYWORDS = ("驾照", "驾驶证", "驾驶资格", "应届", "届别")


def _gate_is_mitigable(blocker: dict) -> bool:
    """这道门槛能不能由能力弥补？不能 → 不给"破格复核"（但仍照常显示阻塞项）。"""
    category = str(blocker.get("category") or "")
    text = str(blocker.get("text") or "")
    # 事实类（驾照/届别）最先判：无论模型把它归到哪一类，都不是能力问题
    if any(word in text for word in _UNCAPPABLE_GATE_KEYWORDS):
        return False
    if category == "certificate":
        if any(token in text for token in _MITIGABLE_CERT_EXCEPTIONS):
            return True
        if any(token in text for token in _UNCAPPABLE_CERT_TOKENS):
            return False
        # 口语写法兜底：写明"注册/执业/准入类"的证书按执业许可处理
        if "注册" in text or "执业" in text or "准入类" in text:
            return False
        return True
    # A 档学历层级、B 档院校层次/年限：都是能力的代理指标，代理失效时经验可以说话
    return True


def _as_int(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _short_list(value, limit: int = 3, item_limit: int = 90) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:item_limit] for item in value if str(item).strip()][:limit]


def _as_text(value, default: str = "未提供") -> str:
    text = str(value or "").strip()
    return text[:120] if text else default


def _as_section(value, fields: tuple[str, ...]) -> dict:
    source = value if isinstance(value, dict) else {}
    return {field: _as_text(source.get(field)) for field in fields}


def _normalize_month_label(value, *, allow_present: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return "未提供"
    if allow_present and text.lower() in {"至今", "目前", "在职", "present", "current", "now"}:
        return "至今"

    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*[-./年]\s*(1[0-2]|0?[1-9])(?:\s*月)?(?!\d)",
        text,
    )
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"

    year_match = re.fullmatch(r"\s*((?:19|20)\d{2})\s*年?\s*", text)
    if year_match:
        return year_match.group(1)
    return _as_text(text)


def _month_index(value: str, current: datetime) -> int | None:
    if value == "至今":
        return current.year * 12 + current.month - 1
    match = re.fullmatch(r"((?:19|20)\d{2})-(1[0-2]|0[1-9])", value)
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2)) - 1


def _month_label(index: int) -> str:
    year, zero_based_month = divmod(index, 12)
    return f"{year:04d}-{zero_based_month + 1:02d}"


def _month_duration(months: int) -> str:
    years, remainder = divmod(max(0, months), 12)
    if years and remainder:
        return f"{years} 年 {remainder} 个月"
    if years:
        return f"{years} 年"
    return f"{remainder} 个月"


def _normalize_employment_records(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    current = datetime.now()
    records: list[dict[str, str]] = []
    for item in value[:_EMPLOYMENT_RECORD_LIMIT]:
        if not isinstance(item, dict):
            continue
        company_name = item.get("company_name") or item.get("company")
        job_title = item.get("job_title") or item.get("position") or item.get("title")
        start_value = item.get("start_date") or item.get("start_time")
        end_value = item.get("end_date") or item.get("end_time")
        if not any((company_name, job_title, start_value, end_value)):
            continue

        start_date = _normalize_month_label(start_value)
        end_date = _normalize_month_label(end_value, allow_present=True)
        start_index = _month_index(start_date, current)
        end_index = _month_index(end_date, current)
        duration = "未提供"
        if start_index is not None and end_index is not None and start_index <= end_index:
            duration = _month_duration(end_index - start_index + 1)

        records.append(
            {
                "company_name": _as_text(company_name),
                "job_title": _as_text(job_title),
                "start_date": start_date,
                "end_date": end_date,
                "duration": duration,
            }
        )

    records.sort(
        key=lambda record: _month_index(record["start_date"], current) or -1,
        reverse=True,
    )
    return records


def _extract_employment_records(resume_content: str) -> list[dict[str, str]]:
    """Extract structured employment rows when the model omits them."""
    lines = [
        re.sub(r"[ \t\u3000]+", " ", line).strip()
        for line in str(resume_content or "").replace("\r\n", "\n").split("\n")
    ]
    lines = [line for line in lines if line]
    if not lines:
        return []

    section_start = next(
        (index for index, line in enumerate(lines) if line.rstrip("：:") in {"工作经历", "工作经验", "任职经历"}),
        -1,
    )
    section_end = len(lines)
    if section_start >= 0:
        for index in range(section_start + 1, len(lines)):
            if lines[index].rstrip("：:") in {
                "教育经历", "教育背景", "项目经历", "项目经验", "专业技能",
                "技能特长", "证书与荣誉", "荣誉奖项", "自我评价",
            }:
                section_end = index
                break
        searchable_lines = lines[section_start + 1:section_end]
    else:
        searchable_lines = lines

    extracted: list[dict[str, str]] = []
    for index, line in enumerate(searchable_lines):
        date_match = _EMPLOYMENT_DATE_RANGE_RE.search(line)
        if not date_match:
            continue

        header_candidates: list[tuple[int, str]] = []
        inline_header = line[:date_match.start()].strip(" |-—–~～·")
        if inline_header:
            header_candidates.append((index, inline_header))
        lookback_start = max(0, index - 6)
        header_candidates.extend(
            (candidate_index, searchable_lines[candidate_index])
            for candidate_index in range(index - 1, lookback_start - 1, -1)
        )

        company_name = ""
        job_title = ""
        for _candidate_index, candidate in header_candidates:
            header_match = _EMPLOYMENT_HEADER_RE.fullmatch(candidate)
            if header_match:
                company_name = header_match.group("company").strip()
                job_title = header_match.group("title").strip()
                break

        if not company_name:
            for candidate_index, candidate in header_candidates:
                company_match = _EMPLOYMENT_COMPANY_RE.fullmatch(candidate)
                if not company_match:
                    continue
                company_name = company_match.group("company").strip()
                nearby_lines = searchable_lines[candidate_index + 1:index]
                job_title = next(
                    (
                        nearby.strip()
                        for nearby in nearby_lines
                        if _looks_like_job_title(nearby)
                    ),
                    "未提供",
                )
                break

        if not company_name:
            continue
        extracted.append(
            {
                "company_name": company_name,
                "job_title": job_title or "未提供",
                "start_date": date_match.group("start"),
                "end_date": date_match.group("end"),
            }
        )

    return _normalize_employment_records(extracted)


def _looks_like_job_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.rstrip("：:") in {"内容", "职责", "业绩", "工作内容", "工作职责"}:
        return False
    return bool(re.search(r"工程师|经理|主管|总监|负责人|顾问|专员|助理|实习|设计师|开发|运维|销售|会计|教师", text))


def _merge_employment_records(
    extracted_records: list[dict[str, str]],
    model_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prefer resume text for factual fields, then add model-only periods."""
    merged = [dict(record) for record in extracted_records]
    keyed = {
        (record["start_date"], record["end_date"]): record
        for record in merged
        if record["start_date"] != "未提供" and record["end_date"] != "未提供"
    }
    for model_record in model_records:
        key = (model_record["start_date"], model_record["end_date"])
        existing = keyed.get(key)
        if existing:
            for field in ("company_name", "job_title"):
                if existing[field] == "未提供" and model_record[field] != "未提供":
                    existing[field] = model_record[field]
            continue
        merged.append(dict(model_record))
        if key[0] != "未提供" and key[1] != "未提供":
            keyed[key] = merged[-1]
    return _normalize_employment_records(merged)


def _find_employment_overlaps(records: list[dict[str, str]]) -> list[dict[str, str | int]]:
    """Find overlapping employment periods instead of silently merging them as gaps."""
    current = datetime.now()
    parsed = []
    for record in records:
        start = _month_index(record.get("start_date", ""), current)
        end = _month_index(record.get("end_date", ""), current)
        if start is None or end is None or start > end:
            continue
        parsed.append((start, end, record))
    overlaps = []
    for index, (start, end, record) in enumerate(parsed):
        for other_start, other_end, other in parsed[index + 1:]:
            overlap_start = max(start, other_start)
            overlap_end = min(end, other_end)
            # 日期是**月份精度**：两段区间端点相接（上一段 2026-10 结束、下一段 2026-10 开始，
            # 即同月离职并入职）是正常交接，不是重叠 —— 只有真正相交才算冲突。
            # 历史缺陷：这里写成 `<=`，把「2026-10 辞职 + 2026-10 入职」报成"任职时间重叠 1 个月"，
            # 前端 dashboard 的 EmploymentOverlapWarning 会误报（YG 2026-10-14 发现）。
            # 核验层 deception_check._overlap_pairs 一直是严格比较（"首尾相接不算重叠"），现已对齐。
            if overlap_start < overlap_end:
                overlaps.append({
                    "company_name": record.get("company_name", "未提供"),
                    "other_company_name": other.get("company_name", "未提供"),
                    "start_date": _month_label(overlap_start),
                    "end_date": _month_label(overlap_end),
                    "months": overlap_end - overlap_start + 1,
                    "message": f"{record.get('company_name', '未提供')} 与 {other.get('company_name', '未提供')} 任职时间重叠 {_month_duration(overlap_end - overlap_start + 1)}，建议人工核实",
                })
    return overlaps


def _calculate_employment_gaps(records: list[dict[str, str]], gap_note: str = "") -> str:
    """Calculate calendar-month gaps after merging overlapping employment periods."""
    if not records:
        return "未提供"

    current = datetime.now()
    current_index = current.year * 12 + current.month - 1
    intervals: list[tuple[int, int]] = []
    incomplete = False
    for record in records:
        start_index = _month_index(record["start_date"], current)
        end_index = _month_index(record["end_date"], current)
        if (
            start_index is None
            or end_index is None
            or start_index > end_index
            or start_index > current_index
            or end_index > current_index
        ):
            incomplete = True
            continue
        intervals.append((start_index, end_index))

    if incomplete:
        summary = "部分经历缺少精确月份，无法完整核算空窗期"
        if gap_note and gap_note != "未提供":
            summary += f"；简历说明：{gap_note}"
        return summary

    intervals.sort()
    merged: list[list[int]] = []
    for start_index, end_index in intervals:
        if not merged or start_index > merged[-1][1] + 1:
            merged.append([start_index, end_index])
        else:
            merged[-1][1] = max(merged[-1][1], end_index)

    gaps: list[tuple[int, int, int, bool]] = []
    for previous, following in zip(merged, merged[1:]):
        gap_start = previous[1] + 1
        gap_end = following[0] - 1
        if gap_start <= gap_end:
            gaps.append((gap_start, gap_end, gap_end - gap_start + 1, False))

    if merged[-1][1] < current_index:
        gap_start = merged[-1][1] + 1
        gaps.append((gap_start, current_index, current_index - gap_start + 1, True))

    if gaps:
        total_months = sum(gap[2] for gap in gaps)
        details = "；".join(
            f"{_month_label(start)} 至 {_month_label(end)}（{_month_duration(months)}{'，当前' if is_current else ''}）"
            for start, end, months, is_current in gaps
        )
        summary = f"共 {len(gaps)} 段，累计 {_month_duration(total_months)}：{details}"
    else:
        summary = f"无空窗期（任职时间连续，截至 {_month_label(current_index)}）"

    if gap_note and gap_note != "未提供":
        summary += f"；简历说明：{gap_note}"
    return summary


def _list_or_default(value, default: str, limit: int = 3) -> list[str]:
    return _short_list(value, limit) or [default]


def _normalize_skill_labels(value) -> list[str]:
    label_aliases = {
        "[直接匹配]": "[符合要求]",
        "[完全匹配]": "[符合要求]",
        "[可迁移]": "[相关经验]",
        "[部分匹配]": "[相关经验]",
        "[缺失]": "[简历未体现]",
    }
    items = _short_list(value, 5)
    normalized: list[str] = []
    for item in items:
        for old_label, new_label in label_aliases.items():
            if item.startswith(old_label):
                item = new_label + item[len(old_label):]
                break
        normalized.append(item)
    return normalized


_BASIC_SCREENING_FIELDS = (
    "native_place", "age", "gender", "work_location", "salary_expectation",
)


_WORK_HISTORY_FIELDS = (
    "total_years", "relevant_years", "industry_match", "company_background",
    "seniority", "team_size", "stability", "employment_gaps",
    "responsibility_match",
)


_EMPLOYMENT_RECORD_LIMIT = 12


_EMPLOYMENT_DATE_TOKEN = r"(?:19|20)\d{2}\s*[-./年]\s*(?:1[0-2]|0?[1-9])(?:\s*月)?"


_EMPLOYMENT_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_EMPLOYMENT_DATE_TOKEN})\s*(?:-|—|–|~|～|至)\s*"
    rf"(?P<end>至今|目前|在职|present|current|{_EMPLOYMENT_DATE_TOKEN})",
    re.IGNORECASE,
)


_EMPLOYMENT_COMPANY_SUFFIX = r"(?:有限责任公司|股份有限公司|有限公司|集团公司|集团|公司|研究院|事务所|中心|工作室|厂)"


_EMPLOYMENT_HEADER_RE = re.compile(
    rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})\s+(?P<title>.+)"
)


_EMPLOYMENT_COMPANY_RE = re.compile(rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})")


_SCORE_BREAKDOWN_LIMITS = {
    "hard_requirements": 25,
    "responsibility_overlap": 25,
    "skills_projects": 25,
    "industry_background": 15,
    "evidence_bonus": 10,
}


_CAPABILITY_EXCLUDED = ("hard_requirements",)


def _capability_score(breakdown: dict, complete: bool, base_score: int) -> tuple[int, str]:
    """能力分 = 剔除硬门槛分项后的质量分，按同一满分归一。

    返回 (分数, 口径)：`ex_gate`（已解耦）/ `total`（无分项可用，退回模型总分）。
    """
    if not complete:
        return base_score, "total"
    maximum = sum(v for k, v in _SCORE_BREAKDOWN_LIMITS.items() if k not in _CAPABILITY_EXCLUDED)
    got = sum(int(breakdown.get(k, 0)) for k in _SCORE_BREAKDOWN_LIMITS if k not in _CAPABILITY_EXCLUDED)
    if maximum <= 0:
        return base_score, "total"
    return max(0, min(100, round(100 * got / maximum))), "ex_gate"


def _capability_core_ok(breakdown: dict, complete: bool) -> bool:
    """破格复核的附加条件：岗位职责重合与技能项目**双双**达到各维度的 80%。

    只看总分会让行业背景+证据加分（占非门槛满分 1/3）把单维强项抬过线——
    那不是"经历很契合"，那是"有一项很强"。
    """
    if not complete:
        return False
    return all(int(breakdown.get(k, 0)) >= _OVERRIDE_CORE_MIN
               for k in ("responsibility_overlap", "skills_projects"))


def _normalize_education_history(raw) -> list[dict]:
    """Normalize education history entries, sorted by degree (博士 > 硕士 > 本科 > 专科)."""
    _DEGREE_ORDER = {"博士": 0, "硕士": 1, "本科": 2, "专科": 3, "大专": 3, "其他": 4}
    if not isinstance(raw, list) or not raw:
        return []
    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        degree = str(item.get("degree") or "未提供").strip()
        entry = {
            "degree": degree,
            "school_name": _as_text(item.get("school_name")),
            "school_tier": _as_text(item.get("school_tier")),
            "major": _as_text(item.get("major")),
            "graduation_year": _as_text(item.get("graduation_year")),
        }
        entries.append(entry)
    entries.sort(key=lambda e: _DEGREE_ORDER.get(e["degree"], 99))
    return entries[:8]


def _normalize_score_breakdown(value) -> tuple[dict[str, int], bool]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, int] = {}
    complete = True
    for field, maximum in _SCORE_BREAKDOWN_LIMITS.items():
        try:
            score = int(round(float(source.get(field))))
        except (TypeError, ValueError):
            score = 0
            complete = False
        normalized[field] = max(0, min(maximum, score))
    return normalized, complete


def _shadow_correlation_key(context: dict | None) -> str:
    """影子模式日志的关联码：不可逆短哈希，同一份分析可对上，但不泄漏简历 ID。

    DECEPTION_SHADOW_VERBOSE=1 时改写成可读的 resume/job/user，供本地定位用。
    """
    if not context:
        return "-"
    if _policy.get().DECEPTION_SHADOW_VERBOSE:
        return "resume=%s job=%s user=%s" % (
            context.get("resume_id") or "-",
            context.get("job_id") or "-",
            context.get("user_id") or "-",
        )
    seed = "%s|%s|%s" % (
        context.get("resume_id") or "",
        context.get("job_id") or "",
        context.get("user_id") or "",
    )
    if not seed.strip("|"):
        return "-"
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:10]


def _log_credibility_shadow(credibility: dict, context: dict | None = None) -> None:
    """影子模式日志：记录规则码、计数与不可逆关联码，不写简历 ID、不写简历正文。

    调阈值只需要两件事：分母（跑了多少次分析）与分子（哪条规则命中了多少次）。
    要看具体某一例就手工重跑那一份，不要把候选人信息散进日志。
    关联码用来把日志行和同一份简历的多条记录（或多个规则命中）串起来——
    没有它，“红标的到底是哪一份”只能靠时间戳瞎猜。需要真 ID 时开
    DECEPTION_SHADOW_VERBOSE=1（仅限本地排查），默认始终是哈希。
    注意：缓存命中的分析不会走到这里，所以这里的计数是“新增分析”的次数，不是总调用数。

    字段固定顺序、固定前缀（`credibility(mode=shadow)`），行内 `k=v` 用空格分隔——
    这样 grep/awk 就能直接算分子分母与命中率，不需要解析自由文本：
        grep -c 'credibility(mode=shadow)' app.log              # 分母
        grep -c 'codes=[^ ]*raw_tenure' app.log                 # 某条规则的分子
    `version` 必须记：规则改过之后命中率会变，不记版本就没法把两个版本的数据混在一起看。
    `enabled=false` 表示简历原文为空、一条规则都没跑（分母要把它排除掉，否则命中率偏低）。
    `findings=` 是去重前的规则命中条数，`codes=` 是命中的规则名（逗号分隔，便于 grep）。
    """
    counts = credibility.get("counts") or {}
    by_source = credibility.get("by_source") or {}
    findings = credibility.get("findings") or []
    codes = ",".join(sorted({str(item.get("code") or "") for item in findings}))
    logger.info(
        "credibility(mode=shadow) version=%s enabled=%s ref=%s checks=%s findings=%s "
        "red=%s review=%s info=%s resume_side=%s report_side=%s codes=%s",
        credibility.get("version", ""),
        "true" if credibility.get("enabled", True) else "false",
        _shadow_correlation_key(context),
        credibility.get("checks_run", 0),
        len(findings),
        counts.get("red_flag", 0),
        counts.get("review", 0),
        counts.get("info", 0),
        by_source.get("resume", 0),
        by_source.get("report", 0),
        codes or "-",
    )


_GATE_CATEGORIES = ("education", "experience", "certificate")


def _undetermined_summary(raw_items) -> list[dict]:
    """规范化"系统不参与判定的要求"清单：类型 + 原文 + 说明，限长限量。"""
    if not isinstance(raw_items, list):
        return []
    summary: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("type") or "").strip()[:20]
        text = str(item.get("text") or "").strip()[:120]
        if not label or not text or (label, text) in seen:
            continue
        seen.add((label, text))
        summary.append({
            "type": label,
            "text": text,
            "note": str(item.get("note") or "系统不参与该项判定，请人工确认")[:80],
        })
        if len(summary) >= 8:
            break
    return summary


def _gate_coverage_summary(raw_coverage: dict | None, checklist: list, hard_gate_fails: int) -> dict:
    """把"门槛到底跑没跑"变成可见数据：补了几条、纠正几条、跑了几条、几条不达标。

    JD 写着硬门槛却一条门槛都没参与判定时给出显式警告——这种情况以前是完全静默的：
    分数照给、报告照出，只有 HR 手动抽查才可能发现。
    """
    source = raw_coverage if isinstance(raw_coverage, dict) else {}
    gate_items = [
        item for item in (checklist or [])
        if isinstance(item, dict) and str(item.get("category") or "") in _GATE_CATEGORIES
    ]
    coverage = {
        "markers_found": int(source.get("markers_found") or 0),
        "backstop_added": int(source.get("added") or 0),
        "upgraded": int(source.get("upgraded") or 0),
        "gate_items": len(gate_items),
        "gate_failed": int(hard_gate_fails or 0),
        "warning": "",
    }
    if coverage["markers_found"] and not coverage["gate_items"]:
        coverage["warning"] = (
            "岗位描述写有硬性门槛，但本次没有任何门槛参与判定，请人工复核岗位要求是否被完整识别"
        )
    return coverage


_INCOMPLETE_REASON_FALLBACK = "分析未能生成有效报告"


def _mark_incomplete_analysis(result: dict, raw: dict) -> None:
    """分析根本没跑成时，别让 "0 分 / D级 / 淘汰" 冒充一个判定结论。

    现场：图片型 PDF 抽不出文字（`resume_sanitize` 返回空）→ 分析层抛“简历内容不能为空”
    → 兜底报告 `job_fit_score=0` → 服务端算出 0 分、D级、**淘汰**。
    候选人什么都没做错，却拿到一份"淘汰"结论，而且失败原因（validation_issues）根本没进 payload。
    这里做两件事：把原因带出来 + 把结论字段换成语义诚实的"无法评估"。
    """
    meta = raw.get("agent_meta") if isinstance(raw.get("agent_meta"), dict) else {}
    if not isinstance(meta, dict) or meta.get("validation_passed") is not False:
        return
    if raw.get("score_breakdown"):
        # 有分项就是真跑过（可能确实低分），不在此列
        return
    issues = [str(item) for item in (meta.get("validation_issues") or []) if str(item).strip()]
    result["analysis_incomplete"] = True
    result["analysis_incomplete_reason"] = "；".join(issues[:3]) or _INCOMPLETE_REASON_FALLBACK
    result["agent_meta"] = {
        "version": meta.get("version") or "",
        "validation_passed": False,
        "validation_issues": issues[:8],
        "llm_calls": meta.get("llm_calls", 0),
    }
    result["fit_grade"] = "无法评估"
    result["recruitment_recommendation"] = "待补充材料"
    result["fit_tag"] = "无法评估"


def _normalize_hr_analysis(
    raw: dict,
    job_content: str = "",
    resume_content: str = "",
    log_context: dict | None = None,
) -> dict:
    """Enforce the scoring formula and all allowed deduction/grade ranges."""
    evidence_score = None
    if raw.get("evidence_pipeline_version") == "evidence-v1":
        evidence_score = evidence_scoring.score_evidence(raw.get("evidence_assessments") or [])
        # Keep the LLM's numbers out of all downstream grade/capability decisions.
        raw = {**raw, "score_breakdown": evidence_score["score_breakdown"],
               "job_fit_score": evidence_score["job_fit_score"]}
    score_breakdown, has_complete_breakdown = _normalize_score_breakdown(raw.get("score_breakdown"))
    base_score = (
        sum(score_breakdown.values())
        if has_complete_breakdown
        else max(0, min(100, _as_int(raw.get("job_fit_score"))))
    )
    if evidence_score is not None:
        base_score = evidence_score["job_fit_score"]
    risk = str(raw.get("ai_risk") or "none").strip().lower()
    if risk not in _AI_RISK_RULES:
        risk = "none"

    minimum, maximum, risk_level, risk_label = _AI_RISK_RULES[risk]
    proposed = _as_int(raw.get("ai_deduction"), minimum)
    suggested = max(minimum, min(maximum, proposed))
    # 默认 readonly：AI 美化是模型的风格印象，没有可核对的对象，且实测同一份简历三次
    # 跑出 5/15/5 的摆动、足以跨过 60 分线改变最终动作。这里只把它作为风险提示留在报告里，
    # 判断权交回 HR；模型建议的分数仍记录在 ai_deduction_suggested。
    # 恢复旧行为：AI_RISK_DEDUCTION_MODE=enforce（apps/backend/.env）。
    deduction = suggested if _policy.get().AI_RISK_DEDUCTION_MODE == "enforce" else 0
    final_score = max(0, base_score - deduction)

    # 硬门槛确定性扣分（A/B/C 三档）：requirements_checklist 中的 not_met 才是确定性不达标，
    # 在 AI 美化扣分之后再次扣分，并按最严的一档封顶。
    # 三态口径：`manual_review`（简历未写明 / 真实性待核实）不在这里——它不扣分、不封顶。
    raw_checklist = raw.get("requirements_checklist") if isinstance(raw.get("requirements_checklist"), list) else []
    effective_by_id = {}
    if evidence_score is not None:
        # The audited evidence ledger is authoritative; model-only checklist
        # entries cannot create a hard-gate deduction after validation.
        raw_checklist = raw.get("evidence_assessments") if isinstance(raw.get("evidence_assessments"), list) else []
        effective_by_id = {
            (str(item.get("id") or ""), str(item.get("text") or "")): item.get("effective_status")
            for item in evidence_score["scoring_meta"]["items"]
        }
    tier_counts = {"A": 0, "B": 0, "C": 0}
    capped_tiers: set[str] = set()
    gate_blockers: list[dict] = []
    for item in raw_checklist:
        if not isinstance(item, dict):
            continue
        assessed_status = effective_by_id.get((str(item.get("id") or ""), str(item.get("text") or "")), item.get("status")) if evidence_score is not None else item.get("status")
        if assessed_status != "not_met":
            continue
        if evidence_score is not None and (not item.get("hard") or item.get("logic") == "preferred" or not item.get("explicit_negative")):
            continue
        tier = str(item.get("gate_tier") or "").strip().upper()
        if tier not in _GATE_TIER_POLICY:
            # 兼容没有 gate_tier 的旧数据：学历层级按 A，其余按 B（与分档前的行为一致）
            is_degree = (str(item.get("category")) == "education"
                         and str(item.get("gate_dimension") or "") != "tier")
            tier = "A" if is_degree else "B"
        tier_counts[tier] += 1
        exempt = bool(item.get("cap_exempt"))
        if not exempt:
            capped_tiers.add(tier)
        gate_blockers.append({
            "text": str(item.get("text") or ""),
            "category": str(item.get("category") or ""),
            "tier": tier,
            "evidence_label": str(item.get("evidence_label") or "已写明不符合"),
            "basis": str(item.get("resume_basis") or ""),
            "cap_exempt": exempt,
        })
    hard_gate_fails = sum(tier_counts.values())
    hard_gate_deduction = 0
    for tier, count in tier_counts.items():
        if not count:
            continue
        rate, limit, _cap = _GATE_TIER_POLICY[tier]
        hard_gate_deduction += min(limit, rate * count)
    final_score = max(0, final_score - hard_gate_deduction)
    gate_cap_applied = None
    if capped_tiers:
        gate_cap_applied = min(_GATE_TIER_POLICY[tier][2] for tier in capped_tiers)
        final_score = min(final_score, gate_cap_applied)

    if final_score >= 90:
        grade = "S级（优质适配）"
    elif final_score >= 80:
        grade = "A级（良好适配）"
    elif final_score >= 70:
        grade = "B级（基本适配）"
    elif final_score >= 60:
        grade = "C级（适配一般）"
    else:
        grade = "D级（不适配）"

    score_recommendation = "优先面试" if final_score >= 80 else "储备观察" if final_score >= 60 else "淘汰"
    requested_recommendation = str(raw.get("recruitment_recommendation") or score_recommendation).strip()
    if requested_recommendation == "酌情考虑":
        requested_recommendation = "储备观察"
    if requested_recommendation not in {"优先面试", "储备观察", "淘汰"}:
        requested_recommendation = score_recommendation
    recommendation_rank = {"淘汰": 0, "储备观察": 1, "优先面试": 2}
    if recommendation_rank[requested_recommendation] > recommendation_rank[score_recommendation]:
        requested_recommendation = score_recommendation

    # 破格复核：经历/技能确实契合（能力分解耦后 ≥80 且两个核心维度各达 80%），却被打到 60 以下时，
    # 系统不假装他达标（分数照封顶、适配标签照"不匹配"），但也不把他丢进"淘汰"堆里沉底。
    # 两个前置条件缺一不可：
    #   ① 能力分（剔除模型对硬门槛的打分后归一）够高；
    #   ② 造成封顶的门槛**都能由能力弥补**——缺注册会计师/一建这类法规执业资格、或驾照/届别这类
    #      事实性门槛时，能力再高也不给"破格复核"：那等于建议 HR 违规上岗。
    capability_score, capability_basis = _capability_score(
        score_breakdown, has_complete_breakdown, base_score
    )
    if evidence_score is not None:
        # Evidence score is already normalized over applicable JD dimensions;
        # fixed 75-point capability arithmetic would contradict that value.
        capability_score, capability_basis = base_score, "resume_jd_evidence_match"
        score_breakdown = evidence_score["scoring_meta"]["normalized_breakdown"]
    non_mitigable_blockers = [
        {"text": b["text"], "category": b["category"], "tier": b["tier"]}
        for b in gate_blockers
        if not _gate_is_mitigable(b)
    ]
    override_review = (
        bool(gate_blockers)
        and not non_mitigable_blockers
        and capability_score >= _OVERRIDE_CAPABILITY
        and _capability_core_ok(score_breakdown, has_complete_breakdown)
        and final_score < 60
    )
    if override_review:
        requested_recommendation = "破格复核"

    score_fit_tag = "高匹配" if final_score >= 80 else "部分匹配" if final_score >= 60 else "不匹配"
    requested_fit_tag = str(raw.get("fit_tag") or score_fit_tag).strip()
    if requested_fit_tag not in {"高匹配", "部分匹配", "不匹配"}:
        requested_fit_tag = score_fit_tag
    fit_rank = {"不匹配": 0, "部分匹配": 1, "高匹配": 2}
    if fit_rank[requested_fit_tag] > fit_rank[score_fit_tag]:
        requested_fit_tag = score_fit_tag

    basic_screening = _as_section(raw.get("basic_screening"), _BASIC_SCREENING_FIELDS)
    education_history = _normalize_education_history(raw.get("education_history"))
    raw_work_history = raw.get("work_history") if isinstance(raw.get("work_history"), dict) else {}
    work_history = _as_section(raw_work_history, _WORK_HISTORY_FIELDS)
    model_employment_records = _normalize_employment_records(raw_work_history.get("employment_records"))
    extracted_employment_records = _extract_employment_records(resume_content)
    employment_records = _merge_employment_records(
        extracted_employment_records,
        model_employment_records,
    )
    work_history["employment_records"] = employment_records
    work_history["employment_overlaps"] = _find_employment_overlaps(employment_records)
    if employment_records:
        gap_note = _as_text(raw_work_history.get("employment_gap_notes"))
        work_history["employment_gaps"] = _calculate_employment_gaps(employment_records, gap_note)
    raw_skill_match = raw.get("skill_match") if isinstance(raw.get("skill_match"), dict) else {}
    skill_match = {
        "hard_skills": _normalize_skill_labels(raw_skill_match.get("hard_skills")) or ["[简历未体现] 未找到可确认的岗位核心技能证据。"],
        "project_match_points": _list_or_default(raw_skill_match.get("project_match_points"), "简历未提供可与 JD 直接对应的项目或工作成果。", 5),
        "soft_skills": _list_or_default(raw_skill_match.get("soft_skills"), "简历未提供可确认的软实力证据。", 3),
    }

    risk_points = _short_list(raw.get("risk_points"), 6)
    if not re.search(r"薪资|薪酬|工资|预算|待遇|\d+\s*[kK]", job_content):
        risk_points = [
            item for item in risk_points
            if not re.search(r"薪资|薪酬|工资|预算|待遇", item)
        ]

    summary = str(raw.get("summary") or "未提供综合判定说明。").strip()[:240]
    # 证据模式会用通用说明覆盖 summary；先保留模型的一句话综合判定供「核心判定」展示。
    core_verdict = summary
    agent_trace = raw.get("agent_trace") if isinstance(raw.get("agent_trace"), dict) else None
    agent_validation = raw.get("agent_validation") if isinstance(raw.get("agent_validation"), dict) else None
    requirements_checklist = raw_checklist if isinstance(raw_checklist, list) else []
    if evidence_score is not None and isinstance(raw.get("evidence_assessments"), list):
        requirements_checklist = [
            {**item, "effective_status": effective_by_id.get((str(item.get("id") or ""), str(item.get("text") or "")), item.get("status"))}
            for item in raw.get("evidence_assessments") if isinstance(item, dict)
        ]
    result = {
        "candidate_name": _as_text(raw.get("candidate_name")),
        "final_score": final_score,
        "fit_grade": grade,
        "hard_gate_deduction": hard_gate_deduction,
        # 能力分与门槛后分数分开呈现：否则"经历很契合、只卡学历"的候选人会以 59 分沉底，
        # HR 永远看不到他的 88。capability_basis 说明这个数是解耦过的还是退回模型总分。
        "capability_score": capability_score,
        "capability_basis": capability_basis,
        "gate_blockers": gate_blockers,
        "gate_tier_counts": tier_counts,
        "gate_cap_applied": gate_cap_applied,
        "override_review": override_review,
        # 造成封顶、但**不能由能力弥补**的门槛（法规执业资格/驾照/届别）：即使能力分很高也不给
        # "破格复核"，前端据此说明"为什么不建议破格"，避免 HR 以为系统漏标了。
        "non_mitigable_blockers": non_mitigable_blockers,
        "gate_coverage": _gate_coverage_summary(raw.get("gate_coverage"), requirements_checklist, hard_gate_fails),
        "undetermined_requirements": _undetermined_summary(raw.get("undetermined_requirements")),
        "job_fit_score": base_score,
        "job_fit_percentage": base_score,
        "score_breakdown": score_breakdown,
        "ai_risk": risk,
        "ai_risk_level": risk_level,
        "ai_risk_label": risk_label,
        "ai_deduction": deduction,
        "ai_deduction_suggested": suggested,
        "ai_risk_note": (
            "疑似 AI 包装，仅作风险提示，未扣分（判断权交给 HR）"
            if deduction == 0 and suggested > 0
            else ""
        ),
        "summary": summary,
        "core_verdict": core_verdict,
        "basic_screening": basic_screening,
        "education_history": education_history,
        "work_history": work_history,
        "skill_match": skill_match,
        "certificates": _list_or_default(raw.get("certificates"), "简历未提供证书资质信息。", 5),
        "bonus_items": _list_or_default(raw.get("bonus_items"), "简历未提供明确加分项。", 5),
        "strengths": _list_or_default(raw.get("strengths"), "简历未提供可确认的核心优势证据。", 5),
        "weaknesses": _list_or_default(raw.get("weaknesses"), "简历未提供足够信息，无法确认关键短板。", 5),
        "risk_points": risk_points or ["未发现明显履历风险；关键事实仍建议在面试中核验。"],
        "role_specific_assessment": _list_or_default(raw.get("role_specific_assessment"), "当前岗位无额外专项判断。", 4),
        "requirements_checklist": requirements_checklist,
        "deduction_reasons": _short_list(raw.get("deduction_reasons"), 3),
        "recruitment_recommendation": requested_recommendation,
        "fit_tag": requested_fit_tag,
        "agent_trace": agent_trace,
        "agent_validation": agent_validation,
        # 结构校验与语义自校是两条独立证据，不能只透传自校而丢掉结构失败。
        "agent_meta": dict(raw.get("agent_meta") or {}) if isinstance(raw.get("agent_meta"), dict) else {},
    }
    if evidence_score is not None:
        result["scoring_meta"] = evidence_score["scoring_meta"]
        result["evidence_assessments"] = [
            {**item, "effective_status": effective_by_id.get((str(item.get("id") or ""), str(item.get("text") or "")), item.get("status"))}
            for item in (raw.get("evidence_assessments") or []) if isinstance(item, dict)
        ]
        # Highlights and unresolved items use the audited effective status, not
        # the model's pre-audit status.
        ledger = result["evidence_assessments"]
        strength_rows = strength_rows_from_ledger(ledger)
        result["strength_evidence"] = strength_rows
        # 兼容旧消费方：strengths 保留纯文本（状态由 strength_evidence 表达），不再拼接“原文支持：”前缀。
        result["strengths"] = [
            f"{row['text']}。{row['quote']}" if row["quote"] else row["text"]
            for row in strength_rows
        ][:5] or ["暂未找到与本次岗位要求对应的有效原文证据。"]
        weakness_rows = weakness_rows_from_ledger(ledger)
        result["weakness_evidence"] = weakness_rows
        result["weaknesses"] = [
            f"{row['text']}。{row['reason']}" if row["reason"] else row["text"]
            for row in weakness_rows
        ][:5] or ["本次逐项核对未发现明确不符合或待补充的要求。"]
        result["recruitment_recommendation"] = score_recommendation
        result["summary"] = "评分仅衡量简历原文对本次岗位要求的证据匹配程度；未体现项不获得匹配分，但不代表候选人不具备，真实性与能力程度仍需核验。"
        if not gate_blockers and (not evidence_score["scoring_meta"].get("decision_ready") or (final_score < 60 and evidence_score["scoring_meta"].get("unknown_ratio", 0) > 0)):
            result["recruitment_recommendation"] = "待补充材料"
            result["fit_grade"] = "待确认"
            result["fit_tag"] = "待确认"
        if evidence_score["scoring_meta"].get("incomplete"):
            result["analysis_incomplete"] = True
            result["analysis_incomplete_reason"] = "简历原文未提供足够匹配证据，请补充材料；不能据此推断候选人不具备相关能力。"

    # 三档招聘建议始终按最终分数给出，不因“证据不足”而消失；证据状态由其他字段表达。
    result["hiring_recommendation"] = score_recommendation
    _mark_incomplete_analysis(result, raw)

    # 简历文案核验（防"文案骗人"）：只读字段，绝不参与评分——
    # 不写 score_breakdown、不写 requirements_checklist，因此不可能触发硬门槛扣分或封顶。
    check_mode = _policy.get().DECEPTION_CHECK_MODE
    if check_mode != "off":
        try:
            credibility = deception_check.check_resume_deception(
                resume_content=resume_content,
                job_content=job_content,
                analysis=result,
            )
            if check_mode == "on":
                result["credibility"] = credibility
            else:
                # 影子模式：只写日志。不挂到 result 上 → 也就不会进缓存，
                # 将来切到 on 时不会突然冒出影子期那批没人复核过的旧判定。
                _log_credibility_shadow(credibility, log_context)
        except Exception:  # 核验失败绝不能影响分析主流程
            logger.warning("resume deception check failed", exc_info=True)

    return result


def strength_rows_from_ledger(ledger):
    """结构化亮点：状态与原文分离，旧字段 strengths 只保留纯文本。"""
    rows = []
    for item in ledger:
        status = item.get("effective_status") or item.get("status")
        quote = (item.get("source_quote")
                 or (item.get("supported_components") or [{}])[0].get("source_quote", ""))
        if status == "met" or item.get("supported_components"):
            rows.append({
                "requirement_id": str(item.get("id") or ""),
                "status": "met" if status == "met" else "partial",
                "text": item.get("text", ""),
                "quote": quote or "",
            })
    return rows[:5]


def weakness_rows_from_ledger(ledger):
    rows = []
    for item in ledger:
        status = item.get("effective_status") or item.get("status")
        if status == "met":
            continue
        rows.append({
            "requirement_id": str(item.get("id") or ""),
            "status": status,
            "text": item.get("text", ""),
            "reason": item.get("reason", ""),
        })
    return rows[:5]


def normalize_hr_analysis(raw, job_content='', resume_content='', log_context=None, *, policy=NormalizationPolicy()):
    if not isinstance(policy, NormalizationPolicy):
        raise TypeError('Explicit normalization policy required')
    token = _policy.set(policy)
    try:
        return _normalize_hr_analysis(raw, job_content, resume_content, log_context)
    finally:
        _policy.reset(token)
