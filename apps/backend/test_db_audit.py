import unittest
import uuid
from unittest.mock import Mock
from db_audit import append_audit

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.in_transaction.return_value = True
        self.actor = uuid.uuid4()
        self.args = dict(actor_user_id=self.actor, action='config.mail.update', target_type='config', target_id='mail',request_id='request-1', changed_fields=['mail'])
        self.session.execute.return_value.mappings.return_value.one_or_none.return_value = {'id':self.actor,'username':'operator','status':'active','deleted_at':None}

    def test_authoritative_actor_and_no_commit(self):
        append_audit(self.session, **self.args)
        compiled = self.session.execute.call_args.args[0].compile()
        self.assertEqual(compiled.params['actor_identity'],str(self.actor))
        self.assertEqual(compiled.params['metadata']['actor_name'],'operator')
        self.session.commit.assert_not_called()
        self.session.close.assert_not_called()

    def test_rejects_secrets_unknown_fields_and_missing_transaction(self):
        for fields in (['api_key'], ['smtp_password'], ['anything'], 'mail'):
            with self.assertRaises(ValueError):
                append_audit(self.session, **dict(self.args,changed_fields=fields))
        self.session.execute.assert_not_called()
        self.session.in_transaction.return_value = False
        with self.assertRaises(ValueError):
            append_audit(self.session, **self.args)

    def test_frozen_actor_rejected(self):
        self.session.execute.return_value.mappings.return_value.one_or_none.return_value = {'status':'locked','deleted_at':None}
        with self.assertRaises(ValueError):
            append_audit(self.session, **self.args)
        self.assertEqual(self.session.execute.call_count,1)

    def test_original_page_audit_only_allows_bounded_integer_page(self):
        args = dict(self.args, action='resume.original_page', target_type='resume', target_id=str(uuid.uuid4()))
        for page in (None, True, 0, 21, '1'):
            with self.assertRaises(ValueError):
                append_audit(self.session, **args, original_page=page)
        self.session.execute.assert_not_called()
        append_audit(self.session, **args, original_page=20)
        self.assertEqual(self.session.execute.call_args.args[0].compile().params['metadata']['page'], 20)
        with self.assertRaises(ValueError):
            append_audit(self.session, **self.args, original_page=1)

    def test_original_content_audit_has_no_page(self):
        args = dict(self.args, action='resume.original_content', target_type='resume', target_id=str(uuid.uuid4()))
        append_audit(self.session, **args)
        params = self.session.execute.call_args.args[0].compile().params
        self.assertEqual(params['action'], 'resume.original_content')
        self.assertNotIn('page', params['metadata'])
        with self.assertRaises(ValueError):
            append_audit(self.session, **args, original_page=1)

    def test_system_retention_audit_does_not_require_user_actor(self):
        append_audit(self.session, actor_user_id=None, action='retention.resume_purge',
                     target_type='resume', target_id=str(uuid.uuid4()), request_id=None,
                     metadata={'archives_marked': 2, 'original_queued': 1})
        compiled = self.session.execute.call_args.args[0].compile()
        self.assertIsNone(compiled.params['actor_user_id'])
        self.assertEqual(compiled.params['actor_identity'], 'retention')
        self.assertEqual(compiled.params['metadata']['actor_id'], 'system:retention')
        self.assertEqual(compiled.params['metadata']['archives_marked'], 2)
        self.assertEqual(compiled.params['metadata']['original_queued'], 1)

    def test_retention_metadata_is_bounded_and_not_user_supplied(self):
        base = dict(actor_user_id=None, action='retention.job_purge', target_type='job',
                    target_id=str(uuid.uuid4()), request_id=None)
        for value in ({'secret': 'x'}, {'archives_marked': True}, {'archives_marked': 1001}):
            with self.assertRaises(ValueError):
                append_audit(self.session, **base, metadata=value)
        with self.assertRaises(ValueError):
            append_audit(self.session, **dict(self.args, metadata={'archives_marked': 1}))

    def test_system_retention_rejects_nonmatching_target_or_actor(self):
        with self.assertRaises(ValueError):
            append_audit(self.session, actor_user_id=self.actor, action='retention.job_purge',
                         target_type='resume', target_id=str(uuid.uuid4()), request_id=None)
        with self.assertRaises(ValueError):
            append_audit(self.session, actor_user_id=self.actor, action='retention.job_purge',
                         target_type='job', target_id=str(uuid.uuid4()), request_id=None)

if __name__ == '__main__':
    unittest.main()
