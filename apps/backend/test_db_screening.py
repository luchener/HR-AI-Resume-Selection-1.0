import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import hr_normalization as core
import screening_agent
from db_screening import SQLScreeningAnalyzer, screening_adapter_factory


class ScreeningIntegrationTests(unittest.TestCase):
    def test_normalizer_helpers_match_existing_algorithm_ast(self):
        class RestoreConfig(ast.NodeTransformer):
            def visit_Call(self, node):
                if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == '_policy' and node.func.attr == 'get'):
                    return ast.Name(id='config', ctx=ast.Load())
                return self.generic_visit(node)
        def definitions(filename):
            tree = ast.parse(Path(filename).read_text(encoding='utf-8'))
            found = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            found[target.id] = node
            return found
        original = definitions('app.py')
        extracted = definitions('hr_normalization.py')
        checked = 0
        for name, node in extracted.items():
            if name in original:
                self.assertEqual(ast.dump(RestoreConfig().visit(node), include_attributes=False),
                                 ast.dump(original[name], include_attributes=False), name)
                checked += 1
        self.assertGreater(checked, 20)

    def test_normalizer_matches_legacy_results_for_both_deduction_policies(self):
        import app
        from test_screening_agent import ScreeningAgentTests
        samples = [{}, ScreeningAgentTests()._report(), {'evidence_pipeline_version': 'evidence-v1',
            'evidence_assessments': [{'id': 'r1', 'text': 'Redis实践', 'category': 'skill', 'hard': True,
            'logic': 'required', 'status': 'met', 'source_quote': '使用Redis实现缓存',
            'evidence_kind': 'direct', 'reason': '原文明确'}]}]
        for mode in ('readonly', 'enforce'):
            for raw in samples:
                with patch.object(app.config, 'AI_RISK_DEDUCTION_MODE', mode), \
                     patch.object(app.config, 'DECEPTION_CHECK_MODE', 'off'):
                    expected = app._normalize_hr_analysis(deepcopy(raw), '要求Redis实践', '使用Redis实现缓存')
                self.assertEqual(core.normalize_hr_analysis(deepcopy(raw), '要求Redis实践', '使用Redis实现缓存',
                    policy=core.NormalizationPolicy(mode, 'off')), expected)

    def test_explicit_agent_model_never_uses_legacy_and_keeps_call_budget(self):
        from test_screening_agent import ScreeningAgentTests
        callback = Mock(return_value=ScreeningAgentTests()._report())
        with patch.object(screening_agent.llm, 'call_llm', side_effect=AssertionError('Legacy model used')):
            result = screening_agent.run_screening_agent(job_content='Python', resume_content='Python项目',
                current_date='2026-10', precomputed_requirements={'requirements': []}, call_llm=callback)
        self.assertGreater(callback.call_count, 0)
        self.assertLessEqual(callback.call_count, screening_agent.MAX_AGENT_LLM_CALLS)
        self.assertEqual(result['agent_meta']['llm_calls'], callback.call_count)
        with self.assertRaises(ValueError):
            screening_agent.run_screening_agent(job_content='Python', resume_content='Python项目',
                current_date='2026-10', call_llm=callback, agent_config={'web_search': True})

    def test_sql_analyzer_freezes_configuration_and_emits_real_agent_events(self):
        from test_screening_agent import ScreeningAgentTests
        events, calls, budgets = [], [], []
        gateway = Mock()
        gateway.configuration.return_value = {'model': 'snapshot', 'api_key': 'private'}
        def complete(*args, **kwargs):
            calls.append(kwargs['runtime_config'].copy())
            budgets.append(kwargs['max_tokens'])
            if len(calls) == 1:
                return {'requirements': []}
            return ScreeningAgentTests()._report()
        gateway.complete.side_effect = complete
        result = SQLScreeningAnalyzer(gateway, policy=core.NormalizationPolicy(DECEPTION_CHECK_MODE='off'),
            on_event=events.append)(resume={'content': 'Python项目'}, job={'content': 'Python'})
        gateway.configuration.assert_called_once_with('llm')
        self.assertTrue(all(cfg == calls[0] for cfg in calls))
        self.assertEqual(budgets[:2], [2200, 8192])
        self.assertIn('generating', [event['status'] for event in events])
        self.assertIn('scoring_meta', result)
        self.assertIn('agent_validation', result)

    def test_requirements_cache_only_reuses_matching_model_and_policy_revisions(self):
        from test_screening_agent import ScreeningAgentTests
        gateway = Mock()
        gateway.complete.side_effect = lambda *args, **kwargs: (
            {'requirements': [{'id': 'r1', 'text': 'Python', 'category': 'skill',
                               'hard': True, 'logic': 'required'}]} if kwargs['screening_call'][0] == 'planning'
            else ScreeningAgentTests()._report())
        analyzer = SQLScreeningAnalyzer(gateway)
        fixture = dict(resume={'content': 'Python fixture'}, job={'content': 'Python cache fixture'})
        with patch('db_screening._JOB_REQUIREMENTS_CACHE', {}):
            for config, policy, should_extract in (
                ('a', 'v1', True), ('a', 'v1', False), ('b', 'v1', True), ('b', 'v2', True),
            ):
                gateway.reset_mock()
                gateway.model_snapshot.return_value = ({'model': f'model-{config}'}, config * 64)
                with patch('db_screening.REQUIREMENTS_EXTRACTION_VERSION', policy):
                    bound, _ = analyzer.prepare_task()
                    bound(**fixture)
                stages = [call.kwargs['screening_call'][0] for call in gateway.complete.call_args_list]
                self.assertEqual('planning' in stages, should_extract)

    def test_sql_report_budget_scales_with_requirements_or_resume_length(self):
        from test_screening_agent import ScreeningAgentTests
        gateway = Mock()
        analyzer = SQLScreeningAnalyzer(gateway)
        for requirement_count, resume_chars, expected in (
            (11, 7999, 8192), (12, 1, 10240), (1, 8000, 10240),
            (19, 11999, 10240), (20, 1, 12288), (1, 12000, 12288), (100, 50000, 12288),
        ):
            with self.subTest(requirement_count=requirement_count, resume_chars=resume_chars):
                self.assertEqual(analyzer.report_max_tokens(requirement_count, resume_chars), expected)

        gateway.configuration.return_value = {'model': 'snapshot'}
        gateway.complete.side_effect = [{'requirements': []}] + [ScreeningAgentTests()._report()] * 4
        content = '\n'.join(f'第 {index} 项经历：使用 Python 开发服务。' for index in range(1000))
        analyzer(resume={'content': content}, job={'content': 'Python'})
        self.assertEqual(gateway.complete.call_args_list[1].kwargs['max_tokens'], 12288)

    def test_policy_is_scoped_across_parallel_calls_and_resets_after_failure(self):
        def run(mode):
            return core.normalize_hr_analysis({'job_fit_score': 80, 'ai_risk': 'high', 'ai_deduction': 30},
                policy=core.NormalizationPolicy(mode, 'off'))['ai_deduction']
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(run, ('readonly', 'enforce')))[0], 0)
        self.assertEqual(core._policy.get(), core.NormalizationPolicy())

    def test_sql_review_reuses_the_same_call_budget_without_a_second_review(self):
        from test_screening_agent import ScreeningAgentTests
        fixture = ScreeningAgentTests()
        revised = fixture._report()
        gateway = Mock()
        gateway.configuration.return_value = {'model': 'snapshot'}
        gateway.complete.side_effect = [
            {'requirements': [{'id': 'r1', 'text': 'Python', 'category': 'skill', 'hard': True}]},
            fixture._suspicious_report(),
            {'issues': [{'rule': 1, 'problem': 'strengths contains Spark without evidence',
                          'suggested_fix': 'remove strengths claim'}], 'should_revise': True},
            revised,
        ]
        result = SQLScreeningAnalyzer(gateway)(resume={'content': '张三，本科，复旦大学，熟悉 Python'},
                                               job={'content': 'Python 开发'})
        calls = gateway.complete.call_args_list
        self.assertEqual([call.kwargs['screening_review'] for call in calls],
                         [False, False, True, False])
        self.assertEqual([call.kwargs['screening_call'][1] for call in calls], [1, 2, 3, 4])
        self.assertTrue(result['agent_validation']['revised'])
        self.assertTrue(result['agent_validation']['passed'])
        self.assertTrue(result['agent_validation']['recheck_skipped'])

    def test_sql_modules_import_without_loading_legacy_configuration(self):
        result = subprocess.run([sys.executable, '-c',
            "import sys; import db_screening; assert not {'app','config','llm','mailer','system_config'} & sys.modules.keys()"],
            cwd=Path(__file__).parent, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ScreeningSQLTests(unittest.TestCase):
    def setUp(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.setUp(self)

    def finish(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.finish(self)

    def upload(self):
        from test_db_application import ApplicationLiveTests
        return ApplicationLiveTests.upload(self)

    def test_real_agent_report_commits_through_sql_workflow_once(self):
        from db_application import create_application
        from test_screening_agent import ScreeningAgentTests
        from uuid import uuid4
        events, gateways = [], []
        def business(gateway):
            gateways.append(gateway)
            return self.callbacks
        self.application.close()
        self.application = create_application(self.config, screening_adapter_factory(business,
            policy=core.NormalizationPolicy(DECEPTION_CHECK_MODE='off'), on_event=events.append), self.objects)
        self.client = self.application.app.test_client()
        gateway = gateways[0]
        responses = [{'requirements': []}] + [ScreeningAgentTests()._report()] * 5
        with patch.object(gateway, 'model_snapshot', return_value=({'model': 'synthetic'}, 'a' * 64)), \
             patch.object(gateway, 'complete', side_effect=responses) as model:
            resume, job = self.upload()
            body = {'resume_ids': [resume], 'job_id': job}
            headers = {**self.headers, 'Idempotency-Key': str(uuid4())}
            first = self.client.post('/api/v1/resumes/hr-analysis', json=body, headers=headers)
            self.assertEqual(first.status_code, 200, first.get_json())
            data = first.get_json()['data']
            self.assertEqual(data['status'], 'completed')
            report = data['analyses'][0]['hr_analysis']
            self.assertIn('scoring_meta', report)
            self.assertIn('agent_validation', report)
            calls = model.call_count
            again = self.client.post('/api/v1/resumes/hr-analysis', json=body, headers=headers)
            self.assertEqual(again.get_json()['data'], data)
            self.assertEqual(model.call_count, calls)
            self.assertIn('generating', [event['status'] for event in events])

    def test_new_request_reanalyzes_incomplete_report_then_reuses_complete_report(self):
        from db_application import create_application
        from uuid import uuid4
        gateways = []

        def business(gateway):
            gateways.append(gateway)
            return self.callbacks

        self.application.close()
        self.application = create_application(self.config, screening_adapter_factory(business), self.objects)
        self.client = self.application.app.test_client()
        gateway = gateways[0]
        with patch.object(gateway, 'model_snapshot', return_value=({'model': 'synthetic'}, 'a' * 64)), \
                patch.object(SQLScreeningAnalyzer, 'analyze_with_events', side_effect=[
                    {'final_score': 0, 'analysis_incomplete': True},
                    {'final_score': 82},
                ]) as analyze:
            resume, job = self.upload()
            body = {'resume_ids': [resume], 'job_id': job}

            def request_report():
                response = self.client.post('/api/v1/resumes/hr-analysis', json=body,
                                            headers={**self.headers, 'Idempotency-Key': str(uuid4())})
                self.assertEqual(response.status_code, 200, response.get_json())
                result = response.json['data']
                self.assertEqual(result['status'], 'completed')
                return result['analyses'][0]['hr_analysis']

            self.assertTrue(request_report()['analysis_incomplete'])
            self.assertEqual(request_report()['final_score'], 82)
            self.assertEqual(request_report()['final_score'], 82)
            self.assertEqual(analyze.call_count, 2)
