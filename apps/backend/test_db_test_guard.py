import unittest
from unittest.mock import patch
from db_test_guard import review_test_url


class ReviewDatabaseGuardTests(unittest.TestCase):
    def test_review_url_accepted(self):
        url = review_test_url('postgresql+psycopg://review_bootstrap:synthetic@127.0.0.1:15439/review_test')
        self.assertEqual(url.database,'review_test')

    def test_invalid_routes_never_create_engine(self):
        from test_database_live import LiveDatabaseTests
        base = 'postgresql+psycopg://review_bootstrap:private-password@127.0.0.1:15439/review_test'
        invalid = [base.replace('127.0.0.1','production.test'),base.replace('15439','5432'),
                   base.replace('review_bootstrap','resume_app'),base.replace('review_test','production'),
                   base+'?host=production.test',base+'?user=postgres',base.replace('postgresql+psycopg','sqlite')]
        with patch('test_database_live.migration_engine') as engine:
            for value in invalid:
                with self.subTest(value=value), patch.dict('os.environ',{'DATABASE_TEST_URL':value}):
                    with self.assertRaises(ValueError) as caught:
                        LiveDatabaseTests.setUpClass()
                    self.assertNotIn('private-password',str(caught.exception))
            engine.assert_not_called()

if __name__ == '__main__':
    unittest.main()
