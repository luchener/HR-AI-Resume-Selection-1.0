"""归档 API 冒烟测试（不调 LLM，纯本地文件存储链路）。

复用 test_smoke.py 的临时目录方案：setUp 里把 store 的目录全部
重定向到 .test-tmp 下的临时目录，测试完 tearDown 清空。
"""
import io
import os
import shutil
import sys
import unittest
import uuid
import zipfile

from xml.sax.saxutils import escape

TEST_TMP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".test-tmp",
    f"archives_api_{uuid.uuid4().hex[:8]}",
)
os.environ["DATA_DIR"] = TEST_TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as backend  # noqa: E402
import auth as auth_module  # noqa: E402


def _make_docx(text: str) -> bytes:
    paragraphs = "".join(
        "<w:p><w:r><w:t>" + escape(line) + "</w:t></w:r></w:p>" for line in text.splitlines()
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml.encode("utf-8"))
    return out.getvalue()


RESUME_A = "苏明远\nAI 产品经理，8 年经验\n熟悉 LLM 应用与 RAG"
RESUME_B = "李四\nPython 后端工程师，5 年经验\n熟悉 FastAPI 与 Docker"


class ArchiveApiSmokeTest(unittest.TestCase):
    def setUp(self):
        os.makedirs(TEST_TMP, exist_ok=True)
        backend.store.RESUMES_DIR = os.path.join(TEST_TMP, "resumes")
        backend.store.JOBS_DIR = os.path.join(TEST_TMP, "jobs")
        backend.store.ARCHIVES_DIR = os.path.join(TEST_TMP, "archives")
        backend.store.ARCHIVE_INDEX_PATH = os.path.join(backend.store.ARCHIVES_DIR, "_index.json")
        # app.py 的路由直接读 config.ARCHIVES_DIR，必须一并覆盖
        backend.config.ARCHIVES_DIR = backend.store.ARCHIVES_DIR
        auth_module.USERS_DIR = os.path.join(TEST_TMP, "users")
        auth_module._INDEX_FILE = os.path.join(auth_module.USERS_DIR, "_index.json")
        auth_module._INDEX_LOCK = os.path.join(auth_module.USERS_DIR, "_index.lock")
        auth_module.RESETS_DIR = os.path.join(TEST_TMP, "resets")
        os.makedirs(auth_module.RESETS_DIR, exist_ok=True)

        user, _err = auth_module.create_user("archiver", "archiver-pw123")
        self.user_id = user["user_id"]
        self.token = auth_module.generate_jwt(user["user_id"], user["username"])
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = backend.app.test_client()

    def tearDown(self):
        shutil.rmtree(TEST_TMP, ignore_errors=True)

    def _upload_resume(self, text: str) -> str:
        resp = self.client.post(
            "/api/v1/resumes/upload",
            data={"file": (io.BytesIO(_make_docx(text)), "resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.get_data())
        return resp.get_json()["resume_id"]

    def _upload_job(self, text: str, resume_id: str) -> str:
        resp = self.client.post(
            "/api/v1/jobs/upload",
            json={"job_descriptions": [text], "resume_id": resume_id},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.get_data())
        return resp.get_json()["job_id"][0]

    def _create_archive(self, resume_id: str, job_id: str, analysis: dict | None = None, tags: list | None = None, category: str | None = None):
        body = {"resume_id": resume_id, "job_id": job_id}
        if analysis is not None:
            body["analysis"] = analysis
        if tags is not None:
            body["custom_tags"] = tags
        if category is not None:
            body["category"] = category
        return self.client.post("/api/v1/archives", json=body, headers=self.headers)

    def test_archive_full_cycle(self):
        """归档 -> 列表/姓名查询 -> 改标签 -> 软删 -> 恢复 -> 软删 -> 彻底删除。"""
        resume_id = self._upload_resume(RESUME_A)
        job_id = self._upload_job("招聘 AI 产品经理，要求熟悉 LLM 应用", resume_id)
        analysis = {
            "candidate_name": "苏明远",
            "final_score": 86,
            "fit_tag": "高匹配",
            "recruitment_recommendation": "优先面试",
            "summary": "岗位匹配度高。",
        }

        # 1. 创建归档
        resp = self._create_archive(resume_id, job_id, analysis=analysis, tags=["AI", "社招"], category="IT")
        self.assertEqual(resp.status_code, 201, resp.get_data())
        data = resp.get_json()["data"]
        self.assertEqual(data["candidate_name"], "苏明远")
        self.assertEqual(data["final_score"], 86)
        self.assertEqual(data["custom_tags"], ["AI", "社招"])
        self.assertEqual(data["category"], "IT")
        self.assertIn("job_title", data)
        aid = data["archive_id"]

        # 2. 幂等：重复归档返回已有记录
        resp2 = self._create_archive(resume_id, job_id, analysis=analysis)
        self.assertEqual(resp2.status_code, 200, resp2.get_data())
        self.assertEqual(resp2.get_json()["data"]["archive_id"], aid)

        # 3. 列表 + 姓名模糊查询
        lst = self.client.get("/api/v1/archives", headers=self.headers)
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(len(lst.get_json()["data"]["archives"]), 1)
        self.assertIn("categories", lst.get_json()["data"]["meta"])
        self.assertIn("IT", lst.get_json()["data"]["meta"]["categories"])
        by_name = self.client.get("/api/v1/archives?name=苏", headers=self.headers)
        self.assertEqual(len(by_name.get_json()["data"]["archives"]), 1)
        no_match = self.client.get("/api/v1/archives?name=不存在", headers=self.headers)
        self.assertEqual(len(no_match.get_json()["data"]["archives"]), 0)
        by_cat = self.client.get("/api/v1/archives?category=IT", headers=self.headers)
        self.assertEqual(len(by_cat.get_json()["data"]["archives"]), 1)

        # 4. 详情（含完整 analysis）
        detail = self.client.get(f"/api/v1/archives/{aid}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["data"]["candidate_name"], "苏明远")
        self.assertIn("analysis", detail.get_json()["data"])

        # 4.5 改分类
        patch_cat = self.client.patch(
            f"/api/v1/archives/{aid}/category",
            json={"category": "技术"},
            headers=self.headers,
        )
        self.assertEqual(patch_cat.status_code, 200, patch_cat.get_data())
        self.assertEqual(patch_cat.get_json()["data"]["category"], "技术")

        # 5. 改标签
        patch = self.client.patch(
            f"/api/v1/archives/{aid}/tags",
            json={"custom_tags": ["AI", "急聘"]},
            headers=self.headers,
        )
        self.assertEqual(patch.status_code, 200, patch.get_data())
        self.assertEqual(patch.get_json()["data"]["custom_tags"], ["AI", "急聘"])

        # 6. 软删除 -> 回收站可见 -> active 列表消失
        soft = self.client.delete(f"/api/v1/archives/{aid}", headers=self.headers)
        self.assertEqual(soft.status_code, 200, soft.get_data())
        trash = self.client.get("/api/v1/archives/trash", headers=self.headers)
        self.assertEqual(len(trash.get_json()["data"]["archives"]), 1)
        active = self.client.get("/api/v1/archives", headers=self.headers)
        self.assertEqual(len(active.get_json()["data"]["archives"]), 0)

        # 7. 恢复 -> 回到 active
        restore = self.client.post(f"/api/v1/archives/{aid}/restore", headers=self.headers)
        self.assertEqual(restore.status_code, 200, restore.get_data())
        active = self.client.get("/api/v1/archives", headers=self.headers)
        self.assertEqual(len(active.get_json()["data"]["archives"]), 1)

        # 8. 软删 -> 彻底删除
        self.client.delete(f"/api/v1/archives/{aid}", headers=self.headers)
        hard = self.client.delete(f"/api/v1/archives/trash/{aid}", headers=self.headers)
        self.assertEqual(hard.status_code, 200, hard.get_data())
        trash = self.client.get("/api/v1/archives/trash", headers=self.headers)
        self.assertEqual(len(trash.get_json()["data"]["archives"]), 0)

    def test_archive_requires_auth(self):
        resp = self.client.get("/api/v1/archives")
        self.assertEqual(resp.status_code, 401)

    def test_archive_requires_resume_job(self):
        resp = self.client.post("/api/v1/archives", json={}, headers=self.headers)
        self.assertEqual(resp.status_code, 422)

    def test_archive_ownership_isolation(self):
        resume_id = self._upload_resume(RESUME_A)
        job_id = self._upload_job("招聘 AI 产品经理", resume_id)
        resp = self._create_archive(resume_id, job_id)
        aid = resp.get_json()["data"]["archive_id"]

        # 第二个用户看不到第一个用户的归档
        user2, _err = auth_module.create_user("archiver2", "archiver2-pw123")
        token2 = auth_module.generate_jwt(user2["user_id"], user2["username"])
        headers2 = {"Authorization": f"Bearer {token2}"}
        lst = self.client.get("/api/v1/archives", headers=headers2)
        self.assertEqual(len(lst.get_json()["data"]["archives"]), 0)
        detail = self.client.get(f"/api/v1/archives/{aid}", headers=headers2)
        self.assertEqual(detail.status_code, 404)

    def test_empty_trash(self):
        resume_id = self._upload_resume(RESUME_A)
        job_id = self._upload_job("招聘 AI 产品经理", resume_id)
        resp = self._create_archive(resume_id, job_id)
        aid = resp.get_json()["data"]["archive_id"]
        self.client.delete(f"/api/v1/archives/{aid}", headers=self.headers)

        empty = self.client.delete("/api/v1/archives/trash", headers=self.headers)
        self.assertEqual(empty.status_code, 200, empty.get_data())
        trash = self.client.get("/api/v1/archives/trash", headers=self.headers)
        self.assertEqual(len(trash.get_json()["data"]["archives"]), 0)


if __name__ == "__main__":
    unittest.main()
