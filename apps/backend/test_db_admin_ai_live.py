'''Real PostgreSQL acceptance for the administrator and AI adapters.

The suite runs only on the disposable review_test database. Fixtures are
written with the migration role, while every service call uses the dedicated
resume_app runtime role.
'''
import os
import unittest
from datetime import datetime, time, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_admin_service import AdminService, AdminServiceError
from db_ai_service import AIService, AIServiceError
from db_analysis_workflow import AnalysisWorkflow
from db_check_schema import metadata
from db_quota import quota_daily_usage, quota_policies, quota_reservations
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_test_guard import review_test_url


REPORT = {
    'candidate_name': 'Synthetic Candidate',
    'final_score': 82,
    'fit_grade': 'B',
    'fit_tag': 'Good match',
    'recruitment_recommendation': 'Interview',
    'summary': 'Synthetic evidence summary',
    'score_breakdown': {'skills_projects': 20},
    'scoring_meta': {'score_semantics': 'resume_jd_evidence_match'},
    'agent_validation': {'passed': True},
    'evidence_assessments': [],
}


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AdminAILiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.admin = migration_engine(url.render_as_string(hide_password=False))
        runtime_url = url.set(username='resume_app', password='review-app-only')
        cls.engine, cls.sessions = build_runtime(runtime_url.render_as_string(hide_password=False))
        if not verify_runtime(cls.engine):
            raise RuntimeError('Runtime role verification failed')
        cls.tables = metadata().tables

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.admin.dispose()

    def _user(self, conn, *, role='user'):
        identity = uuid4()
        username = f'{role}-{identity.hex[:20]}'
        email = f'{username}@example.test'
        conn.execute(self.tables['users'].insert().values(
            id=identity, username=username, email=email,
            password_hash='synthetic', password_salt='synthetic',
            password_version=1, is_admin=role == 'admin',
            is_super_admin=role == 'admin', status='active'))
        return {'id': identity, 'username': username, 'email': email}

    def _job(self, conn, owner):
        identity = uuid4()
        conn.execute(self.tables['jobs'].insert().values(
            id=identity, owner_user_id=owner, title='Synthetic Job',
            raw_content='Synthetic job description',
            parsed_requirements={'skills': ['python']}))
        return identity

    def _resume(self, conn, owner, *, object_key=None, candidate='Synthetic Candidate'):
        identity = uuid4()
        conn.execute(self.tables['resumes'].insert().values(
            id=identity, owner_user_id=owner, candidate_name=candidate,
            source_filename='synthetic.pdf', content_type='pdf',
            content_sha256='0' * 64, object_key=object_key,
            raw_content='Synthetic resume content',
            processed_data={'candidate_name': candidate, 'skills': ['python']},
            parse_status='parsed'))
        return identity

    def _archive(self, conn, owner, resume, *, candidate='Synthetic Candidate'):
        identity = uuid4()
        conn.execute(self.tables['archives'].insert().values(
            id=identity, owner_user_id=owner, resume_id=resume,
            candidate_name=candidate, status='active'))
        return identity

    def _unfinished_task(self, conn, owner, resume, job):
        identity = uuid4()
        conn.execute(self.tables['analysis_tasks'].insert().values(
            id=identity, owner_user_id=owner, job_id=job,
            status='pending', total_candidates=1))
        conn.execute(self.tables['analysis_task_inputs'].insert().values(
            task_id=identity, owner_user_id=owner, password_version=1,
            request_digest='0' * 64,
            snapshot={
                'job': {'job_id': str(job), 'content': 'Synthetic job description',
                        'processed': {'skills': ['python']}},
                'resumes': [{'resume_id': str(resume),
                             'content': 'Synthetic resume content',
                             'processed': {'candidate_name': 'Synthetic Candidate'}}],
            }))
        return identity

    def _legacy_unfinished_task(self, conn, owner, job):
        identity = uuid4()
        conn.execute(self.tables['analysis_tasks'].insert().values(
            id=identity, owner_user_id=owner, job_id=job,
            status='pending', total_candidates=1))
        return identity

    def test_resume_library_resolves_analysis_and_old_text_names_for_search_and_detail(self):
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            analyzed = self._resume(conn, owner['id'], candidate='未提供')
            historical = self._resume(conn, owner['id'], candidate=None)
            confirmed = self._resume(conn, owner['id'], candidate='林知远')
            self._archive(conn, owner['id'], analyzed, candidate='未识别姓名')
            self._archive(conn, owner['id'], confirmed, candidate='误识别姓名')
            conn.execute(self.tables['resumes'].update().where(
                self.tables['resumes'].c.id == historical,
            ).values(raw_content='个人简历\n姓 名\n邵 明\n工作经历'))
            for resume_id, name, offset in [
                (analyzed, '周予安', 0), (analyzed, '\t未提供\n', 1), (confirmed, '误识别姓名', 0),
            ]:
                conn.execute(self.tables['analyses'].insert().values(
                    id=uuid4(), owner_user_id=owner['id'], resume_id=resume_id, job_id=job,
                    status='completed', analysis_result={'candidate_name': name},
                    created_at=datetime.now(timezone.utc) + timedelta(minutes=offset),
                ))

        service = AdminService(self.sessions, admin_emails=[admin['email']])
        for resume, expected in [(analyzed, '周予安'), (historical, '邵明'), (confirmed, '林知远')]:
            with self.subTest(name=expected):
                result = service.list_resumes(
                    admin['id'], password_version=1, keyword=expected, owner_user_id=owner['id'],
                )
                self.assertEqual(result['total'], 1)
                self.assertEqual(result['items'][0]['resume_id'], str(resume))
                self.assertEqual(result['items'][0]['candidate_name'], expected)
                detail = service.get_resume(admin['id'], resume, password_version=1, request_id=str(uuid4()))
                self.assertEqual(detail['candidate_name'], expected)
        last_page = service.list_resumes(
            admin['id'], password_version=1, owner_user_id=owner['id'], page=4, size=1,
        )
        self.assertEqual(last_page['total'], 3)
        self.assertEqual(last_page['items'], [])

    def test_admin_delete_redacts_archive_queues_original_and_audits(self):
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            object_key = f'{uuid4()}.pdf'
            candidate = f'AdminDelete-{uuid4().hex}'
            resume = self._resume(conn, owner['id'], object_key=object_key,
                                  candidate=candidate)
            conn.execute(self.tables['resumes'].update().where(
                self.tables['resumes'].c.id == resume).values(
                legacy_payload={'original': {'ext': 'pdf', 'name': 'fixture.pdf'}}))
            archive = self._archive(conn, owner['id'], resume, candidate=candidate)
            task = self._unfinished_task(conn, owner['id'], resume, job)

        service = AdminService(self.sessions, admin_emails=[admin['email']],
                               original_storage_id=uuid4())
        with self.assertRaises(AdminServiceError) as blocked:
            service.delete_resume(admin['id'], str(resume), password_version=1,
                                  request_id=str(uuid4()))
        self.assertEqual(blocked.exception.status, 409)
        self.assertEqual(service.list_resumes(admin['id'], password_version=1,
                                              keyword=candidate)['total'], 1)

        with self.admin.begin() as conn:
            conn.execute(self.tables['analysis_tasks'].update().where(
                self.tables['analysis_tasks'].c.id == task).values(
                status='completed', completed_candidates=1, finished_at=sa.func.now()))

        request_id = str(uuid4())
        result = service.delete_resume(admin['id'], str(resume), password_version=1,
                                       request_id=request_id)
        self.assertEqual(result['archived_to_trash'], 1)
        self.assertEqual(service.list_resumes(admin['id'], password_version=1,
                                              keyword=candidate)['total'], 0)

        with self.admin.connect() as conn:
            resume_row = conn.execute(sa.select(self.tables['resumes']).where(
                self.tables['resumes'].c.id == resume)).mappings().one()
            archive_row = conn.execute(sa.select(self.tables['archives']).where(
                self.tables['archives'].c.id == archive)).mappings().one()
            gc_row = conn.execute(sa.select(self.tables['original_gc']).where(
                self.tables['original_gc'].c.object_key == object_key)).mappings().one()
            audit = conn.execute(sa.select(self.tables['audit_logs']).where(
                self.tables['audit_logs'].c.action == 'resume.delete',
                self.tables['audit_logs'].c.target_id == str(resume))).mappings().one()

        self.assertIsNotNone(resume_row['deleted_at'])
        self.assertEqual(resume_row['raw_content'], '')
        self.assertEqual(resume_row['processed_data'], {})
        self.assertEqual(resume_row['legacy_payload'], {})
        self.assertIsNone(resume_row['object_key'])
        self.assertIsNone(resume_row['content_sha256'])
        self.assertIsNone(resume_row['source_filename'])
        self.assertIsNone(resume_row['candidate_name'])
        self.assertEqual(archive_row['status'], 'trashed')
        self.assertEqual(gc_row['owner_user_id'], owner['id'])
        self.assertEqual(audit['actor_user_id'], admin['id'])
        self.assertEqual(audit['request_id'], request_id)

        listed = service.list_ops(admin['id'], password_version=1,
                                  op='resume_delete', keyword=str(resume))
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['op'], 'resume_delete')
        self.assertEqual(listed['items'][0]['operator_name'], admin['username'])
        exported = service.export_ops(admin['id'], password_version=1,
                                      op='resume_delete', keyword=str(resume))
        self.assertEqual(exported['total'], 1)

    def test_admin_delete_resume_ignores_unfinished_task_for_other_resume(self):
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            target = self._resume(conn, owner['id'])
            other = self._resume(conn, owner['id'])
            self._unfinished_task(conn, owner['id'], other, job)

        service = AdminService(self.sessions, admin_emails=[admin['email']])
        result = service.delete_resume(admin['id'], str(target), password_version=1,
                                       request_id=str(uuid4()))
        self.assertEqual(result['resume_id'], str(target))
        with self.admin.connect() as conn:
            rows = conn.execute(sa.select(
                self.tables['resumes'].c.id,
                self.tables['resumes'].c.deleted_at,
            ).where(self.tables['resumes'].c.id.in_([target, other]))).mappings().all()
        deleted = {row['id']: row['deleted_at'] for row in rows}
        self.assertIsNotNone(deleted[target])
        self.assertIsNone(deleted[other])

    def test_admin_delete_resume_blocks_legacy_task_without_source_link(self):
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            resume = self._resume(conn, owner['id'])
            self._legacy_unfinished_task(conn, owner['id'], job)

        service = AdminService(self.sessions, admin_emails=[admin['email']])
        with self.assertRaises(AdminServiceError) as blocked:
            service.delete_resume(admin['id'], str(resume), password_version=1,
                                  request_id=str(uuid4()))
        self.assertEqual(blocked.exception.status, 409)
        with self.admin.connect() as conn:
            deleted_at = conn.scalar(sa.select(
                self.tables['resumes'].c.deleted_at
            ).where(self.tables['resumes'].c.id == resume))
        self.assertIsNone(deleted_at)

    def test_usage_is_bucketed_by_utc_day_and_ranked(self):
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=2), today - timedelta(days=1)]
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            resume = self._resume(conn, owner['id'])
            audit_table = self.tables['audit_logs']
            analysis_table = self.tables['analyses']
            for offset in range(2):
                conn.execute(audit_table.insert().values(
                    actor_user_id=owner['id'], actor_identity=str(owner['id']),
                    action='user.login', target_type='user', target_id=str(owner['id']),
                    request_id=str(uuid4()), metadata={},
                    created_at=datetime.combine(days[0], time(23, 30 + offset),
                                                tzinfo=timezone.utc)))
            conn.execute(audit_table.insert().values(
                actor_user_id=owner['id'], actor_identity=str(owner['id']),
                action='user.login', target_type='user', target_id=str(owner['id']),
                request_id=str(uuid4()), metadata={},
                created_at=datetime.combine(days[1], time(0, 10), tzinfo=timezone.utc)))
            for day, count, hour, minute in ((days[0], 3, 23, 40), (days[1], 4, 0, 20)):
                for offset in range(count):
                    created_at = datetime.combine(day, time(hour, minute + offset),
                                                  tzinfo=timezone.utc)
                    conn.execute(analysis_table.insert().values(
                        id=uuid4(), owner_user_id=owner['id'], resume_id=resume,
                        job_id=job, status='completed', analysis_result={},
                        created_at=created_at, updated_at=created_at))

        service = AdminService(self.sessions, admin_emails=[admin['email']])
        usage = service.user_usage(admin['id'], owner['username'], password_version=1,
                                   granularity='day', buckets=3)
        counts = dict(zip(usage['labels'], usage['series']['total']))
        self.assertEqual(counts[days[0].isoformat()], 5)
        self.assertEqual(counts[days[1].isoformat()], 5)
        self.assertEqual(usage['summary']['total'], 10)
        self.assertEqual(usage['summary']['login_total'], 3)
        self.assertEqual(usage['summary']['analysis_total'], 7)
        self.assertEqual(usage['summary']['first_usage'], days[0].isoformat())
        self.assertEqual(usage['summary']['last_usage'], days[1].isoformat())
        ranking = service.usage_ranking(admin['id'], password_version=1, limit=200)
        entry = next(row for row in ranking if row['user_id'] == str(owner['id']))
        self.assertEqual((entry['login'], entry['analysis'], entry['total']), (3, 7, 10))

    def test_ai_improve_uses_durable_task_and_settles_quota_once(self):
        with self.admin.begin() as conn:
            owner = self._user(conn, role='owner')
            job = self._job(conn, owner['id'])
            resume = self._resume(conn, owner['id'])
            conn.execute(pg_insert(quota_policies).values(
                scope_key='global', user_id=None, mode='limited', daily_limit=100,
                enabled=True, quota_enabled=True, email_notify=False
            ).on_conflict_do_update(index_elements=['scope_key'], set_={
                'mode': 'limited', 'daily_limit': 100, 'enabled': True,
                'quota_enabled': True, 'email_notify': False}))
            conn.execute(quota_policies.insert().values(
                scope_key=str(owner['id']), user_id=owner['id'], mode='limited',
                daily_limit=1, enabled=True, email_notify=False))

        analyzer = Mock(return_value=dict(REPORT))
        improver = Mock(return_value='# Synthetic optimized resume\n## Experience\nPython API delivery')
        workflow = AnalysisWorkflow(self.sessions, analyzer, improver=improver, max_workers=1, max_pending=2)
        self.addCleanup(workflow.close)
        service = AIService(self.sessions, model_tester=Mock(), improver=improver,
                            workflow=workflow)
        request_id = str(uuid4())
        first = service.improve(owner['id'], password_version=1, resume_id=str(resume),
            job_id=str(job),
            request_id=request_id)
        second = service.improve(owner['id'], password_version=1, resume_id=str(resume),
            job_id=str(job),
            request_id=request_id)
        self.assertEqual(first['analysis_result'], improver.return_value)
        self.assertEqual(second['analysis_result'], improver.return_value)
        analyzer.assert_not_called()
        self.assertEqual(improver.call_count, 1)

        with self.admin.connect() as conn:
            task = conn.execute(sa.select(self.tables['analysis_tasks']).where(
                self.tables['analysis_tasks'].c.owner_user_id == owner['id'])).mappings().one()
            analyses = conn.execute(sa.select(sa.func.count()).select_from(
                self.tables['analyses']).where(
                self.tables['analyses'].c.owner_user_id == owner['id'])).scalar_one()
            usage = conn.execute(sa.select(quota_daily_usage).where(
                quota_daily_usage.c.user_id == owner['id'])).mappings().one()
            reservation = conn.execute(sa.select(quota_reservations).where(
                quota_reservations.c.task_id == task['id'])).mappings().one()
        self.assertEqual((task['status'], task['completed_candidates'],
                          task['failed_candidates']), ('completed', 1, 0))
        self.assertEqual(analyses, 0)
        self.assertEqual((usage['used'], usage['reserved']), (1, 0))
        self.assertEqual((reservation['count'], reservation['status']), (1, 'succeeded'))

    def test_ai_improve_without_workflow_fails_closed_before_provider(self):
        with self.admin.begin() as conn:
            owner = self._user(conn, role='owner')
        improver = Mock(side_effect=AssertionError('direct provider path must not run'))
        service = AIService(self.sessions, model_tester=Mock(), improver=improver)
        with self.assertRaises(AIServiceError) as unavailable:
            service.improve(owner['id'], password_version=1, resume_id=str(uuid4()),
                            job_id=str(uuid4()), runtime_config={'provider': 'synthetic'},
                            request_id=str(uuid4()))
        self.assertEqual(unavailable.exception.status, 503)
        improver.assert_not_called()
        with self.admin.connect() as conn:
            tasks = conn.execute(sa.select(sa.func.count()).select_from(
                self.tables['analysis_tasks']).where(
                self.tables['analysis_tasks'].c.owner_user_id == owner['id'])).scalar_one()
            reservations = conn.execute(sa.select(sa.func.count()).select_from(
                quota_reservations).where(
                quota_reservations.c.user_id == owner['id'])).scalar_one()
        self.assertEqual((tasks, reservations), (0, 0))

    def test_ai_model_test_checks_current_actor_before_provider(self):
        with self.admin.begin() as conn:
            owner = self._user(conn, role='owner')
        tester = Mock(return_value={'provider': 'synthetic', 'model': 'synthetic-1'})
        service = AIService(self.sessions, model_tester=tester, improver=Mock())
        result = service.test(owner['id'], password_version=1,
                              runtime_config={'provider': 'synthetic', 'model': 'synthetic-1'})
        self.assertTrue(result['ok'])
        self.assertEqual((result['provider'], result['model']),
                         ('synthetic', 'synthetic-1'))
        tester.assert_called_once_with(runtime_config={
            'provider': 'synthetic', 'model': 'synthetic-1'})
        with self.assertRaises(AIServiceError) as revoked:
            service.test(owner['id'], password_version=2,
                         runtime_config={'provider': 'synthetic'})
        self.assertEqual(revoked.exception.status, 401)
        self.assertEqual(tester.call_count, 1)

    def test_admin_requires_current_whitelisted_super_admin(self):
        with self.admin.begin() as conn:
            admin = self._user(conn, role='admin')
            owner = self._user(conn, role='owner')
        service = AdminService(self.sessions, admin_emails=[admin['email']])
        with self.assertRaises(AdminServiceError) as forbidden:
            service.list_ops(owner['id'], password_version=1)
        self.assertEqual(forbidden.exception.status, 403)
        with self.assertRaises(AdminServiceError) as revoked:
            service.list_ops(admin['id'], password_version=2)
        self.assertEqual(revoked.exception.status, 401)


if __name__ == '__main__':
    unittest.main()
