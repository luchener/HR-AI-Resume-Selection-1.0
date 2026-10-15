"""Integration: server grade inputs must ignore LLM numeric/prose judgments."""
import copy
import unittest
from unittest.mock import patch
import app


class EvidencePipelineTests(unittest.TestCase):
    def raw(self):
        return {'evidence_pipeline_version':'evidence-v1','evidence_assessments':[
            {'id':'r1','text':'Redis实践','category':'skill','hard':True,'logic':'required','status':'met','source_quote':'使用Redis实现缓存','evidence_kind':'direct','reason':'原文明确'}],
            'requirements_checklist':[], 'score_breakdown':{'hard_requirements':25,'responsibility_overlap':25,'skills_projects':25,'industry_background':15,'evidence_bonus':10},'job_fit_score':100,
            'strengths':['模型臆断精通所有技术'],'weaknesses':['模型臆断不符合'],
            'agent_meta':{'validation_passed':True},'agent_validation':{'passed':True,'issues':[]}}

    def normalize(self, raw):
        with patch.object(app.config,'DECEPTION_CHECK_MODE','off'):
            return app._normalize_hr_analysis(raw,'要求Redis实践','使用Redis实现缓存')

    def test_llm_numbers_and_prose_do_not_change_evidence_score(self):
        raw=self.raw();a=self.normalize(raw)
        changed=copy.deepcopy(raw);changed['job_fit_score']=0;changed['score_breakdown']={k:0 for k in raw['score_breakdown']}
        b=self.normalize(changed)
        self.assertEqual(a['final_score'],b['final_score'])
        self.assertEqual(a['score_breakdown'],b['score_breakdown'])
        self.assertEqual(a['scoring_meta']['score_source'],'server_deterministic_evidence')
        self.assertNotIn('模型臆断',str(a['strengths'])+str(a['weaknesses']))

    def test_new_semantic_comparison_does_not_call_model(self):
        analysis=self.normalize(self.raw())
        with patch.object(app.llm,'call_llm',side_effect=AssertionError('comparison model must not invent ability ranking')):
            result=app._compare_candidates([{'candidate_name':'甲','hr_analysis':analysis},{'candidate_name':'乙','hr_analysis':analysis}])
        self.assertEqual(len(result['ranking']),2)
        self.assertEqual(result['pairwise'],[])
        self.assertIn('不代表能力',result['recommendation'])

    def test_unknown_does_not_automatically_reject(self):
        raw=self.raw();raw['evidence_assessments'][0].update(status='not_mentioned',source_quote='')
        r=self.normalize(raw)
        self.assertEqual(r['hard_gate_deduction'],0)
        self.assertEqual(r['recruitment_recommendation'],'待补充材料')
        self.assertNotEqual(r['fit_grade'],'D级（不适配）')

    def test_model_only_legacy_checklist_cannot_create_gate_after_audit(self):
        raw=self.raw()
        raw['evidence_assessments'][0].update(status='not_mentioned',source_quote='')
        raw['requirements_checklist']=[{'id':'r1','status':'not_met','logic':'required','hard':True,'explicit_negative':True,'gate_tier':'A','category':'education','text':'硕士学历','source_quote':'本科'}]
        r=self.normalize(raw)
        self.assertEqual(r['hard_gate_deduction'],0)
        self.assertIsNone(r['gate_cap_applied'])
        self.assertEqual(r['requirements_checklist'][0]['effective_status'],'not_mentioned')

    def test_normalized_score_breakdown_matches_score_in_single_dimension(self):
        r=self.normalize(self.raw())
        self.assertEqual(sum(r['score_breakdown'].values()),r['job_fit_score'])
        self.assertEqual(r['capability_score'],r['final_score'])
        self.assertEqual(r['capability_basis'],'resume_jd_evidence_match')

    def test_preference_negative_cannot_trigger_hard_gate(self):
        raw=self.raw();raw['requirements_checklist']=[{'status':'not_met','logic':'preferred','hard':False,'explicit_negative':True,'text':'Redis优先','category':'skill'}]
        r=self.normalize(raw)
        self.assertEqual(r['hard_gate_deduction'],0)
        self.assertIsNone(r['gate_cap_applied'])
