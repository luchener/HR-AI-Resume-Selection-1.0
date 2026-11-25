"""Resolved config contracts against the existing pure legacy view functions."""
from copy import deepcopy
import os
from unittest import TestCase
from unittest.mock import patch

from db_config_resolved import MASK, environment_snapshot, public_config_document


SHADOW_EVIDENCE = []


class ResolvedConfigTests(TestCase):
    def setUp(self):
        self.environment = {'mail': {'host': 'smtp.example.test', 'username': 'sender@example.test',
                                    'password': 'SyntheticEnvMail', 'port': 465, 'security': 'ssl'},
                            'llm': {'api_key': 'SyntheticEnvModel', 'base_url': 'https://api.example.test',
                                    'model': 'env-model'}}

    def test_complete_empty_contract_and_explicit_environment_copy(self):
        source = deepcopy(self.environment)
        snapshot = environment_snapshot(source)
        source['llm']['api_key'] = ''
        self.assertTrue(public_config_document('llm', None, snapshot)['ready'])
        with patch.dict(os.environ,LLM_API_KEY='SyntheticAmbientIgnored',SMTP_PASSWORD='SyntheticAmbientIgnored'):
            empty = environment_snapshot(None)
        llm = public_config_document('llm', None, empty)
        self.assertEqual(llm['source'], 'none')
        self.assertFalse(llm['ready'])
        self.assertEqual(set(llm), {'api_key_set','api_key_mask','base_url','model','timeout','enabled',
                                   'source','ready','env_available','models','models_fetched_at','updated_at','updated_by'})
        mail = public_config_document('mail', None, empty)
        self.assertEqual(set(mail), {'slots','env_available','updated_at','updated_by'})
        self.assertEqual(set(mail['slots']), {'transactional','notification'})
        self.assertEqual(mail['slots']['notification']['source'], 'none')

    def test_mail_fallback_and_disabled_slots_do_not_leak_secrets(self):
        stored = {'slots': {'transactional': {'host':'smtp.ui.test','username':'ui-sender','password':'SyntheticUISecret'},
                            'notification': {'enabled':False,'host':'disabled','password':'DisabledSecret'}}}
        mail = public_config_document('mail', stored, environment_snapshot(self.environment))
        self.assertEqual(mail['slots']['notification']['source'], 'ui.transactional')
        self.assertEqual(mail['slots']['transactional']['source'], 'ui')
        self.assertTrue(mail['env_available'])
        self.assertEqual(mail['slots']['notification']['password_mask'], MASK)
        self.assertNotIn('Secret', str(mail))
        stored['slots']['transactional']['enabled'] = False
        self.assertEqual(public_config_document('mail', stored, environment_snapshot(self.environment))
                         ['slots']['notification']['source'], 'env')

    def test_partial_or_disabled_llm_uses_environment_and_retains_cache_metadata(self):
        stored = {'api_key':'StoredSecret','enabled':False,'models':['m1','m2'],
                  'models_fetched_at':'2026-10-01T00:00:00Z','updated_at':'stamp','updated_by':'operator'}
        view = public_config_document('llm', stored, environment_snapshot(self.environment))
        self.assertEqual(view['source'], 'env')
        self.assertEqual(view['models'], ['m1','m2'])
        self.assertEqual(view['updated_by'], 'operator')
        self.assertNotIn('Secret', str(view))
        stored['enabled'] = True
        view = public_config_document('llm', stored, environment_snapshot(self.environment))
        self.assertEqual(view['source'], 'ui')
        self.assertEqual(view['base_url'], self.environment['llm']['base_url'])

    def test_bad_stored_data_is_not_silently_replaced_by_environment(self):
        for document, data in [('mail',{'slots':{'transactional':{'port':False}}}),
                ('mail',{'slots':[]}), ('llm',{'api_key':['secret']}),
                ('llm',{'enabled':'false'}), ('llm',{'models':{}}), ('llm',{'timeout':0})]:
            with self.subTest(document=document), self.assertRaises(ValueError):
                public_config_document(document, data, environment_snapshot(self.environment))

    def test_selected_legacy_public_views_match_for_fallback_and_cache_cases(self):
        import system_config
        cases = [('mail',None), ('llm',None),
                 ('mail',{'slots':{'transactional':{'host':'ui.smtp','username':'ui','password':'synthetic'}}}),
                 ('mail',{'slots':{'transactional':{'host':'partial'}}}),
                 ('mail',{'slots':{'notification':{'host':'notify','username':'n','password':'synthetic'}}}),
                 ('mail',{'slots':{'transactional':{'enabled':False,'host':'disabled'}}}),
                 ('llm',{'api_key':'synthetic','base_url':'https://ui.example.test','model':'ui-model'}),
                 ('llm',{'api_key':'synthetic','models':['one','two'],'models_fetched_at':'stamp','updated_by':'operator'}),
                 ('llm',{'enabled':False,'api_key':'synthetic','base_url':'https://disabled.test'}),
                 ('llm',{'models':['cached'],'updated_at':'stamp'})]
        count = 0
        for raw_environment in (self.environment, {}):
            environment = environment_snapshot(raw_environment)
            with patch.multiple(system_config.config,
                    LLM_BASE_URL=environment['llm'].get('base_url',''), LL_MODEL=environment['llm'].get('model',''),
                    LLM_API_KEY=environment['llm'].get('api_key',''), SMTP_HOST=environment['mail'].get('host',''),
                    SMTP_PORT=465, SMTP_USER=environment['mail'].get('username',''),
                    SMTP_PASSWORD=environment['mail'].get('password',''), SMTP_FROM=''):
                for document, data in cases:
                    with patch.object(system_config, 'raw', return_value=deepcopy(data or {})):
                        legacy = system_config.public_mail() if document=='mail' else system_config.public_llm()
                    actual = public_config_document(document, data, environment)
                    self.assertEqual(actual, legacy)
                    count += 1
        SHADOW_EVIDENCE.append({'synthetic_cases':count,'differences':0,'scope':'pure_legacy_public_config_views'})
