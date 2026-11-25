import os
import uuid
import unittest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db_security import migration_engine
from db_check_schema import metadata
from db_audit import append_audit

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AuditTransactionTests(unittest.TestCase):
    def test_shared_rollback_and_identity_survives_deletion(self):
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(engine.dispose)
        t = metadata().tables
        actor = uuid.uuid4()
        request = uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(t['users'].insert(), {'id':actor,'username':actor.hex,'password_hash':'synthetic','password_salt':'synthetic'})
        with self.assertRaises(RuntimeError):
            with Session(engine) as session, session.begin():
                session.execute(t['users'].update().where(t['users'].c.id==actor).values(is_admin=True))
                append_audit(session,actor_user_id=actor,action='user.update',target_type='user',target_id=str(actor),request_id=request,changed_fields=['is_admin'])
                raise RuntimeError('rollback')
        with engine.connect() as conn:
            self.assertFalse(conn.scalar(select(t['users'].c.is_admin).where(t['users'].c.id==actor)))
            self.assertIsNone(conn.scalar(select(t['audit_logs'].c.id).where(t['audit_logs'].c.request_id==request)))
        with Session(engine) as session, session.begin():
            append_audit(session,actor_user_id=actor,action='config.mail.update',target_type='config',target_id='mail',request_id=request,changed_fields=['mail'])
        with engine.begin() as conn:
            conn.execute(t['users'].delete().where(t['users'].c.id==actor))
        with engine.connect() as conn:
            row = conn.execute(select(t['audit_logs']).where(t['audit_logs'].c.request_id==request)).mappings().one()
            self.assertIsNone(row['actor_user_id'])
            self.assertEqual(row['actor_identity'],str(actor))
            self.assertEqual(row['metadata']['actor_name'],actor.hex)

if __name__ == '__main__':
    unittest.main()
