import unittest
from unittest.mock import Mock
from uuid import uuid4
from db_reconcile_policy import reconcile_policies


class PolicyReconciliationTests(unittest.TestCase):
    def test_inherit_unlimited_limited_and_switches(self):
        uid = uuid4()
        report = {'records':{'users':[(None,{'user_id':str(uid),'username':'tester'})]},
                  'legacy_records':[]}
        global_row = dict(scope_key='global',user_id=None,mode='limited',daily_limit=10,enabled=True,quota_enabled=True,email_notify=False)
        user_row = dict(scope_key=str(uid),user_id=uid,mode='inherit',daily_limit=None,enabled=False,quota_enabled=None,email_notify=True)
        session = Mock()
        rows = [global_row,user_row]
        session.execute.return_value.mappings.return_value.all.return_value = rows
        for source_limit, expected_mode, expected_limit in ((None,'inherit',None),(-1,'unlimited',None),(0,'limited',0),(3,'limited',3)):
            limits = {'default_daily':10,'quota_enabled':True,'users':{'tester':{'daily_limit':source_limit,'analysis_enabled':False}}}
            report['legacy_records'] = [('quota','quota/limits.json',{'value':limits})]
            user_row.update(mode=expected_mode,daily_limit=expected_limit)
            self.assertEqual(reconcile_policies(session,report)['mismatch_count'],0)
        user_row['enabled'] = True
        summary = reconcile_policies(session,report)
        self.assertEqual(summary['mismatches'],{'quota_policies.enabled':1})
        self.assertNotIn(str(uid),str(summary))

    def test_missing_and_extra_rows(self):
        report = {'records':{'users':[]},'legacy_records':[]}
        session = Mock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        self.assertEqual(reconcile_policies(session,report)['mismatches'],{'quota_policies.missing_rows':1})
        session.execute.return_value.mappings.return_value.all.return_value = [dict(scope_key='unexpected')]
        self.assertEqual(reconcile_policies(session,report)['mismatches'],{'quota_policies.missing_rows':1,'quota_policies.extra_rows':1})

if __name__ == '__main__':
    unittest.main()
