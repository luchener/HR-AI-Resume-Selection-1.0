"""Content HTTP/transaction/GC acceptance in guarded disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from uuid import UUID, uuid4
import zipfile

from sqlalchemy import event, func, select, update

from db_auth import AuthService
from db_analysis_workflow import AnalysisWorkflow, WorkflowError
from db_check_schema import metadata
from db_content_service import ContentError, ContentService
from db_http_preview import create_preview_app
from db_models import Analysis, AnalysisTask, Archive, Job, Resume, User
from db_original_gc import original_gc
from db_original_objects import OriginalObjects, STORE_MARKER
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_task_inputs import task_inputs
from db_test_guard import review_test_url

SHADOW_EVIDENCE = []
_TASK_JOB_UNSET = object()


def analysis_report():
    return {'final_score': 82, 'fit_tag': '较匹配', 'fit_grade': 'B',
            'summary': '合成项目经验', 'recruitment_recommendation': '优先面试',
            'score_breakdown': {'skills_projects': 20},
            'agent_validation': {'passed': True}, 'evidence_assessments': []}


def docx(text='Synthetic Candidate'):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
    return stream.getvalue()


class ContentFixture:
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
        # The guarded management runner may provide a workspace-owned fixture
        # root.  Keeping object fixtures outside the subprocess TEMP tree
        # avoids Windows ACL/indexer races while retaining per-test isolation.
        fixture_root = os.environ.get('DB_REVIEW_OBJECTS_ROOT')
        if fixture_root:
            # tempfile uses restrictive Windows ACLs on some mounted
            # workspace directories.  An explicit per-test directory keeps
            # the same isolation while avoiding that platform-specific ACL.
            path = Path(fixture_root) / ('fixture-' + uuid4().hex)
            path.mkdir(parents=False, exist_ok=False)
            self.temp = type('FixtureDirectory', (), {
                'name': str(path),
                'cleanup': lambda instance: shutil.rmtree(path, ignore_errors=True),
            })()
        else:
            self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.objects = OriginalObjects.initialize(self.temp.name, storage_id=uuid4())
        self.original_bytes = docx()
        import parser
        self.service = ContentService(self.sessions, self.objects, extract_text=parser.extract_text_from_file,
                                      summarize_job=parser.summarize_job_locally)
        self.auth = AuthService(self.sessions)
        self.user, error = self.auth.create_user('content-' + uuid4().hex, 'SyntheticPassword9', uuid4().hex + '@example.test')
        self.assertIsNone(error)
        self.other, error = self.auth.create_user('foreign-' + uuid4().hex, 'SyntheticPassword9', uuid4().hex + '@example.test')
        self.assertIsNone(error)
        self.store = Store(session_factory=self.sessions)
        self.secret = 'synthetic-content-jwt-secret-at-least-48-bytes-for-review'
        self.app = create_preview_app(self.store, self.auth, self.secret, content_service=self.service)
        self.client = self.app.test_client()
        response = self.client.post('/api/v1/auth/login', json={'username': self.user['username'], 'password':'SyntheticPassword9'})
        self.assertEqual(response.status_code, 200)
        self.headers = {'Authorization':'Bearer ' + response.get_json()['data']['token']}
        self.args = dict(password_version=1, request_id='content-' + uuid4().hex)
        self.owner = self.user['user_id']
        self.addCleanup(self.clean_queue)

    def clean_queue(self):
        with self.bootstrap.begin() as conn:
            conn.execute(original_gc.delete().where(original_gc.c.owner_user_id == UUID(self.owner)))

    def counts(self, model):
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(model).where(model.owner_user_id == UUID(self.owner)))

    def upload(self):
        response = self.client.post('/api/v1/resumes/upload', data={'file':(BytesIO(self.original_bytes), 'candidate.docx',
            'application/octet-stream')}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.get_json()['resume_id']

    def sources(self):
        rid = self.upload()
        response = self.client.post('/api/v1/jobs/upload', json={'resume_id':rid,
            'job_descriptions':['Engineer\nPython required']}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return rid, response.get_json()['job_id'][0]

    def archive(self, rid, jid):
        return self.service.create_archive(self.owner, {'resume_id':rid, 'job_id':jid,
            'analysis':{'final_score':85, 'summary':'synthetic report'}, 'category':'IT'}, **self.args)[0]

    def add_unfinished_task(self, rid, jid):
        identity = uuid4()
        with self.bootstrap.begin() as conn:
            conn.execute(AnalysisTask.__table__.insert().values(
                id=identity, owner_user_id=UUID(self.owner), job_id=UUID(jid),
                status='running', total_candidates=1))
            conn.execute(task_inputs.insert().values(
                task_id=identity, owner_user_id=UUID(self.owner), password_version=1,
                request_digest='0' * 64,
                snapshot={
                    'job': {'job_id': jid, 'content': 'Synthetic job description',
                            'processed': {'skills': ['python']}},
                    'resumes': [{'resume_id': rid, 'content': 'Synthetic resume content',
                                 'processed': {'candidate_name': 'Synthetic Candidate'}}],
                }))
        return identity

    def add_legacy_unfinished_task(self, jid=None, *, status='running'):
        identity = uuid4()
        with self.bootstrap.begin() as conn:
            conn.execute(AnalysisTask.__table__.insert().values(
                id=identity, owner_user_id=UUID(self.owner),
                job_id=UUID(jid) if jid else None,
                status=status, total_candidates=1))
        return identity

    def add_task_evidence(self, *, task_job_id=_TASK_JOB_UNSET, snapshot=None,
                          analysis=None, status='running',
                          bypass_task_job_scope=False):
        identity = uuid4()
        owner = UUID(self.owner)
        analysis_job_id = UUID(analysis['job_id']) if analysis else None
        if task_job_id is _TASK_JOB_UNSET:
            effective_job_id = analysis_job_id
        else:
            effective_job_id = UUID(task_job_id) if task_job_id is not None else None
        with self.bootstrap.begin() as conn:
            if bypass_task_job_scope:
                conn.exec_driver_sql(
                    'SET LOCAL session_replication_role = replica')
            conn.execute(AnalysisTask.__table__.insert().values(
                id=identity, owner_user_id=owner,
                job_id=effective_job_id,
                status=status, total_candidates=1))
            if snapshot is not None:
                conn.execute(task_inputs.insert().values(
                    task_id=identity, owner_user_id=owner, password_version=1,
                    request_digest='0' * 64, snapshot=snapshot))
            if analysis is not None:
                conn.execute(Analysis.__table__.insert().values(
                    id=uuid4(), task_id=identity, owner_user_id=owner,
                    resume_id=UUID(analysis['resume_id']),
                    job_id=UUID(analysis['job_id']), status='pending'))
        return identity

    def queue(self):
        with self.engine.connect() as conn:
            return conn.execute(select(original_gc).where(original_gc.c.owner_user_id == UUID(self.owner))).mappings().all()

    def files(self):
        return [p for p in Path(self.temp.name).iterdir() if p.name != STORE_MARKER]

    def audit(self):
        table = metadata().tables['audit_logs']
        with self.engine.connect() as conn:
            return conn.execute(select(table).where(table.c.actor_user_id == UUID(self.owner))).mappings().all()


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ContentLiveTests(ContentFixture, TestCase):
    def test_real_docx_upload_preserves_original_and_frontend_contract_without_private_paths(self):
        rid = self.upload()
        rec = self.store.get_resume(rid, self.owner)
        self.assertEqual(rec['content'], 'Synthetic Candidate')
        with self.engine.connect() as conn:
            row = conn.execute(select(Resume.__table__).where(Resume.id == UUID(rid))).mappings().one()
        self.assertEqual(self.objects.read_verified(row['object_key'], rec['original']), self.original_bytes)
        response = self.client.get('/api/v1/resumes?resume_id=' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('original', response.get_json()['data'])
        self.assertNotIn(row['object_key'], response.get_data(as_text=True))
        self.assertEqual(response.headers['X-DB-Preview'], 'content-local')
        self.assertEqual([row['action'] for row in self.audit()], ['resume.create'])

    def test_default_preview_refuses_all_writes_and_does_not_touch_service(self):
        client = create_preview_app(self.store, self.auth, self.secret).test_client()
        for method, path in [('post','/api/v1/resumes/upload'),('post','/api/v1/jobs/upload'),
                ('post','/api/v1/archives'),('patch','/api/v1/archives/' + str(uuid4()) + '/tags'),
                ('delete','/api/v1/archives/trash')]:
            self.assertEqual(getattr(client, method)(path, headers=self.headers).status_code, 503)
        self.assertEqual(self.counts(Resume), 0)

    def test_invalid_types_magic_zip_expansion_and_parser_errors_leave_no_objects_or_rows(self):
        zipped = BytesIO()
        with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', b'X' * (16 * 1024 * 1024 + 1))
        for data, filename in [(b'fake', 'file.pdf'), (b'PKbad', 'file.docx'), (zipped.getvalue(), 'file.docx'),
                               (b'%PDF broken', 'file.pdf')]:
            response = self.client.post('/api/v1/resumes/upload', data={'file':(BytesIO(data), filename)}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('Traceback', response.get_data(as_text=True))
        self.assertEqual(self.counts(Resume), 0)
        self.assertEqual(self.files(), [])

    def test_upload_audit_failure_rolls_back_row_but_keeps_unreferenced_object(self):
        with patch('db_content_service.append_audit', side_effect=RuntimeError('PRIVATE_SQL_OR_SECRET')):
            response = self.client.post('/api/v1/resumes/upload', data={'file':(BytesIO(docx()), 'file.docx')}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_SQL_OR_SECRET', response.get_data(as_text=True))
        self.assertEqual(self.counts(Resume), 0)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.queue(), [])

    def test_revocation_during_parsing_prevents_publish(self):
        original = self.service.extract_text
        def revoke(data, mime):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == UUID(self.owner)).values(password_version=2))
            return original(data, mime)
        self.service.extract_text = revoke
        response = self.client.post('/api/v1/resumes/upload', data={'file':(BytesIO(docx()), 'file.docx')}, headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.counts(Resume), 0)

    def test_job_batch_audit_failure_rolls_back_all_jobs(self):
        rid = self.upload()
        import db_content_service
        real = db_content_service.append_audit
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic audit failure')
            return real(*args, **kwargs)
        with patch('db_content_service.append_audit', side_effect=fail_second):
            response = self.client.post('/api/v1/jobs/upload', json={'resume_id':rid,
                'job_descriptions':['Engineer', 'Designer']}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.counts(Job), 0)
        self.assertEqual([row['action'] for row in self.audit()], ['resume.create'])

    def test_foreign_sources_and_spoofed_owner_never_create_archive_or_jobs(self):
        rid, jid = self.sources()
        for method in ('jobs', 'archive'):
            with self.subTest(method=method), self.assertRaises(ContentError) as error:
                if method == 'jobs':
                    self.service.upload_jobs(self.other['user_id'], rid, ['Engineer'], **self.args)
                else:
                    self.service.create_archive(self.other['user_id'], {'resume_id':rid, 'job_id':jid,
                        'user_id':self.owner, 'analysis':{'final_score':85}}, **self.args)
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.counts(Archive), 0)

    def test_concurrent_archive_create_is_idempotent_including_trash(self):
        rid, jid = self.sources()
        barrier = Barrier(2)
        def create(_):
            barrier.wait(timeout=10)
            return self.service.create_archive(self.owner, {'resume_id':rid, 'job_id':jid}, **self.args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(self.counts(Archive), 1)
        aid = results[0][0]['archive_id']
        self.service.mutate_archive(self.owner, aid, 'trash', **self.args)
        row, created = self.service.create_archive(self.owner, {'resume_id':rid, 'job_id':jid}, **self.args)
        self.assertFalse(created)
        self.assertEqual((row['archive_id'], row['status']), (aid, 'trashed'))
        self.assertEqual(sum(row['action'] == 'archive.create' for row in self.audit()), 1)

    def test_archive_frontend_lifecycle_and_ownership(self):
        rid, jid = self.sources()
        response = self.client.post('/api/v1/archives', json={'resume_id':rid, 'job_id':jid,
            'analysis':{'final_score':82.5, 'capability_score':91, 'gate_blockers':[{'text':'requirement'}]},
            'custom_tags':[' A '], 'category':'IT'}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        data = response.get_json()['data']
        aid = data['archive_id']
        self.assertEqual((data['final_score'], data['custom_tags'], data['category']), (82,['A'],'IT'))
        self.assertEqual(data['analysis_snapshot']['capability_score'], 91)
        self.assertNotIn('analysis', data)
        self.assertEqual(self.client.post('/api/v1/archives', json={'resume_id':rid,'job_id':jid},
            headers=self.headers).status_code, 200)
        path = '/api/v1/archives/' + aid
        self.assertEqual(self.client.patch(path + '/tags', json={'custom_tags':['B']}, headers=self.headers)
                         .get_json()['data']['custom_tags'], ['B'])
        self.assertEqual(self.client.patch(path + '/category', json={'category':'Engineering'}, headers=self.headers)
                         .get_json()['data']['category'], 'Engineering')
        self.assertEqual(self.client.delete('/api/v1/archives/trash/' + aid, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.patch(path + '/tags', json={'custom_tags':[]}, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post(path + '/restore', headers=self.headers).status_code, 200)
        with self.assertRaises(ContentError) as error:
            self.service.mutate_archive(self.other['user_id'], aid, 'trash', **self.args)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.client.delete('/api/v1/archives/trash', headers=self.headers).status_code, 200)
        self.assertIsNotNone(self.store.get_archive(aid, self.owner))
        self.client.delete(path, headers=self.headers)
        self.assertEqual(self.client.delete('/api/v1/archives/trash', headers=self.headers).status_code, 200)
        self.assertIsNone(self.store.get_archive(aid, self.owner))

    def test_archive_update_and_trash_audit_failures_rollback(self):
        rid, jid = self.sources()
        row = self.archive(rid, jid)
        aid = row['archive_id']
        for action, value in [('tags',['changed']), ('category','Changed'), ('trash',None)]:
            with patch('db_content_service.append_audit', side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    self.service.mutate_archive(self.owner, aid, action, value, **self.args)
            self.assertEqual(self.store.get_archive(aid, self.owner), row)

    def test_source_delete_redacts_hides_and_queues_original_without_breaking_archive_fk(self):
        rid, jid = self.sources()
        aid = self.archive(rid, jid)['archive_id']
        self.assertEqual(self.service.delete_source(self.owner, 'resume', rid, **self.args), 1)
        self.assertIsNone(self.store.get_resume_view(rid, self.owner))
        self.assertEqual(self.client.get('/api/v1/resumes?resume_id=' + rid, headers=self.headers).status_code, 404)
        row = self.store.get_archive(aid, self.owner)
        self.assertEqual((row['status'],row['source_purged'],row['resume_id']), ('trashed',True,rid))
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.service.collect_originals(), 1)
        self.assertEqual(self.service.collect_originals(), 0)
        self.assertEqual(self.files(), [])
        self.service.mutate_archive(self.owner, aid, 'restore', **self.args)
        self.assertTrue(self.store.get_archive(aid, self.owner)['source_purged'])
        self.assertEqual(self.service.delete_source(self.owner, 'job', jid, **self.args), 1)
        self.assertIsNone(self.store.get_job_view(jid, self.owner))

    def test_source_delete_audit_failure_rolls_back_content_archive_and_gc(self):
        rid, jid = self.sources()
        aid = self.archive(rid, jid)['archive_id']
        with patch('db_content_service.append_audit', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                self.service.delete_source(self.owner, 'resume', rid, **self.args)
        self.assertEqual(self.store.get_resume(rid, self.owner)['content'], 'Synthetic Candidate')
        self.assertEqual(self.store.get_archive(aid, self.owner)['status'], 'active')
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.service.collect_originals(), 0)

    def test_unfinished_tasks_block_exact_source_delete(self):
        rid, jid = self.sources()
        self.add_unfinished_task(rid, jid)
        for path in ('/api/v1/resumes/' + rid, '/api/v1/jobs/' + jid):
            with self.subTest(path=path):
                response = self.client.delete(path, headers=self.headers)
                self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_resume(rid, self.owner))
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))
        self.assertEqual(self.queue(), [])

    def test_unfinished_tasks_for_other_sources_do_not_block_delete(self):
        rid, jid = self.sources()
        other_rid, other_jid = self.sources()
        self.add_unfinished_task(other_rid, other_jid)
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.store.get_resume(other_rid, self.owner))
        self.assertIsNotNone(self.store.get_job_view(other_jid, self.owner))

    def test_snapshot_resume_scope_blocks_only_linked_resume_delete(self):
        rid, jid = self.sources()
        other_rid, other_jid = self.sources()
        plain_rid = self.upload()
        self.add_task_evidence(snapshot={
            'job': {'job_id': other_jid},
            'resumes': [{'resume_id': rid}],
        })
        response = self.client.delete('/api/v1/resumes/' + plain_rid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_resume(rid, self.owner))

    def test_analysis_resume_scope_blocks_only_linked_resume_delete(self):
        rid, jid = self.sources()
        other_rid, other_jid = self.sources()
        self.add_task_evidence(analysis={'resume_id': rid, 'job_id': other_jid})
        response = self.client.delete('/api/v1/resumes/' + other_rid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_resume(rid, self.owner))

    def test_direct_job_scope_blocks_only_linked_job_delete(self):
        _rid, jid = self.sources()
        _other_rid, other_jid = self.sources()
        self.add_task_evidence(task_job_id=jid)
        response = self.client.delete('/api/v1/jobs/' + other_jid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))

    def test_snapshot_job_scope_blocks_only_linked_job_delete(self):
        _rid, jid = self.sources()
        other_rid, other_jid = self.sources()
        plain_rid = self.upload()
        self.add_task_evidence(snapshot={
            'job': {'job_id': jid},
            'resumes': [{'resume_id': other_rid}],
        })
        response = self.client.delete('/api/v1/jobs/' + other_jid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/resumes/' + plain_rid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))

    def test_analysis_job_scope_blocks_legacy_linked_job_delete(self):
        rid, jid = self.sources()
        _other_rid, other_jid = self.sources()
        self.add_task_evidence(
            analysis={'resume_id': rid, 'job_id': jid},
            bypass_task_job_scope=True)
        response = self.client.delete('/api/v1/jobs/' + other_jid,
                                      headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))

    def test_analysis_job_scope_without_direct_task_job_blocks_target_delete(self):
        rid, jid = self.sources()
        _other_rid, other_jid = self.sources()
        self.add_task_evidence(
            task_job_id=None,
            analysis={'resume_id': rid, 'job_id': jid},
            bypass_task_job_scope=True)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))
        response = self.client.delete('/api/v1/jobs/' + other_jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_job_view(other_jid, self.owner))

    def test_legacy_task_without_resume_scope_blocks_source_delete(self):
        rid, jid = self.sources()
        self.add_legacy_unfinished_task(jid)
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_resume(rid, self.owner))
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))

    def test_legacy_task_without_job_identity_blocks_unrelated_job_delete(self):
        _rid, jid = self.sources()
        self.add_legacy_unfinished_task()
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_job_view(jid, self.owner))

    def test_legacy_task_with_job_identity_does_not_block_unrelated_job_delete(self):
        rid, jid = self.sources()
        _other_rid, other_jid = self.sources()
        self.add_legacy_unfinished_task(other_jid)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_job_view(jid, self.owner))
        # Resume scope remains unknowable, so resume deletion stays fail-closed.
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.get_resume(rid, self.owner))

    def test_terminal_legacy_task_does_not_block_source_delete(self):
        rid, jid = self.sources()
        self.add_legacy_unfinished_task(jid, status='completed')
        response = self.client.delete('/api/v1/resumes/' + rid, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.delete('/api/v1/jobs/' + jid, headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_source_delete_and_task_submit_are_serialized_without_orphan(self):
        rid, jid = self.sources()
        started, release = Event(), Event()

        def analyzer(**_):
            started.set()
            release.wait(timeout=10)
            return analysis_report()

        workflow = AnalysisWorkflow(self.sessions, analyzer, max_workers=1, max_pending=2)
        self.addCleanup(workflow.close)
        barrier = Barrier(2)

        def delete():
            barrier.wait(timeout=10)
            try:
                return ('deleted', self.service.delete_source(
                    self.owner, 'resume', rid, **self.args))
            except ContentError as exc:
                return ('blocked', exc.status)

        def submit():
            barrier.wait(timeout=10)
            try:
                task, future = workflow.submit(
                    owner_user_id=self.owner, password_version=1,
                    request_id=str(uuid4()), job_id=jid, resume_ids=[rid])
                return ('accepted', task, future)
            except WorkflowError as exc:
                return ('rejected', exc.status_code)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                deleted_future = pool.submit(delete)
                submitted_future = pool.submit(submit)
                deleted = deleted_future.result(timeout=15)
                submitted = submitted_future.result(timeout=15)
            if submitted[0] == 'accepted':
                self.assertEqual(deleted, ('blocked', 409))
                self.assertTrue(started.wait(timeout=10))
                _task, future = submitted[1], submitted[2]
                release.set()
                self.assertTrue(future.result(timeout=10))
                self.service.delete_source(self.owner, 'resume', rid, **self.args)
                self.assertIsNone(self.store.get_resume(rid, self.owner))
            else:
                self.assertEqual(submitted, ('rejected', 404))
                self.assertEqual(deleted[0], 'deleted')
                self.assertIsNone(self.store.get_resume(rid, self.owner))
                with self.engine.connect() as conn:
                    self.assertEqual(conn.scalar(
                        select(func.count()).select_from(AnalysisTask).where(
                            AnalysisTask.owner_user_id == UUID(self.owner))), 0)
                    self.assertEqual(conn.scalar(
                        select(func.count()).select_from(task_inputs).where(
                            task_inputs.c.owner_user_id == UUID(self.owner))), 0)
        finally:
            release.set()

    def test_job_source_delete_and_task_submit_are_serialized_without_orphan(self):
        rid, jid = self.sources()
        started, release = Event(), Event()

        def analyzer(**_):
            started.set()
            release.wait(timeout=10)
            return analysis_report()

        workflow = AnalysisWorkflow(self.sessions, analyzer, max_workers=1, max_pending=2)
        self.addCleanup(workflow.close)
        barrier = Barrier(2)

        def delete():
            barrier.wait(timeout=10)
            try:
                return ('deleted', self.service.delete_source(
                    self.owner, 'job', jid, **self.args))
            except ContentError as exc:
                return ('blocked', exc.status)

        def submit():
            barrier.wait(timeout=10)
            try:
                task, future = workflow.submit(
                    owner_user_id=self.owner, password_version=1,
                    request_id=str(uuid4()), job_id=jid, resume_ids=[rid])
                return ('accepted', task, future)
            except WorkflowError as exc:
                return ('rejected', exc.status_code)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                deleted_future = pool.submit(delete)
                submitted_future = pool.submit(submit)
                deleted = deleted_future.result(timeout=15)
                submitted = submitted_future.result(timeout=15)
            if submitted[0] == 'accepted':
                self.assertEqual(deleted, ('blocked', 409))
                self.assertTrue(started.wait(timeout=10))
                _task, future = submitted[1], submitted[2]
                release.set()
                self.assertTrue(future.result(timeout=10))
                self.service.delete_source(self.owner, 'job', jid, **self.args)
                self.assertIsNone(self.store.get_job_view(jid, self.owner))
            else:
                self.assertEqual(submitted, ('rejected', 404))
                self.assertEqual(deleted[0], 'deleted')
                self.assertIsNone(self.store.get_job_view(jid, self.owner))
                with self.engine.connect() as conn:
                    self.assertEqual(conn.scalar(
                        select(func.count()).select_from(AnalysisTask).where(
                            AnalysisTask.owner_user_id == UUID(self.owner))), 0)
                    self.assertEqual(conn.scalar(
                        select(func.count()).select_from(task_inputs).where(
                            task_inputs.c.owner_user_id == UUID(self.owner))), 0)
        finally:
            release.set()

    def test_gc_failure_and_unlink_then_commit_failure_are_replayable(self):
        rid = self.upload()
        self.service.delete_source(self.owner, 'resume', rid, **self.args)
        with patch.object(self.objects, 'remove', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.service.collect_originals()
        self.assertEqual(len(self.queue()), 1)
        session = self.sessions()
        def fail_commit(_):
            raise RuntimeError('synthetic commit failure')
        event.listen(session, 'before_commit', fail_commit)
        with patch.object(self.service, 'sessions', return_value=session):
            with self.assertRaises(RuntimeError):
                self.service.collect_originals(limit=1)
        self.assertEqual(self.files(), [])
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.service.collect_originals(), 1)

    def test_gc_refuses_referenced_objects(self):
        rid = self.upload()
        with self.bootstrap.begin() as conn:
            key = conn.scalar(select(Resume.object_key).where(Resume.id == UUID(rid)))
            conn.execute(original_gc.insert().values(storage_id=self.objects.storage_id,
                object_key=key, owner_user_id=UUID(self.owner)))
        with self.assertRaisesRegex(ValueError, 'still referenced'):
            self.service.collect_originals()
        self.assertTrue((Path(self.temp.name) / key).is_file())
        self.assertEqual(len(self.queue()), 1)

    def test_invalid_body_ids_and_limits_cannot_write(self):
        rid, jid = self.sources()
        for body in ([], {'resume_id':'bad','job_id':jid}, {'resume_id':rid,'job_id':jid,'final_score':True},
                     {'resume_id':rid,'job_id':jid,'custom_tags':[{}]},
                     {'resume_id':rid,'job_id':jid,'analysis':{'work_history':[],'gate_blockers':{ 'x':1 }}}):
            response = self.client.post('/api/v1/archives', json=body, headers=self.headers)
            self.assertIn(response.status_code, (400,422))
        for descriptions in ('text', [], [''], ['a'] * 51, ['a' * 20001], [True]):
            response = self.client.post('/api/v1/jobs/upload', json={'resume_id':rid,'job_descriptions':descriptions},
                headers=self.headers)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.counts(Archive), 0)
        # Enabling uploads must not raise management/auth request limits.
        response = self.client.post('/api/v1/auth/login', json={'username':'x','password':'x' * 17000})
        self.assertEqual(response.status_code, 413)

    def test_restore_purge_and_empty_trash_audit_failures_rollback(self):
        rid, jid = self.sources()
        aid = self.archive(rid, jid)['archive_id']
        self.service.mutate_archive(self.owner, aid, 'trash', **self.args)
        for action in ('restore', 'purge', 'empty'):
            with patch('db_content_service.append_audit', side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    if action == 'empty':
                        self.service.empty_trash(self.owner, **self.args)
                    else:
                        self.service.mutate_archive(self.owner, aid, action, **self.args)
            self.assertEqual(self.store.get_archive(aid, self.owner)['status'], 'trashed')

    def test_source_delete_and_archive_create_serialize_without_active_dangling_archive(self):
        rid, jid = self.sources()
        barrier = Barrier(2)
        def create():
            barrier.wait(timeout=10)
            try:
                self.archive(rid, jid)
                return 'created'
            except ContentError as exc:
                return exc.status
        def delete():
            barrier.wait(timeout=10)
            return self.service.delete_source(self.owner, 'resume', rid, **self.args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            created, deleted = pool.submit(create), pool.submit(delete)
            self.assertIn(created.result(timeout=15), ('created',404))
            self.assertIn(deleted.result(timeout=15), (0,1))
        self.assertIsNone(self.store.get_resume(rid, self.owner))
        self.assertEqual(self.store.list_archives(self.owner), [])

    def test_cleanup_store_identity_isolation_and_two_workers(self):
        rid = self.upload()
        self.service.delete_source(self.owner, 'resume', rid, **self.args)
        wrong_root = TemporaryDirectory()
        self.addCleanup(wrong_root.cleanup)
        wrong = ContentService(self.sessions, OriginalObjects.initialize(wrong_root.name, storage_id=uuid4()),
            extract_text=self.service.extract_text, summarize_job=self.service.summarize_job)
        self.assertEqual(wrong.collect_originals(), 0)
        self.assertEqual(len(self.queue()), 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: self.service.collect_originals(), range(2)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(self.queue(), [])

    def test_terminal_dispatch_source_is_redacted_with_delete_and_preserves_result_state(self):
        from db_task_inputs import task_inputs
        rid, jid = self.sources()
        task = uuid4()
        with self.bootstrap.begin() as conn:
            conn.execute(AnalysisTask.__table__.insert().values(id=task, owner_user_id=UUID(self.owner),
                job_id=UUID(jid), status='completed', total_candidates=1, completed_candidates=1))
            conn.execute(task_inputs.insert().values(task_id=task, owner_user_id=UUID(self.owner),
                password_version=1, request_digest='a'*64, snapshot={
                    'job':{'job_id':jid,'content':'JD text','processed':{'job_title':'Engineer'}},
                    'resumes':[{'resume_id':rid,'content':'resume text','processed':{'name':'Synthetic'}}]}))
        self.service.delete_source(self.owner, 'resume', rid, **self.args)
        self.service.delete_source(self.owner, 'job', jid, **self.args)
        with self.engine.connect() as conn:
            row = conn.execute(select(task_inputs).where(task_inputs.c.task_id == task)).mappings().one()
            self.assertEqual(row['snapshot']['job']['content'], '')
            self.assertEqual(row['snapshot']['resumes'][0]['content'], '')
            self.assertEqual(row['request_digest'], 'a'*64)
            self.assertEqual(conn.scalar(select(AnalysisTask.status).where(AnalysisTask.id == task)), 'completed')

    def test_nonempty_gc_blocks_downgrade_and_constraints_reject_unsafe_keys(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy.exc import IntegrityError
        rid = self.upload()
        self.service.delete_source(self.owner, 'resume', rid, **self.args)
        config = Config(str(Path(__file__).with_name('alembic.ini')))
        config.set_main_option('script_location', str(Path(__file__).with_name('migrations')))
        # Exercise this revision's real guard against the live queue directly.
        # Newer event/binding history may correctly block a full-chain downgrade
        # first; a race in earlier tests must not change which guard is tested.
        migration = ScriptDirectory.from_config(config).get_revision('0012_original_gc').module
        with self.bootstrap.connect() as conn, patch.object(migration.op, 'get_bind', return_value=conn), \
                patch.object(migration.op, 'drop_table') as drop:
            with self.assertRaisesRegex(ValueError, 'Drain original cleanup'):
                migration.downgrade()
            drop.assert_not_called()
        self.assertEqual(len(self.queue()), 1)
        for storage_id, key in [(uuid4(), '../unsafe.pdf'), (UUID(int=0), str(uuid4()) + '.pdf')]:
            with self.assertRaises(IntegrityError), self.bootstrap.begin() as conn:
                conn.execute(original_gc.insert().values(storage_id=storage_id, object_key=key,
                    owner_user_id=UUID(self.owner)))
        with self.bootstrap.connect() as conn:
            from sqlalchemy import text
            self.assertEqual(conn.scalar(text("SELECT tableowner FROM pg_tables WHERE tablename='original_gc'")),
                             'resume_migrator')

    def test_selected_legacy_archive_handlers_match_sql_mutations_and_detect_drift(self):
        import ast
        from datetime import datetime, timezone
        import uuid
        from flask import Flask, jsonify, request
        from db_http_shadow import compare_responses
        from testsupport import isolated_data_dirs
        directories = isolated_data_dirs('content-shadow')
        self.addCleanup(directories.restore)
        import store as legacy
        directories.reapply()
        rid, jid = self.sources()
        row = self.archive(rid, jid)
        aid = row['archive_id']
        legacy._write_json(os.path.join(legacy.ARCHIVES_DIR, aid + '.json'), row)
        legacy._update_archive_index(self.owner, aid, 'add_active')
        names = {'_archive_payload', '_request_id', 'update_archive_tags_api', 'update_archive_category_api',
                 'soft_delete_archive_api', 'restore_archive_api', 'permanent_delete_archive_api',
                 'empty_trash_api', 'get_archive_api', '_err'}
        tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf8'))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(nodes), len(names))
        old = Flask('legacy-content-shadow')
        namespace = dict(app=old, store=legacy, request=request, jsonify=jsonify, uuid=uuid,
                         _current_user_id=lambda: self.owner)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'legacy-content-handlers', 'exec'), namespace)
        client = old.test_client()
        path = '/api/v1/archives/' + aid
        frozen = datetime(2026,10,16,15,0,tzinfo=timezone.utc)
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)
        comparisons = [('get',path,None), ('patch',path+'/tags',{'custom_tags':['Reviewed']}),
            ('patch',path+'/category',{'category':'Engineering'}), ('delete',path,None),
            ('post',path+'/restore',None), ('get',path,None)]
        with patch.object(legacy, 'datetime', FixedDatetime), patch('db_repository.datetime', FixedDatetime):
            for method, url, body in comparisons:
                left = getattr(client, method)(url, json=body)
                right = getattr(self.client, method)(url, json=body, headers=self.headers)
                self.assertEqual(left.status_code, right.status_code, method)
                left_body, right_body = left.get_json(), right.get_json()
                def differences(a, b, path=''):
                    if type(a) is not type(b):
                        return [path + ':type']
                    if isinstance(a, dict):
                        return [path + ':keys'] if set(a) != set(b) else [p for k in a if k != 'request_id'
                            for p in differences(a[k],b[k],path+'.'+k)]
                    return [] if a == b else [path]
                self.assertTrue(compare_responses(left.status_code, left_body,
                    right.status_code, right_body)['equal'], (method, differences(left_body,right_body)))
            left = client.get(path)
            drifted = deepcopy(left.get_json())
            drifted['data']['category'] = 'deliberate drift'
            self.assertFalse(compare_responses(left.status_code,left.get_json(),left.status_code,drifted)['equal'])
            for method, url in [('delete',path), ('delete','/api/v1/archives/trash/'+aid),
                                ('delete','/api/v1/archives/trash')]:
                left = getattr(client,method)(url)
                right = getattr(self.client,method)(url,headers=self.headers)
                self.assertTrue(compare_responses(left.status_code,left.get_json(),right.status_code,right.get_json())['equal'])
        SHADOW_EVIDENCE.append(dict(synthetic_cases=9,differences=0,deliberate_drift_rejected=True,
            scope='selected_archive_handlers_with_synthetic_identity_not_full_app'))
