"""Existing screening algorithm bound to SQL provider and normalization policy."""
from copy import copy, deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
from functools import partial
import hashlib
import json
from uuid import uuid4

from hr_normalization import NormalizationPolicy, normalize_hr_analysis
from db_provider_gateway import ProviderUnavailable, SCREENING_THINKING_POLICY
from resume_sanitize import sanitize_resume_content
from screening_agent import run_screening_agent

# 五段式优化报告的实测长度接近 4096 token 上限；被截断时只允许**一次**有界重试。
# 这不是"放宽完整性检查"：网关对非 stop 的响应仍然拒绝，绝不会接受截断内容。
IMPROVEMENT_MAX_TOKENS = 4096
IMPROVEMENT_RETRY_MAX_TOKENS = 8192
REQUIREMENTS_EXTRACTION_VERSION = 'requirements-v1'

# 同一 JD 的候选人共享要求抽取结果；键含模型配置，跨进程生命周期内有效。
_JOB_REQUIREMENTS_CACHE: dict[str, dict] = {}


class SQLScreeningAnalyzer:
    def __init__(self, gateway, *, policy=NormalizationPolicy(), on_event=None, search_provider=None):
        if not isinstance(policy, NormalizationPolicy) or (on_event is not None and not callable(on_event)):
            raise ValueError('Explicit scoring policy and optional event callback required')
        self.gateway, self.policy, self.on_event = gateway, policy, on_event
        self.search_provider = search_provider
        self.web_search = False
        self._snapshot = None
        self._requirements_namespace = ''

    def for_task(self, *, web_search=False):
        if type(web_search) is not bool or (web_search and not callable(self.search_provider)):
            raise ValueError('Explicit search provider required')
        bound = copy(self)
        bound.web_search = web_search
        return bound

    reflection_max_tokens = 8192
    report_default_max_tokens = 8192
    # Descending output budgets. Either workload measure can select a higher tier.
    report_budget_tiers = ((20, 12000, 12288), (12, 8000, 10240))

    def report_max_tokens(self, requirement_count, resume_chars):
        for min_requirements, min_chars, max_tokens in self.report_budget_tiers:
            if requirement_count >= min_requirements or resume_chars >= min_chars:
                return max_tokens
        return self.report_default_max_tokens

    def policy_material(self):
        return {'policy': asdict(self.policy), 'web_search': self.web_search,
                'reflection_max_tokens': self.reflection_max_tokens,
                'report_default_max_tokens': self.report_default_max_tokens,
                'report_budget_tiers': self.report_budget_tiers,
                'requirements_extraction_version': REQUIREMENTS_EXTRACTION_VERSION,
                'thinking_policy': SCREENING_THINKING_POLICY}

    def prepare_task(self):
        cfg, revision = self.gateway.model_snapshot()
        bound = copy(self)
        bound._snapshot = deepcopy(cfg)
        binding = {'binding_id': str(uuid4()), 'config_revision': revision,
                   'policy_revision': hashlib.sha256(json.dumps(self.policy_material(), sort_keys=True).encode()).hexdigest()}
        bound._requirements_namespace = revision + ':' + binding['policy_revision']
        return bound, binding

    def __call__(self, *, resume, job):
        return self.analyze_with_events(resume=resume, job=job, on_event=self.on_event)

    def analyze_with_events(self, *, resume, job, on_event):
        # Workflow binds once per batch; direct standalone calls bind per call.
        cfg = deepcopy(self._snapshot) if self._snapshot is not None else self.gateway.configuration('llm')
        stage = 'planning'
        ordinal = 0

        def emit(event):
            nonlocal stage
            stage = event['status']
            if on_event is not None:
                on_event(event)
            if self.on_event is not None and self.on_event is not on_event:
                self.on_event(event)
        def call(prompt, *, expect_json, max_tokens, runtime_config, review=False):
            nonlocal ordinal
            ordinal += 1
            return self.gateway.complete([{'role': 'user', 'content': prompt}],
                runtime_config=deepcopy(cfg), expect_json=expect_json, max_tokens=max_tokens,
                screening_call=(stage, ordinal), screening_review=review)
        raw = run_screening_agent(job_content=job['content'],
            resume_content=sanitize_resume_content(resume['content']),
            current_date=datetime.now(timezone.utc).strftime('%Y-%m'),
            runtime_config={'web_search': self.web_search}, agent_config={'web_search': self.web_search},
            on_event=emit, call_llm=call, search_provider=self.search_provider if self.web_search else None,
            requirements_cache=_JOB_REQUIREMENTS_CACHE if self._requirements_namespace else None,
            requirements_cache_namespace=self._requirements_namespace,
            reflection_max_tokens=self.reflection_max_tokens, report_max_tokens=self.report_max_tokens,
            review_llm=partial(call, review=True))
        return normalize_hr_analysis(deepcopy(raw), job['content'], resume['content'], policy=self.policy)


class SQLResumeImprover(SQLScreeningAnalyzer):
    """Original five-part Markdown prompt, owned by a durable improvement task."""

    max_tokens = IMPROVEMENT_MAX_TOKENS
    retry_max_tokens = IMPROVEMENT_RETRY_MAX_TOKENS
    retry_on_length = True

    def policy_material(self):
        from prompts import PROMPT_HR_JUDGE
        return {'kind': 'improvement',
                'prompt_sha256': hashlib.sha256(PROMPT_HR_JUDGE.encode()).hexdigest(),
                'thinking_policy': 'official-deepseek-flash-improvement-no-thinking',
                'max_tokens': self.max_tokens,
                'retry_on_length': self.retry_on_length,
                'retry_max_tokens': self.retry_max_tokens}

    def analyze_with_events(self, *, resume, job, on_event):
        from prompts import PROMPT_HR_JUDGE
        from prompt_safety import prepare_untrusted
        cfg = deepcopy(self._snapshot) if self._snapshot is not None else self.gateway.configuration('llm')
        if on_event is not None:
            on_event({'status': 'generating'})
        # 岗位描述同样是攻击者可影响的文本（用户可直接粘贴任意内容），必须和简历一样
        # 走不可信数据围栏，否则注入文本会落进指令区。
        job_data = prepare_untrusted(job['content'], 'job', limit=60000)
        resume_data = prepare_untrusted(sanitize_resume_content(resume['content']), 'resume', limit=60000)
        prompt = PROMPT_HR_JUDGE.format(Job_Description=job_data['fenced'],
            raw_resume=resume_data['fenced'],
            datetime=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        messages = [{'role': 'user', 'content': prompt}]
        try:
            return self.gateway.complete(messages, runtime_config=cfg, expect_json=False,
                                         max_tokens=self.max_tokens, improvement_call=1)
        except ProviderUnavailable as error:
            # 只有"输出被长度截断"允许一次有界重试；其它原因（HTTP、超时、
            # 空内容、格式不符）直接失败，不做盲目重试。
            if not self.retry_on_length or getattr(error, 'kind', None) != 'finish_length':
                raise
        if on_event is not None:
            on_event({'status': 'retrying'})
        retry_prompt = prompt + '\n\n上次输出被截断。请精简诊断，优先保留第 4 步的完整改写，输出全部 5 步。'
        return self.gateway.complete([{'role': 'user', 'content': retry_prompt}], runtime_config=cfg,
                                     expect_json=False, max_tokens=self.retry_max_tokens, improvement_call=2)


def screening_adapter_factory(business_builder, *, policy=NormalizationPolicy(), on_event=None):
    """Retain local parsing/config rules, replace model analysis with the real Agent."""
    from dataclasses import replace
    from db_provider_gateway import provider_adapter_factory
    def build(gateway):
        return replace(business_builder(gateway), analyze=SQLScreeningAnalyzer(gateway, policy=policy, on_event=on_event))
    return provider_adapter_factory(build)
