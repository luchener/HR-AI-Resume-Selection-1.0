"""Pure markdown compatibility helpers for the isolated SQL adapter.

These preserve the current app.py behavior without importing its startup code
or reading environment files. Contract tests compare with the original helpers.
"""
import re
import resume_sanitize

def _build_raw_resume_markdown(content: str) -> str:
    """Convert locally extracted resume text into A4 Studio markdown."""
    lines = [line.strip() for line in (content or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    section_names = {
        "个人简介", "个人概况", "职业概况", "自我评价", "工作经验", "工作经历",
        "项目经验", "项目经历", "教育背景", "教育经历", "专业技能", "技能特长",
        "技能", "证书与荣誉", "荣誉奖项", "培训经历", "论文发表", "作品集",
    }
    out = [f"# {lines[0]}"]
    index = 1

    # Common DOCX extraction shape: name -> 联系方式 -> contact line.
    if index < len(lines) and lines[index].rstrip("：:") in {"联系方式", "联系信息"}:
        index += 1
        if index < len(lines):
            out.extend(["", f"> {lines[index]}"])
            index += 1
    elif index < len(lines) and lines[index] not in section_names:
        out.extend(["", f"## {lines[index]}"])
        index += 1

    current_section = False
    for line in lines[index:]:
        normalized = line.rstrip("：:")
        if normalized in section_names:
            out.extend(["", f"## {normalized}"])
            current_section = True
            continue
        if not current_section:
            out.extend(["", line])
            continue
        if line.startswith(("•", "·", "●", "▪", "-", "*")):
            out.append(f"- {line.lstrip('•·●▪-* ').strip()}")
        elif "\t" in line:
            title, meta = (part.strip() for part in line.split("\t", 1))
            out.append(f"### {title} | {meta}" if meta else f"### {title}")
        else:
            out.append(line)

    return "\n".join(out).rstrip() + "\n"


def _resume_studio_markdown(resume: dict) -> str:
    processed = resume.get("processed") or {}
    if any(processed.get(key) for key in ("personal_data", "experiences", "projects", "education", "skills")):
        return _normalize_md_for_a4cv(_build_fallback_markdown(processed))
    return _normalize_md_for_a4cv(
        _build_raw_resume_markdown(resume_sanitize.sanitize_resume_content(resume.get("content", "")))
    )


_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$")
_STEP_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*)?(?:step[ \t]*([1-5])|第[ \t]*([一二三四五1-5])[ \t]*步)(?=$|[ \t:：.、·—*\-])",
    re.IGNORECASE,
)


_HEADING_RE = re.compile(r"^#{1,2}\s+\S+", re.MULTILINE)


_SECTION_RE = re.compile(r"^##\s+\S+", re.MULTILINE)
_STUDIO_ENTRY_RE = re.compile(r"^###[ \t]+([^|]+)")
_STUDIO_FIRST_SECTION_RE = re.compile(
    r"^##[ \t]+(?:自我|工作|项目|教育|技能|证书|荣誉|背景|实习|经历|获奖|资格|发表|论文|社团|奖项|标签)"
)


def _draft_blocks(text: str, *, require_step_four: bool):
    blocks, step_four_blocks = [], []
    step = None
    saw_step = False
    fence = None
    for line in text.splitlines():
        marker = _FENCE_RE.fullmatch(line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                if language in {'', 'md', 'markdown'}:
                    value = '\n'.join(lines).strip()
                    blocks.append(value)
                    if step == '4':
                        step_four_blocks.append(value)
                fence = None
            else:
                lines.append(line)
            continue
        if marker:
            fence, language, lines = marker[1], marker[2].strip().lower(), []
            continue
        heading = _STEP_RE.match(line)
        if heading:
            saw_step = True
            step = heading[1] or heading[2].translate(str.maketrans('一二三四五', '12345'))
    return step_four_blocks if saw_step or require_step_four else blocks


def _extract_md_block(text: str, *, require_step_four: bool = False):
    """
    从分析结果抽取有可编辑正文的简历代码块。返回 (md, source)。
    """
    if not text:
        return None, "none"
    matches = _draft_blocks(text, require_step_four=require_step_four)
    if not matches:
        return None, "none"

    # 简历常见中文小节标题；命中一个 +5 分
    resume_section_keywords = (
        "工作经历", "教育背景", "项目经验", "项目经历",
        "技能", "个人信息", "联系方式", "工作业绩",
        "教育经历", "实习经历", "工作项目", "工作项目经验",
    )

    def _score(candidate: str) -> int:
        score = 0
        score += min(len(candidate) // 100, 50)  # 长度分封顶 50
        for kw in resume_section_keywords:
            if kw in candidate:
                score += 5
        # markdown 结构信号
        score += len(_HEADING_RE.findall(candidate))
        score += len(_SECTION_RE.findall(candidate))
        return score

    eligible = (candidate for candidate in matches if _has_studio_sections(candidate))
    best = max(eligible, key=_score, default=None)
    return (best, "extracted") if best else (None, "none")


def _has_studio_sections(markdown: str) -> bool:
    lines = [line for line in _normalize_md_for_a4cv(markdown).splitlines() if line.strip()]
    if not lines or not re.fullmatch(r"#[ \t]+\S.*", lines[0]):
        return False
    if len(lines) > 1 and _SECTION_RE.match(lines[1]) and not _STUDIO_FIRST_SECTION_RE.match(lines[1]):
        body_index = 3 if len(lines) > 2 and lines[2].lstrip().startswith('>') else 2
        if body_index >= len(lines) or _HEADING_RE.match(lines[body_index]):
            lines.pop(1)
    in_section = False
    for line in lines[1:]:
        if _SECTION_RE.match(line):
            in_section = True
        elif in_section and line.startswith('#'):
            entry = _STUDIO_ENTRY_RE.match(line)
            if entry and any(char.isalnum() for char in entry[1]):
                return True
        elif in_section and any(char.isalnum() for char in line):
            return True
    return False


_ALL_HEADING_RE = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)


def _count_sections(md: str) -> int:
    """统计 markdown 中的标题数量（1-3 级），用于返回 sections_detected。"""
    if not md:
        return 0
    return len(_ALL_HEADING_RE.findall(md))


_A4CV_SECTION_KEYWORDS = (
    "自我评价|个人简介|职业概况|个人优势|工作经历|工作经验|项目经历|项目经验|"
    "实习经历|教育经历|教育背景|技能|技能特长|专业技能|证书|证书与荣誉|荣誉奖项|"
    "关键成果|作品集|发表论文|论文发表|社团经历|培训经历"
)


_BOLD_SECTION_RE = re.compile(
    r"^[ \t]*\*{2}\s*(" + _A4CV_SECTION_KEYWORDS + r")\s*[:：]?\*{2}[ \t]*$",
    re.MULTILINE,
)


def _normalize_md_for_a4cv(md: str) -> str:
    """
    把 LLM 生成的简历 markdown 标准化，让 a4cv 编辑器能正确识别结构：
    1. 加粗小节标题转 ## 标题：**工作经历** → ## 工作经历
    2. 首行若是纯名字（无 #），补成 # 姓名（a4cv 靠 # 识别姓名栏）

    a4cv 的 normalizeImportedMarkdown：只要 markdown 含任何 # 标题就走
    normalizeHeadingMarkdown 直接返回，不会自动补 # 姓名。而 LLM 常把姓名
    写成纯文本首行，所以必须在这里补上。
    """
    if not md:
        return md

    def _repl(m):
        return f"## {m.group(1)}"

    md = _BOLD_SECTION_RE.sub(_repl, md)
    md = re.sub(r"^[ \t]+(?=#{1,3}[ \t]+\S)", "", md, flags=re.MULTILINE)

    # 首行补 # 姓名：若首行不是标题、不是空行、也不是联系方式行，视为姓名
    lines = md.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue  # 跳过前导空行
        if stripped.startswith("#"):
            break  # 首个非空行已是标题，无需补
        # 简易判断：联系方式行（含 @ 或多个数字）不当作姓名
        if "@" in stripped or re.search(r"\d{4,}", stripped):
            break
        lines[i] = f"# {stripped}"
        break

    return "\n".join(lines)


def _build_fallback_markdown(processed: dict) -> str:
    """从结构化简历拼 a4cv 兼容的最小 markdown。"""
    pd = processed.get("personal_data") or {}
    name = pd.get("firstName") or pd.get("name") or "你的姓名"
    title = pd.get("title") or pd.get("position") or ""

    contact_bits = []
    for key in ("email", "phone"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))
    loc = pd.get("location")
    if isinstance(loc, dict):
        city = loc.get("city")
        if city:
            contact_bits.append(str(city))
    for key in ("linkedin", "portfolio"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))

    out = [f"# {name}"]
    if title:
        out.append(f"## {title}")
    if contact_bits:
        out += ["", "> " + " · ".join(contact_bits)]

    experiences = processed.get("experiences") or []
    if experiences:
        out += ["", "## 工作经历"]
        for e in experiences:
            t = e.get("job_title") or e.get("jobTitle") or "职位"
            c = e.get("company") or ""
            sd = e.get("start_date") or e.get("startDate") or ""
            ed = e.get("end_date") or e.get("endDate") or ""
            meta = " · ".join(x for x in (c, f"{sd} - {ed}" if sd else "") if x)
            out.append(f"### {t}" + (f" | {meta}" if meta else ""))
            for b in e.get("description") or []:
                if b:
                    out.append(f"- {b}")

    projects = processed.get("projects") or []
    if projects:
        out += ["", "## 项目经历"]
        for p in projects:
            t = p.get("project_name") or p.get("projectName") or "项目"
            desc = p.get("description") or ""
            out.append(f"### {t}")
            if desc:
                out.append(f"- {desc}")

    education = processed.get("education") or []
    if education:
        out += ["", "## 教育背景"]
        for ed in education:
            school = ed.get("institution") or "学校"
            degree = ed.get("degree") or ""
            out.append(f"### {school}" + (f" | {degree}" if degree else ""))

    skills = processed.get("skills") or []
    if skills:
        out += ["", "## 技能标签"]
        names = []
        for s in skills:
            if isinstance(s, dict):
                names.append(s.get("skill_name") or s.get("skillName") or "")
            else:
                names.append(str(s))
        out.append(" · ".join(filter(None, names)))

    achievements = processed.get("achievements") or []
    if achievements:
        out += ["", "## 证书与荣誉"]
        for a in achievements:
            out.append(f"- {a}")

    return "\n".join(out).rstrip() + "\n"
