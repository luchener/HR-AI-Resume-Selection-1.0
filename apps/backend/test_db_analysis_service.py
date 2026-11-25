"""Result persistence/settlement acceptance on the guarded disposable database."""
import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Event
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from db_analysis_service import _outcomes, write_analysis_batch
from db_check_schema import metadata
from db_models import Analysis, AnalysisTask
from db_quota import EffectiveQuota, quota_daily_usage, quota_reservations, reserve
from db_security import migration_engine
from db_task_lifecycle import create_task, record_batch, recover_stale_tasks, start_task, task_batches
from db_task_lifecycle import recover_expired_tasks, renew_worker_lease
from db_task_inputs import task_inputs
from db_test_guard import review_test_url


def outcome(resume_id, status='completed', score=82):
    result = {
        'candidate_name': '合成候选人', 'final_score': score, 'fit_tag': '较匹配',
        'recruitment_recommendation': '优先面试', 'summary': '具备岗位相关项目证据。',
        'score_breakdown': {'hard_requirements': 25, 'skills_projects': 20},
        'scoring_meta': {'score_semantics': 'resume_jd_evidence_match'},
        'agent_validation': {'passed': True},
        'evidence_assessments': [{'id': 'req-1', 'status': 'met', 'effective_status': 'met',
                                  'source_quotes': ['合成项目经验']}],
    } if status == 'completed' else {'error_code': 'llm_timeout'}
    return {'resume_id': resume_id, 'status': status, 'result': result}


class ResultValidationTests(unittest.TestCase):
    def test_digest_preserves_content_and_ignores_delivery_order(self):
        values = [outcome(uuid4()), outcome(uuid4(), 'failed')]
        first, digest = _outcomes(values)
        reordered = [dict(reversed(list(item.items()))) for item in reversed(values)]
        self.assertEqual(_outcomes(reordered)[1], digest)
        values[0]['result']['agent_validation']['passed'] = 1
        self.assertNotEqual(_outcomes(values)[1], digest)
        self.assertIs(first[0 if first[0]['status'] == 'completed' else 1]['result']['agent_validation']['passed'], True)

    def test_invalid_results_rejected(self):
        valid = outcome(uuid4())
        invalid = [[], [valid, valid], [{**valid, 'owner_user_id': str(uuid4())}],
                   [{**valid, 'status': 'pending'}]]
        for score in (True, None, '82', float('nan'), float('inf'), -1, 101, 1.234, 10**400):
            invalid.append([outcome(uuid4(), score=score)])
        for field, value in (('summary', '\x00'), ('summary', '\ud800'),
                             ('fit_tag', 'x' * 65), ('score_breakdown', []),
                             ('evidence_assessments', {}), ('unknown', {1: 'coerced-key'}),
                             ('unknown', object())):
            changed = copy.deepcopy(valid)
            changed['result'][field] = value
            invalid.append([changed])
        for values in invalid:
            with self.subTest(values_type=type(values).__name__), self.assertRaises(ValueError):
                _outcomes(values)


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AtomicAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = review_test_url(os.environ['DATABASE_TEST_URL']).render_as_string(hide_password=False)
        cls.engine = migration_engine(cls.url)
        cls.tables = metadata().tables

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.owner, self.other, self.job, self.task, self.request = [uuid4() for _ in range(5)]
        self.resumes = [uuid4() for _ in range(4)]
        with self.engine.begin() as conn:
            for owner in (self.owner, self.other):
                conn.execute(self.tables['users'].insert().values(
                    id=owner, username=owner.hex, password_hash='synthetic', password_salt='synthetic'))
            conn.execute(self.tables['jobs'].insert().values(id=self.job, owner_user_id=self.owner))
            for resume in self.resumes:
                conn.execute(self.tables['resumes'].insert().values(id=resume, owner_user_id=self.owner))
            create_task(conn, owner_user_id=self.owner, job_id=self.job, total_candidates=3,
                        request_id=str(self.request), task_id=self.task)
            reserve(conn, request_id=self.request, user_id=self.owner, task_id=self.task,
                    count=3, day=date(2026, 10, 16), policy=EffectiveQuota(True, 10, False))
            start_task(conn, task_id=self.task, owner_user_id=self.owner)

    def write(self, batch, values, conn=None, owner=None, worker_token=None):
        kwargs = dict(task_id=self.task, owner_user_id=owner or self.owner,
                      batch_id=batch, outcomes=values, worker_token=worker_token)
        if conn is not None:
            return write_analysis_batch(conn, **kwargs)
        with self.engine.begin() as connection:
            return write_analysis_batch(connection, **kwargs)

    def state(self):
        with self.engine.connect() as conn:
            task = conn.execute(sa.select(AnalysisTask.__table__).where(AnalysisTask.id == self.task)).mappings().one()
            usage = conn.execute(sa.select(quota_daily_usage).where(quota_daily_usage.c.user_id == self.owner)).mappings().one()
            reservation = conn.execute(sa.select(quota_reservations).where(quota_reservations.c.request_id == self.request)).mappings().one()
            results = conn.scalar(sa.select(sa.func.count()).select_from(Analysis.__table__).where(Analysis.task_id == self.task))
            batches = conn.scalar(sa.select(sa.func.count()).select_from(task_batches).where(task_batches.c.task_id == self.task))
            return (task['status'], task['completed_candidates'], task['failed_candidates'],
                    usage['used'], usage['reserved'], reservation['status'], results, batches)

    def recover(self, conn=None):
        kwargs = dict(before=datetime.now(timezone.utc) + timedelta(minutes=1),
                      confirmed_stopped_task_ids=[self.task], owner_user_id=self.owner)
        if conn is not None:
            return recover_stale_tasks(conn, **kwargs)
        with self.engine.begin() as connection:
            return recover_stale_tasks(connection, **kwargs)

    def wait_for_task_lock(self, pid):
        deadline = monotonic() + 5
        with self.engine.connect() as conn:
            while monotonic() < deadline:
                # Observe actual server-side blocking, not just a scheduled thread.
                if conn.scalar(sa.text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"), {'pid': pid}):
                    return
                Event().wait(0.01)
        self.fail('concurrent transaction never waited on the held task lock')

    def test_partial_results_projection_and_terminal_charge(self):
        first = [outcome(self.resumes[0]), outcome(self.resumes[1], 'failed')]
        accepted = self.write('first', first)
        self.assertFalse(accepted['duplicate'])
        self.assertEqual(self.state(), ('running', 1, 1, 0, 3, 'pending', 2, 1))
        success = next(row for row in accepted['analyses'] if row['status'] == 'completed')
        self.assertEqual(success['analysis_result'], first[0]['result'])
        self.assertEqual(success['final_score'], 82)
        for field in ('fit_tag', 'summary', 'score_breakdown', 'scoring_meta', 'agent_validation', 'evidence_assessments'):
            self.assertEqual(success[field], first[0]['result'][field])
        self.assertEqual(success['recommendation'], '优先面试')
        self.write('last', [outcome(self.resumes[2])])
        self.assertEqual(self.state(), ('partial_success', 2, 1, 2, 0, 'failed', 3, 2))

    def test_actual_hr_normalizer_output_is_retained_without_losing_evidence(self):
        import app
        raw = {
            'evidence_pipeline_version': 'evidence-v1',
            'evidence_assessments': [{
                'id': 'r1', 'text': 'Redis实践', 'category': 'skill', 'hard': True,
                'logic': 'required', 'status': 'met', 'source_quote': '使用Redis实现缓存',
                'evidence_kind': 'direct', 'reason': '原文明确',
            }],
            'requirements_checklist': [], 'job_fit_score': 0,
            'agent_meta': {'validation_passed': True},
            'agent_validation': {'passed': True, 'issues': []},
        }
        with patch.object(app.config, 'DECEPTION_CHECK_MODE', 'off'):
            normalized = app._normalize_hr_analysis(raw, '要求Redis实践', '使用Redis实现缓存')
        accepted = self.write('normalized-report', [{
            'resume_id': self.resumes[0], 'status': 'completed', 'result': normalized,
        }])
        stored = accepted['analyses'][0]
        self.assertEqual(stored['analysis_result'], normalized)
        self.assertEqual(stored['final_score'], normalized['final_score'])
        for field in ('fit_grade', 'fit_tag', 'summary', 'score_breakdown',
                      'scoring_meta', 'agent_validation', 'evidence_assessments'):
            self.assertEqual(stored[field], normalized[field])
        self.assertEqual(stored['scoring_meta']['score_source'], 'server_deterministic_evidence')
        self.assertEqual(self.state(), ('running', 1, 0, 0, 3, 'pending', 1, 1))

    def test_all_successes_are_charged(self):
        self.write('all', [outcome(resume) for resume in self.resumes[:3]])
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_all_failures_release_without_charge(self):
        self.write('all', [outcome(resume, 'failed') for resume in self.resumes[:3]])
        self.assertEqual(self.state(), ('failed', 0, 3, 0, 0, 'failed', 3, 1))

    def test_expired_worker_lease_fences_late_result_and_settles_reservation(self):
        token = uuid4()
        with self.engine.begin() as conn:
            conn.execute(task_inputs.insert().values(
                task_id=self.task, owner_user_id=self.owner, password_version=1,
                request_digest='a' * 64,
                snapshot={'resumes': [{'resume_id': str(value)} for value in self.resumes[:3]]},
                worker_token=token,
                lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'"),
            ))
            self.assertEqual(recover_expired_tasks(
                conn, owner_user_id=self.owner), 1)

        with self.assertRaisesRegex(ValueError, 'fenced by operator recovery'):
            self.write('late-after-lease-expiry', [outcome(self.resumes[0])],
                       owner=self.owner)
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(AnalysisTask.status, AnalysisTask.error_code).where(
                AnalysisTask.id == self.task)).one()
            self.assertEqual(tuple(row), ('failed', 'worker_lease_expired'))
        self.assertEqual(self.state(), ('failed', 0, 3, 0, 0, 'failed', 0, 0))

    def install_lease(self, *, expired=False):
        token = uuid4()
        with self.engine.begin() as conn:
            conn.execute(task_inputs.insert().values(
                task_id=self.task, owner_user_id=self.owner, password_version=1,
                request_digest='b' * 64,
                snapshot={'resumes': [{'resume_id': str(value)} for value in self.resumes[:3]]},
                worker_token=token,
                lease_until=sa.func.clock_timestamp() + sa.text(
                    "INTERVAL '-1 second'" if expired else "INTERVAL '120 seconds'"),
            ))
        return token

    def expire_lease(self):
        with self.engine.begin() as conn:
            conn.execute(task_inputs.update().where(task_inputs.c.task_id == self.task).values(
                lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")))

    def test_expired_lease_rejects_results_before_reaper_and_cannot_renew(self):
        token = self.install_lease(expired=True)
        with self.engine.begin() as conn:
            self.assertFalse(renew_worker_lease(conn, task_id=self.task,
                owner_user_id=self.owner, worker_token=token))
        with self.assertRaisesRegex(ValueError, 'lease has expired'):
            self.write('expired', [outcome(self.resumes[0])], worker_token=token)
        self.assertEqual(self.state(), ('running', 0, 0, 0, 3, 'pending', 0, 0))

    def test_live_lease_requires_matching_owner_and_token(self):
        token = self.install_lease()
        with self.engine.begin() as conn:
            for owner, claim in ((self.other, token), (self.owner, uuid4())):
                self.assertFalse(renew_worker_lease(conn, task_id=self.task,
                    owner_user_id=owner, worker_token=claim))
            self.assertTrue(renew_worker_lease(conn, task_id=self.task,
                owner_user_id=self.owner, worker_token=token))
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 0)
        with self.assertRaisesRegex(ValueError, 'does not own'):
            self.write('wrong-token', [outcome(self.resumes[0])], worker_token=uuid4())
        values = [outcome(value) for value in self.resumes[:3]]
        self.write('valid', values, worker_token=token)
        with self.engine.connect() as conn:
            self.assertIsNone(conn.scalar(sa.select(task_inputs.c.lease_until).where(
                task_inputs.c.task_id == self.task)))
        self.assertTrue(self.write('valid', values, worker_token=token)['duplicate'])
        with self.assertRaisesRegex(ValueError, 'already terminal'):
            self.write('new-terminal-batch', [outcome(self.resumes[0])], worker_token=token)
        with self.engine.begin() as conn:
            self.assertFalse(renew_worker_lease(conn, task_id=self.task,
                owner_user_id=self.owner, worker_token=token))
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 0)
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_lease_recovery_charges_committed_successes_and_rolls_back_atomically(self):
        token = self.install_lease()
        self.write('progress', [outcome(self.resumes[0]), outcome(self.resumes[1], 'failed')],
                   worker_token=token)
        self.expire_lease()
        with self.assertRaises(RuntimeError), self.engine.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 1)
            raise RuntimeError('abort recovery')
        self.assertEqual(self.state(), ('running', 1, 1, 0, 3, 'pending', 2, 1))
        with self.engine.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 1)
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 0)
        self.assertEqual(self.state(), ('partial_success', 1, 2, 1, 0, 'failed', 2, 1))
        with self.assertRaisesRegex(ValueError, 'fenced'):
            self.write('progress', [outcome(self.resumes[0]), outcome(self.resumes[1], 'failed')],
                       worker_token=token)

    def test_reaper_excludes_pending_legacy_and_unleased_tasks_and_other_owners(self):
        with self.engine.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 0)
        self.install_lease(expired=True)
        for status, token, deadline in (
                ('pending', None, sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")),
                ('running', None, sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'")),
                ('running', uuid4(), None)):
            with self.engine.begin() as conn:
                conn.execute(AnalysisTask.__table__.update().where(AnalysisTask.id == self.task).values(status=status))
                conn.execute(task_inputs.update().where(task_inputs.c.task_id == self.task).values(
                    worker_token=token, lease_until=deadline))
                self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 0)
        self.expire_lease()
        with self.engine.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.other), 0)
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 1)

    def test_concurrent_reapers_settle_expired_claim_exactly_once(self):
        self.install_lease(expired=True)
        barrier = Barrier(4)
        def recover():
            barrier.wait(timeout=10)
            with self.engine.begin() as conn:
                return recover_expired_tasks(conn, owner_user_id=self.owner)
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(lambda _: recover(), range(4)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(self.state(), ('failed', 0, 3, 0, 0, 'failed', 0, 0))

    def test_reaper_skips_locked_task_and_retries_after_release(self):
        self.install_lease(expired=True)
        with self.engine.begin() as writer:
            writer.execute(sa.select(AnalysisTask.id).where(
                AnalysisTask.id == self.task).with_for_update()).one()
            with ThreadPoolExecutor(max_workers=1) as pool:
                def recover():
                    with self.engine.begin() as conn:
                        return recover_expired_tasks(conn, owner_user_id=self.owner)
                self.assertEqual(pool.submit(recover).result(timeout=5), 0)
        with self.engine.begin() as conn:
            self.assertEqual(recover_expired_tasks(conn, owner_user_id=self.owner), 1)
        self.assertEqual(self.state(), ('failed', 0, 3, 0, 0, 'failed', 0, 0))

    def test_replay_does_not_replace_rows_or_charge_twice(self):
        values = [outcome(resume) for resume in self.resumes[:3]]
        first = self.write('all', values)
        repeated = self.write('all', list(reversed(values)))
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(first['analyses'], repeated['analyses'])
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))
        changed = copy.deepcopy(values)
        changed[0]['result']['final_score'] = 81
        for invalid in (changed, [outcome(resume) for resume in self.resumes[1:4]]):
            with self.assertRaisesRegex(ValueError, 'different result content'):
                self.write('all', invalid)
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_candidate_repeated_in_different_batch_rolls_back(self):
        self.write('first', [outcome(self.resumes[0])])
        original = self.state()
        with self.engine.begin() as conn:
            with self.assertRaises(sa.exc.IntegrityError):
                self.write('second', [outcome(self.resumes[1]), outcome(self.resumes[0])], conn)
            self.assertEqual(conn.scalar(sa.text('SELECT 1')), 1)
        self.assertEqual(self.state(), original)

    def test_foreign_candidate_rolls_back_settlement_even_if_caller_commits(self):
        foreign = uuid4()
        outer_email = self.owner.hex + '@example.test'
        with self.engine.begin() as conn:
            conn.execute(self.tables['resumes'].insert().values(id=foreign, owner_user_id=self.other))
        original = self.state()
        with self.engine.begin() as conn:
            with self.assertRaises(sa.exc.IntegrityError):
                self.write('invalid', [outcome(self.resumes[0]), outcome(self.resumes[1]), outcome(foreign)], conn)
            conn.execute(self.tables['users'].update().where(self.tables['users'].c.id == self.owner).values(email=outer_email))
        self.assertEqual(self.state(), original)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(self.tables['users'].c.email).where(self.tables['users'].c.id == self.owner)), outer_email)

    def test_failure_after_first_result_insert_rolls_back_entire_batch(self):
        original = self.state()
        seen = []
        def interrupt(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO analyses '):
                seen.append(statement)
                if len(seen) == 2:
                    raise RuntimeError('simulated interruption during result persistence')
        sa.event.listen(self.engine, 'before_cursor_execute', interrupt)
        try:
            with self.engine.begin() as conn:
                with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                    self.write('interrupted', [outcome(resume) for resume in self.resumes[:3]], conn)
                self.assertEqual(conn.scalar(sa.text('SELECT 1')), 1)
        finally:
            sa.event.remove(self.engine, 'before_cursor_execute', interrupt)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.state(), original)
        self.write('interrupted', [outcome(resume) for resume in self.resumes[:3]])
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_outer_rollback_removes_results_progress_and_quota_charge(self):
        original = self.state()
        with self.assertRaisesRegex(RuntimeError, 'before commit'):
            with self.engine.begin() as conn:
                self.write('rolled-back', [outcome(resume) for resume in self.resumes[:3]], conn)
                raise RuntimeError('process failed before commit')
        self.assertEqual(self.state(), original)

    def test_failed_settlement_prevents_result_persistence(self):
        original = self.state()
        with patch('db_task_lifecycle.settle_partial', side_effect=RuntimeError('settlement unavailable')):
            with self.engine.begin() as conn:
                with self.assertRaisesRegex(RuntimeError, 'settlement unavailable'):
                    self.write('failed-settlement', [outcome(resume) for resume in self.resumes[:3]], conn)
        self.assertEqual(self.state(), original)

    def test_foreign_task_and_excess_results_are_rejected(self):
        original = self.state()
        with self.assertRaises(KeyError):
            self.write('foreign', [outcome(self.resumes[0])], owner=self.other)
        with self.assertRaisesRegex(ValueError, 'exceeds total_candidates'):
            self.write('too-many', [outcome(resume) for resume in self.resumes])
        self.assertEqual(self.state(), original)

    def test_count_only_batches_and_existing_result_drift_fail_closed(self):
        with self.engine.begin() as conn:
            record_batch(conn, task_id=self.task, owner_user_id=self.owner, batch_id='legacy', completed=1)
        original = self.state()
        for batch, message in (('legacy', 'count-only'), ('new', 'inconsistent')):
            with self.assertRaisesRegex(ValueError, message):
                self.write(batch, [outcome(self.resumes[0])])
        self.assertEqual(self.state(), original)

    def test_duplicate_checks_stored_content_and_recovery_fence(self):
        values = [outcome(self.resumes[0])]
        self.write('first', values)
        with self.engine.begin() as conn:
            conn.execute(Analysis.__table__.update().where(Analysis.task_id == self.task).values(analysis_result={'changed': True}))
        with self.assertRaisesRegex(ValueError, 'content is inconsistent'):
            self.write('first', values)
        self.assertEqual(self.recover(), 1)
        self.assertEqual(self.state(), ('partial_success', 1, 2, 1, 0, 'failed', 1, 1))
        for batch, payload in (('first', values), ('late', [outcome(self.resumes[1])])):
            with self.assertRaisesRegex(ValueError, 'fenced'):
                self.write(batch, payload)
        self.assertEqual(self.recover(), 0)

    def test_concurrent_identical_callbacks_accept_once(self):
        barrier = Barrier(4)
        values = [outcome(resume) for resume in self.resumes[:3]]
        def attempt():
            barrier.wait(timeout=10)
            return self.write('concurrent', values)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(attempt) for _ in range(4)]
            responses = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(not response['duplicate'] for response in responses), 1)
        self.assertTrue(all(response['analyses'] == responses[0]['analyses'] for response in responses))
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_concurrent_changed_callbacks_reject_one(self):
        barrier = Barrier(2)
        def attempt(score):
            barrier.wait(timeout=10)
            try:
                return self.write('same-id', [outcome(self.resumes[0], score=score)])
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(attempt, (81, 82)))
        self.assertEqual(sum(response is not None for response in responses), 1)
        self.assertEqual(self.state(), ('running', 1, 0, 0, 3, 'pending', 1, 1))

    def test_concurrent_disjoint_batches_settle_once(self):
        barrier = Barrier(3)
        def attempt(index):
            barrier.wait(timeout=10)
            return self.write(f'candidate-{index}', [outcome(self.resumes[index])])
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(attempt, range(3)))
        self.assertEqual(sum(response['task']['status'] == 'completed' for response in responses), 1)
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 3))

    def test_app_role_can_write_results_and_settle_without_elevated_access(self):
        from db_runtime import build_runtime, verify_runtime
        url = review_test_url(self.url).set(username='resume_app', password='review-app-only')
        app, _ = build_runtime(url.render_as_string(hide_password=False))
        self.addCleanup(app.dispose)
        self.assertTrue(verify_runtime(app))
        with app.begin() as conn:
            self.write('runtime', [outcome(resume) for resume in self.resumes[:3]], conn)
        self.assertEqual(self.state(), ('completed', 3, 0, 3, 0, 'succeeded', 3, 1))

    def test_recovery_waits_for_inflight_result_and_charges_it(self):
        writer_holds_lock, release_writer, recovery_started = Event(), Event(), Event()
        recovery_pid = []
        def paused_record(*args, **kwargs):
            result = record_batch(*args, **kwargs)
            writer_holds_lock.set()
            if not release_writer.wait(timeout=10):
                raise RuntimeError('writer was not released')
            return result
        def recovery():
            with self.engine.begin() as conn:
                recovery_pid.append(conn.scalar(sa.text('SELECT pg_backend_pid()')))
                recovery_started.set()
                return self.recover(conn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch('db_analysis_service.record_batch', side_effect=paused_record):
                writer = pool.submit(self.write, 'inflight', [outcome(self.resumes[0])])
                try:
                    self.assertTrue(writer_holds_lock.wait(timeout=10))
                    recovering = pool.submit(recovery)
                    self.assertTrue(recovery_started.wait(timeout=10))
                    self.wait_for_task_lock(recovery_pid[0])
                finally:
                    release_writer.set()
                writer.result(timeout=20)
                self.assertEqual(recovering.result(timeout=20), 1)
        self.assertEqual(self.state(), ('partial_success', 1, 2, 1, 0, 'failed', 1, 1))

    def test_result_waits_for_recovery_commit_and_is_rejected(self):
        recovery_holds_lock, release_recovery, writer_started = Event(), Event(), Event()
        writer_pid = []
        def recovery():
            with self.engine.begin() as conn:
                result = self.recover(conn)
                recovery_holds_lock.set()
                if not release_recovery.wait(timeout=10):
                    raise RuntimeError('recovery was not released')
                return result
        def writer():
            with self.engine.begin() as conn:
                writer_pid.append(conn.scalar(sa.text('SELECT pg_backend_pid()')))
                writer_started.set()
                return self.write('late', [outcome(self.resumes[0])], conn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovering = pool.submit(recovery)
            try:
                self.assertTrue(recovery_holds_lock.wait(timeout=10))
                writing = pool.submit(writer)
                self.assertTrue(writer_started.wait(timeout=10))
                self.wait_for_task_lock(writer_pid[0])
            finally:
                release_recovery.set()
            self.assertEqual(recovering.result(timeout=20), 1)
            with self.assertRaisesRegex(ValueError, 'fenced'):
                writing.result(timeout=20)
        self.assertEqual(self.state(), ('failed', 0, 3, 0, 0, 'failed', 0, 0))


if __name__ == '__main__':
    unittest.main()
