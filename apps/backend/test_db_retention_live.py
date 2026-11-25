"""Transactional retention acceptance on a guarded disposable PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

import sqlalchemy as sa

from db_models import AnalysisTask, Archive, Job, Resume
from db_retention import purge_expired


# Keep the fixture local to this test module; the content live module exposes
# the same isolated setup but is intentionally not imported as a test suite.
from test_db_content_live import ContentFixture as SQLContentFixture


@skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class RetentionLiveTests(SQLContentFixture, TestCase):
    NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    CUTOFF = NOW - timedelta(days=30)

    def setUp(self):
        # Retention is intentionally a database-wide scan.  Keep each test's
        # synthetic original store isolated from rows created by prior tests.
        with self.bootstrap.begin() as conn:
            conn.execute(sa.text("TRUNCATE users CASCADE"))
        super().setUp()

    def expire(self, *, resume_id=None, job_id=None, retention_until=None):
        with self.bootstrap.begin() as conn:
            if resume_id is not None:
                conn.execute(sa.update(Resume).where(Resume.id == UUID(str(resume_id))).values(
                    created_at=self.CUTOFF - timedelta(seconds=1), retention_until=retention_until))
            if job_id is not None:
                conn.execute(sa.update(Job).where(Job.id == UUID(str(job_id))).values(
                    created_at=self.CUTOFF - timedelta(seconds=1)))

    def purge(self, *, days=30, limit=100):
        with self.sessions() as session, session.begin():
            return purge_expired(session, self.objects, now=self.NOW, days=days, limit=limit)

    def resume_row(self, rid):
        with self.bootstrap.connect() as conn:
            return conn.execute(sa.select(Resume.__table__).where(Resume.id == UUID(str(rid)))).mappings().one()

    def job_row(self, jid):
        with self.bootstrap.connect() as conn:
            return conn.execute(sa.select(Job.__table__).where(Job.id == UUID(str(jid)))).mappings().one()

    def test_resume_purge_commits_archive_marker_and_gc_worker_replays(self):
        rid, jid = self.sources()
        self.archive(rid, jid)
        before = self.resume_row(rid)
        self.expire(resume_id=rid)

        result = self.purge()
        self.assertEqual(result["resumes"], 1)
        self.assertEqual(result["jobs"], 0)
        self.assertEqual(result["originals_queued"], 1)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(len(self.files()), 1)

        row = self.resume_row(rid)
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual((row["raw_content"], row["processed_data"], row["legacy_payload"],
                          row["object_key"], row["content_sha256"], row["source_filename"], row["candidate_name"]),
                         ("", {}, {}, None, None, None, None))
        with self.bootstrap.connect() as conn:
            archive = conn.execute(sa.select(Archive.__table__).where(
                Archive.owner_user_id == UUID(self.owner), Archive.resume_id == UUID(rid))).mappings().one()
            audit = conn.execute(sa.text("SELECT action, actor_identity FROM audit_logs "
                                        "WHERE target_type='resume' AND target_id=:rid "
                                        "ORDER BY id DESC LIMIT 1"), {"rid": rid}).mappings().one()
        self.assertTrue(archive["payload"]["source_purged"])
        self.assertEqual(archive["payload"]["analysis_snapshot"]["final_score"], 85)
        self.assertEqual((audit["action"], audit["actor_identity"]), ("retention.resume_purge", "retention"))
        self.assertEqual(before["object_key"], self.queue()[0]["object_key"])

        self.assertEqual(self.service.collect_originals(), 1)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.files(), [])

        # A committed tombstone is safe to replay and cannot duplicate billing,
        # audit, or GC work.
        self.assertEqual(self.purge()["resumes"], 0)
        self.assertEqual(self.queue(), [])

    def test_expired_job_redacts_source_and_preserves_archive_snapshot(self):
        rid, jid = self.sources()
        self.archive(rid, jid)
        # Keep the source resume alive so this invocation exercises the job path.
        self.expire(resume_id=rid, retention_until=self.NOW + timedelta(days=1), job_id=jid)

        result = self.purge(limit=1)
        self.assertEqual((result["resumes"], result["jobs"]), (0, 1))
        row = self.job_row(jid)
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual((row["raw_content"], row["parsed_requirements"], row["legacy_payload"],
                          row["title"], row["company_name"]), ("", {}, {}, None, None))
        with self.bootstrap.connect() as conn:
            archive = conn.execute(sa.select(Archive.__table__).where(
                Archive.owner_user_id == UUID(self.owner), Archive.job_id == UUID(jid))).mappings().one()
        self.assertTrue(archive["payload"]["source_purged"])
        self.assertIn("analysis_snapshot", archive["payload"])

    def test_unfinished_task_is_skipped_then_terminal_retry_purges(self):
        rid = self.upload()
        self.expire(resume_id=rid)
        task = uuid4()
        with self.bootstrap.begin() as conn:
            conn.execute(AnalysisTask.__table__.insert().values(
                id=task, owner_user_id=UUID(self.owner), status="running", total_candidates=1,
                created_at=self.CUTOFF - timedelta(seconds=1)))
        skipped = self.purge()
        self.assertEqual((skipped["resumes"], skipped["skipped_unfinished"]), (0, 1))
        self.assertNotEqual(self.resume_row(rid)["raw_content"], "")
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(AnalysisTask).where(AnalysisTask.id == task).values(
                status="failed", finished_at=self.NOW))
        committed = self.purge()
        self.assertEqual(committed["resumes"], 1)
        self.assertIsNotNone(self.resume_row(rid)["deleted_at"])

    def test_audit_failure_rolls_back_redaction_archive_and_gc(self):
        rid = self.upload()
        self.expire(resume_id=rid)
        original = deepcopy(self.resume_row(rid))
        with patch("db_retention.append_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                self.purge()
        row = self.resume_row(rid)
        self.assertEqual(row["raw_content"], original["raw_content"])
        self.assertIsNone(row["deleted_at"])
        self.assertEqual(self.queue(), [])

    def test_storage_identity_mismatch_fails_before_any_write(self):
        rid = self.upload()
        row = self.resume_row(rid)
        payload = deepcopy(row["legacy_payload"])
        payload["original"]["storage_id"] = str(uuid4())
        self.expire(resume_id=rid)
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(Resume).where(Resume.id == UUID(rid)).values(legacy_payload=payload))
        with self.assertRaisesRegex(Exception, "storage identity mismatch"):
            self.purge()
        after = self.resume_row(rid)
        self.assertEqual(after["raw_content"], row["raw_content"])
        self.assertIsNone(after["deleted_at"])
        self.assertEqual(self.queue(), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
