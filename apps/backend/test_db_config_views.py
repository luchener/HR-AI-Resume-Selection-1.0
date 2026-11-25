import unittest
from unittest.mock import patch
from db_config_views import public_stored_document
import test_db_config_service as service_fixtures

class ConfigViewTests(unittest.TestCase):
    def test_mail_and_llm_never_return_secret_or_unknown_fields(self):
        mail = public_stored_document('mail',{'slots':{'transactional':{'password':'secret-mail','host':'smtp.test','extra':'unknown-secret'}}})
        llm = public_stored_document('llm',{'api_key':'secret-model','model':'m','unknown':'unknown-secret'})
        self.assertTrue(mail['slots']['transactional']['password_set'])
        self.assertTrue(llm['api_key_set'])
        for view in (mail,llm):
            self.assertNotIn('secret',str(view))
        self.assertEqual(public_stored_document('llm',None),{'configured':False,'source':'none'})

    def test_malformed_nested_field_fails_closed(self):
        with self.assertRaises(ValueError):
            public_stored_document('llm',{'model':{'api_key':'secret'}})

    def test_service_checks_identity_before_decryption(self):
        fixture = service_fixtures.ConfigServiceTests()
        fixture.setUp()
        with patch('db_config_service.EncryptedConfigRepository') as repo:
            repo.return_value.get.return_value = {'system_config/llm.json':{'api_key':'private','base_url':'https://api.example.test'}}
            result = fixture.service.public_document(fixture.actor,'llm',password_version=2)
            self.assertTrue(result['api_key_set'])
            fixture.session.execute.return_value.mappings.return_value.one_or_none.return_value = dict(fixture.row,email='ordinary@test')
            with self.assertRaises(PermissionError):
                fixture.service.public_document(fixture.actor,'llm',password_version=2)
            self.assertEqual(repo.return_value.get.call_count,1)

if __name__ == '__main__':
    unittest.main()
