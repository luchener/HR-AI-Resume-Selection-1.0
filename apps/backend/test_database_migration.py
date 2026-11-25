"""Safety regressions for the staged importer; no production connections."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from db_migrate_json import inventory, validate_inventory, _user_record, _resume_record, _archive_record, main
from db_security import normalize_url, bounded_page, literal_like
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from db_models import User, Base


class MigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.uid = str(uuid.uuid4())
        self.user = {'user_id': self.uid, 'username': 'tester', 'password_hash': 'hash', 'password_salt': 'salt'}
        self.put('users', self.uid, self.user)

    def put(self, folder, name, payload):
        path = self.root / folder / f'{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding='utf8')
        return path

    def test_archive_identity_aliases_and_conflicts(self):
        from db_migrate_json import archive_identity
        aid = str(uuid.uuid4())
        for key in ('archive_id','archiveId','id'):
            row = {key:aid,'user_id':self.uid}
            self.put('archives','record',row)
            validate_inventory(inventory(self.root))
            self.assertEqual(_archive_record(Path('record.json'),row)['id'],uuid.UUID(aid))
        self.assertEqual(archive_identity({'archive_id':aid,'archiveId':aid.upper()}),uuid.UUID(aid))
        self.put('archives','record',{'archive_id':aid,'id':str(uuid.uuid4()),'user_id':self.uid})
        with self.assertRaisesRegex(ValueError,'conflicting archive'):
            validate_inventory(inventory(self.root))

    def test_conflicting_owner_alias_blocks_before_database(self):
        other = str(uuid.uuid4())
        self.put('users',other,dict(self.user,user_id=other,username='other'))
        rid = str(uuid.uuid4())
        self.put('resumes',rid,{'resume_id':rid,'user_id':self.uid,'owner_user_id':other})
        with self.assertRaisesRegex(ValueError,'Conflicting source owner'):
            validate_inventory(inventory(self.root))

    def test_login_freeze_uses_hashed_snapshot_not_reopened_file(self):
        import hashlib
        from db_migrate_json import snapshot_login_failure
        key = hashlib.sha256(self.user['username'].lower().encode()).hexdigest()
        path = self.put('login_failures',key,{'frozen':True,'frozen_by':'admin'})
        report = inventory(self.root)
        path.write_text('{"frozen":false}',encoding='utf8')
        self.assertTrue(snapshot_login_failure(report,self.user['username'])['frozen'])
        self.assertNotEqual(report['manifest'],inventory(self.root)['manifest'])

    def test_login_freeze_invalid_shape_blocks_preflight(self):
        import hashlib
        key = hashlib.sha256(self.user['username'].lower().encode()).hexdigest()
        self.put('login_failures',key,[])
        with self.assertRaisesRegex(ValueError,'login failure'):
            validate_inventory(inventory(self.root))

    def test_login_freeze_invalid_flag_and_epoch_block_preflight(self):
        import hashlib
        key = hashlib.sha256(self.user['username'].lower().encode()).hexdigest()
        for record in ({'frozen':'false'},{'frozen':1},{'frozen_at':'NaN'},{'frozen_at':True},{'frozen_at':-1},
                       {'frozen_at':1e99},{'frozen_by':'unknown'},{'frozen_reason':'x'*513},
                       {'consecutive_failures':True},{'failures':{}}):
            self.put('login_failures',key,record)
            with self.subTest(record=record), self.assertRaises(ValueError):
                validate_inventory(inventory(self.root))
        self.put('login_failures',key,{'frozen':True,'frozen_at':1234.5})
        validate_inventory(inventory(self.root))

    def test_ambiguous_or_non_jsonb_source_blocks_without_echo(self):
        target = self.put('metadata','unsafe',{})
        for raw in ('{"private-key":1,"private-key":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":"\\u0000"}', '{"x":"\\ud800"}'):
            target.write_text(raw,encoding='utf8')
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(raw=raw), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main([str(self.root)]),2)
            self.assertNotIn('private-key',output.getvalue()+errors.getvalue())

    def test_dry_run_needs_no_database(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([str(self.root)]), 0)
        self.assertIn('DRY RUN', output.getvalue())
        self.assertNotIn('password_hash', output.getvalue())

    def test_dry_run_blocks_invalid_auxiliary_without_echoing_values(self):
        self.put('quota','limits',{'users':{'sensitive-orphan':{'daily_limit':2}}})
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main([str(self.root)]),2)
        self.assertNotIn('sensitive-orphan', output.getvalue()+errors.getvalue())
        self.assertIn('Preflight blocked', errors.getvalue())

    def test_dry_run_blocks_missing_original_without_secret_output(self):
        rid = str(uuid.uuid4())
        self.put('resumes',rid,{'resume_id':rid,'user_id':self.uid,'original':{'name':'private-candidate.pdf','ext':'pdf','bytes':10,'sha256':'missing'}})
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main([str(self.root)]),2)
        self.assertNotIn('private-candidate',output.getvalue()+errors.getvalue())
        self.assertIn('Preflight blocked',errors.getvalue())
        from db_migrate_json import apply_import
        from unittest.mock import patch
        with patch('db_security.migration_engine',side_effect=AssertionError('Database connection attempted')):
            with self.assertRaisesRegex(ValueError,'original missing'):
                apply_import(inventory(self.root),'postgresql://user:pass@localhost/test')

    def test_duplicate_and_zero_user_uuid_blocked(self):
        self.put('users','duplicate',dict(self.user,username='different',email='different@example.test'))
        with self.assertRaisesRegex(ValueError,'Duplicate source user UUID'):
            validate_inventory(inventory(self.root))
        from db_migrate_json import _uuid
        with self.assertRaises(ValueError):
            _uuid('00000000-0000-0000-0000-000000000000')

    def test_uppercase_reference_cannot_bypass_cross_owner_guard(self):
        other = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        self.put('users',other,dict(self.user,user_id=other,username='other'))
        self.put('resumes',rid,{'resume_id':rid,'user_id':other})
        jid = str(uuid.uuid4())
        self.put('jobs',jid,{'job_id':jid,'user_id':self.uid,'resume_id':rid.upper()})
        with self.assertRaisesRegex(ValueError,'Cross-owner'):
            validate_inventory(inventory(self.root))

    def test_mixed_case_uuid_owner_is_not_orphan(self):
        rid = str(uuid.uuid4())
        self.put('resumes',rid,{'resume_id':rid.upper(),'user_id':self.uid.upper()})
        jid = str(uuid.uuid4())
        self.put('jobs',jid,{'job_id':jid,'user_id':self.uid,'resume_id':rid})
        report = inventory(self.root)
        validate_inventory(report)
        self.assertEqual(report['orphan_owner_references'],{'resumes':0,'jobs':0,'archives':0})
        self.assertIn(rid,report['known_ids']['resumes'])

    def test_invalid_projection_fields_block_preflight(self):
        for changed in ({'password_version':0},{'password_version':True},{'created_at':'bad-time'},{'username':'x'*1000}):
            self.put('users',self.uid,dict(self.user,**changed))
            with self.subTest(fields=list(changed)), self.assertRaises(ValueError):
                validate_inventory(inventory(self.root))
        self.put('users',self.uid,self.user)
        aid = str(uuid.uuid4())
        self.put('archives',aid,{'archive_id':aid,'user_id':self.uid,'status':'unsupported'})
        with self.assertRaisesRegex(ValueError,'archive status'):
            validate_inventory(inventory(self.root))

    def test_unknown_user_fields_preserved(self):
        self.user['restored_at'] = '2026-01-01T00:00:00Z'
        converted = _user_record(Path('u.json'), self.user)
        self.assertEqual(converted['legacy_payload'], self.user)

    def test_deleted_source_archive_retains_payload(self):
        row = {'archive_id': str(uuid.uuid4()), 'user_id': self.uid, 'custom_tags': ['HR'], 'source_purged': True}
        converted = _archive_record(Path('a.json'), row)
        self.assertEqual(converted['tags'], ['HR'])
        self.assertEqual(converted['legacy_payload'], row)

    def test_missing_owner_blocks(self):
        self.put('resumes', 'r', {'resume_id': str(uuid.uuid4()), 'user_id': str(uuid.uuid4())})
        with self.assertRaises(ValueError):
            validate_inventory(inventory(self.root))

    def test_duplicate_case_insensitive_identity_blocks(self):
        second = dict(self.user, user_id=str(uuid.uuid4()), username='TESTER')
        self.put('users', second['user_id'], second)
        with self.assertRaises(ValueError):
            validate_inventory(inventory(self.root))

    def test_invalid_uuid_not_replaced(self):
        self.put('resumes', 'bad', {'resume_id': 'bad', 'user_id': self.uid})
        with self.assertRaises(ValueError):
            validate_inventory(inventory(self.root))

    def test_manifest_includes_binary_and_auxiliary(self):
        original = self.root / 'resume_originals' / 'a.pdf'
        original.parent.mkdir()
        original.write_bytes(b'%PDF-example')
        self.put('quota', 'policy', {'limit': -1})
        report = inventory(self.root)
        self.assertIn('resume_originals/a.pdf', report['manifest'])
        self.assertIn('quota/policy.json', report['manifest'])

    def test_corrupt_auxiliary_blocks(self):
        path = self.root / 'quota' / 'bad.json'
        path.parent.mkdir()
        path.write_bytes(b'{broken')
        with self.assertRaises(ValueError):
            validate_inventory(inventory(self.root))

    def test_auxiliary_arrays_and_root_json_preserved(self):
        (self.root / 'metadata.json').write_text('[1,2]', encoding='utf8')
        report = inventory(self.root)
        self.assertIn(('__root__', 'metadata.json', {'value': [1, 2]}), report['legacy_records'])

    def test_sql_injection_payload_is_bound(self):
        payload = "' OR 1=1; DROP TABLE users; --"
        query = select(User.id).where(User.username == payload)
        compiled = query.compile(dialect=postgresql.dialect())
        self.assertNotIn(payload, str(compiled))
        self.assertIn(payload, compiled.params.values())

    def test_runtime_rejects_elevated_database_identity(self):
        from db_runtime import build_runtime
        for role in ('postgres', 'resume_migrator', 'resume_bootstrap_admin'):
            with self.assertRaises(ValueError):
                build_runtime(f'postgresql://{role}:x@localhost/example')
        for query in ('user=postgres', 'dbname=other', 'host=remote', 'options=-csearch_path=other'):
            with self.assertRaises(ValueError):
                build_runtime('postgresql://resume_app:x@localhost/example?' + query)
        engine, factory = build_runtime('postgresql://resume_app:x@localhost/example')
        engine.dispose()  # No connect occurred.

    def test_postgres_driver_is_normalized(self):
        self.assertTrue(normalize_url('postgresql://u:p@localhost/test').startswith('postgresql+psycopg://'))
        with self.assertRaises(ValueError):
            normalize_url('sqlite:///x.db')

    def test_pagination_and_like_boundaries(self):
        self.assertEqual(bounded_page(2, 20), (20, 20))
        for values in [(0, 20), (1, 101), (True, 10), (999999, 100)]:
            with self.assertRaises(ValueError):
                bounded_page(*values)
        self.assertEqual(literal_like('a%_'), '%a\\%\\_%')

    def test_frozen_revision_keeps_schema_parity(self):
        import importlib.util
        from sqlalchemy.schema import CreateTable, CreateIndex
        path = Path(__file__).parent / 'migrations/versions/0001_initial_schema.py'
        spec = importlib.util.spec_from_file_location('frozen_schema', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        dialect = postgresql.dialect()
        self.assertEqual(set(Base.metadata.tables), set(module.metadata.tables))
        for name, table in Base.metadata.tables.items():
            frozen = module.metadata.tables[name]
            def signature(t):
                lifecycle = {'frozen_at', 'frozen_by', 'frozen_reason', 'deleted_by', 'deletion_previous_status',
                             'login_consecutive_failures', 'login_window_failures'}
                if name == 'analysis_tasks':
                    lifecycle.update({'kind', 'web_search'})
                return ([(c.name, str(c.type.compile(dialect=dialect)), c.nullable) for c in t.columns if c.name not in lifecycle],
                        sorted((type(c).__name__, str(getattr(c, 'sqltext', '')), tuple(col.name for col in c.columns)) for c in t.constraints
                               if c.name not in {'uq_analysis_tasks_request_id', 'ck_analysis_tasks_request_uuid',
                                                 'ck_users_frozen_by', 'ck_users_deletion_previous_status',
                                                 'ck_users_login_failure_counts', 'ck_analysis_tasks_kind'}))
            # 0008/0011/0024 evolve runtime metadata without rewriting 0001.
            # New constraint presence is asserted in test_db_task_lifecycle.
            self.assertEqual(signature(table), signature(frozen), name)

    def test_original_copy_integrity(self):
        import hashlib
        from db_originals import copy_originals
        rid = str(uuid.uuid4())
        raw = b'%PDF-synthetic'
        source = self.root / 'resume_originals' / f'{rid}.pdf'
        source.parent.mkdir()
        source.write_bytes(raw)
        self.put('resumes', rid, {'resume_id': rid, 'user_id': self.uid, 'original': {'ext': 'pdf', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}})
        with tempfile.TemporaryDirectory() as target:
            self.assertEqual(copy_originals(self.root, Path(target))['verified_copied'], 1)
            self.assertEqual((Path(target)/'resume_originals'/f'{rid}.pdf').read_bytes(), raw)
        source.write_bytes(b'corrupt')
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaises(ValueError):
                copy_originals(self.root, Path(target))

    def test_cross_owner_reference_blocks(self):
        other = str(uuid.uuid4())
        self.put('users', other, dict(self.user, user_id=other, username='other'))
        rid = str(uuid.uuid4())
        self.put('resumes', rid, {'resume_id': rid, 'user_id': other})
        self.put('jobs', 'j', {'job_id': str(uuid.uuid4()), 'resume_id': rid, 'user_id': self.uid})
        with self.assertRaisesRegex(ValueError, 'Cross-owner'):
            validate_inventory(inventory(self.root))

    def test_foreign_keys_and_ddl_compile(self):
        from sqlalchemy.schema import CreateTable
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=postgresql.dialect()))
        self.assertTrue(Base.metadata.tables['resumes'].c.owner_user_id.foreign_keys)


if __name__ == '__main__':
    unittest.main()
