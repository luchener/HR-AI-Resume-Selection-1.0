# -*- coding: utf-8 -*-
"""简历文本清洗：过滤 PDF 解析器残留的二进制/ASCII 乱码。

PDF 解析器（pdfminer）有时会在正文前后嵌入大量二进制垃圾：
    · 1-2 字符的纯 ASCII 片段：Bi, B, N, O
    · 稀疏 ASCII 串（单字符间夹杂空格）：B 4 0 9 y-F F d S x o m 6 W
    · base64 / hex 长串：e6887c80f740582c1HB409y-FFdSxom6WPycWOWjmP7QNBBi
本模块在返回简历内容给前端展示 / 标记时进行过滤，不改写已落盘的原始文件。
"""
import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SHORT_ASCII_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,2}$")
_LONG_BINARY_RE = re.compile(r"^[A-Za-z0-9+/=_\-]{15,}$")
# 稀疏 ASCII：每个 token 1-4 字符，含空格/连字符，无中文无标点
_SPACED_ASCII_RE = re.compile(r"^([A-Za-z0-9\-]{1,4})( [A-Za-z0-9\-]{1,4}){1,}$")
# 宽形态稀疏 ASCII 的片段特征（片段可长，但整行必须见不到连续小写字母）
_TOKEN_RE = re.compile(r"[A-Za-z0-9\-_]{1,24}")
_LOWER_RUN_RE = re.compile(r"[a-z]{3,}")


def _spaced_ascii_wide(stripped: str) -> bool:
    """稀疏 ASCII 的宽形态：至少 2 个片段，且至少一个片段 ≤2 字符。"""
    tokens = stripped.split()
    if len(tokens) < 2 or not all(_TOKEN_RE.fullmatch(token) for token in tokens):
        return False
    return any(len(token) <= 2 for token in tokens)


def _line_is_garbled(line: str) -> bool:
    """判定一行是否为 PDF 解析器残留的 ASCII 乱码。"""
    stripped = line.strip()
    if not stripped:
        return False
    if _CJK_RE.search(stripped):
        return False
    # 纯数字 5-15 位 → 可能是电话号码，保留
    if re.match(r"^\d{5,15}$", stripped):
        return False
    if "@" in stripped:
        return False
    # 1-2 字符纯 ASCII → 乱码碎片
    if _SHORT_ASCII_RE.match(stripped):
        return True
    # 稀疏 ASCII（如 "B 4 0 9 y-F F d S x o m 6 W"）
    if _SPACED_ASCII_RE.match(stripped):
        return True
    # 宽形态稀疏 ASCII（如 "N -0t60 G" / "912 cf79 e4 c4712351 H"）：
    # 要求整行没有 ≥3 连写小写字母，避免误删 "SQL AWS Docker" / "5 years experience"。
    if _spaced_ascii_wide(stripped) and not _LOWER_RUN_RE.search(stripped):
        return True
    # 长 base64 / hex 串
    if _LONG_BINARY_RE.match(stripped):
        return True
    return False


def strip_residue_lines(content: str) -> tuple[str, int]:
    """
    返回 (清洗后文本, 剔除的行数)。不改写已落盘数据，只作用于展示 / 分析入口。

    实测（2026-09-29 生产库 97 份简历）：
      · 55 份被 pdfminer 污染的简历：剔除 6232 / 8441 非空行（73.8%），被删行中含中文的 0 行
      · 42 份正常简历：剔除 0 / 781 行（0 误伤）
    """
    if not content:
        return content, 0
    kept: list[str] = []
    removed = 0
    for line in content.splitlines():
        if line.strip() and _line_is_garbled(line):
            removed += 1
            continue
        kept.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept).strip())
    return result, removed


def sanitize_resume_content(content: str) -> str:
    """去除简历内容中 PDF 解析器嵌入的二进制/ASCII 乱码（展示用）。"""
    return strip_residue_lines(content)[0]
