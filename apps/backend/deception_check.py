"""简历文案核验：把简历/报告里的“可证伪声明”和结构与原文逐条对账。

设计原则（与项目既有风格一致）：
- 零依赖、零 LLM、零网络、零额外成本：纯标准库确定性计算。
- 不指控、只提示：产出全部是“需要追问或人工核实”的线索，绝不断言造假。
- 证据优先：每条结论必须附原文或结构依据；拿不到依据就不产出（宁缺毋滥）。
- 只读：不参与评分、不写 requirements_checklist，绝不影响 final_score。
- 自保：内部逐规则 try/except，单条规则异常不影响其余规则，也不影响分析主流程。

词汇选择：对外不用“造假/虚假”，统一“与结构不一致 / 原文未检索到 / 待核实”。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

CREDIBILITY_VERSION = "deception-check-v3"

# 严重度：red_flag=结构性矛盾（建议面试必问）；review=需要人工确认；info=仅供了解
_LEVEL_RED = "red_flag"
_LEVEL_REVIEW = "review"

_MAX_FINDINGS = 12
_MAX_EVIDENCE = 220
_MAX_QUOTE = 200

# 简历与 JD 文本的高度重合判定阈值（8 字连续片段重合比例）
_JD_COPY_RATIO = 0.12
_JD_COPY_MIN_HITS = 15
_JD_COPY_NGRAM = 8
_JD_COPY_MIN_LEN = 200

# 只在“正向成果声明”字段里找数字，负向字段（短板/风险）不查，避免自证噪声
_CLAIM_PATHS: tuple[tuple[str, ...], ...] = (
    ("summary",),
    ("strengths",),
    ("skill_match", "project_match_points"),
    ("bonus_items",),
    ("role_specific_assessment",),
)

# 量化指标的“量词”：只查成果指标，刻意排除 年/月/周/天/小时——
# 年限和时长是服务端基于日期推导出来的，不在原文出现是正常的，查了必然误报。
_METRIC_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|％|倍|万|亿|人次|人|名|个|台|件|次)"
)
_METRIC_FREE_UNITS = {"%", "％", "倍", "万", "亿"}
_METRIC_MIN_VALUE = 10  # 无单位强度的量词要求数值足够大才提示

# 空泛话术（不可证伪表述）
_PUFF_RE = re.compile(
    r"丰富|较强|良好|出色|优秀|扎实|深厚|全面|高效|显著|极大|充分|卓越|突出"
    r"|敏锐|娴熟|精通|熟练掌握|能力突出|沟通能力|团队协作|责任心|抗压"
)
_NUMBER_RE = re.compile(r"\d")

# 稳定性标签与在职时长的对账
_SHORT_TENURE_MONTHS = 12
_MANAGEMENT_TITLES = ("总监", "管理岗", "总经理", "副总", "负责人", "厂长", "院长", "校长")
_MANAGEMENT_EVIDENCE_RE = re.compile(
    r"带领|带队|管理\s*\d+\s*人|团队\s*\d+\s*人|下属|带人|团队规模|负责\s*\d+\s*人|汇报\s*\d+\s*人"
)

_MISSING_LABELS = {
    "", "-", "—", "–", "无", "未提供", "未知", "未说明", "不清楚", "暂无", "n/a", "na", "none", "null",
}

_NOTICE = (
    "本项为简历表述与结构信息的自动对账，仅用于提示需要追问或核实的点，"
    "不构成对候选人的任何结论，不影响评分。所有条目均附原文或结构依据，"
    "请结合面试与背景调查判断。"
)


# ── 通用工具 ────────────────────────────────────────────────────────

def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_as_text(item) for item in value if _as_text(item)]
    text = _as_text(value)
    return [text] if text else []


def _lookup(analysis: dict, path: tuple[str, ...]) -> list[str]:
    """按字段路径取出待核验的正向声明文本。"""
    node: Any = analysis
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return _as_list(node)


def _is_missing(value: Any) -> bool:
    return _as_text(value).lower() in _MISSING_LABELS


def _digits_only(value: str) -> str:
    """去掉空白/千分位/全角空格，便于数字包含判断。"""
    return re.sub(r"[\s,，、\u3000]", "", value)


def _normalized_text(value: Any) -> str:
    """只保留中日韩汉字、字母、数字，用于重合度与包含判断。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(value or "")).lower()


def _ngrams(text: str, size: int) -> set[str]:
    if len(text) < size:
        return set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def _month_index(value: Any, now_index: int) -> tuple[int, bool] | None:
    """把日期标签转成绝对月序号；返回 (序号, 是否精确到月)。无法解析返回 None。"""
    text = _as_text(value)
    if not text or _is_missing(text):
        return None
    if re.search(r"至今|目前|现在|在职|present|current", text, re.I):
        return now_index, True
    match = re.match(r"\s*((?:19|20)\d{2})\s*(?:[-/.年]\s*(\d{1,2}))?", text)
    if not match:
        return None
    year = int(match.group(1))
    has_month = bool(match.group(2))
    month = int(match.group(2)) if has_month else 1
    if not 1 <= month <= 12:
        month, has_month = 1, False
    return year * 12 + month - 1, has_month


def _parse_years(value: Any) -> float | None:
    """从“8 年”“8年6个月”“约 8.5 年”解析年限。"""
    text = _as_text(value)
    if not text or _is_missing(text):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        years = float(match.group(1))
    except ValueError:
        return None
    extra = re.search(r"年\s*(\d{1,2})\s*(?:个)?月", text)
    if extra:
        try:
            years += int(extra.group(1)) / 12
        except ValueError:
            pass
    if years <= 0 or years > 60:
        return None
    return years


def _clip(value: Any, limit: int = _MAX_EVIDENCE) -> str:
    text = _as_text(value)
    return text if len(text) <= limit else text[:limit] + "…"


# 报告里的英文字段路径 → HR 看得懂的中文字段名（详情里不再出现 summary / risk_points 这种词）
_PLAIN_WHERE = {
    "summary": "总体评价",
    "recommendation_reason": "推荐理由",
    "risk_points": "风险提示",
    "weaknesses": "短板与风险",
    "strengths": "匹配亮点",
    "project_match_points": "项目匹配点",
    "role_specific_assessment": "岗位定制判断",
    "deduction_reasons": "扣分理由",
    "work_history": "工作履历",
    "work_history.total_years": "工作履历里的总年限",
    "work_history.stability": "工作履历里的稳定性",
    "work_history.seniority": "工作履历里的岗位层级",
    "skill_match.hard_skills": "硬技能匹配",
    "requirements_checklist": "岗位要求逐条判定",
    "resume_content": "简历原文",
    "strengths / project_match_points": "匹配亮点 / 项目匹配点",
}


def _plain_where(value) -> str:
    text = str(value)
    return _PLAIN_WHERE.get(text, text)


def _readable(value: str) -> str:
    """抹掉偶尔混进来的序列化痕迹：{\'skill\': \'x\', \'status\': \'y\'} → x ｜ y。"""
    text = _as_text(value).strip()
    if "{" in text and "}" in text:
        text = re.sub(r"[{}\[\]'\"]", "", text)
        text = re.sub(
            r"\b(?:skill|status|evidence|name|match|match_status|resume_basis|requirement)\s*:\s*",
            "",
            text,
        )
        text = re.sub(r"\s*,\s*", " ｜ ", text)
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def _finding(code: str, level: str, title: str, detail: str, evidence: str, ask: str, where: str = "", quote: str = "", source: str = "report") -> dict:
    """source: report=对账 LLM 报告的声明；resume=直接对账简历原文（不依赖模型）。"""
    item = {
        "code": code,
        "level": level,
        "title": title,
        "detail": _clip(detail),
        "evidence": _clip(evidence),
        "ask": _clip(ask, 160),
        "where": where,
        "source": source,
    }
    if quote:
        item["quote"] = _clip(quote, _MAX_QUOTE)
    return item


def _employment_records(analysis: dict) -> list[dict]:
    work = analysis.get("work_history")
    if not isinstance(work, dict):
        return []
    records = work.get("employment_records")
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


# ── 规则 1：量化成果在原文中查无此数 ────────────────────────────────

def _check_metric_claims(resume_norm: str, analysis: dict) -> list[dict]:
    findings: list[dict] = []
    seen: set[str] = set()
    for path in _CLAIM_PATHS:
        where = ".".join(path)
        for text in _lookup(analysis, path):
            for match in _METRIC_RE.finditer(text):
                raw_value, unit = match.group(1), match.group(2)
                literal = f"{raw_value}{unit}"
                if literal in seen:
                    continue
                try:
                    numeric = float(raw_value)
                except ValueError:
                    continue
                if numeric <= 0:
                    continue
                # 小数值且量词本身没有量级含义时，噪声太大，跳过
                if unit not in _METRIC_FREE_UNITS and numeric < _METRIC_MIN_VALUE:
                    continue
                if _digits_only(raw_value) in resume_norm:
                    continue
                seen.add(literal)
                findings.append(
                    _finding(
                        "metric_not_in_resume",
                        _LEVEL_RED,
                        "量化结果在简历原文中未检索到",
                        f"报告在“{_plain_where(where)}”里写了“{literal}”，但简历原文里找不到这个数字 —— "
                        f"可能是模型看错了，也可能是简历里确实没写。",
                        f"断言原文：{_clip(text, 160)}",
                        f"先别问候选人：这条说的是报告里出现了简历上没有的数字（{literal}），"
                        f"先回原文核对一遍。若确认要问，可以照这样问：『“{literal}”这个数是怎么算出来的？"
                        f"统计的是哪个时间段？』",
                        where,
                    )
                )
                if len(findings) >= 4:
                    return findings
    return findings


# ── 规则 2：总年限与分段经历之和不符 ────────────────────────────────

def _check_tenure_sum(analysis: dict, now_index: int) -> list[dict]:
    work = analysis.get("work_history")
    if not isinstance(work, dict):
        return []
    claimed_years = _parse_years(work.get("total_years"))
    if claimed_years is None:
        return []
    records = _employment_records(analysis)
    if not records:
        return []
    covered: set[int] = set()
    approximate = 0
    for record in records:
        start = _month_index(record.get("start_date"), now_index)
        end = _month_index(record.get("end_date"), now_index)
        if not start or not end:
            continue
        if start[0] > end[0]:
            continue
        if not (start[1] and end[1]):
            approximate += 1
        covered.update(range(start[0], end[0] + 1))
    if not covered:
        return []
    actual_months = len(covered)
    claimed_months = claimed_years * 12
    # 年份精度不精确 + 每年给 6 个月容差，宁可漏报也不要误报
    tolerance = 12 + 6 * approximate
    if claimed_months - actual_months <= tolerance:
        return []
    return [
        _finding(
            "tenure_sum_mismatch",
            _LEVEL_RED,
            "总年限明显高于分段经历合计",
            f"报告写的总工作年限约 {claimed_years:g} 年，但按它自己列出的分段经历加起来只有约 "
            f"{actual_months / 12:.1f} 年，差了约 {(claimed_months - actual_months) / 12:.1f} 年。",
            "依据：工作履历里各段经历的起止月份（逐段相加，重叠部分不重复计）",
            f"直接问：『报告按您列出的经历算出约 {actual_months / 12:.1f} 年，但总年限写的是约 {claimed_years:g} 年，"
            f"差的这段时间是在哪家公司？还是把实习、兼职一起算进来了？』",
            "work_history.total_years",
        )
    ]


# ── 规则 3：稳定性标签与在职时长矛盾 ────────────────────────────────

def _check_stability_conflict(analysis: dict, now_index: int) -> list[dict]:
    work = analysis.get("work_history")
    if not isinstance(work, dict):
        return []
    if _as_text(work.get("stability")) != "稳定":
        return []
    records = _employment_records(analysis)
    durations: list[int] = []
    for record in records:
        start = _month_index(record.get("start_date"), now_index)
        end = _month_index(record.get("end_date"), now_index)
        if not start or not end or start[0] > end[0]:
            continue
        durations.append(end[0] - start[0] + 1)
    if len(durations) < 2:
        return []
    short = [months for months in durations if months < _SHORT_TENURE_MONTHS]
    average = sum(durations) / len(durations)
    if len(short) < 2 or len(short) * 2 < len(durations):
        return []
    return [
        _finding(
            "stability_conflict",
            _LEVEL_REVIEW,
            "稳定性标签与实际在职时长不一致",
            f"报告把稳定性标为“稳定”，但有日期的 {len(durations)} 段经历里有 {len(short)} 段不满 "
            f"{_SHORT_TENURE_MONTHS} 个月（平均 {average:.1f} 个月）。",
            "依据：工作履历里每段经历的起止月份",
            f"直接问：『您有 {len(short)} 段经历不到 {_SHORT_TENURE_MONTHS} 个月（最短 {min(short)} 个月），"
            f"当时分别是因为什么离开的？下一份是提前找好的吗？』",
            "work_history.stability",
        )
    ]


# ── 规则 4：管理职级声明缺少带人证据 ────────────────────────────────

def _check_management_claim(resume_content: str, analysis: dict) -> list[dict]:
    work = analysis.get("work_history")
    if not isinstance(work, dict):
        return []
    seniority = _as_text(work.get("seniority"))
    if not any(title in seniority for title in _MANAGEMENT_TITLES):
        return []
    if not _is_missing(work.get("team_size")):
        return []
    if _MANAGEMENT_EVIDENCE_RE.search(resume_content):
        return []
    return [
        _finding(
            "management_claim_without_team",
            _LEVEL_REVIEW,
            "管理职级声明缺少带人规模依据",
            f"报告将岗位层级标为“{seniority}”，但带人规模为“未提供”，简历原文也未见任何团队规模或下属描述。",
            "依据：工作履历里的岗位层级、带人规模，以及简历原文里的带人描述",
            f"直接问：『您这段经历里带几个人？向谁汇报？有没有预算或审批权限？"
            f"下面的人是谁招的、绩效是不是您打？』",
            "work_history.seniority",
        )
    ]


# ── 规则 5：报告声称“符合要求”但证据在原文中找不到 ─────────────────

def _term_hits(text: str, corpus_norm: str) -> tuple[int, int]:
    """text 里的有意义词有多少个能在语料里找到；返回（命中数, 词总数）。"""
    terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#.-]{1,}", text or "")]
    if not terms:
        return 0, 0
    return sum(1 for term in terms if term.lower() in corpus_norm), len(terms)


# 报告里的"证据"常常只是一句指针（"简历第 19 行明确负责此类工作"），不是可核对的描述。
# 实测这种写法会让"证据在原文里找不到"必然成立 —— 而模型引用的行号是**它自己那份排版**，
# 跟真实原文的行号对不上（金利源真实报告引"第 19 行"，原文第 19 行是"责任心与耐心"）。
_EVIDENCE_POINTER_RE = re.compile(r"第\s*\d+\s*行|见原文|如上|同上|见简历|原文第")


def _check_skill_evidence(resume_content: str, analysis: dict) -> list[dict]:
    resume_norm = _normalized_text(resume_content)
    if not resume_norm:
        return []
    skill_match = analysis.get("skill_match")
    if not isinstance(skill_match, dict):
        return []
    findings: list[dict] = []
    for entry in _as_list(skill_match.get("hard_skills")):
        if "符合要求" not in entry:
            continue
        parts = re.split(r"[:：]", entry)
        if len(parts) < 2:
            continue
        # 报告声称"符合要求"的是**这一项技能本身**（冒号前的部分），证据只是它的说明。
        # 核对要以技能项为主：项本身能在原文里定位 → 报告没夸大，不该报。
        claim = re.sub(r"^\s*[\[【(（\-*·]*\s*(?:符合要求|已具备|具备)\s*[\]】)）-]*\s*", "", _as_text(parts[0]))
        evidence = _as_text(parts[-1])
        if len(evidence) < 4 or _is_missing(evidence):
            continue
        claim_hits, claim_total = _term_hits(claim, resume_norm)
        if claim_total and claim_hits >= max(1, min(2, claim_total)):
            continue
        if not _EVIDENCE_POINTER_RE.search(evidence):
            evidence_hits, evidence_total = _term_hits(evidence, resume_norm)
            if evidence_total and evidence_hits >= max(1, min(2, evidence_total)):
                continue
        findings.append(
            _finding(
                "skill_evidence_not_found",
                _LEVEL_REVIEW,
                "技能证据在简历原文中未检索到",
                "报告把该项技能标为“符合要求”，但该项技能描述与证据都无法在简历原文中定位。",
                f"报告条目：{_clip(_readable(entry), 160)}",
                "直接问：『这项技能您是在哪个项目里用的？什么版本？具体哪部分由您负责？』"
                "（先核报告：模型可能只是把技能名写进了报告，而简历里并没有提）",
                "skill_match.hard_skills",
            )
        )
        if len(findings) >= 3:
            break
    return findings


# ── 规则 6：简历与岗位描述文本高度重合（疑似粘贴 JD）────────────────

def _first_common_snippet(resume_norm: str, jd_grams: set[str], size: int = 24) -> str:
    if len(resume_norm) < size:
        return ""
    for index in range(len(resume_norm) - size + 1):
        if resume_norm[index:index + size] in jd_grams:
            start = max(0, index - 10)
            return resume_norm[start:index + size + 10]
    return ""


def _check_jd_copy(resume_content: str, job_content: str) -> list[dict]:
    resume_norm = _normalized_text(resume_content)
    jd_norm = _normalized_text(job_content)
    if len(jd_norm) < _JD_COPY_MIN_LEN or len(resume_norm) < _JD_COPY_MIN_LEN:
        return []
    jd_grams = _ngrams(jd_norm, _JD_COPY_NGRAM)
    resume_grams = _ngrams(resume_norm, _JD_COPY_NGRAM)
    if not jd_grams or not resume_grams:
        return []
    hits = sum(1 for gram in resume_grams if gram in jd_grams)
    if hits < _JD_COPY_MIN_HITS:
        return []
    ratio = hits / len(resume_grams)
    if ratio < _JD_COPY_RATIO:
        return []
    snippet = _first_common_snippet(resume_norm, jd_grams)
    return [
        _finding(
            "resume_jd_overlap",
            _LEVEL_RED,
            "简历与岗位描述文本高度重合",
            f"简历原文有 {ratio * 100:.1f}% 的 {_JD_COPY_NGRAM} 字连续片段与岗位描述完全相同"
            f"（{hits} 个片段），正常独立撰写的简历通常不会出现这种量级的逐字重合。",
            f"重合示例：{_clip(snippet, 120)}" if snippet else "结构依据：字符级 n-gram 重合统计",
            f"直接问：『简历里有 {hits} 处和岗位描述逐字一样，比如上面这条 —— 这具体是您在哪家公司、"
            f"哪一年做的事？当时负责到什么程度？』",
            "resume_content",
            quote=snippet,
        )
    ]


# ── 规则 7：成段空泛话术（不可证伪表述）────────────────────────────

def _check_unfalsifiable(analysis: dict) -> list[dict]:
    empty: list[tuple[str, str]] = []
    for path in _CLAIM_PATHS:
        where = ".".join(path)
        for text in _lookup(analysis, path):
            if _PUFF_RE.search(text) and not _NUMBER_RE.search(text):
                empty.append((where, text))
    if len(empty) < 3:
        return []
    sample = "；".join(f"[{_plain_where(where)}] {_clip(text, 90)}" for where, text in empty[:3])
    return [
        _finding(
            "unfalsifiable_praise",
            _LEVEL_REVIEW,
            "多条优势描述无法量化核验",
            f"共有 {len(empty)} 条优势/成果描述使用了评价性措辞且完全不含任何数字，"
            "既不含量化结果也不含具体项目或工具名，无法在面试之外核验。",
            f"示例：{_clip(sample, 200)}",
            "别问“您沟通能力怎么样”这种 —— 直接用具体场景验证。可以照这样问："
            "『说一个您说服业务部门改流程的例子：对方一开始反对什么？您怎么谈下来的？最后改成了什么？』",
            "strengths / project_match_points",
        )
    ]


# ── 规则 12：清单"已写明符合" vs 报告正文的否定表述（结论与依据打架）─────
#
# 真数据实测抓到的两类情况（真实 IOT 实施岗 JD × 两份真实简历）：
#   A. **不是矛盾**（口径误报）：清单把「3年以上软硬件现场实施或IOT项目落地经验」判成“已写明符合”
#      —— 服务端只按**年限**闸核过（依据=「简历年限10年，满足岗位3年要求」），报告否定的是
#      **领域**（「无IOT/一箱一码现场部署经验」）。两处在数不同维度，谁都没错。
#   B. **是真矛盾**：清单凭简历里某句话判“符合”（比如依据里就引了“熟练使用RFID”），
#      报告同一条却说“简历未体现RFID经验”。同一个关键点，两个相反结论。
# 设计约束（窄口径，宁可漏报不误报）：
#   ① 只比**同一个小分句**里的否定词——跨句比会把“本科学历达标，但无IOT经验”算到学历头上。
#   ② 只用有辨识度的词（与清单同一套词表）——否则「负责/手册」这种通用词又会制造噪声。
#   ③ **关键点必须出现在清单自己声称的依据（resume_basis）里**——这是区分 A/B 的唯一判据：
#      依据里没有这个词，说明清单根本没拿它当证据，两处讲的不是同一件事。没有依据一律不报。
#   ④ 单字否定词先过同形词（无线/非常/未来…），避免「无线通信调试」被读成否定。
#   ⑤ 一条要求只报一次，最高 review，绝不 red：这是报告与清单之间的口径核对，不是简历造假。
#
# 口径修正记录（2026-07 第十一轮）：③④ 是本轮加的。加之前该规则在两份真实简历上共命中 2 条，
# 全部属于 A 类误报；加之后真实数据上 0 命中（见 test_deception_check.py 的 A 类回归用例）。

_REPORT_TEXT_KEYS = ("summary", "recommendation_reason")
_REPORT_LIST_KEYS = ("weaknesses", "risk_points", "deduction_reasons")
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;、\n]")
_CLAUSE_NEGATION_RE = re.compile(r"无|没有|未|非|不具备|缺乏|不匹配|不满足|不足|均未|尚无")
# 单字否定词会误伤同形词：「无线通信」不是否定，「非常熟悉」不是否定，「未来可期」不是否定。
# 先把这些同形词从分句里剔除，再判断有没有否定词。
_NEGATION_FALSE_FRIENDS = ("无锡", "无线", "无论", "无疑", "无可", "无法避免", "非常", "非但", "未来", "未知")
_MAX_CONTRADICTION_FINDINGS = 3

try:
    # 词表必须与清单判定用同一套，否则规则和它审计的东西会各说各话。
    from screening_agent import _distinctive_terms as _distinctive_terms_impl
except Exception:  # pragma: no cover - 兜底：导入失败也不能让核验整体挂掉
    _FALLBACK_GENERIC_TERMS = frozenset({
        "熟练掌握", "熟悉掌握", "独立完成", "独立编写", "独立负责", "能够独立",
        "相关经验", "工作经验", "项目经验", "行业经验", "团队合作", "沟通协调",
        "学习能力", "抗压能力", "具备良好", "具备较强", "较强的", "良好的",
        "优秀的", "一定的", "及其他", "团队协作", "责任心", "沟通能力", "表达能力",
    })

    def _distinctive_terms_impl(text: str) -> list[str]:
        terms = [
            term.lower()
            for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#.-]{1,}", str(text or ""))
        ]
        return [term for term in terms if len(term) >= 3 and term not in _FALLBACK_GENERIC_TERMS]


def _report_sentences(analysis: dict) -> list[tuple[str, str]]:
    """报告里"人写给人看"的正文：综合判定、短板、风险、扣分理由。"""
    parts: list[tuple[str, str]] = []
    for key in _REPORT_TEXT_KEYS:
        value = analysis.get(key)
        if isinstance(value, str) and value.strip():
            parts.append((key, value))
    for key in _REPORT_LIST_KEYS:
        for entry in _as_list(analysis.get(key)):
            if entry.strip():
                parts.append((key, entry))
    return parts


def _clause_negates(clause: str) -> bool:
    """分句里是否真有否定表述（先剔除「无线/非常/未来」这类同形词）。"""
    cleaned = clause
    for word in _NEGATION_FALSE_FRIENDS:
        cleaned = cleaned.replace(word, "")
    return bool(_CLAUSE_NEGATION_RE.search(cleaned))


def _negated_clauses_for(terms: list[str], paragraphs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """找出"含有否定词、且同句出现了该要求关键点"的小分句。"""
    hits: list[tuple[str, str, str]] = []
    for key, text in paragraphs:
        for clause in _CLAUSE_SPLIT_RE.split(text):
            clause = clause.strip()
            if not clause or not _clause_negates(clause):
                continue
            lowered = clause.lower()
            for term in terms:
                if term in lowered:
                    hits.append((term, clause, key))
                    break
    return hits


def _check_checklist_contradiction(analysis: dict) -> list[dict]:
    """规则 12：清单说"符合"的这一条，与报告正文的否定表述是否在说**同一件事**。

    判据（这条规则最容易误报，所以口径必须窄）：
      清单判"符合"时自己声称的依据（`resume_basis`）里就含有某个关键点，
      而报告正文的否定分句里出现的正是同一个关键点 —— 这才叫两边在说同一件事。
    反例（真实数据上踩到的）：清单那条「3年以上软硬件现场实施或IOT项目落地经验」是
      服务端按**年限**闸判的（依据=「简历年限10年，满足岗位3年要求」），报告 summary 否定的
      是**领域**（"无IOT/一箱一码现场部署经验"）。关键点 iot 根本不在清单依据里 ——
      两条在数不同维度，不是矛盾。旧口径只看"要求原文里的词"，于是把它误报成自相矛盾。
    """
    checklist = analysis.get("requirements_checklist")
    if not isinstance(checklist, list) or not checklist:
        return []
    paragraphs = _report_sentences(analysis)
    if not paragraphs:
        return []
    findings: list[dict] = []
    for item in checklist:
        if not isinstance(item, dict) or str(item.get("status") or "") != "met":
            continue
        text = _as_text(item.get("text"))
        basis = _as_text(item.get("resume_basis"))
        if not basis:
            # 没有依据就没法判断"是不是同一件事"，宁可漏报也不误报。
            continue
        lowered_basis = basis.lower()
        # 关键点必须同时出现在"要求原文"和"清单自己声称的依据"里。
        terms = [term for term in _distinctive_terms_impl(text) if term and term in lowered_basis]
        if not terms:
            continue
        hits = _negated_clauses_for(terms, paragraphs)
        if not hits:
            continue
        term, clause, key = hits[0]
        findings.append(
            _finding(
                "claim_contradicts_report",
                _LEVEL_REVIEW,
                "清单依据与报告正文表述不一致",
                "清单按自己给出的依据把这条判为“符合”，但同一份报告的正文里，"
                "同一个关键点出现在否定表述中——HR 只看清单会以为这条已经确认符合。"
                "这是报告与清单之间的口径核对，不是对候选人的结论。",
                f"要求：{_clip(text, 90)}｜清单依据：{_clip(basis, 100)}"
                f"｜关键点：{term}｜报告正文的“{_plain_where(key)}”：{_clip(clause, 110)}",
                f"这条不用问候选人：矛盾在报告内部（清单说“符合”、正文又说没有）。先按清单依据里的原话"
                f"回简历核对“{term}”这件事到底有没有，再决定要不要在面试里问。",
                "requirements_checklist",
            )
        )
        if len(findings) >= _MAX_CONTRADICTION_FINDINGS:
            break
    return findings


# ════════════════════════════════════════════════════════════════════
# 原文侧：直接读简历原文，完全不依赖 LLM 报告
# 价值：即使模型报告质量差、或还没跑模型，这部分也能独立工作。
# ════════════════════════════════════════════════════════════════════

_RAW_DATE_TOKEN = r"(?:19|20)\d{2}(?:\s*[-./年]\s*(?:1[0-2]|0?[1-9])(?:\s*月)?)?"
_RAW_PERIOD_RE = re.compile(
    rf"(?P<start>{_RAW_DATE_TOKEN})\s*[年月]?\s*(?:-|—|–|~|～|至|到)\s*[年月]?\s*"
    rf"(?P<end>至今|现在|目前|{_RAW_DATE_TOKEN})",
    re.IGNORECASE,
)
_INTERN_RE = re.compile(r"实习|兼职|见习|实训|intern", re.IGNORECASE)
# 教育段落的识别（第十二轮）：教育经历的年份区间**不是任职区间**。
# 实测两份真简历都因此产生假阳性：汪林军的「2015-2019」（华南农业大学本科）与
# 金利源的「2012-2016」（宁波大学科学技术学院本科）都被当成了一段工作，
# 于是"最早工作早于毕业年份"必然命中 —— 而这个 bug 还会把在校年限算进工作年限合计。
_EDU_HEADER_RE = re.compile(r"教育经历|教育背景|学历信息|学习经历|教育培训|教育情况")
_PURE_DATE_LINE_RE = re.compile(r"^[\s\-—–~～至到./年月日()（）]+$")

# 简历自述年限：“8 年工作经验”“5年以上相关经验”
_SELF_TENURE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*年\s*(?:以上)?\s*(?:工作|相关|从业|行业|开发|运营|管理|产品)?\s*经验")

# 毕业年份：“2018 年 6 月毕业”“2015.07 本科毕业”“毕业时间：2018”
# degree 组同时用来识别中学毕业——否则“1998 年高中毕业”会被当成最早毕业年份，
# 把 2000 年之后的正职经历误判成“毕业前工作”。
_GRADUATION_RE = re.compile(
    r"((?:19|20)\d{2})\s*(?:[-./年]\s*(?:\d{1,2}\s*月?)?)?\s*"
    r"(?P<degree>本科|硕士|研究生|博士|专科|大专|学士|高中|中专|职高|技校|初中)?\s*毕业"
)
_GRADUATION_AFTER_RE = re.compile(r"毕业(?:于|时间|年份|年月)?\s*[:：]?\s*((?:19|20)\d{2})")
_SECONDARY_SCHOOL_RE = re.compile(r"高中|中专|职高|技校|初中|中学")
_SECONDARY_DEGREES = {"高中", "中专", "职高", "技校", "初中"}

# 教育行识别（第十二轮修 D1）：真实简历的教育经历常写成「2014.09-2018.06 某大学 软件工程 本科」，
# **没有「毕业」二字**，只靠上面的正则实测 97 份里只有 2 份能提取到年份 → 这条规则等于没分母。
# 判据：同一行里既有年份、又有学历词或院校词 → 视为教育行，取该行最大年份为毕业年份（区间取后一个）。
_DEGREE_IN_LINE_RE = re.compile(r"本科|硕士|研究生|博士|专科|大专|学士|专升本|统招|全日制")
_SCHOOL_IN_LINE_RE = re.compile(r"大学|学院|学校|职业技术|技师学院|中学|高中|中专|职高|技校")
_YEAR_IN_LINE_RE = re.compile(r"(?:19|20)\d{2}")

# 量化声明识别（第十二轮扩展）：这个候选人池的量化写法是「提升/优化/降低 + 数字（带单位）」，
# 而不是百分号 —— 97 份真实简历里 %／％／倍／百分之 出现 0 次，原来只认百分号的规则永远看不到东西。
_CHANGE_VERB_RE = re.compile(
    r"提升|提高|优化|改善|向好|降低|下降|减少|缩短|节省|节约|增加|增长|新增|翻倍|扩展|放大|压缩|提速|提效|降本|增效"
)
# 单位白名单：刻意不含「年/月/日」，否则「2023.06」「3 年经验」会被当成量化成果
_METRIC_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|％|倍|万|亿|K|k|千|个|人|台|套|天|小时|分钟|秒|次|条|件|项|单|张|页|吨|米|元|ms|毫秒)"
)
# 期望薪资/实习兼职语境：那里的数字不是"成果"，报出来就是纯噪声
_PAY_OR_INTERN_RE = re.compile(r"期望|期望薪资|薪资|薪酬|月薪|年薪|工资|报价|预算|待遇|补贴|实习|兼职|见习|实训")
# 给了"绝对值"的声明（达成/完成/覆盖/达到某个数）不算"缺基线"——它至少给了一个可核的数。
# 刻意**不收单字**（从/由/较/共）：它们在中文里太常见（"由我负责""共同推进"），
# 收进来会把真实句子整片判成"已给基线"；而「从 200ms 优化到 80ms」这种真基线，
# 靠下面"数字附近还有别的数字"就已经能识别。
_ABSOLUTE_VALUE_CUE_RE = re.compile(
    r"相比|对比|环比|同比|达到|达成|实现|完成|做到|覆盖|服务|累计|不少于|至少|超过|突破|"
    r"增至|增长到|提升至|提升到|提高到|优化到|降到|下降到|降低到|减少到|缩短到|控制在|保持在|高于|低于"
)

# 原文里的量化增幅声明（只取百分比与倍数——这类数字最容易没有基线）
_RAW_METRIC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|倍)")
# 有基线的线索：从/由…到、相比/对比/环比/同比/较、达到了某个数
_BASELINE_CUE_RE = re.compile(r"从|由|相比|对比|环比|同比|较|达到|增至|增长到|提升至|降到|下降到|高于|低于")

# 用程度词代替数字：“大幅提升性能”“显著降低成本”
_DEGREE_WORD_RE = re.compile(r"大幅|显著|极大|成倍|数倍|数十倍|巨幅|明显")
_PERFORMANCE_NOUN_RE = re.compile(r"性能|效率|销量|营收|收入|转化|用户|指标|成本|故障|速度|产量|时延|耗时")

_RAW_RULE_MIN_HITS = 3  # 聚合类规则：至少 3 处才成一条，避免逐条噪声


def _extract_raw_periods(text: str) -> list[dict]:
    """从原文逐行抽取任职区间；同行的“实习/兼职”标记会带上，用于后续排除。

    教育经历要排除掉（第十二轮）：教育段里的年份区间（「2015-2019」）与教育行（「华南农业大学 本科…」）
    都不是任职区间，混进来既会造出"工作早于毕业"的假阳性，也会把在校年限算进工作年限合计。
    判据：① 本行含院校/学历词 → 教育行；② 本行是"纯日期行"、且上面最近一行非空行是教育行或教育标题 → 教育段。
    """
    records: list[dict] = []
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    previous_nonempty = ""
    for line in lines:
        if not line:
            continue
        is_edu_line = bool(_SCHOOL_IN_LINE_RE.search(line) or _DEGREE_IN_LINE_RE.search(line) or _EDU_HEADER_RE.search(line))
        is_pure_date = not _PURE_DATE_LINE_RE.sub("", _RAW_PERIOD_RE.sub("", line))
        if is_edu_line:
            previous_nonempty = line
            continue
        if is_pure_date and (
            _SCHOOL_IN_LINE_RE.search(previous_nonempty)
            or _DEGREE_IN_LINE_RE.search(previous_nonempty)
            or _EDU_HEADER_RE.search(previous_nonempty)
        ):
            previous_nonempty = line
            continue
        for match in _RAW_PERIOD_RE.finditer(line):
            records.append({
                "start": match.group("start").strip(),
                "end": match.group("end").strip(),
                "line": line[:120],
                "intern": bool(_INTERN_RE.search(line)),
            })
        previous_nonempty = line
    return records


def _sentence_at(text: str, index: int) -> str:
    """取包含 index 的那一句（以中英文句读和换行切分）。"""
    start = 0
    for match in re.finditer(r"[。；;！!？?\n]", text):
        if match.end() > index:
            return text[start:match.end()].strip()
        start = match.end()
    return text[start:].strip()


# 被 PDF 换行截断的句子要接回去（第十四轮，待办 12）：
# 平台导出的简历里，一句话常被硬换行切成两行（「…7×12 小时处理各部门单」/「据报错、登录…」），
# 而核验层的分句是含 `\n` 的 → 于是半个句子被当成一句，甚至切出「优化」这种 2 字碎片。
# 后果不只是难听：`从 200ms` / `优化到 80ms` 这种**真基线会被拆到两句里**，右边那句没了邻居 → 误报风险。
# 只作用于"按句读判断"的规则；任职区间抽取仍走原始行结构（那里的换行是有意义的）。
_SENTENCE_END_RE = re.compile(r"[。；;！!？?：:）)】」”\"]$")
_LINE_START_MARKER_RE = re.compile(r"^\s*(?:[0-9０-９]{1,2}\s*[、.)）]|[（(【\[]|[·•\-*※]|(?:19|20)\d{2})")


def _join_wrapped_lines(text: str) -> str:
    """把被硬换行截断的句子接回一行（不新增/删除任何字符，只把换行换成空格）。

    合并条件（三条同时满足，宁可少接也不要错接）：
      ① 上一行非空，且**不以句读/冒号结尾**（句号结尾说明句子已经完了）；
      ② 本行**不以**列表序号、括号、项目符号、年份开头（那是新条目的开头）；
      ③ 本行不是"看起来自成一条"的短行（≤4 字且不含标点，如小节标题「资格证书」）。
    """
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    merged: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            merged.append("")
            continue
        previous = merged[-1].strip() if merged else ""
        can_join = (
            bool(previous)
            and not _SENTENCE_END_RE.search(previous)
            and not _LINE_START_MARKER_RE.match(line)
            and not (len(line) <= 4 and _NUMBER_RE.search(line) is None and not re.search(r"[，,、]", line))
        )
        if can_join:
            # 中文换行处本来没有空格，接回去不能凭空插一个（会污染原文子串匹配）；
            # 但英文/数字边界要空格，否则 "Windows" + "Server" 会粘成一个词。
            separator = " " if (previous[-1].isascii() and previous[-1].isalnum()
                                and line[0].isascii() and line[0].isalnum()) else ""
            merged[-1] = previous + separator + line
        else:
            merged.append(line)
    return "\n".join(merged)


def _check_raw_tenure(resume_content: str, now_index: int) -> list[dict]:
    """简历自述年限 vs 原文日期区间合计（只看原文，不看报告）。"""
    claimed = [float(match) for match in _SELF_TENURE_RE.findall(str(resume_content or ""))]
    claimed = [value for value in claimed if 0 < value <= 60]
    if not claimed:
        return []
    records = [item for item in _extract_raw_periods(resume_content) if not item["intern"]]
    if not records:
        return []
    covered: set[int] = set()
    approximate = 0
    for record in records:
        start = _month_index(record["start"], now_index)
        end = _month_index(record["end"], now_index)
        if not start or not end or start[0] > end[0]:
            continue
        if not (start[1] and end[1]):
            approximate += 1
        covered.update(range(start[0], end[0] + 1))
    if not covered:
        return []
    claimed_years = max(claimed)
    actual_months = len(covered)
    tolerance = 12 + 6 * approximate
    if claimed_years * 12 - actual_months <= tolerance:
        return []
    return [
        _finding(
            "raw_tenure_mismatch",
            _LEVEL_RED,
            "自述年限高于原文经历合计",
            f"简历写的是“约 {claimed_years:g} 年经验”，但按简历上写出的经历一段段加起来只有约 "
            f"{actual_months / 12:.1f} 年，差了约 {(claimed_years * 12 - actual_months) / 12:.1f} 年。",
            "依据：简历里各段经历的起止月份（逐段相加，重叠部分不重复计；已排除标注实习/兼职的段落）",
            f"直接问：『您写的是约 {claimed_years:g} 年经验，按简历上的日期算只有约 {actual_months / 12:.1f} 年 —— "
            f"是还有没写进来的经历，还是把实习、兼职也算进去了？』",
            "resume_content",
            source="resume",
        )
    ]


def _extract_education_years(text: str) -> list[int]:
    """从"教育经历"段落抽取毕业年份（不依赖「毕业」二字）。

    真实简历的教育信息有两种排版，都要认：
      ① 同行：「2014.09-2018.06 某大学 软件工程 本科」「2011-2014 芜湖职业技术学院 大专」
      ② **跨行**（实测汪林军的真简历就是这种）：
            教育经历
            华南农业大学 本科 农业机械化及其自动化 双一流
            2015-2019
         院校/学历一行、年份另起一行 —— 只认同行会漏掉它（这正是缺陷 D1 的另一半）。

    一行里同时有年份与学历词/院校词才算教育行（①）；院校/学历行后面紧跟"纯年份行"也算（②）。
    中学行（高中/中专/中学）一律跳过——它们不是"全职工作起点"的参照。
    """
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    years: list[int] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        if _SECONDARY_SCHOOL_RE.search(line) and not _DEGREE_IN_LINE_RE.search(line):
            continue
        has_degree = bool(_DEGREE_IN_LINE_RE.search(line))
        has_school = bool(_SCHOOL_IN_LINE_RE.search(line))
        line_years = [int(item) for item in _YEAR_IN_LINE_RE.findall(line)]
        if line_years and (has_degree or has_school):
            years.append(max(line_years))                    # ① 同行
            continue
        if not (has_degree or has_school):
            continue
        # ② 跨行：本行是院校/学历行但没年份 → 看下一行（最多 2 行）是否"只有年份"
        for follow in lines[index + 1:index + 3]:
            if not follow:
                continue
            follow_years = [int(item) for item in _YEAR_IN_LINE_RE.findall(follow)]
            if not follow_years:
                break
            # 下一行只允许含年份、区间符号与"年月"字样，避免把工作经历行当成毕业年份
            residue = _YEAR_IN_LINE_RE.sub("", follow)
            if re.fullmatch(r"[\s\-—–~～至到./年月日]+", residue or ""):
                years.append(max(follow_years))
            break
    return years


def _extract_graduation_years(text: str) -> list[int]:
    """抽取毕业年份，跳过中学毕业（中学毕业不能作为“全职工作起点”的参照）。"""
    years: list[int] = []
    for match in _GRADUATION_RE.finditer(text):
        degree = match.group("degree") or ""
        tail = text[match.end():match.end() + 8]
        if degree in _SECONDARY_DEGREES or _SECONDARY_SCHOOL_RE.search(tail):
            continue
        years.append(int(match.group(1)))
    for match in _GRADUATION_AFTER_RE.finditer(text):
        years.append(int(match.group(1)))
    # 第十二轮：真实简历多数只在教育行写起止年份、不写「毕业」，这里补上
    years.extend(_extract_education_years(text))
    return years


def _check_raw_work_before_graduation(resume_content: str, now_index: int) -> list[dict]:
    """最早一段全职工作早于最早毕业年份（排除实习；多段学历取最早毕业年份，允许本科与硕士之间的工作）。"""
    text = str(resume_content or "")
    now_year = now_index // 12
    grad_years = [year for year in _extract_graduation_years(text) if 1950 <= year <= now_year + 1]
    if not grad_years:
        return []
    earliest_grad = min(grad_years)
    starts: list[tuple[int, dict]] = []
    for record in _extract_raw_periods(text):
        if record["intern"]:
            continue
        start = _month_index(record["start"], now_index)
        if start:
            starts.append((start[0], record))
    if not starts:
        return []
    start_index, record = min(starts, key=lambda item: item[0])
    start_year = start_index // 12
    if start_year >= earliest_grad:
        return []
    return [
        _finding(
            "raw_work_before_graduation",
            _LEVEL_REVIEW,
            "最早工作经历早于毕业年份",
            f"简历最早一段工作的起始时间为 {record['start']}，而毕业年份为 {earliest_grad} 年，"
            "即该段工作发生在毕业之前。若为实习或在校兼职可忽略，否则时间线需要核实。",
            f"原文行：{record['line']}",
            f"直接问：『{record['start']} 那会儿您还没毕业，这段是实习、在校兼职，还是全职？"
            f"当时课业和上班是怎么安排的？』",
            "resume_content",
            source="resume",
        )
    ]


def _collect_unbaselined_metrics(text: str) -> list[str]:
    """收集"给了幅度、没给基线"的量化声明（同句去重）。

    两类来源：
      ① 百分比/倍数（原口径，保留）：`35%`、`3 倍`；
      ② 第十二轮扩展：**变化动词 + 带单位数字**（`提升 200 台`、`降低 30 万成本`、`缩短 15 天`）——
         这是本候选人池真实的写法（97 份里 0 处百分号，却有大量"提升/优化"）。

    关键：基线线索要**贴着数字**判断，不能全句扫。像「由」「较」「共」「实现」这类词在中文里
    太常见（"由我负责""较熟悉""共 3 人"），全句匹配会把几乎所有真实句子都判成"已给基线"——
    实测这样会得到 0 命中（等于规则又白写）。
    """
    claims: list[str] = []
    window = 12

    def _sentence_with_offset(index: int) -> tuple[str, int]:
        """返回包含 index 的那一句，以及该句起始在原文里的偏移（用于精确的邻近判断）。"""
        start = 0
        for match in re.finditer(r"[。；;！!？?\n]", text):
            if match.end() > index:
                break
            start = match.end()
        end = len(text)
        for match in re.finditer(r"[。；;！!？?\n]", text[start:]):
            end = start + match.end()
            break
        raw = text[start:end]
        lead = len(raw) - len(raw.lstrip())
        return raw.strip(), start + lead

    def _unbaselined(sentence: str, value: str, pos: int) -> bool:
        near = sentence[max(0, pos - window): pos + len(value) + window]
        if _PAY_OR_INTERN_RE.search(near):
            return False
        if _ABSOLUTE_VALUE_CUE_RE.search(near):
            return False
        # 数字附近还有别的数字 → 视为已经给出对照值（如「由 320ms 降至 180ms」）。
        # 但 19xx/20xx 这种年份不算基线：「2021 年主导产线改造，效率提升 30%」里的 2021 不是对照值。
        for other in re.finditer(r"\d+(?:\.\d+)?", sentence):
            if other.group(0) == value or abs(other.start() - pos) > 24:
                continue
            if _YEAR_IN_LINE_RE.fullmatch(other.group(0)):
                continue
            return False
        return True

    for match in _RAW_METRIC_RE.finditer(text):
        sentence, start = _sentence_with_offset(match.start())
        pos = match.start() - start
        if 0 <= pos <= len(sentence) and _unbaselined(sentence, match.group(1), pos):
            claims.append(sentence[:90])

    for match in _METRIC_VALUE_RE.finditer(text):
        sentence, start = _sentence_with_offset(match.start())
        pos = match.start() - start
        if not (0 <= pos <= len(sentence)):
            continue
        near = sentence[max(0, pos - window): pos + len(match.group(1)) + window]
        if not _CHANGE_VERB_RE.search(near):
            continue
        if _unbaselined(sentence, match.group(1), pos):
            claims.append(sentence[:90])

    return list(dict.fromkeys(claims))


def _check_raw_metric_baseline(resume_content: str) -> list[dict]:
    """原文中的量化增幅声明缺少基线（聚合为一条，避免逐条噪声）。"""
    text = _join_wrapped_lines(resume_content)
    unbaselined = _collect_unbaselined_metrics(text)
    if len(unbaselined) < _RAW_RULE_MIN_HITS:
        return []
    sample = "；".join(unbaselined)[:200]
    return [
        _finding(
            "raw_metric_without_baseline",
            _LEVEL_REVIEW,
            "量化成果普遍缺少基线",
            f"简历中有 {len(unbaselined)} 处量化声明没有给出基线或对比对象"
            "（既没有“从…到…”“相比…”，同一句也没有第二个数字），无法判断增幅的真实口径。",
            f"示例：{sample}",
            "挑一个数字直接问：『这个提升幅度是从多少做到多少？统计的是一年、一个季度还是一个月？』",
            "resume_content",
            source="resume",
        )
    ]


def _check_raw_degree_words(resume_content: str) -> list[dict]:
    """用程度词代替数字的性能/成果描述（聚合为一条）。"""
    text = _join_wrapped_lines(resume_content)
    vague: list[str] = []
    for match in _DEGREE_WORD_RE.finditer(text):
        sentence = _sentence_at(text, match.start())
        if not _PERFORMANCE_NOUN_RE.search(sentence):
            continue
        if _NUMBER_RE.search(sentence):
            continue
        vague.append(sentence[:90])
    if len(vague) < _RAW_RULE_MIN_HITS:
        return []
    sample = "；".join(dict.fromkeys(vague))[:200]
    return [
        _finding(
            "raw_degree_word_instead_of_number",
            _LEVEL_REVIEW,
            "成果描述用程度词代替数字",
            f"简历中有 {len(vague)} 处成果描述使用了“大幅/显著/成倍”等程度词，且同句没有任何数字，"
            "属于无法核验的表述。",
            f"示例：{sample}",
            "直接问：『您写的“大幅/显著提升”，能不能给个具体数：改之前多少、改之后多少？』",
            "resume_content",
            source="resume",
        )
    ]


# ── 规则 13：报告称"时间线重叠"，但可计算的经历区间零重叠 ──────────────
# 真实来源（第十四轮）：王龙龙那份真报告写「工作经历时间线存在重叠：宁波欧达光电（2024.09-2026.03）
# 与奥哲软件（2023.12-2024.08）时间不重叠，但广州卓尔（2022.11-2023.11）与中软国际；」——
# 前半句下结论、后半句自己否掉，而原文四段经历首尾相接、**一处都不重叠**。这是"报告给候选人
# 下了原文不支持的结论"，属于核验层的本职；现有 12 条都管不到（stability_conflict 只管稳定性标签）。
_OVERLAP_TEXT_PATHS = (
    ("risk_points",),
    ("weaknesses",),
    ("summary",),
    ("role_specific_assessment",),
    ("deduction_reasons",),
)
_TIMELINE_OVERLAP_RE = re.compile(r"重叠|重合|交叠")
# 关键词前 6 个字里出现否定 → 这句是在说"不重叠"，不能当命中：
# 「时间不重叠」/「不存在重叠」/「避免经历重叠」都算否定。
_NEGATED_OVERLAP_RE = re.compile(r"(?:不|未|无|没有|并非|并不|避免|防止)[^，。；]{0,4}$")


def _overlap_pairs(ranges: list[tuple[int, int]]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """两两比对月份区间，返回真正重叠的对（首尾相接不算重叠）。"""
    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for index, (start_a, end_a) in enumerate(ranges):
        for start_b, end_b in ranges[index + 1:]:
            if not (end_a < start_b or end_b < start_a):
                pairs.append(((start_a, end_a), (start_b, end_b)))
    return pairs


def _month_ranges(values: list[tuple[Any, Any]], now_index: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start_value, end_value in values:
        start = _month_index(start_value, now_index)
        end = _month_index(end_value, now_index)
        if start and end and start[0] <= end[0]:
            ranges.append((start[0], end[0]))
    return sorted(ranges)


def _check_report_timeline_overlap(resume_content: str, analysis: dict, now_index: int) -> list[dict]:
    """报告下了"时间线重叠"的结论，但报告自己的分段经历与简历原文都对不上这个结论。"""
    claims: list[tuple[str, str]] = []
    for path in _OVERLAP_TEXT_PATHS:
        where = ".".join(path)
        for text in _lookup(analysis, path):
            for match in _TIMELINE_OVERLAP_RE.finditer(text):
                prefix = text[max(0, match.start() - 6):match.start()]
                if _NEGATED_OVERLAP_RE.search(prefix):
                    continue
                claims.append((where, _clip(text, _MAX_EVIDENCE)))
                break
    if not claims:
        return []

    report_ranges = _month_ranges(
        [(item.get("start_date"), item.get("end_date")) for item in _employment_records(analysis)], now_index
    )
    resume_ranges = _month_ranges(
        [(item["start"], item["end"]) for item in _extract_raw_periods(resume_content)], now_index
    )
    # 两侧都要有 ≥2 段可比区间，且**两侧都零重叠**才算"原文明确不支持这个结论"
    if len(report_ranges) < 2 and len(resume_ranges) < 2:
        return []
    for ranges in (report_ranges, resume_ranges):
        if len(ranges) >= 2 and _overlap_pairs(ranges):
            return []

    def _label(month_index: int) -> str:
        return "%d年%d月" % (month_index // 12, month_index % 12 + 1)

    reference = resume_ranges if len(resume_ranges) >= 2 else report_ranges
    span = "、".join("%s到%s" % (_label(start), _label(end)) for start, end in reference)
    where, snippet = claims[0]
    # 证据必须交代清楚：模型的这句话本身就是断的（写到“与中软国际”就停了）。
    # HR 看到半句话根本不知道什么意思 —— 所以要标明"这句在报告里就是断的"，不能当成完整依据。
    unfinished = not re.search(r"[。！？；!?;]\s*$", snippet)
    evidence = (
        f"这句在报告里本身就是断的（写到一半就停了），报告原文：{snippet}"
        if unfinished
        else f"报告原文：{snippet}"
    )
    return [
        _finding(
            "report_timeline_overlap_unsupported",
            _LEVEL_REVIEW,
            "报告说经历有重叠，但实际一段接一段",
            f"报告在风险里写了“工作经历时间线存在重叠”，但把 {len(reference)} 段经历按日期排好后是"
            f"一段接一段（{span}），没有任何两段是重叠的 —— 这句话站不住，属于报告自己写错了。",
            evidence,
            f"这条不用问候选人：写错的是报告，不是简历。若还想确认经历衔接，可以照这样问："
            f"『您从{_label(reference[0][0])}到{_label(reference[-1][1])}是连续在工作吗？中间有没有空档期？』",
            where,
        )
    ]


# ── 主入口 ──────────────────────────────────────────────────────────

def check_resume_deception(*, resume_content: str, job_content: str = "", analysis: dict | None = None) -> dict:
    """对账简历表述与结构/原文。任何异常都不得向上抛出。

    返回：{version, enabled, checks_run, findings, counts, notice}
    findings 只读，不参与评分。
    """
    result: dict[str, Any] = {
        "version": CREDIBILITY_VERSION,
        "enabled": True,
        "checks_run": 0,
        "findings": [],
        "counts": {"red_flag": 0, "review": 0, "info": 0},
        "by_source": {"resume": 0, "report": 0},
        "notice": _NOTICE,
    }
    resume_text = str(resume_content or "")
    if not resume_text.strip():
        result["enabled"] = False
        result["notice"] = "简历原文为空，未进行表述核验。"
        return result
    if not isinstance(analysis, dict):
        analysis = {}

    now_index = datetime.now().year * 12 + datetime.now().month - 1
    resume_norm = _digits_only(_normalized_text(resume_text))

    checks = (
        # 原文侧（不依赖 LLM 报告，可独立工作）
        ("raw_tenure", lambda: _check_raw_tenure(resume_text, now_index)),
        ("raw_work_before_graduation", lambda: _check_raw_work_before_graduation(resume_text, now_index)),
        ("raw_metric_baseline", lambda: _check_raw_metric_baseline(resume_text)),
        ("raw_degree_words", lambda: _check_raw_degree_words(resume_text)),
        # 报告侧（对账 LLM 报告的声明，同时也能抓模型自身的幻觉）
        ("metric_claims", lambda: _check_metric_claims(resume_norm, analysis)),
        ("tenure_sum", lambda: _check_tenure_sum(analysis, now_index)),
        ("stability_conflict", lambda: _check_stability_conflict(analysis, now_index)),
        ("management_claim", lambda: _check_management_claim(resume_text, analysis)),
        ("skill_evidence", lambda: _check_skill_evidence(resume_text, analysis)),
        ("resume_jd_overlap", lambda: _check_jd_copy(resume_text, job_content)),
        ("unfalsifiable_praise", lambda: _check_unfalsifiable(analysis)),
        ("checklist_contradiction", lambda: _check_checklist_contradiction(analysis)),
        ("report_timeline_overlap", lambda: _check_report_timeline_overlap(resume_text, analysis, now_index)),
    )

    findings: list[dict] = []
    for name, runner in checks:
        try:
            produced = runner()
        except Exception as exc:  # 单条规则失败不影响其余规则，也绝不影响主流程
            # 必须记下是哪条规则挂了，而且用 warning（生产环境日志级别是 INFO，
            # debug 会被丢掉）：否则影子期调阈值时，一条永远抛异常的规则会表现成
            # “从未命中”，被误判为阈值过严。
            logger.warning("deception rule failed: %s (%s: %s)", name, type(exc).__name__, exc)
            continue
        result["checks_run"] += 1
        if produced:
            findings.extend(produced)

    # 严重度排序：结构性矛盾在前，同一级别保持发现顺序
    findings.sort(key=lambda item: {"red_flag": 0, "review": 1, "info": 2}.get(item.get("level", ""), 3))
    findings = findings[:_MAX_FINDINGS]
    counts = {"red_flag": 0, "review": 0, "info": 0}
    by_source = {"resume": 0, "report": 0}
    for item in findings:
        level = item.get("level", "info")
        counts[level] = counts.get(level, 0) + 1
        source = item.get("source", "report")
        by_source[source] = by_source.get(source, 0) + 1
    result["findings"] = findings
    result["counts"] = counts
    result["by_source"] = by_source
    return result
