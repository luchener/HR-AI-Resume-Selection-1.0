"""Original access/audit and GC drain acceptance on real disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import os
from threading import Event
from unittest import TestCase, skipUnless
from unittest.mock import patch
from urllib.parse import quote
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from db_content_service import ContentError
from db_drain import DrainTracker
from db_http_preview import create_preview_app
from db_models import Resume, User
from db_original_gc import original_gc
from db_original_render import inspect_pdf
from db_original_view import OriginalViewService
from db_original_worker import OriginalGCWorker
from test_db_content_live import ContentFixture
from test_db_original_render import pdf


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class OriginalLiveTests(ContentFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admins = (self.other['email'],)
        self.viewer = OriginalViewService(self.sessions, self.objects, admin_emails=self.admins)
        self.tracker = DrainTracker()
        self.app = create_preview_app(self.store, self.auth, self.secret, admin_emails=self.admins,
            content_service=self.service, original_service=self.viewer, activity_tracker=self.tracker)
        self.client = self.app.test_client()
        response = self.client.post('/api/v1/auth/login', json={'username': self.other['username'],
                                                             'password': 'SyntheticPassword9'})
        self.admin_headers = {'Authorization': 'Bearer ' + response.get_json()['data']['token']}

    def path(self, rid, page=None):
        return '/api/v1/admin/resumes/' + rid + '/original' + ('' if page is None else '/pages/' + str(page))

    def seed_pdf(self):
        # Use real bytes-only renderer here; extraction was covered separately.
        self.service.extract_text = lambda data, mime: 'Synthetic PDF Candidate'
        return self.service.upload_resume(self.owner, pdf(), 'candidate.pdf', **self.args)['resume_id']

    def view(self, rid, **kwargs):
        return self.viewer.view(self.other['user_id'], rid, **self.args, **kwargs)

    def original_audits(self):
        from db_check_schema import metadata
        table = metadata().tables['audit_logs']
        with self.engine.connect() as conn:
            return conn.execute(select(table).where(table.c.actor_user_id == UUID(self.other['user_id']))
                .order_by(table.c.id)).mappings().all()

    def test_metadata_docx_and_every_page_access_commit_distinct_safe_audits(self):
        rid = self.seed_pdf()
        info = self.client.get(self.path(rid), headers=self.admin_headers)
        self.assertEqual(info.status_code, 200)
        meta = info.get_json()['data']
        self.assertTrue(meta['available'] and meta['renderable'])
        self.assertEqual(meta['page_count'], 1)
        self.assertNotIn('storage_id', meta)
        for _ in range(2):
            response = self.client.get(self.path(rid, 1), headers={**self.admin_headers, 'Range': 'bytes=0-5'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/png')
            self.assertTrue(response.data.startswith(b'\x89PNG'))
            expected = {'Content-Disposition': 'inline', 'Cache-Control': 'private, no-store, max-age=0',
                'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'none',
                'X-Original-Page': '1', 'X-Original-Pages': '1'}
            for key, value in expected.items():
                self.assertEqual(response.headers[key], value)
            response.close()
        rows = self.original_audits()
        self.assertEqual([r['action'] for r in rows], ['resume.original_info', 'resume.original_page', 'resume.original_page'])
        self.assertEqual([r['metadata'].get('page') for r in rows], [None, 1, 1])
        self.assertEqual(len({r['request_id'] for r in rows}), 3)
        self.assertNotIn('candidate.pdf', str(rows))
        self.assertNotIn(str(self.objects.root), str(rows))
        docx_id = self.upload()
        result = self.view(docx_id)
        self.assertTrue(result['available'])
        self.assertFalse(result['renderable'])
        self.assertEqual(self.client.get(self.path(docx_id, 1), headers=self.admin_headers).status_code, 404)

    def test_nonrenderable_chinese_docx_download_requires_superadmin_and_audits_request(self):
        name = '中文简历-测试.docx'
        rid = self.service.upload_resume(self.owner, self.original_bytes, name, **self.args)['resume_id']
        self.assertFalse(self.view(rid)['renderable'])
        path = self.path(rid) + '/download'
        before = self.original_audits()
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 403)
        self.assertEqual(self.original_audits(), before)

        request_uuid = uuid4()
        with patch('db_http_preview.uuid4', return_value=request_uuid):
            response = self.client.get(path, headers=self.admin_headers)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        disposition = response.headers['Content-Disposition']
        self.assertTrue(disposition.startswith('attachment;'))
        self.assertIn("filename*=UTF-8''" + quote(name), disposition)
        digest = sha256(self.original_bytes).hexdigest()
        self.assertEqual(sha256(response.data).hexdigest(), digest)
        self.assertEqual(response.headers['X-Original-SHA256'], digest)

        rows = self.original_audits()
        self.assertEqual(len(rows), len(before) + 1)
        audit = rows[-1]
        self.assertEqual(audit['action'], 'resume.original_download')
        self.assertEqual(str(audit['target_id']), rid)
        self.assertEqual(audit['request_id'], 'db-preview:' + str(request_uuid))

    def test_docx_content_is_rendered_and_audited_for_superadmin(self):
        rid = self.upload()
        path = self.path(rid) + '/content'
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 403)
        self.assertEqual(self.original_audits(), [])
        response = self.client.get(path, headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data']['name'], 'candidate.docx')
        self.assertIn('<p>', response.json['data']['html'])
        audits = self.original_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['action'], 'resume.original_content')
        self.assertEqual(audits[0]['request_id'], response.json['request_id'])

    def test_default_preview_ordinary_user_and_spoofed_superadmin_cannot_access(self):
        rid = self.seed_pdf()
        client = create_preview_app(self.store, self.auth, self.secret).test_client()
        self.assertEqual(client.get(self.path(rid), headers=self.admin_headers).status_code, 503)
        with self.bootstrap.begin() as conn:
            conn.execute(update(User).where(User.id == UUID(self.owner)).values(is_admin=True,
                legacy_payload={'is_super_admin': True}))
        for path in (self.path(rid), self.path(rid, 1)):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 403)
        with self.assertRaises(ContentError) as caught:
            self.viewer.view(self.owner, rid, **self.args)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.original_audits(), [])

    def test_revocation_during_native_render_prevents_release(self):
        rid = self.seed_pdf()
        def render(*args, **kwargs):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == UUID(self.other['user_id'])).values(password_version=2))
            return inspect_pdf(*args, **kwargs)
        with patch('db_original_view.inspect_pdf', side_effect=render):
            response = self.client.get(self.path(rid, 1), headers=self.admin_headers)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.data.startswith(b'\x89PNG'))
        self.assertEqual(self.original_audits(), [])

    def test_superadmin_email_change_during_render_is_rechecked(self):
        rid = self.seed_pdf()
        def render(*args, **kwargs):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id == UUID(self.other['user_id'])).values(email=uuid4().hex+'@example.test'))
            return inspect_pdf(*args, **kwargs)
        with patch('db_original_view.inspect_pdf', side_effect=render):
            response = self.client.get(self.path(rid, 1), headers=self.admin_headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.original_audits(), [])

    def test_delete_and_cleanup_during_render_reject_late_image(self):
        rid = self.seed_pdf()
        def render(*args, **kwargs):
            self.service.delete_source(self.owner, 'resume', rid, **self.args)
            self.assertEqual(self.service.collect_originals(), 1)
            return inspect_pdf(*args, **kwargs)
        with patch('db_original_view.inspect_pdf', side_effect=render):
            response = self.client.get(self.path(rid, 1), headers=self.admin_headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.original_audits(), [])

    def test_audit_insert_failure_returns_no_image_or_partial_audit(self):
        rid = self.seed_pdf()
        with patch('db_original_view.append_audit', side_effect=RuntimeError('PRIVATE_DSN')):
            response = self.client.get(self.path(rid, 1), headers=self.admin_headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'PRIVATE_DSN', response.data)
        self.assertEqual(self.original_audits(), [])

    def test_commit_failure_after_audit_insert_returns_no_bytes(self):
        rid = self.seed_pdf()
        from sqlalchemy import event
        commits = []
        def fail_second(conn):
            commits.append(1)
            if len(commits) == 2:
                raise RuntimeError('synthetic commit failure')
        event.listen(self.engine, 'commit', fail_second)
        try:
            response = self.client.get(self.path(rid, 1), headers=self.admin_headers)
        finally:
            event.remove(self.engine, 'commit', fail_second)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.data.startswith(b'\x89PNG'))
        self.assertEqual(self.original_audits(), [])

    def test_corrupt_hash_missing_object_and_wrong_storage_return_no_content(self):
        rid = self.seed_pdf()
        with self.engine.connect() as conn:
            row = conn.execute(select(Resume.__table__).where(Resume.id == UUID(rid))).mappings().one()
        original = deepcopy(row['legacy_payload'])
        for mode in ('store', 'hash', 'missing'):
            payload = deepcopy(original)
            if mode == 'store':
                payload['original']['storage_id'] = str(uuid4())
            elif mode == 'hash':
                payload['original']['sha256'] = '0'*64
            else:
                self.objects.remove(row['object_key'])
            with self.bootstrap.begin() as conn:
                conn.execute(update(Resume).where(Resume.id == UUID(rid)).values(legacy_payload=payload))
            response = self.client.get(self.path(rid), headers=self.admin_headers)
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.original_audits(), [])

    def test_missing_original_legacy_layout_and_bad_pages(self):
        rid = self.seed_pdf()
        for page in (0, 2, 21):
            self.assertEqual(self.client.get(self.path(rid, page), headers=self.admin_headers).status_code, 404)
        with self.bootstrap.begin() as conn:
            row = conn.execute(select(Resume.__table__).where(Resume.id == UUID(rid))).mappings().one()
            payload = deepcopy(row['legacy_payload'])
            del payload['original']['storage_id']
            self.objects._path(row['object_key']).rename(self.objects._path(rid + '.pdf'))
            conn.execute(update(Resume).where(Resume.id == UUID(rid)).values(object_key=None, legacy_payload=payload))
        self.assertTrue(self.view(rid)['renderable'])
        with self.bootstrap.begin() as conn:
            conn.execute(update(Resume).where(Resume.id == UUID(rid)).values(legacy_payload={}))
        self.assertEqual(self.view(rid), {'available': False})

    def enqueue(self):
        rid = self.upload()
        self.service.delete_source(self.owner, 'resume', rid, **self.args)
        return rid

    def test_scheduler_replays_failed_unlink_and_reports_success_after_restart(self):
        self.enqueue()
        failed = Event()
        def fail(key):
            failed.set()
            raise OSError('PRIVATE_PATH')
        worker = OriginalGCWorker(self.service, self.tracker, interval=0.01)
        with patch.object(self.objects, 'remove', side_effect=fail):
            worker.start()
            try:
                self.assertTrue(failed.wait(5))
            finally:
                self.assertTrue(worker.stop(timeout=5))
        report = worker.drain(timeout=1)
        self.assertGreater(report['failures'], 0)
        self.assertFalse(report['complete'])
        self.assertEqual(report['pending_local'], 1)
        self.assertNotIn('PRIVATE_PATH', str(report))
        next_tracker = DrainTracker()
        deleted = Event()
        original = self.objects.remove
        def remove(key):
            original(key)
            deleted.set()
        with patch.object(self.objects, 'remove', side_effect=remove):
            next_worker = OriginalGCWorker(self.service, next_tracker, interval=0.01).start()
            try:
                self.assertTrue(deleted.wait(5))
            finally:
                self.assertTrue(next_worker.stop(timeout=5))
        self.assertTrue(next_worker.drain(timeout=1)['complete'])
        self.assertEqual(self.queue(), [])

    def test_locked_and_foreign_storage_intents_never_count_as_empty(self):
        self.enqueue()
        with self.sessions() as session, session.begin():
            session.execute(select(original_gc).where(original_gc.c.owner_user_id == UUID(self.owner)).with_for_update()).all()
            self.assertEqual(self.service.collect_originals(), 0)
            report = OriginalGCWorker(self.service, self.tracker).drain(timeout=0)
            self.assertFalse(report['complete'])
            self.assertEqual(report['pending'], 1)
        self.service.collect_originals()
        with self.bootstrap.begin() as conn:
            conn.execute(insert(original_gc).values(storage_id=uuid4(), object_key=str(uuid4())+'.pdf', owner_user_id=UUID(self.owner)))
        report = OriginalGCWorker(self.service, self.tracker).report()
        self.assertEqual(report['pending_local'], 0)
        self.assertEqual(report['pending'], 1)
        self.assertFalse(report['complete'])

    def test_drain_tracks_inflight_http_and_refuses_new_view_and_upload(self):
        rid = self.seed_pdf()
        entered, release = Event(), Event()
        def render(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Synthetic render timeout')
            return inspect_pdf(*args, **kwargs)
        worker = OriginalGCWorker(self.service, self.tracker)
        with patch('db_original_view.inspect_pdf', side_effect=render), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: self.app.test_client().get(self.path(rid, 1), headers=self.admin_headers))
            try:
                self.assertTrue(entered.wait(5))
                report = worker.drain(timeout=0)
                self.assertFalse(report['complete'])
                self.assertEqual(report['requests'], 1)
                self.assertEqual(self.client.get(self.path(rid), headers=self.admin_headers).status_code, 503)
                self.assertEqual(self.client.post('/api/v1/resumes/upload', headers=self.headers).status_code, 503)
                self.assertEqual(self.client.get('/ping').status_code, 200)
            finally:
                release.set()
            response = future.result(timeout=10)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.tracker.status()['streams'], 1)
            self.assertFalse(worker.report()['complete'])
            response.close()
        self.assertTrue(worker.report()['complete'])

    def test_buffered_page_stays_registered_until_response_closes(self):
        rid = self.seed_pdf()
        response = self.client.get(self.path(rid, 1), headers=self.admin_headers, buffered=False)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.tracker.status()['requests'], 0)
            self.assertEqual(self.tracker.status()['streams'], 1)
            self.assertFalse(OriginalGCWorker(self.service, self.tracker).drain(timeout=0)['complete'])
        finally:
            response.close()
        self.assertTrue(OriginalGCWorker(self.service, self.tracker).report()['complete'])
