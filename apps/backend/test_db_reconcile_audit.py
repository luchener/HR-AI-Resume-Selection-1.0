import unittest
from unittest.mock import Mock
from uuid import uuid4
from datetime import datetime, timezone
from db_reconcile_audit import reconcile_audits


class AuditReconciliationTests(unittest.TestCase):
    def fixture(self, linked=True):
        uid = uuid4()
        item = dict(operator_id=str(uid).upper(),op='user_delete',target_username='private-user',
                    request_id='private-request',detail='secret-detail',created_at='2026-01-01T00:00:00Z')
        report = {'records':{'users':[(None,{'user_id':str(uid)})] if linked else []},
                  'legacy_records':[('admin_ops','admin_ops/a.json',{'value':item})]}
        row = dict(source_key='admin_ops/a.json',actor_identity=str(uid).upper(),actor_user_id=uid if linked else None,
                   action='user_delete',target_type='legacy_user',target_id='private-user',request_id='private-request',
                   metadata={'legacy_detail_quarantined':True},created_at=datetime(2026,1,1,tzinfo=timezone.utc),ip_address=None,user_agent=None)
        session = Mock()
        session.execute.return_value.mappings.return_value.all.return_value = [row]
        return session,report,row

    def test_linked_and_deleted_actor_snapshots_match(self):
        for linked in (True,False):
            session,report,_ = self.fixture(linked)
            self.assertEqual(reconcile_audits(session,report)['mismatch_count'],0)

    def test_changed_projection_and_detail_leak_detected(self):
        session,report,row = self.fixture()
        row['action'] = 'wrong'
        row['metadata']['detail'] = 'secret-detail'
        summary = reconcile_audits(session,report)
        self.assertEqual(summary['mismatches'],{'audit_logs.action':1,'audit_logs.metadata':1})
        for secret in ('secret-detail','private-user','private-request'):
            self.assertNotIn(secret,str(summary))

    def test_null_defaults_and_boolean_metadata_are_checked(self):
        session,report,row = self.fixture()
        row.update(ip_address='127.0.0.1',user_agent='unexpected')
        row['metadata'] = {'legacy_detail_quarantined':1}
        self.assertEqual(reconcile_audits(session,report)['mismatches'],{'audit_logs.metadata':1,'audit_logs.ip_address':1,'audit_logs.user_agent':1})

    def test_missing_and_runtime_rows_detected(self):
        session,report,row = self.fixture()
        row['source_key'] = None
        self.assertEqual(reconcile_audits(session,report)['mismatches'],{'audit_logs.missing_rows':1,'audit_logs.unexpected_runtime_rows':1})

if __name__ == '__main__':
    unittest.main()
