"""Account lifecycle transactions, revocation and concurrent callbacks on PG."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, update

from db_accounts import AccountError
from db_config_resolved import MASK, environment_snapshot
from db_models import User, Resume
from test_db_accounts_live import AccountsLiveFixture


class AccountLifecycleLiveTests(AccountsLiveFixture):
    def path(self, action='', user=None):
        return '/api/v1/admin/users/' + (user or self.member)['username'] + ('/' + action if action else '')

    def row(self, user=None):
        with self.sessions() as session:
            return session.execute(select(User.__table__).where(User.id == (user or self.member)['user_id'])).mappings().one()

    def test_freeze_unfreeze_revokes_old_tokens_and_replay_does_not_repeat_audit(self):
        frozen = self.client.post(self.path('freeze'), json={'reason':'synthetic review'}, headers=self.admin_headers)
        self.assertEqual(frozen.status_code, 200)
        row = self.row()
        self.assertEqual((row['status'], row['password_version'], row['frozen_by']), ('locked',2,'admin'))
        self.assertIsNotNone(row['frozen_at'])
        self.assertEqual(self.client.get('/api/v1/auth/me', headers=self.member_headers).status_code, 401)
        self.assertIsNone(self.accounts.authenticate_user(self.member['username'], self.password))
        self.assertEqual(self.client.post(self.path('reset-password'), headers=self.owner_headers).status_code, 423)
        self.assertEqual(self.client.post(self.path('freeze'), json={}, headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.row()['password_version'], 2)
        items = self.client.get('/api/v1/admin/users/frozen', headers=self.admin_headers).get_json()['data']['items']
        entry = next(item for item in items if item['username']==self.member['username'])
        self.assertEqual(entry['frozen_reason'], 'synthetic review')
        self.assertNotIn('password', str(entry))
        result = self.client.post(self.path('unfreeze'), headers=self.admin_headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual((self.row()['status'],self.row()['password_version']), ('active',3))
        self.assertEqual(self.client.get('/api/v1/auth/me', headers=self.member_headers).status_code, 401)
        self.assertEqual(self.client.post(self.path('unfreeze'), headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.row()['password_version'], 3)
        self.assertEqual(len(self.audit(frozen.get_json()['request_id'])), 1)
        self.assertEqual(len(self.audit(result.get_json()['request_id'])), 1)

    def test_no_self_lockout_and_ordinary_admin_cannot_target_admin_or_restore(self):
        for action in ('freeze','unfreeze'):
            self.assertEqual(self.client.post(self.path(action, self.owner), headers=self.owner_headers).status_code, 400)
            self.assertEqual(self.client.post(self.path(action, self.owner), headers=self.admin_headers).status_code, 403)
        self.assertEqual(self.client.delete(self.path(user=self.owner), headers=self.owner_headers,
                                           json={'admin_password':self.password}).status_code, 400)
        self.assertEqual(self.client.delete(self.path(), headers=self.admin_headers,
                                           json={'admin_password':self.password}).status_code, 403)
        self.assertEqual(self.client.post(self.path('restore'), headers=self.admin_headers).status_code, 403)
        self.assertEqual(self.client.get('/api/v1/admin/users/deleted', headers=self.admin_headers).status_code, 403)

    def test_delete_requires_current_actor_password_and_retains_identity(self):
        resume_id=uuid4()
        with self.sessions() as session,session.begin():
            session.execute(Resume.__table__.insert().values(id=resume_id,owner_user_id=self.member['user_id']))
        for payload,status in [({},422),({'admin_password':'incorrect'},403),({'admin_password':[]},422)]:
            self.assertEqual(self.client.delete(self.path(), json=payload, headers=self.owner_headers).status_code,status)
            self.assertIsNone(self.row()['deleted_at'])
        result=self.client.delete(self.path(),json={'admin_password':self.password},headers=self.owner_headers)
        self.assertEqual(result.status_code,200)
        row=self.row()
        self.assertEqual(row['deletion_previous_status'],'active')
        self.assertEqual(row['deleted_by'],self.owner['user_id'])
        self.assertEqual(row['status'],'deleted')
        self.assertEqual(self.client.get('/api/v1/auth/me',headers=self.member_headers).status_code,401)
        self.assertIsNone(self.accounts.authenticate_user(self.member['username'],self.password))
        deleted=self.client.get('/api/v1/admin/users/deleted',headers=self.owner_headers).get_json()['data']['items']
        self.assertIn(self.member['username'],[item['username'] for item in deleted])
        self.assertNotIn('password',str(deleted))
        # Retained unique keys block identity reuse while the account is deleted.
        response=self.client.post('/api/v1/admin/users',headers=self.owner_headers,
            json={'username':self.member['username'],'password':'SyntheticPassword9'})
        self.assertEqual(response.status_code,409)
        with self.sessions() as session:
            self.assertEqual(str(session.scalar(select(Resume.owner_user_id).where(Resume.id==resume_id))),self.member['user_id'])
        self.assertEqual(len(self.audit(result.get_json()['request_id'])),1)

    def test_restore_preserves_freeze_and_never_reactivates_old_tokens(self):
        self.assertEqual(self.client.post(self.path('freeze'),headers=self.admin_headers).status_code,200)
        self.assertEqual(self.client.delete(self.path(),json={'admin_password':self.password},headers=self.owner_headers).status_code,200)
        result=self.client.post(self.path('restore'),headers=self.owner_headers)
        self.assertEqual(result.status_code,200)
        row=self.row()
        self.assertEqual((row['status'],row['password_version']),('locked',4))
        self.assertIsNotNone(row['frozen_at'])
        self.assertIsNone(row['deleted_at'])
        self.assertIsNone(row['deletion_previous_status'])
        self.assertEqual(self.client.get('/api/v1/auth/me',headers=self.member_headers).status_code,401)
        self.assertEqual(self.client.post(self.path('restore'),headers=self.owner_headers).status_code,400)
        self.assertEqual(len(self.audit(result.get_json()['request_id'])),1)

    def test_expired_future_and_unknown_historical_state_refuse_restore(self):
        self.client.delete(self.path(),json={'admin_password':self.password},headers=self.owner_headers)
        for changes in ({'deleted_at':datetime.now(timezone.utc)-timedelta(days=91)},
                        {'deleted_at':datetime.now(timezone.utc)+timedelta(days=1)},
                        {'deleted_at':datetime.now(timezone.utc),'deletion_previous_status':None}):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id==self.member['user_id']).values(**changes))
            self.assertEqual(self.client.post(self.path('restore'),headers=self.owner_headers).status_code,400)
            self.assertEqual(self.row()['status'],'deleted')

    def test_audit_failures_roll_back_every_transition(self):
        actions=[('freeze','POST',None),('','DELETE',{'admin_password':self.password}),
                 ('unfreeze','POST',None),('restore','POST',None)]
        for action,method,payload in actions:
            before=self.row()
            with patch('db_accounts.append_audit',side_effect=RuntimeError('private failure')):
                result=self.client.open(self.path(action),method=method,json=payload,headers=self.owner_headers)
            self.assertEqual(result.status_code,503)
            self.assertNotIn('private failure',result.get_data(as_text=True))
            self.assertEqual(dict(self.row()),dict(before))
            if action=='':
                self.client.post(self.path('freeze'),headers=self.owner_headers)
            elif action=='unfreeze':
                self.client.delete(self.path(),json={'admin_password':self.password},headers=self.owner_headers)

    def test_concurrent_deletes_do_not_extend_window_and_commit_one_audit(self):
        gate=Barrier(2)
        requests=['delete-'+uuid4().hex for _ in range(2)]
        def run(request_id):
            gate.wait(timeout=10)
            return self.accounts.delete_account(self.owner['user_id'],self.member['username'],self.password,
                                                password_version=1,request_id=request_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,requests))
        self.assertEqual(len(results),2)
        self.assertEqual(self.row()['password_version'],2)
        self.assertEqual(sum(len(self.audit(key)) for key in requests),1)

    def test_actor_revoked_after_http_gate_cannot_freeze_target(self):
        original=self.accounts.set_frozen
        def revoked(*args,**kwargs):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id==self.admin['user_id']).values(is_admin=False))
            return original(*args,**kwargs)
        with patch.object(self.accounts,'set_frozen',side_effect=revoked):
            self.assertEqual(self.client.post(self.path('freeze'),headers=self.admin_headers).status_code,403)
        self.assertEqual(self.row()['status'],'active')

    def test_complete_config_contract_metadata_and_clear_to_explicit_environment(self):
        self.configs.environment=environment_snapshot({
            'mail':{'host':'env.smtp.test','username':'env-sender','password':'SyntheticEnvMail'},
            'llm':{'api_key':'SyntheticEnvModel','base_url':'https://env.example.test','model':'env-model'}})
        mail='/api/v1/admin/system/mail'
        body={'slots':{'transactional':{'host':'ui.smtp.test','username':'ui-sender','password':'SyntheticUISMTP'}}}
        response=self.client.put(mail,json=body,headers=self.owner_headers)
        self.assertEqual(response.status_code,200)
        view=response.get_json()['data']
        self.assertEqual(view['slots']['notification']['source'],'ui.transactional')
        self.assertEqual(view['slots']['transactional']['password_mask'],MASK)
        self.assertEqual(view['updated_by'],self.owner['username'])
        self.assertTrue(view['updated_at'])
        self.assertEqual(self.client.get(mail,headers=self.owner_headers).get_json()['data'],view)
        cleared=self.client.delete(mail,headers=self.owner_headers).get_json()['data']
        self.assertEqual(cleared['slots']['notification']['source'],'env')
        self.assertTrue(cleared['env_available'])
        self.assertEqual(cleared['updated_at'],'')
        llm='/api/v1/admin/system/llm'
        response=self.client.put(llm,headers=self.owner_headers,
            json={'api_key':'SyntheticUIModel','base_url':'https://api.example.test','model':'ui-model','updated_by':'forged'})
        self.assertEqual(response.status_code,200)
        view=response.get_json()['data']
        self.assertEqual(view['source'],'ui')
        self.assertEqual(view['models'],[])
        self.assertTrue(view['ready'])
        self.assertEqual(view['updated_by'],self.owner['username'])
        cleared=self.client.delete(llm,headers=self.owner_headers).get_json()['data']
        self.assertEqual((cleared['source'],cleared['model']),('env','env-model'))
        self.assertNotIn('Synthetic',str(cleared))
        from cryptography.fernet import Fernet
        from db_secrets import ConfigCipher
        self.configs.cipher=ConfigCipher(Fernet.generate_key(),key_id=self.cipher.key_id)
        self.assertEqual(self.client.get(llm,headers=self.owner_headers).status_code,503)
