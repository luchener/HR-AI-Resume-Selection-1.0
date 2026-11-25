"""Pure announcement rules and explicitly enabled HTTP boundaries."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import Mock
from uuid import UUID, uuid4

import jwt

from db_announcements import AnnouncementError, schedule
from db_announcement_import import project_announcement_snapshot
from db_auth import InternalUser
from db_http_preview import create_preview_app
from db_migrate_json import inventory


NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
BODY = dict(title='维护公告', content='合成公告正文', start_mode='now', duration_value=2, duration_unit='hour')


def snapshot_fixture(root):
    uid, aid = str(uuid4()), str(uuid4())
    user = dict(user_id=uid, username='snapshot-' + UUID(uid).hex, password_hash='synthetic',
                password_salt='synthetic', password_version=1, email='', created_at=NOW.isoformat())
    record = dict(announcement_id=aid, title='合成源公告', content='冻结正文', start_mode='now',
                  start_at=NOW.isoformat(), end_at=(NOW + timedelta(hours=1)).isoformat(), status='active',
                  created_by_id=uid, created_by_name=user['username'], created_at=NOW.isoformat())
    for name, value in [('users/' + uid, user), ('announcements/' + aid, record),
                        ('announcement_reads/' + uid, dict(dismissed=[aid, aid], updated_at=NOW.isoformat()))]:
        path = root / (name + '.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return uid, aid, record


class AnnouncementRuleTests(TestCase):
    def test_schedule_boundaries_offsets_and_legacy_minimums(self):
        self.assertEqual(schedule(BODY, now=NOW), ('now', NOW, NOW + timedelta(hours=2)))
        self.assertEqual(schedule(BODY | dict(start_mode='delay', delay_value=5, delay_unit='minute'), now=NOW)[1],
                         NOW + timedelta(minutes=5))
        self.assertEqual(schedule(BODY | dict(start_mode='at', start_at='2026-10-19T17:00:00+08:00'), now=NOW)[1],
                         NOW + timedelta(hours=1))
        for start in ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00']:
            self.assertEqual(schedule(BODY | dict(start_mode='at', start_at=start), now=NOW)[1], NOW)
        self.assertEqual(schedule(dict(start_mode='delay', delay_value=-2, duration_value=0), now=NOW),
                         ('delay', NOW, NOW + timedelta(days=1)))

    def test_malformed_or_overflow_schedule_rejected_without_reflecting_values(self):
        cases = [dict(start_mode='private-mode'), dict(duration_unit=['invalid']), dict(delay_unit='private-unit', start_mode='delay'),
                 dict(duration_value=True), dict(duration_value='private-value'), dict(duration_value=10**100),
                 dict(start_mode='at', start_at='private-time'), dict(start_mode='at', start_at=float('inf'))]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(AnnouncementError) as caught:
                    schedule(BODY | extra, now=NOW)
                self.assertNotIn('private-', caught.exception.detail)

    def test_projection_uses_captured_inventory_and_deduplicates_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uid, aid, row = snapshot_fixture(root)
            report = inventory(root)
            path = root / 'announcements' / (aid + '.json')
            path.write_text(json.dumps(row | {'content': '后续文件漂移'}), encoding='utf-8')
            plan = project_announcement_snapshot(report)
            self.assertEqual(plan['announcements'][0]['content'], '冻结正文')
            self.assertEqual(len(plan['dismissals']), 1)
            self.assertEqual(plan['dismissals'][0]['user_id'], UUID(uid))
            self.assertEqual(plan['dismissals'][0]['dismissed_at'], NOW)
            self.assertNotEqual(plan['projection_sha256'], project_announcement_snapshot(inventory(root))['projection_sha256'])

    def test_projection_preserves_soft_deletion_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _uid, aid, row = snapshot_fixture(root)
            path = root / 'announcements' / (aid + '.json')
            path.write_text(json.dumps(row | dict(status='cancelled', cancelled_at=NOW.isoformat(), deleted_at=NOW.isoformat())), encoding='utf-8')
            converted = project_announcement_snapshot(inventory(root))['announcements'][0]
            self.assertEqual((converted['status'], converted['cancelled_at'], converted['deleted_at']), ('cancelled', NOW, NOW))

    def test_projection_refuses_orphans_conflicting_ids_and_invalid_lifecycle(self):
        for change in [dict(announcement_id=str(uuid4())), dict(created_by_id=str(uuid4())), dict(end_at=NOW.isoformat()),
                       dict(status='scheduled'), dict(status='cancelled'), dict(title='private-' * 20)]:
            with self.subTest(fields=list(change)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _uid, aid, row = snapshot_fixture(root)
                (root / 'announcements' / (aid + '.json')).write_text(json.dumps(row | change), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '^Announcement snapshot projection blocked$'):
                    project_announcement_snapshot(inventory(root))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uid, _aid, _row = snapshot_fixture(root)
            (root / 'announcement_reads' / (uid + '.json')).write_text(json.dumps(
                dict(dismissed=[str(uuid4())], updated_at=NOW.isoformat())), encoding='utf-8')
            with self.assertRaises(ValueError):
                project_announcement_snapshot(inventory(root))


class AnnouncementHttpTests(TestCase):
    def setUp(self):
        self.identity, self.secret = str(uuid4()), 'synthetic-announcement-jwt-secret-at-least-48-bytes'
        self.user = InternalUser(user_id=UUID(self.identity), username='alice', email='',
            password_hash='PRIVATE', password_salt='PRIVATE', password_version=3,
            is_admin=False, is_super_admin=False, status='active', deleted_at=None, created_at=NOW, updated_at=NOW)
        self.auth = Mock()
        self.auth.find_user_by_id.return_value = self.user
        self.service = Mock()
        self.service.admin_emails = frozenset()
        self.service.active_for_user.return_value = {'items': []}
        self.service.dismiss.return_value = {'message': '已关闭。'}
        self.service.create.return_value = {'announcement_id': str(uuid4())}
        self.app = create_preview_app(Mock(), self.auth, self.secret, announcement_service=self.service)
        self.client = self.app.test_client()
        now = time.time()
        token = jwt.encode(dict(user_id=self.identity, username='forged-name', pwd_ver=3, iss='resume-matcher',
                                jti=str(uuid4()), iat=now, exp=now + 3600), self.secret, algorithm='HS256')
        self.headers = {'Authorization': 'Bearer ' + token}

    def test_user_routes_forward_authoritative_identity(self):
        self.assertEqual(self.client.get('/api/v1/notifications/announcements', headers=self.headers).status_code, 200)
        identity = str(uuid4())
        self.assertEqual(self.client.post('/api/v1/notifications/announcements/' + identity + '/dismiss',
            json={'user_id': str(uuid4())}, headers=self.headers).status_code, 200)
        self.service.dismiss.assert_called_once()
        args = self.service.dismiss.call_args.kwargs
        self.assertEqual((args['actor_id'], args['password_version'], args['announcement_id']), (self.identity, 3, identity))
        self.assertTrue(args['request_id'])

    def test_default_closed_authentication_and_admin_gates(self):
        closed = create_preview_app(Mock(), self.auth, self.secret).test_client()
        self.assertEqual(closed.get('/api/v1/notifications/announcements', headers=self.headers).status_code, 503)
        self.assertEqual(self.client.get('/api/v1/notifications/announcements').status_code, 401)
        for method, path in [('get', '/api/v1/admin/announcements'), ('post', '/api/v1/admin/announcements'),
                             ('post', '/api/v1/admin/announcements/x/cancel'), ('delete', '/api/v1/admin/announcements/x')]:
            self.assertEqual(getattr(self.client, method)(path, headers=self.headers).status_code, 403)
        self.service.create.assert_not_called()

    def test_policy_match_is_required(self):
        self.service.admin_emails = frozenset({'admin@example.test'})
        with self.assertRaises(ValueError):
            create_preview_app(Mock(), self.auth, self.secret, announcement_service=self.service)

    def test_errors_are_generic_and_bodies_are_bounded_per_endpoint(self):
        self.auth.find_user_by_id.return_value = replace(self.user, is_admin=True)
        self.service.create.side_effect = RuntimeError('PRIVATE_DATABASE_URL password=PRIVATE')
        response = self.client.post('/api/v1/admin/announcements', json=BODY, headers=self.headers)
        self.assertEqual((response.status_code, response.get_json()['detail']), (503, 'DB preview unavailable'))
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.service.create.side_effect = None
        self.assertEqual(self.client.post('/api/v1/admin/announcements', json=BODY | {'content': '合' * 5000},
                                         headers=self.headers).status_code, 201)
        self.assertEqual(self.client.post('/api/v1/admin/announcements', json=BODY | {'content': 'x' * (64 * 1024)},
                                         headers=self.headers).status_code, 413)
        self.assertEqual(self.client.post('/api/v1/auth/login', json={'username': 'a', 'password': 'x' * 20000}).status_code, 413)
        for payload in [None, [], 'private-body']:
            self.assertEqual(self.client.post('/api/v1/admin/announcements', json=payload,
                                             headers=self.headers).status_code, 422)
