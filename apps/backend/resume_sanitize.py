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
    # 长 base64 / hex 串
    if _LONG_BINARY_RE.match(stripped):
        return True
    return False


def sanitize_resume_content(content: str) -> str:
    """去除简历内容中 PDF 解析器嵌入的二进制/ASCII 乱码。"""
    if not content:
        return content
    filtered = [line for line in content.splitlines() if not _line_is_garbled(line)]
    result = "\n".join(filtered).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result
