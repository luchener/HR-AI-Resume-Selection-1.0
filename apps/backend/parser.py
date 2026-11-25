"""
文档解析：从 PDF / DOCX 提取纯文本。
逻辑照搬自旧版 resume_service.py，仅去掉类封装。
"""
import io
import zipfile
import xml.etree.ElementTree as _stdlib_et

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring
from pdfminer.high_level import extract_text

try:
    import pypdfium2 as _pdfium
except Exception:  # pragma: no cover - dependency is pinned in requirements.txt
    _pdfium = None

# DOCX 段落命名空间
_WML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_EXTRACTED_TEXT_CHARS = 2 * 1024 * 1024
MAX_DOCX_ENTRIES = 512
MAX_DOCX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DOCX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200
MAX_DOCX_XML_PARTS = 64


class InvalidDocument(ValueError):
    """The uploaded bytes are not a supported or well-formed document."""


class DocumentTooLarge(ValueError):
    """The document exceeds a parser resource limit."""

    http_status = 413


def _extract_word_xml(xml_bytes: bytes) -> str:
    """Extract text while preserving paragraphs, table cells and hard breaks."""
    try:
        root = safe_fromstring(xml_bytes)
    except (DefusedXmlException, _stdlib_et.ParseError) as exc:
        raise InvalidDocument("Invalid DOCX XML") from exc

    out: list[str] = []
    total = 0

    def append(value: str) -> None:
        nonlocal total
        if total >= MAX_EXTRACTED_TEXT_CHARS:
            return
        value = value[:MAX_EXTRACTED_TEXT_CHARS - total]
        out.append(value)
        total += len(value)

    stack = [(root, False)]
    while stack and total < MAX_EXTRACTED_TEXT_CHARS:
        elem, exiting = stack.pop()
        tag = elem.tag
        if exiting:
            if tag == f"{_WML_NS}tc" and out and not out[-1].endswith(("\t", "\n")):
                append("\t")
            elif tag == f"{_WML_NS}p" and out and not out[-1].endswith("\n"):
                append("\n")
            continue

        if tag == f"{_WML_NS}t" and elem.text:
            append(elem.text)
        elif tag == f"{_WML_NS}tab":
            append("\t")
        elif tag in (f"{_WML_NS}br", f"{_WML_NS}cr"):
            append("\n")
        elif tag == f"{_WML_NS}noBreakHyphen":
            append("-")

        stack.append((elem, True))
        for child in reversed(list(elem)):
            stack.append((child, False))

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


def _pdf_page_count(file_bytes: bytes) -> int:
    if _pdfium is None:
        raise InvalidDocument("PDF parser is unavailable")
    try:
        document = _pdfium.PdfDocument(file_bytes)
    except Exception as exc:
        raise InvalidDocument("Invalid PDF file") from exc
    try:
        return int(len(document))
    except Exception as exc:
        raise InvalidDocument("Invalid PDF file") from exc
    finally:
        try:
            document.close()
        except Exception:
            pass


def extract_pdf(file_bytes: bytes) -> str:
    """Extract at most MAX_PDF_PAGES pages from a structurally valid PDF."""
    pages = _pdf_page_count(file_bytes)
    if pages < 1:
        raise InvalidDocument("Invalid PDF file")
    if pages > MAX_PDF_PAGES:
        raise DocumentTooLarge("PDF page limit exceeded")
    try:
        return extract_text(io.BytesIO(file_bytes), maxpages=MAX_PDF_PAGES)
    except Exception as exc:
        raise InvalidDocument("Invalid PDF file") from exc


def _validate_docx_member(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise InvalidDocument("Encrypted DOCX is not supported")
    if info.file_size < 0 or info.compress_size < 0:
        raise InvalidDocument("Invalid DOCX file")
    if info.file_size > MAX_DOCX_ENTRY_BYTES:
        raise DocumentTooLarge("DOCX entry limit exceeded")
    if info.file_size:
        if not info.compress_size:
            raise DocumentTooLarge("Invalid DOCX compression sizes")
        if info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO:
            raise DocumentTooLarge("DOCX compression ratio limit exceeded")


def _read_docx_member(docx: zipfile.ZipFile, name: str, total: int) -> tuple[bytes, int]:
    try:
        info = docx.getinfo(name)
    except KeyError as exc:
        raise InvalidDocument("Invalid DOCX file: missing required XML part") from exc
    _validate_docx_member(info)
    with docx.open(info) as handle:
        data = handle.read(MAX_DOCX_ENTRY_BYTES + 1)
    if len(data) > MAX_DOCX_ENTRY_BYTES:
        raise DocumentTooLarge("DOCX entry limit exceeded")
    total += len(data)
    if total > MAX_DOCX_TOTAL_BYTES:
        raise DocumentTooLarge("DOCX expanded size limit exceeded")
    return data, total


def extract_docx(file_bytes: bytes) -> str:
    """
    手写 zip+xml 解析 DOCX（仅依赖标准库，不装 python-docx）。
    按文档序输出，遇 <w:tab> 插入 \\t、<w:br> 插入 \\n、<w:p> 末尾换行。
    这样双列布局的简历（个人信息｜联系方式）不会被压成一团。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            members = docx.infolist()
            if len(members) > MAX_DOCX_ENTRIES:
                raise DocumentTooLarge("DOCX entry count limit exceeded")
            if sum(info.file_size for info in members) > MAX_DOCX_TOTAL_BYTES:
                raise DocumentTooLarge("DOCX expanded size limit exceeded")
            for info in members:
                _validate_docx_member(info)

            total = 0
            document, total = _read_docx_member(docx, "word/document.xml", total)
            supplemental = sorted(
                name for name in docx.namelist()
                if name.startswith(("word/header", "word/footer")) and name.endswith(".xml")
            )
            if len(supplemental) + 1 > MAX_DOCX_XML_PARTS:
                raise DocumentTooLarge("DOCX XML part limit exceeded")
            parts = [document]
            for name in supplemental:
                part, total = _read_docx_member(docx, name, total)
                parts.append(part)
    except zipfile.BadZipFile as exc:
        raise InvalidDocument("Invalid DOCX file") from exc

    return _normalize_extracted_text("\n".join(_extract_word_xml(part) for part in parts))


def extract_text_from_file(file_bytes: bytes, content_type: str) -> str:
    """
    根据 MIME 类型提取文本。

    content_type: application/pdf 或
                  application/vnd.openxmlformats-officedocument.wordprocessingml.document
    """
    if type(file_bytes) is not bytes or not file_bytes:
        raise InvalidDocument("No file content provided")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise DocumentTooLarge("Uploaded file exceeds the 30 MB limit")

    if content_type == "application/pdf":
        text = extract_pdf(file_bytes)
    elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text = extract_docx(file_bytes)
    else:
        raise InvalidDocument("Unsupported file type")

    text = text[:MAX_EXTRACTED_TEXT_CHARS]
    text = _normalize_extracted_text(text)
    if not text:
        raise InvalidDocument("No text could be extracted from the uploaded file")
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
