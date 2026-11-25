import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import sqlalchemy as sa
from db_task_lifecycle import (
    create_task, record_batch, recover_expired_tasks, recover_stale_tasks,
    renew_worker_lease, start_task,
)

class TaskLifecycleUnitTests(unittest.TestCase):
    def test_invalid_transition_and_counts_fail_closed(self):
        conn = MagicMock()
        conn.dialect.name = 'sqlite'
        conn.in_transaction.return_value = False
        with self.assertRaises(RuntimeError):
            create_task(conn, owner_user_id=uuid4(), job_id=None, total_candidates=1)

    def test_mixed_incomplete_batch_does_not_settle_or_finish(self):
        from unittest.mock import patch
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        conn.in_transaction.return_value = True
        row = dict(id=uuid4(), owner_user_id=uuid4(), status='running',
                   completed_candidates=0, failed_candidates=0, total_candidates=10,
                   request_id=str(uuid4()))
        prior = MagicMock()
        prior.mappings.return_value.one_or_none.return_value = None
        conn.execute.side_effect = [prior, MagicMock(), MagicMock()]
        with patch('db_task_lifecycle._task', return_value=row), patch('db_task_lifecycle._pending_task_reservation'), patch('db_task_lifecycle.settle_partial') as settle:
            record_batch(conn, task_id=row['id'], owner_user_id=row['owner_user_id'],
                         batch_id='mixed-first', completed=1, failed=1)
            settle.assert_not_called()
        update = conn.execute.call_args_list[-1].args[0].compile().params
        self.assertEqual(update['status'], 'running')
        self.assertNotIn('finished_at', update)

    def test_recovery_requires_explicit_stopped_worker_confirmation(self):
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        conn.in_transaction.return_value = True
        cutoff = datetime.now(timezone.utc)
        for ids in (None, [], 'all', ['not-a-uuid']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                recover_stale_tasks(conn, before=cutoff, confirmed_stopped_task_ids=ids)
        conn.execute.assert_not_called()

    def test_lease_renewal_requires_a_live_claim_and_strict_duration(self):
        from unittest.mock import patch
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        conn.in_transaction.return_value = True
        task_id, owner, token = uuid4(), uuid4(), uuid4()
        row = {'status': 'running', 'error_code': None}
        result = MagicMock(rowcount=1)
        conn.execute.return_value = result
        with patch('db_task_lifecycle._task', return_value=row):
            self.assertTrue(renew_worker_lease(
                conn, task_id=task_id, owner_user_id=owner,
                worker_token=token, lease_seconds=6))
        update = conn.execute.call_args.args[0]
        compiled = str(update.compile())
        self.assertIn('lease_until', compiled)
        self.assertIn('clock_timestamp', compiled)
        self.assertTrue(any(clause.left.name == 'worker_token'
                            for clause in update._where_criteria))
        for bad in (True, 2.0, '6', None, 0, 3601):
            with self.subTest(lease_seconds=bad), self.assertRaises(ValueError):
                renew_worker_lease(
                    conn, task_id=task_id, owner_user_id=owner,
                    worker_token=token, lease_seconds=bad)

    def test_expired_task_recovery_requires_a_transaction_and_bounded_limit(self):
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        conn.in_transaction.return_value = False
        with self.assertRaises(RuntimeError):
            recover_expired_tasks(conn)
        conn.in_transaction.return_value = True
        for limit in (True, 1.0, '1', 0, 1001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                recover_expired_tasks(conn, limit=limit)

    def test_reservation_validation_does_not_lock_before_usage(self):
        from db_task_lifecycle import _reservation_for_task
        conn = MagicMock()
        owner, task, req = uuid4(), uuid4(), uuid4()
        conn.execute.return_value.mappings.return_value.one_or_none.return_value = dict(task_id=task, user_id=owner)
        _reservation_for_task(conn, task, owner, req)
        statement = conn.execute.call_args.args[0]
        self.assertIsNone(statement._for_update_arg)

    def test_dispatch_requires_matching_pending_reservation(self):
        from db_task_lifecycle import _pending_task_reservation
        conn = MagicMock()
        owner, task, req = uuid4(), uuid4(), uuid4()
        row = dict(id=task, owner_user_id=owner, request_id=str(req), total_candidates=3)
        for count, status in ((2, 'pending'), (3, 'succeeded'), (3, 'failed')):
            conn.execute.return_value.mappings.return_value.one_or_none.return_value = dict(
                task_id=task, user_id=owner, count=count, status=status)
            with self.subTest(count=count, status=status), self.assertRaises(ValueError):
                _pending_task_reservation(conn, row)
        row['request_id'] = None
        with self.assertRaises(ValueError):
            _pending_task_reservation(conn, row)

    def test_recovered_task_rejects_even_previous_batch_replay(self):
        from unittest.mock import patch
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        for error_code in ('operator_stopped_recovery', 'worker_lease_expired'):
            row = dict(status='partial_success', error_code=error_code)
            with self.subTest(error_code=error_code), \
                 patch('db_task_lifecycle._task', return_value=row), \
                 self.assertRaises(ValueError):
                record_batch(conn, task_id=uuid4(), owner_user_id=uuid4(),
                             batch_id='old', completed=1)
            conn.execute.assert_not_called()

    def test_zero_total_creation_is_terminal_without_quota(self):
        from unittest.mock import patch
        conn = MagicMock()
        conn.dialect.name = 'postgresql'
        with patch('db_task_lifecycle.settle_partial') as settle:
            create_task(conn, owner_user_id=uuid4(), job_id=None, total_candidates=0)
            settle.assert_not_called()
        params = conn.execute.call_args.args[0].compile().params
        self.assertEqual(params['status'], 'completed')

    def test_request_constraints_are_in_merged_metadata(self):
        from db_check_schema import metadata
        constraints = {c.name for c in metadata().tables['analysis_tasks'].constraints}
        self.assertIn('uq_analysis_tasks_request_id', constraints)
        self.assertIn('ck_analysis_tasks_request_uuid', constraints)

    def test_transition_matrix_is_explicit(self):
        from db_task_lifecycle import _transition
        _transition('pending','running')
        _transition('running','partial_success')
        for target in ('running', 'completed', 'failed'):
            with self.assertRaises(ValueError):
                _transition('partial_success', target)
        with self.assertRaises(ValueError):
            _transition('completed','running')

if __name__ == '__main__':
    unittest.main()
