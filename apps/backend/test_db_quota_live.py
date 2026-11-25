"""Opt-in real concurrent reservation test on disposable review_test only."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
import uuid
from sqlalchemy import text, select
from sqlalchemy.engine import make_url
from db_security import migration_engine
from db_models import User
from db_quota import reserve, settle, EffectiveQuota, QuotaDenied, quota_daily_usage


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class QuotaConcurrencyTests(unittest.TestCase):
    def test_capacity_and_idempotent_settlement(self):
        url = os.environ['DATABASE_TEST_URL']
        if make_url(url).database != 'review_test':
            self.fail('Only review_test is allowed')
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        uid = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(User.__table__.insert(), {'id': uid, 'username': 'quota-' + uid.hex, 'password_hash':'test','password_salt':'test'})
        barrier = Barrier(2)
        def attempt():
            request_id = uuid.uuid4()
            barrier.wait(timeout=10)
            try:
                with engine.begin() as conn:
                    reserve(conn, request_id=request_id, user_id=uid, count=1, policy=EffectiveQuota(True, 1, False))
                return request_id
            except QuotaDenied:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            accepted = [r for r in (f.result(timeout=20) for f in futures) if r]
        self.assertEqual(len(accepted), 1)
        for outcome in (True, True, False):
            with engine.begin() as conn:
                settle(conn, accepted[0], succeeded=outcome)
        with engine.connect() as conn:
            row = conn.execute(select(quota_daily_usage).where(quota_daily_usage.c.user_id == uid)).mappings().one()
            self.assertEqual((row['used'], row['reserved']), (1, 0))

if __name__ == '__main__':
    unittest.main()
