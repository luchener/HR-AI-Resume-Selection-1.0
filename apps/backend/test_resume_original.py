# -*- coding: utf-8 -*-
"""简历原件留存与渲染单元测试（test_resume_original.py）

验证：
- 原件只按 pdf/docx 留存；文件名去路径（防穿越）；元信息含 sha256 / 大小 / 留存时间
- 删除简历记录时原件同步删除（store.delete_resume 钩子，覆盖用户自助 / 超管 / 超期三条路径）
- 孤儿原件（记录已不存在）能被扫描与回收
- PDF 逐页渲染为 PNG：尺寸正确、有实际像素内容；越界页返回空
- 未留存原件时 info() 返回 available=False（历史简历）

运行：python -m unittest test_resume_original -v
"""

import os
import shutil
import struct
import unittest
import zlib

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-resume-original")
os.environ["ENV"] = "local"

import config  # noqa: E402
import store  # noqa: E402
import resume_original  # noqa: E402

_DIR_NAMES = (
    "users",
    "resumes",
    "jobs",
    "archives",
    "admin_ops",
    "resume_originals",
    "usage",
    "login_failures",
    "rate_limits",
)
_TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata")
_SAMPLE_PDF = os.path.join(_TESTDATA, "sample_en.pdf")
_SAMPLE_COLOR = os.path.join(_TESTDATA, "sample_color.pdf")


def _isolate():
    os.makedirs(_TMP, exist_ok=True)
    config.DATA_DIR = _TMP
    for name in _DIR_NAMES:
        os.makedirs(os.path.join(_TMP, name), exist_ok=True)
    for mod in (config, store):
        for attr in [a for a in dir(mod) if a.endswith("_DIR")]:
            base = os.path.basename(getattr(mod, attr) or "")
            if base in _DIR_NAMES:
                setattr(mod, attr, os.path.join(_TMP, base))
    store.ARCHIVE_INDEX_PATH = os.path.join(store.ARCHIVES_DIR, "_index.json")


_isolate()


def _png_size(png: bytes) -> tuple:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("不是合法 PNG")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _png_raw(png: bytes) -> bytes:
    """解压 IDAT，返回逐行（每行前 1 字节是 filter）的原始像素。"""
    offset = 8
    idat = b""
    while offset + 12 <= len(png):
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        tag = png[offset + 4:offset + 8]
        if tag == b"IDAT":
            idat += png[offset + 8:offset + 8 + length]
        offset += 12 + length
    return zlib.decompress(idat)


def _png_has_ink(png: bytes) -> bool:
    """逐行看像素：存在非白像素说明真的渲染出了内容（不是空白页）。"""
    width, _height = _png_size(png)
    raw = _png_raw(png)
    stride = 1 + width * 3
    for y in range(len(raw) // stride):
        row = raw[y * stride + 1:(y + 1) * stride]
        if any(byte < 200 for byte in row):
            return True
    return False


class ResumeOriginalTests(unittest.TestCase):

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _isolate()

    def test_save_keeps_metadata_and_sanitizes_name(self):
        meta = resume_original.save("rid-1", b"hello-pdf", "../../etc/passwd.pdf")
        self.assertEqual(meta["ext"], "pdf")
        self.assertEqual(meta["name"], "passwd.pdf")  # 去掉路径，防穿越
        self.assertEqual(meta["bytes"], 9)
        self.assertEqual(len(meta["sha256"]), 64)
        self.assertTrue(resume_original.exists("rid-1"))
        self.assertEqual(resume_original.read("rid-1"), b"hello-pdf")

    def test_unsupported_extension_is_not_stored(self):
        self.assertEqual(resume_original.save("rid-2", b"plain", "note.txt"), {})
        self.assertFalse(resume_original.exists("rid-2"))

    def test_info_reports_missing_original(self):
        info = resume_original.info("no-such-resume")
        self.assertFalse(info["available"])
        self.assertFalse(resume_original.exists("no-such-resume"))

    def test_store_delete_removes_original(self):
        resume_id = store.save_resume(content="张三", processed={}, user_id="u1")
        self.assertTrue(resume_original.save(resume_id, b"pdf-bytes", "resume.pdf"))
        self.assertTrue(store.delete_resume(resume_id, user_id="u1"))
        self.assertFalse(resume_original.exists(resume_id))

    def test_sweep_removes_orphans(self):
        resume_original.save("orphan-1", b"x", "a.pdf")
        self.assertIn("orphan-1.pdf", resume_original.orphans())
        self.assertEqual(resume_original.sweep(), 1)
        self.assertFalse(resume_original.exists("orphan-1"))

    @unittest.skipUnless(os.path.exists(_SAMPLE_PDF), "缺少 testdata/sample_en.pdf")
    def test_pdf_render_pages(self):
        with open(_SAMPLE_PDF, "rb") as handle:
            data = handle.read()
        self.assertTrue(resume_original.save("rid-3", data, "sample_en.pdf"))
        info = resume_original.info("rid-3")
        self.assertTrue(info["available"])
        self.assertTrue(info["renderable"])
        self.assertEqual(info["page_count"], 2)

        png, total = resume_original.render_page("rid-3", 1)
        self.assertEqual(total, 2)
        width, height = _png_size(png)
        self.assertGreater(width, 200)
        self.assertGreater(height, 200)
        self.assertTrue(_png_has_ink(png), "渲染结果是一片空白")

        empty, total_again = resume_original.render_page("rid-3", 3)
        self.assertEqual(empty, b"")
        self.assertEqual(total_again, 2)

    @unittest.skipUnless(os.path.exists(_SAMPLE_PDF), "缺少 testdata/sample_en.pdf")
    def test_docx_is_kept_but_not_renderable(self):
        resume_original.save("rid-4", b"PK-fake-docx", "resume.docx")
        info = resume_original.info("rid-4")
        self.assertTrue(info["available"])
        self.assertEqual(info["ext"], "docx")
        self.assertFalse(info["renderable"])
        self.assertEqual(resume_original.render_page("rid-4", 1), (b"", 0))


    @unittest.skipUnless(os.path.exists(_SAMPLE_COLOR), "缺少 testdata/sample_color.pdf")
    def test_png_channel_order_is_rgb(self):
        """红色块必须红通道高、蓝通道低——通道写反会渲染成蓝块。"""
        with open(_SAMPLE_COLOR, "rb") as handle:
            data = handle.read()
        self.assertTrue(resume_original.save("rid-5", data, "sample_color.pdf"))
        png, _total = resume_original.render_page("rid-5", 1)
        width, height = _png_size(png)
        raw = _png_raw(png)
        stride = 1 + width * 3
        x, y = int(width * 0.2), int(height * 0.14)  # 落在红色矩形内部
        offset = y * stride + 1 + x * 3
        red, green, blue = raw[offset], raw[offset + 1], raw[offset + 2]
        self.assertGreater(red, 180, f"红通道过低：{red},{green},{blue}")
        self.assertLess(blue, 90, f"蓝通道过高（通道写反）：{red},{green},{blue}")
        self.assertLess(green, 90, f"绿通道过高：{red},{green},{blue}")


if __name__ == "__main__":
    unittest.main()
