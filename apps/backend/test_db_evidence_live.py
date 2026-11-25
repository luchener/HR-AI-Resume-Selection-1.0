"""Reject evidence linking two different jobs of the same tenant."""
import os
import unittest
import uuid
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from db_security import migration_engine
from db_check_schema import metadata

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class EvidenceScopeTests(unittest.TestCase):
    def test_same_owner_different_job_rejected(self):
        url = os.environ['DATABASE_TEST_URL']
        if make_url(url).database != 'review_test':
            self.fail('Only review_test allowed')
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        tables = metadata().tables
        uid, rid, j1, j2, aid, req = [uuid.uuid4() for _ in range(6)]
        with engine.begin() as conn:
            conn.execute(tables['users'].insert(), {'id':uid,'username':uid.hex,'password_hash':'test','password_salt':'test'})
            conn.execute(tables['resumes'].insert(), {'id':rid,'owner_user_id':uid})
            for jid in (j1,j2):
                conn.execute(tables['jobs'].insert(), {'id':jid,'owner_user_id':uid})
            conn.execute(tables['analyses'].insert(), {'id':aid,'owner_user_id':uid,'job_id':j1,'resume_id':rid})
            conn.execute(tables['job_requirements'].insert(), {'id':req,'owner_user_id':uid,'job_id':j2,'snapshot_version':'v1','requirement_key':'r','text':'Python'})
        with self.assertRaises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(tables['evidence_assessments'].insert(), {'id':uuid.uuid4(),'owner_user_id':uid,'analysis_id':aid,'requirement_id':req,'job_id':j1,'status':'met','effective_status':'met'})

if __name__ == '__main__':
    unittest.main()
