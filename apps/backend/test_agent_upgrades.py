"""Step 2/4 新增能力测试：SSE 流式端点 + 可选的 Web Search 工具。"""
import json
import os
import unittest
from unittest.mock import patch

import app as backend
import auth as auth_module
import screening_agent
from tools import web_search


class WebSearchToolTests(unittest.TestCase):
    """tools/web_search 模块的确定性/降级行为。"""

    def test_summarize_results_empty(self):
        self.assertEqual(web_search.summarize_results(None), "")
        self.assertEqual(web_search.summarize_results([]), "")

    def test_summarize_results_formats_entries(self):
        text = web_search.summarize_results(
            [{"title": "某公司", "snippet": "一家科技企业", "url": "https://example.com"}]
        )
        self.assertIn("某公司", text)
        self.assertIn("一家科技企业", text)
        self.assertIn("example.com", text)

    def test_search_web_failure_returns_none(self):
        """网络失败必须静默降级返回 None，绝不抛异常。"""
        with patch.object(web_search.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(web_search.search_web("某公司 简介"))

    def test_search_web_empty_query_returns_none(self):
        self.assertIsNone(web_search.search_web(""))
        self.assertIsNone(web_search.search_web("   "))

    def test_search_web_parallel_returns_same_length(self):
        with patch.object(web_search, "search_web", side_effect=lambda q, **kw: [{"title": q}]):
            results = web_search.search_web_parallel(["a", "b"])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0][0]["title"], "a")


class CompanyExtractionTests(unittest.TestCase):
    """screening_agent 里确定性公司名抽取（零 LLM 成本）。"""

    def test_extracts_company_from_resume(self):
        resume = "张三\n工作经历\n2020-2023 深圳市某科技公司 后端工程师\n负责系统开发"
        queries = screening_agent._extract_company_queries(resume)
        self.assertIn("深圳市某科技公司 公司 简介", queries)

    def test_no_company_returns_empty(self):
        self.assertEqual(screening_agent._extract_company_queries("张三\n无公司信息"), [])

    def test_dedupes_repeated_companies(self):
        resume = "某科技公司 A\n某科技公司 A\n某集团 B"
        queries = screening_agent._extract_company_queries(resume, limit=5)
        self.assertEqual(len(queries), len(set(queries)))


class HrAnalysisStreamTests(unittest.TestCase):
    """hr-analysis SSE 流式端点。"""

    def setUp(self):
        self.temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-data-upgrades")
        os.makedirs(self.temp_dir, exist_ok=True)
        backend._HR_ANALYSIS_CACHE.clear()
        backend.store.RESUMES_DIR = os.path.join(self.temp_dir, "resumes")
        backend.store.JOBS_DIR = os.path.join(self.temp_dir, "jobs")
        auth_module.USERS_DIR = os.path.join(self.temp_dir, "users")
        auth_module._INDEX_FILE = os.path.join(auth_module.USERS_DIR, "_index.json")
        auth_module._INDEX_LOCK = os.path.join(auth_module.USERS_DIR, "_index.lock")
        auth_module.RESETS_DIR = os.path.join(self.temp_dir, "password_resets")
        os.makedirs(auth_module.RESETS_DIR, exist_ok=True)
        user, _err = auth_module.create_user("tester", "password123")
        self.user_id = user["user_id"]
        self.token = auth_module.generate_jwt(user["user_id"], user["username"])
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = backend.app.test_client()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _model_result(self):
        return {
            "job_fit_score": 80,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "岗位经验基本匹配。",
            "strengths": ["具备相关经验"],
            "weaknesses": ["量化成果未提供"],
            "risk_points": ["无明显风险"],
        }

    def test_single_stream_emits_progress_then_completed(self):
        resume_id = backend.store.save_resume("张三\n某科技公司\nPython 5年", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘后端工程师，要求 Python", {}, self.user_id)

        events: list[dict] = []
        with patch.object(backend.screening_agent, "run_screening_agent", return_value=self._model_result()) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis?stream=true",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )
            # SSE 生成器是惰性的，必须在 patch 生效期间消费响应体
            body = response.get_data(as_text=True)
        for line in body.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        statuses = [event["status"] for event in events]
        self.assertEqual(statuses[0], "starting")
        self.assertEqual(statuses[-1], "completed")
        completed = events[-1]
        self.assertEqual(completed["result"]["data"]["hr_analysis"]["job_fit_score"], 80)
        self.assertEqual(call.call_count, 1)

    def test_stream_passes_agent_config_web_search(self):
        resume_id = backend.store.save_resume("张三\n某科技公司\nPython 5年", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘后端工程师，要求 Python", {}, self.user_id)

        with patch.object(backend.screening_agent, "run_screening_agent", return_value=self._model_result()) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis?stream=true",
                json={"resume_id": resume_id, "job_id": job_id, "agent_config": {"web_search": True}},
                headers=self.headers,
            )
            response.get_data(as_text=True)  # 消费生成器

        self.assertEqual(call.call_args.kwargs["agent_config"], {"web_search": True})

    def test_batch_stream_emits_candidate_progress(self):
        resume_ids = [
            backend.store.save_resume("候选人甲\n某科技公司\nPython 5年", {}, self.user_id),
            backend.store.save_resume("候选人乙\n某集团\nJava 3年", {}, self.user_id),
        ]
        job_id = backend.store.save_job(resume_ids[0], "招聘后端工程师", {}, self.user_id)

        with patch.object(backend.screening_agent, "run_screening_agent", return_value=self._model_result()), patch.object(
            backend.screening_agent, "_extract_requirements",
            return_value={"requirements": [{"id": "req-1", "text": "后端开发经验", "category": "experience", "hard": True, "logic": "required"}], "extraction_failed": False},
        ):
            response = self.client.post(
                "/api/v1/resumes/hr-analysis?stream=true",
                json={"resume_ids": resume_ids, "job_id": job_id},
                headers=self.headers,
            )
            body = response.get_data(as_text=True)

        events = []
        for line in body.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        candidate_events = [e for e in events if e["status"] == "candidate"]
        self.assertEqual(len(candidate_events), 2)
        self.assertEqual(candidate_events[0]["index"], 1)
        self.assertEqual(candidate_events[-1]["total"], 2)
        self.assertEqual(events[-1]["status"], "completed")

    def test_stream_error_emits_error_event(self):
        resume_id = backend.store.save_resume("张三", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘后端工程师", {}, self.user_id)
        with patch.object(backend.screening_agent, "run_screening_agent", side_effect=ValueError("boom")):
            response = self.client.post(
                "/api/v1/resumes/hr-analysis?stream=true",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )
            body = response.get_data(as_text=True)
        events = []
        for line in body.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        self.assertEqual(events[-1]["status"], "error")


if __name__ == "__main__":
    unittest.main()
