# -*- coding: utf-8 -*-
"""PDF 解析残留清洗规则测试（样本取自 2026-09-29 生产库真实简历）。"""
import unittest

import resume_sanitize as rs


class ResidueLineTests(unittest.TestCase):
    # 生产库里实际出现的残留形态
    RESIDUE = [
        "Rj",
        "R",
        "w",
        "M",
        "F B",
        "G q n P",
        "P q a W",
        "Y 2 9 W",
        "Y29 W",
        "N -0t60 G",
        "N -0t6 0 G",
        "912 cf79 e4 c4712351 H",
        "e6887 c80f740582 c1 H",
        "912cf79e4c4712351HN-0t60GFBXwY29WPqaWOGqnPHQMRRj",
    ]
    # 真内容：一行都不能删
    KEEP = [
        "金利源",
        "男 |年龄：32岁 |",
        "18005835438 |",
        "779221546@qq.com",
        "10年工作经验 |求职意向：运维工程师 |期望薪资：7-8K",
        "个人优势",
        "1.熟悉常见通讯协议及网络协议，具备良好的技术理解能力",
        "嘉兴前程服务外包有限公司软件实施工程师",
        "2016.09-至今",
        "Java Developer",
        "5 years experience in Python",
        "SQL AWS Docker",
        "Node.js / React",
        "Golang",
        "2012-2016",
    ]

    def test_residue_forms_are_detected(self):
        for line in self.RESIDUE:
            self.assertTrue(rs._line_is_garbled(line), f"残留行未被识别：{line!r}")

    def test_real_content_is_never_garbled(self):
        for line in self.KEEP:
            self.assertFalse(rs._line_is_garbled(line), f"真内容被误判为残留：{line!r}")

    def test_strip_returns_removed_count(self):
        raw = "\n\n".join(["金利源", "Rj", "R", "个人优势", "G q n P", "工作经历"])
        clean, removed = rs.strip_residue_lines(raw)
        self.assertEqual(removed, 3)
        self.assertIn("金利源", clean)
        self.assertIn("工作经历", clean)
        self.assertNotIn("G q n P", clean)
        self.assertNotIn("Rj", clean)
        self.assertNotIn("\n\n\n", clean)

    def test_clean_document_is_untouched(self):
        raw = "张三\nPython 后端工程师\n负责订单系统\n5 年经验"
        clean, removed = rs.strip_residue_lines(raw)
        self.assertEqual(removed, 0)
        self.assertEqual(clean, raw)

    def test_empty_input(self):
        self.assertEqual(rs.strip_residue_lines(""), ("", 0))
        self.assertEqual(rs.sanitize_resume_content(""), "")

    def test_real_polluted_sample_keeps_every_chinese_line(self):
        """生产库真实样本：中文行一行不少，垃圾行全消失。"""
        raw = (
            "Rj\n\nR\n\nQ\n\nH\n\nG q n P\n\nO\n\nP q a W\n\nY 2 9 W\n\nw\n\nX\n\nF B\n\n"
            "N -0t6 0 G\n\n912cf79e4c4712351HN-0t60GFBXwY29WPqaWOGqnPHQMRRj\n\n"
            "金利源\n\n男 |年龄：32岁 |\n\n18005835438 |\n\n779221546@qq.com\n\n"
            "10年工作经验 |求职意向：运维工程师 |期望薪资：7-8K |期望城市：嘉兴\n\n"
            "个人优势\n\n1.熟悉常见通讯协议及网络协议，具备良好的技术理解能力\n\n"
            "工作经历\n\n嘉兴前程服务外包有限公司软件实施工程师\n\n2016.09-至今\n"
        )
        clean, removed = rs.strip_residue_lines(raw)
        self.assertGreater(removed, 10)
        for must_keep in (
            "金利源",
            "个人优势",
            "工作经历",
            "嘉兴前程服务外包有限公司软件实施工程师",
            "2016.09-至今",
            "779221546@qq.com",
        ):
            self.assertIn(must_keep, clean)
        for must_drop in ("Rj", "G q n P", "Y 2 9 W", "912cf79e4c4712351HN"):
            self.assertNotIn(must_drop, clean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
