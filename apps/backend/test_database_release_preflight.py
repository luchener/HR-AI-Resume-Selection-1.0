"""The read-only release report must not certify failed checks or cleanup."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import uuid
from unittest import TestCase
from unittest.mock import patch


class ReleasePreflightTests(TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'scripts/database-production-release-preflight.py'
        spec = importlib.util.spec_from_file_location('release_preflight', path)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def _run_synthetic_evidence_check(self, evidence, source_paths):
        with patch.object(self.runner, 'REQUIRED_EVIDENCE', {'synthetic.json': 1}), \
             patch.object(self.runner, '_source_files_from_manifest', return_value=tuple(source_paths)), \
             patch.object(self.runner, '_read_json', return_value=(evidence, None)):
            return self.runner._evidence_check('a' * 40, 'b' * 40)

    def _bound_evidence(self):
        path = Path(__file__).resolve()
        relative = path.relative_to(self.runner.ROOT).as_posix()
        digest = self.runner._source_digest_bytes(path.read_bytes())
        container_id = 'a' * 64
        review_session = 'b' * 32
        return relative, dict(
            tests={'run': 1, 'errors': 0, 'failures': 0, 'skipped': 0},
            production_acceptance=False,
            production_dual_write_enabled=False,
            passed=True,
            source_unchanged=True,
            temporary_container_removed=True,
            loopback_port_released=True,
            loopback_port_checks={'local': {
                'host': '127.0.0.1', 'port': 15439, 'scope': 'local',
                'method': 'connect-and-bind-v1', 'released': True}},
            container_identity={
                'container_id': container_id,
                'container_name': self.runner.CONTAINER_NAME,
                'review_session': review_session,
                'purpose_label': self.runner.CONTAINER_PURPOSE,
                'labels': {'purpose': self.runner.CONTAINER_PURPOSE,
                           'review-session': review_session},
                'image_reference': self.runner.CONTAINER_IMAGE,
                'image_id': 'sha256:' + 'c' * 64,
                'network_mode': self.runner.CONTAINER_NETWORK_MODE,
                'storage': {
                    'tmpfs_target': self.runner.TMPFS_TARGET,
                    'tmpfs_options': self.runner.TMPFS_OPTIONS,
                    'persistent_volume_count': 0,
                    'volume_ids': [],
                    'persistent_volume_absent': True,
                },
                'port': {
                    'container_port': '5432/tcp', 'host_ip': '127.0.0.1',
                    'host_port': 15439, 'binding_count': 1,
                    'host_config_matches': True, 'network_settings_matches': True,
                },
            },
            container_removal={
                'container_id': container_id,
                'container_name': self.runner.CONTAINER_NAME,
                'inspect_absent': True, 'container_id_absent': True,
                'port_released': True,
            },
            source_hash_mode=self.runner.SOURCE_HASH_MODE,
            source_sha256={relative: digest},
            source_binding={
                'passed': True, 'worktree_matches_head': True,
                'review_subject_matches_head': True,
                'commit': 'a' * 40, 'tree': 'b' * 40,
                'expected_commit': 'a' * 40, 'expected_tree': 'b' * 40,
            },
            source_binding_at_end={
                'passed': True, 'worktree_matches_head': True,
                'review_subject_matches_head': True,
                'commit': 'a' * 40, 'tree': 'b' * 40,
                'expected_commit': 'a' * 40, 'expected_tree': 'b' * 40,
            },
        )

    def test_source_manifest_is_complete(self):
        paths = self.runner._source_files_from_manifest()
        self.assertGreater(len(paths), 100)
        self.assertIn('apps/backend/app.py', paths)
        self.assertIn('scripts/database-source-manifest.json', paths)
        self.assertIn('apps/backend/test_security_fixes.py', paths)
        self.assertIn('apps/backend/test_quota.py', paths)
        self.assertIn('apps/backend/test_user_admin.py', paths)
        self.assertIn('apps/backend/test_login_lockout.py', paths)
        self.assertIn('apps/backend/system_config.py', paths)
        self.assertIn('apps/backend/llm.py', paths)
        self.assertIn('apps/backend/auth.py', paths)
        self.assertIn('apps/backend/quota.py', paths)
        self.assertIn('apps/backend/test_llm_config.py', paths)
        self.assertIn('apps/backend/test_config_dotenv.py', paths)
        self.assertIn('apps/backend/quota.py', paths)
        self.assertIn('apps/backend/migrations/versions/0018_change_log_compensation.py', paths)
        self.assertIn('apps/backend/migrations/versions/0019_change_log_state_machine.py', paths)
        self.assertIn('apps/backend/migrations/versions/0020_change_log_insert_guard.py', paths)
        self.assertIn('apps/backend/migrations/versions/0021_task_worker_lease.py', paths)
        self.assertIn('scripts/database-test-management.py', paths)
        self.assertIn('apps/backend/test_database_release_preflight.py', paths)
        self.assertIn('scripts/database-production-release-preflight.py', paths)
        self.assertIn('apps/backend/db_source_scope.py', paths)
        self.assertEqual(self.runner._source_hash_mode_from_manifest(),
                         self.runner.SOURCE_HASH_MODE)
        tracked = subprocess.check_output(
            ['git', 'ls-files', 'apps/backend', 'scripts'],
            cwd=self.runner.ROOT, text=True).splitlines()
        expected_tracked = {
            path for path in tracked
            if ((path.startswith('apps/backend/') and not path.lower().endswith('.pdf'))
                or path.startswith('scripts/database-')
                or path.startswith('scripts/database-drain-review/'))
        }
        self.assertEqual(expected_tracked - set(paths), set())

        harness_path = self.runner.ROOT / 'scripts' / 'database-test-management.py'
        spec = importlib.util.spec_from_file_location('synthetic_acceptance_harness', harness_path)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        self.assertEqual(tuple(paths), harness.SOURCE_FILES)
        self.assertEqual(self.runner._source_hash_mode_from_manifest(), harness.SOURCE_HASH_MODE)

    def test_review_package_path_and_json_are_fail_closed(self):
        commit, tree = 'a' * 40, 'b' * 40
        temp = self.runner.ROOT / '.test-tmp' / ('r05-package-' + uuid.uuid4().hex)
        temp.mkdir(parents=True)
        try:
            package = temp / 'package.json'
            package.write_text(json.dumps({'candidate_state': {
                'review_subject_commit': commit, 'review_subject_tree': tree,
            }}), encoding='utf-8')
            self.assertEqual(self.runner._load_review_subject(package), (commit, tree))
            package.write_text('{bad json', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.runner._load_review_subject(package)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        with self.assertRaises(ValueError):
            self.runner._load_review_subject(self.runner.ROOT.parent / 'missing-review-package.json')

    def test_manifest_rejects_duplicate_and_escaping_paths(self):
        valid = self.runner._load_source_manifest()
        variants = (
            valid['files'] + [valid['files'][0]],
            ['../outside.py', 'scripts/database-source-manifest.json'],
            ['apps\\backend\\auth.py', 'scripts/database-source-manifest.json'],
        )
        for paths in variants:
            temp = self.runner.ROOT / '.test-tmp' / ('r05-manifest-' + uuid.uuid4().hex)
            temp.mkdir(parents=True)
            try:
                path = temp / 'manifest.json'
                path.write_text(json.dumps({
                    'format': 1,
                    'source_hash_mode': self.runner.SOURCE_HASH_MODE,
                    'files': paths,
                }), encoding='utf-8')
                with self.subTest(paths=paths[:2]), \
                     patch.object(self.runner, 'SOURCE_MANIFEST_PATH', path), \
                     self.assertRaises(ValueError):
                    self.runner._load_source_manifest()
            finally:
                shutil.rmtree(temp, ignore_errors=True)

    def test_manifest_format_requires_a_json_integer(self):
        valid = self.runner._load_source_manifest()
        for invalid_format in (True, False, 1.0, "1", None):
            temp = self.runner.ROOT / '.test-tmp' / ('r05-format-' + uuid.uuid4().hex)
            temp.mkdir(parents=True)
            try:
                path = temp / 'manifest.json'
                variant = dict(valid, format=invalid_format)
                path.write_text(json.dumps(variant), encoding='utf-8')
                with self.subTest(format=invalid_format), \
                     patch.object(self.runner, 'SOURCE_MANIFEST_PATH', path), \
                     self.assertRaises(ValueError):
                    self.runner._load_source_manifest()
            finally:
                shutil.rmtree(temp, ignore_errors=True)

    def test_review_subject_requires_full_commit_and_tree_ids(self):
        commit, tree = 'a' * 40, 'b' * 40
        self.assertEqual(self.runner._review_subject_from_data({
            'candidate_state': {
                'review_subject_commit': commit,
                'review_subject_tree': tree,
            }
        }), (commit, tree))
        variants = (
            {'review_subject_commit': 'a' * 7, 'review_subject_tree': tree},
            {'review_subject_commit': commit, 'review_subject_tree': 'z' * 40},
            {'review_subject_commit': commit},
        )
        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.runner._review_subject_from_data({'candidate_state': candidate})

    def test_synthetic_runner_rejects_dirty_review_subject_before_docker(self):
        harness_path = self.runner.ROOT / 'scripts/database-test-management.py'
        spec = importlib.util.spec_from_file_location('synthetic_subject_guard', harness_path)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        destination = self.runner.ROOT / 'docs/database-evidence' / (
            'rejected-subject-' + uuid.uuid4().hex + '.json')
        argv = ['database-test-management.py', '--execute-synthetic', '--local',
                '--evidence', str(destination), '--review-package', 'synthetic-package.json']
        with patch.object(sys, 'argv', argv), \
             patch.object(harness, 'review_source_binding', return_value={'passed': False}), \
             patch.object(harness, 'remote') as docker:
            with self.assertRaisesRegex(SystemExit, 'before container creation'):
                harness.main()
        docker.assert_not_called()
        self.assertFalse(destination.exists())

    def test_source_hash_is_line_ending_independent(self):
        lf = self.runner._source_digest_bytes(b'alpha\nbeta\n')
        crlf = self.runner._source_digest_bytes(b'alpha\r\nbeta\r\n')
        mixed = self.runner._source_digest_bytes(b'alpha\r\nbeta\r')
        self.assertEqual(lf, crlf)
        self.assertEqual(lf, mixed)

    def test_required_evidence_uses_final10_successor_set(self):
        self.assertEqual(self.runner.REQUIRED_EVIDENCE, {
            '2026-10-29-r03-task-final10.json': 73,
            '2026-10-29-r03-admin-ai-final10.json': 14,
            '2026-10-29-r03-full-final10.json': 427,
            '2026-10-29-r03-retention-final10.json': 15,
            '2026-10-29-r03-drain-final10.json': 1,
            '2026-10-29-r03-email-final10.json': 77,
        })

    def test_evidence_rejects_missing_mismatched_and_changed_git_binding(self):
        relative, evidence = self._bound_evidence()
        variants = {}
        for label in ('source_binding', 'source_binding_at_end'):
            variants[label + '_missing'] = {key: value for key, value in evidence.items()
                                            if key != label}
            for key, value in (('commit', 'c' * 40), ('tree', 'd' * 40),
                               ('passed', False), ('worktree_matches_head', False),
                               ('expected_commit', None), ('review_subject_matches_head', False)):
                variant = deepcopy(evidence)
                variant[label][key] = value
                variants[label + '_' + key] = variant
        for label, variant in variants.items():
            with self.subTest(label=label):
                result = self._run_synthetic_evidence_check(variant, (relative,))
                self.assertFalse(result['passed'])
                self.assertTrue(any('source_binding' in item for item in result['failures']))

    def test_evidence_requires_complete_current_source_manifest(self):
        relative, evidence = self._bound_evidence()
        result = self._run_synthetic_evidence_check(evidence, (relative,))
        self.assertTrue(result['passed'])
        self.assertEqual(result['source_manifest_files'], 1)
        self.assertTrue(result['files']['synthetic.json']['source_manifest']['passed'])

        variants = {
            'missing_manifest': {key: value for key, value in evidence.items()
                                 if key != 'source_sha256'},
            'wrong_hash': {**evidence, 'source_sha256': {relative: '0' * 64}},
            'non_hex_hash': {**evidence, 'source_sha256': {relative: 'z' * 64}},
            'missing_path': {**evidence, 'source_sha256': {}},
            'missing_hash_mode': {key: value for key, value in evidence.items()
                                  if key != 'source_hash_mode'},
            'wrong_hash_mode': {**evidence, 'source_hash_mode': 'raw-bytes-sha256'},
            'extra_path': {**evidence, 'source_sha256': {
                relative: evidence['source_sha256'][relative],
                'apps/backend/not-in-manifest.py': '0' * 64,
            }},
        }
        for label, variant in variants.items():
            with self.subTest(label=label):
                result = self._run_synthetic_evidence_check(variant, (relative,))
                self.assertFalse(result['passed'])
                self.assertIn('source manifest', ' '.join(result['failures']))

    def test_unreadable_source_manifest_entry_is_rejected(self):
        relative, evidence = self._bound_evidence()
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('denied')):
            result = self._run_synthetic_evidence_check(evidence, (relative,))
        manifest = result['files']['synthetic.json']['source_manifest']
        self.assertFalse(result['passed'])
        self.assertEqual(manifest['unreadable'], [relative])
        self.assertIn('unreadable', ' '.join(manifest['failures']))

    def test_evidence_requires_loopback_release_proof(self):
        relative, evidence = self._bound_evidence()
        variants = {
            'missing_port_release': {key: value for key, value in evidence.items()
                                     if key != 'loopback_port_released'},
            'false_port_release': {**evidence, 'loopback_port_released': False},
            'missing_port_checks': {key: value for key, value in evidence.items()
                                    if key != 'loopback_port_checks'},
            'empty_port_checks': {**evidence, 'loopback_port_checks': {}},
            'missing_local_check': {**evidence, 'loopback_port_checks': {'remote': {
                **evidence['loopback_port_checks']['local'], 'scope': 'remote'}}},
            'wrong_host': {**evidence, 'loopback_port_checks': {'local': {
                **evidence['loopback_port_checks']['local'], 'host': '0.0.0.0'}}},
            'wrong_scope': {**evidence, 'loopback_port_checks': {'local': {
                **evidence['loopback_port_checks']['local'], 'scope': 'remote'}}},
            'wrong_port': {**evidence, 'loopback_port_checks': {'local': {
                **evidence['loopback_port_checks']['local'], 'port': 15440}}},
            'wrong_method': {**evidence, 'loopback_port_checks': {'local': {
                **evidence['loopback_port_checks']['local'], 'method': 'unknown'}}},
            'item_not_released': {**evidence, 'loopback_port_checks': {'local': {
                **evidence['loopback_port_checks']['local'], 'released': False}}},
        }
        for label, variant in variants.items():
            with self.subTest(label=label):
                result = self._run_synthetic_evidence_check(variant, (relative,))
                self.assertFalse(result['passed'])
                self.assertIn('loopback port', ' '.join(result['failures']))

    def test_evidence_requires_container_storage_port_and_removal_identity(self):
        relative, evidence = self._bound_evidence()
        variants = {
            'missing_identity': {key: value for key, value in evidence.items()
                                 if key != 'container_identity'},
            'missing_removal': {key: value for key, value in evidence.items()
                                if key != 'container_removal'},
        }

        def changed(section, path, replacement, label):
            variant = deepcopy(evidence)
            target = variant[section]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            variants[label] = variant

        changed('container_identity', ('container_id',), 'bad', 'invalid_id')
        changed('container_identity', ('container_name',), 'production', 'wrong_name')
        changed('container_identity', ('review_session',), 'c' * 32, 'wrong_session')
        changed('container_identity', ('purpose_label',), 'production', 'wrong_purpose')
        changed('container_identity', ('image_reference',), 'postgres:latest', 'wrong_image')
        changed('container_identity', ('image_id',), 'sha256:bad', 'wrong_image_id')
        changed('container_identity', ('network_mode',), 'host', 'wrong_network')
        changed('container_identity', ('storage', 'persistent_volume_count'), 1,
                'persistent_volume')
        changed('container_identity', ('storage', 'volume_ids'), ['volume-id'],
                'volume_ids')
        changed('container_identity', ('storage', 'tmpfs_target'), '/tmp', 'wrong_tmpfs')
        changed('container_identity', ('storage', 'tmpfs_options'), 'rw', 'wrong_tmpfs_options')
        changed('container_identity', ('port', 'host_ip'), '0.0.0.0', 'wrong_bind_host')
        changed('container_identity', ('port', 'host_port'), 15440, 'wrong_bind_port')
        changed('container_identity', ('port', 'binding_count'), 2, 'extra_binding')
        changed('container_identity', ('port', 'host_config_matches'), False,
                'host_config_mismatch')
        changed('container_identity', ('port', 'network_settings_matches'), False,
                'network_settings_mismatch')
        changed('container_removal', ('container_id',), 'd' * 64, 'removal_id_mismatch')
        changed('container_removal', ('inspect_absent',), False, 'container_still_present')
        changed('container_removal', ('container_id_absent',), False, 'container_id_present')
        changed('container_removal', ('port_released',), False, 'container_port_still_bound')

        for label, variant in variants.items():
            with self.subTest(label=label):
                result = self._run_synthetic_evidence_check(variant, (relative,))
                self.assertFalse(result['passed'])
                self.assertIn('container identity', ' '.join(result['failures']))

    def test_evidence_rejects_non_integer_counter_and_port_scalars(self):
        relative, evidence = self._bound_evidence()
        fields = (
            (('tests', 'run'), (True, 1.0, '1', None)),
            (('tests', 'errors'), (False, 0.0, '0', None)),
            (('tests', 'failures'), (False, 0.0, '0', None)),
            (('tests', 'skipped'), (False, 0.0, '0', None)),
            (('container_identity', 'storage', 'persistent_volume_count'),
             (False, 0.0, '0', None)),
            (('container_identity', 'port', 'host_port'),
             (True, 15439.0, '15439', None)),
            (('container_identity', 'port', 'binding_count'),
             (True, 1.0, '1', None)),
            (('loopback_port_checks', 'local', 'port'),
             (True, 15439.0, '15439', None)),
        )
        for path, values in fields:
            for value in values:
                variant = deepcopy(evidence)
                target = variant
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.subTest(path=path, value=value):
                    result = self._run_synthetic_evidence_check(variant, (relative,))
                    self.assertFalse(result['passed'])

    def test_evidence_rejects_unknown_and_malformed_container_fields(self):
        relative, evidence = self._bound_evidence()
        variants = {}

        def changed(path, replacement, label):
            variant = deepcopy(evidence)
            target = variant
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            variants[label] = variant

        def removed(path, label):
            variant = deepcopy(evidence)
            target = variant
            for key in path[:-1]:
                target = target[key]
            target.pop(path[-1])
            variants[label] = variant

        changed(('container_identity', 'unexpected_extra'), 'x',
                'identity_unknown_key')
        changed(('container_identity', 'labels', 'unexpected_extra'), 'x',
                'label_unknown_key')
        changed(('container_identity', 'storage', 'unexpected_extra'), 'x',
                'storage_unknown_key')
        changed(('container_identity', 'port', 'unexpected_extra'), 'x',
                'port_unknown_key')
        changed(('container_removal', 'unexpected_extra'), 'x',
                'removal_unknown_key')
        removed(('container_identity', 'storage'), 'missing_storage')
        removed(('container_identity', 'port'), 'missing_port')
        removed(('container_identity', 'labels'), 'missing_labels')
        removed(('container_removal', 'container_name'), 'missing_removal_name')
        changed(('container_identity', 'storage'), 'not-an-object',
                'storage_not_object')
        changed(('container_identity', 'port'), [], 'port_not_object')
        changed(('container_identity', 'labels'), 'not-an-object',
                'labels_not_object')
        variants['removal_not_object'] = {**evidence, 'container_removal': []}

        for label, variant in variants.items():
            with self.subTest(label=label):
                result = self._run_synthetic_evidence_check(variant, (relative,))
                self.assertFalse(result['passed'])
                self.assertTrue(result['failures'])

    def test_failed_syntax_or_evidence_cannot_be_reported_as_passed(self):
        for failed in ['_syntax_check', '_evidence_check', '_git_diff_check', '_safe_environment_check']:
            with patch.multiple(self.runner,
                    _syntax_check=lambda: {'passed': failed != '_syntax_check'},
                    _evidence_check=lambda *_args: {'passed': failed != '_evidence_check'},
                    _git_diff_check=lambda: {'passed': failed != '_git_diff_check'},
                    _safe_environment_check=lambda: {'passed': failed != '_safe_environment_check'},
                    _route_coverage_check=lambda: {'passed': True}, _local_health_check=lambda: {}):
                self.assertEqual(self.runner.build_report(expected_commit='a' * 40,
                    expected_tree='b' * 40)['status'], 'preflight_failed')

    def test_successful_checks_still_report_missing_routes_as_release_blockers(self):
        with patch.multiple(self.runner, _syntax_check=lambda: {'passed': True},
                _evidence_check=lambda *_args: {'passed': True}, _git_diff_check=lambda: {'passed': True},
                _git_binding=lambda *_args: {'passed': True, 'failures': []},
                _safe_environment_check=lambda: {'passed': True}, _local_health_check=lambda: {},
                _route_coverage_check=lambda: {'passed': False, 'missing_routes': ['POST /api/example']}):
            report = self.runner.build_report(expected_commit='a' * 40, expected_tree='b' * 40)
        self.assertEqual(report['status'], 'preflight_passed_with_release_blockers')
        self.assertTrue(any('1 个生产接口' in item for item in report['release_blockers']))
        self.assertFalse(report['production_runtime']['dual_write_enabled'])

    def test_report_binds_git_commit_tree_and_source_manifest(self):
        binding = {'passed': True, 'commit': 'a' * 40, 'tree': 'b' * 40,
                   'expected_commit': 'a' * 40, 'expected_tree': 'b' * 40,
                   'review_subject_matches_head': True, 'failures': []}
        with patch.multiple(self.runner,
                _syntax_check=lambda: {'passed': True},
                _evidence_check=lambda *_args: {'passed': True, 'source_manifest_files': 7,
                                         'source_manifest_sha256': 'c' * 64},
                _git_diff_check=lambda: {'passed': True},
                _git_binding=lambda *_args: binding,
                _safe_environment_check=lambda: {'passed': True},
                _local_health_check=lambda: {},
                _route_coverage_check=lambda: {'passed': True}):
            report = self.runner.build_report(expected_commit='a' * 40, expected_tree='b' * 40)
        self.assertEqual(report['source_binding']['commit'], 'a' * 40)
        self.assertEqual(report['source_binding']['tree'], 'b' * 40)
        self.assertEqual(report['source_binding']['expected_commit'], 'a' * 40)
        self.assertEqual(report['source_binding']['expected_tree'], 'b' * 40)
        self.assertEqual(report['review_subject']['expected_commit'], 'a' * 40)
        self.assertEqual(report['review_subject']['expected_tree'], 'b' * 40)
        self.assertTrue(report['review_subject']['matches_head'])
        self.assertEqual(report['source_binding']['source_manifest_files'], 7)
        self.assertEqual(report['source_binding']['source_manifest_sha256'], 'c' * 64)

    def test_report_fails_when_git_binding_is_unavailable(self):
        with patch.multiple(self.runner,
                _syntax_check=lambda: {'passed': True},
                _evidence_check=lambda *_args: {'passed': True},
                _git_diff_check=lambda: {'passed': True},
                _git_binding=lambda *_args: {'passed': False, 'commit': None, 'tree': None,
                                      'failures': ['git commit unavailable']},
                _safe_environment_check=lambda: {'passed': True},
                _local_health_check=lambda: {},
                _route_coverage_check=lambda: {'passed': True}):
            report = self.runner.build_report(expected_commit='a' * 40, expected_tree='b' * 40)
        self.assertEqual(report['status'], 'preflight_failed')

    def test_git_binding_requires_expected_review_subject(self):
        commit, tree = 'a' * 40, 'b' * 40

        def fake_git(args, **_kwargs):
            output = commit if args[-1] == 'HEAD' else tree
            return SimpleNamespace(returncode=0, stdout=output + '\n')

        with patch.object(self.runner.subprocess, 'run', side_effect=fake_git), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths', return_value=set()):
            matching = self.runner._git_binding(commit, tree)
        self.assertTrue(matching['passed'])
        self.assertTrue(matching['review_subject_matches_head'])

        with patch.object(self.runner.subprocess, 'run', side_effect=fake_git), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths', return_value=set()):
            mismatch = self.runner._git_binding('c' * 40, tree)
        self.assertFalse(mismatch['passed'])
        self.assertIn('HEAD commit differs from review subject commit', mismatch['failures'])

        with patch.object(self.runner.subprocess, 'run', side_effect=fake_git), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths', return_value=set()):
            missing = self.runner._git_binding(None, tree)
        self.assertFalse(missing['passed'])
        self.assertIn('expected review commit is missing or invalid', missing['failures'])

        outputs = iter((commit + '\n', tree + '\n'))
        with patch.object(self.runner.subprocess, 'run', side_effect=lambda *_args, **_kwargs:
                          SimpleNamespace(returncode=0, stdout=next(outputs))), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths', return_value=set()):
            missing_tree = self.runner._git_binding(commit, None)
        self.assertFalse(missing_tree['passed'])
        self.assertIn('expected review tree is missing or invalid', missing_tree['failures'])

        outputs = iter((commit + '\n', tree + '\n'))
        with patch.object(self.runner.subprocess, 'run', side_effect=lambda *_args, **_kwargs:
                          SimpleNamespace(returncode=0, stdout=next(outputs))), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths', return_value=set()):
            tree_mismatch = self.runner._git_binding(commit, 'd' * 40)
        self.assertFalse(tree_mismatch['passed'])
        self.assertIn('HEAD tree differs from review subject tree', tree_mismatch['failures'])

    def test_git_binding_rejects_dirty_manifest_sources(self):
        with patch.object(self.runner, '_source_files_from_manifest',
                          return_value=('apps/backend/app.py',)), \
             patch.object(self.runner, '_git_dirty_source_paths',
                          return_value={'apps/backend/app.py'}), \
             patch.object(self.runner, '_tracked_source_paths', return_value={'apps/backend/app.py'}), \
             patch.object(self.runner.subprocess, 'run') as run:
            run.side_effect = [
                type('Result', (), {'returncode': 0, 'stdout': 'a' * 40 + '\n'})(),
                type('Result', (), {'returncode': 0, 'stdout': 'b' * 40 + '\n'})(),
            ]
            binding = self.runner._git_binding()
        self.assertFalse(binding['passed'])
        self.assertFalse(binding['worktree_matches_head'])
        self.assertEqual(binding['dirty_source_files'], ['apps/backend/app.py'])

    def test_git_binding_fails_if_tracked_source_is_unmanifested(self):
        commit, tree = 'a' * 40, 'b' * 40
        outputs = iter((commit + '\n', tree + '\n'))
        with patch.object(self.runner.subprocess, 'run', side_effect=lambda *_args, **_kwargs:
                          SimpleNamespace(returncode=0, stdout=next(outputs))), \
             patch.object(self.runner, '_source_files_from_manifest',
                          return_value=('apps/backend/app.py',)), \
             patch.object(self.runner, '_git_dirty_source_paths', return_value=set()), \
             patch.object(self.runner, '_tracked_source_paths',
                          return_value={'apps/backend/app.py', 'apps/backend/auth.py'}):
            binding = self.runner._git_binding(commit, tree)
        self.assertFalse(binding['passed'])
        self.assertEqual(binding['unmanifested_tracked_files'], ['apps/backend/auth.py'])
        self.assertIn('1 tracked source files are missing from the manifest', binding['failures'])

    def test_green_test_counts_cannot_hide_failed_acceptance_or_cleanup(self):
        evidence = dict(tests={'run': 1, 'errors': 0, 'failures': 0, 'skipped': 0},
            production_acceptance=False, production_dual_write_enabled=False,
            passed=True, source_unchanged=True, temporary_container_removed=True,
            loopback_port_released=True, loopback_port_checks={'local': {
                'host': '127.0.0.1', 'port': 15439, 'scope': 'local',
                'method': 'connect-and-bind-v1', 'released': True}})
        for field in ['passed', 'source_unchanged', 'temporary_container_removed']:
            with patch.object(self.runner, 'REQUIRED_EVIDENCE', {'synthetic.json': 1}), \
                 patch.object(self.runner, '_read_json', return_value=({**evidence, field: False}, None)):
                self.assertFalse(self.runner._evidence_check()['passed'])
