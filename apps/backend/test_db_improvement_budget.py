# -*- coding: utf-8 -*-
"""有界修复：SQL 深度优化的输出预算与"仅截断才重试一次"策略。

诊断（脱敏）：HTTP 200、finish_reason=length、completion_tokens=4095、content 为空。
因此失败不是重试逻辑问题，而是输出预算被截断；修复只针对这一类做一次有界重试，
其它原因一律不重试，且绝不接受截断内容。
"""
import unittest
from unittest.mock import Mock

from db_provider_gateway import ProviderUnavailable
from db_screening import (IMPROVEMENT_MAX_TOKENS, IMPROVEMENT_RETRY_MAX_TOKENS,
                          SQLResumeImprover)


def build_gateway(responses):
    """responses: list of exceptions or markdown strings, consumed per call."""
    calls = []
    gateway = Mock()
    gateway.configuration.return_value = {'model': 'synthetic-model'}

    def complete(messages, **kwargs):
        calls.append(kwargs.get('max_tokens'))
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    gateway.complete.side_effect = complete
    return gateway, calls


class ImprovementBudgetTests(unittest.TestCase):
    def run_improver(self, responses):
        gateway, calls = build_gateway(responses)
        improver = SQLResumeImprover(gateway)
        result = improver.analyze_with_events(resume={'content': '合成简历'},
                                              job={'content': '合成岗位'}, on_event=None)
        return result, calls, improver

    def test_success_uses_single_call_with_base_budget(self):
        result, calls, improver = self.run_improver(['# 合成报告\n内容'])
        self.assertEqual(result, '# 合成报告\n内容')
        self.assertEqual(calls, [IMPROVEMENT_MAX_TOKENS])
        self.assertEqual(IMPROVEMENT_MAX_TOKENS, 4096)
        self.assertEqual(IMPROVEMENT_RETRY_MAX_TOKENS, 8192)

    def test_length_truncation_retries_once_with_larger_budget(self):
        result, calls, _ = self.run_improver([
            ProviderUnavailable('finish_length'), '# 合成报告\n内容'])
        self.assertEqual(result, '# 合成报告\n内容')
        self.assertEqual(calls, [IMPROVEMENT_MAX_TOKENS, IMPROVEMENT_RETRY_MAX_TOKENS])

    def test_second_truncation_fails_without_third_attempt(self):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.run_improver([ProviderUnavailable('finish_length'),
                               ProviderUnavailable('finish_length')])
        self.assertEqual(caught.exception.kind, 'finish_length')

    def test_non_retryable_causes_are_not_retried(self):
        for kind in ('http_status', 'timeout', 'transport', 'empty_content',
                     'invalid_format', 'finish_other', 'unknown', 'unexpected'):
            gateway, calls = build_gateway([ProviderUnavailable(kind)])
            improver = SQLResumeImprover(gateway)
            with self.assertRaises(ProviderUnavailable, msg=kind) as caught:
                improver.analyze_with_events(resume={'content': 'r'}, job={'content': 'j'},
                                             on_event=None)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(calls, [IMPROVEMENT_MAX_TOKENS], kind)

    def test_policy_material_records_budget_and_retry(self):
        _, _, improver = self.run_improver(['# 合成报告'])
        policy = improver.policy_material()
        self.assertEqual(policy['kind'], 'improvement')
        self.assertEqual(policy['max_tokens'], IMPROVEMENT_MAX_TOKENS)
        self.assertIs(policy['retry_on_length'], True)
        self.assertEqual(policy['retry_max_tokens'], IMPROVEMENT_RETRY_MAX_TOKENS)
        self.assertEqual(len(policy['prompt_sha256']), 64)

    def test_retry_can_be_disabled_explicitly(self):
        gateway, calls = build_gateway([ProviderUnavailable('finish_length')])
        improver = SQLResumeImprover(gateway)
        improver.retry_on_length = False
        with self.assertRaises(ProviderUnavailable):
            improver.analyze_with_events(resume={'content': 'r'}, job={'content': 'j'}, on_event=None)
        self.assertEqual(calls, [IMPROVEMENT_MAX_TOKENS])


class ProviderUnavailableKindTests(unittest.TestCase):
    def test_default_is_unknown(self):
        self.assertEqual(ProviderUnavailable().kind, 'unknown')

    def test_known_kinds_round_trip(self):
        for kind in sorted(ProviderUnavailable.KINDS):
            self.assertEqual(ProviderUnavailable(kind).kind, kind)

    def test_unknown_kind_normalises(self):
        self.assertEqual(ProviderUnavailable('whatever').kind, 'unknown')

    def test_message_has_no_model_text(self):
        self.assertNotIn('content', str(ProviderUnavailable('finish_length')).lower())


if __name__ == '__main__':
    unittest.main()
