"""Opt-in tenant isolation checks against disposable review_test."""
import os
import unittest
import uuid
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from db_security import migration_engine
from db_models import User
from db_repository import ResumeRepository, JobRepository

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class TenantLiveTests(unittest.TestCase):
    def test_tenant_guard_and_database_foreign_key(self):
        url = os.environ['DATABASE_TEST_URL']
        if make_url(url).database != 'review_test':
            self.fail('Only review_test allowed')
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        a, b = uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            for uid in (a, b):
                conn.execute(User.__table__.insert(), {'id': uid, 'username': uid.hex, 'password_hash':'test','password_salt':'test'})
        with Session(engine) as session, session.begin():
            repo = ResumeRepository(session)
            row = repo.insert(a, {'raw_content': "' OR 1=1; --", 'candidate_name':'100%_HR'})
            rid = row['id']
            self.assertIsNone(repo.get(b, rid))
            self.assertIsNone(repo.update(b, rid, {'raw_content': 'hijack'}))
            self.assertEqual(len(repo.list(a, search='%_')), 1)
            self.assertEqual(repo.list(b), [])
        with self.assertRaises(IntegrityError):
            with Session(engine) as session, session.begin():
                JobRepository(session).insert(b, {'source_resume_id': rid, 'raw_content':'wrong owner'})

if __name__ == '__main__':
    unittest.main()
