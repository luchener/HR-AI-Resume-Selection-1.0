"""Synthetic practice-OR regression tests; no config or real resume/JD IO."""
import unittest

from requirement_branches import match_practice_alternatives


class PracticeAlternativesTests(unittest.TestCase):
    def test_each_branch_alone_matches_explicit_or(self):
        requirement = "有RAG应用、问答系统、SQL自动生成或数据分析助手开发经验"
        for resume, index in (
            ("开发了RAG应用用于检索。", 0),
            ("搭建了知识问答服务。", 1),
            ("实现了NL2SQL转换。", 2),
            ("开发数据分析助手。", 3),
        ):
            with self.subTest(resume=resume):
                result = match_practice_alternatives(requirement, resume)
                self.assertTrue(result["matched"])
                self.assertFalse(result["conflicting"])
                self.assertEqual({m["branch_index"] for m in result["matches"]}, {index})
                self.assertEqual(result["branches"], ["RAG应用开发经验", "问答系统开发经验",
                                                       "SQL自动生成开发经验", "数据分析助手开发经验"])

    def test_multiple_generic_or_lists_and_shared_tails(self):
        for requirement, first, second in (
            ("具备Zeta平台或Orbit工具开发经验", "开发Zeta平台", "实现Orbit工具"),
            ("有海洋监测，卫星追踪任一实施经验", "部署海洋监测", "交付卫星追踪"),
            ("拥有火星数据库、星云平台任一使用经验", "使用火星数据库", "采用星云平台"),
            ("有星云平台或者月球系统实践经验", "搭建星云平台", "使用月球系统"),
            ("有知识问答或text2sql开发经验", "实现问答系统", "开发SQL自动生成"),
        ):
            for resume in (first, second):
                with self.subTest(requirement=requirement, resume=resume):
                    result = match_practice_alternatives(requirement, resume)
                    self.assertIsNotNone(result)
                    self.assertTrue(result["matched"])
                    self.assertEqual(len(result["matches"]), 1)

    def test_related_words_and_skill_list_do_not_establish_practice(self):
        requirement = "有RAG应用、问答系统、SQL自动生成或数据分析助手开发经验"
        for resume in (
            "技能：RAG、知识问答、text2sql、分析助手。",
            "了解问答系统，熟悉RAG。", "开发通用Agent并调用API。",
            "编写SQL查询。", "开发NoSQL服务。", "开发text2sqlish工具。",
            "使用问答系统。", "问答系统\n开发了API。",
            "RAG。开发通用服务。", "项目：知识问答\n负责接口开发。",
            "知识问答，负责接口开发。",
        ):
            with self.subTest(resume=resume):
                result = match_practice_alternatives(requirement, resume)
                self.assertFalse(result["matched"])
                self.assertEqual(result["matches"], [])

    def test_negation_never_positive_and_conflicts_are_branch_local(self):
        requirement = "有问答系统或SQL自动生成开发经验"
        for resume in ("从未开发问答系统。", "没有知识问答开发经验。", "不会开发NL2SQL。",
                       "未开展问答系统开发。", "没做过知识问答开发。", "缺少NL2SQL开发经验。"):
            with self.subTest(resume=resume):
                result = match_practice_alternatives(requirement, resume)
                self.assertFalse(result["matched"])
                self.assertTrue(result["negative_matches"])
                self.assertFalse(result["conflicting"])
        result = match_practice_alternatives(requirement, "开发了知识问答。\n尚未开发问答系统。")
        self.assertTrue(result["matched"])
        self.assertTrue(result["conflicting"])
        result = match_practice_alternatives(requirement, "没有开发问答系统，但实现了text2sql。")
        self.assertTrue(result["matched"])
        self.assertFalse(result["conflicting"])
        self.assertEqual(result["matches"][0]["branch_index"], 1)
        result = match_practice_alternatives(requirement, "不仅开发问答系统，还实现text2sql。")
        self.assertTrue(result["matched"])
        self.assertFalse(result["negative_matches"])

    def test_intent_including_heading_never_positive(self):
        requirement = "有问答系统或RAG应用开发经验"
        for resume in ("求职意向：开发问答系统。", "希望开发RAG应用。",
                       "计划搭建问答系统。", "求职意向\n开发问答系统\n搭建RAG应用"):
            with self.subTest(resume=resume):
                self.assertFalse(match_practice_alternatives(requirement, resume)["matched"])
        resume = "求职意向\n开发RAG应用\n\n项目经历\n开发了问答系统。"
        result = match_practice_alternatives(requirement, resume)
        self.assertTrue(result["matched"])
        self.assertEqual({m["branch_index"] for m in result["matches"]}, {0})

    def test_and_ambiguous_and_complex_requirements_fall_back(self):
        for requirement in (
            "有问答系统并且RAG应用开发经验", "有问答系统且RAG应用开发经验",
            "有问答系统同时RAG应用开发经验", "有问答系统和RAG应用开发经验",
            "有问答系统、RAG应用开发经验", "有问答系统或RAG应用经验",
            "熟悉问答系统或RAG应用", "有问答系统或RAG应用开发经验且精通Python",
            "有问答系统或RAG应用开发经验，同时有SQL自动生成实践经验",
            "有(A或B)且C开发经验", "有A或B或开发经验", "有A或B实施和开发经验",
            "有问答系统开发经验或RAG应用开发经验", "有A或B开发经验；有C或D开发经验",
        ):
            with self.subTest(requirement=requirement):
                self.assertIsNone(match_practice_alternatives(requirement, "开发了A和问答系统及RAG应用。"))

    def test_exact_quote_offsets_unicode_crlf_and_repeated_text(self):
        requirement = "有问答系统或SQL自动生成开发经验"
        resume = "张某😀\r\n项目经历\r\n  开发知识问答。\r\n开发知识问答；没有开发NL2SQL。\r\n实现text2sql。"
        result = match_practice_alternatives(requirement, resume)
        for item in result["matches"] + result["negative_matches"]:
            self.assertEqual(resume[item["start"]:item["end"]], item["quote"])
        first, second = [item for item in result["matches"] if item["branch_index"] == 0]
        self.assertEqual(first["quote"], second["quote"])
        self.assertNotEqual(first["start"], second["start"])
        self.assertEqual(first["start"], resume.index("开发知识问答"))

    def test_empty_resume_and_invalid_inputs(self):
        self.assertFalse(match_practice_alternatives("有A或B开发经验", "")["matched"])
        self.assertIsNone(match_practice_alternatives(None, "开发A"))
        self.assertIsNone(match_practice_alternatives("有A或B开发经验", None))


if __name__ == "__main__":
    unittest.main()
