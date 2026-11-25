"""Atomic config+audit service acceptance against disposable PostgreSQL."""
import os
import uuid
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from db_check_schema import metadata
from db_security import migration_engine
from db_config_service import ConfigService, ConfigPermissionError
from db_secrets import ConfigCipher, EncryptedConfigRepository

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ConfigServiceLiveTests(unittest.TestCase):
    def test_commit_authorization_and_audit_failure_rollback(self):
        url = make_url(os.environ['DATABASE_TEST_URL'])
        if url.query or url.host != '127.0.0.1' or url.port != 15439 or url.database != 'review_test':
            self.fail('Only isolated review_test allowed')
        engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(engine.dispose)
        t = metadata().tables
        admin, ordinary = uuid.uuid4(),uuid.uuid4()
        with engine.begin() as conn:
            for uid,flag in ((admin,True),(ordinary,False)):
                conn.execute(t['users'].insert(),{'id':uid,'username':uid.hex,'email':uid.hex+'@example.test','is_admin':flag,'password_hash':'synthetic','password_salt':'synthetic'})
        cipher = ConfigCipher(Fernet.generate_key(),key_id='service-test')
        service = ConfigService(sessionmaker(bind=engine),cipher,validator=lambda document,payload:dict(payload),admin_emails=[admin.hex+'@example.test'])
        first,failed = uuid.uuid4().hex,uuid.uuid4().hex
        service.update(admin,'mail',{'password':'synthetic-secret'},request_id=first,password_version=1)
        with self.assertRaises(ConfigPermissionError):
            service.update(ordinary,'llm',{'api_key':'must-not-save'},request_id='denied',password_version=1)
        with patch('db_config_service.append_audit',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                service.update(admin,'mail',{'password':'replacement'},request_id=failed,password_version=1)
        import system_config
        def product_merge(document, current, incoming):
            if document == 'llm':
                return system_config.merge_llm(current, incoming)
            return system_config.merge_mail(current, incoming)
        merged_service = ConfigService(sessionmaker(bind=engine),cipher,
            validator=lambda document,payload: dict(payload), merger=product_merge,
            admin_emails=[admin.hex+'@example.test'])
        merged_service.update(admin,'llm',{'api_key':'original-model-key','base_url':'https://api.example.test/v1','model':'model-1'},request_id='model-create',password_version=1)
        merged_service.update(admin,'llm',{'api_key':'','model':'model-2'},request_id='model-merge',password_version=1)
        with patch('db_config_service.append_audit',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                merged_service.update(admin,'llm',{'model':'must-rollback'},request_id='model-failed',password_version=1)
        with engine.begin() as conn:
            conn.execute(t['users'].update().where(t['users'].c.id==ordinary).values(is_admin=True))
        with self.assertRaises(ConfigPermissionError):
            merged_service.update(ordinary,'llm',{'model':'forbidden'},request_id='admin-denied',password_version=1)
        with Session(engine) as session, session.begin():
            payload = EncryptedConfigRepository(session,cipher).get()
            self.assertEqual(payload['system_config/mail.json']['password'],'synthetic-secret')
            self.assertEqual(payload['system_config/mail.json']['updated_by'],admin.hex)
            self.assertEqual(payload['system_config/llm.json']['api_key'],'original-model-key')
            self.assertEqual(payload['system_config/llm.json']['model'],'model-2')
            self.assertIsNone(session.scalar(select(t['audit_logs'].c.id).where(t['audit_logs'].c.request_id=='model-failed')))
            events = session.execute(select(t['audit_logs']).where(t['audit_logs'].c.request_id.in_([first,failed,'denied']))).mappings().all()
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['request_id'],first)
            self.assertNotIn('synthetic-secret',str(dict(events[0])))
            envelope = session.execute(select(t['system_configs'].c.value)).scalar_one()
            self.assertNotIn('synthetic-secret',str(envelope))

if __name__ == '__main__':
    unittest.main()
