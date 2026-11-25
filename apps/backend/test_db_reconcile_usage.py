import unittest
from datetime import date
from unittest.mock import Mock
from uuid import uuid4
from db_reconcile_usage import reconcile_usage


class UsageReconciliationTests(unittest.TestCase):
    def fixture(self):
        uid = uuid4()
        report = {'records': {'users': [(None, {'user_id': str(uid)})]},
                  'legacy_records': [('usage', f'usage/{uid}.json', {'value': {'2026-01-02': {'analysis': 3}}})]}
        session = Mock()
        table = 'quota_daily_usage'
        def execute(statement):
            result = Mock()
            result.mappings.return_value.all.return_value = [{
                'user_id': uid, 'date': date(2026,1,2), 'used': 3, 'reserved': 0}]
            return result
        session.execute.side_effect = execute
        return session, report, uid

    def test_matching_utc_usage_has_no_drift(self):
        session, report, _ = self.fixture()
        self.assertEqual(reconcile_usage(session, report)['mismatch_count'], 0)

    def test_usage_drift_reports_only_safe_fields(self):
        session, report, uid = self.fixture()
        def execute(statement):
            result = Mock()
            result.mappings.return_value.all.return_value = [{
                'user_id': uid, 'date': date(2026,1,2), 'used': 7, 'reserved': 1}]
            return result
        session.execute.side_effect = execute
        summary = reconcile_usage(session, report)
        self.assertEqual(summary['mismatches'], {'quota_daily_usage.used': 1, 'quota_daily_usage.reserved': 1})
        self.assertNotIn(str(uid), str(summary))

    def test_unknown_or_noncanonical_day_blocks(self):
        session, report, uid = self.fixture()
        report['legacy_records'][0] = ('usage', f'usage/{uid}.json', {'value': {'2026-1-2': {'analysis': 3}}})
        with self.assertRaises(ValueError):
            reconcile_usage(session, report)

if __name__ == '__main__':
    unittest.main()
