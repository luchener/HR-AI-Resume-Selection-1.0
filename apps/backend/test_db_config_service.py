import unittest
import uuid
from unittest.mock import Mock, patch
from db_config_service import ConfigService, ConfigPermissionError

class ConfigServiceTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.factory = Mock()
        self.factory.return_value.__enter__ = Mock(return_value=self.session)
        self.factory.return_value.__exit__ = Mock(return_value=False)
        self.session.begin.return_value.__enter__ = Mock()
        self.session.begin.return_value.__exit__ = Mock(return_value=False)
        self.actor = uuid.uuid4()
        self.row = {'id':self.actor,'username':'owner','email':'owner@example.test','is_admin':True,'status':'active','deleted_at':None,'password_version':2}
        self.session.execute.return_value.mappings.return_value.one_or_none.return_value = self.row
        self.validator = Mock(return_value={'normalized':True})
        self.service = ConfigService(self.factory, Mock(), validator=self.validator, admin_emails=['owner@example.test'])

    def test_authorized_update_and_audit_share_session(self):
        with patch('db_config_service.EncryptedConfigRepository') as repository, patch('db_config_service.append_audit') as audit:
            self.service.update(self.actor,'mail',{'secret':'private'},request_id='r1',password_version=2)
            arguments = repository.return_value.update_document.call_args.args
            self.assertEqual(arguments[0], 'system_config/mail.json')
            self.assertTrue(arguments[1]['normalized'])
            self.assertEqual(arguments[1]['updated_by'], 'owner')
            self.assertTrue(arguments[1]['updated_at'])
            self.assertIs(audit.call_args.args[0],self.session)
            self.assertNotIn('private',str(audit.call_args))
        self.session.commit.assert_not_called()

    def test_non_admin_inactive_and_revoked_never_validate_or_write(self):
        for changed in ({'email':'ordinary@example.test'},{'status':'locked'},{'password_version':3}):
            self.session.execute.return_value.mappings.return_value.one_or_none.return_value = dict(self.row,**changed)
            with self.assertRaises(ConfigPermissionError):
                self.service.update(self.actor,'llm',{},request_id='r1',password_version=2)
        self.validator.assert_not_called()

    def test_external_whitelist_and_explicit_clear(self):
        self.row['is_admin'] = False
        service = ConfigService(self.factory,Mock(),validator=self.validator,admin_emails=['owner@example.test'])
        with patch('db_config_service.EncryptedConfigRepository') as repository, patch('db_config_service.append_audit'):
            service.update(self.actor,'llm',None,request_id='r1',password_version=2)
            repository.return_value.update_document.assert_called_once_with('system_config/llm.json',None)
        self.validator.assert_not_called()

if __name__ == '__main__':
    unittest.main()
