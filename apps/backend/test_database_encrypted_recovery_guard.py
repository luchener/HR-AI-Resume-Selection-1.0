"""Synthetic recovery orchestration must never touch a production container."""
import json
from pathlib import Path
import runpy
import subprocess
import unittest
from unittest.mock import Mock, patch


class RecoveryContainerGuards(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/database-test-encrypted-recovery.py'
        self.tools = runpy.run_path(str(path))

    def test_foreign_container_identities_fail_before_ssh(self):
        remote = Mock(side_effect=AssertionError('Unexpected SSH operation'))
        with patch.dict(self.tools['remove'].__globals__, remote=remote):
            for call in (
                lambda: self.tools['remove']('hr-ai-resume-selection-backend'),
                lambda: self.tools['docker']('resume-db-staging', ['psql']),
                lambda: self.tools['provision']('resume-db-staging', 'production', 5432, []),
                lambda: self.tools['provision'](self.tools['SOURCE'], 'production', 15439, []),
            ):
                with self.assertRaises(ValueError): call()
        remote.assert_not_called()

    def captured_code(self, action):
        codes = []
        def remote(code, **kwargs): codes.append(code); return b''
        with patch.dict(self.tools[action].__globals__, remote=remote):
            if action == 'docker': self.tools[action](self.tools['SOURCE'], ['psql'])
            else: self.tools[action](self.tools['TARGET'])
        return codes[0]

    def assert_guard(self, code, container):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:2] != ['docker', 'inspect']:
                self.fail('A foreign or mounted container was mutated')
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([container]))
        with patch('subprocess.run', side_effect=run), self.assertRaises(SystemExit):
            # Optimization must not remove the remote guard.
            exec(compile(code, 'synthetic-container-guard', 'exec', optimize=2), {})
        self.assertEqual(len(calls), 1)

    def test_exec_rejects_wrong_label_and_bind_or_named_volumes(self):
        code = self.captured_code('docker')
        for container in (
            {'Config': {'Labels': {'purpose': 'production'}}, 'Mounts': []},
            {'Config': {'Labels': {'purpose': self.tools['LABEL']}}, 'Mounts': [{'Type': 'bind'}]},
            {'Config': {'Labels': {'purpose': self.tools['LABEL']}}, 'Mounts': [{'Type': 'volume'}]},
        ):
            with self.subTest(container=container): self.assert_guard(code, container)

    def test_cleanup_rejects_wrong_label_and_mounted_data(self):
        code = self.captured_code('remove')
        for container in (
            {'Config': {'Labels': {}}, 'Mounts': []},
            {'Config': {'Labels': {'purpose': self.tools['LABEL']}}, 'Mounts': [{'Type': 'volume'}]},
        ):
            with self.subTest(container=container): self.assert_guard(code, container)


if __name__ == '__main__':
    unittest.main()
