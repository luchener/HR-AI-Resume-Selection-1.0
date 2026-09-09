import io
import os
import shutil
import zipfile
import unittest

import app as backend
import auth as auth_module

# 验证码默认开启；冒烟套件登录调用统一关闭（验证码专项用例见 test_captcha_*）
backend.config.CAPTCHA_ENABLED = False

from xml.sax.saxutils import escape


RESUME_TEXT = "张三\nPython AI 产品经理，10 年经验\n负责公司内部 AI 产品选型与上线\n熟悉 LLM 调用、RAG 与前端技术栈\n参与过 5 个以上 AI 产品落地"
JOB_TEXT = "招聘 AI 产品经理，要求熟悉 LLM 产品、有 AI 产品落地经验，能协调前后端工程团队"


def _make_docx(text: str) -> bytes:
    paragraphs = "".join(
        "<w:p><w:r><w:t>" + escape(line) + "</w:t></w:r></w:p>" for line in text.splitlines()
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
    return out.getvalue()


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-data-smoke")
        os.makedirs(self.temp_dir, exist_ok=True)
        backend.store.RESUMES_DIR = os.path.join(self.temp_dir, "resumes")
        backend.store.JOBS_DIR = os.path.join(self.temp_dir, "jobs")
        auth_module.USERS_DIR = os.path.join(self.temp_dir, "users")
        auth_module._INDEX_FILE = os.path.join(auth_module.USERS_DIR, "_index.json")
        auth_module._INDEX_LOCK = os.path.join(auth_module.USERS_DIR, "_index.lock")
        auth_module.RESETS_DIR = os.path.join(self.temp_dir, "resets")
        auth_module.LOGIN_FAILURES_DIR = os.path.join(self.temp_dir, "login_failures")
        auth_module.RATE_LIMITS_DIR = os.path.join(self.temp_dir, "rate_limits")
        auth_module.INVITE_REQUESTS_DIR = os.path.join(self.temp_dir, "invite_requests")
        auth_module.INVITE_CODES_DIR = os.path.join(self.temp_dir, "invite_codes")
        auth_module.INVITE_CODES_AUDIT_DIR = os.path.join(self.temp_dir, "invite_codes_audit")
        for _d in (
            auth_module.RESETS_DIR,
            auth_module.LOGIN_FAILURES_DIR,
            auth_module.RATE_LIMITS_DIR,
            auth_module.INVITE_REQUESTS_DIR,
            auth_module.INVITE_CODES_DIR,
            auth_module.INVITE_CODES_AUDIT_DIR,
        ):
            os.makedirs(_d, exist_ok=True)
        user, _err = auth_module.create_user("smoke-user", "smoke-pw123")
        self.user_id = user["user_id"]
        self.token = auth_module.generate_jwt(user["user_id"], user["username"])
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = backend.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _upload_resume(self, text):
        resp = self.client.post(
            "/api/v1/resumes/upload",
            data={"file": (io.BytesIO(_make_docx(text)), "resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.get_data())
        return resp.get_json()["resume_id"]

    def _upload_job(self, text, resume_id):
        resp = self.client.post(
            "/api/v1/jobs/upload",
            json={"job_descriptions": [text], "resume_id": resume_id},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.get_data())
        return resp.get_json()["job_id"][0]

    def test_smoke_full_flow(self):
        """End-to-end: auth -> upload resume -> upload job -> hr-analysis (stubbed) -> review-markers."""
        # 1. login
        login = self.client.post("/api/v1/auth/login", json={"username": "smoke-user", "password": "smoke-pw123"})
        self.assertEqual(login.status_code, 200, login.get_data())
        login_json = login.get_json()
        self.assertIn("token", login_json["data"])

        # 2. upload resume
        resume_id = self._upload_resume(RESUME_TEXT)
        # verify readable
        v = self.client.get(f"/api/v1/resumes?resume_id={resume_id}", headers=self.headers)
        self.assertEqual(v.status_code, 200)
        self.assertIn("content", v.get_json()["data"]["raw_resume"])

        # 3. upload job
        job_id = self._upload_job(JOB_TEXT, resume_id)

        # 4. hr-analysis 走 Agent；为冒烟测试 mock 掉 run_screening_agent，避免真 LLM
        from unittest.mock import patch
        stub = {
            "candidate_name": "张三",
            "job_fit_score": 82,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "岗位经验匹配。",
            "strengths": ["AI 产品经验"],
            "weaknesses": ["无"],
            "risk_points": ["无"],
            "recruitment_recommendation": "优先面试",
            "fit_tag": "高匹配",
            "basic_screening": {"native_place": "上海", "age": "30", "gender": "男", "work_location": "上海", "salary_expectation": "30K+"},
            "education_history": [{"degree": "本科", "school_name": "XX 大学", "school_tier": "普通", "major": "计算机", "graduation_year": "2018"}],
        }
        with patch.object(backend.screening_agent, "run_screening_agent", return_value=stub):
            analysis = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )
        self.assertEqual(analysis.status_code, 200, analysis.get_data())
        data = analysis.get_json()["data"]
        self.assertIn("hr_analysis", data)
        self.assertEqual(data["hr_analysis"]["final_score"], 82)
        self.assertEqual(data["candidate_name"], "张三")

        # 5. review-markers
        markers = self.client.post(
            "/api/v1/resumes/review-markers",
            json={
                "resume_id": resume_id,
                "candidate_name": "张三",
                "analysis": data["hr_analysis"],
            },
            headers=self.headers,
        )
        self.assertEqual(markers.status_code, 200, markers.get_data())
        md = markers.get_json()["data"]
        self.assertEqual(md["candidate_name"], "张三")
        self.assertIsInstance(md["annotations"], list)
        for annotation in md["annotations"]:
            self.assertIn(annotation["category"], {"strength", "match", "risk", "missing", "verify"})
            self.assertIn(annotation["quote"], RESUME_TEXT)


if __name__ == "__main__":
    unittest.main()
