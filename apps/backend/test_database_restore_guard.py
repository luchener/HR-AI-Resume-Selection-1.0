"""Restore tools reject connection overrides before creating any engine."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

class RestoreGuardTests(unittest.TestCase):
    def test_untrusted_url_never_opens_connection(self):
        path = Path(__file__).resolve().parents[2] / 'scripts' / 'database-test-verify-restore.py'
        namespace = runpy.run_path(str(path))
        verify = namespace['verify_restore']
        invalid = [
            'postgresql+psycopg://review_bootstrap@127.0.0.1:15439/review_test?host=other&dbname=production',
            'postgresql+psycopg://review_bootstrap@127.0.0.1:15439/review_test?options=-csearch_path=other',
            'postgresql+psycopg://review_bootstrap@127.0.0.1:5432/review_test',
            'postgresql+psycopg://review_bootstrap@remote:15439/review_test',
            'postgresql+psycopg://admin@localhost:15439/review_test',
            'postgresql+psycopg://review_bootstrap@localhost:15439/production',
        ]
        with patch.dict(verify.__globals__, migration_engine=lambda value: self.fail('Connection attempted')):
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    verify(value)

if __name__ == '__main__':
    unittest.main()
