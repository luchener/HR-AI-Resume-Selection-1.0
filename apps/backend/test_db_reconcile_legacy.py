import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from db_reconcile_legacy import reconcile_legacy

MANIFEST = {'users/user.json': 'hash'}
PLAN = {'projection_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
        'announcements': [{'id': 1}], 'dismissals': []}


class LegacyReconciliationTests(unittest.TestCase):
    def fixture(self):
        report = {'manifest': dict(MANIFEST),
                  'records':{'users':[(Path('user.json'),{'password_hash':'private-hash'})]},
                  'legacy_records':[('system_config','system_config/llm.json',{'value':{'api_key':'private-key'}}),
                                    ('admin_ops','admin_ops/event.json',{'value':{'detail':'private-detail'}})]}
        rows = [dict(root_name='users',record_key='users/user.json',payload={'password_hash':'private-hash'}),
                dict(root_name='system_config',record_key='system_config/llm.json',payload={'encrypted_in':'system_configs','source_key':'system_config/llm.json'}),
                dict(root_name='admin_ops',record_key='admin_ops/event.json',payload={'value':{'detail':'private-detail'}}),
                dict(root_name='__migration__',record_key='source_manifest',payload=dict(MANIFEST)),
                dict(root_name='__migration__',record_key='target_attestation_v1',payload={'version':1,'tables':{}}),
                dict(root_name='__announcement_migration__',record_key='projection',
                     payload={'projection_sha256': PLAN['projection_sha256'],
                              'inventory_sha256': PLAN['inventory_sha256'],
                              'announcements': 1, 'dismissals': 0})]
        session = Mock()
        session.execute.return_value.mappings.return_value.all.return_value = rows
        return session, report, rows

    def reconcile(self, session, report):
        with patch('db_reconcile_legacy.verify_attestation') as verify, \
             patch('db_reconcile_legacy.project_announcement_snapshot', return_value=PLAN):
            result = reconcile_legacy(session, report)
        self.verify = verify
        return result

    def test_complete_payloads_and_metadata_match(self):
        session,report,_ = self.fixture()
        result = self.reconcile(session,report)
        self.assertEqual(result['mismatch_count'],0)
        self.assertEqual(result['metadata_rows'],3)
        self.verify.assert_called_once()

    def test_deleted_changed_and_plaintext_rows_report_counts_only(self):
        session,report,rows = self.fixture()
        rows.pop(0)
        rows[0]['payload'] = {'api_key':'private-key'}
        rows.append(dict(root_name='extra',record_key='extra.json',payload={}))
        result = self.reconcile(session,report)
        self.assertEqual(result['mismatches'],{'legacy_records.missing_rows':1,'legacy_records.extra_rows':1,'legacy_records.changed_payloads':1})
        self.assertNotIn('private',str(result))

    def test_reserved_namespace_rejected(self):
        session,report,_ = self.fixture()
        report['legacy_records'].append(('__migration__','__migration__/identity_policy',{'value':{}}))
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_other_reserved_namespaces_rejected_in_source(self):
        session,report,_ = self.fixture()
        report['legacy_records'].append(('__future_marker__','__future_marker__/x',{'value':{}}))
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_unknown_reserved_root_in_target_is_extra_row(self):
        session,report,rows = self.fixture()
        rows.append(dict(root_name='__unexpected_payload__',record_key='anything',payload={'x':1}))
        result = self.reconcile(session,report)
        self.assertEqual(result['mismatches'],{'legacy_records.extra_rows':1})

    def test_unknown_key_under_known_root_is_extra_row(self):
        session,report,rows = self.fixture()
        rows.append(dict(root_name='__migration__',record_key='unexpected_key',payload={}))
        result = self.reconcile(session,report)
        self.assertEqual(result['mismatches'],{'legacy_records.extra_rows':1})

    def test_formatted_but_wrong_projection_digest_is_rejected(self):
        session,report,rows = self.fixture()
        rows[-1]['payload'].update(projection_sha256='c' * 64)
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_formatted_but_wrong_projection_counts_are_rejected(self):
        session,report,rows = self.fixture()
        rows[-1]['payload'].update(announcements=999)
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_missing_announcement_marker_with_pending_source_is_rejected(self):
        session,report,rows = self.fixture()
        rows.pop()
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_wrong_source_manifest_is_rejected(self):
        session,report,rows = self.fixture()
        rows[3]['payload'] = {'users/user.json': 'other'}
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_empty_attestation_is_rejected_by_verifier(self):
        session,report,rows = self.fixture()
        rows[4]['payload'] = {}
        with patch('db_reconcile_legacy.verify_attestation',
                   side_effect=ValueError('Target rows differ')):
            with self.assertRaises(ValueError):
                reconcile_legacy(session,report)

    def test_missing_attestation_is_rejected(self):
        session,report,rows = self.fixture()
        rows.pop(4)
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_invalid_evaluation_time_is_rejected(self):
        session,report,rows = self.fixture()
        rows.append(dict(root_name='__migration__',record_key='evaluation_time',
                         payload={'evaluated_at': 'not-a-time'}))
        with self.assertRaises(ValueError):
            self.reconcile(session,report)

    def test_identity_policy_must_match_declared_admin_mapping(self):
        session,report,rows = self.fixture()
        rows.append(dict(root_name='__migration__',record_key='identity_policy',
                         payload={'admin_emails': ['other@example.invalid'], 'auto_unfreeze_hours': '0'}))
        with patch.dict('os.environ', {'MIGRATION_ADMIN_EMAILS': 'admin@example.invalid'}):
            with self.assertRaises(ValueError):
                self.reconcile(session,report)


if __name__ == '__main__':
    unittest.main()
