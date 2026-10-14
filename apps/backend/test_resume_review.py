import unittest

import resume_review


class ResumeReviewTests(unittest.TestCase):
    def test_markers_only_reference_original_resume_text(self):
        content = "张三\n负责 Java 服务开发和上线\n负责招聘培训"
        result = resume_review.build_review_markers(
            content,
            {
                "final_score": 88,
                "recruitment_recommendation": "优先面试",
                "strengths": ["负责 Java 服务开发和上线"],
                "skill_match": {"project_match_points": ["负责招聘培训"], "hard_skills": ["Python（简历未体现）"]},
                "risk_points": ["无明显风险"],
            },
        )
        self.assertEqual(len(result["annotations"]), 2)
        for annotation in result["annotations"]:
            self.assertEqual(content[annotation["start"]:annotation["end"]], annotation["quote"])

    def test_missing_quotes_are_not_marked(self):
        result = resume_review.build_review_markers(
            "候选人经历",
            {"strengths": ["不存在的经历"], "skill_match": {}, "risk_points": []},
        )
        self.assertEqual(result["annotations"], [])

    def test_structured_education_gets_a_verify_marker(self):
        """原实现把 degree+school+major 拼成一个串去原文里找，结构化教育经历一条标记都挂不上。"""
        content = "张三\n复旦大学 计算机科学与技术 本科\n2016-2020"
        result = resume_review.build_review_markers(
            content,
            {
                "education_history": [
                    {"degree": "本科", "school_name": "复旦大学", "major": "计算机科学与技术"}
                ],
                "strengths": [],
                "skill_match": {},
                "risk_points": [],
            },
        )

        verify = [item for item in result["annotations"] if item["category"] == "verify"]
        self.assertTrue(verify)
        self.assertIn("学信网", verify[0]["reason"])

    def test_certificates_get_their_own_verify_marker(self):
        content = "张三\n证书：PMP 认证（2019）\n技能：Python"
        result = resume_review.build_review_markers(
            content,
            {"certificates": ["PMP 认证"], "education_history": [], "strengths": [], "skill_match": {}, "risk_points": []},
        )

        labels = {item["label"] for item in result["annotations"]}
        self.assertIn("证书待核实", labels)
        cert = next(item for item in result["annotations"] if item["label"] == "证书待核实")
        self.assertIn("原件", cert["reason"])

    def test_verify_marker_does_not_fire_on_unrelated_text(self):
        content = "张三\n负责 Java 服务开发\n负责招聘培训"
        result = resume_review.build_review_markers(
            content,
            {"education_history": [], "certificates": [], "strengths": [], "skill_match": {}, "risk_points": []},
        )

        self.assertEqual([item for item in result["annotations"] if item["category"] == "verify"], [])


    def test_overlapping_same_category_markers_are_deduped(self):
        """“复旦大学”与“复旦大学计算机科学与技术”会同时命中，不能叠成重影。"""
        content = "张三\n复旦大学计算机科学与技术本科\n技能：Python"
        result = resume_review.build_review_markers(
            content,
            {
                "education_history": [
                    {"degree": "本科", "school_name": "复旦大学", "major": "复旦大学计算机科学与技术"}
                ],
                "certificates": [],
                "strengths": [],
                "skill_match": {},
                "risk_points": [],
            },
        )

        spans = [(item["start"], item["end"]) for item in result["annotations"]]
        for index, (start, end) in enumerate(spans):
            for other_start, other_end in spans[index + 1:]:
                self.assertFalse(start < other_end and other_start < end, spans)

    def test_different_categories_may_still_share_a_line(self):
        """同类去重，但跨类别允许落在同一段文字上（亮点与待核实是两回事）。"""
        content = "张三\n负责支付网关重构\n证书：PMP"
        result = resume_review.build_review_markers(
            content,
            {
                "education_history": [],
                "certificates": ["PMP"],
                "strengths": ["负责支付网关重构"],
                "skill_match": {},
                "risk_points": [],
            },
        )

        categories = {item["category"] for item in result["annotations"]}
        self.assertIn("strength", categories)
        self.assertIn("verify", categories)


if __name__ == "__main__":
    unittest.main()
