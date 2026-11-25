import unittest
import uuid
from db_audit_import import audit_rows

class AuditImportTests(unittest.TestCase):
    def test_actor_snapshot_and_sensitive_detail_quarantine(self):
        uid = str(uuid.uuid4())
        source = {'admin_ops/a.json': {'op':'user_delete','operator_id':uid,'target_username':'target','detail':'secret should stay quarantined','created_at':'2026-01-01T00:00:00Z'}}
        row = audit_rows(source, [uid])[0]
        self.assertEqual(row['actor_identity'], uid)
        self.assertEqual(str(row['actor_user_id']), uid)
        self.assertNotIn('secret', str(row))
        orphan = audit_rows(source, [])[0]
        self.assertIsNone(orphan['actor_user_id'])
        self.assertEqual(orphan['actor_identity'], uid)

    def test_uppercase_actor_links_canonical_user(self):
        uid = uuid.uuid4()
        row = audit_rows({'admin_ops/a.json':{'op':'x','operator_id':str(uid).upper(),'created_at':'2026-01-01T00:00:00Z'}},[str(uid)])[0]
        self.assertEqual(row['actor_user_id'],uid)
        self.assertEqual(row['actor_identity'],str(uid).upper())

    def test_bad_time_and_source_block(self):
        for records in ({'../a.json': {}}, {'admin_ops/a.json': {'op':'x','created_at':'bad'}}):
            with self.assertRaises(ValueError):
                audit_rows(records, [])

if __name__ == '__main__':
    unittest.main()
