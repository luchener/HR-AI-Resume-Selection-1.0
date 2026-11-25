import copy
import unittest
from unittest.mock import patch
import system_config

class PureConfigMergeTests(unittest.TestCase):
    def test_llm_blank_key_preserves_original_without_storage_access(self):
        current = {'api_key':'synthetic-secret','base_url':'https://api.example.test/v1','model':'old','models':['cached']}
        before = copy.deepcopy(current)
        with patch.object(system_config,'_write',side_effect=AssertionError('write')), patch.object(system_config,'raw',side_effect=AssertionError('read')):
            merged = system_config.merge_llm(current,{'api_key':'','model':'new'})
        self.assertEqual(current,before)
        self.assertEqual(merged['api_key'],'synthetic-secret')
        self.assertEqual(merged['models'],['cached'])
        self.assertEqual(merged['model'],'new')

    def test_merge_service_uses_locked_merger(self):
        from test_db_config_service import ConfigServiceTests
        from db_config_service import ConfigService
        fixture = ConfigServiceTests()
        fixture.setUp()
        merger = lambda document,current,incoming: dict(current,**incoming)
        service = ConfigService(fixture.factory,object(),validator=fixture.validator,merger=merger,admin_emails=['owner@example.test'])
        with patch('db_config_service.EncryptedConfigRepository') as repo, patch('db_config_service.append_audit'):
            service.update(fixture.actor,'llm',{'model':'new'},request_id='r1',password_version=2)
            args = repo.return_value.merge_document.call_args.args
            self.assertEqual(args[0],'system_config/llm.json')
            result = args[2]({'api_key':'old'},{'model':'new'})
            self.assertEqual({key:result[key] for key in ('api_key','model')},{'api_key':'old','model':'new'})
            self.assertEqual(result['updated_by'],'owner')
            from datetime import datetime
            self.assertIsNotNone(datetime.fromisoformat(result['updated_at']).tzinfo)
        fixture.validator.assert_not_called()

if __name__ == '__main__':
    unittest.main()
