"""Deterministic scheduler lifecycle checks; queue semantics tested in PG."""
from threading import Event
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from db_drain import DrainTracker, DrainingError
from db_original_worker import OriginalGCWorker


class OriginalWorkerTests(TestCase):
    def test_no_implicit_start_bounds_and_stop_before_start(self):
        service, tracker = Mock(), DrainTracker()
        worker = OriginalGCWorker(service, tracker)
        service.collect_originals.assert_not_called()
        self.assertTrue(worker.stop(timeout=0))
        with self.assertRaises(ValueError):
            worker.start()
        for options in ({'interval': float('nan')}, {'interval': True}, {'interval': 0}, {'batch_limit': True}):
            with self.assertRaises(ValueError):
                OriginalGCWorker(service, tracker, **options)
        for timeout in (float('nan'), -1, 61, True):
            with self.assertRaises(ValueError):
                worker.stop(timeout=timeout)

    def test_admitted_thread_stays_counted_until_active_delete_completes(self):
        entered, release = Event(), Event()
        def collect(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic wait timed out')
            return 1
        service, tracker = Mock(), DrainTracker()
        service.collect_originals.side_effect = collect
        worker = OriginalGCWorker(service, tracker, interval=0.01).start()
        try:
            self.assertTrue(entered.wait(3))
            tracker.begin()
            self.assertFalse(worker.stop(timeout=0))
            self.assertEqual(tracker.status()['workers'], 1)
            self.assertFalse(tracker.wait(0))
        finally:
            release.set()
            self.assertTrue(worker.stop(timeout=5))
        self.assertTrue(tracker.wait(1))
        self.assertEqual(service.collect_originals.call_count, 1)
        with self.assertRaises(ValueError):
            worker.start()

    def test_draining_refuses_start_and_database_failure_never_certifies(self):
        service, tracker = Mock(), DrainTracker()
        tracker.begin()
        worker = OriginalGCWorker(service, tracker)
        with self.assertRaises(DrainingError):
            worker.start()
        service.sessions.side_effect = RuntimeError('PRIVATE_DSN')
        report = worker.drain(timeout=0)
        self.assertIsNone(report['pending'])
        self.assertFalse(report['complete'])
        self.assertNotIn('PRIVATE_DSN', str(report))

    def test_last_writer_finishing_during_empty_snapshot_never_certifies(self):
        tracker, service = DrainTracker(), MagicMock()
        finish = tracker.enter('requests')
        tracker.begin()
        def concurrent_commit():
            finish()
            return (0, 0)
        service.sessions.return_value.__enter__.return_value.execute.return_value.one.side_effect = concurrent_commit
        worker = OriginalGCWorker(service, tracker)
        self.assertFalse(worker.report()['complete'])
        service.sessions.return_value.__enter__.return_value.execute.return_value.one.side_effect = None
        service.sessions.return_value.__enter__.return_value.execute.return_value.one.return_value = (1, 1)
        self.assertFalse(worker.report()['complete'])

    def test_thread_launch_failure_releases_activity_and_refuses_restart(self):
        tracker = DrainTracker()
        worker = OriginalGCWorker(Mock(), tracker)
        with patch('db_original_worker.Thread.start', side_effect=RuntimeError('Synthetic thread failure')):
            with self.assertRaises(RuntimeError):
                worker.start()
        self.assertEqual(tracker.status()['workers'], 0)
        self.assertTrue(worker.stop(timeout=0))
        with self.assertRaises(ValueError):
            worker.start()
