import unittest
from unittest.mock import Mock
from db_reconcile_empty import reconcile_empty_tables, EXPECTED_EMPTY


class EmptyTableReconciliationTests(unittest.TestCase):
    def test_empty_tables_match(self):
        session = Mock()
        session.scalar.return_value = 0
        self.assertEqual(reconcile_empty_tables(session,{})['mismatch_count'],0)
        self.assertEqual(session.scalar.call_count,len(EXPECTED_EMPTY))

    def test_runtime_rows_are_never_silently_ignored(self):
        session = Mock()
        def count(statement):
            name = statement.get_final_froms()[0].name
            return {'analysis_tasks':2,'quota_reservations':1,'original_gc':1}.get(name,0)
        session.scalar.side_effect = count
        result = reconcile_empty_tables(session,{})
        self.assertEqual(result['mismatches'],{'analysis_tasks.unexpected_rows':2,'quota_reservations.unexpected_rows':1,
                                             'original_gc.unexpected_rows':1})

if __name__ == '__main__':
    unittest.main()
