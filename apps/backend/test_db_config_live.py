"""Real PostgreSQL merge prevents lost concurrent config documents."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db_security import migration_engine
from db_secrets import ConfigCipher, EncryptedConfigRepository

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ConfigConcurrentTests(unittest.TestCase):
    def test_document_merge(self):
        url = os.environ['DATABASE_TEST_URL']
        if make_url(url).database != 'review_test':
            self.fail('Only review_test allowed')
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        cipher = ConfigCipher(Fernet.generate_key(), key_id='concurrency-test')
        with Session(engine) as session, session.begin():
            EncryptedConfigRepository(session, cipher).put({})
        barrier = Barrier(2)
        def write(name):
            barrier.wait(timeout=10)
            with Session(engine) as session, session.begin():
                EncryptedConfigRepository(session,cipher).update_document(name, {'value':name})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, name) for name in ('system_config/mail.json','system_config/llm.json')]
            for result in futures:
                result.result(timeout=20)
        with Session(engine) as session, session.begin():
            payload = EncryptedConfigRepository(session,cipher).get()
            self.assertEqual(set(payload), {'system_config/mail.json','system_config/llm.json'})

if __name__ == '__main__':
    unittest.main()
