"""归档（候选人才库 + 回收站）store 层单元测试。

不触碰真实 data/，全部在临时目录里做（TEST_TMP_DIR 指向 .test-tmp/）。
"""
import os
import sys
import unittest
import uuid

# 注意：config.DATA_DIR 是硬编码的（不读环境变量），所以必须在 import store
# 之前把 config 的目录属性全部改成测试临时目录；store 在 import 时绑定这些值。
TEST_TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp", f"tmp_{uuid.uuid4().hex[:8]}")
os.environ["DATA_DIR"] = TEST_TMP_DIR
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

config.DATA_DIR = TEST_TMP_DIR
config.RESUMES_DIR = os.path.join(TEST_TMP_DIR, "resumes")
config.JOBS_DIR = os.path.join(TEST_TMP_DIR, "jobs")
config.ARCHIVES_DIR = os.path.join(TEST_TMP_DIR, "archives")

# 在 import store 之前，把 sys.modules 里已存在的 store 模块替换为
# 重新加载的副本（unittest 批量运行时，store 可能已被其它测试 import 过，
# 其目录常量已绑定真实 data/；不替换就会写进真实数据目录）。
if "store" in sys.modules:
    import importlib

    del sys.modules["store"]
import store  # noqa: E402
from config import ARCHIVES_DIR  # noqa: E402


class ArchiveStoreTest(unittest.TestCase):
    USER_A = "user-a"
    USER_B = "user-b"

    @classmethod
    def setUpClass(cls):
        os.makedirs(TEST_TMP_DIR, exist_ok=True)

    def setUp(self):
        # 清空归档目录，保证用例间完全隔离（_index.json 是全局文件）
        os.makedirs(ARCHIVES_DIR, exist_ok=True)
        for name in os.listdir(ARCHIVES_DIR):
            os.remove(os.path.join(ARCHIVES_DIR, name))
        self.aid = store.save_archive(
            user_id=self.USER_A,
            resume_id="resume-1",
            job_id="job-1",
            candidate_name="苏明远",
            final_score=86,
            fit_tag="高匹配",
            recruitment_recommendation="优先面试",
            job_title="AI Agent 工程师",
            custom_tags=["AI", "社招"],
            analysis_snapshot={"summary": "test"},
            category="IT",
            full_analysis={"final_score": 86, "candidate_name": "苏明远"},
        )

    def test_save_and_get(self):
        rec = store.get_archive(self.aid, user_id=self.USER_A)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "active")
        self.assertEqual(rec["candidate_name"], "苏明远")
        self.assertEqual(rec["final_score"], 86)
        self.assertEqual(rec["job_title"], "AI Agent 工程师")
        self.assertEqual(rec["category"], "IT")
        self.assertEqual(rec["analysis"]["final_score"], 86)
        self.assertIsNone(rec["trashed_at"])

    def test_ownership_isolation(self):
        # 其他用户读不到
        self.assertIsNone(store.get_archive(self.aid, user_id=self.USER_B))

    def test_find_existing_archive(self):
        found = store.find_existing_archive(self.USER_A, "resume-1", "job-1")
        self.assertEqual(found, self.aid)
        self.assertIsNone(store.find_existing_archive(self.USER_A, "resume-1", "job-2"))
        # 其他用户查不到
        self.assertIsNone(store.find_existing_archive(self.USER_B, "resume-1", "job-1"))

    def test_list_and_query(self):
        # 第二条归档，分数更高
        aid2 = store.save_archive(
            user_id=self.USER_A,
            resume_id="resume-2",
            job_id="job-2",
            candidate_name="李四",
            final_score=92,
            fit_tag="高匹配",
            recruitment_recommendation="优先面试",
            job_title="后端工程师",
        )
        active = store.list_archives(self.USER_A, status="active")
        self.assertEqual(len(active), 2)
        # 按分数降序：李四(92) 在前
        self.assertEqual(active[0]["archive_id"], aid2)
        self.assertEqual(active[1]["archive_id"], self.aid)

        # 姓名模糊查询
        by_name = store.query_archives(self.USER_A, name_keyword="李")
        self.assertEqual(len(by_name), 1)
        self.assertEqual(by_name[0]["candidate_name"], "李四")

        # 岗位筛选
        by_job = store.query_archives(self.USER_A, job_title="后端")
        self.assertEqual(len(by_job), 1)

        # 分类筛选（category 字段）
        by_cat = store.query_archives(self.USER_A, category="IT")
        self.assertEqual(len(by_cat), 1)
        self.assertEqual(by_cat[0]["candidate_name"], "苏明远")

        # 标签筛选
        by_tag = store.query_archives(self.USER_A, tag="AI")
        self.assertEqual(len(by_tag), 1)

        # 按创建时间倒序
        by_created = store.query_archives(self.USER_A, sort="created")
        self.assertEqual(len(by_created), 2)

        # 用户隔离：B 看不到 A 的任何归档
        self.assertEqual(store.list_archives(self.USER_B, status="active"), [])

    def test_distinct_job_titles(self):
        titles = store.get_distinct_job_titles(self.USER_A)
        self.assertIn("AI Agent 工程师", titles)

    def test_distinct_categories_and_fallback(self):
        # 第一个归档 category=IT；第二个归档未传 category，应回退为 job_title
        store.save_archive(
            user_id=self.USER_A,
            resume_id="resume-2",
            job_id="job-2",
            candidate_name="李四",
            final_score=92,
            fit_tag="高匹配",
            recruitment_recommendation="优先面试",
            job_title="后端工程师",
        )
        cats = store.get_distinct_categories(self.USER_A)
        self.assertIn("IT", cats)
        self.assertIn("后端工程师", cats)

    def test_update_category(self):
        self.assertTrue(store.update_archive_category(self.aid, self.USER_A, "技术"))
        rec = store.get_archive(self.aid, user_id=self.USER_A)
        self.assertEqual(rec["category"], "技术")
        # 其他用户不能改
        self.assertFalse(store.update_archive_category(self.aid, self.USER_B, "销售"))

    def test_soft_delete_restore_cycle(self):
        # 软删除
        self.assertTrue(store.soft_delete_archive(self.aid, self.USER_A))
        rec = store.get_archive(self.aid, user_id=self.USER_A)
        self.assertEqual(rec["status"], "trashed")
        self.assertIsNotNone(rec["trashed_at"])
        # 不在 active，在 trashed
        self.assertNotIn(self.aid, [a["archive_id"] for a in store.list_archives(self.USER_A, "active")])
        self.assertIn(self.aid, [a["archive_id"] for a in store.list_archives(self.USER_A, "trashed")])

        # 恢复
        self.assertTrue(store.restore_archive(self.aid, self.USER_A))
        rec = store.get_archive(self.aid, user_id=self.USER_A)
        self.assertEqual(rec["status"], "active")
        self.assertIsNone(rec["trashed_at"])
        self.assertIn(self.aid, [a["archive_id"] for a in store.list_archives(self.USER_A, "active")])

    def test_soft_delete_idempotent_and_ownership(self):
        self.assertTrue(store.soft_delete_archive(self.aid, self.USER_A))
        # 重复软删除失败
        self.assertFalse(store.soft_delete_archive(self.aid, self.USER_A))
        # 其他用户不能操作
        store.soft_delete_archive(self.aid, self.USER_A)
        self.assertFalse(store.restore_archive(self.aid, self.USER_B))

    def test_permanent_delete_and_empty_trash(self):
        aid2 = store.save_archive(
            user_id=self.USER_A,
            resume_id="resume-3",
            job_id="job-3",
            candidate_name="王五",
            final_score=70,
            fit_tag="部分匹配",
            recruitment_recommendation="储备观察",
            job_title="前端工程师",
        )
        # 未进回收站不能彻底删除
        self.assertFalse(store.permanent_delete_archive(self.aid, self.USER_A))

        store.soft_delete_archive(self.aid, self.USER_A)
        store.soft_delete_archive(aid2, self.USER_A)

        # 彻底删除单条
        self.assertTrue(store.permanent_delete_archive(self.aid, self.USER_A))
        self.assertIsNone(store.get_archive(self.aid, user_id=self.USER_A))
        self.assertNotIn(self.aid, [a["archive_id"] for a in store.list_archives(self.USER_A, "trashed")])

        # 清空回收站
        count = store.empty_trash(self.USER_A)
        self.assertEqual(count, 1)
        self.assertEqual(store.list_archives(self.USER_A, "trashed"), [])

    def test_update_tags(self):
        # 更新标签
        self.assertTrue(store.update_archive_tags(self.aid, self.USER_A, ["AI", "社招", "急聘"]))
        rec = store.get_archive(self.aid, user_id=self.USER_A)
        self.assertEqual(rec["custom_tags"], ["AI", "社招", "急聘"])

        # 回收站内不允许改标签
        store.soft_delete_archive(self.aid, self.USER_A)
        self.assertFalse(store.update_archive_tags(self.aid, self.USER_A, ["x"]))

    def test_files_live_in_archive_dirs(self):
        json_path = os.path.join(ARCHIVES_DIR, f"{self.aid}.json")
        self.assertTrue(os.path.exists(json_path))
        index_path = os.path.join(ARCHIVES_DIR, "_index.json")
        self.assertTrue(os.path.exists(index_path))


if __name__ == "__main__":
    unittest.main()
