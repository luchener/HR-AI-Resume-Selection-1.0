"""
文档解析：从 PDF / DOCX 提取纯文本。
逻辑照搬自旧版 resume_service.py，仅去掉类封装。
"""
import io
import zipfile
import xml.etree.ElementTree as ET

from pdfminer.high_level import extract_text

# DOCX 段落命名空间
_WML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_word_xml(xml_bytes: bytes) -> str:
    """Extract text while preserving paragraphs, table cells and hard breaks."""
    root = ET.fromstring(xml_bytes)
    out: list[str] = []

    def walk(elem):
        tag = elem.tag
        if tag == f"{_WML_NS}t" and elem.text:
            out.append(elem.text)
        elif tag == f"{_WML_NS}tab":
            out.append("\t")
        elif tag in (f"{_WML_NS}br", f"{_WML_NS}cr"):
            out.append("\n")
        elif tag == f"{_WML_NS}noBreakHyphen":
            out.append("-")

        for child in elem:
            walk(child)

        if tag == f"{_WML_NS}tc" and out and not out[-1].endswith(("\t", "\n")):
            out.append("\t")
        elif tag == f"{_WML_NS}p" and out and not out[-1].endswith("\n"):
            out.append("\n")

    walk(root)
    return "".join(out)


def _normalize_extracted_text(text: str) -> str:
    lines = []
    blank = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(part for part in raw_line.replace("\xa0", " ").split(" ") if part)
        line = line.strip(" \t")
        if line:
            lines.append(line)
            blank = False
        elif lines and not blank:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def extract_pdf(file_bytes: bytes) -> str:
    """用 pdfminer 提取 PDF 文本。"""
    return extract_text(io.BytesIO(file_bytes))


def extract_docx(file_bytes: bytes) -> str:
    """
    手写 zip+xml 解析 DOCX（仅依赖标准库，不装 python-docx）。
    按文档序输出，遇 <w:tab> 插入 \\t、<w:br> 插入 \\n、<w:p> 末尾换行。
    这样双列布局的简历（个人信息｜联系方式）不会被压成一团。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            parts = [docx.read("word/document.xml")]
            supplemental = sorted(
                name for name in docx.namelist()
                if name.startswith(("word/header", "word/footer")) and name.endswith(".xml")
            )
            parts.extend(docx.read(name) for name in supplemental)
    except KeyError as e:
        raise ValueError("Invalid DOCX file: missing word/document.xml") from e
    except zipfile.BadZipFile as e:
        raise ValueError("Invalid DOCX file") from e

    return _normalize_extracted_text("\n".join(_extract_word_xml(part) for part in parts))


def extract_text_from_file(file_bytes: bytes, content_type: str) -> str:
    """
    根据 MIME 类型提取文本。

    content_type: application/pdf 或
                  application/vnd.openxmlformats-officedocument.wordprocessingml.document
    """
    if content_type == "application/pdf":
        text = extract_pdf(file_bytes)
    elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text = extract_docx(file_bytes)
    else:
        raise ValueError("Unsupported file type")

    text = _normalize_extracted_text(text)
    if not text:
        raise ValueError("No text could be extracted from the uploaded file")
    return text


def summarize_job_locally(text: str) -> dict:
    """Build a fast preview for the UI without spending an LLM request."""
    lines = [line.strip(" \t-•") for line in text.splitlines() if line.strip()]
    title = lines[0][:100] if lines else "未命名岗位"
    bullet_lines = [line[:180] for line in lines[1:] if len(line) >= 6]
    required = [
        line for line in bullet_lines
        if any(word in line.lower() for word in ("要求", "具备", "熟悉", "精通", "经验", "学历", "required", "must"))
    ]
    responsibilities = [
        line for line in bullet_lines
        if any(word in line.lower() for word in ("负责", "职责", "工作", "推动", "搭建", "responsib"))
    ]
    return {
        "job_title": title,
        "job_summary": " ".join(lines[:4])[:300],
        "key_responsibilities": responsibilities[:8] or bullet_lines[:5],
        "qualifications": {"required": required[:8]},
    }
