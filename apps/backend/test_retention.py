# -*- coding: utf-8 -*-
"""保留期自动清理测试（test_retention.py）。

覆盖：
- 只清理超期记录，未超期的原样保留（按 created_at，缺失时退回文件 mtime）
- dry-run 只统计、不落盘
- 归档不删，只标记 source_purged
- 开关关闭时 maybe_purge 完全不动数据
- 每日哨兵：同一天内第二次调用不再清理

运行：python -m unittest test_retention -v
"""

import os
import shutil
import unittest
import uuid
from datetime import datetime, timedelta, timezone

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp", "ret-" + uuid.uuid4().hex[:8])
os.environ["ENV"] = "local"
os.makedirs(_TMP, exist_ok=True)

import config as C  # noqa: E402

C.DATA_DIR = _TMP
for _name in [n for n in dir(C) if n.endswith("_DIR") and n not in ("BASE_DIR", "DATA_DIR", "LOG_DIR")]:
    _path = os.path.join(_TMP, _name.lower())
    os.makedirs(_path, exist_ok=True)
    setattr(C, _name, _path)
C.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(C.LOG_DIR, exist_ok=True)

import retention  # noqa: E402
import store as store_mod  # noqa: E402

_USER = "u-" + uuid.uuid4().hex[:8]


def _age_record(directory: str, record_id: str, days: int) -> None:
    """把记录时间改成 N 天前。"""
    path = os.path.join(directory, f"{record_id}.json")
    record = store_mod._read_json(path)
    record["created_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store_mod._write_json(path, record)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(C.RESUMES_DIR, ignore_errors=True)
        shutil.rmtree(C.JOBS_DIR, ignore_errors=True)
        shutil.rmtree(C.ARCHIVES_DIR, ignore_errors=True)

        for _path in (C.RESUMES_DIR, C.JOBS_DIR, C.ARCHIVES_DIR):
            os.makedirs(_path, exist_ok=True)
        C.RETENTION_ENABLED = True
        C.RETENTION_DAYS = 30
        # 自动清理只在生产环境执行；模块内除回归用例外一律按生产环境验证
        self._env_backup = C.ENV
        C.ENV = "production"
        retention._LAST_CHECK = 0.0
        try:
            os.remove(retention._SENTINEL)
        except OSError:
            pass

    def _make_resume(self, days: int) -> str:
        resume_id = store_mod.save_resume("候选人简历正文", {}, _USER)
        if days:
            _age_record(C.RESUMES_DIR, resume_id, days)
        return resume_id

    def _make_job(self, resume_id: str, days: int) -> str:
        job_id = store_mod.save_job(resume_id, "招聘 Python 工程师", {}, _USER)
        if days:
            _age_record(C.JOBS_DIR, job_id, days)
        return job_id

    def tearDown(self):
        C.ENV = self._env_backup

    def test_auto_purge_disabled_outside_production(self):
        """回归（2026-09-29 真实事故）：本地/测试环境绝不自动删数据，手动清理仍可用。

        事故经过：RETENTION_ENABLED 默认改为 on 后，test_notifications / test_quota 这两个
        没隔离 store 目录的模块一发起请求就触发了自动清理，把本地 data/ 下 83 份简历与
        63 个岗位全部删除。
        """
        old_resume = self._make_resume(90)
        C.ENV = "local"
        retention._LAST_CHECK = 0.0
        try:
            os.remove(retention._SENTINEL)
        except OSError:
            pass
        self.assertFalse(retention.enabled())
        self.assertIsNone(retention.maybe_purge())
        self.assertTrue(os.path.exists(os.path.join(C.RESUMES_DIR, old_resume + ".json")))
        # 显式手动清理不受环境限制
        self.assertEqual(retention.purge()["resumes"], 1)
        self.assertFalse(os.path.exists(os.path.join(C.RESUMES_DIR, old_resume + ".json")))

    def test_scan_finds_only_expired(self):
        old_resume = self._make_resume(40)
        new_resume = self._make_resume(0)
        self._make_job(old_resume, 45)
        found = retention.scan_expired(30)
        ids = [rid for rid, _ in found["resumes"]]
        self.assertEqual(ids, [old_resume])
        self.assertNotIn(new_resume, ids)
        self.assertEqual(len(found["jobs"]), 1)

    def test_purge_deletes_expired_keeps_recent(self):
        old_resume = self._make_resume(31)
        new_resume = self._make_resume(2)
        old_job = self._make_job(old_resume, 31)
        new_job = self._make_job(new_resume, 0)

        report = retention.purge()

        self.assertEqual(report["resumes"], 1)
        self.assertEqual(report["jobs"], 1)
        self.assertFalse(os.path.exists(os.path.join(C.RESUMES_DIR, old_resume + ".json")))
        self.assertFalse(os.path.exists(os.path.join(C.JOBS_DIR, old_job + ".json")))
        self.assertTrue(os.path.exists(os.path.join(C.RESUMES_DIR, new_resume + ".json")))
        self.assertTrue(os.path.exists(os.path.join(C.JOBS_DIR, new_job + ".json")))

    def test_archive_kept_and_marked(self):
        old_resume = self._make_resume(40)
        job_id = self._make_job(old_resume, 0)
        archive_id = store_mod.save_archive(
            user_id=_USER,
            resume_id=old_resume,
            job_id=job_id,
            candidate_name="张三",
            final_score=88,
            fit_tag="高匹配",
            recruitment_recommendation="优先面试",
            job_title="Python 工程师",
        )

        report = retention.purge()

        self.assertEqual(report["archives_marked"], 1)
        archive = store_mod.get_archive(archive_id, _USER)
        self.assertIsNotNone(archive, "归档不能被保留期清理删掉")
        self.assertEqual(archive["final_score"], 88)
        self.assertTrue(archive.get("source_purged"))
        self.assertTrue(archive.get("source_purged_at"))
        # 再跑一次不会重复标记
        self.assertEqual(retention.purge()["archives_marked"], 0)

    def test_dry_run_touches_nothing(self):
        old_resume = self._make_resume(60)
        archive_id = store_mod.save_archive(
            user_id=_USER,
            resume_id=old_resume,
            job_id=self._make_job(old_resume, 0),
            candidate_name="李四",
            final_score=70,
            fit_tag="部分匹配",
            recruitment_recommendation="储备观察",
            job_title="Python 工程师",
        )

        report = retention.purge(dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["resumes"], 1)
        self.assertEqual(report["archives_marked"], 1)
        self.assertTrue(os.path.exists(os.path.join(C.RESUMES_DIR, old_resume + ".json")))
        self.assertFalse(store_mod.get_archive(archive_id, _USER).get("source_purged"))


    def test_disabled_switch_does_nothing(self):
        old_resume = self._make_resume(90)
        C.RETENTION_ENABLED = False
        try:
            self.assertIsNone(retention.maybe_purge())
            self.assertFalse(retention.enabled())
            self.assertTrue(os.path.exists(os.path.join(C.RESUMES_DIR, old_resume + ".json")))
        finally:
            C.RETENTION_ENABLED = True

    def test_maybe_purge_runs_once_per_day(self):
        old_resume = self._make_resume(90)
        first = retention.maybe_purge()
        self.assertIsNotNone(first)
        self.assertEqual(first["resumes"], 1)
        # 哨兵刚写过 → 同一天内（且进程内节流已过）第二次不再清理
        retention._LAST_CHECK = 0.0
        second_resume = self._make_resume(90)
        self.assertIsNone(retention.maybe_purge())
        self.assertTrue(os.path.exists(os.path.join(C.RESUMES_DIR, second_resume + ".json")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
