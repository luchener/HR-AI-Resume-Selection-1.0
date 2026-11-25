# -*- coding: utf-8 -*-
"""Failure-path tests for the rehearsal restore tool (no Docker daemon required)."""
import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('rehearsal_restore', ROOT / 'scripts' / 'database-rehearsal-restore.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

MIGRATOR_URL = 'postgresql+psycopg://resume_migrator:secret@10.0.0.1:15453/resume_sql_preview_rehearsal_20261101'
APP_URL = 'postgresql+psycopg://resume_app:secret@postgres:5432/resume_sql_preview_rehearsal_20261101'


class FakeArgs:
    def __init__(self, run_dir, target='resume_sql_preview_rehearsal_clone_x'):
        self.dump = run_dir / 'pristine.dump'
        self.dump.write_bytes(b'PGDMPfake')
        self.password_file = run_dir / 'pg-password'
        self.password_file.write_text('pw\n')
        self.init_script = run_dir / 'database-init.sh'
        self.init_script.write_text("<<'SQL'\nSELECT 1;\nSQL\n<<'SQL'\nGRANT SELECT ON t TO app;\nSQL\n")
        self.target_db = target
        self.pg_container = 'resume-db-rehearsal-20261101'
        self.purpose = 'rehearsal-20261101'
        self.sql_image = 'image:test'
        self.init_db = 'resume_sql_preview_rehearsal_20261101'
        self.output = run_dir / 'out.json'


def write_config(run_dir, *, migrator=MIGRATOR_URL, app=APP_URL, drop_migration=False):
    env = {'DATABASE_URL': app, 'DATABASE_MIGRATION_URL': migrator}
    (run_dir / 'migration.env').write_text(''.join(k + '=' + v + '\n' for k, v in env.items()))
    (run_dir / 'deployment.json').write_text(json.dumps({'database_url': app}))
    if drop_migration:
        (run_dir / 'migration.env').write_text('DATABASE_URL=' + app + '\n')


def completed(returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RestoreToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        write_config(self.run_dir)
        self.args = FakeArgs(self.run_dir)

    def tearDown(self):
        self.temp.cleanup()

    # --- DSN/endpoint binding ---
    def test_source_dsn_must_match_init_db(self):
        parsed = tool.validate_source(MIGRATOR_URL, 'resume_migrator', 'resume_sql_preview_rehearsal_20261101')
        self.assertEqual(parsed.path, '/resume_sql_preview_rehearsal_20261101')
        with self.assertRaises(tool.StageError):
            tool.validate_source(MIGRATOR_URL.replace('20261101', '20261030'), 'resume_migrator',
                                 'resume_sql_preview_rehearsal_20261101')

    def test_source_role_must_match(self):
        with self.assertRaises(tool.StageError):
            tool.validate_source(MIGRATOR_URL, 'resume_app', 'resume_sql_preview_rehearsal_20261101')

    def test_local_dsn_keeps_credentials_and_binds_loopback(self):
        parsed = tool.validate_source(APP_URL, 'resume_app', 'resume_sql_preview_rehearsal_20261101')
        dsn = tool.local_dsn(parsed, 'resume_sql_preview_rehearsal_clone_x')
        self.assertIn('resume_app:secret@127.0.0.1:5432/', dsn)
        self.assertTrue(dsn.endswith('/resume_sql_preview_rehearsal_clone_x'))

    def test_target_dsn_changes_only_database(self):
        parsed = tool.validate_source(MIGRATOR_URL, 'resume_migrator', 'resume_sql_preview_rehearsal_20261101')
        dsn = tool.target_dsn(parsed, 'resume_sql_preview_clone')
        self.assertIn('@10.0.0.1:15453/resume_sql_preview_clone', dsn)

    # --- container identity ---
    def test_container_purpose_mismatch_stops_before_write(self):
        inspect = {'Id': 'abc', 'State': {'Running': True},
                   'Config': {'Labels': {'purpose': 'other'}},
                   'HostConfig': {'PortBindings': {'5432/tcp': [{'HostPort': '15453'}]}}}
        with patch.object(tool, '_docker', return_value=completed(0, json.dumps([inspect]))):
            with self.assertRaises(tool.StageError):
                tool.inspect_container('resume-db-rehearsal-20261101', 'rehearsal-20261101')

    def test_container_without_published_port_is_rejected(self):
        inspect = {'Id': 'abc', 'State': {'Running': True},
                   'Config': {'Labels': {'purpose': 'rehearsal-20261101'}},
                   'HostConfig': {'PortBindings': {}}}
        with patch.object(tool, '_docker', return_value=completed(0, json.dumps([inspect]))):
            with self.assertRaises(tool.StageError):
                tool.inspect_container('resume-db-rehearsal-20261101', 'rehearsal-20261101')

    # --- pre-existing target and config failures ---
    def test_existing_target_is_refused_and_not_dropped(self):
        calls = []

        def fake_exec(container, script, *, password, args=(), timeout=300):
            calls.append(script)
            sql = args[1] if len(args) > 1 else ''
            if 'pg_database' in sql:
                return completed(0, '1\n')
            return completed(0, '')

        args = self.args
        with patch.object(tool, 'inspect_container', return_value={'id': 'abc', 'purpose': 'rehearsal-20261101', 'host_port': '15453'}), \
             patch.object(tool, '_exec', side_effect=fake_exec), \
             patch.object(tool, 'cleanup_target') as cleanup:
            code = tool.run(args, run_dir=self.run_dir)
        self.assertEqual(code, 2)
        cleanup.assert_not_called()
        report = json.loads(args.output.read_text())
        self.assertEqual(report['stage'], 'create')
        self.assertEqual(report['cleanup'], 'none')

    def test_missing_config_key_fails_before_writing(self):
        write_config(self.run_dir, drop_migration=True)
        with patch.object(tool, 'inspect_container') as inspect:
            code = tool.run(self.args, run_dir=self.run_dir)
        self.assertEqual(code, 2)
        inspect.assert_not_called()
        report = json.loads(self.args.output.read_text())
        self.assertEqual(report['stage'], 'config')

    def test_output_write_failure_returns_nonzero(self):
        args = self.args
        args.output = self.run_dir  # a directory cannot be written as a file
        with patch.object(tool, 'inspect_container', return_value={'id': 'abc', 'purpose': 'rehearsal-20261101', 'host_port': '15453'}), \
             patch.object(tool, '_exec', return_value=completed(0, '1\n')):
            code = tool.run(args, run_dir=self.run_dir)
        self.assertEqual(code, 2)

    # --- timeout and cleanup reporting ---
    def _run_with_restore_failure(self, restore_exc):
        def fake_exec(container, script, *, password, args=(), timeout=300):
            sql = args[1] if len(args) > 1 else ''
            if 'pg_database' in sql and 'SELECT 1' in sql:
                return completed(0, '0\n')
            if 'pg_restore' in script:
                if isinstance(restore_exc, Exception):
                    raise restore_exc
                return completed(restore_exc)
            return completed(0, '')

        with patch.object(tool, 'inspect_container', return_value={'id': 'abc', 'purpose': 'rehearsal-20261101', 'host_port': '15453'}), \
             patch.object(tool, '_exec', side_effect=fake_exec), \
             patch.object(tool, '_docker', return_value=completed(0, '')):
            return tool.run(self.args, run_dir=self.run_dir)

    def test_timeout_triggers_cleanup_and_reports_dropped(self):
        timeout = subprocess.TimeoutExpired(['docker'], 5)
        with patch.object(tool, 'cleanup_target', return_value={'status': 'dropped', 'drop_exit': 0, 'verify': 'absent'}) as cleanup:
            code = self._run_with_restore_failure(timeout)
        self.assertEqual(code, 2)
        cleanup.assert_called_once()
        report = json.loads(self.args.output.read_text())
        self.assertEqual(report['error_type'], 'TimeoutExpired')
        self.assertEqual(report['cleanup'], 'dropped')

    def test_cleanup_failure_is_not_reported_as_dropped(self):
        with patch.object(tool, 'cleanup_target', return_value={'status': 'failed', 'drop_exit': 1, 'verify': 'query_failed'}):
            code = self._run_with_restore_failure(1)
        self.assertEqual(code, 2)
        report = json.loads(self.args.output.read_text())
        self.assertEqual(report['cleanup'], 'failed')
        self.assertEqual(report['cleanup_detail'], {'drop_exit': 1, 'verify': 'query_failed'})

    def test_cleanup_reports_retained_when_query_finds_target(self):
        with patch.object(tool, '_exec') as exec_mock:
            exec_mock.side_effect = [completed(0, ''), completed(0, '1\n')]
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result, {'status': 'retained', 'drop_exit': 0, 'verify': 'present'})

    def test_cleanup_reports_dropped_only_after_successful_absence_query(self):
        with patch.object(tool, '_exec') as exec_mock:
            exec_mock.side_effect = [completed(0, ''), completed(0, '')]
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result, {'status': 'dropped', 'drop_exit': 0, 'verify': 'absent'})

    def test_cleanup_with_drop_and_query_both_failing_is_failed(self):
        with patch.object(tool, '_exec') as exec_mock:
            exec_mock.side_effect = [completed(1, ''), completed(1, '')]
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['drop_exit'], 1)
        self.assertEqual(result['verify'], 'query_failed')

    def test_cleanup_with_drop_ok_but_query_failing_is_failed(self):
        with patch.object(tool, '_exec') as exec_mock:
            exec_mock.side_effect = [completed(0, ''), completed(1, '')]
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['verify'], 'query_failed')

    def test_cleanup_with_failed_drop_but_verified_absence_is_dropped(self):
        with patch.object(tool, '_exec') as exec_mock:
            exec_mock.side_effect = [completed(1, ''), completed(0, '')]
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result, {'status': 'dropped', 'drop_exit': 1, 'verify': 'absent'})

    def test_cleanup_reports_failed_when_drop_raises(self):
        with patch.object(tool, '_exec', side_effect=subprocess.TimeoutExpired(['docker'], 5)):
            result = tool.cleanup_target(self.args, 'pw')
        self.assertEqual(result['status'], 'failed')

    # --- certificate write failure after a successful restore ---
    def test_certificate_write_failure_after_success_is_explicit(self):
        buffer = io.StringIO()
        self.args.output = self.run_dir  # directory path -> write fails
        with contextlib.redirect_stdout(buffer):
            code = tool.finish(self.args, {'target': 'resume_sql_preview_clone_x', 'passed': True,
                                           'production_database_cutover': False,
                                           'certificate_written': True}, 0)
        self.assertEqual(code, 2)
        report = json.loads(buffer.getvalue().strip())
        self.assertIs(report['passed'], False)
        self.assertIs(report['restore_passed'], True)
        self.assertIs(report['certificate_written'], False)
        self.assertEqual(report['clone_state'], 'retained')
        self.assertEqual(report['target'], 'resume_sql_preview_clone_x')

    def test_successful_restore_with_certificate_write_failure_keeps_clone(self):
        checks = {'schema_version': '0024_task_features', 'table_owners': '["resume_migrator"]',
                  'sequence_owners': '["resume_migrator"]', 'app_is_super': 'f',
                  'app_public_create': 'f', 'app_temp': 'f',
                  'readonly_select_users': 'f', 'readonly_insert_users': 'f',
                  'migrator_owns_tables': True, 'migrator_owns_sequences': True,
                  'app_safe': True, 'readonly_safe': True}

        def fake_exec(container, script, *, password, args=(), timeout=300):
            sql = args[1] if len(args) > 1 else ''
            if 'pg_database' in sql and 'SELECT 1' in sql:
                return completed(0, '0\n')
            return completed(0, '')

        self.args.output = self.run_dir  # directory path -> write fails
        buffer = io.StringIO()
        with patch.object(tool, 'inspect_container', return_value={'id': 'abc', 'purpose': 'rehearsal-20261101', 'host_port': '15453'}), \
             patch.object(tool, '_exec', side_effect=fake_exec), \
             patch.object(tool, '_docker', return_value=completed(0, 'True')), \
             patch.object(tool, 'verify_privileges', side_effect=lambda psql, target_checks, target: target_checks.update(checks)), \
             contextlib.redirect_stdout(buffer):
            code = tool.run(self.args, run_dir=self.run_dir)
        self.assertEqual(code, 2)
        report = json.loads(buffer.getvalue().strip())
        self.assertIs(report['passed'], False)
        self.assertIs(report['restore_passed'], True)
        self.assertIs(report['certificate_written'], False)
        self.assertEqual(report['clone_state'], 'retained')
        self.assertEqual(report['checks']['schema_version'], '0024_task_features')


if __name__ == '__main__':
    unittest.main()
