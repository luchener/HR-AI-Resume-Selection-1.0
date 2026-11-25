import unittest
from unittest.mock import Mock
from uuid import uuid4
from datetime import datetime, timezone
from db_reconcile import reconcile


class ReconciliationTests(unittest.TestCase):
    def test_evaluation_timestamp_requires_explicit_timezone(self):
        from db_reconcile import evaluation_time
        with self.assertRaises(ValueError):
            evaluation_time('2026-01-01T00:00:00')
        self.assertEqual(evaluation_time('2026-01-01T00:00:00Z'),evaluation_time('2026-01-01T08:00:00+08:00'))

    def test_source_and_identity_policy_must_match_import_ledger(self):
        from db_reconcile import verify_migration_context
        report = {'manifest':{'users/private.json':'digest'}}
        policy = {'admin_emails':['admin@example.test'],'auto_unfreeze_hours':'1'}
        session = Mock()
        def verify(source, stored_policy, hours='1'):
            session.scalar.side_effect = [source,stored_policy]
            verify_migration_context(session,report,admin_emails=[' ADMIN@example.test '],auto_unfreeze_hours=hours)
        verify(report['manifest'],policy)
        for source,stored in ((None,policy),({},policy),(report['manifest'],None),(report['manifest'],dict(policy,admin_emails=[]))):
            with self.subTest(source=source), self.assertRaises(ValueError):
                verify(source,stored)
        with self.assertRaises(ValueError):
            verify(report['manifest'],policy,hours='2')

    def test_freeze_precedence_and_exact_expiry_boundary(self):
        from db_reconcile import expected_user_status
        from hashlib import sha256
        item = {'username':'tester'}
        key = 'login_failures/' + sha256(b'tester').hexdigest() + '.json'
        at = datetime(2026,1,1,tzinfo=timezone.utc)
        failure = {'frozen':True,'frozen_at':at.timestamp()-3600,'frozen_by':'auto'}
        failures = {key:failure}
        def status(record=item, when=at):
            return expected_user_status(record,failures,auto_unfreeze_hours=1,evaluated_at=when)
        self.assertEqual(status(),'locked')
        self.assertEqual(status(when=at.replace(microsecond=1)),'active')
        failure['frozen_by'] = 'admin'
        self.assertEqual(status(when=at.replace(microsecond=1)),'locked')
        self.assertEqual(status(dict(item,deleted_at='2026-01-01')),'deleted')
        failure['frozen_by'] = 'auto'
        with self.assertRaises(ValueError):
            status(when=None)
        self.assertEqual(status(dict(item,frozen_until='legacy'),at.replace(microsecond=1)),'locked')

    def test_millisecond_freeze_timestamp_uses_seconds_for_expiry_and_preserves_delete_state(self):
        from db_reconcile import expected_user_status, expected_lifecycle
        from hashlib import sha256
        at=datetime(2026,1,1,tzinfo=timezone.utc)
        item={'username':'tester','deleted_at':at.isoformat(),'deleted_by':'operator'}
        failure={'frozen':True,'frozen_at':(at.timestamp()-7200)*1000,'frozen_by':'auto'}
        failures={'login_failures/'+sha256(b'tester').hexdigest()+'.json':failure}
        self.assertEqual(expected_user_status(dict(item,deleted_at=None),failures,auto_unfreeze_hours=1,evaluated_at=at),'active')
        self.assertEqual(expected_lifecycle(item,failures,auto_unfreeze_hours=1,evaluated_at=at)['deletion_previous_status'],'active')
        failure['frozen_by']='admin'
        result=expected_lifecycle(item,failures,auto_unfreeze_hours=1,evaluated_at=at)
        self.assertEqual(result['deletion_previous_status'],'locked')
        self.assertEqual(result['frozen_at'].timestamp(),at.timestamp()-7200)

    def fixture(self):
        uid, rid, jid, aid = (uuid4() for _ in range(4))
        stamp = '2026-01-02T03:04:05Z'
        source = {'users':[], 'resumes':[], 'jobs':[(None,{'job_id':str(jid),'user_id':str(uid),'resume_id':str(rid),'created_at':stamp})],
                  'archives':[(None,{'archive_id':str(aid),'user_id':str(uid),'resume_id':str(rid),'job_id':str(jid)})]}
        job = dict(id=jid,owner_user_id=uid,raw_content='',parsed_requirements={},legacy_payload=source['jobs'][0][1],deleted_at=None,
                   created_at=datetime(2026,1,2,3,4,5,tzinfo=timezone.utc),updated_at=datetime(2026,1,2,3,4,5,tzinfo=timezone.utc),source_resume_id=None,title=None,company_name=None)
        archive = dict(id=aid,owner_user_id=uid,tags=[],candidate_name=None,category=None,status='active',legacy_payload=source['archives'][0][1],deleted_at=None,
                       resume_id=None,job_id=jid,analysis_id=None,note=None,payload={})
        session = Mock()
        def execute(statement):
            rows = {'users':[],'resumes':[],'jobs':[job],'archives':[archive]}[statement.get_final_froms()[0].name]
            result = Mock()
            result.mappings.return_value.all.return_value = rows
            return result
        session.execute.side_effect = execute
        return session,{'records':source},job,archive

    def test_archive_alias_reconciles_independently(self):
        for key in ('archiveId','id'):
            session,report,_,_ = self.fixture()
            item = report['records']['archives'][0][1]
            item[key] = item.pop('archive_id')
            self.assertEqual(reconcile(session,report,admin_emails=[])['mismatch_count'],0)

    def test_retired_source_reference_and_time_fallback_match(self):
        session,report,_,_ = self.fixture()
        self.assertEqual(reconcile(session,report,admin_emails=[])['mismatch_count'],0)

    def test_display_projections_and_original_hash_drift(self):
        session,report,job,archive = self.fixture()
        rid = uuid4()
        uid = job['owner_user_id']
        item = {'resume_id':str(rid),'user_id':str(uid),'content':'private-body',
                'processed':{'personal_data':{'姓名':'private-name'}},'original':{'name':'private.pdf','sha256':'f'*64}}
        report['records']['resumes'] = [(None,item)]
        resume = dict(id=rid,owner_user_id=uid,legacy_payload=item,raw_content='private-body',processed_data=item['processed'],
                      deleted_at=None,retention_until=None,candidate_name='private-name',source_filename='private.pdf',
                      content_type='md',content_sha256='f'*64,object_key=None,parse_status='parsed')
        original_execute = session.execute.side_effect
        def execute(statement):
            if statement.get_final_froms()[0].name == 'resumes':
                result = Mock()
                result.mappings.return_value.all.return_value = [resume]
                return result
            return original_execute(statement)
        session.execute.side_effect = execute
        self.assertEqual(reconcile(session,report,admin_emails=[])['mismatch_count'],0)
        resume['content_sha256'] = 'wrong'
        resume['candidate_name'] = 'wrong'
        job['company_name'] = 'wrong'
        archive['note'] = 'wrong'
        summary = reconcile(session,report,admin_emails=[])
        self.assertEqual(summary['mismatches'],{'resumes.candidate_name':1,'resumes.content_sha256':1,'jobs.company_name':1,'archives.note':1})
        self.assertNotIn('private',str(summary))

    def test_archive_unknown_payload_preserved_independently(self):
        session,report,_,archive = self.fixture()
        report['records']['archives'][0][1]['private_extra'] = {'analysis':'private-value'}
        archive['payload'] = {'private_extra':{'analysis':'private-value'}}
        self.assertEqual(reconcile(session,report,admin_emails=[])['mismatch_count'],0)
        archive['payload'] = {}
        summary = reconcile(session,report,admin_emails=[])
        self.assertEqual(summary['mismatches'],{'archives.payload':1})
        self.assertNotIn('private-value',str(summary))

    def test_wrong_reference_and_timestamp_report_only_field_names(self):
        session,report,job,archive = self.fixture()
        job['updated_at'] = datetime(2030,1,1,tzinfo=timezone.utc)
        archive['job_id'] = None
        summary = reconcile(session,report,admin_emails=[])
        self.assertEqual(summary['mismatches'],{'jobs.updated_at':1,'archives.job_id':1})
        self.assertNotIn(str(job['id']),str(summary))

if __name__ == '__main__':
    unittest.main()
