"""History reads and selected legacy contracts in guarded disposable PostgreSQL."""
import ast
from copy import deepcopy
from db_analysis_markdown import _extract_md_block, _normalize_md_for_a4cv
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from flask import Flask, jsonify, request
import sqlalchemy as sa

from db_analysis_history import AnalysisHistoryError, AnalysisHistoryService
from db_analysis_service import write_analysis_batch
from db_auth import AuthService
from db_check_schema import metadata
from db_http_preview import create_preview_app
from db_http_shadow import compare_responses
from db_models import Analysis, AnalysisTask, Job, Resume, User
from db_quota import EffectiveQuota, quota_daily_usage, reserve
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_task_lifecycle import create_task, start_task
from db_test_guard import review_test_url
import resume_review
import resume_sanitize

SHADOW_EVIDENCE = []


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AnalysisHistoryLiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.bootstrap = migration_engine(url.render_as_string(hide_password=False))
        cls.engine, cls.sessions = build_runtime(url.set(username='resume_app', password='review-app-only')
                                               .render_as_string(hide_password=False))
        verify_runtime(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.bootstrap.dispose()

    def setUp(self):
        suffix = uuid4().hex
        self.super_email = suffix + '@example.test'
        self.auth = AuthService(self.sessions, admin_emails=[self.super_email])
        self.users = []
        for role, email in [('owner', ''), ('other', ''), ('admin', ''), ('super', self.super_email)]:
            user, error = self.auth.create_user(role + '-' + suffix, 'SyntheticPassword9', email)
            self.assertIsNone(error)
            self.users.append(user)
        self.owner, self.other, self.admin, self.super = [user['user_id'] for user in self.users]
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.admin).values(is_admin=True))
        self.store = Store(session_factory=self.sessions)
        self.content = '张三\nN\n负责 Java 服务开发\n工作经历\n某公司高级工程师\n教育经历\n本科 复旦大学'
        self.resume = self.store.save_resume(self.content, {}, self.owner)
        self.second_resume = self.store.save_resume('李四\n负责 Go 服务开发', {}, self.owner)
        self.foreign_resume = self.store.save_resume('外部租户正文', {}, self.other)
        self.job = self.store.save_job(None, '合成岗位\nJava 技能', {'job_title': 'Java Engineer'}, self.owner)
        self.report = {'candidate_name': '张三', 'final_score': 88, 'fit_tag': '较匹配', 'fit_grade': 'B',
            'summary': '合成历史结果', 'recruitment_recommendation': '优先面试',
            'strengths': ['负责 Java 服务开发'], 'skill_match': {'hard_skills': ['Java']},
            'education_history': [{'school_name': '复旦大学', 'degree': '本科'}],
            'agent_validation': {'passed': True},
            'evidence_assessments': [{'id': 'r1', 'source_quote': '负责 Java 服务开发', 'status': 'met'}]}
        self.task, self.reservation = uuid4(), uuid4()
        with self.bootstrap.begin() as conn:
            create_task(conn, owner_user_id=UUID(self.owner), job_id=UUID(self.job), total_candidates=2,
                        request_id=str(self.reservation), task_id=self.task)
            reserve(conn, request_id=self.reservation, user_id=UUID(self.owner), task_id=self.task, count=2,
                    day=date(2026, 10, 19), policy=EffectiveQuota(True, 10, False))
            start_task(conn, task_id=self.task, owner_user_id=UUID(self.owner))
            written = write_analysis_batch(conn, task_id=self.task, owner_user_id=UUID(self.owner),
                batch_id='history-fixture', outcomes=[
                    {'resume_id': self.resume, 'status': 'completed', 'result': self.report},
                    {'resume_id': self.second_resume, 'status': 'failed', 'result': {'error_code': 'analysis_unavailable'}},
                ])
        self.analysis = str(next(row['id'] for row in written['analyses'] if row['status'] == 'completed'))
        self.service = AnalysisHistoryService(self.sessions, admin_emails=[self.super_email])
        self.app = create_preview_app(self.store, self.auth, 'synthetic-history-jwt-secret-with-at-least-48-bytes',
            admin_emails=[self.super_email], analysis_history_service=self.service)
        self.client = self.app.test_client()
        self.headers = {}
        for user in self.users:
            response = self.client.post('/api/v1/auth/login', json={
                'username': user['username'], 'password': 'SyntheticPassword9'})
            self.assertEqual(response.status_code, 200)
            self.headers[user['user_id']] = {'Authorization': 'Bearer ' + response.get_json()['data']['token']}

    def read(self, path, owner=None):
        return self.client.get(path, headers=self.headers[owner or self.owner])

    def post(self, kind, body, owner=None):
        return self.client.post('/api/v1/resumes/' + kind, json=body, headers=self.headers[owner or self.owner])

    def business_state(self):
        with self.bootstrap.connect() as conn:
            task = dict(conn.execute(sa.select(AnalysisTask.__table__).where(AnalysisTask.id == self.task)).mappings().one())
            usage = [dict(row) for row in conn.execute(sa.select(quota_daily_usage).where(
                quota_daily_usage.c.user_id == self.owner)).mappings()]
            results = [dict(row) for row in conn.execute(sa.select(Analysis.__table__).where(
                Analysis.task_id == self.task).order_by(Analysis.id)).mappings()]
            return task, usage, results

    def test_user_history_is_tenant_scoped_and_retains_full_committed_evidence(self):
        initial = self.business_state()
        response = self.read('/api/v1/analysis-history?size=1')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual((data['total'], data['pages'], len(data['items'])), (2, 2, 1))
        self.assertNotIn('hr_analysis', data['items'][0])
        empty = self.read('/api/v1/analysis-history?page=10&size=1').get_json()['data']
        self.assertEqual((empty['total'], empty['pages'], empty['items']), (2, 2, []))
        detail = self.read('/api/v1/analysis-history/' + self.analysis).get_json()['data']
        self.assertEqual(detail['hr_analysis'], self.report)
        self.assertEqual(detail['evidence_assessments'], self.report['evidence_assessments'])
        self.assertEqual(self.read('/api/v1/analysis-history/' + self.analysis, self.other).status_code, 404)
        self.assertEqual(self.read('/api/v1/analysis-history?resume_id=' + self.resume, self.other).get_json()['data']['total'], 0)
        selected = self.read('/api/v1/analysis-history?status=completed&task_id=' + str(self.task)).get_json()['data']
        self.assertEqual([item['analysis_id'] for item in selected['items']], [self.analysis])
        self.assertEqual(self.business_state(), initial)

    def test_helpers_never_change_tasks_or_charge_for_viewing(self):
        initial = self.business_state()
        for _ in range(2):
            self.assertEqual(self.post('review-markers', {'resume_id': self.resume,
                'analysis_id': self.analysis}).status_code, 200)
            self.assertEqual(self.post('improved-markdown', {'resume_id': self.resume}).status_code, 200)
        self.assertEqual(self.business_state(), initial)

    def test_review_markers_use_persisted_result_and_reject_foreign_or_mismatched_ids(self):
        initial = self.business_state()
        response = self.post('review-markers', {'resume_id': self.resume, 'analysis_id': self.analysis})
        self.assertEqual(response.status_code, 200)
        annotations = response.get_json()['data']['annotations']
        clean = resume_sanitize.sanitize_resume_content(self.content)
        self.assertTrue(annotations)
        for item in annotations:
            self.assertEqual(clean[item['start']:item['end']], item['quote'])
        for payload, actor in [({'resume_id': self.foreign_resume}, self.owner),
                ({'resume_id': self.resume, 'analysis_id': self.analysis}, self.other),
                ({'resume_id': self.second_resume, 'analysis_id': self.analysis}, self.owner)]:
            self.assertEqual(self.post('review-markers', payload, actor).status_code, 404)
        self.assertEqual(self.business_state(), initial)

    def test_admin_history_requires_current_superadmin_policy_and_summary_is_bounded(self):
        path = '/api/v1/admin/analyses?owner_user_id=' + self.owner
        for actor in [self.owner, self.admin]:
            self.assertEqual(self.read(path, actor).status_code, 403)
            with self.assertRaises(AnalysisHistoryError) as caught:
                self.service.list_admin(actor, password_version=1)
            self.assertEqual(caught.exception.status, 403)
        response = self.read(path, self.super)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual(data['total'], 2)
        for item in data['items']:
            for forbidden in ['hr_analysis', 'raw_content', 'processed_data', 'password_hash', 'legacy_payload']:
                self.assertNotIn(forbidden, item)
        self.assertEqual(self.read(path + '&keyword=%25', self.super).get_json()['data']['total'], 0)
        self.assertEqual(self.read(path + '&keyword=Java', self.super).get_json()['data']['total'], 2)
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.super).values(email='revoked-' + uuid4().hex + '@example.test'))
        self.assertEqual(self.read(path, self.super).status_code, 403)
        with self.assertRaises(AnalysisHistoryError):
            self.service.list_admin(self.super, password_version=1)

    def test_admin_detail_requires_audit_commit_before_response(self):
        path = '/api/v1/admin/analyses/' + self.analysis
        table = metadata().tables['audit_logs']
        def count():
            with self.bootstrap.connect() as conn:
                return conn.scalar(sa.select(sa.func.count()).select_from(table).where(
                    table.c.actor_user_id == self.super, table.c.action == 'analysis.view'))
        self.assertEqual(self.read(path, self.super).status_code, 200)
        self.assertEqual(count(), 1)
        with patch('db_analysis_history.append_audit', side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
            response = self.read(path, self.super)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_DATABASE_URL', response.get_data(as_text=True))
        self.assertNotIn('hr_analysis', response.get_data(as_text=True))
        self.assertEqual(count(), 1)
        def fail_commit(conn):
            raise RuntimeError('synthetic commit failure')
        sa.event.listen(self.engine, 'commit', fail_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, 'synthetic commit failure'):
                self.service.get_admin(self.super, self.analysis, password_version=1,
                                       request_id='history-commit-failure')
        finally:
            sa.event.remove(self.engine, 'commit', fail_commit)
        self.assertEqual(count(), 1)
        with self.bootstrap.connect() as conn:
            row = conn.execute(sa.select(table).where(table.c.actor_user_id == self.super,
                table.c.action == 'analysis.view')).mappings().one()
        self.assertEqual(row['target_id'], self.analysis)
        self.assertNotIn('合成历史结果', str(row['metadata']))

    def test_revoked_password_and_locked_actor_rejected_inside_service(self):
        for update in [{'password_version': 2}, {'password_version': 1, 'status': 'locked'},
                       {'status': 'active', 'deleted_at': datetime.now(timezone.utc)}]:
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(User).where(User.id == self.owner).values(**update))
            self.assertEqual(self.read('/api/v1/analysis-history').status_code, 401)
            with self.assertRaises(AnalysisHistoryError) as caught:
                self.service.get_user(self.owner, self.analysis, password_version=1)
            self.assertEqual(caught.exception.status, 401)

    def test_malformed_filters_and_default_preview_fail_closed(self):
        for query in ['size=101', 'page=0', 'page=1.5', 'status=unknown', 'resume_id=bad', 'task_id=bad']:
            self.assertEqual(self.read('/api/v1/analysis-history?' + query).status_code, 422)
        self.assertEqual(self.read('/api/v1/analysis-history/' + str(uuid4())).status_code, 404)
        self.assertEqual(self.post('review-markers', {'resume_id': 'bad'}).status_code, 422)
        self.assertEqual(self.post('improved-markdown', {'analysis_result': ['invalid']}).status_code, 422)
        default = create_preview_app(self.store, self.auth, 'synthetic-history-jwt-secret-with-at-least-48-bytes').test_client()
        for path in ['/api/v1/analysis-history', '/api/v1/admin/analyses']:
            self.assertEqual(default.get(path, headers=self.headers[self.super]).status_code, 503)
        self.assertEqual(default.post('/api/v1/resumes/review-markers', json={'resume_id': self.resume},
                                     headers=self.headers[self.owner]).status_code, 503)

    def test_deleted_source_does_not_reappear_in_helpers_but_committed_history_is_retained(self):
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(Resume).where(Resume.id == self.resume).values(
                deleted_at=datetime.now(timezone.utc), raw_content='', processed_data={}, legacy_payload={}))
        self.assertEqual(self.post('review-markers', {'resume_id': self.resume}).status_code, 404)
        data = self.post('improved-markdown', {'resume_id': self.resume}).get_json()['data']
        self.assertEqual(data, {'markdown': '', 'source': 'none', 'sections_detected': 0})
        self.assertEqual(self.read('/api/v1/analysis-history/' + self.analysis).get_json()['data']['hr_analysis'], self.report)

    def test_selected_legacy_helper_http_contracts_and_mutation_detection(self):
        source = Path(__file__).with_name('app.py').read_text(encoding='utf8')
        tree = ast.parse(source)
        names = {'review_markers', 'improved_markdown', '_count_sections',
                 '_build_raw_resume_markdown', '_build_fallback_markdown',
                 '_resume_studio_markdown'}
        constants = {'_ALL_HEADING_RE'}
        nodes = [node for node in tree.body if
            (isinstance(node, ast.FunctionDef) and node.name in names) or
            (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets))]
        self.assertEqual(len(nodes), len(names) + len(constants))
        old = Flask('legacy-history-shadow')
        records = {self.resume: {'content': self.content, 'processed': {}}}
        namespace = dict(app=old, request=request, jsonify=jsonify, re=re, _extract_md_block=_extract_md_block,
            _normalize_md_for_a4cv=_normalize_md_for_a4cv,
            resume_review=resume_review, resume_sanitize=resume_sanitize,
            store=SimpleNamespace(get_resume=lambda identity, user_id: deepcopy(records.get(identity))),
            _current_user_id=lambda: self.owner, _request_id=lambda *_: 'legacy:' + uuid4().hex,
            _err=lambda detail, status, *_: (jsonify(detail=detail, request_id='legacy:' + uuid4().hex), status))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'legacy-history-handlers', 'exec'), namespace)
        client = old.test_client()
        cases = [('review-markers', {'resume_id': self.resume, 'analysis': self.report, 'candidate_name': '张三'}),
            ('review-markers', {'resume_id': self.resume}),
            ('review-markers', {}),
            ('improved-markdown', {'resume_id': self.resume}),
            ('improved-markdown', {}),
            ('improved-markdown', {'resume_id': str(uuid4())}),
            ('improved-markdown', {'analysis_result': '```md\n短文本\n```'}),
            ('improved-markdown', {'analysis_result': '```md\n# 张三\n## 项目经验\n' + '负责 Java 开发。' * 40 + '\n```'}),
            ('improved-markdown', {'analysis_result': '```md\n# 短报告\n```\n```markdown\n张三\n**教育经历**\n' + '复旦大学。' * 50 + '\n```'})]
        for processed in [
                {'personal_data': {'firstName': '张三', 'email': 'synthetic@example.test'},
                 'experiences': [{'jobTitle': '工程师', 'company': '合成公司', 'description': ['服务开发']}],
                 'projects': [{'projectName': '合成项目', 'description': '实现审计'}],
                 'education': [{'institution': '复旦大学', 'degree': '本科'}],
                 'skills': [{'skillName': 'Java'}, 'Go'], 'achievements': ['合成证书']},
                {}]:
            records[self.resume]['processed'] = processed
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(Resume).where(Resume.id == self.resume).values(processed_data=processed))
            for kind, body in cases:
                left = client.post('/api/v1/resumes/' + kind, json=body)
                right = self.post(kind, body)
                self.assertTrue(compare_responses(left.status_code, left.get_json(),
                    right.status_code, right.get_json())['equal'], (kind, body.keys()))
        baseline = self.post('review-markers', {'resume_id': self.resume, 'analysis': self.report}).get_json()
        drifted = deepcopy(baseline)
        drifted['data']['summary']['final_score'] = 0
        self.assertFalse(compare_responses(200, baseline, 200, drifted)['equal'])
        SHADOW_EVIDENCE.append(dict(synthetic_cases=len(cases) * 2, differences=0, deliberate_drift_rejected=True,
            scope='selected_legacy_history_helper_handlers_not_full_production_app'))
