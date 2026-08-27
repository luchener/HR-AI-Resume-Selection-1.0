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


if __name__ == "__main__":
    unittest.main()
