"""Pure rules + opt-in live PostgreSQL accounting/concurrency tests.

Run: python -m unittest test_db_quota -v
Set QUOTA_TEST_DATABASE_URL explicitly for live tests. They create/drop a unique
scratch schema, never touch existing application tables, and need CREATE SCHEMA.
"""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import db_quota as q


class PolicyTest(unittest.TestCase):
    def test_legacy_precedence_matrix(self):
        for admin in (False, True):
            for quota_on in (False, True):
                for mode, raw, expected in (("inherit", None, None if admin else 7),
                                             ("unlimited", None, None), ("limited", 2, 2),
                                             ("limited", 0, 0)):
                    with self.subTest(admin=admin, quota_on=quota_on, mode=mode, raw=raw):
                        policy = q.resolve_policy(
                            {"quota_enabled": quota_on, "mode": "limited", "daily_limit": 7},
                            {"mode": mode, "daily_limit": raw}, is_admin=admin)
                        self.assertEqual(policy.daily_limit, expected if quota_on else None)
                        self.assertTrue(policy.analysis_enabled)

    def test_disabled_independent_of_quota_and_admin(self):
        for admin in (False, True):
            for quota_on in (False, True):
                policy = q.resolve_policy({"quota_enabled": quota_on}, {"enabled": False}, is_admin=admin)
                with self.assertRaises(q.QuotaDenied) as caught:
                    q.check_capacity(policy, 0, 0, 1)
                self.assertEqual(caught.exception.status_code, 403)

    def test_missing_and_global_unlimited(self):
        self.assertEqual(q.resolve_policy(), q.EffectiveQuota(True, None, False))
        for mode in ("inherit", "unlimited"):
            self.assertIsNone(q.resolve_policy({"quota_enabled": True, "mode": mode}).daily_limit)

    def test_email_master_and_user_switch(self):
        for master in (False, True):
            for user in (False, True):
                policy = q.resolve_policy({"email_notify": master}, {"email_notify": user})
                self.assertEqual(policy.email_notify, master and user)
        self.assertTrue(q.resolve_policy({"email_notify": True}).email_notify)

    def test_invalid_policies_rejected(self):
        for row in ({"mode": "other"}, {"mode": "limited"},
                    {"mode": "limited", "daily_limit": -1},
                    {"mode": "limited", "daily_limit": True},
                    {"mode": "limited", "daily_limit": "2"},
                    {"mode": "inherit", "daily_limit": 3}):
            for side in ("global", "user"):
                with self.subTest(row=row, side=side), self.assertRaises(ValueError):
                    q.resolve_policy(row if side == "global" else {}, row if side == "user" else {})

    def test_capacity_includes_pending_and_allows_exact_boundary(self):
        policy = q.EffectiveQuota(True, 5, False)
        q.check_capacity(policy, 2, 1, 2)
        with self.assertRaises(q.QuotaDenied) as caught:
            q.check_capacity(policy, 2, 2, 2)
        self.assertEqual(caught.exception.status_code, 429)
        with self.assertRaises(q.QuotaDenied):
            q.check_capacity(q.EffectiveQuota(True, 0, False), 0, 0, 1)
        q.check_capacity(q.EffectiveQuota(True, None, False), 999, 999, 999)

    def test_invalid_counts_not_coerced(self):
        for count in (0, -1, True, 1.1, "2", None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                q.check_capacity(q.resolve_policy(), 0, 0, count)
        for used, reserved in ((-1, 0), (0, -1), (True, 0), (0, 1.2)):
            with self.assertRaises(ValueError):
                q.check_capacity(q.resolve_policy(), used, reserved, 1)

    def test_utc_day(self):
        local = datetime(2026, 6, 2, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(q.utc_day(local), date(2026, 6, 1))
        with self.assertRaises(ValueError):
            q.utc_day(datetime(2026, 6, 1))

    def test_core_schema_compiles_without_db_models(self):
        for table in q.TABLES:
            ddl = str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
            self.assertIn("CREATE TABLE", ddl)
        self.assertEqual(set(q.quota_daily_usage.primary_key.columns.keys()), {"user_id", "date"})
        self.assertEqual(set(q.quota_reservations.primary_key.columns.keys()), {"request_id"})

    def test_operations_reject_other_databases_before_writing(self):
        engine = sa.create_engine("sqlite://")
        try:
            with engine.begin() as conn:
                with self.assertRaises(RuntimeError):
                    q.reserve(conn, request_id=uuid4(), user_id=uuid4(), count=1, policy=q.resolve_policy())
                with self.assertRaises(RuntimeError):
                    q.settle(conn, uuid4(), succeeded=True)
        finally:
            engine.dispose()


@unittest.skipUnless(os.environ.get("QUOTA_TEST_DATABASE_URL"), "set QUOTA_TEST_DATABASE_URL for live PostgreSQL")
class LiveQuotaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["QUOTA_TEST_DATABASE_URL"]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        cls.raw_engine = sa.create_engine(url)
        if cls.raw_engine.dialect.name != "postgresql":
            cls.raw_engine.dispose()
            raise RuntimeError("live quota tests require PostgreSQL")
        cls.schema = "quota_test_" + uuid4().hex
        with cls.raw_engine.begin() as conn:
            conn.execute(sa.schema.CreateSchema(cls.schema))
        cls.engine = cls.raw_engine.execution_options(schema_translate_map={None: cls.schema})
        try:
            q.metadata.create_all(cls.engine)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.raw_engine.begin() as conn:
                conn.execute(sa.schema.DropSchema(cls.schema, cascade=True))
        finally:
            cls.raw_engine.dispose()

    def setUp(self):
        self.uid = uuid4()
        self.day = date(2026, 6, 1)
        self.policy = q.EffectiveQuota(True, 3, False)
        with self.engine.begin() as conn:
            conn.execute(q.metadata.tables["users"].insert().values(id=self.uid))

    def reserve(self, rid, count=1, **kwargs):
        with self.engine.begin() as conn:
            return dict(q.reserve(conn, request_id=rid, user_id=self.uid, day=self.day,
                                  count=count, policy=self.policy, **kwargs))

    def settle(self, rid, succeeded):
        with self.engine.begin() as conn:
            return dict(q.settle(conn, rid, succeeded=succeeded))

    def usage(self):
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(q.quota_daily_usage).where(
                q.quota_daily_usage.c.user_id == self.uid)).mappings().one()
            return row["used"], row["reserved"]

    def test_partial_settlement_charges_only_completed_and_is_idempotent(self):
        rid = uuid4()
        self.reserve(rid, 3)
        with self.engine.begin() as conn:
            first = dict(q.settle_partial(conn, rid, 1))
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(self.usage(), (1, 0))
        with self.engine.begin() as conn:
            repeated = dict(q.settle_partial(conn, rid, 3))
        self.assertEqual(repeated['status'], 'failed')
        self.assertEqual(self.usage(), (1, 0))
        with self.assertRaises(KeyError):
            with self.engine.begin() as conn:
                q.settle_partial(conn, uuid4(), 1)

    def test_success_failure_and_first_terminal_outcome(self):
        a, b = uuid4(), uuid4()
        self.reserve(a, 2)
        self.reserve(b)
        self.assertEqual(self.usage(), (0, 3))
        with self.assertRaises(q.QuotaDenied):
            self.reserve(uuid4())
        self.settle(a, True)
        self.settle(a, True)
        self.settle(a, False)
        self.settle(b, False)
        self.settle(b, True)
        self.assertEqual(self.usage(), (2, 0))
        self.assertEqual(self.reserve(a, 2)["status"], "succeeded")
        self.assertEqual(self.reserve(b)["status"], "failed")
        self.assertEqual(self.usage(), (2, 0))

    def test_concurrent_capacity_and_duplicate_id(self):
        rid = uuid4()
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: self.reserve(rid), range(6)))
        self.assertEqual({row["status"] for row in rows}, {"pending"})
        self.assertEqual(self.usage(), (0, 1))
        def attempt(_):
            try:
                self.reserve(uuid4())
                return True
            except q.QuotaDenied:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(6))), 2)
        self.assertEqual(self.usage(), (0, 3))

    def test_concurrent_settlement_charges_once(self):
        rid = uuid4()
        self.reserve(rid, 3)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.settle(rid, True), range(6)))
        self.assertEqual(self.usage(), (3, 0))

    def test_id_mismatch_and_task_unique(self):
        rid, task = uuid4(), uuid4()
        self.reserve(rid, task_id=task)
        with self.assertRaises(ValueError):
            self.reserve(rid, 2, task_id=task)
        with self.assertRaises(sa.exc.IntegrityError):
            self.reserve(uuid4(), task_id=task)
        with self.engine.begin() as conn, self.assertRaises(KeyError):
            q.settle(conn, uuid4(), succeeded=True)
        self.assertEqual(self.usage(), (0, 1))

    def test_rollback_and_settlement_original_day(self):
        rid = uuid4()
        with self.assertRaises(RuntimeError):
            with self.engine.begin() as conn:
                q.reserve(conn, request_id=rid, user_id=self.uid, day=self.day,
                          count=3, policy=self.policy)
                raise RuntimeError("simulate dispatch setup failure")
        self.reserve(rid, 3)
        self.settle(rid, True)  # No current-day lookup; settles reservation's day.
        self.assertEqual(self.usage(), (3, 0))


if __name__ == "__main__":
    unittest.main()
