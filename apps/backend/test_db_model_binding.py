from copy import deepcopy
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import sqlalchemy as sa
from db_model_binding import bind_model
from db_screening import SQLScreeningAnalyzer
from db_task_inputs import task_inputs


class BindingUnitTests(unittest.TestCase):
    def test_prepared_analyzers_are_independent_and_do_not_share_mutable_config(self):
        from test_screening_agent import ScreeningAgentTests
        gateway = Mock()
        gateway.model_snapshot.side_effect = [({'api_key': 'Old'}, 'a' * 64), ({'api_key': 'New'}, 'b' * 64)]
        analyzer = SQLScreeningAnalyzer(gateway)
        first, old = analyzer.prepare_task()
        second, new = analyzer.prepare_task()
        calls = []
        def complete(*args, **kwargs):
            calls.append(kwargs['runtime_config']['api_key'])
            kwargs['runtime_config']['api_key'] = 'Mutated'
            return {}
        gateway.complete.side_effect = complete
        def agent(**kwargs):
            kwargs['call_llm']('synthetic', expect_json=True, max_tokens=10, runtime_config={})
            return {**ScreeningAgentTests()._report(), 'agent_validation': {'passed': True, 'issues': []}}
        with patch('db_screening.run_screening_agent', side_effect=agent):
            for bound in (first, second, first):
                bound(resume={'content': 'Python项目'}, job={'content': 'Python'})
        self.assertEqual(calls, ['Old', 'New', 'Old'])
        self.assertNotEqual(old['binding_id'], new['binding_id'])
        self.assertEqual(set(old), {'binding_id', 'config_revision', 'policy_revision'})
        self.assertNotIn('api_key', json.dumps(old))


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class BindingLiveTests(unittest.TestCase):
    def setUp(self):
        from test_db_task_events import TaskEventTests
        TaskEventTests.setUp(self)

    def submit(self, **kwargs):
        from test_db_analysis_workflow import AnalysisWorkflowTests
        return AnalysisWorkflowTests.submit(self, **kwargs)

    def usage(self):
        from test_db_analysis_workflow import AnalysisWorkflowTests
        return AnalysisWorkflowTests.usage(self)

    def snapshot(self, task):
        return self.workflow.snapshot(task_id=task, owner_user_id=self.owner, password_version=1)

    def test_batch_uses_one_snapshot_and_next_task_sees_new_configuration(self):
        from test_screening_agent import ScreeningAgentTests
        from db_provider_gateway import SQLProviderGateway
        from db_secrets import ConfigCipher, EncryptedConfigRepository
        from cryptography.fernet import Fernet
        cipher = ConfigCipher(Fernet.generate_key(), key_id='synthetic-batch')
        gateway = SQLProviderGateway(self.sessions, cipher)
        def save(key):
            with self.sessions() as session, session.begin():
                EncryptedConfigRepository(session, cipher).put({'system_config/llm.json': {
                    'api_key': key, 'model': 'synthetic', 'base_url': 'https://api.example.test'}})
        save('OldPrivate')
        self.workflow.analyzer = SQLScreeningAnalyzer(gateway)
        calls = []
        def model(*args, **kwargs):
            calls.append(deepcopy(kwargs['runtime_config']))
            if len(calls) == 1:
                save('NewPrivate')
            return {}
        def agent(**kwargs):
            kwargs['call_llm']('synthetic', expect_json=True, max_tokens=10, runtime_config={})
            return {**ScreeningAgentTests()._report(), 'agent_validation': {'passed': True, 'issues': []}}
        with patch('db_screening.run_screening_agent', side_effect=agent), \
             patch.object(gateway, 'complete', side_effect=model), \
             patch.object(gateway, 'model_snapshot', wraps=gateway.model_snapshot) as prepare:
            task, future = self.submit(resumes=self.resumes[:2])
            self.assertTrue(future.result(10))
            self.assertEqual(prepare.call_count, 1)
            first = self.snapshot(task)
            self.assertEqual((first['status'], first['completed_candidates']), ('completed', 2))
            same, replay = self.submit(resumes=self.resumes[:2])
            self.assertEqual(same, task)
            self.assertFalse(replay.result(10))
            self.assertEqual(self.snapshot(task), first)
            following, future = self.submit(resumes=self.resumes[2:3], key=str(uuid4()))
            self.assertTrue(future.result(10))
        self.assertEqual([cfg['api_key'] for cfg in calls], ['OldPrivate', 'OldPrivate', 'NewPrivate'])
        self.assertNotEqual(self.snapshot(following)['model_binding']['config_revision'], first['model_binding']['config_revision'])
        self.assertNotIn('OldPrivate', json.dumps(first))
        self.assertEqual(self.usage(), (3, 0))

    def test_configuration_failure_never_calls_provider_and_releases_reservation(self):
        gateway = Mock()
        gateway.model_snapshot.side_effect = ValueError('PRIVATE_CONFIG_KEY')
        self.workflow.analyzer = SQLScreeningAnalyzer(gateway)
        task, future = self.submit(resumes=self.resumes[:2])
        self.assertTrue(future.result(10))
        gateway.complete.assert_not_called()
        result = self.snapshot(task)
        self.assertEqual((result['status'], result['failed_candidates']), ('failed', 2))
        self.assertNotIn('PRIVATE_CONFIG_KEY', json.dumps(result))
        self.assertEqual(self.usage(), (0, 0))

    def test_binding_is_write_once_fenced_and_blocks_destructive_downgrade(self):
        from test_db_analysis_workflow import report
        def blocked(**kwargs):
            self.started.set()
            self.release.wait(10)
            return report()
        self.workflow.analyzer = blocked
        task, future = self.submit()
        self.assertTrue(self.started.wait(5))
        with self.admin.connect() as conn:
            token = conn.scalar(sa.select(task_inputs.c.worker_token).where(task_inputs.c.task_id == task))
        binding = dict(binding_id=str(uuid4()), config_revision='c' * 64, policy_revision='d' * 64)
        args = dict(task_id=UUID(task), owner_user_id=UUID(self.owner), binding=binding)
        with self.assertRaises(ValueError), self.engine.begin() as conn:
            bind_model(conn, worker_token=uuid4(), **args)
        with self.engine.begin() as conn:
            bind_model(conn, worker_token=token, **args)
        with self.assertRaises(ValueError), self.engine.begin() as conn:
            bind_model(conn, worker_token=token, **args)
        with self.assertRaises(sa.exc.DBAPIError), self.engine.begin() as conn:
            conn.execute(task_inputs.update().where(task_inputs.c.task_id == task).values(model_binding=sa.null()))
        with self.assertRaises(sa.exc.DBAPIError), self.engine.begin() as conn:
            conn.execute(task_inputs.delete().where(task_inputs.c.task_id == task))
        from alembic import command
        from alembic.config import Config
        from pathlib import Path
        config = Config(str(Path(__file__).parent / 'alembic.ini'))
        config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        from alembic.script import ScriptDirectory
        migration = ScriptDirectory.from_config(config).get_revision('0023_model_binding').module
        with self.admin.connect() as conn, patch.object(migration.op, 'get_bind', return_value=conn):
            with self.assertRaisesRegex(ValueError, 'Reconcile model bindings'):
                migration.downgrade()
        self.release.set()
        self.assertTrue(future.result(10))

    def test_ciphertext_revision_refresh_and_explicit_fallback_do_not_hash_plaintext(self):
        from db_provider_gateway import SQLProviderGateway
        from db_secrets import ConfigCipher, EncryptedConfigRepository
        from cryptography.fernet import Fernet
        cipher = ConfigCipher(Fernet.generate_key(), key_id='synthetic-binding')
        gateway = SQLProviderGateway(self.sessions, cipher)
        revisions = []
        for key in ('SQLSecret1', 'SQLSecret2'):
            with self.sessions() as session, session.begin():
                EncryptedConfigRepository(session, cipher).put({'system_config/llm.json': {
                    'api_key': key, 'model': 'synthetic', 'base_url': 'https://api.example.test'}})
            cfg, revision = gateway.model_snapshot()
            revisions.append(revision)
            self.assertEqual(cfg['api_key'], key)
            self.assertEqual(gateway.model_snapshot()[1], revision)
            self.assertEqual(SQLProviderGateway(self.sessions, cipher).model_snapshot()[1], revision)
        self.assertNotEqual(*revisions)
