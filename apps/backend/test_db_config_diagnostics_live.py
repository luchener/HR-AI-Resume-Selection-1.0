"""Frontend diagnostic endpoints with synthetic callbacks, never providers."""
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from db_config_diagnostics import ConfigDiagnostics
from db_config_resolved import environment_snapshot
from db_http_preview import create_preview_app
from db_models import User
from db_secrets import EncryptedConfigRepository
from test_db_accounts_live import AccountsLiveFixture


class ConfigDiagnosticsLiveTests(AccountsLiveFixture):
    def setUp(self):
        super().setUp()
        import system_config
        self.configs.environment=environment_snapshot({
            'mail':{'host':'smtp.example.test','username':'sender','password':'SyntheticEnvMail'},
            'llm':{'api_key':'SyntheticEnvKey','base_url':'https://api.example.test','model':'env-model'}})
        self.llm=Mock(return_value={'reply_chars':2,'secret':'MUST_NOT_RETURN'})
        self.models=Mock(return_value=['one','two','one'])
        self.mail=Mock(side_effect=lambda cfg,recipient: bool(recipient))
        self.diagnostics=ConfigDiagnostics(self.configs,validate_base_url=system_config.validate_base_url,
            llm_probe=self.llm,models_probe=self.models,mail_probe=self.mail)
        self.app=create_preview_app(Mock(),self.accounts,'synthetic-account-jwt-secret-with-at-least-48-bytes',
            admin_emails=[self.owner_email],account_service=self.accounts,config_service=self.configs,
            config_writes=True,config_diagnostics=self.diagnostics)
        self.client=self.app.test_client()

    def request_probe(self,kind,payload=None,headers=None):
        path='/api/v1/admin/system/'+('mail/test' if kind=='mail' else 'llm/'+('models' if kind=='models' else 'test'))
        return self.client.post(path,json=payload or {},headers=headers or self.owner_headers)

    def stored(self):
        with self.sessions() as session,session.begin():
            return EncryptedConfigRepository(session,self.cipher).get() or {}

    def test_llm_unsaved_override_is_validated_and_does_not_save_secrets(self):
        response=self.request_probe('llm',{'api_key':'SyntheticUnsavedKey','model':'unsaved-model'})
        self.assertEqual(response.status_code,200)
        result=response.get_json()['data']
        self.assertEqual(result['reply_chars'],2)
        self.assertEqual(result['model'],'unsaved-model')
        self.assertNotIn('Synthetic',str(result))
        self.assertNotIn('MUST_NOT_RETURN',str(result))
        self.assertEqual(self.llm.call_args.args[0]['api_key'],'SyntheticUnsavedKey')
        self.assertEqual(self.stored(),{})
        self.assertEqual(len(self.audit(response.get_json()['request_id'])),1)

    def test_mail_send_is_explicit_and_recipient_is_authoritative_actor_email(self):
        response=self.request_probe('mail')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.get_json()['data']['test_email_sent'])
        self.assertEqual(self.mail.call_args.args[1],'')
        response=self.request_probe('mail',{'slot':'notification','send':True})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()['data']['test_email_sent'])
        self.assertEqual(self.mail.call_args.args[1],self.owner_email)
        self.assertEqual(self.request_probe('mail',{'send':True,'to_email':'untrusted@example.test'}).status_code,422)
        self.assertEqual(self.request_probe('mail',{'send':'false'}).status_code,422)

    def test_models_cache_and_audit_commit_together_without_saving_probe_key(self):
        response=self.request_probe('models',{'api_key':'SyntheticUnsavedKey'})
        self.assertEqual(response.status_code,200)
        result=response.get_json()['data']
        self.assertEqual(result['models'],['one','two'])
        self.assertEqual(result['count'],2)
        self.assertTrue(result['models_fetched_at'])
        self.assertEqual(result['config']['source'],'env')
        self.assertEqual(result['config']['models'],['one','two'])
        self.assertNotIn('api_key',self.stored()['system_config/llm.json'])
        self.assertEqual(len(self.audit(response.get_json()['request_id'])),1)
        self.models.return_value=['replacement']
        with patch('db_config_diagnostics.append_audit',side_effect=RuntimeError('private audit failure')):
            self.assertEqual(self.request_probe('models').status_code,503)
        self.assertEqual(self.stored()['system_config/llm.json']['models'],['one','two'])

    def test_configuration_change_during_probe_rejects_stale_model_cache(self):
        def change(cfg):
            self.configs.update(self.owner['user_id'],'llm',
                {'api_key':'SyntheticNewConfigKey','base_url':'https://api.example.test','model':'new-model'},
                password_version=1,request_id='concurrent-config-update')
            return ['stale-model']
        self.models.side_effect=change
        response=self.request_probe('models')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.stored()['system_config/llm.json']['model'],'new-model')
        self.assertNotIn('models',self.stored()['system_config/llm.json'])
        self.assertEqual(len(self.audit(response.get_json()['request_id'])),0)

    def test_permission_revoked_during_callback_cannot_commit_cache(self):
        def revoke(cfg):
            with self.bootstrap.begin() as conn:
                conn.execute(update(User).where(User.id==self.owner['user_id']).values(password_version=2))
            return ['must-not-save']
        self.models.side_effect=revoke
        self.assertEqual(self.request_probe('models').status_code,403)
        self.assertEqual(self.stored(),{})

    def test_provider_errors_are_fixed_and_unprivileged_never_call_provider(self):
        for headers in (self.admin_headers,self.member_headers):
            self.assertEqual(self.request_probe('llm',headers=headers).status_code,403)
        self.llm.assert_not_called()
        self.llm.side_effect=RuntimeError('PRIVATE_KEY provider error')
        response=self.request_probe('llm')
        self.assertEqual(response.status_code,422)
        self.assertNotIn('PRIVATE_KEY',response.get_data(as_text=True))
        self.assertEqual(self.stored(),{})

    def test_private_urls_and_bad_types_fail_before_any_provider_call(self):
        for value in ({'base_url':'https://127.0.0.1'},{'api_key':[]},{'model':'bad\nmodel'}):
            self.assertEqual(self.request_probe('llm',value).status_code,422)
        self.llm.assert_not_called()

    def test_default_app_and_missing_callback_refuse_probes(self):
        default=create_preview_app(Mock(),self.accounts,'synthetic-account-jwt-secret-with-at-least-48-bytes',
            admin_emails=[self.owner_email],config_service=self.configs,config_writes=True).test_client()
        self.assertEqual(default.post('/api/v1/admin/system/llm/test',json={},headers=self.owner_headers).status_code,503)
        self.diagnostics.callbacks['llm']=None
        self.assertEqual(self.request_probe('llm').status_code,503)

    def test_callback_cannot_return_private_keys_as_model_identifiers(self):
        self.models.return_value=['safe','SyntheticEnvKey']
        response=self.request_probe('models')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('SyntheticEnvKey',response.get_data(as_text=True))
        self.assertEqual(self.stored(),{})
