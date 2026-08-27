import io
import os
import shutil
import unittest

import app as backend
import auth as auth_module


class ReviewMarkersRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-data-review")
        os.makedirs(self.temp_dir, exist_ok=True)
        backend.store.RESUMES_DIR = os.path.join(self.temp_dir, "resumes")
        backend.store.JOBS_DIR = os.path.join(self.temp_dir, "jobs")
        auth_module.USERS_DIR = os.path.join(self.temp_dir, "users")
        auth_module._INDEX_FILE = os.path.join(auth_module.USERS_DIR, "_index.json")
        auth_module._INDEX_LOCK = os.path.join(auth_module.USERS_DIR, "_index.lock")
        auth_module.RESETS_DIR = os.path.join(self.temp_dir, "password_resets")
        os.makedirs(auth_module.RESETS_DIR, exist_ok=True)
        user, _err = auth_module.create_user("reviewer", "password123")
        self.user_id = user["user_id"]
        self.token = auth_module.generate_jwt(user["user_id"], user["username"])
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = backend.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_review_markers_returns_annotations_linked_to_original_text(self):
        resume_id = backend.store.save_resume(
            "张三\n负责 Java 服务开发和上线\n负责招聘培训",
            {},
            self.user_id,
        )
        response = self.client.post(
            "/api/v1/resumes/review-markers",
            json={
                "resume_id": resume_id,
                "candidate_name": "张三",
                "analysis": {
                    "final_score": 88,
                    "recruitment_recommendation": "优先面试",
                    "strengths": ["负责 Java 服务开发和上线"],
                    "skill_match": {"project_match_points": ["负责招聘培训"], "hard_skills": []},
                    "risk_points": [],
                },
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["candidate_name"], "张三")
        self.assertGreaterEqual(len(data["annotations"]), 1)
        for annotation in data["annotations"]:
            self.assertIn(annotation["quote"], "张三\n负责 Java 服务开发和上线\n负责招聘培训")

    def test_review_markers_require_auth_and_resume(self):
        response = self.client.post(
            "/api/v1/resumes/review-markers",
            json={"resume_id": "does-not-exist", "analysis": {}},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
