import unittest
from unittest.mock import patch

import screening_agent


class ScreeningAgentTests(unittest.TestCase):
    def _report(self):
        return {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 20,
                "responsibility_overlap": 20,
                "skills_projects": 20,
                "industry_background": 10,
                "evidence_bonus": 8,
            },
            "job_fit_score": 78,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "经历与岗位基本匹配。",
            "basic_screening": {},
            "work_history": {},
            "skill_match": {},
            "strengths": ["有产品经验"],
            "weaknesses": ["部分技能未体现"],
            "risk_points": ["无明显风险"],
            "recruitment_recommendation": "储备观察",
            "fit_tag": "部分匹配",
        }

    def test_agent_retries_once_when_hard_requirement_is_missing(self):
        first = self._report()
        first["score_breakdown"]["hard_requirements"] = 20
        second = dict(self._report())
        second["summary"] = "已核对本科及以上学历要求，经历基本匹配。"
        second["score_breakdown"] = dict(self._report()["score_breakdown"])
        with patch.object(
            screening_agent,
            "_extract_requirements",
            return_value={"requirements": [{"id": "req-1", "text": "本科及以上学历", "category": "education", "hard": True, "logic": "required"}]},
        ), patch.object(screening_agent.llm, "call_llm", side_effect=[first, second]) as call:
            result = screening_agent.run_screening_agent(
                job_content="本科及以上学历",
                resume_content="张三\n本科，复旦大学",
                current_date="2026-08",
            )
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["agent_meta"]["retry_count"], 1)
        self.assertTrue(result["agent_meta"]["validation_passed"])

    def test_agent_returns_second_result_when_validation_still_fails(self):
        report = self._report()
        report["score_breakdown"]["hard_requirements"] = 19
        with patch.object(
            screening_agent,
            "_extract_requirements",
            return_value={"requirements": [{"id": "req-1", "text": "Java 技能", "category": "skill", "hard": True, "logic": "required"}]},
        ), patch.object(screening_agent.llm, "call_llm", side_effect=[report, report]) as call:
            result = screening_agent.run_screening_agent(
                job_content="必须掌握 Java",
                resume_content="张三\n产品经理",
                current_date="2026-08",
            )
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["agent_meta"]["retry_count"], 1)
        self.assertFalse(result["agent_meta"]["validation_passed"])
        self.assertTrue(result["agent_meta"]["validation_issues"])

    def test_agent_enforces_model_call_budget(self):
        report = self._report()
        requirements = {"requirements": [{"id": "r1", "text": "Java 技能", "category": "skill", "hard": True}]}
        original = screening_agent.MAX_AGENT_LLM_CALLS
        try:
            with patch.object(screening_agent, "_extract_requirements", return_value=requirements), patch.object(screening_agent.llm, "call_llm", return_value=report) as call:
                screening_agent.MAX_AGENT_LLM_CALLS = 2
                result = screening_agent.run_screening_agent(job_content="Java", resume_content="候选人", current_date="2026-08")
            self.assertLessEqual(call.call_count, 2)
            self.assertLessEqual(result["agent_meta"]["llm_calls"], 2)
        finally:
            screening_agent.MAX_AGENT_LLM_CALLS = original

    def test_clean_report_skips_reflection_llm_call(self):
        report = self._report()
        report["summary"] = "候选人满足本科及以上学历，经历与岗位基本匹配。"
        requirements = {"requirements": [{"id": "r1", "text": "本科及以上学历", "category": "education", "hard": True}]}
        with patch.object(screening_agent, "_extract_requirements", return_value=requirements), patch.object(screening_agent.llm, "call_llm", return_value=report) as call:
            result = screening_agent.run_screening_agent(job_content="本科及以上学历", resume_content="张三\n本科，复旦大学", current_date="2026-08")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["agent_validation"]["mode"], "预检")
        self.assertTrue(result["agent_validation"]["passed"])
        self.assertFalse(result["agent_validation"]["revised"])
        self.assertEqual(result["agent_validation"]["issues"], [])

    def _suspicious_report(self):
        report = self._report()
        report["summary"] = "满足本科及以上学历，经历与岗位基本匹配。"
        report["basic_screening"] = {"education_level": "本科", "salary_expectation": "30K-40K"}
        report["strengths"] = ["精通 Spark 流式计算"]
        return report

    def test_precheck_flags_fabricated_salary_and_absent_tech(self):
        findings = screening_agent._precheck_findings(
            self._suspicious_report(),
            {"requirements": []},
            "张三\n本科，复旦大学\n熟悉 Python",
        )
        rules = {item["rule"] for item in findings}
        self.assertIn(1, rules)
        self.assertIn(4, rules)

    def test_precheck_clean_report_has_no_findings(self):
        findings = screening_agent._precheck_findings(self._report(), {"requirements": []}, "张三\n本科，复旦大学")
        self.assertEqual(findings, [])

    def test_suspicious_report_triggers_deep_validation_with_revision(self):
        report = self._suspicious_report()
        reflection = {
            "issues": [{"rule": 1, "quote": "精通 Spark 流式计算", "problem": "简历未提及 Spark", "suggested_fix": "删除该论断"}],
            "should_revise": True,
        }
        revised = dict(report)
        revised["strengths"] = ["熟悉 Python"]
        revised["basic_screening"] = {"education_level": "本科", "salary_expectation": "未提供"}
        requirements = {"requirements": [{"id": "r1", "text": "本科及以上学历", "category": "education", "hard": True}]}
        with patch.object(screening_agent, "_extract_requirements", return_value=requirements), patch.object(screening_agent.llm, "call_llm", side_effect=[report, reflection, revised]) as call:
            result = screening_agent.run_screening_agent(job_content="本科及以上学历", resume_content="张三\n本科，复旦大学\n熟悉 Python", current_date="2026-08")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(result["agent_validation"]["mode"], "深度")
        self.assertTrue(result["agent_validation"]["revised"])
        self.assertEqual(len(result["agent_validation"]["issues"]), 1)
        self.assertEqual(result["strengths"], ["熟悉 Python"])
        self.assertEqual(result["agent_meta"]["llm_calls"], 3)

    def test_revision_failing_validation_keeps_original_report(self):
        report = self._suspicious_report()
        reflection = {
            "issues": [{"rule": 1, "quote": "精通 Spark 流式计算", "problem": "简历未提及 Spark", "suggested_fix": "删除该论断"}],
            "should_revise": True,
        }
        broken = dict(report)
        broken["strengths"] = ["熟悉 Python"]
        broken["job_fit_score"] = 90
        requirements = {"requirements": [{"id": "r1", "text": "本科及以上学历", "category": "education", "hard": True}]}
        with patch.object(screening_agent, "_extract_requirements", return_value=requirements), patch.object(screening_agent.llm, "call_llm", side_effect=[report, reflection, broken]) as call:
            result = screening_agent.run_screening_agent(job_content="本科及以上学历", resume_content="张三\n本科，复旦大学\n熟悉 Python", current_date="2026-08")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(result["agent_validation"]["mode"], "深度")
        self.assertFalse(result["agent_validation"]["revised"])
        self.assertEqual(len(result["agent_validation"]["issues"]), 1)
        self.assertEqual(result["job_fit_score"], 78)

    def test_requirement_string_false_is_not_hard(self):
        with patch.object(screening_agent.llm, "call_llm", return_value={"requirements": [{"text": "优先经验", "hard": "false"}]}):
            result = screening_agent._extract_requirements("优先经验", None, {"calls": 0})
        self.assertFalse(result["requirements"][0]["hard"])

    def test_resume_experiences_are_found_by_requirement_terms(self):
        matches = screening_agent._find_resume_experiences(
            "张三\n负责 Java 服务开发和上线\n负责招聘培训",
            [{"id": "r1", "text": "Java 开发经验", "category": "skill", "hard": True}],
        )
        self.assertEqual(matches[0]["resume_experiences"][0]["line"], 2)
        self.assertEqual(matches[0]["status"], "met")

    def test_java_does_not_match_javascript_or_negated_experience(self):
        matches = screening_agent._find_resume_experiences(
            "熟悉 JavaScript\n无 Java 开发经验",
            [{"id": "r1", "text": "Java 开发经验", "category": "skill", "hard": True}],
        )
        self.assertEqual(matches[0]["status"], "unknown")

    def test_requirements_keep_unique_text_and_repair_duplicate_ids(self):
        response = {"requirements": [
            {"id": "req-1", "text": "本科及以上", "category": "education", "hard": True},
            {"id": "req-1", "text": "本科及以上", "category": "education", "hard": True},
            {"id": "req-1", "text": "5年以上经验", "category": "experience", "hard": True},
        ]}
        with patch.object(screening_agent.llm, "call_llm", return_value=response):
            result = screening_agent._extract_requirements("岗位要求", None, {"calls": 0})
        self.assertEqual(len(result["requirements"]), 2)
        self.assertEqual(len({item["id"] for item in result["requirements"]}), 2)

    def test_compound_requirements_are_split_without_cutting_conditions(self):
        response = {"requirements": [{"id": "req-1", "text": "本科及以上学历，5年以上产品经验，熟悉 Python 和 SQL", "category": "experience", "hard": True}]}
        with patch.object(screening_agent.llm, "call_llm", return_value=response):
            result = screening_agent._extract_requirements("岗位要求", None, {"calls": 0})
        texts = [item["text"] for item in result["requirements"]]
        self.assertGreaterEqual(len(texts), 2)
        self.assertTrue(any("本科及以上学历" in text for text in texts))
        self.assertTrue(any("5年以上产品经验" in text for text in texts))
        self.assertTrue(all(len(text) <= screening_agent._MAX_REQUIREMENT_TEXT for text in texts))

    def test_long_compound_requirement_marks_truncation_instead_of_silent_loss(self):
        long_text = "负责" + "非常长的岗位职责描述，" * 80 + "完成商业化验证"
        response = {"requirements": [{"id": "req-1", "text": long_text, "category": "responsibility", "hard": True}]}
        with patch.object(screening_agent.llm, "call_llm", return_value=response):
            result = screening_agent._extract_requirements("岗位要求", None, {"calls": 0})
        self.assertTrue(any(item["truncated"] for item in result["requirements"]))
        self.assertTrue(all(item["original_length"] >= len(item["text"]) for item in result["requirements"]))

    def test_invalid_score_breakdown_is_rejected(self):
        report = self._report()
        report["score_breakdown"]["hard_requirements"] = -1
        validation = screening_agent._validate_report(
            report,
            {"requirements": []},
            "候选人经历",
        )
        self.assertFalse(validation["passed"])
        self.assertTrue(any("分项" in issue for issue in validation["issues"]))

    def test_empty_input_returns_safe_report_without_llm_call(self):
        with patch.object(screening_agent.llm, "call_llm") as call:
            result = screening_agent.run_screening_agent(job_content="", resume_content="简历", current_date="2026-08")
        self.assertEqual(call.call_count, 0)
        self.assertFalse(result["agent_meta"]["validation_passed"])


if __name__ == "__main__":
    unittest.main()
