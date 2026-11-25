"""Store contract tests: real repositories, mocked SQL results, no legacy imports.

Optional integration uses DATABASE_TEST_URL only when its database is review_test.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from db_store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.rid, self.jid, self.aid = [uuid4() for _ in range(4)]
        self.now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.session = MagicMock()
        self.factory = MagicMock(return_value=self.session)
        self.session.__enter__.return_value = self.session
        self.store = Store(session_factory=self.factory)

    def result(self, row=None, rows=None, scalar=None):
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        result.mappings.return_value.one.return_value = row
        result.mappings.return_value.all.return_value = rows or []
        result.scalar_one_or_none.return_value = scalar
        return result

    def resume(self):
        return dict(id=self.rid, owner_user_id=self.owner, raw_content="候选人 résumé",
                    content_type="md", processed_data={"personal_data": None, "skills": ["Python"],
                    "education": None, "processed_at": "parsed"}, legacy_payload={"original": {"name": "a.pdf"},
                    "unknown": {"preserve": True}}, created_at=self.now, deleted_at=None)

    def job(self):
        return dict(id=self.jid, owner_user_id=self.owner, source_resume_id=self.rid,
                    raw_content="Engineer JD", parsed_requirements={"job_title": "Engineer",
                    "compensation_and_benfits": {"salary": "100"}, "job_summary": None},
                    legacy_payload={}, created_at=self.now, deleted_at=None)

    def archive(self, **changes):
        row = dict(id=self.aid, owner_user_id=self.owner, resume_id=self.rid, job_id=self.jid,
                   candidate_name="Ada", category="IT", tags=["screened"], status="active",
                   payload={"job_title": "Engineer", "final_score": 91,
                   "analysis_snapshot": {"html": "<article>report</article>"}, "analysis": {"risk": ["none"]}},
                   legacy_payload={"unknown": {"keep": 1}}, created_at=self.now)
        row.update(changes)
        return row

    def compiled(self, index=-1):
        return self.session.execute.call_args_list[index].args[0].compile(dialect=postgresql.dialect())

    def test_requires_exactly_one_dependency_without_connecting(self):
        with self.assertRaises(ValueError):
            Store()
        with self.assertRaises(ValueError):
            Store(engine=MagicMock(), session_factory=self.factory)
        engine = MagicMock()
        Store(engine=engine)
        engine.connect.assert_not_called()
        self.factory.assert_not_called()

    def test_all_tenant_operations_reject_invalid_owner_before_session(self):
        calls = [lambda u: self.store.save_resume("", {}, u),
                 lambda u: self.store.get_resume(self.rid, u), lambda u: self.store.get_resume_view(self.rid, u),
                 lambda u: self.store.list_resumes(u), lambda u: self.store.delete_resume(self.rid, u),
                 lambda u: self.store.set_resume_original(self.rid, {}, u),
                 lambda u: self.store.save_job(self.rid, "", {}, u),
                 lambda u: self.store.get_job(self.jid, u), lambda u: self.store.get_job_view(self.jid, u),
                 lambda u: self.store.delete_job(self.jid, u),
                 lambda u: self.store.save_archive(u, self.rid, self.jid, "", 0, "", "", ""),
                 lambda u: self.store.get_archive(self.aid, u), lambda u: self.store.list_archives(u),
                 lambda u: self.store.update_archive_tags(self.aid, u, []),
                 lambda u: self.store.update_archive_category(self.aid, u, ""),
                 lambda u: self.store.soft_delete_archive(self.aid, u),
                 lambda u: self.store.restore_archive(self.aid, u),
                 lambda u: self.store.permanent_delete_archive(self.aid, u),
                 lambda u: self.store.archives_referencing(u),
                 lambda u: self.store.archives_referencing(u, resume_id=self.rid),
                 lambda u: self.store.find_existing_archive(u, self.rid, self.jid),
                 lambda u: self.store.query_archives(u),
                 lambda u: self.store.get_distinct_job_titles(u),
                 lambda u: self.store.get_distinct_categories(u),
                 lambda u: self.store.empty_trash(u)]
        for call in calls:
            for bad in (None, "", " ", "invalid", UUID(int=0)):
                with self.subTest(owner=bad), self.assertRaises(ValueError):
                    call(bad)
        self.factory.assert_not_called()
        for call in (lambda: self.store.get_resume(self.rid), lambda: self.store.list_resumes(),
                     lambda: self.store.set_resume_original(self.rid, {}), lambda: self.store.get_job(self.jid),
                     lambda: self.store.get_archive(self.aid)):
            with self.assertRaises(TypeError):
                call()

    def test_resume_record_and_exact_view_defaults(self):
        row = self.resume()
        self.session.execute.return_value = self.result(row)
        record = self.store.get_resume(self.rid, self.owner)
        self.assertEqual(record, {"resume_id": str(self.rid), "user_id": str(self.owner),
            "content": "候选人 résumé", "content_type": "md", "created_at": self.now.isoformat(),
            "processed": row["processed_data"], "original": {"name": "a.pdf"}, "unknown": {"preserve": True}})
        record["original"]["name"] = "mutated"
        self.assertEqual(row["legacy_payload"]["original"]["name"], "a.pdf")
        self.assertEqual(self.store.get_resume_view(self.rid, self.owner), {
            "resume_id": str(self.rid), "raw_resume": {"id": 0, "content": "候选人 résumé",
            "content_type": "md", "created_at": self.now.isoformat()}, "processed_resume": {
            "personal_data": {}, "experiences": [], "projects": [], "skills": ["Python"],
            "research_work": [], "achievements": [], "education": [], "extracted_keywords": [], "processed_at": "parsed"}})

    def test_job_exact_view_preserves_legacy_benfits_spelling(self):
        self.session.execute.return_value = self.result(self.job())
        self.assertEqual(self.store.get_job_view(self.jid, self.owner), {
            "job_id": str(self.jid), "raw_job": {"id": 0, "resume_id": str(self.rid),
            "content": "Engineer JD", "created_at": self.now.isoformat()}, "processed_job": {
            "job_title": "Engineer", "company_profile": {}, "location": {}, "date_posted": None,
            "employment_type": None, "job_summary": "", "key_responsibilities": [], "qualifications": {},
            "compensation_and_benfits": {"salary": "100"}, "application_info": {},
            "extracted_keywords": [], "processed_at": None}})

    def test_missing_and_foreign_rows_have_legacy_none_false_results(self):
        self.session.execute.return_value = self.result()
        for method, rid in (("get_resume", self.rid), ("get_resume_view", self.rid),
                            ("get_job", self.jid), ("get_job_view", self.jid), ("get_archive", self.aid)):
            self.assertIsNone(getattr(self.store, method)(rid, self.owner))
        for method, rid in (("delete_resume", self.rid), ("delete_job", self.jid),
                            ("soft_delete_archive", self.aid), ("restore_archive", self.aid),
                            ("permanent_delete_archive", self.aid)):
            self.assertFalse(getattr(self.store, method)(rid, self.owner))
        self.assertFalse(self.store.set_resume_original(self.rid, {}, self.owner))
        self.assertFalse(self.store.update_archive_tags(self.aid, self.owner, []))
        self.assertFalse(self.store.update_archive_category(self.aid, self.owner, "IT"))
        for call in self.session.execute.call_args_list:
            compiled = call.args[0].compile(dialect=postgresql.dialect())
            self.assertIn(self.owner, compiled.params.values())
            self.assertIn("owner_user_id = %(", str(compiled))

    def test_saves_bind_columns_and_preserve_snapshots(self):
        self.session.execute.return_value = self.result({"id": self.rid})
        self.assertEqual(self.store.save_resume("原文", {"skills": ["SQL"]}, self.owner), str(self.rid))
        params = self.compiled().params
        self.assertEqual(params["processed_data"], {"skills": ["SQL"]})
        self.assertEqual(params["raw_content"], "原文")
        self.assertEqual(params["owner_user_id"], self.owner)
        self.session.execute.return_value = self.result({"id": self.jid})
        self.assertEqual(self.store.save_job(str(self.rid), "JD", {"job_title": "Dev"}, self.owner), str(self.jid))
        self.assertEqual(self.compiled().params["source_resume_id"], self.rid)
        snapshot = {"nested": ["frozen"]}
        aid = self.store.save_archive(self.owner, str(self.rid), str(self.jid), "Ada", 91,
            "fit", "hire", "Engineer", analysis_snapshot=snapshot, full_analysis={"score": 91})
        self.assertIsInstance(UUID(aid), UUID)
        snapshot["nested"].append("changed")
        params = self.compiled().params
        self.assertEqual(params["payload"]["analysis_snapshot"], {"nested": ["frozen"]})
        self.assertEqual(params["payload"]["analysis"], {"score": 91})
        self.assertEqual(params["category"], "Engineer")
        self.assertEqual(params["tags"], [])

    def test_archive_snapshot_survives_missing_sources_and_metadata_changes(self):
        row = self.archive(resume_id=None, job_id=None,
                           legacy_payload={"resume_id": str(self.rid), "job_id": str(self.jid), "unknown": 1})
        self.session.execute.return_value = self.result(row)
        rec = self.store.get_archive(self.aid, self.owner)
        self.assertEqual(rec["resume_id"], str(self.rid))
        self.assertEqual(rec["job_id"], str(self.jid))
        self.assertEqual(rec["analysis_snapshot"], {"html": "<article>report</article>"})
        self.assertEqual(rec["analysis"], {"risk": ["none"]})
        self.assertEqual(rec["status"], "active")
        self.assertIsNone(rec["trashed_at"])
        self.assertTrue(self.store.update_archive_category(self.aid, self.owner, ""))
        self.assertEqual(self.compiled().params["category"], "Engineer")
        self.assertNotIn("payload", self.compiled().params)
        self.assertTrue(self.store.update_archive_tags(self.aid, self.owner, ["new"]))
        self.assertEqual(self.compiled().params["tags"], ["new"])
        self.assertEqual(row["tags"], ["screened"])

    def test_archive_state_guards_and_transition_timestamp(self):
        for method, status in (("soft_delete_archive", "trashed"), ("restore_archive", "active"),
                               ("permanent_delete_archive", "active")):
            self.session.execute.return_value = self.result(self.archive(status=status))
            self.assertFalse(getattr(self.store, method)(self.aid, self.owner))
        self.session.execute.return_value = self.result(self.archive(status="trashed"))
        self.assertFalse(self.store.update_archive_tags(self.aid, self.owner, []))
        self.assertFalse(self.store.update_archive_category(self.aid, self.owner, "X"))
        self.session.execute.return_value = self.result(self.archive())
        self.assertTrue(self.store.soft_delete_archive(self.aid, self.owner))
        self.assertEqual(self.compiled().params["status"], "trashed")
        self.assertIsNotNone(self.compiled().params["legacy_payload"]["trashed_at"])
        self.assertEqual(self.compiled().params["legacy_payload"]["unknown"], {"keep": 1})
        self.session.execute.return_value = self.result(self.archive(status="trashed"))
        self.assertTrue(self.store.restore_archive(self.aid, self.owner))
        self.assertIsNone(self.compiled().params["legacy_payload"]["trashed_at"])
        self.session.execute.side_effect = [self.result(self.archive(status="trashed")), self.result(scalar=self.aid)]
        self.assertTrue(self.store.permanent_delete_archive(self.aid, self.owner))

    def test_lists_are_untruncated_owner_scoped_and_admin_is_explicit(self):
        self.session.execute.return_value = self.result(rows=[self.resume()] * 105)
        self.assertEqual(len(self.store.list_resumes(self.owner)), 105)
        self.assertIn(self.owner, self.compiled().params.values())
        self.assertNotIn("LIMIT", str(self.compiled()))
        self.store.admin_list_resumes()
        self.assertNotIn("owner_user_id =", str(self.compiled()))
        low = self.archive(payload={"final_score": 20})
        high = self.archive(payload={"final_score": 90})
        self.session.execute.return_value = self.result(rows=[low, high])
        self.assertEqual([r["final_score"] for r in self.store.list_archives(self.owner, "trashed")], [90, 20])
        self.assertIn(self.owner, self.compiled().params.values())
        self.assertIn("trashed", self.compiled().params.values())
        with self.assertRaises(ValueError):
            self.store.list_archives(self.owner, "deleted")

    def test_admin_resume_lookup_is_explicit_and_validated(self):
        for invalid in (None, "", "invalid", UUID(int=0)):
            with self.assertRaises(ValueError):
                self.store.admin_get_resume(invalid)
        self.factory.assert_not_called()
        self.session.execute.return_value = self.result(self.resume())
        self.assertEqual(self.store.admin_get_resume(str(self.rid))["user_id"], str(self.owner))
        self.assertIn(self.rid, self.compiled().params.values())
        self.assertIn("deleted_at IS NULL", str(self.compiled()))
        self.assertNotIn("owner_user_id =", str(self.compiled()))
        self.session.execute.return_value = self.result()
        self.assertIsNone(self.store.admin_get_resume(self.rid))
        with self.assertRaises(TypeError):
            self.store.get_resume(self.rid)

    def test_archive_references_use_or_without_duplicates_and_preserve_status_order(self):
        trashed = self.archive(id=uuid4(), status="trashed", resume_id=None, job_id=None,
                               payload={"resume_id": str(self.rid), "job_id": str(self.jid), "final_score": 100})
        active = self.archive()
        unrelated = self.archive(id=uuid4(), resume_id=uuid4(), job_id=uuid4(), payload={})
        self.session.execute.return_value = self.result(rows=[trashed, active, unrelated])
        hits = self.store.archives_referencing(self.owner, resume_id=str(self.rid), job_id=str(self.jid))
        self.assertEqual([r["archive_id"] for r in hits], [str(self.aid), str(trashed["id"])])
        self.assertEqual(self.store.find_existing_archive(self.owner, str(self.rid), str(self.jid)), str(self.aid))
        self.assertIn(self.owner, self.compiled().params.values())
        self.assertNotIn("LIMIT", str(self.compiled()))
        self.session.execute.return_value = self.result(rows=[trashed])
        self.assertEqual(self.store.find_existing_archive(self.owner, str(self.rid), str(self.jid)), str(trashed["id"]))
        self.assertIsNone(self.store.find_existing_archive(self.owner, str(uuid4()), str(self.jid)))
        self.session.execute.reset_mock()
        self.assertEqual(self.store.archives_referencing(self.owner), [])
        self.session.execute.assert_not_called()

    def test_archive_query_filters_literals_exact_tags_fallbacks_and_sort(self):
        old = self.archive(candidate_name="ADA %_ Example", category="", tags=["Screened"],
                           payload={"job_title": "Data Engineer", "final_score": 99})
        new = self.archive(id=uuid4(), candidate_name="Ada %_ Next", category="Data", tags=["screened"],
                           created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                           payload={"job_title": "Data Engineer", "final_score": None})
        other = self.archive(id=uuid4(), candidate_name="Ada Other", tags=["screened extra"],
                             payload={"job_title": "Manager", "final_score": 100})
        self.session.execute.return_value = self.result(rows=[new, other, old])
        hits = self.store.query_archives(self.owner, "aDa %_", "ENGINE", "SCREENED", "data", "created")
        self.assertEqual([r["archive_id"] for r in hits], [str(new["id"]), str(self.aid)])
        self.assertEqual([r["final_score"] for r in self.store.query_archives(
            self.owner, "aDa %_", "ENGINE", "SCREENED", "data", "unknown")], [99, None])
        self.assertEqual(self.store.query_archives(self.owner, tag="screen"), [])
        self.assertEqual(self.store.query_archives(self.owner, name_keyword="%_x"), [])
        self.assertIn("active", self.compiled().params.values())
        self.assertIn(self.owner, self.compiled().params.values())
        self.session.execute.return_value = self.result(rows=[old] * 105)
        self.assertEqual(len(self.store.query_archives(self.owner)), 105)
        self.assertNotIn("LIMIT", str(self.compiled()))

    def test_distinct_filters_strip_sort_deduplicate_and_fallback(self):
        rows = [self.archive(category="", payload={"job_title": " Engineer "}),
                self.archive(category=" IT ", payload={"job_title": "Engineer"}),
                self.archive(category=None, payload={"job_title": None}),
                self.archive(category="IT", payload={"job_title": " Analyst "})]
        self.session.execute.return_value = self.result(rows=rows)
        self.assertEqual(self.store.get_distinct_job_titles(self.owner), ["Analyst", "Engineer"])
        self.assertEqual(self.store.get_distinct_categories(self.owner), ["Engineer", "IT"])
        self.assertIn("active", self.compiled().params.values())

    def test_admin_names_include_retained_ids_strip_truncate_and_first_name_wins(self):
        rows = [self.archive(candidate_name=" " + "名" * 45 + " ", resume_id=None,
                             payload={"resume_id": str(self.rid)}),
                self.archive(candidate_name="Later", status="trashed"),
                self.archive(candidate_name="", resume_id=uuid4()),
                self.archive(candidate_name="Missing source", resume_id=None, payload={}),
                self.archive(candidate_name="Other", resume_id=uuid4(), owner_user_id=uuid4())]
        self.session.execute.return_value = self.result(rows=rows)
        names = self.store.admin_name_by_resume_id()
        self.assertEqual(names[str(self.rid)], "名" * 40)
        self.assertEqual(len(names), 2)
        self.assertNotIn("owner_user_id =", str(self.compiled()))
        self.assertIn(["active", "trashed"], self.compiled().params.values())
        self.assertFalse(hasattr(self.store, "name_by_resume_id"))

    def test_empty_trash_is_atomic_owner_and_status_scoped_and_counts_deleted_rows(self):
        self.session.execute.return_value.all.return_value = [(self.aid,), (uuid4(),)]
        self.assertEqual(self.store.empty_trash(self.owner), 2)
        statement = str(self.compiled())
        self.assertTrue(statement.startswith("DELETE FROM archives"))
        self.assertIn("RETURNING archives.id", statement)
        self.assertIn("owner_user_id =", statement)
        self.assertIn(self.owner, self.compiled().params.values())
        self.assertIn("trashed", self.compiled().params.values())
        self.factory.assert_called_once_with()
        self.session.begin.assert_called_once_with()
        self.session.execute.return_value.all.return_value = []
        self.assertEqual(self.store.empty_trash(self.owner), 0)
        self.session.execute.side_effect = RuntimeError("delete failed")
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            self.store.empty_trash(self.owner)
        self.assertIs(self.session.begin.return_value.__exit__.call_args.args[0], RuntimeError)

    def test_metadata_update_merges_unknown_legacy_and_does_not_mutate_input(self):
        self.session.execute.return_value = self.result(self.resume())
        metadata = {"name": "new.pdf", "nested": [1]}
        self.assertTrue(self.store.set_resume_original(self.rid, metadata, self.owner))
        metadata["nested"].append(2)
        self.assertEqual(self.compiled().params["legacy_payload"], {
            "original": {"name": "new.pdf", "nested": [1]}, "unknown": {"preserve": True}})
        self.assertIn("FOR UPDATE", str(self.compiled(-2)))

    def test_one_transaction_and_session_per_operation_and_rollback_on_error(self):
        self.session.execute.return_value = self.result(self.resume())
        self.store.get_resume_view(self.rid, self.owner)
        self.factory.assert_called_once_with()
        self.session.begin.assert_called_once_with()
        self.session.begin.return_value.__exit__.assert_called_once_with(None, None, None)
        self.session.__exit__.assert_called_once_with(None, None, None)
        self.session.reset_mock()
        self.session.execute.side_effect = RuntimeError("database failure")
        with self.assertRaisesRegex(RuntimeError, "database failure"):
            self.store.delete_resume(self.rid, self.owner)
        self.assertIs(self.session.begin.return_value.__exit__.call_args.args[0], RuntimeError)
        self.assertIs(self.session.__exit__.call_args.args[0], RuntimeError)


@unittest.skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class StoreLiveTests(unittest.TestCase):
    def test_crud_tenant_isolation_snapshots_and_transaction_rollback(self):
        from sqlalchemy import delete
        from sqlalchemy.orm import Session
        from db_models import Archive, Job, Resume, User
        from db_security import migration_engine
        url = os.environ["DATABASE_TEST_URL"]
        self.assertEqual(make_url(url).database, "review_test", "Only disposable review_test allowed")
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        owners = [uuid4(), uuid4()]
        def cleanup():
            with engine.begin() as conn:
                for model in (Archive, Job, Resume):
                    conn.execute(delete(model).where(model.owner_user_id.in_(owners)))
                conn.execute(delete(User).where(User.id.in_(owners)))
        self.addCleanup(cleanup)
        with engine.begin() as conn:
            for owner in owners:
                conn.execute(User.__table__.insert(), {"id": owner, "username": owner.hex,
                    "password_hash": "test", "password_salt": "test"})
        store = Store(engine=engine)
        a, b = map(str, owners)
        rid = store.save_resume("原文", {"skills": ["SQL"]}, a)
        jid = store.save_job(rid, "JD", {}, a)
        aid = store.save_archive(a, rid, jid, "Ada", 90, "fit", "hire", "Engineer",
                                 analysis_snapshot={"report": ["retained"]})
        self.assertIsNone(store.get_resume(rid, b))
        self.assertIsNone(store.get_job(jid, b))
        self.assertIsNone(store.get_archive(aid, b))
        self.assertFalse(store.delete_resume(rid, b))
        self.assertFalse(store.update_archive_tags(aid, b, ["hijack"]))
        self.assertFalse(store.soft_delete_archive(aid, b))
        self.assertEqual(store.list_resumes(b), [])
        self.assertEqual(store.list_archives(b), [])
        self.assertEqual(store.archives_referencing(b, resume_id=rid, job_id=jid), [])
        self.assertIsNone(store.find_existing_archive(b, rid, jid))
        self.assertEqual(store.query_archives(b, name_keyword="Ada"), [])
        self.assertEqual(store.get_distinct_job_titles(b), [])
        self.assertEqual(store.get_distinct_categories(b), [])
        self.assertEqual(store.admin_get_resume(rid)["user_id"], a)
        self.assertEqual(store.admin_name_by_resume_id()[rid], "Ada")
        self.assertEqual(store.find_existing_archive(a, rid, jid), aid)
        self.assertEqual([r["archive_id"] for r in store.query_archives(a, "ADA", "engine")], [aid])
        self.assertTrue(store.set_resume_original(rid, {"name": "original.pdf"}, a))
        self.assertEqual(store.get_resume(rid, a)["original"], {"name": "original.pdf"})
        self.assertTrue(store.update_archive_category(aid, a, "IT"))
        self.assertTrue(store.soft_delete_archive(aid, a))
        self.assertFalse(store.update_archive_tags(aid, a, ["blocked"]))
        self.assertEqual(store.list_archives(a), [])
        self.assertEqual(len(store.list_archives(a, "trashed")), 1)
        self.assertTrue(store.restore_archive(aid, a))
        self.assertTrue(store.delete_job(jid, a))
        self.assertTrue(store.delete_resume(rid, a))
        self.assertEqual(store.get_archive(aid, a)["analysis_snapshot"], {"report": ["retained"]})
        self.assertEqual(store.get_archive(aid, a)["resume_id"], rid)
        self.assertEqual(store.find_existing_archive(a, rid, jid), aid)
        self.assertEqual([r["archive_id"] for r in store.archives_referencing(a, resume_id=rid)], [aid])
        self.assertEqual(store.empty_trash(a), 0)  # Active archives survive.
        self.assertTrue(store.soft_delete_archive(aid, a))
        self.assertEqual(store.empty_trash(b), 0)  # A different owner cannot empty this trash.
        self.assertIsNotNone(store.get_archive(aid, a))
        self.assertEqual(store.empty_trash(a), 1)
        self.assertEqual(store.empty_trash(a), 0)
        self.assertIsNone(store.get_archive(aid, a))
        # A real database FK failure rolls the operation back.
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            store.save_job(str(uuid4()), "invalid source", {}, a)
        with Session(engine) as session:
            self.assertEqual(session.execute(Job.__table__.select().where(Job.owner_user_id == owners[0])).all(), [])


if __name__ == "__main__":
    unittest.main()
