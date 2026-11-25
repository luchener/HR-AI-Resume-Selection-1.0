"""Conservative candidate-name extraction for resume uploads and library reads."""
import re

CANDIDATE_NAME_PLACEHOLDERS = frozenset({
    "-", "--", "—", "n/a", "none", "unknown", "null", "未提供", "未知", "无法识别",
    "未识别", "未识别姓名", "未命名", "未命名候选人", "候选人", "姓名", "名字", "待确认",
    "简历", "个人简历", "求职简历", "基本信息", "个人信息", "个人优势", "工作经历",
    "教育经历", "项目经历", "专业技能", "保密", "不公开", "待补充", "resume", "cv",
    "电话", "联系电话", "手机", "手机号", "联系方式", "邮箱", "电子邮箱", "年龄", "性别",
    "地址", "住址", "求职意向", "phone", "telephone", "mobile", "email", "gender", "address",
})
_NAME_LABEL = r"(?:姓\s*名|名字|名\s*(?=[:：])|(?:full\s+)?name)"
_LABEL = re.compile(rf"(?:^|[\t|；;])[ ]*{_NAME_LABEL}[ ]*(?:[:：|]|\t)", re.IGNORECASE)
_LABEL_ONLY = re.compile(rf"^{_NAME_LABEL}\s*[:：|]?$", re.IGNORECASE)
_OTHER_FIELD = re.compile(
    r"\s*(?:电话|手机|联系方式|邮箱|年龄|性别|地址|求职意向|phone|email|gender)\s*[:：]"
    r"|\s+(?=\d{7,15}(?:\D|$))",
    re.IGNORECASE,
)
_CJK_NAME = re.compile(r"[\u3400-\u9fff]{2,6}(?:[·・][\u3400-\u9fff]{1,6}){0,2}")
_LATIN_NAME = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[.'’\-][A-Za-zÀ-ÖØ-öø-ÿ]+)*(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[.'’\-][A-Za-zÀ-ÖØ-öø-ÿ]+)*){0,4}")
_COMMON_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹苏潘葛范彭鲁"
    "韦马方俞任袁柳鲍史唐费薛雷贺倪汤滕罗毕安傅齐康伍余顾孟黄穆萧尹姚邵汪毛戴宋熊"
    "舒屈项董梁杜阮蓝闵席季贾路娄江颜郭梅林钟徐邱骆高夏蔡田胡凌霍虞万支柯管莫房"
    "卢解应宗丁邓单洪包左石崔程陆荣翁荀羊甄封芮储靳焦伊叶白赖卓谭蒙池乔闻党翟"
    "劳甘黎龚文温庄柴瞿阎连涂艾向龙段覃欧区司诸慕肖刘曾廖邝尤付阳"
)
_SECTION_WORDS = (
    "优势", "简介", "评价", "意向", "工作", "项目", "教育", "技能", "证书", "经验", "经历",
    "简历", "候选人", "工程师", "经理", "主管", "总监", "开发", "设计", "管理", "资料",
    "电话", "手机", "邮箱", "年龄", "性别", "期望", "求职", "住址", "地址", "联系方式",
    "个人", "基本", "信息", "公司", "附件", "文件", "模板", "测试", "熟悉", "精通", "负责", "擅长",
)
_HEADER_LINES = {"简历", "个人简历", "求职简历", "中文简历", "基本信息", "个人信息", "resume", "cv", "personal information"}
_ENGLISH_HEADINGS = {
    "about me", "personal profile", "professional summary", "work experience", "career objective",
    "contact information", "technical skills", "key skills", "education", "experience", "skills",
}


def clean_candidate_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = " ".join(value.split())
    return name[:256] if name.casefold() not in CANDIDATE_NAME_PLACEHOLDERS else ""


def _name_field(value: str, *, explicit: bool = False) -> str:
    value = _OTHER_FIELD.split(value, maxsplit=1)[0]
    value = re.split(r"[|\t,，。;；]", value, maxsplit=1)[0].strip(" \t:：|")
    compact = re.sub(r"\s+", "", value)
    if _CJK_NAME.fullmatch(compact):
        if explicit or (len(compact) <= 4 and compact[0] in _COMMON_SURNAMES
                        and not any(word in compact for word in _SECTION_WORDS)):
            return clean_candidate_name(compact)
    elif _LATIN_NAME.fullmatch(value):
        if value.casefold() not in _ENGLISH_HEADINGS and (
            explicit or (" " in value and all(part[0].isupper() and not part.isupper() for part in value.split()))
        ):
            return clean_candidate_name(value)
    return ""


def _header_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"[\u200b\ufeff]", "", raw).replace("**", "").replace("__", "")
        line = line.strip(" #>\t")
        if line:
            lines.append(line)
            if len(lines) == 61:
                break
    return lines


def _labeled_name(lines: list[str]) -> str:
    for index, line in enumerate(lines[:60]):
        label = _LABEL.search(line)
        value = line[label.end():].strip() if label else ""
        if value and (name := _name_field(value, explicit=True)):
            return name
        if _LABEL_ONLY.fullmatch(line.strip(" |\t")) and index + 1 < len(lines):
            continuation = lines[index + 1].lstrip(" |\t")
            name = _name_field(continuation, explicit=True)
            if name and not any(word in name for word in _SECTION_WORDS):
                return name
    return ""


def _header_name(lines: list[str]) -> str:
    for line in lines[:8]:
        if re.sub(r"\s+", "", line).casefold() in _HEADER_LINES or line.casefold() in _HEADER_LINES:
            continue
        if any(word in line for word in ("工作经历", "教育经历", "项目经历", "专业技能", "个人优势", "个人简介", "自我评价")):
            break
        if "：" in line or ":" in line:
            continue
        if name := _name_field(line):
            return name
    return ""


def candidate_name_from_text(text: str) -> str:
    lines = _header_lines(text)
    return _labeled_name(lines) or _header_name(lines)


def candidate_name_from_filename(filename: str) -> str:
    basename = re.split(r"[/\\]", filename or "")[-1]
    stem, extension = basename.rsplit(".", 1) if "." in basename else (basename, "")
    if extension.lower() not in {"pdf", "docx"}:
        return ""
    stem = re.sub(r"[【】\[\]（）()]", " ", stem)
    stem = re.sub(r"个人简历|求职简历|简历|\b(?:resume|cv)\b", " ", stem, flags=re.IGNORECASE)
    names = {
        name for token in re.split(r"[\s_\-—]+", stem)
        if token and _CJK_NAME.fullmatch(token) and (name := _name_field(token))
    }
    return next(iter(names)) if len(names) == 1 else ""


def resolve_candidate_name(
    *known_names: object, text: str = "", filename: str = "", fallback_names: tuple[object, ...] = (),
) -> str:
    for value in known_names:
        if name := clean_candidate_name(value):
            return name
    lines = _header_lines(text)
    if name := _labeled_name(lines):
        return name
    for value in fallback_names:
        if name := clean_candidate_name(value):
            return name
    return _header_name(lines) or candidate_name_from_filename(filename)

