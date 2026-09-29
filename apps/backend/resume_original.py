"""
简历原始文件留存与渲染（PDF / DOCX）。

为什么留：PDF 缺 ToUnicode 映射时，提取出的文本会混入字形码与内容流操作符（如
"Rj" / "G q n P"），字段还可能被拆开（"姓" 与 "名：王龙龙" 分处两行）——只有
原始文件是完整真相，也支持将来换解析引擎重新提取。

硬约束：
- 只存不导出：管理端以图片形式在线查看，接口不返回 attachment、不提供下载入口。
- 保留期与简历原文一致（config.RETENTION_DAYS，默认 30 天）。删除简历记录时由
  store.delete_resume 同步删除；超期清理与孤儿文件由 retention.purge 回收。
- 渲染只在服务端进行（pypdfium2，BSD 许可），文件字节不出容器。缺渲染库时自动
  降级为「已留存、暂不可渲染」，不影响上传与其它功能。
"""
import hashlib
import os
import re
import struct
import zlib
from datetime import datetime, timezone

import config

try:
    import pypdfium2 as _pdfium
except Exception:  # pragma: no cover - 缺渲染库时降级
    _pdfium = None

_KNOWN_EXTS = ("pdf", "docx")
_MAX_NAME_CHARS = 120
_MAX_RENDER_PAGES = 20
_MAX_LONG_SIDE = 2200
_MAX_RENDER_SCALE = 2.0
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"/|?*]+')


def _dir() -> str:
    path = str(getattr(config, "RESUME_ORIGINALS_DIR", "") or "")
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def _ext_for(filename: str, content_type: str = "") -> str:
    """由扩展名 / MIME 判定原件类型，只认 pdf 与 docx。"""
    name = str(filename or "").lower()
    for ext in _KNOWN_EXTS:
        if name.endswith("." + ext):
            return ext
    mime = str(content_type or "").lower()
    if "pdf" in mime:
        return "pdf"
    if "wordprocessingml" in mime or "msword" in mime:
        return "docx"
    return ""


def _safe_name(filename: str, ext: str) -> str:
    """只保留文件名本身：去掉路径分隔符与控制字符，避免路径穿越与响应头注入。"""
    base = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip("._ ")
    if not base:
        base = "resume." + ext
    return base[:_MAX_NAME_CHARS]


def path_for(resume_id: str) -> str:
    """返回已留存原件的路径；不存在返回空串。"""
    directory = _dir()
    if not directory or not resume_id:
        return ""
    for ext in _KNOWN_EXTS:
        path = os.path.join(directory, "%s.%s" % (resume_id, ext))
        if os.path.exists(path):
            return path
    return ""


def exists(resume_id: str) -> bool:
    return bool(path_for(resume_id))


def ext_for(resume_id: str) -> str:
    path = path_for(resume_id)
    return os.path.splitext(path)[1].lstrip(".").lower() if path else ""


def read(resume_id: str) -> bytes:
    path = path_for(resume_id)
    if not path:
        return b""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def save(resume_id: str, data: bytes, filename: str = "", content_type: str = "") -> dict:
    """
    留存原始文件，返回元信息；类型不支持或写入失败返回空 dict。

    不抛异常：上传链路此时文本已入库，原件留存失败只是少一层保底。
    """
    if not data or not resume_id:
        return {}
    ext = _ext_for(filename, content_type)
    directory = _dir()
    if not ext or not directory:
        return {}
    path = os.path.join(directory, "%s.%s" % (resume_id, ext))
    tmp = "%s.%s.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {}
    return {
        "name": _safe_name(filename, ext),
        "ext": ext,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def delete(resume_id: str) -> bool:
    """删除该简历留存的全部原件文件（含历史扩展名）。"""
    directory = _dir()
    if not directory or not resume_id:
        return False
    removed = False
    for ext in _KNOWN_EXTS:
        try:
            os.remove(os.path.join(directory, "%s.%s" % (resume_id, ext)))
            removed = True
        except OSError:
            continue
    return removed


def can_render(ext: str) -> bool:
    return bool(_pdfium) and str(ext or "").lower() == "pdf"


def page_count(data: bytes, ext: str = "pdf") -> int:
    """总页数；不可渲染或文件损坏返回 0。"""
    if not data or not can_render(ext):
        return 0
    try:
        doc = _pdfium.PdfDocument(data)
    except Exception:
        return 0
    try:
        return int(len(doc))
    except Exception:
        return 0
    finally:
        _close(doc)


def info(resume_id: str, record_meta: dict | None = None) -> dict:
    """
    在线查看所需的元信息。available=False 表示未留存原件（功能上线前的历史简历）。
    """
    path = path_for(resume_id)
    if not path:
        return {"available": False}
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    meta = dict(record_meta or {})
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    total = 0
    if can_render(ext):
        total = page_count(read(resume_id), ext)
    return {
        "available": True,
        "ext": ext,
        "name": str(meta.get("name") or ("resume." + ext)),
        "bytes": int(meta.get("bytes") or size),
        "sha256": str(meta.get("sha256") or ""),
        "stored_at": str(meta.get("stored_at") or ""),
        "renderable": bool(total),
        "page_count": total,
    }


def render_page(resume_id: str, page: int) -> tuple:
    """
    渲染指定页（1 起）为 PNG，返回 (png 字节, 总页数)；不可渲染时返回 (b"", 0)。

    调用方负责权限、审计与响应头（inline / no-store，不提供下载语义）。
    """
    ext = ext_for(resume_id)
    data = read(resume_id)
    if not data or not can_render(ext):
        return b"", 0
    try:
        doc = _pdfium.PdfDocument(data)
    except Exception:
        return b"", 0
    try:
        total = int(len(doc))
        if page < 1 or page > total or page > _MAX_RENDER_PAGES:
            return b"", total
        pdf_page = doc[page - 1]
        try:
            width, height = pdf_page.get_size()
            long_side = max(float(width or 0), float(height or 0), 1.0)
            scale = max(1.0, min(_MAX_RENDER_SCALE, _MAX_LONG_SIDE / long_side))
            bitmap = pdf_page.render(scale=scale)
        except Exception:
            return b"", total
        try:
            return _encode_png(bitmap), total
        finally:
            _close(bitmap)
    finally:
        _close(doc)


def orphans() -> list:
    """孤儿原件文件名：对应的简历记录已不存在（账号删除、手工清理留下的文件）。"""
    directory = _dir()
    if not directory:
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    result = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot or ext.lower() not in _KNOWN_EXTS or not stem:
            continue
        if os.path.exists(os.path.join(config.RESUMES_DIR, "%s.json" % stem)):
            continue
        result.append(name)
    return result


def sweep() -> int:
    """回收孤儿原件，返回删除数量。"""
    directory = _dir()
    removed = 0
    for name in orphans():
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            continue
    return removed


def _close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _encode_png(bitmap) -> bytes:
    """零依赖 PNG 编码（stdlib zlib），不为渲染再引入 Pillow。"""
    width = int(bitmap.width)
    height = int(bitmap.height)
    stride = int(bitmap.stride)
    channels = int(bitmap.n_channels)
    mode = str(getattr(bitmap, "mode", "") or "")
    raw = bytes(bitmap.buffer)
    rows = bytearray()
    for y in range(height):
        start = y * stride
        buf = bytearray(raw[start:start + width * channels])
        if mode.upper().startswith("BGR") and channels >= 3:
            buf[0::channels], buf[2::channels] = buf[2::channels], buf[0::channels]
        if channels == 4:
            buf = bytearray(value for index, value in enumerate(buf) if index % 4 != 3)
        rows += b"\x00" + bytes(buf)
    color_type = 0 if channels == 1 else 2
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )
