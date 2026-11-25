"""Public authentication on guarded PostgreSQL, with captured synthetic mail."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
import os
from threading import Barrier, Event
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import event

from db_accounts import AccountService
from db_auth import AuthService, verify_password
from db_check_schema import metadata
from db_http_preview import create_preview_app
from db_models import User
from db_public_auth import GENERIC_RESET, PublicAuthError, PublicAuthService
from db_public_auth_state import TABLES, challenges, invites, limits, login_state
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_test_guard import review_test_url


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class PublicAuthLiveTests(TestCase):
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
        self.auth = AuthService(self.sessions)
        self.admin, _ = self.auth.create_user('owner-' + uuid4().hex, 'SyntheticPassword9', uuid4().hex+'@example.test')
        self.user, _ = self.auth.create_user('member-' + uuid4().hex, 'SyntheticPassword9', uuid4().hex+'@example.test')
        self.admins = (self.admin['email'],)
        self.mail = []
        self.ip = '2001:db8::' + uuid4().hex[-4:]
        self.secret = 'synthetic-public-auth-jwt-with-at-least-48-bytes'
        self.service = self.new_service()
        self.store = Store(session_factory=self.sessions)
        self.app = self.make_app(self.service)
        self.client = self.app.test_client()
        self.args = dict(password_version=1, request_id='auth-' + uuid4().hex)
        self.addCleanup(self.clear_state)

    def new_service(self, **options):
        return PublicAuthService(self.sessions, secret=b'synthetic-shared-HMAC-key-for-review-only',
            send_mail=lambda **message: self.mail.append(message), admin_emails=self.admins,
            captcha_enabled=options.pop('captcha_enabled', False), **options)

    def make_app(self, service):
        return create_preview_app(self.store, self.auth, self.secret, admin_emails=self.admins, public_auth=service)

    def clear_state(self):
        # Only this guarded disposable test suite owns these tables. Never a
        # production cleanup path; all concurrent futures finish before cleanup.
        with self.bootstrap.begin() as conn:
            for table in reversed(TABLES):
                conn.execute(table.delete())

    def call(self, path, data=None, *, method='post', client=None, headers=None):
        return getattr(client or self.client, method)('/api/v1/auth/' + path, json=data,
            headers=headers, environ_base={'REMOTE_ADDR': self.ip})

    def row(self, user=None):
        with self.engine.connect() as conn:
            return conn.execute(sa.select(User.__table__).where(User.id == UUID((user or self.user)['user_id']))).mappings().one()

    def code_row(self, purpose, email):
        with self.engine.connect() as conn:
            return conn.execute(sa.select(challenges).where(challenges.c.purpose == purpose,
                challenges.c.subject == self.service.digest('subject:' + purpose, email))).mappings().one()

    def audit(self, action, user=None):
        table = metadata().tables['audit_logs']
        with self.engine.connect() as conn:
            query = sa.select(table).where(table.c.action == action)
            if user:
                query = query.where(table.c.target_id == user['user_id'])
            return conn.execute(query).mappings().all()

    def invitation(self, email=None):
        email = email or uuid4().hex + '@example.test'
        code = self.service.issue_invite(self.admin['user_id'], email, **self.args)
        return email, code

    def registration(self):
        email, invite = self.invitation()
        self.service.request_code('register', email, ip=self.ip, invite_code=invite)
        return dict(username='new-' + uuid4().hex, password='SyntheticPassword9', email=email,
                    code=self.mail[-1]['code'], invite_code=invite)

    def reset(self):
        response = self.call('reset-password/request', {'email': self.user['email']})
        self.assertEqual(response.status_code, 200)
        return self.mail[-1]['code']

    def login(self, password='SyntheticPassword9'):
        return self.call('login', dict(username=self.user['username'], password=password))

    def test_registration_contract_privilege_fields_and_replay(self):
        body = self.registration()
        self.assertEqual(self.call('invite-code/check', {'invite_code': body['invite_code']}).status_code, 200)
        response = self.call('register', dict(body, is_admin=True, is_super_admin=True, status='active'))
        self.assertEqual(response.status_code, 200)
        result = response.get_json()['data']
        self.assertEqual(set(result), {'user_id','username','token'})
        user = self.auth.find_user_by_id(result['user_id'])
        self.assertFalse(user.is_admin or user.is_super_admin)
        me = self.call('me', method='get', headers={'Authorization':'Bearer '+result['token']})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(response.headers['X-DB-Preview'], 'auth-local')
        self.assertEqual(self.code_row('register', body['email'])['status'], 'used')
        self.assertEqual(self.call('register', body).status_code, 422)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(User).where(User.email == body['email'])), 1)
        events = self.audit('user.create', {'user_id':result['user_id']})
        self.assertEqual(len(events), 1)
        for value in (body['code'], body['invite_code'], body['password'], body['email']):
            self.assertNotIn(value, str(events))

    def test_parallel_registration_consumes_one_invite_and_code_once(self):
        body, barrier = self.registration(), Barrier(2)
        def register(index):
            barrier.wait(5)
            try:
                return self.new_service().register(**body, ip=self.ip, request_id='parallel-'+str(index))
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(register, range(2)))
        self.assertEqual(sum(isinstance(x, dict) for x in values), 1)
        self.assertIn(422, values)

    def test_registration_audit_failure_restores_both_codes_and_retry_succeeds(self):
        body = self.registration()
        with patch('db_public_auth.append_audit', side_effect=RuntimeError('PRIVATE_DB_PASSWORD')):
            response = self.call('register', body)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_DB_PASSWORD', response.get_data(as_text=True))
        self.assertIsNone(self.auth.find_user_by_email(body['email']))
        self.assertEqual(self.code_row('register', body['email'])['status'], 'sent')
        self.assertEqual(self.call('register', body).status_code, 200)

    def test_unique_race_with_trusted_creation_preserves_code_and_invite(self):
        body = self.registration()
        original = self.service._challenge
        def racing(*args):
            result = original(*args)
            rival, error = self.auth.create_user(body['username'], 'SyntheticPassword9', uuid4().hex+'@example.test')
            self.assertIsNone(error)
            self.assertIsNotNone(rival)
            return result
        with patch.object(self.service, '_challenge', side_effect=racing):
            self.assertEqual(self.call('register', body).status_code, 409)
        row = self.code_row('register', body['email'])
        self.assertEqual((row['status'], row['attempts']), ('sent', 0))
        body['username'] = 'retry-' + uuid4().hex
        self.assertEqual(self.call('register', body).status_code, 200)

    def test_invite_collisions_never_overwrite_and_expired_invites_release_capacity(self):
        email, code = self.invitation()
        with self.engine.connect() as conn:
            before = dict(conn.execute(sa.select(invites)).mappings().one())
        letters = iter(code * 8)
        with patch('db_public_auth.secrets.choice', side_effect=lambda alphabet: next(letters)):
            with self.assertRaises(PublicAuthError) as caught:
                self.service.issue_invite(self.admin['user_id'], uuid4().hex+'@example.test', **self.args)
        self.assertEqual(caught.exception.status, 503)
        with self.engine.connect() as conn:
            self.assertEqual(dict(conn.execute(sa.select(invites)).mappings().one()), before)
        small = self.new_service(max_keys=10)
        for _ in range(9):
            small.issue_invite(self.admin['user_id'], uuid4().hex+'@example.test', **self.args)
        with self.assertRaises(PublicAuthError):
            small.issue_invite(self.admin['user_id'], email, **self.args)
        with self.bootstrap.begin() as conn:
            conn.execute(invites.update().values(expires_at=sa.func.now()-sa.text("interval '1 second'")))
        small.issue_invite(self.admin['user_id'], email, **self.args)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(invites)), 1)

    def test_login_audit_failure_does_not_clear_shared_failures_or_change_last_login(self):
        self.assertEqual(self.login('WrongPassword9').status_code, 401)
        with patch('db_public_auth.append_audit', side_effect=RuntimeError('PRIVATE_AUDIT_FAILURE')):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_AUDIT_FAILURE', response.get_data(as_text=True))
        self.assertEqual(self.row()['login_window_failures'], 1)
        self.assertIsNone(self.row()['last_login_at'])
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(login_state)), 1)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.row()['login_window_failures'], 0)

    def test_duplicate_username_does_not_burn_codes_and_email_binding_is_enforced(self):
        body = self.registration()
        body['username'] = self.user['username']
        self.assertEqual(self.call('register', body).status_code, 409)
        self.assertEqual(self.code_row('register', body['email'])['attempts'], 0)
        self.assertEqual(self.call('register', dict(body, email=uuid4().hex+'@example.test')).status_code, 422)
        body['username'] = 'retry-' + uuid4().hex
        self.assertEqual(self.call('register', body).status_code, 200)

    def test_invite_alias_cannot_skip_gate_and_superadmin_email_cannot_public_register(self):
        self.assertEqual(self.call('email-code/send', {'email':uuid4().hex+'@example.test'}).status_code, 422)
        with self.assertRaises(PublicAuthError):
            self.service.issue_invite(self.admin['user_id'], self.admin['email'], **self.args)
        email, invite = self.invitation()
        with self.assertRaises(PublicAuthError) as error:
            self.service.issue_invite(self.user['user_id'], email, **self.args)
        self.assertEqual(error.exception.status, 403)
        with self.bootstrap.begin() as conn:
            conn.execute(invites.update().values(expires_at=sa.func.now()-sa.text("interval '1 second'")))
        self.assertEqual(self.call('invite-code/check', {'invite_code':invite}).status_code, 422)

    def test_reset_rejects_old_tokens_password_and_replayed_code(self):
        token = self.login().get_json()['data']['token']
        code = self.reset()
        response = self.call('reset-password/confirm', {'email':self.user['email'],'code':code,'new_password':'NewPassword9'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.row()['password_version'], 2)
        self.assertEqual(self.call('me', method='get', headers={'Authorization':'Bearer '+token}).status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login('NewPassword9').status_code, 200)
        self.assertEqual(self.call('reset-password/confirm', {'email':self.user['email'],'code':code,'new_password':'OtherPassword9'}).status_code, 400)
        self.assertEqual(len(self.audit('user.password_reset', self.user)), 1)

    def test_reset_code_wrong_attempt_limit_persists_and_does_not_authenticate(self):
        correct = self.reset()
        wrong = '000000' if correct != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.call('reset-password/confirm', {'email':self.user['email'],'code':wrong,'new_password':'NewPassword9'}).status_code, 400)
        self.assertEqual(self.code_row('reset',self.user['email'])['attempts'], 5)
        self.assertEqual(self.call('reset-password/confirm', {'email':self.user['email'],'code':correct,'new_password':'NewPassword9'}).status_code, 400)
        self.assertEqual(self.row()['password_version'], 1)

    def test_parallel_reset_succeeds_once_and_audit_is_atomic(self):
        code, barrier = self.reset(), Barrier(2)
        def recover(index):
            barrier.wait(5)
            try:
                return self.new_service().recover('reset', self.user['email'], code, password='NewPassword9',
                    request_id='recover-'+str(index), ip=self.ip)
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(recover, range(2)))
        self.assertEqual(sum(isinstance(x,dict) for x in values), 1)
        self.assertIn(400, values)
        self.assertEqual(self.row()['password_version'], 2)
        self.assertEqual(len(self.audit('user.password_reset', self.user)), 1)

    def test_reset_audit_and_commit_failures_restore_password_and_code(self):
        code = self.reset()
        for failure in ('audit','commit'):
            session = self.sessions()
            def fail(*args, **kwargs):
                raise RuntimeError('Synthetic infrastructure failure')
            if failure == 'commit':
                event.listen(session, 'before_commit', fail)
            try:
                with patch.object(self.service, '_session_factory', return_value=session):
                    with patch('db_public_auth.append_audit', side_effect=fail) if failure == 'audit' else nullcontext():
                        with self.assertRaises(RuntimeError):
                            self.service.recover('reset', self.user['email'], code, password='NewPassword9',
                                                 request_id='failed-reset', ip=self.ip)
            finally:
                session.close()
            self.assertEqual(self.row()['password_version'], 1)
            self.assertEqual(self.code_row('reset',self.user['email'])['status'], 'sent')
        self.service.recover('reset',self.user['email'],code,password='NewPassword9',request_id='retry-reset',ip=self.ip)

    def test_unknown_deleted_admin_frozen_and_provider_failure_have_generic_request_response(self):
        bodies = []
        for email in (uuid4().hex+'@example.test', self.user['email']):
            response = self.call('reset-password/request', {'email':email})
            self.assertEqual(response.status_code, 200)
            bodies.append(response.get_json()['detail'])
        self.assertEqual(bodies, [GENERIC_RESET, GENERIC_RESET])
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(status='locked', frozen_by='admin'))
        self.assertEqual(self.call('reset-password/request',{'email':self.user['email']}).get_json()['detail'],GENERIC_RESET)
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(status='active', frozen_by=None))
        with patch.object(self.service,'send_mail', side_effect=RuntimeError('PRIVATE_SMTP_CREDENTIAL')):
            response = self.call('reset-password/request', {'email':self.user['email']})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE_SMTP_CREDENTIAL',response.get_data(as_text=True))
        self.assertEqual(self.code_row('reset',self.user['email'])['status'],'failed')
        sent_count = len(self.mail)
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(
                status='deleted', deleted_at=sa.func.clock_timestamp()))
        response = self.call('reset-password/request', {'email':self.user['email']})
        self.assertEqual((response.status_code, response.get_json()['detail']), (200, GENERIC_RESET))
        self.assertEqual(len(self.mail), sent_count)

    def test_shared_email_cooldown_prevents_concurrent_sends(self):
        barrier = Barrier(2)
        def send(index):
            barrier.wait(5)
            try:
                self.new_service().request_code('reset',self.user['email'],ip=self.ip)
                return 200
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(send,range(2)))
        self.assertEqual(sorted(values),[200,429])
        self.assertEqual(len(self.mail),1)

    def test_provider_callback_can_revoke_account_and_leaves_code_unusable(self):
        def send(**message):
            self.mail.append(message)
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(password_version=2))
        self.service.send_mail = send
        self.assertEqual(self.call('reset-password/request',{'email':self.user['email']}).status_code,200)
        self.assertEqual(self.code_row('reset',self.user['email'])['status'],'failed')
        self.assertEqual(self.call('reset-password/confirm',{'email':self.user['email'],'code':self.mail[-1]['code'],
            'new_password':'NewPassword9'}).status_code,400)

    def test_delayed_old_delivery_cannot_activate_replacement_challenge(self):
        entered, release = Event(), Event()
        messages = []
        def delayed(**message):
            messages.append(message)
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Synthetic delivery timed out')
        self.service.send_mail = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.request_code, 'reset', self.user['email'], ip=self.ip)
            try:
                self.assertTrue(entered.wait(5))
                old_id = self.code_row('reset', self.user['email'])['id']
                with self.bootstrap.begin() as conn:
                    conn.execute(limits.update().where(limits.c.key == self.service.digest('mail_cooldown', self.user['email']))
                        .values(expires_at=sa.func.now()-sa.text("interval '1 second'")))
                replacement = self.new_service()
                replacement.send_mail = lambda **message: (_ for _ in ()).throw(RuntimeError('Synthetic send failure'))
                replacement.request_code('reset', self.user['email'], ip=self.ip)
                new_row = self.code_row('reset', self.user['email'])
                self.assertNotEqual(new_row['id'], old_id)
                self.assertEqual(new_row['status'], 'failed')
            finally:
                release.set()
            future.result(10)
        self.assertEqual(self.code_row('reset', self.user['email'])['status'], 'failed')
        with self.assertRaises(PublicAuthError):
            self.service.recover('reset', self.user['email'], messages[0]['code'], password='NewPassword9',
                request_id='late-code', ip=self.ip)

    def test_delivery_success_but_activation_commit_failure_keeps_code_pending(self):
        calls = 0
        def factory():
            nonlocal calls
            calls += 1
            session = self.sessions()
            if calls == 2:
                def fail(*args):
                    raise RuntimeError('Synthetic activation commit failure')
                event.listen(session, 'before_commit', fail)
            return session
        with patch.object(self.service, '_session_factory', side_effect=factory):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic activation commit failure'):
                self.service.request_code('reset', self.user['email'], ip=self.ip)
        self.assertEqual(len(self.mail), 1)
        self.assertEqual(self.code_row('reset', self.user['email'])['status'], 'pending')
        with self.assertRaises(PublicAuthError):
            self.service.recover('reset', self.user['email'], self.mail[-1]['code'], password='NewPassword9',
                request_id='pending-code', ip=self.ip)

    def test_reset_code_is_revoked_by_password_email_and_admin_freeze_changes(self):
        original = self.user['email']
        for values, email in [({'password_version':2}, original),
                              ({'email':'changed-'+original}, 'changed-'+original),
                              ({'status':'locked', 'frozen_by':'admin'}, original)]:
            with self.subTest(changes=sorted(values)):
                self.clear_state()
                with self.bootstrap.begin() as conn:
                    conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(
                        email=original, password_version=1, status='active', frozen_by=None))
                code = self.reset()
                self.assertEqual(self.code_row('reset', original)['status'], 'sent')
                with self.bootstrap.begin() as conn:
                    conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(**values))
                with self.assertRaises(PublicAuthError):
                    self.service.recover('reset', email, code, password='NewPassword9', request_id='revoked-code', ip=self.ip)
                self.assertTrue(verify_password('SyntheticPassword9', self.row()['password_hash'], self.row()['password_salt']))
        self.assertEqual(self.row()['status'], 'locked')

    def test_admin_unfreeze_discards_previous_shared_failure_window(self):
        now = None
        with self.bootstrap.begin() as conn:
            now = conn.scalar(sa.select(sa.func.clock_timestamp()))
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(
                status='locked', frozen_by='auto', login_consecutive_failures=6, login_window_failures=6))
            conn.execute(login_state.insert().values(user_id=UUID(self.user['user_id']),
                last_failure_at=now, failure_times=[now.timestamp()]*6))
        AccountService(self.sessions, admin_emails=self.admins).set_frozen(self.admin['user_id'],
            self.user['username'], False, **self.args)
        self.assertEqual(self.login('WrongPassword9').status_code, 401)
        self.assertEqual((self.row()['status'], self.row()['login_window_failures']), ('active',1))

    def test_expired_captcha_and_email_code_are_rejected(self):
        secure = self.new_service(captcha_enabled=True)
        identity, answer = secure.captcha(ip=self.ip)
        code = self.reset()
        with self.bootstrap.begin() as conn:
            conn.execute(challenges.update().values(expires_at=sa.func.now()-sa.text("interval '1 second'")))
        with self.assertRaises(PublicAuthError):
            secure.login(self.user['username'], 'SyntheticPassword9', ip=self.ip, request_id='expired-captcha',
                captcha_id=identity, captcha_code=answer)
        with self.assertRaises(PublicAuthError):
            self.service.recover('reset', self.user['email'], code, password='NewPassword9', request_id='expired-email', ip=self.ip)

    def test_login_limit_is_shared_across_instances_and_ignores_spoofed_forwarded_ip(self):
        other_client = self.make_app(self.new_service()).test_client()
        for i in range(10):
            response = self.call('login',{'username':'absent-user','password':'Password9'},
                client=other_client if i%2 else None, headers={'X-Forwarded-For':'192.0.2.'+str(i+1)})
            self.assertEqual(response.status_code,401)
        response = self.call('login',{'username':'absent-user','password':'Password9'},client=other_client)
        self.assertEqual(response.status_code,429)
        self.assertGreater(int(response.headers['Retry-After']),0)

    def test_cooldown_expiry_starts_new_streak_then_auto_freeze_revokes_token(self):
        token = self.login().get_json()['data']['token']
        for _ in range(3):
            self.assertEqual(self.login('WrongPassword9').status_code,401)
        self.assertEqual(self.login().status_code,429)
        with self.bootstrap.begin() as conn:
            now = conn.scalar(sa.select(sa.func.clock_timestamp()))
            conn.execute(login_state.update().where(login_state.c.user_id==UUID(self.user['user_id'])).values(
                last_failure_at=now-timedelta(seconds=301),failure_times=[now.timestamp()-301]*3))
        for expected in (401,401,423):
            self.assertEqual(self.login('WrongPassword9').status_code,expected)
        row = self.row()
        self.assertEqual((row['status'],row['frozen_by'],row['password_version']),('locked','auto',2))
        self.assertEqual(self.call('me',method='get',headers={'Authorization':'Bearer '+token}).status_code,401)
        events = self.audit('auth.auto_freeze',self.user)
        self.assertEqual(len(events),1)
        self.assertIsNone(events[0]['actor_user_id'])

    def test_parallel_login_failures_do_not_lose_counts(self):
        barrier = Barrier(3)
        def fail(index):
            barrier.wait(5)
            try:
                self.new_service().login(self.user['username'],'WrongPassword9',ip='192.0.2.'+str(index+1),request_id='failed-login')
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(list(pool.map(fail,range(3))),[401]*3)
        self.assertEqual(self.row()['login_consecutive_failures'],3)
        self.assertEqual(len(self.audit('auth.login_failed',self.user)),3)

    def test_captcha_is_one_time_shared_and_http_never_returns_answer(self):
        secure = self.new_service(captcha_enabled=True)
        client = self.make_app(secure).test_client()
        self.assertEqual(self.call('login',{'username':self.user['username'],'password':'SyntheticPassword9'},client=client).status_code,422)
        with patch('captcha_image.render_data_uri',return_value='data:image/png;base64,synthetic') as render:
            response = self.call('captcha',method='get',client=client)
        data = response.get_json()['data']
        self.assertEqual(set(data),{'captcha_id','image','ttl_seconds'})
        code = render.call_args.args[0]
        body = dict(username=self.user['username'],password='SyntheticPassword9',captcha_id=data['captcha_id'],captcha_code=code)
        self.assertEqual(self.call('login',body,client=client).status_code,200)
        self.assertEqual(self.call('login',body,client=client).status_code,422)
        identity, answer = secure.captcha(ip=self.ip)
        with self.assertRaises(PublicAuthError):
            self.new_service(captcha_enabled=True).login(self.user['username'],'SyntheticPassword9',ip=self.ip,
                request_id='wrong-captcha',captcha_id=identity,captcha_code='wrong')
        with self.assertRaises(PublicAuthError):
            secure.login(self.user['username'],'SyntheticPassword9',ip=self.ip,request_id='used-captcha',
                         captcha_id=identity,captcha_code=answer)

    def test_auto_frozen_self_unfreeze_needs_password_and_bound_code_admin_freeze_cannot_bypass(self):
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(status='locked',frozen_by='auto',password_version=2))
        self.assertEqual(self.call('unfreeze/send-code',{'username':self.user['username'],'email':self.user['email']}).status_code,200)
        code = self.mail[-1]['code']
        data = dict(username=self.user['username'],email=self.user['email'],code=code,password='WrongPassword9')
        self.assertEqual(self.call('unfreeze',data).status_code,401)
        self.assertEqual(self.code_row('unfreeze',self.user['email'])['status'],'sent')
        data['password']='SyntheticPassword9'
        response=self.call('unfreeze',data)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()['data']['token'])
        self.assertEqual((self.row()['status'],self.row()['password_version']),('active',3))
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id==UUID(self.user['user_id'])).values(status='locked',frozen_by='admin'))
        self.assertEqual(self.call('unfreeze',data).status_code,400)

    def test_limit_capacity_fails_closed_and_expired_rows_are_reclaimed(self):
        small = self.new_service(max_keys=10)
        for i in range(10):
            small.check_request('login', '192.0.2.'+str(i+1))
        with self.assertRaises(PublicAuthError) as caught:
            small.check_request('login','192.0.2.100')
        self.assertEqual(caught.exception.status,503)
        with self.bootstrap.begin() as conn:
            conn.execute(limits.update().values(expires_at=sa.func.now()-sa.text("interval '1 second'")))
        small.check_request('login','192.0.2.100')
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(limits)),1)

    def test_drain_waits_for_inflight_mail_and_refuses_new_auth_admission(self):
        entered,release=Event(),Event()
        def send(**message):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Synthetic mail timed out')
            self.mail.append(message)
        self.service.send_mail=send
        tracker=self.app.extensions['db_drain_tracker']
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(lambda:self.call('reset-password/request',{'email':self.user['email']},client=self.app.test_client()))
            try:
                self.assertTrue(entered.wait(5))
                tracker.begin()
                self.assertFalse(tracker.wait(0))
                self.assertEqual(self.call('register-config',method='get').status_code,503)
            finally:
                release.set()
            self.assertEqual(future.result(10).status_code,200)
        self.assertTrue(tracker.wait(1))

    def test_secret_codes_and_subjects_are_hmac_only_in_runtime_tables(self):
        body=self.registration()
        with self.engine.connect() as conn:
            stored=[dict(row) for table in (challenges,invites,limits) for row in conn.execute(sa.select(table)).mappings()]
        text=str(stored)
        for value in (body['email'],body['code'],body['invite_code'],body['password'],self.ip):
            self.assertNotIn(value,text)
        digest=self.service.digest('code:register',body['email']+'\0'+body['code'])
        self.assertEqual(self.code_row('register',body['email'])['code_hash'],digest)
