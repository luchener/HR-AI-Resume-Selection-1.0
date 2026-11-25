"""Unit and guarded PostgreSQL tests for the opt-in change log."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from db_models import User
from db_security import migration_engine
from db_test_guard import review_test_url

from db_change_log import (
    acknowledge_change,
    append_change,
    change_log,
    claim_changes,
    compensate_dead_change,
    fail_change,
)


def _conn(*results):
    conn = MagicMock()
    conn.dialect.name = "postgresql"
    conn.in_transaction.return_value = True
    conn.execute.side_effect = list(results)
    return conn


def _result(*, row=None, rows=None, scalar=None):
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.one.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one.return_value = scalar
    return result


class ChangeLogUnitTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid4()
        self.now = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)

    def test_requires_caller_owned_postgresql_transaction(self):
        conn = MagicMock()
        conn.dialect.name = "sqlite"
        conn.in_transaction.return_value = True
        with self.assertRaisesRegex(RuntimeError, "explicit PostgreSQL transaction"):
            append_change(conn, owner_user_id=self.owner, aggregate_type="resume", aggregate_id="r1",
                          event_type="resume.created", idempotency_key="req-1", payload={})
        conn.execute.assert_not_called()

    def test_rejects_non_object_or_non_finite_payload_before_sql(self):
        for payload in ([], "text", {"score": float("nan")}, {"api_key": "never-store"}):
            conn = _conn()
            with self.subTest(payload=repr(payload)), self.assertRaises(ValueError):
                append_change(conn, owner_user_id=self.owner, aggregate_type="resume", aggregate_id="r1",
                              event_type="resume.created", idempotency_key="req-1", payload=payload)
            conn.execute.assert_not_called()

    def test_append_locks_stream_assigns_sequence_and_never_commits(self):
        row = {"id": uuid4(), "owner_user_id": self.owner, "aggregate_type": "resume",
               "aggregate_id": "r1", "sequence_no": 1, "event_type": "resume.created",
               "idempotency_key": "req-1", "payload_sha256": "0" * 64}
        conn = _conn(_result(), _result(row=None), _result(scalar=1), _result(row=row))
        actual = append_change(conn, owner_user_id=self.owner, aggregate_type="resume", aggregate_id="r1",
                               event_type="resume.created", idempotency_key="req-1", payload={"name": "Ada"})
        self.assertEqual(actual, row)
        self.assertEqual(conn.execute.call_count, 4)
        lock_sql = str(conn.execute.call_args_list[0].args[0])
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        insert_sql = str(conn.execute.call_args_list[3].args[0])
        self.assertIn("ON CONFLICT", insert_sql)
        self.assertNotIn("Ada", insert_sql)
        conn.commit.assert_not_called()

    def test_replay_returns_existing_and_mismatched_replay_is_rejected(self):
        payload_digest = __import__("hashlib").sha256(b'{"name":"Ada"}').hexdigest()
        existing = {"id": uuid4(), "owner_user_id": self.owner, "aggregate_type": "resume",
                    "aggregate_id": "r1", "sequence_no": 1, "event_type": "resume.created",
                    "idempotency_key": "req-1", "payload_sha256": payload_digest}
        conn = _conn(_result(), _result(row=existing))
        self.assertEqual(append_change(conn, owner_user_id=self.owner, aggregate_type="resume", aggregate_id="r1",
                                       event_type="resume.created", idempotency_key="req-1", payload={"name": "Ada"}), existing)
        self.assertEqual(conn.execute.call_count, 2)

        conn = _conn(_result(), _result(row=existing))
        with self.assertRaisesRegex(ValueError, "different change"):
            append_change(conn, owner_user_id=self.owner, aggregate_type="resume", aggregate_id="r1",
                          event_type="resume.updated", idempotency_key="req-1", payload={"name": "Ada"})

    def test_claim_uses_skip_locked_and_bounded_lease(self):
        event_id = uuid4()
        candidate = {"id": event_id, "state": "pending", "attempts": 0, "lease_until": None}
        claimed = {**candidate, "state": "in_flight", "attempts": 1}
        conn = _conn(_result(rows=[candidate]), _result(row=claimed))
        actual = claim_changes(conn, consumer_id="shadow-worker-1", limit=1, lease_seconds=30, now=self.now)
        self.assertEqual(actual, [claimed])
        sql = str(conn.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("SKIP LOCKED", sql)
        values = conn.execute.call_args_list[1].args[0].compile().params
        self.assertEqual(values["claimed_by"], "shadow-worker-1")
        self.assertIn("attempts", str(conn.execute.call_args_list[1].args[0]))

    def test_claim_has_predecessor_order_gate(self):
        event_id = uuid4()
        candidate = {"id": event_id, "state": "pending", "attempts": 0, "lease_until": None}
        claimed = {**candidate, "state": "in_flight", "attempts": 1}
        conn = _conn(_result(rows=[candidate]), _result(row=claimed))
        claim_changes(conn, consumer_id="shadow-worker-1", limit=1, lease_seconds=30, now=self.now)
        sql = str(conn.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn("predecessor", sql)
        self.assertIn("sequence_no <", sql)
        self.assertIn("state NOT IN", sql)
        self.assertIn("compensated", sql)

    def test_ack_requires_lease_owner_and_fail_dead_letters_at_limit(self):
        event_id = uuid4()
        conn = _conn(_result(row=None))
        with self.assertRaisesRegex(ValueError, "not leased"):
            acknowledge_change(conn, event_id=event_id, consumer_id="other", now=self.now)

        leased = {"id": event_id, "attempts": 2, "state": "in_flight", "claimed_by": "shadow-worker-1"}
        dead = {**leased, "state": "dead", "last_error_code": "timeout"}
        conn = _conn(_result(row=leased), _result(row=dead))
        actual = fail_change(conn, event_id=event_id, consumer_id="shadow-worker-1", error_code="timeout",
                             max_attempts=2, now=self.now)
        self.assertEqual(actual, dead)
        values = conn.execute.call_args_list[1].args[0].compile().params
        self.assertEqual(values["state"], "dead")
        self.assertEqual(values["last_error_code"], "timeout")

    def test_compensation_requires_dead_and_records_operator(self):
        event_id = uuid4()
        dead = {"id": event_id, "state": "dead", "attempts": 2,
                "claimed_by": None, "lease_until": None}
        compensated = {**dead, "state": "compensated",
                       "compensated_by": "operator-1", "compensated_at": self.now}
        conn = _conn(_result(row=dead), _result(row=compensated))
        actual = compensate_dead_change(conn, event_id=event_id,
                                        operator_id="operator-1", now=self.now)
        self.assertEqual(actual, compensated)
        values = conn.execute.call_args_list[1].args[0].compile().params
        self.assertEqual(values["state"], "compensated")
        self.assertEqual(values["compensated_by"], "operator-1")
        self.assertEqual(values["compensated_at"], self.now)

        conn = _conn(_result(row=None))
        with self.assertRaisesRegex(ValueError, "not dead"):
            compensate_dead_change(conn, event_id=event_id,
                                   operator_id="operator-1", now=self.now)

    def test_invalid_datetime_is_rejected_before_sql(self):
        event_id = uuid4()
        invalid_values = (
            ("naive", datetime(2026, 10, 19, 1, 0)),
            ("zero", 0),
            ("empty", ""),
            ("false", False),
            ("object", object()),
        )
        for value_label, value in invalid_values:
            calls = (
                ("claim", lambda conn: claim_changes(conn, consumer_id="worker", now=value)),
                ("ack", lambda conn: acknowledge_change(conn, event_id=event_id,
                                                         consumer_id="worker", now=value)),
                ("fail", lambda conn: fail_change(conn, event_id=event_id,
                                                   consumer_id="worker", error_code="timeout",
                                                   now=value)),
                ("compensate", lambda conn: compensate_dead_change(
                    conn, event_id=event_id, operator_id="operator", now=value)),
            )
            for call_label, call in calls:
                with self.subTest(value=value_label, call=call_label):
                    conn = _conn()
                    with self.assertRaisesRegex(ValueError, "timezone"):
                        call(conn)
                    conn.execute.assert_not_called()


@unittest.skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class ChangeLogLiveTests(unittest.TestCase):
    """Run against the disposable guarded target, never a production database."""

    @classmethod
    def setUpClass(cls):
        cls.url = review_test_url(os.environ["DATABASE_TEST_URL"])
        cls.engine = migration_engine(cls.url.render_as_string(hide_password=False))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_business_and_event_share_rollback_boundary(self):
        owner = uuid4()
        event_key = "rollback-" + uuid4().hex
        with self.engine.connect() as conn:
            tx = conn.begin()
            conn.execute(sa.insert(User.__table__).values(
                id=owner, username="rollback-" + owner.hex[:12], email=None,
                password_hash="synthetic", password_salt="synthetic"))
            append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=str(owner),
                          event_type="user.created", idempotency_key=event_key, payload={"status": "active"})
            tx.rollback()
            self.assertEqual(conn.execute(sa.select(sa.func.count()).select_from(change_log)
                                          .where(change_log.c.idempotency_key == event_key)).scalar_one(), 0)

    def test_idempotent_delivery_retry_order_and_ack(self):
        owner = uuid4()
        event_key = "delivery-" + uuid4().hex
        event_id = uuid4()
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(User.__table__).values(
                    id=owner, username="delivery-" + owner.hex[:12], email=None,
                    password_hash="synthetic", password_salt="synthetic"))
                first = append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=str(owner),
                                      event_type="user.updated", idempotency_key=event_key,
                                      payload={"status": "active"}, event_id=event_id)
                replay = append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=str(owner),
                                       event_type="user.updated", idempotency_key=event_key,
                                       payload={"status": "active"})
                self.assertEqual(first["id"], replay["id"])
                self.assertEqual(first["sequence_no"], 1)

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="live-test", limit=1, lease_seconds=30,
                                        now=datetime.now(timezone.utc))
                self.assertEqual(len(claimed), 1)
                failed = fail_change(conn, event_id=event_id, consumer_id="live-test", error_code="timeout",
                                     retry_delay_seconds=0, max_attempts=3, now=datetime.now(timezone.utc))
                self.assertEqual(failed["state"], "pending")

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="live-test-2", limit=1, lease_seconds=30,
                                        now=datetime.now(timezone.utc) + timedelta(seconds=1))
                self.assertEqual(len(claimed), 1)
                acknowledged = acknowledge_change(conn, event_id=event_id, consumer_id="live-test-2",
                                                  now=datetime.now(timezone.utc))
                self.assertEqual(acknowledged["state"], "delivered")
        finally:
            with self.engine.begin() as conn:
                conn.execute(sa.delete(change_log).where(change_log.c.id == event_id))
                conn.execute(sa.delete(User.__table__).where(User.id == owner))

    def test_later_event_waits_for_predecessor_delivery(self):
        owner = uuid4()
        stream = str(owner)
        event_ids = [uuid4(), uuid4()]
        keys = ["order-" + uuid4().hex, "order-" + uuid4().hex]
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(User.__table__).values(
                    id=owner, username="order-" + owner.hex[:12], email=None,
                    password_hash="synthetic", password_salt="synthetic"))
                first = append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=stream,
                                      event_type="user.created", idempotency_key=keys[0],
                                      payload={"step": 1}, event_id=event_ids[0])
                second = append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=stream,
                                       event_type="user.updated", idempotency_key=keys[1],
                                       payload={"step": 2}, event_id=event_ids[1])
                self.assertEqual((first["sequence_no"], second["sequence_no"]), (1, 2))

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="order-worker-1", limit=10,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual([row["id"] for row in claimed], [event_ids[0]])

            # The first row is still leased, so a second worker cannot pass it.
            with self.engine.begin() as conn:
                blocked = claim_changes(conn, consumer_id="order-worker-2", limit=10,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual(blocked, [])

            with self.engine.begin() as conn:
                acknowledged = acknowledge_change(conn, event_id=event_ids[0],
                                                  consumer_id="order-worker-1",
                                                  now=datetime.now(timezone.utc))
                self.assertEqual(acknowledged["state"], "delivered")

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="order-worker-2", limit=10,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual([row["id"] for row in claimed], [event_ids[1]])
        finally:
            with self.engine.begin() as conn:
                conn.execute(sa.delete(change_log).where(change_log.c.id.in_(event_ids)))
                conn.execute(sa.delete(User.__table__).where(User.id == owner))

    def test_dead_predecessor_blocks_until_explicit_compensation(self):
        owner = uuid4()
        stream = str(owner)
        event_ids = [uuid4(), uuid4()]
        keys = ["dead-" + uuid4().hex, "dead-" + uuid4().hex]
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(User.__table__).values(
                    id=owner, username="dead-" + owner.hex[:12], email=None,
                    password_hash="synthetic", password_salt="synthetic"))
                append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=stream,
                              event_type="user.created", idempotency_key=keys[0],
                              payload={"step": 1}, event_id=event_ids[0])
                append_change(conn, owner_user_id=owner, aggregate_type="user", aggregate_id=stream,
                              event_type="user.updated", idempotency_key=keys[1],
                              payload={"step": 2}, event_id=event_ids[1])

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="dead-worker-1", limit=1,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual([row["id"] for row in claimed], [event_ids[0]])
                failed = fail_change(conn, event_id=event_ids[0], consumer_id="dead-worker-1",
                                     error_code="timeout", max_attempts=1,
                                     now=datetime.now(timezone.utc))
                self.assertEqual(failed["state"], "dead")

            future = datetime(2099, 1, 1, tzinfo=timezone.utc)
            for values in ({"available_at": future}, {"last_error_code": "unknown"},
                           {"updated_at": future}):
                with self.subTest(values=values), self.assertRaises(sa.exc.DBAPIError), \
                        self.engine.begin() as conn:
                    conn.execute(change_log.update().where(change_log.c.id == event_ids[0])
                                 .values(**values))

            with self.engine.begin() as conn:
                blocked = claim_changes(conn, consumer_id="dead-worker-2", limit=10,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual(blocked, [])

            compensated_at = datetime.now(timezone.utc)
            with self.assertRaises(sa.exc.DBAPIError), self.engine.begin() as conn:
                conn.execute(change_log.update().where(change_log.c.id == event_ids[0]).values(
                    state="compensated", compensated_at=compensated_at,
                    compensated_by="operator-1", available_at=future))
            with self.assertRaises(sa.exc.DBAPIError), self.engine.begin() as conn:
                conn.execute(change_log.update().where(change_log.c.id == event_ids[0]).values(
                    state="compensated", compensated_at=compensated_at,
                    compensated_by="operator-1", last_error_code="unknown"))
            with self.engine.connect() as conn:
                unchanged = conn.execute(sa.select(
                    change_log.c.state, change_log.c.last_error_code
                ).where(change_log.c.id == event_ids[0])).one()
                self.assertEqual(unchanged.state, "dead")
                self.assertEqual(unchanged.last_error_code, "timeout")

            with self.engine.begin() as conn:
                compensated = compensate_dead_change(
                    conn, event_id=event_ids[0], operator_id="operator-1", now=compensated_at)
                self.assertEqual(compensated["state"], "compensated")
                self.assertEqual(compensated["compensated_by"], "operator-1")
                self.assertEqual(compensated["compensated_at"], compensated_at)

            for values in ({"available_at": future}, {"last_error_code": "unknown"},
                           {"updated_at": future}):
                with self.subTest(values=values), self.assertRaises(sa.exc.DBAPIError), \
                        self.engine.begin() as conn:
                    conn.execute(change_log.update().where(change_log.c.id == event_ids[0])
                                 .values(**values))

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="dead-worker-2", limit=10,
                                        lease_seconds=30, now=datetime.now(timezone.utc))
                self.assertEqual([row["id"] for row in claimed], [event_ids[1]])
        finally:
            with self.engine.begin() as conn:
                conn.execute(sa.delete(change_log).where(change_log.c.id.in_(event_ids)))
                conn.execute(sa.delete(User.__table__).where(User.id == owner))

    def test_direct_insert_must_start_pending_without_delivery_metadata(self):
        owner = uuid4()
        now = datetime.now(timezone.utc)
        row_ids = [uuid4() for _ in range(6)]
        invalid_rows = []
        for index, (state, changes) in enumerate((
            ("in_flight", {"attempts": 1, "claimed_by": "worker-1",
                            "claimed_at": now, "lease_until": now + timedelta(seconds=30)}),
            ("delivered", {"delivered_at": now}),
            ("dead", {"last_error_code": "timeout"}),
            ("compensated", {"compensated_at": now, "compensated_by": "operator-1"}),
            ("pending", {"attempts": 1}),
            ("pending", {"last_error_code": "timeout"}),
        )):
            event_id = row_ids[index]
            invalid_rows.append((event_id, {
                "id": event_id,
                "owner_user_id": owner,
                "aggregate_type": "user",
                "aggregate_id": str(owner),
                "sequence_no": index + 1,
                "event_type": "user.created",
                "idempotency_key": "insert-" + event_id.hex,
                "payload": {},
                "payload_sha256": "0" * 64,
                "state": state,
                "attempts": 0,
                "available_at": now,
                "created_at": now,
                "updated_at": now,
                **changes,
            }))

        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(User.__table__).values(
                    id=owner, username="insert-" + owner.hex[:12], email=None,
                    password_hash="synthetic", password_salt="synthetic"))

            for event_id, values in invalid_rows:
                with self.subTest(state=values["state"], changes=values):
                    with self.assertRaises(sa.exc.DBAPIError):
                        with self.engine.begin() as conn:
                            conn.execute(sa.text("SET LOCAL ROLE resume_app"))
                            self.assertTrue(conn.scalar(sa.text(
                                "SELECT has_table_privilege(current_user, 'public.change_log', 'INSERT')")))
                            conn.execute(change_log.insert().values(**values))
                    with self.engine.connect() as conn:
                        count = conn.scalar(sa.select(sa.func.count()).select_from(change_log)
                                            .where(change_log.c.id == event_id))
                    self.assertEqual(count, 0)
        finally:
            with self.engine.begin() as conn:
                conn.execute(change_log.delete().where(change_log.c.id.in_(row_ids)))
                conn.execute(sa.delete(User.__table__).where(User.id == owner))

    def test_insert_guard_migration_downgrades_and_reapplies(self):
        config = Config(str(Path(__file__).parent / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        # Roundtrip this revision's real trigger DDL without downgrading newer
        # immutable task/artifact history or changing the global version ledger.
        migration = ScriptDirectory.from_config(config).get_revision('0020_change_log_insert_guard').module
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        def apply_revision(callback):
            with self.engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    callback()
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.text("SELECT version_num FROM alembic_version")),
                             head)
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(change_log)), 0)

        try:
            apply_revision(migration.downgrade)
            with self.engine.connect() as conn:
                self.assertEqual(conn.scalar(sa.text("SELECT version_num FROM alembic_version")),
                                 head)
                self.assertFalse(conn.scalar(sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'change_log_guard_insert_trigger' AND NOT tgisinternal)")))
                self.assertTrue(conn.scalar(sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'change_log_guard_update_trigger' AND NOT tgisinternal)")))
        finally:
            apply_revision(migration.upgrade)

        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.text("SELECT version_num FROM alembic_version")),
                             head)
            self.assertTrue(conn.scalar(sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'change_log_guard_insert_trigger' AND NOT tgisinternal)")))

    def test_trigger_enforces_complete_state_machine(self):
        owner = uuid4()
        event_id = uuid4()
        event_key = "state-machine-" + uuid4().hex
        now = datetime.now(timezone.utc) + timedelta(seconds=5)
        lease_until = now + timedelta(seconds=30)

        def reject_update(values):
            with self.assertRaises(sa.exc.DBAPIError), self.engine.begin() as conn:
                conn.execute(change_log.update().where(change_log.c.id == event_id).values(**values))

        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(User.__table__).values(
                    id=owner, username="state-" + owner.hex[:12], email=None,
                    password_hash="synthetic", password_salt="synthetic"))
                append_change(conn, owner_user_id=owner, aggregate_type="user",
                              aggregate_id=str(owner), event_type="user.created",
                              idempotency_key=event_key, payload={"status": "active"},
                              event_id=event_id)

            # pending may only enter in_flight through an atomic claim.
            reject_update({"state": "delivered", "delivered_at": now})
            reject_update({"state": "dead"})
            reject_update({"state": "compensated", "compensated_at": now,
                           "compensated_by": "operator-1"})
            reject_update({"delivered_at": now})
            reject_update({"claimed_by": "worker-1", "claimed_at": now,
                           "lease_until": lease_until})
            reject_update({"state": "in_flight", "attempts": 0,
                           "claimed_by": "worker-1", "claimed_at": now,
                           "lease_until": lease_until})

            with self.engine.begin() as conn:
                claimed = claim_changes(conn, consumer_id="worker-1", limit=1,
                                        lease_seconds=30, now=now)
                self.assertEqual([row["id"] for row in claimed], [event_id])
                self.assertEqual(claimed[0]["attempts"], 1)

            # in_flight requires a complete lease and cannot skip delivery.
            reject_update({"claimed_by": "worker-1", "claimed_at": None,
                           "lease_until": lease_until})
            reject_update({"state": "delivered"})
            reject_update({"state": "compensated", "compensated_at": now,
                           "compensated_by": "operator-1"})
            reject_update({"attempts": 2})

            delivered_at = now + timedelta(seconds=1)
            with self.engine.begin() as conn:
                delivered = acknowledge_change(
                    conn, event_id=event_id, consumer_id="worker-1", now=delivered_at)
                self.assertEqual(delivered["state"], "delivered")

            # Delivered is terminal and its delivery metadata is immutable.
            reject_update({"delivered_at": delivered_at + timedelta(seconds=1)})
            reject_update({"attempts": 2})
            reject_update({"state": "pending"})
            reject_update({"available_at": datetime(2099, 1, 1, tzinfo=timezone.utc)})
            reject_update({"last_error_code": "timeout"})
            reject_update({"updated_at": datetime(2099, 1, 1, tzinfo=timezone.utc)})
        finally:
            with self.engine.begin() as conn:
                conn.execute(sa.delete(change_log).where(change_log.c.id == event_id))
                conn.execute(sa.delete(User.__table__).where(User.id == owner))


if __name__ == "__main__":
    unittest.main()
