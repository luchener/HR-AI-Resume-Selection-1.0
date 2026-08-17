import io
import os
import tempfile
import unittest
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import app as backend
import llm as llm_module
from test_helpers import build_anonymous_resume_docx


class HrAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        backend._HR_ANALYSIS_CACHE.clear()
        backend.store.RESUMES_DIR = os.path.join(self.temp_dir.name, "resumes")
        backend.store.JOBS_DIR = os.path.join(self.temp_dir.name, "jobs")
        self.client = backend.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resume_and_job_upload_do_not_call_llm(self):
        file_bytes = build_anonymous_resume_docx()

        with patch.object(backend.llm, "call_llm", side_effect=AssertionError("LLM called")):
            upload = self.client.post(
                "/api/v1/resumes/upload",
                data={"file": (io.BytesIO(file_bytes), "anonymous-resume.docx")},
                content_type="multipart/form-data",
            )
            self.assertEqual(upload.status_code, 200)
            resume_id = upload.get_json()["resume_id"]

            job = self.client.post(
                "/api/v1/jobs/upload",
                json={
                    "resume_id": resume_id,
                    "job_descriptions": ["AI 产品经理\n负责 AI Agent 产品\n要求 5 年产品经验"],
                },
            )
            self.assertEqual(job.status_code, 200)

    def test_resume_upload_accepts_octet_stream_docx(self):
        response = self.client.post(
            "/api/v1/resumes/upload",
            data={
                "file": (
                    io.BytesIO(build_anonymous_resume_docx()),
                    "anonymous-resume.docx",
                    "application/octet-stream",
                )
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.get_json()["extracted_characters"], 500)

    def test_hr_analysis_enforces_formula_and_fixed_fields(self):
        resume_id = backend.store.save_resume("Python AI 产品经理，8 年经验", {})
        job_id = backend.store.save_job(resume_id, "招聘 AI 产品经理，要求 5 年经验", {})
        model_result = {
            "candidate_name": "张三",
            "job_fit_score": 88,
            "ai_risk": "light",
            "ai_deduction": 7,
            "summary": "核心经历匹配，表达略显模板化。",
            "basic_screening": {
                "highest_education": "硕士",
                "school_name": "复旦大学",
                "school_tier": "985/211",
                "major_match": "相关",
            },
            "work_history": {"total_years": "8 年", "relevant_years": "6 年", "stability": "稳定"},
            "skill_match": {"hard_skills": ["AI 产品", "Python 熟练"], "project_match_points": ["AI Agent 项目对应岗位任务"], "soft_skills": ["跨部门协作"]},
            "bonus_items": ["量化提升 30%"],
            "strengths": ["8 年 AI 产品经验，超过岗位要求的 5 年门槛。"],
            "weaknesses": ["未提供商业化成果证据。"],
            "risk_points": ["无明显风险"],
            "recruitment_recommendation": "优先面试",
            "fit_tag": "高匹配",
            "deduction_reasons": ["部分表述较为同质化"],
        }

        with patch.object(backend.llm, "call_llm", return_value=model_result) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()["data"]["hr_analysis"]
        self.assertEqual(result["final_score"], 81)
        self.assertEqual(result["fit_grade"], "A级（良好适配）")
        self.assertEqual(result["job_fit_percentage"], 88)
        self.assertEqual(result["recruitment_recommendation"], "优先面试")
        self.assertEqual(result["strengths"], model_result["strengths"])
        self.assertEqual(result["fit_tag"], "高匹配")
        self.assertEqual(result["basic_screening"]["highest_education"], "硕士")
        self.assertEqual(result["basic_screening"]["school_name"], "复旦大学")
        self.assertEqual(result["basic_screening"]["school_tier"], "985/211")
        self.assertEqual(response.get_json()["data"]["candidate_name"], "张三")
        self.assertIn("# Python AI 产品经理", response.get_json()["data"]["studio_markdown"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["max_tokens"], 4000)
        self.assertIn("必须全部来自这同一段教育经历", call.call_args.args[0])

    def test_normalizer_uses_ai_score_breakdown_sum(self):
        raw = {
            "candidate_name": "张三",
            "job_fit_score": 0,
            "score_breakdown": {
                "hard_requirements": 18,
                "responsibility_overlap": 20,
                "skills_projects": 17,
                "industry_background": 8,
                "evidence_bonus": 6,
            },
            "ai_risk": "none",
            "ai_deduction": 0,
        }

        result = backend._normalize_hr_analysis(raw)

        self.assertEqual(result["job_fit_score"], 69)
        self.assertEqual(result["final_score"], 69)
        self.assertEqual(result["score_breakdown"]["skills_projects"], 17)

    def test_normalizer_caps_recommendation_and_tag_to_final_score(self):
        raw = {
            "candidate_name": "张三",
            "job_fit_score": 47,
            "ai_risk": "none",
            "ai_deduction": 0,
            "recruitment_recommendation": "优先面试",
            "fit_tag": "高匹配",
        }

        result = backend._normalize_hr_analysis(raw)

        self.assertEqual(result["recruitment_recommendation"], "淘汰")
        self.assertEqual(result["fit_tag"], "不匹配")

    def test_normalizer_removes_salary_risk_without_job_budget(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "risk_points": [
                    "期望薪资可能高于岗位预算",
                    "缺少 RFID 项目经验，影响现场联调",
                ],
            },
            "要求三年以上现场实施经验，可接受出差",
        )

        self.assertEqual(result["risk_points"], ["缺少 RFID 项目经验，影响现场联调"])

    def test_compact_analysis_text_removes_pdf_watermark_noise(self):
        source = "\n".join(
            [
                "G q n P",
                "N -0t6 0 G",
                "912cf79e4c4712351HN0t60GFBXwY29WPqaWOGqnPHQMRRj",
                "Linux",
                "负责现场通讯设备的安装、调试、配置与上线验收",
            ]
        )

        compacted = backend._compact_analysis_text(source, 8000)

        self.assertNotIn("G q n P", compacted)
        self.assertNotIn("912cf79", compacted)
        self.assertIn("Linux", compacted)
        self.assertIn("负责现场通讯设备", compacted)

    def test_normalizer_replaces_legacy_skill_labels(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "skill_match": {
                    "hard_skills": [
                        "[直接匹配] Python：5年经验",
                        "[可迁移] Java：后端经验",
                        "[缺失] Kubernetes：简历无证据",
                    ]
                },
            }
        )

        self.assertEqual(
            result["skill_match"]["hard_skills"],
            [
                "[符合要求] Python：5年经验",
                "[相关经验] Java：后端经验",
                "[简历未体现] Kubernetes：简历无证据",
            ],
        )

    def test_llm_accepts_reasoning_content_when_content_is_empty(self):
        class FakeCompletions:
            def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                reasoning_content='完成分析，最终结果：{"ok": true}',
                            )
                        )
                    ]
                )

        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
        finally:
            llm_module._client = previous

    def test_llm_enables_deepseek_thinking_for_quality_mode(self):
        class FakeCompletions:
            def __init__(self):
                self.request = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
                )

        completions = FakeCompletions()
        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        try:
            with patch.object(llm_module, "LLM_BASE_URL", "https://api.deepseek.com"), patch.object(
                llm_module, "LL_MODEL", "deepseek-v4-flash"
            ):
                result = llm_module.call_llm(
                    "输出 JSON", expect_json=True, max_tokens=100, thinking=True
                )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(completions.request["extra_body"], {"thinking": {"type": "enabled"}})
        finally:
            llm_module._client = previous

    def test_llm_parses_reasoning_json_when_content_has_prose(self):
        class FakeCompletions:
            def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="我已经完成分析。",
                                reasoning_content='最终结果：{"ok": true}',
                            )
                        )
                    ]
                )

        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
        finally:
            llm_module._client = previous

    def test_llm_retries_when_first_json_is_truncated(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                content = '{"ok":' if self.calls == 1 else '{"ok": true}'
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        completions = FakeCompletions()
        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
            self.assertEqual(completions.calls, 2)
        finally:
            llm_module._client = previous

    def test_llm_uses_ai_repair_after_two_invalid_responses(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                content = '{"ok":' if len(self.calls) < 3 else '{"ok": true}'
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                )

        completions = FakeCompletions()
        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        try:
            self.assertEqual(
                llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50),
                {"ok": True},
            )
            self.assertEqual(len(completions.calls), 3)
        finally:
            llm_module._client = previous

    def test_lenient_json_accepts_trailing_commas(self):
        self.assertEqual(
            llm_module.parse_json_lenient('{"items": ["a",],}'),
            {"items": ["a"]},
        )

    def test_candidate_name_falls_back_to_resume_heading(self):
        self.assertEqual(
            backend._candidate_name_from_resume(
                {"content": "李四\n联系方式\n13800000000", "processed": {}},
                {"candidate_name": "未提供"},
            ),
            "李四",
        )

    def test_hr_analysis_returns_error_when_model_returns_invalid_json(self):
        resume_id = backend.store.save_resume("Python 后端工程师，5 年经验，FastAPI 开发", {})
        job_id = backend.store.save_job(resume_id, "招聘 Python 后端工程师，要求 3 年经验，FastAPI", {})

        with patch.object(backend.llm, "call_llm", side_effect=ValueError("invalid JSON")):
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("AI 分析结果无法解析", response.get_json()["detail"])

    def test_batch_hr_analysis_keeps_successful_results(self):
        resume_ids = [
            backend.store.save_resume("Python 后端工程师，5 年经验", {}),
            backend.store.save_resume("Java 后端工程师，3 年经验", {}),
        ]
        job_id = backend.store.save_job(
            resume_ids[0],
            "招聘后端工程师，要求 Python、API 和数据库经验",
            {},
        )
        model_result = {
            "job_fit_score": 80,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "岗位经验基本匹配。",
            "strengths": ["具备后端开发经验"],
            "weaknesses": ["项目数据未提供"],
            "risk_points": ["无明显风险"],
        }

        def analyze_candidate(prompt, **_kwargs):
            if "Java 后端工程师，3 年经验" in prompt:
                raise ValueError("invalid candidate output")
            return model_result

        with patch.object(backend.llm, "call_llm", side_effect=analyze_candidate) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_ids": resume_ids, "job_id": job_id},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(len(data["batch_analyses"]), 1)
        self.assertEqual(len(data["batch_failures"]), 1)
        self.assertEqual(data["batch_analyses"][0]["resume_id"], resume_ids[0])
        self.assertEqual(data["batch_failures"][0]["resume_id"], resume_ids[1])
        self.assertEqual(call.call_count, 2)
        for item in call.call_args_list:
            self.assertEqual(item.kwargs["max_tokens"], 4000)
            self.assertEqual(item.args[0].count("招聘后端工程师，要求 Python、API 和数据库经验"), 1)

    def test_batch_hr_analysis_maps_all_candidates_from_one_model_call(self):
        resume_ids = [
            backend.store.save_resume("候选人甲，Python 5 年", {}),
            backend.store.save_resume("候选人乙，Java 4 年", {}),
            backend.store.save_resume("候选人丙，Go 3 年", {}),
        ]
        job_id = backend.store.save_job(resume_ids[0], "招聘后端工程师", {})

        def analysis(name: str, score: int) -> dict:
            return {
                "candidate_name": name,
                "job_fit_score": score,
                "ai_risk": "none",
                "ai_deduction": 0,
                "summary": f"{name}的岗位证据已完成分析。",
                "strengths": ["具备相关开发经验"],
                "weaknesses": ["量化成果未提供"],
                "risk_points": ["无明显风险"],
            }

        barrier = Barrier(3)

        def analyze_candidate(prompt, **_kwargs):
            barrier.wait(timeout=2)
            if "候选人甲" in prompt:
                return analysis("候选人甲", 85)
            if "候选人乙" in prompt:
                return analysis("候选人乙", 72)
            return analysis("候选人丙", 65)

        with patch.object(backend.llm, "call_llm", side_effect=analyze_candidate) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_ids": resume_ids, "job_id": job_id},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(call.call_count, 3)
        self.assertEqual(len(data["batch_analyses"]), 3)
        self.assertEqual(data["batch_failures"], [])
        self.assertEqual(
            [item["candidate_name"] for item in data["batch_analyses"]],
            ["候选人甲", "候选人乙", "候选人丙"],
        )
        self.assertEqual(
            [item["hr_analysis"]["final_score"] for item in data["batch_analyses"]],
            [85, 72, 65],
        )

    def test_ai_connection_uses_request_scoped_config(self):
        ai_config = {
            "provider": "deepseek",
            "api_key": "sk-browser-only",
            "base_url": "https://api.deepseek.com/",
            "model": "deepseek-chat",
        }
        with patch.object(backend.llm, "call_llm", return_value="OK") as call:
            response = self.client.post(
                "/api/v1/ai/test",
                json={"ai_config": ai_config},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["data"]["ok"])
        runtime_config = call.call_args.kwargs["runtime_config"]
        self.assertEqual(runtime_config["api_key"], "sk-browser-only")
        self.assertEqual(runtime_config["base_url"], "https://api.deepseek.com")
        self.assertEqual(call.call_args.kwargs["max_tokens"], 8)

    def test_ai_connection_rejects_invalid_compatible_url(self):
        response = self.client.post(
            "/api/v1/ai/test",
            json={
                "ai_config": {
                    "provider": "custom",
                    "api_key": "sk-test",
                    "base_url": "file:///tmp/model",
                    "model": "custom-chat",
                }
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("HTTP", response.get_json()["detail"])

    def test_hr_cache_is_isolated_by_request_model_config(self):
        resume_id = backend.store.save_resume("Python 工程师，5 年经验", {})
        job_id = backend.store.save_job(resume_id, "招聘 Python 工程师", {})
        model_result = {
            "job_fit_score": 76,
            "ai_risk": "none",
            "summary": "岗位经验匹配。",
        }
        first_config = {
            "provider": "deepseek",
            "api_key": "sk-first",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
        }
        second_config = {
            **first_config,
            "api_key": "sk-second",
        }

        with patch.object(backend.llm, "call_llm", return_value=model_result) as call:
            first = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": first_config},
            )
            cached = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": first_config},
            )
            isolated = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": second_config},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(isolated.status_code, 200)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(
            [item.kwargs["runtime_config"]["api_key"] for item in call.call_args_list],
            ["sk-first", "sk-second"],
        )


if __name__ == "__main__":
    unittest.main()
