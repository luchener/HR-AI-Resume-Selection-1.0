import unittest
from unittest.mock import Mock
from cryptography.fernet import Fernet
from db_secrets import ConfigCipher, ConfigSecretError
from db_reconcile_config import reconcile_configs


class ConfigReconciliationTests(unittest.TestCase):
    def fixture(self):
        cipher = ConfigCipher(Fernet.generate_key(),key_id='test')
        document = {'api_key':'private-secret'}
        expected = {'system_config/llm.json':document}
        report = {'legacy_records':[('system_config','system_config/llm.json',{'value':document})]}
        session = Mock()
        session.execute.return_value.mappings.return_value.all.return_value = [{'key':'system_config','value':cipher.encrypt(expected)}]
        return session,report,cipher,expected

    def test_matches_and_changed_values_do_not_leak(self):
        session,report,cipher,expected = self.fixture()
        self.assertEqual(reconcile_configs(session,report,cipher=cipher)['mismatch_count'],0)
        altered = {'system_config/llm.json':{'api_key':'different-private-secret'}}
        session.execute.return_value.mappings.return_value.all.return_value = [{'key':'system_config','value':cipher.encrypt(altered)}]
        result = reconcile_configs(session,report,cipher=cipher)
        self.assertEqual(result['mismatches'],{'system_configs.changed_documents':1})
        self.assertNotIn('private',str(result))

    def test_boolean_numeric_configuration_drift_detected(self):
        session,report,cipher,expected = self.fixture()
        report['legacy_records'][0][2]['value']['enabled'] = True
        changed = {'system_config/llm.json':{'api_key':'private-secret','enabled':1}}
        session.execute.return_value.mappings.return_value.all.return_value = [{'key':'system_config','value':cipher.encrypt(changed)}]
        self.assertEqual(reconcile_configs(session,report,cipher=cipher)['mismatches'],{'system_configs.changed_documents':1})

    def test_missing_envelope_and_extra_rows_detected(self):
        session,report,cipher,_ = self.fixture()
        session.execute.return_value.mappings.return_value.all.return_value = [{'key':'unexpected','value':{}}]
        self.assertEqual(reconcile_configs(session,report,cipher=cipher)['mismatches'],{'system_configs.extra_rows':1,'system_configs.missing_envelope':1})

    def test_wrong_key_fails_closed(self):
        session,report,_,_ = self.fixture()
        with self.assertRaises(ConfigSecretError):
            reconcile_configs(session,report,cipher=ConfigCipher(Fernet.generate_key(),key_id='test'))

if __name__ == '__main__':
    unittest.main()
