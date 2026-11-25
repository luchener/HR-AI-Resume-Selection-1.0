"""Candidate-only SQL entrypoint: real application, selectable model adapter.

Used exclusively by the isolated acceptance environment. It reuses the reviewed
deployment loader and the real application composition; the only switch is
whether the screening/improvement adapters call a model or return a synthetic
result. `CANDIDATE_FAKE_MODEL=0` (default) is the production path unchanged.

The file is mounted read-only into the candidate container; it is not baked into
any image and is never used by the production service.
"""
import os
import time
from dataclasses import replace

from db_application import create_application
from db_deployment import business_adapters, load_configuration
from db_provider_gateway import provider_adapter_factory

FAKE_REPORT = {
    'candidate_name': '合成候选人',
    'final_score': 82,
    'fit_tag': '较匹配',
    'fit_grade': 'B',
    'summary': '合成项目经验，用于隔离验收。',
    'recruitment_recommendation': '优先面试',
    'score_breakdown': {'skills_projects': 20},
    'agent_validation': {'passed': True},
    'evidence_assessments': [],
}
FAKE_MARKDOWN = '# 合成优化简历\n\n## 经历\n- 合成内容（假模型）\n'
# 仅在候选验收中使用：可控耗时用于制造"运行中断线"，以及记录真实执行次数。
FAKE_DELAY = float(os.environ.get('CANDIDATE_FAKE_DELAY', '0') or 0)
FAKE_COUNTER = '/tmp/candidate_fake_runs'


def _record_run(kind):
    try:
        with open(FAKE_COUNTER, 'a', encoding='utf-8') as handle:
            handle.write(kind + '\n')
    except OSError:
        pass


def fake_analyze(*, resume, job):
    if FAKE_DELAY:
        time.sleep(FAKE_DELAY)
    _record_run('screening')
    return dict(FAKE_REPORT)


def fake_improve(*, resume, job):
    if FAKE_DELAY:
        time.sleep(FAKE_DELAY)
    _record_run('improvement')
    return FAKE_MARKDOWN


def build():
    settings, originals, environment, search = load_configuration(os.environ['SQL_DEPLOYMENT_FILE'])
    fake = os.environ.get('CANDIDATE_FAKE_MODEL', '0') == '1'

    def builder(gateway):
        adapters = business_adapters(gateway, search_config=search)
        if not fake:
            return adapters
        return replace(adapters, analyze=fake_analyze, improver=fake_improve)

    return create_application(settings, provider_adapter_factory(builder), originals,
                              environment=environment)


application = build().app
