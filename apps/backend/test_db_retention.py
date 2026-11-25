"""Offline policy and redaction checks for the SQL retention adapter."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

import sqlalchemy as sa

from db_models import Job, Resume
from db_retention import RetentionError, _archive_payload, _expiry_clause, _validate_policy, purge_expired


class RetentionPolicyTests(TestCase):
    def test_policy_requires_aware_time_and_bounded_values(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.assertEqual(_validate_policy(now, 30, 10)[1], datetime(2026, 9, 17, tzinfo=timezone.utc))
        for value in (datetime(2026, 10, 17), None):
            with self.assertRaises(RetentionError):
                _validate_policy(value, 30, 10)
        for days in (0, -1, 36501, True, 1.0):
            with self.assertRaises(RetentionError):
                _validate_policy(now, days, 10)
        for limit in (0, -1, 1001, True, 1.0):
            with self.assertRaises(RetentionError):
                _validate_policy(now, 30, limit)

    def test_explicit_deadline_precedes_created_at_fallback(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        cutoff = datetime(2026, 9, 17, tzinfo=timezone.utc)
        resume_sql = str(_expiry_clause(Resume.__table__, now=now, cutoff=cutoff))
        job_sql = str(_expiry_clause(Job.__table__, now=now, cutoff=cutoff))
        self.assertIn('retention_until', resume_sql)
        self.assertIn('created_at', resume_sql)
        self.assertNotIn('retention_until', job_sql)
        self.assertIn('created_at', job_sql)

    def test_archive_marker_preserves_existing_payload(self):
        marked = _archive_payload({'payload': {'analysis_snapshot': {'score': 99}, 'source_purged': False}})
        self.assertEqual(marked['analysis_snapshot']['score'], 99)
        self.assertTrue(marked['source_purged'])
        with self.assertRaises(RetentionError):
            _archive_payload({'payload': ['invalid']})
        for empty in ([], ''):
            with self.assertRaises(RetentionError):
                _archive_payload({'payload': empty})

    def test_purge_requires_open_transaction_and_explicit_store(self):
        session = Mock()
        session.in_transaction.return_value = False
        with self.assertRaises(RetentionError):
            purge_expired(session, Mock(), now=datetime.now(timezone.utc), days=30)
        session.in_transaction.return_value = True
        with self.assertRaises(RetentionError):
            purge_expired(session, object(), now=datetime.now(timezone.utc), days=30)


if __name__ == '__main__':
    import unittest
    unittest.main()
