"""Pure evidence guards: missing evidence never becomes a factual rejection."""
import unittest
import evidence_judgment


class EvidenceJudgmentTests(unittest.TestCase):
    def item(self, text='熟练掌握 Python', category='skill', hard=True):
        return {'id':'r1','text':text,'category':category,'hard':hard,'logic':'required'}

    def judge(self, resume, status, quotes, req=None):
        return evidence_judgment.judge([req or self.item()], [{'id':'r1','status':status,'source_quotes':quotes}], resume, [])[0]

    def test_fabricated_quote_cannot_prove_requirement(self):
        result = self.judge('项目使用 Flask 开发接口。', 'met', ['精通 Python'])
        self.assertEqual(result['status'], 'manual_review')
        self.assertTrue(result['evidence'])
        for e in result['evidence']:
            self.assertEqual('项目使用 Flask 开发接口。'[e['start']:e['end']], e['quote'])

    def test_legacy_single_quote_is_verified_and_retained(self):
        resume='使用 Redis 实现缓存。'
        req=self.item('Redis',category='skill')
        result=evidence_judgment.judge([req],[{'id':'r1','status':'met','source_quote':'使用 Redis 实现缓存。'}],resume,[])[0]
        self.assertTrue(result['evidence'])
        self.assertIn(result['evidence'][0]['quote'],resume)

    def test_year_gate_ignores_project_duration_and_calendar_dates(self):
        req=self.item('5年以上软件开发经验',category='experience_years')
        for quote in ('项目周期2年。','任职时间2019-2024年。'):
            r=evidence_judgment.judge([req],[{'id':'r1','status':'not_met','source_quotes':[quote]}],quote,[{'id':'r1','status':'not_met','resume_basis':quote}])[0]
            self.assertEqual(r['status'],'manual_review')
            self.assertFalse(r['explicit_negative'])

    def test_absence_does_not_prove_noncompliance(self):
        self.assertEqual(self.judge('负责客户支持。','not_met',[])['status'],'not_mentioned')

    def test_explicit_negative_is_separate_from_absence(self):
        result = self.judge('不会 Python。','not_met',['不会 Python。'])
        self.assertEqual(result['status'],'not_met')
        self.assertTrue(result['explicit_negative'])

    def test_one_denied_alternative_does_not_reject_whole_or(self):
        result=self.judge('不会 Python。','not_met',['不会 Python。'],self.item('Python 或 Java'))
        self.assertEqual(result['status'],'manual_review')

    def test_related_major_is_text_match_not_unknown_authenticity(self):
        req=self.item('计算机、软件工程、人工智能等相关专业',category='education')
        r=evidence_judgment.judge([req],[{'id':'r1','status':'met','source_quotes':['专业：计算机科学与技术']}],'专业：计算机科学与技术',[{'id':'r1','status':'manual_review','resume_basis':'真实性待核验'}])[0]
        self.assertEqual(r['status'],'met')

    def test_whole_practice_alternative_not_all_technologies_required(self):
        req=self.item('有RAG应用、问答系统、SQL自动生成或数据分析助手开发经验')
        for resume in ['开发知识问答系统。','实现NL2SQL自动查询。','构建数据分析助手。','开发检索增强生成应用。']:
            with self.subTest(resume=resume):
                r=self.judge(resume,'not_mentioned',[],req)
                self.assertEqual(r['status'],'met')
                self.assertEqual(len(r['required_components']),1)
                self.assertEqual(len(r['supported_components']),1)
        r=self.judge('维护SQL数据库。开发Agent工作流。','met',[],req)
        self.assertEqual(r['status'],'not_mentioned')
        self.assertFalse(r['supported_components'])

    def test_same_paragraph_unrelated_action_does_not_certify_task(self):
        resume='使用Agent生成报告。维护网络设备。'
        r=self.judge(resume,'met',[],self.item('维护Agent系统',category='responsibility'))
        self.assertNotIn('action-2',{c['id'] for c in r['supported_components']})

    def test_full_resume_project_survives_model_quote_omission(self):
        resume='项目经验\n独立设计并开发Agent系统。\n实现Agent工具调用和维护。'
        r=self.judge(resume,'not_mentioned',[],self.item('设计开发维护Agent系统',category='responsibility'))
        self.assertTrue(r['supported_components'])
        self.assertEqual(r['status'],'manual_review')
        for c in r['supported_components']:
            self.assertIn(c['source_quote'],resume)

    def test_custom_agent_does_not_prove_named_framework(self):
        r=self.judge('自研Agent工作流实现工具调用。','met',['自研Agent工作流实现工具调用。'],self.item('熟悉AI智能体框架：Dify / LangChain / LlamaIndex / AutoGen任意一种'))
        self.assertEqual(r['status'],'not_mentioned')
        self.assertFalse(r['supported_components'])

    def test_multistage_does_not_prove_multi_agent(self):
        r=self.judge('Agent工作流实现多阶段任务编排。','met',['Agent工作流实现多阶段任务编排。'],self.item('有Agent多工具调用、多智能体协作经验'))
        self.assertEqual(r['status'],'manual_review')
        self.assertIn('multi_agent',{c['id'] for c in r['required_components']})
        self.assertNotIn('multi_agent',{c['id'] for c in r['supported_components']})

    def test_related_practice_receives_component_without_proficiency(self):
        r=self.judge('使用Spring开发接口。','met',['使用Spring开发接口。'],self.item('熟练Java'))
        self.assertEqual(r['status'],'manual_review')
        self.assertTrue(r['supported_components'])
        self.assertIn('proficiency',{c['id'] for c in r['required_components']})
        self.assertNotIn('proficiency',{c['id'] for c in r['supported_components']})

    def test_related_framework_is_not_proficiency(self):
        for requirement, resume in [('熟练掌握 Python','使用 Flask 开发服务。'),('精通 Java','使用 Spring Boot 开发接口。')]:
            with self.subTest(requirement=requirement):
                result=self.judge(resume,'met',[resume],self.item(requirement))
                self.assertEqual(result['status'],'manual_review')
                self.assertEqual(result['evidence_kind'],'related')

    def test_direct_basic_mention_and_preferred_items_are_retained(self):
        req=self.item('有 Redis 实践经验',hard=False)
        req['logic']='preferred'
        result=self.judge('使用 Redis 实现缓存。','met',['使用 Redis 实现缓存。'],req)
        self.assertEqual(result['status'],'met')
        self.assertFalse(result['hard'])

    def test_compound_requirement_needs_all_components(self):
        result=self.judge('使用 Docker 部署服务。','met',['使用 Docker 部署服务。'],self.item('Docker 和 Kubernetes 实践经验'))
        self.assertEqual(result['status'],'manual_review')

    def test_non_technical_negative_needs_reverse_evidence(self):
        result=self.judge('参与客户沟通。','not_met',['参与客户沟通。'],self.item('良好沟通能力',category='other'))
        self.assertEqual(result['status'],'manual_review')

    def test_desired_skill_is_not_practice_and_cannot_fill_and(self):
        for text,resume in [('Python','求职意向：Python工程师。'),('Python和Java','求职意向：Python工程师。\n使用Java开发。')]:
            r=self.judge(resume,'met',[resume],self.item(text))
            self.assertEqual(r['status'],'manual_review')

    def test_proficiency_cannot_be_borrowed_across_technologies(self):
        r=self.judge('熟练Python，了解Java。','met',['熟练Python，了解Java。'],self.item('熟练Python和Java'))
        self.assertEqual(r['status'],'manual_review')

    def test_desired_role_does_not_prove_delivered_work(self):
        result=self.judge('求职岗位：Agent工程师。','met',['求职岗位：Agent工程师。'],self.item('参与Agent系统设计开发维护',category='responsibility'))
        self.assertEqual(result['status'],'manual_review')

    def test_duty_requires_actions_not_just_same_technology(self):
        result=self.judge('负责AI服务运行监控。','met',['负责AI服务运行监控。'],self.item('参与AI应用评测体系建设',category='responsibility'))
        self.assertEqual(result['status'],'manual_review')

    def test_unrelated_quote_cannot_make_requirement_met(self):
        result=self.judge('负责维护服务器。','met',['负责维护服务器。'],self.item('具备数据分析能力',category='other'))
        self.assertEqual(result['status'],'manual_review')
