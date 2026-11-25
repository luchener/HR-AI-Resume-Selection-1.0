"""Opt-in actual password migration compatibility and version checks."""
import os
import unittest
from uuid import uuid4
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from db_security import migration_engine
from db_auth import AuthService

@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AuthLiveTests(unittest.TestCase):
    def test_create_auth_change_and_unique(self):
        url = os.environ['DATABASE_TEST_URL']
        if make_url(url).database != 'review_test':
            self.fail('Only review_test allowed')
        engine = migration_engine(url)
        self.addCleanup(engine.dispose)
        service = AuthService(sessionmaker(engine))
        name = 'test_' + uuid4().hex
        created, error = service.create_user(name, 'Password123')
        self.assertIsNone(error)
        self.assertNotIn('password_hash', created)
        self.assertIsNotNone(service.authenticate_user(name.upper(), 'Password123'))
        self.assertIsNone(service.authenticate_user(name, 'wrong'))
        self.assertEqual(service.update_password(created['user_id'], 'Changed456'), (True, None))
        self.assertIsNone(service.authenticate_user(name, 'Password123'))
        self.assertEqual(service.authenticate_user(name, 'Changed456')['password_version'], 2)
        duplicate, error = service.create_user(name.upper(), 'Another123')
        self.assertIsNone(duplicate)
        self.assertIsNotNone(error)

if __name__ == '__main__':
    unittest.main()
