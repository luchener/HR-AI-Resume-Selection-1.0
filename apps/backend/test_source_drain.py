"""Cross-process admission, disconnects, crashes and legacy write-source checks."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
import shutil
import subprocess
import sys
from unittest.mock import patch

from source_drain import (AdmissionClosed, Coordinator, DrainMiddleware,
                          DrainUnavailable, activity, start_thread, writer)


def _held_process(path, ready, release):
    c = Coordinator(path)
    lease = c.enter('commands')
    ready.set()
    release.wait(15)
    lease.close()


class SharedDrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'control.sqlite')
        self.c = Coordinator.initialize(self.path)

    def test_refuses_reinitialization_and_missing_registry(self):
        with self.assertRaises(FileExistsError):
            Coordinator.initialize(self.path)
        with self.assertRaises(DrainUnavailable):
            Coordinator(self.path + '-missing').enter('requests')
        self.assertFalse(Path(self.path + '-missing').exists())

    def test_begin_blocks_all_new_roots_and_is_idempotent(self):
        request = self.c.enter('requests')
        first = self.c.begin()
        self.assertEqual(first['epoch'], self.c.begin()['epoch'])
        for kind in ('requests','streams','workers','scheduled','commands'):
            with self.assertRaises(AdmissionClosed):
                Coordinator(self.path).enter(kind)
        self.assertFalse(self.c.wait(0)['admission_drained'])
        request.close()
        request.close()
        self.assertTrue(self.c.wait(1)['admission_drained'])

    def test_preaccepted_request_can_dispatch_children_during_drain(self):
        parent = self.c.enter('requests')
        self.c.begin()
        child = self.c.enter('workers', parent.token)
        parent.close()
        self.assertEqual(self.c.status()['workers'], 1)
        self.assertFalse(self.c.status()['admission_drained'])
        with self.assertRaises(AdmissionClosed):
            self.c.enter('workers', parent.token)
        child.close()
        self.assertTrue(self.c.status()['admission_drained'])

    def test_resume_preserves_outstanding_work_and_changes_epoch(self):
        self.c.enter('workers')
        before = self.c.begin()
        resumed = self.c.resume()
        self.assertGreater(resumed['epoch'], before['epoch'])
        self.assertEqual(resumed['workers'], 1)
        self.assertFalse(resumed['draining'])
        self.assertFalse(resumed['admission_drained'])

    def test_cross_process_writer_is_visible_and_must_finish(self):
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_held_process, args=(self.path, ready, release))
        proc.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertEqual(self.c.begin()['commands'], 1)
            self.assertFalse(self.c.wait(0)['admission_drained'])
            release.set()
            proc.join(10)
            self.assertEqual(proc.exitcode, 0)
            self.assertTrue(self.c.wait(1)['admission_drained'])
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)

    def test_crashed_process_does_not_become_false_success(self):
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_held_process, args=(self.path, ready, release))
        proc.start()
        try:
            self.assertTrue(ready.wait(10))
            proc.terminate()
            proc.join(10)
            self.assertEqual(self.c.begin()['commands'], 1)
            self.assertFalse(self.c.wait(0)['admission_drained'])
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)

    def test_parallel_admission_and_begin_never_lose_accepted_work(self):
        barrier = threading.Barrier(13)
        leases, errors = [], []
        def admit():
            barrier.wait()
            try:
                leases.append(Coordinator(self.path).enter('requests'))
            except AdmissionClosed:
                pass
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=admit) for _ in range(12)]
        for thread in threads:
            thread.start()
        barrier.wait()
        self.c.begin()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.c.status()['requests'], len(leases))
        for lease in leases:
            lease.close()
        self.assertTrue(self.c.status()['admission_drained'])

    def test_disconnect_does_not_release_background_worker(self):
        started, release = threading.Event(), threading.Event()
        parent = self.c.enter('requests')
        def work():
            started.set()
            release.wait(10)
        with parent.activate():
            worker = start_thread(work)
        try:
            self.assertTrue(started.wait(3))
            parent.close()
            report = self.c.begin()
            self.assertEqual((report['requests'],report['workers']), (0,1))
            self.assertFalse(report['admission_drained'])
        finally:
            release.set()
            worker.join(10)
        self.assertTrue(self.c.wait(1)['admission_drained'])

    def test_thread_start_failure_releases_reserved_worker(self):
        parent = self.c.enter('requests')
        with parent.activate(), patch('threading.Thread.start', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                start_thread(lambda: None)
        self.assertEqual(self.c.status()['workers'], 0)
        parent.close()

    def test_scheduler_and_command_decorators_refuse_before_function_body(self):
        calls = []
        @writer('scheduled')
        def scheduled():
            calls.append('scheduled')
        with patch.dict(os.environ, {'SOURCE_DRAIN_STATE_PATH':self.path}):
            scheduled()
            self.c.begin()
            with self.assertRaises(AdmissionClosed):
                scheduled()
        self.assertEqual(calls, ['scheduled'])

    def test_exit_hooks_do_not_clear_live_or_crashed_activities(self):
        token = self.c.process_started('worker')
        self.c.enter('workers')
        self.c.process_exited(token)
        report = self.c.begin()
        self.assertIsNotNone(report['processes'][0]['exited_at'])
        self.assertFalse(report['admission_drained'])
        with self.assertRaises(AdmissionClosed):
            self.c.process_started('worker')

    def test_open_sse_counted_until_iterable_closes_including_lazy_headers(self):
        def app(environ, respond):
            def generate():
                respond('200 OK', [('Content-Type','text/event-stream')])
                yield b'data: started\n\n'
                yield b'data: done\n\n'
            return generate()
        statuses = []
        result = DrainMiddleware(app,self.c)({'PATH_INFO':'/stream'},lambda s,h,e=None:statuses.append(s))
        self.assertEqual(next(result), b'data: started\n\n')
        report = self.c.begin()
        self.assertEqual((report['requests'],report['streams']), (1,1))
        self.assertFalse(report['admission_drained'])
        result.close()
        self.assertTrue(self.c.status()['admission_drained'])
        self.assertIn({'kind':'streams','outcome':'cancelled','count':1}, self.c.status()['outcomes'])

    def test_http_refused_before_app_and_registry_failure_is_503(self):
        calls, statuses = [], []
        def app(environ, respond):
            calls.append(True)
            return []
        self.c.begin()
        middleware = DrainMiddleware(app,self.c)
        self.assertIn(b'draining',b''.join(middleware({'PATH_INFO':'/write'},lambda s,h:statuses.append(s))))
        self.assertEqual(statuses, ['503 Service Unavailable'])
        self.assertEqual(calls, [])
        Path(self.path).unlink()
        middleware({'PATH_INFO':'/write'},lambda s,h:statuses.append(s))
        self.assertEqual(statuses[-1], '503 Service Unavailable')

    def test_wsgi_exception_releases_request(self):
        def app(environ, respond):
            raise RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):
            DrainMiddleware(app,self.c)({},lambda s,h:None)
        self.assertEqual(self.c.status()['requests'], 0)
        self.assertIn({'kind':'requests','outcome':'failed','count':1},self.c.status()['outcomes'])

    def test_completed_and_rejected_responses_have_durable_outcomes(self):
        for status in ('200 OK','422 Unprocessable Entity'):
            def app(environ,respond):
                respond(status,[('Content-Type','application/json')])
                return [b'{}']
            self.assertEqual(b''.join(DrainMiddleware(app,self.c)({},lambda s,h,e=None:None)),b'{}')
        outcomes = self.c.status()['outcomes']
        self.assertIn({'kind':'requests','outcome':'completed','count':1},outcomes)
        self.assertIn({'kind':'requests','outcome':'rejected','count':1},outcomes)

    def test_new_process_cannot_create_source_directories_during_drain(self):
        root = Path(self.tmp.name)/'new-process'
        root.mkdir()
        for filename in ('config.py','source_drain.py'):
            shutil.copyfile(Path(__file__).with_name(filename),root/filename)
        self.c.begin()
        env = {**os.environ,'ENV':'local','SOURCE_DRAIN_STATE_PATH':self.path}
        result = subprocess.run([sys.executable,'-c','import config'],cwd=root,env=env,
                                capture_output=True,encoding='utf-8',timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((root/'data').exists())
        self.assertEqual(self.c.status()['commands'],0)

    def test_iterator_exception_closes_stream_and_request(self):
        def app(environ, respond):
            respond('200 OK',[('Content-Type','text/event-stream')])
            def generate():
                yield b'hello'
                raise RuntimeError('synthetic')
            return generate()
        result = DrainMiddleware(app,self.c)({},lambda s,h,e=None:None)
        next(result)
        with self.assertRaises(RuntimeError):
            next(result)
        self.assertEqual((self.c.status()['requests'],self.c.status()['streams']), (0,0))


class LegacyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['ENV'] = 'local'
        import app
        cls.app_module = app

    def setUp(self):
        from testsupport import isolated_data_dirs
        self.dirs = isolated_data_dirs('source-drain')
        self.addCleanup(self.dirs.restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'control.sqlite')
        self.c = Coordinator.initialize(self.path)
        self.env = patch.dict(os.environ,{'SOURCE_DRAIN_STATE_PATH':self.path})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_real_ping_does_not_run_retention_during_drain(self):
        app = self.app_module.app
        original = app.wsgi_app
        app.wsgi_app = DrainMiddleware(original,self.c)
        self.addCleanup(setattr,app,'wsgi_app',original)
        self.c.begin()
        with patch.object(self.app_module.retention,'maybe_purge') as purge:
            response = app.test_client().get('/ping')
        self.assertEqual(response.status_code,200)
        purge.assert_not_called()
        self.assertEqual(app.test_client().post('/api/v1/jobs/upload',json={}).status_code,503)

    def test_real_retention_and_auth_cleanup_refuse_before_scanning(self):
        import auth, retention
        self.c.begin()
        with patch.object(retention,'scan_expired') as scan:
            with self.assertRaises(AdmissionClosed):
                retention.purge()
            scan.assert_not_called()
        with patch.object(auth,'_prune_due') as due:
            with self.assertRaises(AdmissionClosed):
                auth.maybe_prune()
            due.assert_not_called()

    def test_all_administrative_entrypoints_refuse_before_parsing_or_writing(self):
        import admin_owner_cli, auth_admin_cli, invite_cli, reset_password_cli
        self.c.begin()
        for module in (admin_owner_cli,auth_admin_cli,invite_cli,reset_password_cli):
            with self.subTest(module=module.__name__), patch('argparse.ArgumentParser.parse_args') as parse:
                with self.assertRaises(AdmissionClosed):
                    module.main()
                parse.assert_not_called()

    def test_real_single_sse_disconnect_keeps_worker_counted(self):
        appmod = self.app_module
        started, release = threading.Event(), threading.Event()
        workers = []
        original_start = start_thread
        def capture(*args, **kwargs):
            worker = original_start(*args, **kwargs)
            workers.append(worker)
            return worker
        def analyze(*args, **kwargs):
            started.set()
            release.wait(10)
            return {'synthetic':True}
        parent = self.c.enter('requests')
        try:
            with patch.object(appmod,'_run_hr_analysis',side_effect=analyze), patch.object(appmod.source_drain,'start_thread',side_effect=capture):
                stream = appmod._hr_analysis_stream(['synthetic-resume'],'synthetic-job','synthetic-request','synthetic-user',None,{})
                with parent.activate():
                    next(stream)
                    # One real Agent event makes the generator yield after dispatch.
                    def emitting(*args,**kwargs):
                        kwargs['on_event']({'status':'processing'})
                        return analyze(*args,**kwargs)
                    with patch.object(appmod,'_run_hr_analysis',side_effect=emitting):
                        next(stream)
                    stream.close()
            parent.close()
            self.assertTrue(started.wait(3))
            self.assertEqual(self.c.begin()['workers'],1)
            self.assertFalse(self.c.status()['admission_drained'])
        finally:
            release.set()
            for worker in workers:
                worker.join(10)
        self.assertTrue(self.c.wait(1)['admission_drained'])


if __name__ == '__main__':
    unittest.main()
