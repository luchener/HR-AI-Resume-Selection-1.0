"""Guarded PostgreSQL invite workflow and captured mail, never real providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
import os
from threading import Barrier, Event
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from sqlalchemy import event
import sqlalchemy as sa

from db_auth import AuthService
from db_check_schema import metadata
from db_http_preview import create_preview_app
from db_invite_cipher import InviteCipher
from db_invite_state import requests
from db_invites import InviteService
from db_models import User
from db_public_auth import PublicAuthError, PublicAuthService
from db_public_auth_state import TABLES, invites, challenges, limits
from db_runtime import build_runtime, verify_runtime
from db_security import migration_engine
from db_store import Store
from db_test_guard import review_test_url


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class InviteLiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.bootstrap = migration_engine(url.render_as_string(hide_password=False))
        cls.engine, cls.sessions = build_runtime(url.set(username='resume_app',password='review-app-only')
                                                .render_as_string(hide_password=False))
        verify_runtime(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.bootstrap.dispose()

    def setUp(self):
        self.auth = AuthService(self.sessions)
        self.owner, _ = self.auth.create_user('owner-'+uuid4().hex,'SyntheticPassword9',uuid4().hex+'@example.test')
        self.member, _ = self.auth.create_user('member-'+uuid4().hex,'SyntheticPassword9',uuid4().hex+'@example.test')
        self.mail = []
        self.public = PublicAuthService(self.sessions, secret=b'Synthetic-shared-invite-HMAC-key-only',
            send_mail=lambda **m:self.mail.append(m), admin_emails=(self.owner['email'],), captcha_enabled=False)
        self.cipher_key = Fernet.generate_key()
        self.cipher = InviteCipher(self.cipher_key)
        self.service = self.new_service()
        self.app = create_preview_app(Store(session_factory=self.sessions), self.auth,
            'synthetic-invite-jwt-key-at-least-48-bytes-for-tests', admin_emails=(self.owner['email'],),
            public_auth=self.public, invite_service=self.service)
        self.client = self.app.test_client()
        self.ip = '2001:db8::'+uuid4().hex[-4:]
        self.args = dict(actor_id=self.owner['user_id'],password_version=1,request_id='invite-'+uuid4().hex)
        self.addCleanup(self.clear)

    def new_service(self, **options):
        return InviteService(self.public, cipher=self.cipher, send_mail=lambda **m:self.mail.append(m), **options)

    def clear(self):
        with self.bootstrap.begin() as conn:
            conn.execute(requests.delete())
            for table in reversed(TABLES):
                conn.execute(table.delete())

    def call(self, path, data=None, *, method='post', token=None, client=None):
        return getattr(client or self.client,method)(path, json=data,
            headers={'Authorization':'Bearer '+token} if token else {}, environ_base={'REMOTE_ADDR':self.ip})

    def token(self, user=None):
        user = user or self.owner
        response = self.call('/api/v1/auth/login',dict(username=user['username'],password='SyntheticPassword9'))
        self.assertEqual(response.status_code,200)
        return response.get_json()['data']['token']

    def submit(self, email=None, note='Synthetic invitation reason'):
        email = email or uuid4().hex+'@example.test'
        self.service.submit(email,note,ip=self.ip,request_id='submit-'+uuid4().hex)
        with self.engine.connect() as conn:
            identity = conn.scalar(sa.select(requests.c.id).where(requests.c.email_hash==self.public.digest('email',email),
                                                                 requests.c.status=='pending'))
        return identity, email

    def row(self, identity):
        with self.engine.connect() as conn:
            return conn.execute(sa.select(requests).where(requests.c.id==identity)).mappings().one()

    def codes(self):
        with self.engine.connect() as conn:
            return conn.execute(sa.select(invites)).mappings().all()

    def count_audit(self, action):
        table = metadata().tables['audit_logs']
        with self.engine.connect() as conn:
            return conn.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.action=='invite.'+action,
                table.c.actor_user_id==UUID(self.owner['user_id'])))

    def approve(self, identity):
        return self.service.review(**self.args,identity=identity,action='approve')

    def test_http_contract_complete_workflow_lists_mask_and_registers(self):
        email = uuid4().hex+'@example.test'
        response = self.call('/api/v1/invite-request',dict(email=email,note='Synthetic reason'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['data']['email'],email)
        token = self.token()
        listed = self.call('/api/v1/admin/invite-requests',method='get',token=token).get_json()['data']
        item = listed['items'][0]
        self.assertEqual(set(item),{'request_id','email','note','status','created_at','reviewed_at','reject_reason','code_sent','rejected_count'})
        self.assertNotIn(email,str(item))
        identity = item['request_id']
        response = self.call('/api/v1/admin/invite-requests/'+identity+'/approve',token=token)
        self.assertEqual(response.status_code,200)
        code = response.get_json()['data']['invite_code']
        self.assertTrue(response.get_json()['data']['code_sent'])
        self.assertEqual(self.mail[-1]['email'],email)
        self.public.request_code('register',email,ip=self.ip,invite_code=code)
        user = self.public.register('new-'+uuid4().hex,'SyntheticPassword9',email,self.mail[-1]['code'],code,
            ip=self.ip,request_id='register-approved')
        codes = self.call('/api/v1/admin/invite-codes?status=used',method='get',token=token).get_json()['data']
        self.assertEqual(codes['items'][0]['used_by_username'],user['username'])
        self.assertNotIn(code,str(codes))
        self.assertEqual(self.count_audit('approve'),1)

    def test_roles_rechecked_and_anonymous_nonwhitelisted_admin_denied(self):
        identity, _ = self.submit()
        path = '/api/v1/admin/invite-requests/'+str(identity)+'/approve'
        self.assertEqual(self.call(path).status_code,401)
        with self.bootstrap.begin() as conn:
            conn.execute(sa.update(User).where(User.id==UUID(self.member['user_id'])).values(is_admin=True))
        self.assertEqual(self.call(path,token=self.token(self.member)).status_code,403)
        original = self.service.review
        def revoke_then_review(*args,**kwargs):
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(User).where(User.id==UUID(self.owner['user_id'])).values(password_version=2))
            return original(*args,**kwargs)
        token = self.token()
        with patch.object(self.service,'review',side_effect=revoke_then_review):
            self.assertEqual(self.call(path,token=token).status_code,401)
        self.assertEqual(self.row(identity)['status'],'pending')
        self.assertEqual(len(self.codes()),0)

    def test_shared_application_limits_duplicate_and_pending_unique_constraint(self):
        email, barrier = uuid4().hex+'@example.test', Barrier(2)
        def submit(index):
            barrier.wait(5)
            try:
                return self.new_service().submit(email,'Synthetic reason',ip=self.ip,request_id='parallel-submit')
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(submit,range(2)))
        self.assertEqual(sum(isinstance(v,dict) for v in values),1)
        self.assertIn(429,values)
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
        with self.assertRaises(PublicAuthError) as caught:
            self.service.submit(email,'Synthetic reason',ip=self.ip,request_id='duplicate')
        self.assertEqual(caught.exception.status,409)
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError), self.bootstrap.begin() as conn:
            conn.execute(requests.insert().values(id=uuid4(),email_hash=self.public.digest('email',email),sealed={}))

    def test_rejection_count_limit_and_best_effort_rejection_mail(self):
        email = uuid4().hex+'@example.test'
        for index in range(3):
            with self.bootstrap.begin() as conn:
                conn.execute(limits.delete())
            identity,_ = self.submit(email)
            if index == 1:
                with patch.object(self.service,'send_mail',side_effect=RuntimeError('PRIVATE_SMTP_PASSWORD')):
                    result = self.service.review(**self.args,identity=identity,action='reject',reason='Synthetic rejection')
            else:
                result = self.service.review(**self.args,identity=identity,action='reject',reason='Synthetic rejection')
            self.assertEqual(result['rejected_total'],index+1)
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
        with self.assertRaises(PublicAuthError) as caught:
            self.submit(email)
        self.assertEqual(caught.exception.status,429)
        self.assertEqual(self.count_audit('reject'),3)

    def test_concurrent_approve_reject_has_single_winner_and_single_invitation(self):
        identity, _ = self.submit()
        barrier = Barrier(2)
        def review(action):
            barrier.wait(5)
            try:
                return self.new_service().review(**self.args,identity=identity,action=action)
            except PublicAuthError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(review,['approve','reject']))
        self.assertEqual(sum(isinstance(v,dict) for v in results),1)
        self.assertIn(409,results)
        self.assertEqual(len(self.codes()),int(self.row(identity)['status']=='approved'))
        self.assertEqual(self.count_audit('approve')+self.count_audit('reject'),1)

    def test_approval_audit_and_commit_faults_roll_back_and_send_nothing(self):
        identity, _ = self.submit()
        for failure in ('audit','commit'):
            session = self.sessions()
            def fail(*args,**kwargs):
                raise RuntimeError('Synthetic transaction failure')
            if failure == 'commit':
                event.listen(session,'before_commit',fail)
            try:
                with patch.object(self.public,'_session_factory',return_value=session):
                    with patch('db_invites.append_audit',side_effect=fail) if failure=='audit' else nullcontext():
                        with self.assertRaises(RuntimeError):
                            self.approve(identity)
            finally:
                session.close()
            self.assertEqual(self.row(identity)['status'],'pending')
            self.assertEqual(len(self.codes()),0)
            self.assertEqual(self.mail,[])
        self.assertTrue(self.approve(identity)['code_sent'])

    def test_batch_generation_collision_and_audit_failures_have_no_partial_codes(self):
        self.service.generate(**self.args)
        before = self.codes()[0]['code_hash']
        original = self.service._allocate
        calls = 0
        def allocate(*args,**kwargs):
            nonlocal calls
            calls += 1
            return original(*args,**kwargs) if calls==1 else PublicAuthError('Synthetic collision',503)
        with patch.object(self.service,'_allocate',side_effect=allocate):
            with self.assertRaises(PublicAuthError):
                self.service.generate(**self.args,count=2)
        self.assertEqual([r['code_hash'] for r in self.codes()],[before])
        with patch('db_invites.append_audit',side_effect=RuntimeError('Synthetic audit fault')):
            with self.assertRaises(RuntimeError):
                self.service.generate(**self.args,count=2)
        self.assertEqual([r['code_hash'] for r in self.codes()],[before])

    def test_unbound_manual_code_claim_is_atomic_and_does_not_change_for_invalid_account(self):
        code = self.service.generate(**self.args)['codes'][0]
        with self.assertRaises(PublicAuthError):
            self.public.request_code('register',self.owner['email'],ip=self.ip,invite_code=code)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',''))
        first, second, barrier = uuid4().hex+'@example.test',uuid4().hex+'@example.test',Barrier(2)
        def claim(email):
            barrier.wait(5)
            try:
                self.public.request_code('register',email,ip=self.ip,invite_code=code)
                return email
            except PublicAuthError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim,[first,second]))
        self.assertEqual(sum(value is not None for value in results),1)
        winner = next(value for value in results if value)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',winner))

    def test_unbound_claim_refusals_and_commit_failure_do_not_bind_code(self):
        code = self.service.generate(**self.args)['codes'][0]
        email = uuid4().hex+'@example.test'
        self.public.request_code('reset',email,ip=self.ip)
        with self.assertRaises(PublicAuthError) as caught:
            self.public.request_code('register',email,ip=self.ip,invite_code=code)
        self.assertEqual(caught.exception.status,429)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',''))
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
            conn.execute(challenges.insert().values(purpose='captcha',subject='a'*64,id=uuid4(),
                code_hash='b'*64,status='sent',expires_at=sa.func.now()+sa.text("interval '1 hour'")))
        with patch.object(self.public,'max_keys',1), self.assertRaises(PublicAuthError) as caught:
            self.public.request_code('register',email,ip=self.ip,invite_code=code)
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',''))
        with self.bootstrap.begin() as conn:
            conn.execute(limits.delete())
            conn.execute(challenges.delete())
        session = self.sessions()
        def fail(*args,**kwargs):
            raise RuntimeError('Synthetic claim commit failure')
        event.listen(session,'before_commit',fail)
        with patch.object(self.public,'_session_factory',return_value=session), self.assertRaises(RuntimeError):
            self.public.request_code('register',email,ip=self.ip,invite_code=code)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',''))
        self.assertEqual(self.mail,[])
        self.public.request_code('register',email,ip=self.ip,invite_code=code)
        self.assertEqual(self.codes()[0]['email_hash'],self.public.digest('email',email))

    def test_recreated_services_with_restored_keys_keep_records_and_shared_limits(self):
        identity,email = self.submit()
        code = self.approve(identity)['invite_code']
        restored_auth = PublicAuthService(self.sessions,secret=b'Synthetic-shared-invite-HMAC-key-only',
            send_mail=lambda **m:self.mail.append(m),admin_emails=(self.owner['email'],),captcha_enabled=False)
        restored = InviteService(restored_auth,cipher=InviteCipher(self.cipher_key),send_mail=lambda **m:None)
        listed = restored.list_requests(self.owner['user_id'],password_version=1,status='approved')
        self.assertEqual(listed['items'][0]['request_id'],str(identity))
        self.assertTrue(restored_auth.check_invite(code,ip=self.ip))
        with self.assertRaises(PublicAuthError) as caught:
            restored.submit(email,'Synthetic reason',ip=self.ip,request_id='after-key-restore')
        self.assertEqual(caught.exception.status,429)
        restored_auth.request_code('register',email,ip=self.ip,invite_code=code)
        self.assertEqual(self.mail[-1]['email'],email)

    def test_revoke_invalidates_sent_registration_code_and_is_idempotent(self):
        identity, email = self.submit()
        code = self.approve(identity)['invite_code']
        self.public.request_code('register',email,ip=self.ip,invite_code=code)
        answer = self.mail[-1]['code']
        key = self.public.digest('invite',code)
        self.service.revoke(**self.args,code_or_hash=key)
        self.service.revoke(**self.args,code_or_hash=code)
        with self.assertRaises(PublicAuthError):
            self.public.register('new-'+uuid4().hex,'SyntheticPassword9',email,answer,code,ip=self.ip,request_id='revoked')
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(sa.select(challenges.c.status).where(challenges.c.invite_hash==key)),'failed')
        self.assertEqual(self.count_audit('revoke'),1)
        self.assertFalse(self.row(identity)['code_sent'])

    def test_resend_replaces_code_old_code_stays_revoked_and_current_only_sent(self):
        identity,_ = self.submit()
        old = self.approve(identity)['invite_code']
        new = self.service.review(**self.args,identity=identity,action='resend')['invite_code']
        self.assertNotEqual(old,new)
        with self.assertRaises(PublicAuthError):
            self.public.check_invite(old,ip=self.ip)
        self.assertTrue(self.public.check_invite(new,ip=self.ip))
        self.assertTrue(self.row(identity)['code_sent'])
        with self.assertRaises(PublicAuthError):
            self.service.review(**self.args,identity=identity,action='resend')

    def test_send_failure_returns_once_plain_code_without_provider_error(self):
        identity,_ = self.submit()
        with patch.object(self.service,'send_mail',side_effect=RuntimeError('PRIVATE_SMTP_PASSWORD')):
            result = self.approve(identity)
        self.assertFalse(result['code_sent'])
        self.assertTrue(self.public.check_invite(result['invite_code'],ip=self.ip))
        self.assertNotIn('PRIVATE_SMTP_PASSWORD',str(result))
        self.assertFalse(self.row(identity)['code_sent'])

    def test_revoke_during_delivery_does_not_reactivate_or_return_code(self):
        identity,_ = self.submit()
        def deliver(**message):
            self.mail.append(message)
            self.new_service().revoke(**self.args,code_or_hash=message['code'])
        self.service.send_mail=deliver
        with self.assertRaises(PublicAuthError) as caught:
            self.approve(identity)
        self.assertEqual(caught.exception.status,409)
        self.assertFalse(self.row(identity)['code_sent'])
        with self.assertRaises(PublicAuthError):
            self.public.check_invite(self.mail[-1]['code'],ip=self.ip)

    def test_permission_revoked_during_delivery_revokes_invite_and_hides_secret(self):
        identity,_ = self.submit()
        def deliver(**message):
            self.mail.append(message)
            with self.bootstrap.begin() as conn:
                conn.execute(sa.update(User).where(User.id==UUID(self.owner['user_id'])).values(password_version=2))
        self.service.send_mail=deliver
        with self.assertRaises(PublicAuthError) as caught:
            self.approve(identity)
        self.assertEqual(caught.exception.status,401)
        self.assertIsNotNone(self.codes()[0]['revoked_at'])

    def test_activation_commit_failure_stays_pending_and_explicit_resend_recovers(self):
        identity,_ = self.submit()
        calls = 0
        def factory():
            nonlocal calls
            calls += 1
            session = self.sessions()
            if calls == 2:
                def fail(*args,**kwargs):
                    raise RuntimeError('Synthetic activation commit failure')
                event.listen(session,'before_commit',fail)
            return session
        with patch.object(self.public,'_session_factory',side_effect=factory):
            with self.assertRaises(RuntimeError):
                self.approve(identity)
        old=self.mail[-1]['code']
        self.assertTrue(self.codes()[0]['delivery_pending'])
        with self.assertRaises(PublicAuthError):
            self.public.check_invite(old,ip=self.ip)
        with self.bootstrap.begin() as conn:
            conn.execute(invites.update().values(created_at=sa.func.now()-sa.text("interval '61 seconds'")))
        result=self.new_service().review(**self.args,identity=identity,action='resend')
        self.assertTrue(result['code_sent'])
        self.assertTrue(self.public.check_invite(result['invite_code'],ip=self.ip))
        with self.assertRaises(PublicAuthError):
            self.public.check_invite(old,ip=self.ip)

    def test_old_delivery_cannot_mark_new_replacement_sent(self):
        identity,_=self.submit()
        def old_send(**message):
            self.mail.append(message)
            with self.bootstrap.begin() as conn:
                conn.execute(invites.update().values(created_at=sa.func.now()-sa.text("interval '61 seconds'")))
            replacement=self.new_service()
            replacement.send_mail=lambda **m: (_ for _ in ()).throw(RuntimeError('Synthetic send failure'))
            self.replacement=replacement.review(**self.args,identity=identity,action='resend')['invite_code']
        self.service.send_mail=old_send
        with self.assertRaises(PublicAuthError):
            self.approve(identity)
        self.assertFalse(self.row(identity)['code_sent'])
        self.assertTrue(self.public.check_invite(self.replacement,ip=self.ip))
        with self.assertRaises(PublicAuthError):
            self.public.check_invite(self.mail[0]['code'],ip=self.ip)

    def test_encrypted_records_wrong_key_swap_and_no_secret_audit(self):
        identity,email=self.submit(note='Private synthetic application reason')
        code=self.approve(identity)['invite_code']
        stored=str(dict(self.row(identity)))+str([dict(r) for r in self.codes()])
        for value in [email,code,'Private synthetic application reason']:
            self.assertNotIn(value,stored)
        wrong=InviteService(self.public,cipher=InviteCipher(Fernet.generate_key()),send_mail=lambda **m:None)
        with self.assertRaisesRegex(ValueError,'authentication failed'):
            wrong.list_requests(self.owner['user_id'],password_version=1,status='approved')
        table=metadata().tables['audit_logs']
        with self.engine.connect() as conn:
            audit=str([dict(r) for r in conn.execute(sa.select(table).where(table.c.actor_user_id==UUID(self.owner['user_id']))).mappings()])
        for value in [email,code,'Private synthetic application reason']:
            self.assertNotIn(value,audit)

    def test_http_bad_inputs_and_drain_tracks_inflight_invitation_mail(self):
        token=self.token()
        for body in [dict(count=True),dict(count=21),dict(count=1,email=[]),dict(note='x\0y')]:
            self.assertEqual(self.call('/api/v1/admin/invite-codes/generate',body,token=token).status_code,422)
        identity,_=self.submit()
        entered,release=Event(),Event()
        def deliver(**message):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Synthetic send wait timeout')
        self.service.send_mail=deliver
        tracker=self.app.extensions['db_drain_tracker']
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(lambda:self.call('/api/v1/admin/invite-requests/'+str(identity)+'/approve',
                token=token,client=self.app.test_client()))
            try:
                self.assertTrue(entered.wait(5))
                tracker.begin()
                self.assertFalse(tracker.wait(0))
                self.assertEqual(self.call('/api/v1/invite-request',dict(email='x@example.test',note='reason')).status_code,503)
            finally:
                release.set()
            self.assertEqual(future.result(10).status_code,200)
        self.assertTrue(tracker.wait(1))
