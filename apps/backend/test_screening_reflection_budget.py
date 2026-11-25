# -*- coding: utf-8 -*-
"""SQL review has room for low-effort reasoning and a complete response."""
import unittest
from unittest.mock import Mock

import screening_agent
from db_screening import SQLResumeImprover, SQLScreeningAnalyzer
from db_provider_gateway import ProviderUnavailable


class ReflectionBudgetTests(unittest.TestCase):
    def test_improvement_retries_length_once_and_requests_complete_markdown(self):
        gateway = Mock()
        gateway.configuration.return_value = {}
        gateway.complete.side_effect = [ProviderUnavailable('finish_length'), '# 优化稿']
        emit = Mock()
        improver = SQLResumeImprover(gateway)
        self.assertEqual(improver.analyze_with_events(
            resume={'content': '合成简历'}, job={'content': '合成岗位'}, on_event=emit,
        ), '# 优化稿')
        first, retry = gateway.complete.call_args_list
        self.assertEqual(first.kwargs['improvement_call'], 1)
        self.assertEqual(retry.kwargs['improvement_call'], 2)
        self.assertIn('输出全部 5 步', retry.args[0][0]['content'])
        self.assertEqual([entry.args[0]['status'] for entry in emit.call_args_list], ['generating', 'retrying'])

    def test_malformed_report_is_retried_once_within_call_budget(self):
        call = Mock(side_effect=[ProviderUnavailable('invalid_format'), {'ok': True}])
        event = Mock()
        budget = {'calls': screening_agent.MAX_AGENT_LLM_CALLS - 2, 'call_llm': call}
        result, retries = screening_agent._generate_report(
            'prompt', max_tokens=8192, runtime_config=None, budget=budget, on_event=event,
        )
        self.assertEqual((result, retries), ({'ok': True}, 1))
        self.assertEqual(budget['calls'], screening_agent.MAX_AGENT_LLM_CALLS)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(event.call_args.args[0]['status'], 'retrying')

    def test_malformed_report_does_not_retry_past_budget(self):
        call = Mock(side_effect=ProviderUnavailable('invalid_format'))
        budget = {'calls': screening_agent.MAX_AGENT_LLM_CALLS - 1, 'call_llm': call}
        with self.assertRaises(ProviderUnavailable):
            screening_agent._generate_report(
                'prompt', max_tokens=8192, runtime_config=None, budget=budget, on_event=None,
            )
        self.assertEqual(call.call_count, 1)

    def test_budget_key_is_only_applied_at_reflection_call_sites(self):
        calls = []

        def stub(prompt, **kwargs):
            calls.append(kwargs.get('max_tokens'))
            return {'ok': True}

        budget = {'calls': 0, 'reflection_max_tokens': 4096, 'call_llm': stub}
        screening_agent._call_json('prompt', max_tokens=6500, runtime_config=None, budget=budget)
        self.assertEqual(calls, [6500])

    def test_default_legacy_budget_is_1500(self):
        calls = []

        def stub(prompt, **kwargs):
            calls.append(kwargs.get('max_tokens'))
            return {'ok': True}

        budget = {'calls': 0, 'call_llm': stub}
        screening_agent._call_json('prompt', max_tokens=1500, runtime_config=None, budget=budget)
        self.assertEqual(calls, [1500])

    def test_sql_policy_records_reflection_budget(self):
        analyzer = SQLScreeningAnalyzer(Mock())
        material = analyzer.policy_material()
        self.assertEqual(material['reflection_max_tokens'], 8192)
        self.assertEqual(analyzer.reflection_max_tokens, 8192)


if __name__ == '__main__':
    unittest.main()
