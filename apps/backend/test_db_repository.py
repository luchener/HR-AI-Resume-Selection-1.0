"""Repository boundary regressions. PostgreSQL compilation + mocked sessions only."""
import unittest
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from db_repository import ArchiveRepository, JobRepository, ResumeRepository, UserRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid4()
        self.record_id = uuid4()
        self.session = MagicMock()

    def compile(self, statement):
        return statement.compile(dialect=postgresql.dialect())

    def returned(self, value):
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = value
        result.mappings.return_value.one.return_value = value
        result.scalar_one_or_none.return_value = self.record_id if value else None
        return result

    def assert_guard(self, statement, repository):
        compiled = self.compile(statement)
        sql = str(compiled)
        self.assertIn(f"{repository.table.name}.{repository.owner_key} = %(", sql)
        self.assertIn(self.owner, compiled.params.values())
        self.assertNotIn(str(self.owner), sql)
        return sql, compiled.params

    def test_owner_is_required_for_every_tenant_entrypoint(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            calls = [lambda owner: repo.get(owner, self.record_id), lambda owner: repo.list(owner),
                     lambda owner: repo.insert(owner, {}), lambda owner: repo.update(owner, self.record_id, {}),
                     lambda owner: repo.delete(owner, self.record_id)]
            if kind is ArchiveRepository:
                calls += [lambda owner: repo.soft_delete(owner, self.record_id), lambda owner: repo.restore(owner, self.record_id)]
            for bad in (None, "", " ", "not-uuid", 123, UUID(int=0)):
                for call in calls:
                    with self.subTest(kind=kind.__name__, owner=bad):
                        with self.assertRaises(ValueError):
                            call(bad)
        self.session.execute.assert_not_called()

    def test_no_implicit_owner_default(self):
        repo = ResumeRepository(self.session)
        for method in (repo.get, repo.list, repo.insert, repo.update, repo.delete):
            with self.assertRaises(TypeError):
                method()

    def test_get_and_list_queries_are_guarded_and_paginated(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            repo.get(str(self.owner), str(self.record_id))
            sql, params = self.assert_guard(self.session.execute.call_args.args[0], repo)
            self.assertIn(self.record_id, params.values())
            repo.list(self.owner, page=3, size=10)
            sql, params = self.assert_guard(self.session.execute.call_args.args[0], repo)
            self.assertIn("LIMIT", sql)
            self.assertIn("OFFSET", sql)
            self.assertIn(10, params.values())
            self.assertIn(20, params.values())

    def test_search_is_literal_and_bound(self):
        repo = ResumeRepository(self.session)
        payload = "x%' OR 1=1; --_\\"
        repo.list(self.owner, search=payload)
        sql, params = self.assert_guard(self.session.execute.call_args.args[0], repo)
        self.assertNotIn(payload, sql)
        self.assertIn("%x\\%' OR 1=1; --\\_\\\\%", params.values())
        self.assertIn("ESCAPE", sql)

    def test_pagination_and_sort_reject_before_query(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            for options in ({"sort": "created_at; DROP TABLE users"}, {"page": 0}, {"size": 101},
                            {"size": True}, {"page": 100002}, {"descending": "false"}, {"search": "x" * 201}):
                with self.subTest(kind=kind.__name__, options=options), self.assertRaises(ValueError):
                    repo.list(self.owner, **options)
        self.session.execute.assert_not_called()

    def test_insert_binds_owner_ids_and_payload(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            identity = self.owner if kind is UserRepository else self.record_id
            repo.insert(self.owner, {"legacy_payload": {"unknown": "'; DROP TABLE users; --"}}, record_id=identity)
            compiled = self.compile(self.session.execute.call_args.args[0])
            self.assertIn(self.owner, compiled.params.values())
            self.assertIn(identity, compiled.params.values())
            self.assertNotIn("DROP TABLE", str(compiled))
            self.assertEqual(compiled.params["legacy_payload"], {"unknown": "'; DROP TABLE users; --"})

    def test_identity_fields_cannot_be_changed(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            for values in ({"id": uuid4()}, {"owner_user_id": uuid4()}, {"created_at": "now"}, {"unrecognized": 1}):
                with self.assertRaises(ValueError):
                    repo.update(self.owner, self.record_id, values)
                with self.assertRaises(ValueError):
                    repo.insert(self.owner, values)
        self.session.execute.assert_not_called()

    def test_updates_lock_then_write_with_owner_and_preserve_unknown_legacy(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            current = {"legacy_payload": {"unknown": {"nested": 1}, "old": 2}, "status": "active"}
            self.session.execute.side_effect = [self.returned(current), self.returned(current)]
            repo.update(self.owner, self.record_id, {"legacy_payload": {"old": 3}})
            lock, write = [call.args[0] for call in self.session.execute.call_args_list[-2:]]
            self.assertIn("FOR UPDATE", self.assert_guard(lock, repo)[0])
            _, params = self.assert_guard(write, repo)
            self.assertEqual(params["legacy_payload"], {"unknown": {"nested": 1}, "old": 3})
            self.assertEqual(current["legacy_payload"]["old"], 2)
            self.assertIn("updated_at", params)

    def test_regular_update_does_not_replace_legacy_payload(self):
        repo = ResumeRepository(self.session)
        self.session.execute.side_effect = [self.returned({"legacy_payload": {"original": 1}}), self.returned({})]
        repo.update(self.owner, self.record_id, {"candidate_name": "Ada"})
        compiled = self.compile(self.session.execute.call_args.args[0])
        self.assertNotIn("legacy_payload", str(compiled).split(" RETURNING")[0])

    def test_missing_or_foreign_row_prevents_mutation(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            for operation in ("update", "delete"):
                session = MagicMock()
                session.execute.return_value = self.returned(None)
                repo = kind(session)
                args = (self.owner, self.record_id, {}) if operation == "update" else (self.owner, self.record_id)
                self.assertFalse(getattr(repo, operation)(*args))
                session.execute.assert_called_once()
                self.assertIn("FOR UPDATE", self.assert_guard(session.execute.call_args.args[0], repo)[0])

    def test_deletes_lock_and_bind_owner_on_delete_statement(self):
        for kind in (ResumeRepository, JobRepository, ArchiveRepository, UserRepository):
            repo = kind(self.session)
            self.session.execute.side_effect = [self.returned({"status": "trashed"}), self.returned(True)]
            self.assertTrue(repo.delete(self.owner, self.record_id))
            lock, write = [call.args[0] for call in self.session.execute.call_args_list[-2:]]
            self.assertIn("FOR UPDATE", self.assert_guard(lock, repo)[0])
            self.assertTrue(self.assert_guard(write, repo)[0].startswith("DELETE"))

    def test_archive_transitions_check_state_and_preserve_payload(self):
        repo = ArchiveRepository(self.session)
        for operation, before, after in (("soft_delete", "active", "trashed"), ("restore", "trashed", "active")):
            current = {"status": before, "legacy_payload": {"unknown": "keep", "status": before}}
            self.session.execute.side_effect = [self.returned(current), self.returned(current)]
            self.assertTrue(getattr(repo, operation)(self.owner, self.record_id))
            sql, params = self.assert_guard(self.session.execute.call_args.args[0], repo)
            self.assertEqual(params["status"], after)
            self.assertEqual(params["legacy_payload"]["unknown"], "keep")
            self.assertEqual(params["legacy_payload"]["status"], after)
            self.assertEqual(current["status"], before)

    def test_archive_wrong_state_never_writes(self):
        for method, status, extra in (("soft_delete", "trashed", ()), ("restore", "active", ()),
                                      ("delete", "active", ()), ("update", "trashed", ({"note": "x"},))):
            session = MagicMock()
            session.execute.return_value = self.returned({"status": status})
            self.assertFalse(getattr(ArchiveRepository(session), method)(self.owner, self.record_id, *extra))
            session.execute.assert_called_once()

    def test_archive_filters_are_scoped_literal_and_bound(self):
        repo = ArchiveRepository(self.session)
        repo.list(self.owner, status="trashed", job_title="Dev%", category="IT_", tag="HR' OR true--", search="Ada", sort="score")
        sql, params = self.assert_guard(self.session.execute.call_args.args[0], repo)
        for value in ("trashed", "%Dev\\%%", "%IT\\_%", "HR' OR true--"):
            self.assertIn(value, params.values())
            self.assertNotIn(value, sql)
        self.assertIn("jsonb_array_elements_text", sql)
        self.assertIn("jsonb_typeof", sql)
        self.assertIn("CASE WHEN", sql)

    def test_sql_expression_values_cannot_bypass_binding(self):
        for kind, field in ((ResumeRepository, "raw_content"), (JobRepository, "title"),
                            (ArchiveRepository, "note"), (UserRepository, "username")):
            repo = kind(self.session)
            with self.assertRaises(ValueError):
                repo.insert(self.owner, {field: text("current_user")})
            with self.assertRaises(ValueError):
                repo.update(self.owner, self.record_id, {field: text("current_user")})
        self.session.execute.assert_not_called()

    def test_reference_ids_are_validated_and_bound(self):
        for kind, field in ((JobRepository, "source_resume_id"), (ArchiveRepository, "resume_id"),
                            (ArchiveRepository, "job_id"), (ArchiveRepository, "analysis_id")):
            repo = kind(self.session)
            with self.assertRaises(ValueError):
                repo.insert(self.owner, {field: "bad"})
            repo.insert(self.owner, {field: str(self.record_id)})
            self.assertEqual(self.compile(self.session.execute.call_args.args[0]).params[field], self.record_id)

    def test_username_lookup_is_case_insensitive_bound_and_not_tenant_join(self):
        repo = UserRepository(self.session)
        payload = "' OR 1=1 --"
        repo.get_by_username(payload)
        compiled = self.compile(self.session.execute.call_args.args[0])
        self.assertIn("lower(users.username) = lower(%(", str(compiled))
        self.assertIn(payload, compiled.params.values())
        self.assertNotIn(payload, str(compiled))
        self.assertNotIn("JOIN", str(compiled))
        for invalid in (None, "", " ", "x" * 65):
            with self.assertRaises(ValueError):
                repo.get_by_username(invalid)

    def test_user_insert_must_use_owner_id(self):
        with self.assertRaises(ValueError):
            UserRepository(self.session).insert(self.owner, {}, record_id=self.record_id)
        self.session.execute.assert_not_called()

    def test_repository_never_controls_transaction(self):
        repo = ResumeRepository(self.session)
        self.session.execute.return_value = self.returned({"legacy_payload": {}})
        repo.get(self.owner, self.record_id)
        repo.list(self.owner)
        repo.insert(self.owner, {"raw_content": "sample"})
        repo.update(self.owner, self.record_id, {"raw_content": "changed"})
        repo.delete(self.owner, self.record_id)
        for method in ("commit", "rollback", "begin", "close"):
            getattr(self.session, method).assert_not_called()


if __name__ == "__main__":
    unittest.main()
