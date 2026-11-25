"""Announcement transactions, concurrency and legacy HTTP comparison in local PG."""
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import os
import safe_fs as safe_fs_module
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from flask import Flask, g, jsonify, request
import sqlalchemy as sa

from db_announcement_state import announcements, dismissals
from db_announcements import AnnouncementError, AnnouncementService
from db_auth import AuthService
from db_check_schema import metadata
from db_http_preview import create_preview_app
from db_http_shadow import compare_responses
from db_models import User
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_test_guard import review_test_url
from test_db_announcements import BODY, NOW

SHADOW_EVIDENCE = []


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AnnouncementLiveTests(TestCase):
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
        self.super_email = 'super-' + suffix + '@example.test'
        self.auth = AuthService(self.sessions, admin_emails=[self.super_email])
        self.users = []
        for role, email in [('user', ''), ('other', ''), ('admin', ''), ('super', self.super_email)]:
            user, error = self.auth.create_user(role + '-' + suffix, 'SyntheticPassword9', email)
            self.assertIsNone(error)
            self.users.append(user)
        self.user, self.other, self.admin, self.super = [user['user_id'] for user in self.users]
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.admin).values(is_admin=True))
        self.now = NOW
        self.service = AnnouncementService(self.sessions, admin_emails=[self.super_email], clock=lambda: self.now)
        self.app = create_preview_app(Store(session_factory=self.sessions), self.auth,
            'synthetic-announcement-preview-secret-with-at-least-48-bytes',
            admin_emails=[self.super_email], announcement_service=self.service)
        self.client = self.app.test_client()
        self.headers = {}
        for user in self.users:
            response = self.client.post('/api/v1/auth/login', json={'username': user['username'], 'password': 'SyntheticPassword9'})
            self.assertEqual(response.status_code, 200)
            self.headers[user['user_id']] = {'Authorization': 'Bearer ' + response.get_json()['data']['token']}

    def call(self, method, path, *, actor=None, body=None):
        return getattr(self.client, method)(path, json=body, headers=self.headers[actor or self.admin])

    def create(self, body=None, actor=None):
        response = self.call('post', '/api/v1/admin/announcements', actor=actor, body=body or BODY)
        self.assertEqual(response.status_code, 201)
        return response.get_json()['data']['announcement_id']

    def state(self, identity):
        audit = metadata().tables['audit_logs']
        with self.bootstrap.connect() as conn:
            row = conn.execute(sa.select(announcements).where(announcements.c.id == identity)).mappings().one_or_none()
            closed = [dict(r) for r in conn.execute(sa.select(dismissals).where(
                dismissals.c.announcement_id == identity).order_by(dismissals.c.user_id)).mappings()]
            logs = [dict(r) for r in conn.execute(sa.select(audit).where(audit.c.target_id == str(identity)).order_by(audit.c.id)).mappings()]
            return dict(row) if row else None, closed, logs

    def active_ids(self, actor):
        response = self.call('get', '/api/v1/notifications/announcements', actor=actor)
        self.assertEqual(response.status_code, 200)
        return [item['announcement_id'] for item in response.get_json()['data']['items']]

    def test_lifecycle_per_user_closure_and_soft_delete_are_persistent_and_idempotent(self):
        identity = self.create(BODY | {'created_by_id': self.other, 'created_by_name': 'forged'})
        row = self.state(identity)[0]
        self.assertEqual(str(row['created_by_id']), self.admin)
        self.assertEqual(row['created_by_name'], self.users[2]['username'])
        self.assertIn(identity, self.active_ids(self.user))
        path = '/api/v1/notifications/announcements/' + identity + '/dismiss'
        response = self.call('post', path, actor=self.user, body={'user_id': self.other})
        self.assertEqual(response.get_json()['data'], {'message': '已关闭。'})
        first = self.state(identity)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.call('post', path, actor=self.user).status_code, 200)
        self.assertEqual(self.state(identity), first)
        self.assertNotIn(identity, self.active_ids(self.user))
        self.assertIn(identity, self.active_ids(self.other))
        cancel = '/api/v1/admin/announcements/' + identity + '/cancel'
        self.assertEqual(self.call('post', cancel).status_code, 200)
        cancelled = self.state(identity)
        self.assertNotIn(identity, self.active_ids(self.other))
        self.assertEqual(self.call('post', cancel).status_code, 200)
        self.assertEqual(self.state(identity), cancelled)
        delete = '/api/v1/admin/announcements/' + identity
        self.assertEqual(self.call('delete', delete).status_code, 200)
        deleted = self.state(identity)
        self.assertEqual(deleted[0]['deleted_at'], self.now)
        self.assertEqual(self.call('delete', delete).status_code, 200)
        self.assertEqual(self.state(identity), deleted)
        self.assertEqual(len(deleted[1]), 1)
        self.assertEqual([r['action'] for r in deleted[2]],
                         ['announcement.create', 'announcement.dismiss', 'announcement.cancel', 'announcement.delete'])
        for log in deleted[2]:
            self.assertNotIn(BODY['content'], str(log['metadata']))

    def test_schedule_edges_pagination_stable_order_and_summary_contract(self):
        self.now = NOW + timedelta(days=500)
        identity = self.create(BODY | dict(start_mode='delay', delay_value=5, delay_unit='minute'))
        self.assertNotIn(identity, self.active_ids(self.user))
        self.now += timedelta(minutes=5)
        self.assertIn(identity, self.active_ids(self.user))
        visible = self.call('get', '/api/v1/notifications/announcements', actor=self.user).get_json()['data']['items']
        item = next(row for row in visible if row['announcement_id'] == identity)
        self.assertEqual(set(item), {'announcement_id', 'title', 'content', 'created_at', 'start_at', 'end_at'})
        self.now += timedelta(hours=2)
        self.assertNotIn(identity, self.active_ids(self.user))
        listing = self.call('get', '/api/v1/admin/announcements?size=1').get_json()['data']
        count = listing['total']
        self.assertEqual(len(listing['items']), 1)
        self.assertEqual(listing['items'][0]['status'], 'expired')
        overflow = self.call('get', '/api/v1/admin/announcements?page=99999999999999999999999999999999&size=1').get_json()['data']
        self.assertEqual(overflow, {'total': count, 'items': []})
        tie = self.create()
        third = self.create()
        ordered = self.call('get', '/api/v1/admin/announcements?size=2').get_json()['data']['items']
        self.assertEqual([r['announcement_id'] for r in ordered], sorted([tie, third], reverse=True))
        clamped = self.call('get', '/api/v1/admin/announcements?size=0&page=invalid').get_json()['data']
        self.assertEqual(len(clamped['items']), 1)

    def test_permissions_revocation_and_current_superadmin_policy(self):
        self.assertEqual(self.call('get', '/api/v1/admin/announcements', actor=self.user).status_code, 403)
        self.assertTrue(self.create(actor=self.super))
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.admin).values(is_admin=False, is_super_admin=True))
        for method in [lambda: self.service.create(self.admin, BODY, password_version=1, request_id='revoked'),
                       lambda: self.service.list_admin(self.admin, password_version=1)]:
            with self.assertRaises(AnnouncementError) as caught:
                method()
            self.assertEqual(caught.exception.status, 403)
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id == self.super).values(email='revoked-' + uuid4().hex + '@example.test'))
        self.assertEqual(self.call('get', '/api/v1/admin/announcements', actor=self.super).status_code, 403)
        for values in [dict(password_version=2), dict(password_version=1, status='locked'),
                       dict(status='active', deleted_at=NOW)]:
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(User).where(User.id == self.user).values(**values))
            with self.assertRaises(AnnouncementError) as caught:
                self.service.active_for_user(self.user, password_version=1)
            self.assertEqual(caught.exception.status, 401)

    def test_audit_failure_rolls_back_each_business_mutation(self):
        identity = self.create()
        before = self.state(identity)
        for method, path, actor in [('post', '/api/v1/admin/announcements/' + identity + '/cancel', self.admin),
                                   ('delete', '/api/v1/admin/announcements/' + identity, self.admin),
                                   ('post', '/api/v1/notifications/announcements/' + identity + '/dismiss', self.user)]:
            with patch('db_announcements.append_audit', side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
                response = self.call(method, path, actor=actor)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))
            self.assertEqual(self.state(identity), before)
        failed_id = uuid4()
        with patch('db_announcements.uuid4', return_value=failed_id), patch('db_announcements.append_audit', side_effect=RuntimeError('synthetic')):
            self.assertEqual(self.call('post', '/api/v1/admin/announcements', body=BODY).status_code, 503)
        self.assertEqual(self.state(failed_id), (None, [], []))

    def test_real_commit_failure_returns_no_success_and_leaves_no_change(self):
        identity = self.create()
        before = self.state(identity)
        def fail_commit(_connection):
            raise RuntimeError('synthetic commit failure')
        sa.event.listen(self.engine, 'commit', fail_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, 'synthetic commit failure'):
                self.service.dismiss(self.user, identity, password_version=1, request_id='failed-commit')
        finally:
            sa.event.remove(self.engine, 'commit', fail_commit)
        self.assertEqual(self.state(identity), before)

    def test_concurrent_repeated_closures_are_unique_and_do_not_hide_other_users(self):
        identity = self.create()
        actors, gate = [self.user, self.other, self.user, self.other], Barrier(4)
        def dismiss(actor):
            gate.wait(timeout=10)
            return self.service.dismiss(actor, identity, password_version=1, request_id='concurrent-' + uuid4().hex)
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(dismiss, actors))
        self.assertEqual(replies, [{'message': '已关闭。'}] * 4)
        row, closed, logs = self.state(identity)
        self.assertEqual(len(closed), 2)
        self.assertEqual(len([log for log in logs if log['action'] == 'announcement.dismiss']), 2)
        self.assertIn(identity, self.active_ids(self.admin))
        self.assertNotIn(identity, self.active_ids(self.user))
        self.assertNotIn(identity, self.active_ids(self.other))
        self.assertEqual(row['status'], 'active')

    def test_cancel_racing_closure_never_reactivates_announcement(self):
        identity, gate = self.create(), Barrier(2)
        def run(cancel):
            gate.wait(timeout=10)
            if cancel:
                return self.service.cancel(self.admin, identity, password_version=1, request_id='race-cancel')
            return self.service.dismiss(self.user, identity, password_version=1, request_id='race-dismiss')
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(run, [True, False]))
        self.assertEqual(len(replies), 2)
        self.assertEqual((self.state(identity)[0]['status'], len(self.state(identity)[1])), ('cancelled', 1))
        self.assertNotIn(identity, self.active_ids(self.other))

    def test_role_change_committed_before_waiting_actor_lock_is_rechecked(self):
        entered = Event()
        original = self.service._actor
        def actor(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with self.bootstrap.begin() as conn, ThreadPoolExecutor(max_workers=1) as pool:
            conn.execute(sa.update(User).where(User.id == self.admin).values(is_admin=False))
            with patch.object(self.service, '_actor', side_effect=actor):
                future = pool.submit(self.service.create, self.admin, BODY, password_version=1, request_id='role-race')
                self.assertTrue(entered.wait(5))
                conn.commit()
                with self.assertRaises(AnnouncementError) as caught:
                    future.result(timeout=10)
                self.assertEqual(caught.exception.status, 403)

    def test_validation_missing_records_and_plain_text_are_preserved(self):
        for body in [BODY | {'title': ' '}, BODY | {'content': 'x' * 5001}, BODY | {'content': {'bad': 1}},
                     BODY | {'duration_value': 10**100}, BODY | {'content': 'x\x00y'}]:
            self.assertEqual(self.call('post', '/api/v1/admin/announcements', body=body).status_code, 422)
        identity = self.create(BODY | {'content': '<script>synthetic</script>'})
        self.assertEqual(self.state(identity)[0]['content'], '<script>synthetic</script>')
        for missing in ['invalid-id', str(uuid4())]:
            self.assertEqual(self.call('post', '/api/v1/admin/announcements/' + missing + '/cancel').status_code, 404)
            self.assertEqual(self.call('delete', '/api/v1/admin/announcements/' + missing).status_code, 404)
            self.assertEqual(self.call('post', '/api/v1/notifications/announcements/' + missing + '/dismiss', actor=self.user).status_code, 404)

    def test_runtime_cannot_hard_delete_and_readonly_cannot_read_business_records(self):
        from sqlalchemy.exc import DBAPIError
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        reader = migration_engine(url.set(username='resume_readonly', password='review-reader-only')
                                  .render_as_string(hide_password=False))
        self.addCleanup(reader.dispose)
        for table in ('announcements', 'announcement_dismissals'):
            for engine, statement in [(self.engine, 'DELETE FROM ' + table + ' WHERE false'),
                                      (self.engine, 'TRUNCATE ' + table),
                                      (reader, 'SELECT * FROM public.' + table + ' LIMIT 0')]:
                with self.subTest(sql=statement), self.assertRaises(DBAPIError) as caught:
                    with engine.begin() as conn:
                        conn.execute(sa.text(statement))
                self.assertEqual(caught.exception.orig.sqlstate, '42501')

    def test_selected_legacy_http_contracts_and_deliberate_drift_detection(self):
        namespace, memory = self.legacy_handlers()
        old_client = namespace['app'].test_client()
        namespace['actor'] = self.users[2]
        fixed_id = str(uuid4())
        namespace['uuid'] = SimpleNamespace(uuid4=lambda: UUID(fixed_id))
        prefix = '/api/v1/admin/announcements'
        user_path = '/api/v1/notifications/announcements'
        cases = [('post', prefix, BODY, self.admin), ('get', prefix, None, self.admin),
            ('get', prefix + '?page=bad&size=0', None, self.admin),
            ('get', prefix + '?page=99999', None, self.admin), ('get', user_path, None, self.user),
            ('post', user_path + '/' + fixed_id + '/dismiss', None, self.user),
            ('post', user_path + '/' + fixed_id + '/dismiss', None, self.user),
            ('get', user_path, None, self.user), ('get', user_path, None, self.other),
            ('post', prefix + '/' + fixed_id + '/cancel', None, self.admin), ('get', prefix, None, self.admin),
            ('delete', prefix + '/' + fixed_id, None, self.admin), ('delete', prefix + '/' + fixed_id, None, self.admin),
            ('get', prefix, None, self.admin), ('get', user_path, None, self.other),
            ('post', prefix + '/missing/cancel', None, self.admin), ('delete', prefix + '/missing', None, self.admin),
            ('post', user_path + '/missing/dismiss', None, self.user),
            ('post', prefix, BODY | {'title': ''}, self.admin), ('post', prefix, BODY | {'duration_unit': 'bad'}, self.admin)]
        # Other tests share the disposable DB. Compare only this scenario's
        # records, avoiding a hidden dependency on test ordering.
        with self.bootstrap.begin() as conn:
            conn.execute(announcements.update().where(announcements.c.deleted_at.is_(None)).values(deleted_at=NOW))
        for method, path, body, actor in cases:
            namespace['actor'] = next(user for user in self.users if user['user_id'] == actor)
            with patch('db_announcements.uuid4', return_value=UUID(fixed_id)):
                left = getattr(old_client, method)(path, json=body)
                right = self.call(method, path, body=body, actor=actor)
            self.assertTrue(compare_responses(left.status_code, left.get_json(), right.status_code, right.get_json())['equal'],
                            (method, path, left.status_code, right.status_code))
        baseline = self.call('get', prefix).get_json()
        drifted = deepcopy(baseline)
        drifted['data']['total'] += 1
        self.assertFalse(compare_responses(200, baseline, 200, drifted)['equal'])
        self.assertTrue(memory)
        SHADOW_EVIDENCE.append(dict(synthetic_cases=len(cases), differences=0, deliberate_drift_rejected=True,
            scope='selected_legacy_announcement_handlers_not_full_production_app'))

    def legacy_handlers(self):
        names = {'_now_iso', '_parse_iso', 'compute_schedule', '_announcement_path', 'effective_status',
                 'create_announcement', 'get_announcement', '_list_all_announcements', 'list_announcements',
                 'cancel_announcement', 'delete_announcement', '_reads_path', '_dismissed_ids',
                 'user_active_announcements', 'dismiss_announcement'}
        tree = ast.parse(Path(__file__).with_name('notifications.py').read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(nodes), len(names))
        memory = {}
        old = Flask('legacy-announcement-contract')
        namespace = dict(app=old, request=request, jsonify=jsonify, g=g, _now=lambda: self.now,
            datetime=__import__('datetime').datetime, timedelta=timedelta, timezone=__import__('datetime').timezone,
            _TIME_UNITS={'minute': 1, 'hour': 60, 'day': 1440}, MAX_TITLE=100, MAX_CONTENT=5000,
            config=SimpleNamespace(ANNOUNCEMENTS_DIR='announcements', ANNOUNCEMENT_READS_DIR='announcement_reads'),
            os=SimpleNamespace(path=SimpleNamespace(join=lambda *parts: '/'.join(parts), isdir=lambda _path: True)),
            safe_fs=SimpleNamespace(
                listdir=lambda path: [key.split('/')[-1] for key in memory if key.startswith(path + '/')],
                safe_component=safe_fs_module.safe_component,
            ),
            _read_json=lambda path: deepcopy(memory.get(path)),
            _write_json=lambda path, value: memory.update({path: deepcopy(value)}),
            _request_id=lambda *_: 'legacy:' + uuid4().hex,
            _current_user_id=lambda: namespace['actor']['user_id'],
            auth_mod=SimpleNamespace(record_admin_op=lambda *args, **kwargs: None))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'legacy-announcement-functions', 'exec'), namespace)
        # Functions use the same globals as this proxy; assignment of uuid and
        # actor between requests must be visible to the extracted handlers.
        namespace['notify_mod'] = SimpleNamespace(**{name: namespace[name] for name in names})
        handler_names = {'admin_list_announcements', 'admin_create_announcement', 'admin_cancel_announcement',
                         'admin_delete_announcement', 'user_announcements', 'user_dismiss_announcement'}
        source = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8'))
        handlers = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in handler_names]
        self.assertEqual(len(handlers), len(handler_names))
        exec(compile(ast.Module(body=handlers, type_ignores=[]), 'legacy-announcement-handlers', 'exec'), namespace)
        @old.before_request
        def actor():
            g.auth_user = namespace['actor']
        return namespace, memory
