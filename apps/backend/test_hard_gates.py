# -*- coding: utf-8 -*-
"""硬门槛（学历层级 / 院校层次 / 年限 / 证书）的判定与防误杀边界。

背景 bug 两个：
1. JD 写“本科 985/211”时，985/211 被整条静默丢掉——候选人无论什么院校都得到
   manual_review，note 反而写着“满足岗位要求”。
2. 年限/证书门槛不看 `优先` 与 `logic=preferred`，JD 写“5年以上经验优先”“可放宽至3年”
   也会被当成硬拦截。

设计原则（与 deception_check 一致）：not_met 会扣分并封顶，
所以只有**确定性低于要求**才判 not_met，其余一律 manual_review 交人工。
"""
import unittest

import app as backend
import screening_agent


def _requirements(text, logic="required", category="education"):
    return {"requirements": [{"id": "r1", "text": text, "category": category, "hard": True, "logic": logic}]}


def _report(degree="本科", tier="985", school="某某大学"):
    """默认校名刻意用**名单里没有**的名字。

    校名一旦命中 `school_tiers.json`（如"复旦大学"），服务端名单就会覆盖模型自报的 tier——
    这是新行为，所以想测"模型自报 tier 的解析"就必须用名单外的校名。
    """
    return {
        "candidate_name": "张三",
        "education_history": [
            {"degree": degree, "school_name": school, "school_tier": tier,
             "major": "计算机", "graduation_year": "2020"}
        ],
        # 基础分来自 score_breakdown 合计（88），不是 job_fit_score
        "score_breakdown": {"hard_requirements": 22, "responsibility_overlap": 22,
                            "skills_projects": 22, "industry_background": 12, "evidence_bonus": 10},
    }


def _gate(text, report, logic="required"):
    checklist = screening_agent._build_requirements_checklist(
        _requirements(text, logic), report, "张三\n简历正文"
    )
    return checklist[0]["status"], checklist[0]["resume_basis"]


class DegreeLevelTests(unittest.TestCase):
    """学历层级维度。"""

    def test_graduate_token_is_recognized(self):
        """JD 写“研究生及以上”必须被识别——原来会整条静默丢掉。"""
        status, note = _gate("研究生及以上学历", _report(degree="本科"))

        self.assertEqual(status, "not_met")
        self.assertIn("研究生", note)

    def test_bachelor_degree_token_is_recognized(self):
        status, note = _gate("学士及以上学历", _report(degree="专科", tier="专科"))

        self.assertEqual(status, "not_met")

    def test_lower_degree_is_not_met(self):
        status, note = _gate("硕士及以上学历", _report(degree="专科", tier="专科"))

        self.assertEqual(status, "not_met")
        self.assertIn("硕士", note)

    def test_preferred_higher_degree_does_not_raise_the_floor(self):
        """“本科及以上，硕士优先”的真正门槛是本科：本科候选人绝不能被判不达标。"""
        status, note = _gate("本科及以上学历，硕士优先", _report(degree="本科", tier="985"))

        self.assertEqual(status, "manual_review")

    def test_only_preferred_degree_is_not_a_floor(self):
        """“硕士优先”单独出现时不构成硬门槛。"""
        status, note = _gate("硕士优先", _report(degree="本科", tier="985"))

        self.assertEqual(status, "manual_review")

    def test_parenthetical_preferred_higher_degree_keeps_the_floor(self):
        """括号里写“博士优先”不能把“硕士及以上”这个硬门槛一起软化掉。"""
        status, note = _gate("硕士及以上学历（博士优先）", _report(degree="本科", tier="985"))

        self.assertEqual(status, "not_met")
        self.assertIn("硕士", note)


class SchoolTierTests(unittest.TestCase):
    """院校层次维度（这就是用户报的那个 bug）。"""

    def test_jd_985_211_with_ordinary_school_is_not_met(self):
        """JD“本科 985/211” + 二本 → 必须拦下来，且 note 要写明院校层次。"""
        status, note = _gate("本科及以上学历，985/211院校", _report(degree="本科", tier="二本"))

        self.assertEqual(status, "not_met")
        self.assertIn("211", note)
        self.assertIn("二本", note)

    def test_jd_985_with_ordinary_school_is_not_met(self):
        status, note = _gate("985院校毕业", _report(degree="本科", tier="二本"))

        self.assertEqual(status, "not_met")

    def test_note_no_longer_claims_satisfaction_when_tier_unknown(self):
        """原 bug 的核心：院校层次没核对，就不许说“满足要求”。"""
        status, note = _gate("本科及以上学历，985/211院校", _report(degree="本科", tier="未提供"))

        self.assertEqual(status, "manual_review")
        self.assertIn("人工核实", note)
        self.assertNotIn("院校层次未提供满足", note)

    def test_985_candidate_satisfies_211_requirement(self):
        status, note = _gate("211院校", _report(degree="本科", tier="985"))

        self.assertEqual(status, "manual_review")
        self.assertIn("满足", note)

    def test_negated_tier_is_not_read_as_a_positive_tier(self):
        """“非985/211”里的 211 也是被否定的，不能被读成 '211' 再写“满足要求”。"""
        self.assertEqual(screening_agent._tier_label("非985/211"), "below")
        status, note = _gate("211院校本科及以上", _report(degree="本科", tier="非985/211"))

        self.assertEqual(status, "not_met")
        self.assertIn("211", note)
        self.assertNotIn("满足岗位211院校要求", note)

    def test_negation_only_swallows_its_own_run(self):
        """“非985但属211”是明确的 211，不能被整句否定掉。"""
        self.assertEqual(screening_agent._tier_label("非985但属211"), "211")

    def test_bare_negation_is_below(self):
        self.assertEqual(screening_agent._tier_label("不是211"), "below")
        self.assertEqual(screening_agent._tier_label("未列入双一流"), "below")

    def test_211_candidate_satisfies_dual_first_class_requirement(self):
        """双一流 ⊇ 211，所以 211 院校满足双一流要求。"""
        status, note = _gate("双一流院校", _report(degree="本科", tier="211"))

        self.assertEqual(status, "manual_review")
        self.assertIn("满足", note)

    def test_211_candidate_not_auto_rejected_for_985(self):
        """211 院校对“要求 985”的岗位不下确定性结论（宁可人工，也不误封顶）。"""
        status, note = _gate("985院校", _report(degree="本科", tier="211"))

        self.assertEqual(status, "manual_review")

    def test_ambiguous_985_slash_211_label_is_not_rejected(self):
        """报告写“985/211”（可能只是 211）时，不得对 985 岗位判不达标。"""
        status, note = _gate("985院校", _report(degree="本科", tier="985/211"))

        self.assertEqual(status, "manual_review")

    def test_overseas_and_military_schools_are_never_rejected(self):
        """境外/军校/公安不在 985/211 名单里，但它们不代表更差（如国防科技大学）。"""
        for tier in ("海外", "境外", "军校", "公安", "中外合作办学", "独立学院"):
            with self.subTest(tier=tier):
                status, note = _gate("985院校", _report(degree="本科", tier=tier))

                self.assertEqual(status, "manual_review")
                self.assertIn("人工核实", note)

    def test_reads_tier_of_highest_degree_only(self):
        """硕士院校未提供 + 本科是二本：不能拿本科的层次替硕士下结论。"""
        report = {
            "candidate_name": "张三",
            "education_history": [
                {"degree": "硕士", "school_name": "某大学", "school_tier": "未提供",
                 "major": "计算机", "graduation_year": "2023"},
                {"degree": "本科", "school_name": "某学院", "school_tier": "二本",
                 "major": "计算机", "graduation_year": "2020"},
            ],
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25,
                                "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
        }

        status, note = _gate("硕士及以上学历，985院校", report)

        self.assertEqual(status, "manual_review")

    def test_ordinary_plain_label_is_unclear_not_rejected(self):
        """报告写“普通”含义不明，不得据此判不达标。"""
        status, note = _gate("985院校", _report(degree="本科", tier="普通"))

        self.assertEqual(status, "manual_review")


class SoftRequirementTests(unittest.TestCase):
    """软性要求（优先/加分）永远不得进入硬门槛扣分。"""

    def test_preferred_logic_is_never_not_met(self):
        status, note = _gate("985院校", _report(degree="本科", tier="二本"), logic="preferred")

        self.assertEqual(status, "manual_review")
        self.assertIn("软性", note)

    def test_alternative_logic_is_never_not_met(self):
        status, note = _gate("985院校", _report(degree="本科", tier="二本"), logic="alternative")

        self.assertEqual(status, "manual_review")

    def test_tier_preferred_wording_is_not_a_hard_gate(self):
        for text in ("985/211院校优先", "优先考虑985院校", "本科及以上学历，985/211院校优先"):
            with self.subTest(text=text):
                status, note = _gate(text, _report(degree="本科", tier="二本"))

                self.assertEqual(status, "manual_review")

    def test_distant_preference_does_not_cancel_a_hard_tier_requirement(self):
        """“须为 985 院校，有海外经历者优先”里的 985 仍是硬门槛。"""
        status, note = _gate("须为985院校，有海外经历者优先", _report(degree="本科", tier="二本"))

        self.assertEqual(status, "not_met")

    def test_preferred_degree_wording_keeps_lower_floor(self):
        """“本科及以上，硕士优先”里的“优先”不得把门槛抬成硕士。"""
        status, note = _gate("本科及以上，硕士优先", _report(degree="本科", tier="二本"))

        self.assertEqual(status, "manual_review")


class EndToEndCapTests(unittest.TestCase):
    """整条链路：checklist not_met → 服务端扣分 + 分档封顶。

    院校层次不达标封顶 69（储备观察），学历层级不达标才封顶 59（淘汰级）。
    """

    def _run(self, jd, report):
        raw = dict(report)
        checklist = screening_agent._build_requirements_checklist(_requirements(jd), raw, "张三\n简历正文")
        raw["requirements_checklist"] = checklist
        return checklist, backend._normalize_hr_analysis(raw, jd, "张三\n简历正文")

    def test_tier_mismatch_caps_at_69_not_59(self):
        """院校层次不达标（B 档）→ 扣 8、封顶 69、储备观察（不是淘汰级）。"""
        checklist, result = self._run("本科及以上学历，985/211院校", _report(degree="本科", tier="二本", school="某某学院"))

        self.assertEqual(checklist[0]["status"], "not_met")
        self.assertEqual(checklist[0]["gate_dimension"], "tier")
        self.assertEqual(checklist[0]["gate_tier"], "B")
        self.assertEqual(result["hard_gate_deduction"], 8)  # B 档：每条 8 分，上限 24
        self.assertEqual(result["final_score"], 69)      # 88 - 8 = 80 → 院校层次封顶 69
        self.assertEqual(result["fit_grade"], "C级（适配一般）")
        self.assertEqual(result["recruitment_recommendation"], "储备观察")

    def test_degree_mismatch_still_caps_at_59(self):
        """学历层级不达标（A 档）仍然封顶 59（淘汰级），但结论改成"破格复核"。"""
        checklist, result = self._run("硕士及以上学历", _report(degree="本科", tier="985"))

        self.assertEqual(checklist[0]["gate_dimension"], "degree")
        self.assertEqual(checklist[0]["gate_tier"], "A")
        self.assertEqual(result["final_score"], 59)
        self.assertEqual(result["fit_grade"], "D级（不适配）")
        # 能力分很高时不再简单丢进"淘汰"：分数照封顶，但结论是"请人工看一眼"
        self.assertEqual(result["capability_score"], 88)
        self.assertTrue(result["override_review"])
        self.assertEqual(result["recruitment_recommendation"], "破格复核")

    def test_low_capability_degree_mismatch_stays_rejected(self):
        """能力分本来就不高、学历又不达标 → 仍然是淘汰（破格只给真正契合的人）。"""
        report = _report(degree="本科", tier="985")
        report["score_breakdown"] = {"hard_requirements": 8, "responsibility_overlap": 10,
                                     "skills_projects": 10, "industry_background": 5, "evidence_bonus": 3}
        report["job_fit_score"] = 36
        _, result = self._run("硕士及以上学历", report)

        # 解耦后能力分 = 非门槛四项(10+10+5+3=28) / 75 × 100 = 37（不再是含门槛分项的 36）
        self.assertEqual(result["capability_score"], 37)
        self.assertEqual(result["capability_basis"], "ex_gate")
        self.assertFalse(result["override_review"])
        self.assertEqual(result["recruitment_recommendation"], "淘汰")

    def test_both_dimensions_failing_uses_the_stricter_cap(self):
        """同时不达标时按更严的一档（59）。"""
        report = _report(degree="专科", tier="二本", school="某某学院")
        _, result = self._run("本科及以上学历，985/211院校", report)

        self.assertEqual(result["final_score"], 59)

    def test_legacy_checklist_without_dimension_stays_strict(self):
        """没有 gate_dimension 的旧数据按学历层级处理（更严），保持向后兼容。"""
        raw = _report(degree="本科", tier="985")
        raw["requirements_checklist"] = [
            {"id": "r1", "text": "本科及以上学历", "category": "education",
             "logic": "required", "status": "not_met", "resume_basis": "旧数据"},
        ]

        result = backend._normalize_hr_analysis(raw, "本科及以上学历", "张三\n本科")

        self.assertEqual(result["final_score"], 59)

    def test_unknown_tier_candidate_is_not_penalized(self):
        checklist, result = self._run("本科及以上学历，985/211院校", _report(degree="本科", tier="未提供"))

        self.assertEqual(checklist[0]["status"], "manual_review")
        self.assertEqual(result["hard_gate_deduction"], 0)
        self.assertEqual(result["final_score"], 88)


class ExperienceGateTests(unittest.TestCase):
    """年限门槛：不得把“优先/可放宽”读成硬门槛（候选 3 年经验）。"""

    def _gate(self, text, years="3年", logic="required"):
        report = _report()
        report["work_history"] = {"total_years": years, "relevant_years": years}
        checklist = screening_agent._build_requirements_checklist(
            _requirements(text, logic, category="experience"), report, "张三\n简历正文"
        )
        return checklist[0]["status"], checklist[0]["resume_basis"]

    def test_below_requirement_is_not_met(self):
        status, note = self._gate("5年以上软件开发经验")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)

    def test_preferred_wording_is_not_a_hard_gate(self):
        status, note = self._gate("5年以上软件开发经验优先")

        self.assertEqual(status, "manual_review")
        self.assertIn("软性", note)   # 不判定要给出理由，不能空白

    def test_wording_preference_does_not_soften_a_neighbouring_clause(self):
        """“5年以上经验，熟悉Linux者优先”里的“优先”属于另一个分句。"""
        status, note = self._gate("5年以上经验，熟悉Linux者优先")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)

    def test_preferred_logic_is_not_a_hard_gate(self):
        status, note = self._gate("5年以上软件开发经验", logic="preferred")

        self.assertEqual(status, "manual_review")
        self.assertIn("软性", note)

    def test_relaxed_clause_lowers_the_floor(self):
        """JD 明写“可放宽至3年”时，3 年候选人不能被判不达标。"""
        status, note = self._gate("5年以上经验，优秀者可放宽至3年")

        self.assertNotEqual(status, "not_met")
        self.assertIn("3", note)

    def test_relaxed_clause_still_rejects_below_the_relaxed_floor(self):
        """放宽到 3 年，2 年仍然不达标。"""
        status, note = self._gate("5年以上经验，优秀者可放宽至3年", years="2年")

        self.assertEqual(status, "not_met")
        self.assertIn("可放宽", note)

    def test_unrelated_relaxation_does_not_lower_the_floor(self):
        """“年龄可放宽至40岁”不是在放宽年限，5 年门槛必须保留。"""
        status, note = self._gate("5年以上经验，年龄可放宽至40岁")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)

    def test_lower_requirement_wins_over_preferred_higher_one(self):
        status, note = self._gate("3年以上经验即可，5年以上优先")

        self.assertNotEqual(status, "not_met")

    def test_first_hard_number_is_the_floor_not_the_largest(self):
        """“3年以上开发经验，5年以上管理经验”不能被抬成 5 年门槛。"""
        status, note = self._gate("3年以上开发经验，5年以上团队管理经验")

        self.assertNotEqual(status, "not_met")

    def test_unrelated_earlier_preference_does_not_soften_the_year(self):
        """“本科优先，5年以上经验”里的“优先”与年限无关。"""
        status, note = self._gate("本科优先，5年以上经验")

        self.assertEqual(status, "not_met")

    def test_missing_candidate_years_is_manual_review(self):
        report = _report()
        report["work_history"] = {"total_years": "", "relevant_years": ""}
        checklist = screening_agent._build_requirements_checklist(
            _requirements("5年以上经验", category="experience"), report, "张三\n简历正文"
        )

        self.assertEqual(checklist[0]["status"], "manual_review")

    def test_chinese_numeral_years_are_a_hard_gate(self):
        """JD 写“五年以上”而不是“5年以上”——中文数字不能被静默丢掉。"""
        status, note = self._gate("五年以上软件开发经验")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)

    def test_chinese_numeral_above_ten(self):
        """“十五年以上”要能解析成 15，不是 1 也不是 10。"""
        self.assertEqual(screening_agent._required_years_in_text("十五年以上经验")[0], 15)
        self.assertEqual(screening_agent._required_years_in_text("二十五年以上经验")[0], 25)

    def test_year_range_floor_is_the_lower_bound(self):
        """JD 写“3-5年”，3 年就是合格的；取上限会误杀。"""
        status, note = self._gate("3-5年软件开发经验")

        self.assertNotEqual(status, "not_met")
        self.assertIn("3年", note)

    def test_year_range_uppercase_variant(self):
        status, note = self._gate("3至5年工作经验")

        self.assertNotEqual(status, "not_met")

    def test_year_range_still_rejects_below_the_floor(self):
        """“5-8年”的门槛是 5 年，3 年候选人仍然不达标，且理由要写 5 不是 8。"""
        status, note = self._gate("5-8年工作经验")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)
        self.assertNotIn("8年以上经验", note)

    def test_date_range_is_not_a_year_requirement(self):
        """“2019-2024年”是日期区间，不能解析成“24年经验”。"""
        self.assertIsNone(screening_agent._required_years_in_text("任职时间 2019-2024年")[0])

    def test_upper_bound_wording_is_not_a_floor(self):
        """“1年以内工作经验”是上限，不能拿来拦 0.5 年候选人。"""
        status, note = self._gate("1年以内工作经验", years="0.5年")

        self.assertNotEqual(status, "not_met")

    def test_parenthetical_preference_does_not_soften_the_main_requirement(self):
        """括号注释里的“优先”属于注释，不能把 5 年硬门槛洗成软性要求。"""
        status, note = self._gate("5年以上经验（有大厂背景者优先）")

        self.assertEqual(status, "not_met")
        self.assertIn("5", note)

    def test_gate_reason_is_shown_instead_of_a_random_resume_line(self):
        """status=not_met 时依据必须是判定理由，不能是“技能：Python”这种关键词命中行。"""
        report = _report()
        report["work_history"] = {"total_years": "3年", "relevant_years": "3年"}
        checklist = screening_agent._build_requirements_checklist(
            _requirements("五年以上经验，熟悉Linux者优先", category="experience"),
            report,
            "张三\n技能：Python Linux",
        )

        self.assertEqual(checklist[0]["status"], "not_met")
        self.assertIn("年限", checklist[0]["resume_basis"])
        self.assertNotIn("技能：Python", checklist[0]["resume_basis"])


class CertificateGateTests(unittest.TestCase):
    """证书门槛：软性要求同样不得进入扣分。"""

    # 简历原文默认**不含**任何证书名：声称"持有"必须有原文正证（只认正证）。
    # 需要 met 的用例必须显式提供含证书名的原文，见 test_holding_claim_requires_text_evidence。
    _RESUME = "张三\n简历正文"

    def _gate(self, text, logic="required", certificates=("英语六级",), resume=None):
        report = _report()
        report["certificates"] = list(certificates)
        checklist = screening_agent._build_requirements_checklist(
            _requirements(text, logic, category="certificate"), report, self._RESUME if resume is None else resume
        )
        return checklist[0]["status"], checklist[0]["resume_basis"]

    def test_chinese_certificate_name_matches_despite_the_verb_prefix(self):
        """核心 bug：token 把“必须持有”一起吞了，导致简历里真有这个证书也判不达标。"""
        status, note = self._gate(
            "必须持有注册会计师证书", certificates=("注册会计师证书（CPA）",),
            resume="张三\n资格证书：注册会计师（CPA）",
        )

        self.assertEqual(status, "met")

    def test_teacher_certificate_matches(self):
        status, note = self._gate(
            "必须持有教师资格证", certificates=("教师资格证",), resume="张三\n持有教师资格证"
        )

        self.assertEqual(status, "met")

    def test_certificate_alias_shorthand_matches(self):
        """简历写“一建”，JD 写“一级建造师”，不能判未找到。"""
        status, note = self._gate(
            "必须持有一级建造师证书", certificates=("一建",), resume="张三\n证书：一建"
        )

        self.assertEqual(status, "met")

    def test_verb_less_hard_requirement_is_still_recognized(self):
        """“必须持有PMP”没写“证书”二字，仍然要认（原来看不到凭证名就整条不判定）。"""
        status, note = self._gate("必须持有PMP", certificates=("PMP认证",), resume="张三\nPMP认证")

        self.assertEqual(status, "met")

    def test_certificate_name_missing_is_manual_review(self):
        """只写“需具备以下证书”却没有证书名 → 不能据此判人缺证。"""
        status, note = self._gate("需具备以下证书", certificates=("英语六级",))

        self.assertEqual(status, "manual_review")

    def test_certificate_not_mentioned_is_manual_review(self):
        """三态·未写明：简历列了别的证书但没提这项 → 交人工，不判不达标、不扣分。"""
        status, note = self._gate("必须持有PMP证书")

        self.assertEqual(status, "manual_review")
        self.assertIn("未写明", note)

    def test_certificate_explicitly_absent_is_not_met(self):
        """三态·明确不符合：简历自己写明没有这项 → 这才是 not_met。"""
        status, note = self._gate("必须持有PMP证书", certificates=("无PMP证书",))

        self.assertEqual(status, "not_met")
        self.assertIn("已写明不符合", note)

    def test_certificate_level_conflict_is_not_met(self):
        """三态·明确不符合：要求一级建造师、简历只有二级 → 等级冲突。"""
        status, note = self._gate("必须持有一级建造师证书", certificates=("二级建造师",))

        self.assertEqual(status, "not_met")
        self.assertIn("已写明不符合", note)

    def test_preferred_logic_is_not_a_hard_gate(self):
        status, note = self._gate("必须持有PMP证书", logic="preferred")

        self.assertEqual(status, "manual_review")
        self.assertIn("软性", note)

    def test_matching_certificate_is_met(self):
        status, note = self._gate(
            "必须持有PMP证书", certificates=("PMP认证 项目管理专业认证",), resume="张三\nPMP认证"
        )

        self.assertEqual(status, "met")

    def test_resume_without_certificate_list_is_manual_review(self):
        status, note = self._gate("必须持有PMP证书", certificates=())

        self.assertEqual(status, "manual_review")

    # ── 真数据实测（docs/真实数据实测记录.md）抓到的误判：模型把 JD 口径写进简历证书字段 ──

    def test_model_status_string_is_not_a_proof_of_holding(self):
        """核心 bug：`信息系统项目管理师证书（高级）：必备，简历未体现` 被判成"已写明符合"。

        "未体现"是**缺席声明**，不是持有证明。旧实现只认"无/未取得"这类自述否证，
        于是把没写的证说成有——比漏放严重得多。
        """
        status, note = self._gate(
            "必须持有信息系统项目管理师证书（高级）",
            certificates=("信息系统项目管理师证书（高级）：必备，简历未体现",),
            resume="张三\n7年工作经验\n技能：SOLIDWORKS、PROE",
        )

        self.assertEqual(status, "manual_review")
        self.assertIn("未体现", note)

    def test_holding_claim_requires_text_evidence(self):
        """三态延伸：模型说持有、但简历原文里根本没有这个证名 → 不自动认定持有。"""
        status, note = self._gate(
            "必须持有PMP证书", certificates=("PMP认证",), resume="张三\n技能：Python Linux"
        )

        self.assertEqual(status, "manual_review")
        self.assertIn("未写明不等于没有", note)

    def test_unstated_note_does_not_become_not_met(self):
        """"未体现"只能降级为人工复核，绝不能升级成 not_met —— 未写明 ≠ 没有。"""
        status, note = self._gate(
            "必须持有PMP证书", certificates=("PMP证书：简历未注明",), resume="张三\n简历正文"
        )

        self.assertEqual(status, "manual_review")
        self.assertNotIn("已写明不符合", note)

    def test_self_declared_absence_is_still_not_met(self):
        """候选人在简历里自己写"无PMP证书" → 仍是明确不符合，不能被新护栏放宽掉。"""
        status, note = self._gate("必须持有PMP证书", certificates=("无PMP证书",))

        self.assertEqual(status, "not_met")
        self.assertIn("已写明不符合", note)

    def test_level_conflict_is_not_rescued_by_the_text_guard(self):
        """要求一级、简历只有二级：等级冲突优先于"原文里能找到建造师"这条正证。"""
        status, note = self._gate(
            "必须持有一级建造师证书", certificates=("二级建造师",), resume="张三\n二级建造师"
        )

        self.assertEqual(status, "not_met")
        self.assertIn("已写明不符合", note)

    def test_text_guard_accepts_alias_and_bare_forms(self):
        """正证认别名与去修饰语：JD 写"注册消防工程师"，简历原文写"消防工程师"。"""
        status, note = self._gate(
            "必须持有一级注册消防工程师证书", certificates=("消防工程师",), resume="张三\n消防工程师"
        )

        self.assertEqual(status, "met")

    def test_unstated_note_about_a_detail_is_not_an_absence(self):
        """真数据回归：模型那行写的是"简历未注明**有效期**"，说的是细节缺失，不是没有这项证。

        第一版护栏按整行命中"未注明"就把金利源（简历里真的写了这个证）误降级成人工复核。
        """
        status, note = self._gate(
            "必须持有信息系统项目管理师证书（高级）",
            certificates=("信息系统项目管理师（高级）+必备+简历未注明有效期，属国家软考高级资格",),
            resume="金利源\n7. 有信息系统项目管理师高级证书。\n资格证书\n信息系统项目管理师",
        )

        self.assertEqual(status, "met")

    def test_explicit_holding_mark_wins_over_a_note_in_the_same_line(self):
        """同一行里"PMP：已持有；CPA：简历未体现"，PMP 仍应算持有。"""
        status, note = self._gate(
            "必须持有PMP证书",
            certificates=("PMP：已持有；CPA：简历未体现",),
            resume="张三\nPMP认证",
        )

        self.assertEqual(status, "met")

    def test_weak_wording_is_not_a_hard_gate_and_says_why(self):
        """“有PMP证书者优先”没有强约束措辞，不构成硬门槛，并说明原因。"""
        status, note = self._gate("有PMP证书者优先")

        self.assertEqual(status, "manual_review")
        self.assertIn("强约束", note)


class ParsingCoverageTests(unittest.TestCase):
    """2026-10-10 第二轮对抗测试暴露的缺口（证书列名 / 专项年限 / 专业方向 / 别名等级）。"""

    def _gate(self, text, category="education", report=None, logic="required", resume=None):
        report = report or _report()
        checklist = screening_agent._build_requirements_checklist(
            _requirements(text, logic, category=category), report,
            "张三\n简历正文" if resume is None else resume,
        )
        return checklist[0]["status"], checklist[0]["resume_basis"]

    def _with_years(self, years):
        report = _report()
        report["work_history"] = {"total_years": years, "relevant_years": years}
        return report

    def _with_certs(self, *certs):
        report = _report()
        report["certificates"] = list(certs)
        return report

    # ── 证书名写在冒号后面 ──────────────────────────────────────────
    def test_certificates_listed_after_colon_are_gated(self):
        """冒号后面写的证书名照样是门槛；简历只有别的证书 → 未写明，交人工。"""
        status, note = self._gate(
            "需具备以下证书：一级建造师", "certificate", self._with_certs("英语六级")
        )

        self.assertEqual(status, "manual_review")
        self.assertIn("未写明", note)

    def test_certificates_listed_after_colon_match(self):
        status, note = self._gate(
            "需具备以下证书：一级建造师", "certificate", self._with_certs("一级建造师证书"),
            resume="张三\n一级建造师证书",
        )

        self.assertEqual(status, "met")

    def test_multiple_certificates_after_colon_are_split(self):
        status, note = self._gate(
            "需具备以下证书：注册会计师、软考中级", "certificate", self._with_certs("注册会计师证书"),
            resume="张三\n注册会计师证书",
        )

        self.assertEqual(status, "met")

    # ── 别名 / 修饰语 / 等级容错 ────────────────────────────────────
    def test_institutional_modifier_is_ignored(self):
        status, note = self._gate(
            "必须持有一级注册消防工程师", "certificate", self._with_certs("消防工程师"),
            resume="张三\n消防工程师",
        )

        self.assertEqual(status, "met")

    def test_certificate_level_conflict_is_not_a_match(self):
        """简历写“二级建造师”不能算满足“一级建造师”。"""
        status, note = self._gate(
            "必须持有一级建造师证书", "certificate", self._with_certs("二级建造师"),
            resume="张三\n二级建造师",
        )

        self.assertEqual(status, "not_met")

    def test_english_level_alias_matches(self):
        status, note = self._gate(
            "必须持有CET6证书", "certificate", self._with_certs("英语六级证书"),
            resume="张三\n英语六级证书",
        )

        self.assertEqual(status, "met")

    def test_accounting_title_alias_matches(self):
        status, note = self._gate(
            "必须持有中级会计职称证书", "certificate", self._with_certs("中级会计师"),
            resume="张三\n中级会计师",
        )

        self.assertEqual(status, "met")

    def test_unrelated_certificate_is_not_a_match(self):
        """简历有 ACCA、岗位要注会 → 不算满足，但也不算"明确不符合"（未写明，交人工）。"""
        status, note = self._gate("必须持有注册会计师证书", "certificate", self._with_certs("ACCA"))

        self.assertEqual(status, "manual_review")
        self.assertIn("未写明", note)

    # ── 专项年限 ────────────────────────────────────────────────────
    def test_specialized_years_are_not_confirmed_by_total_years(self):
        """总年限 8 年不能替“5 年以上团队管理经验”下满足结论。"""
        status, note = self._gate("5年以上团队管理经验", "experience", self._with_years("8年"))

        self.assertEqual(status, "manual_review")
        self.assertIn("专项年限", note)

    def test_specialized_years_still_reject_impossible_candidates(self):
        """总年限只有 3 年 → 5 年专项年限必然不满足，仍然要拦。"""
        status, note = self._gate("5年以上团队管理经验", "experience", self._with_years("3年"))

        self.assertEqual(status, "not_met")

    def test_plain_development_years_are_unaffected(self):
        """“5年以上软件开发经验”是最常用的写法，不能被降级成人工确认。"""
        status, note = self._gate("5年以上软件开发经验", "experience", self._with_years("5年"))

        self.assertEqual(status, "met")

    def test_teamwork_in_another_clause_is_not_a_specialization(self):
        """“熟悉团队协作”不是专项年限限定。"""
        status, note = self._gate("5年以上经验，熟悉团队协作", "experience", self._with_years("5年"))

        self.assertEqual(status, "met")

    # ── 专业方向（只提示，不改判定）─────────────────────────────────
    def test_major_direction_matched_is_mentioned(self):
        report = _report()
        report["education_history"][0]["major"] = "计算机科学与技术"
        status, note = self._gate("本科及以上学历，计算机相关专业", report=report)

        self.assertEqual(status, "manual_review")
        self.assertIn("计算机类方向", note)

    def test_major_mismatch_is_flagged_for_humans_without_penalty(self):
        report = _report()
        report["education_history"][0]["major"] = "汉语言文学"
        status, note = self._gate("本科及以上学历，计算机相关专业", report=report)

        self.assertEqual(status, "manual_review")
        self.assertIn("汉语言文学", note)
        self.assertIn("人工判断", note)

    def test_missing_major_is_asked_for(self):
        report = _report()
        report["education_history"][0]["major"] = ""
        status, note = self._gate("本科及以上学历，计算机相关专业", report=report)

        self.assertIn("未提供专业信息", note)

    def test_preferred_major_is_not_stated_as_a_requirement(self):
        report = _report()
        report["education_history"][0]["major"] = "汉语言文学"
        status, note = self._gate("本科及以上学历，计算机相关专业优先", report=report)

        self.assertNotIn("计算机类", note)

    def test_major_note_does_not_change_a_failing_degree(self):
        """专业提示不得干扰学历层级的 not_met（该拦的还是要拦）。"""
        report = _report(degree="大专")
        report["education_history"][0]["major"] = "汉语言文学"
        status, note = self._gate("本科及以上学历，计算机相关专业", report=report)

        self.assertEqual(status, "not_met")


class GateBackstopTests(unittest.TestCase):
    """门禁的"启用权"不能只在模型手里：分类只升不降 + 原文直扫兜底。

    模型填的 `hard` / `category` / 抽取完整度任何一处失手，整层门槛就静默消失
    （清单为空 → 零扣分、零封顶），所以原文必须再扫一遍。
    反向风险同样致命：把公司叙述（"成员均为本科以上学历"）补成门槛会冤杀专科候选人。
    """

    def _reqs(self, *items):
        return {
            "requirements": [
                {"id": f"r{index}", "text": text, "category": category, "hard": hard, "logic": "required"}
                for index, (text, category, hard) in enumerate(items)
            ]
        }

    def test_mislabeled_gate_is_upgraded(self):
        for text, category in (
            ("硕士及以上学历", "skill"),
            ("5年以上后端开发经验", "responsibility"),
            ("必须持有PMP证书", "other"),
            ("本科985/211院校", "other"),
        ):
            requirements = self._reqs((text, category, False))

            self.assertEqual(screening_agent._upgrade_requirement_gates(requirements), 1, text)
            item = requirements["requirements"][0]
            self.assertNotEqual(item["category"], category, text)
            self.assertTrue(item["hard"], text)
            self.assertEqual(item["source"], "gate_backstop_upgrade")
            self.assertEqual(item["category_upgraded_from"], category)

    def test_only_the_hard_flag_is_upgraded(self):
        requirements = self._reqs(("本科及以上学历", "education", False))

        screening_agent._upgrade_requirement_gates(requirements)

        self.assertEqual(requirements["requirements"][0]["category"], "education")
        self.assertTrue(requirements["requirements"][0]["hard"])

    def test_soft_and_waived_are_never_upgraded(self):
        """优先项 / 不限 / 纯描述性技能：兜底不许把它们硬起来。"""
        for text in ("5年以上经验者优先", "学历不限，专业不限", "熟悉Linux者优先", "具备良好的沟通能力"):
            requirements = self._reqs((text, "skill", False))

            self.assertEqual(screening_agent._upgrade_requirement_gates(requirements), 0, text)
            self.assertFalse(requirements["requirements"][0]["hard"], text)

    def test_description_is_not_upgraded_to_a_gate(self):
        requirements = self._reqs(("成员均为本科以上学历", "skill", False))

        self.assertEqual(screening_agent._upgrade_requirement_gates(requirements), 0)
        self.assertFalse(requirements["requirements"][0]["hard"])

    def test_backstop_recovers_missed_requirements(self):
        requirements = {"requirements": []}
        coverage = screening_agent._gate_backstop(
            "岗位要求：\n1.硕士及以上学历；\n2.5年以上后端开发经验；\n3.必须持有PMP证书。", requirements
        )

        self.assertEqual(coverage["added"], 3)
        self.assertEqual(coverage["markers_found"], 3)
        self.assertEqual(
            sorted(item["category"] for item in requirements["requirements"]),
            ["certificate", "education", "experience"],
        )
        for item in requirements["requirements"]:
            self.assertTrue(item["hard"])
            self.assertEqual(item["source"], "gate_backstop")

    def test_backstop_ignores_descriptive_clauses(self):
        """叙述句不是要求：补成门槛会让专科候选人被封顶 59。"""
        for job in ("团队介绍：成员均为本科以上学历，公司位于深圳。", "我们团队成员毕业于985/211院校。"):
            requirements = {"requirements": []}
            coverage = screening_agent._gate_backstop(job, requirements)

            self.assertEqual(requirements["requirements"], [], job)
            self.assertEqual(coverage["markers_found"], 0, job)

    def test_backstop_ignores_duty_sections(self):
        requirements = {"requirements": []}

        screening_agent._gate_backstop("岗位职责：\n负责为本科以上学历客户提供咨询服务", requirements)

        self.assertEqual(requirements["requirements"], [])

    def test_backstop_ignores_preference_and_waived_clauses(self):
        for job in (
            "硕士及以上学历优先；5年以上经验加分；PMP尤佳。",
            "学历不限；经验不限；不要求985/211；不需要证书。",
        ):
            requirements = {"requirements": []}
            coverage = screening_agent._gate_backstop(job, requirements)

            self.assertEqual(requirements["requirements"], [], job)
            self.assertEqual(coverage["markers_found"], 0, job)

    def test_backstop_does_not_duplicate_existing_requirements(self):
        requirements = self._reqs(
            ("本科及以上学历", "education", True),
            ("3年以上工作经验", "experience", True),
        )

        coverage = screening_agent._gate_backstop("本科及以上学历，3年以上工作经验。", requirements)

        self.assertEqual(coverage["added"], 0)
        self.assertEqual(len(requirements["requirements"]), 2)

    def test_backstop_is_idempotent(self):
        job = "硕士及以上学历；5年以上经验；必须持有PMP。"
        requirements = {"requirements": []}

        first = screening_agent._gate_backstop(job, requirements)
        second = screening_agent._gate_backstop(job, requirements)

        self.assertEqual(first["added"], 3)
        self.assertEqual(second["added"], 0)

    def test_backstop_item_count_is_capped(self):
        job = "\n".join(["岗位要求："] + [f"{year}年以上工作经验" for year in range(3, 12)])
        requirements = {"requirements": []}

        coverage = screening_agent._gate_backstop(job, requirements)

        self.assertLessEqual(coverage["added"], screening_agent._MAX_BACKSTOP_ITEMS)
        self.assertGreater(coverage["added"], 0)

    def test_backstop_restores_a_gate_the_model_switched_off(self):
        """模型把硬门槛全标成 hard=false：兜底之后门槛仍然会跑，且仍然封顶。"""
        job = "岗位要求：\n1.硕士及以上学历\n2.5年以上后端开发经验"
        requirements = self._reqs(
            ("硕士及以上学历", "skill", False),
            ("5年以上后端开发经验", "skill", False),
        )
        screening_agent._upgrade_requirement_gates(requirements)
        screening_agent._gate_backstop(job, requirements)
        report = _report(degree="大专", tier="")
        report["work_history"] = {"total_years": "3年"}

        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n大专\n3年经验")
        failed = [item for item in checklist if item["status"] == "not_met"]

        self.assertEqual([item["category"] for item in failed], ["education", "experience"])
        self.assertEqual(failed[0]["gate_dimension"], "degree")
        self.assertTrue(all(item.get("source") for item in failed))


class WaivedRequirementTests(unittest.TestCase):
    """“不要求/不限”是取消要求，不是要求；豁免必须按分句生效。"""

    def test_waived_tier_is_not_a_requirement(self):
        report = _report(degree="大专", tier="普通本科")

        status, note, dimension = screening_agent._check_education_gate("不要求985/211学历", report)

        self.assertEqual(status, "manual_review")
        self.assertEqual(dimension, "")

    def test_waived_degree_is_not_a_requirement(self):
        report = _report(degree="大专", tier="普通本科")

        status, _note, dimension = screening_agent._check_education_gate("学历不限", report)

        self.assertEqual(status, "manual_review")
        self.assertEqual(dimension, "")

    def test_waiver_is_clause_scoped(self):
        """“5年以上经验，不限专业”里的“不限”只能豁免专业，不能把年限一起豁免。"""
        report = {"work_history": {"total_years": "3年"}}
        requirements = _requirements("5年以上经验，不限专业", category="experience")

        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n3年经验")

        self.assertEqual(checklist[0]["status"], "not_met")

    def test_waived_requirement_does_not_fire_the_major_note(self):
        report = _report(degree="本科")
        report["education_history"][0]["major"] = "汉语言文学"

        _status, note = _gate("本科及以上学历，专业不限", report)

        self.assertNotIn("汉语言文学", note)


class GateBackstopConcurrencyTests(unittest.TestCase):
    """批量分析（最多 3 份）共享同一份 requirements 并发跑：兜底必须幂等。

    否则两个线程同时判断"这条门槛没人覆盖"就会重复补一条 → 重复扣分、清单出现重影。
    """

    def test_shared_requirements_are_backstopped_only_once(self):
        from concurrent.futures import ThreadPoolExecutor

        job = "硕士及以上学历；5年以上经验；必须持有PMP。"
        requirements = {"requirements": []}

        with ThreadPoolExecutor(max_workers=3) as pool:
            reports = list(pool.map(lambda _: screening_agent._apply_gate_backstop(job, requirements), range(3)))

        self.assertEqual(len(requirements["requirements"]), 3)
        self.assertEqual({item["id"] for item in requirements["requirements"]},
                         {"gate-backstop-1", "gate-backstop-2", "gate-backstop-3"})
        self.assertEqual(len({tuple(sorted(item["text"] for item in requirements["requirements"]))}), 1)
        self.assertTrue(all(report["added"] == 3 for report in reports))

    def test_repeated_calls_reuse_the_same_report(self):
        job = "本科及以上学历；3年以上工作经验。"
        requirements = {"requirements": []}

        first = screening_agent._apply_gate_backstop(job, requirements)
        second = screening_agent._apply_gate_backstop(job, requirements)

        self.assertEqual(first, second)
        self.assertEqual(len(requirements["requirements"]), 2)

    def test_private_bookkeeping_never_reaches_the_prompt(self):
        """兜底会在 requirements 上留私有账目，不能污染提示词。"""
        job = "硕士及以上学历"
        requirements = {"requirements": []}

        screening_agent._apply_gate_backstop(job, requirements)
        prompt = screening_agent._report_prompt(job, "张三\n大专", requirements, [], "2026-10")

        self.assertNotIn("_gate_backstop", prompt)
        self.assertIn("硕士及以上学历", prompt)


class GateHardeningTests(unittest.TestCase):
    """对抗性审计复现出的四个"门槛启得动、但结论是错的"缺口（详见 docs §3.7）。"""

    def test_preference_in_the_same_line_does_not_blind_the_backstop(self):
        """门槛与"I prefer"并列在同一行时，兜底不能对整行失明。

        旧实现把豁免/优先分句剥掉后用空格重拼，分句边界被抹平，
        `_hard_tokens` 的按分句判断于是把整行看成软性 → 这一行的硬门槛静默消失。
        """
        for text, token in (("本科及以上学历，硕士优先", "本科"), ("5年以上开发经验，熟悉Linux者优先", "5年")):
            markers = screening_agent._gate_markers_in_text(text)

            self.assertEqual([m["token"] for m in markers], [token], text)

        job = "岗位要求：\n1.本科及以上学历，硕士优先\n"
        requirements = {"requirements": []}

        coverage = screening_agent._gate_backstop(job, requirements)

        self.assertEqual(coverage["added"], 1)
        self.assertEqual(requirements["requirements"][0]["category"], "education")

    def test_soft_logic_is_corrected_along_with_the_hard_flag(self):
        """文本自己写着"必须持有/及以上"却带 logic=preferred：升级必须连 logic 一起纠正。

        只改 hard 的话三个门槛函数第一行就返回 manual_review，而覆盖面板显示"已纠正 1 条"——
        所有可见信号都是"已生效"，实际一道门槛都没跑。
        """
        requirements = {"requirements": [
            {"id": "r1", "text": "必须持有PMP证书", "category": "certificate", "hard": False, "logic": "preferred"},
        ]}

        self.assertEqual(screening_agent._upgrade_requirement_gates(requirements), 1)
        item = requirements["requirements"][0]

        self.assertEqual(item["logic"], "required")
        self.assertEqual(item["logic_upgraded_from"], "preferred")
        # 升级后门槛真的在跑：简历没有 PMP → 按三态交人工（判出确定性结论才算"没白升"）
        self.assertEqual(
            screening_agent._check_certificate_gate(item["text"], {"certificates": ["英语六级"]}, item["logic"])[0],
            "manual_review",
        )
        self.assertEqual(
            screening_agent._check_certificate_gate(item["text"], {"certificates": ["无PMP证书"]}, item["logic"])[0],
            "not_met",
        )

    def test_duty_sentences_are_not_degree_gates(self):
        """"大学"是机构名不是学位层级：职责句里的它不能变成"要求本科学历"。"""
        self.assertEqual(screening_agent._min_degree_in_text("负责与大学院校的技术对接"), "")
        self.assertFalse(screening_agent._gate_clause_is_requirement("负责与大学院校的技术对接", "1、"))
        self.assertFalse(screening_agent._gate_clause_is_requirement("负责为本科以上学历客户提供咨询", "职责"))

        requirements = {"requirements": [
            {"id": "r1", "text": "负责与大学院校的技术对接", "category": "responsibility",
             "hard": False, "logic": "required"},
        ]}

        self.assertEqual(screening_agent._upgrade_requirement_gates(requirements), 0)
        self.assertEqual(requirements["requirements"][0]["category"], "responsibility")

        # 职责区整个跳过：编号列表也不能把职责句顶上"上一句是标题"的通道
        job = "岗位职责：\n1、负责与大学院校的技术对接\n任职要求：\n1、大专及以上学历\n"
        requirements = {"requirements": []}

        coverage = screening_agent._gate_backstop(job, requirements)

        self.assertEqual([item["text"] for item in requirements["requirements"]], ["大专及以上学历"])
        self.assertEqual(coverage["added"], 1)

    def test_other_degree_matches_unknown_not_below_college(self):
        """"其他"等于"说不清"，必须和 `未提供`/`高中` 一样走人工复核，不能当成低于大专封顶 59。"""
        for degree in ("其他", "未提供", "高中"):
            report = {"education_history": [{"degree": degree, "school_name": "某学院", "school_tier": "普通本科"}]}
            status = screening_agent._check_education_gate("本科及以上学历", report)[0]

            self.assertEqual(status, "manual_review", degree)

    def test_waived_stripping_keeps_original_punctuation(self):
        """剥豁免分句时必须保留原标点：证书清单靠冒号/顿号取值，重拼会把清单拆散。"""
        self.assertEqual(screening_agent._waived_text("需具备以下证书：一级建造师"), "需具备以下证书：一级建造师")
        self.assertEqual(
            screening_agent._check_certificate_gate("需具备以下证书：一级建造师", {"certificates": ["一级建造师证书"]})[0],
            "met",
        )
        # 被豁免的分句连内容一起消失，但分隔符留在原位
        self.assertEqual(screening_agent._waived_text("需具备以下证书：不限"), "需具备以下证书：")


class SchoolListTests(unittest.TestCase):
    """服务端院校名单：把"是不是 985/211"从模型自报改成确定性事实。"""

    def test_list_overrides_an_underclaimed_model_label(self):
        """模型把复旦标成"一本"，名单知道它是 985 → JD 要求 985 时必须算满足。"""
        status, note = _gate("985院校毕业", _report(tier="一本", school="复旦大学"))

        self.assertEqual(status, "manual_review")
        self.assertIn("985", note)
        self.assertNotIn("不达标", note)

    def test_list_overrides_a_wrong_model_label_in_the_safe_direction(self):
        """模型把复旦标成"二本"（明显错），名单同样纠正，不产生 not_met。"""
        status = _gate("985院校毕业", _report(tier="二本", school="复旦大学"))[0]

        self.assertEqual(status, "manual_review")

    def test_double_first_class_requirement_no_longer_kills_a_dual_class_school(self):
        """冤杀回归：JD"双一流" + 真·双一流非 211 学校 → 不许判 not_met。

        “一本”与“双一流”并不互斥，模型词表里又没有“双一流”，
        旧逻辑把“一本”读成“明确不是 985/211/双一流”并封顶 69。
        现在名单里有这 30 所（第二轮新增），所以直接判满足。
        """
        for school in ("宁波大学", "南京邮电大学", "山西大学"):
            status, note = _gate("双一流院校毕业", _report(tier="一本", school=school))

            self.assertEqual(status, "manual_review", school)
            self.assertIn("双一流", note, school)
            self.assertIn("满足", note, school)

    def test_dual_class_school_without_211_is_not_treated_as_211(self):
        """名单是分档的：宁波大学是双一流但**不是**211，211 门槛不能算满足。"""
        status, note = _gate("211院校本科及以上", _report(tier="一本", school="宁波大学"))

        self.assertEqual(status, "manual_review")
        self.assertIn("无法确定", note)
        self.assertIn("人工核实", note)

    def test_unknown_school_still_goes_to_human_review(self):
        """名单查不到（如独立学院/境外/写法不同）→ 只认正证，交人工，不封顶。"""
        status, note = _gate("双一流院校毕业", _report(tier="一本", school="某某大学"))

        self.assertEqual(status, "manual_review")
        self.assertIn("人工核实", note)

    def test_985_requirement_still_rejects_a_plain_college(self):
        """名单存在的前提：985/211 名单是完整的，所以"明确低于"仍要拦住。"""
        status = _gate("985院校毕业", _report(tier="二本", school="某某学院"))[0]

        self.assertEqual(status, "not_met")

    def test_campus_alias_maps_to_its_parent(self):
        """分校区名（哈工大威海）指回本部：名单里查得到，就不再靠模型猜。"""
        self.assertEqual(screening_agent.school_tiers.tier_label("哈尔滨工业大学（威海）"), "985")
        self.assertEqual(screening_agent.school_tiers.tier_label("北京大学医学部"), "985")

    def test_unknown_school_yields_no_positive_claim(self):
        """名单查不到 → 空标签（无法证明），不能反推"不是"985/211。"""
        self.assertEqual(screening_agent.school_tiers.tier_label("某某大学"), "")
        self.assertEqual(screening_agent.school_tiers.tier_label(""), "")
        self.assertEqual(screening_agent.school_tiers.tier_label("北京邮电大学"), "211")

    def test_list_data_is_complete_enough_to_trust(self):
        """数据自检：985 = 39 所、211 含官方全部、双一流 = 教育部第二轮 147 所。"""
        stats = screening_agent.school_tiers.stats()

        self.assertEqual(stats["985"], 39)
        self.assertGreaterEqual(stats["211"], 116)
        # 148 条 = 官方 147 所 + 华北电力大学（保定）校区条目（按本部归属补标）
        self.assertGreaterEqual(stats["dual_first_class"], 147)

    def test_every_985_211_school_is_also_dual_first_class(self):
        """一致性自检：985/211 全部在双一流名单内（华北电力保定校区按本部归属）。"""
        import json
        import os

        with open(os.path.join(os.path.dirname(screening_agent.__file__), "school_tiers.json"),
                  encoding="utf-8") as handle:
            schools = json.load(handle)["schools"]

        for entry in schools:
            if {"985", "211"} & set(entry["tags"]):
                self.assertIn("双一流", entry["tags"], entry["name"])


class EducationFormTests(unittest.TestCase):
    """学历形式（全日制/统招/第一学历）：只提示、永不判定。"""

    def test_full_time_requirement_produces_a_note_not_a_failure(self):
        status, note = _gate("全日制本科及以上学历", _report(degree="本科"))

        self.assertEqual(status, "manual_review")
        self.assertIn("全日制", note)
        self.assertIn("人工核实", note)
        self.assertNotIn("不达标", note)

    def test_promoted_degree_never_turns_form_into_a_failure(self):
        """统招本科要求 + 硕士学历：学历满足，形式仍只提示。"""
        status, note = _gate("统招本科及以上学历", _report(degree="硕士"))

        self.assertEqual(status, "manual_review")
        self.assertIn("统招", note)

    def test_waived_form_requirement_is_silent(self):
        self.assertEqual(screening_agent._education_form_note("本科及以上学历，不限学习形式"), "")
        self.assertEqual(screening_agent._education_form_note("本科及以上学历"), "")

    def test_preferred_form_requirement_is_silent(self):
        """"全日制优先"不是门槛，不该产出提示。"""
        self.assertEqual(screening_agent._education_form_note("本科及以上学历，全日制优先"), "")

    def test_tier_and_form_notes_coexist(self):
        status, note = _gate("全日制本科及以上学历，985院校", _report(degree="本科", tier="985"))

        self.assertEqual(status, "manual_review")
        self.assertIn("全日制", note)
        self.assertIn("满足", note)

    def test_first_degree_phrasing_is_covered(self):
        self.assertIn("第一学历", screening_agent._education_form_note("第一学历须为本科"))


class UndeterminedRequirementTests(unittest.TestCase):
    """系统不判定的真实门槛必须**可见**，不能静默。"""

    def test_common_real_world_gates_are_listed(self):
        job = (
            "任职要求：\n"
            "1、全日制本科及以上学历，2026届应届毕业生；\n"
            "2、年龄35岁以下，中共党员；\n"
            "3、英语六级425分以上，持C1驾照；\n"
            "4、能接受长期出差，工作地点深圳。\n"
        )
        found = {item["type"] for item in screening_agent._undetermined_requirements(job)}

        self.assertEqual(
            found, {"学历形式", "届别", "年龄", "政治面貌", "语言等级", "驾驶资格", "工作地点"}
        )

    def test_preference_clauses_are_not_listed(self):
        self.assertEqual(screening_agent._undetermined_requirements("中共党员优先，年龄不限"), [])

    def test_waived_clauses_are_not_listed(self):
        self.assertEqual(screening_agent._undetermined_requirements("工作地点不限，无年龄要求"), [])

    def test_plain_job_description_yields_nothing(self):
        job = "任职要求：本科及以上学历，3年以上后端开发经验，熟悉 Python。"
        self.assertEqual(screening_agent._undetermined_requirements(job), [])

    def test_each_item_carries_evidence_and_a_note(self):
        result = screening_agent._undetermined_requirements("任职要求：\n1、年龄35岁以下")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "年龄")
        self.assertIn("35", result[0]["text"])
        self.assertIn("人工", result[0]["note"])

    def test_certificate_style_gates_are_not_reported_as_undetermined(self):
        """报告不能自相矛盾：`必须持有英语六级证书` 走的是证书门槛（判出 met），就不该再列进"未判定"。"""
        report = {"certificates": ["英语六级"], "education_history": []}

        self.assertEqual(screening_agent._check_certificate_gate("必须持有英语六级证书", report)[0], "met")
        self.assertEqual(screening_agent._undetermined_requirements(
            "任职要求：\n1、必须持有英语六级证书", report), [])

    def test_license_stays_listed_because_the_gate_only_declines(self):
        """`必须持有C1驾照` 时证书门槛只回 manual_review（没真判）→ 必须留在可见清单里。"""
        report = {"certificates": ["英语六级"], "education_history": []}

        self.assertEqual(screening_agent._check_certificate_gate("必须持有C1驾照", report)[0], "manual_review")
        listed = screening_agent._undetermined_requirements("任职要求：\n1、必须持有C1驾照", report)

        self.assertEqual([item["type"] for item in listed], ["驾驶资格"])

    def test_threshold_style_language_gate_is_still_reported(self):
        """`英语六级425分以上` 没有证书写法，证书门槛不接管 → 仍然必须可见。"""
        report = {"certificates": ["英语六级"], "education_history": []}
        listed = screening_agent._undetermined_requirements("任职要求：\n1、英语六级425分以上", report)

        self.assertEqual([item["type"] for item in listed], ["语言等级"])

    def test_without_report_nothing_is_skipped(self):
        """拿不到简历时（report=None）不做覆盖判断：宁可多列，也不漏报。"""
        listed = screening_agent._undetermined_requirements("任职要求：\n1、必须持有英语六级证书")

        self.assertEqual([item["type"] for item in listed], ["语言等级"])

    def test_list_is_capped(self):
        job = "任职要求：" + "；".join(
            ["全日制本科", "2026届应届", "年龄35岁以下", "中共党员", "英语六级", "持C1驾照",
             "工作地点北京", "身高170以上", "中级及以上职称", "本地户籍"]
        ) + "。"
        self.assertLessEqual(len(screening_agent._undetermined_requirements(job)), 8)


class EvidenceStateTests(unittest.TestCase):
    """证据三态：人话标签 + 只有"明确不符合"才算不达标。"""

    def _entry(self, text, category="education", **report_over):
        report = _report()
        report.update(report_over)
        checklist = screening_agent._build_requirements_checklist(
            _requirements(text, category=category), report, "张三\n简历正文"
        )
        return checklist[0]

    def test_labels_are_plain_language(self):
        self.assertEqual(screening_agent._EVIDENCE_LABELS, {
            "matched": "已写明符合",
            "conflict": "已写明不符合",
            "missing": "未写明或无法核对，需人工确认",
        })

    def test_degree_met_is_matched_not_missing(self):
        """学历满足时门槛也返回 manual_review（真实性待核实），但证据态必须是"已写明符合"。"""
        entry = self._entry("本科及以上学历")

        self.assertEqual(entry["status"], "manual_review")
        self.assertEqual(entry["evidence_state"], "matched")
        self.assertEqual(entry["evidence_label"], "已写明符合")

    def test_degree_below_is_conflict(self):
        entry = self._entry("硕士及以上学历")

        self.assertEqual(entry["status"], "not_met")
        self.assertEqual(entry["evidence_state"], "conflict")

    def test_no_degree_information_is_missing(self):
        entry = self._entry("本科及以上学历", education_history=[])

        self.assertEqual(entry["evidence_state"], "missing")

    def test_experience_unclear_is_missing(self):
        entry = self._entry("5年以上经验", category="experience")

        self.assertEqual(entry["evidence_state"], "missing")

    def test_certificate_three_states(self):
        # 原文必须真的写了这项证：声称持有要有正证（只认正证）
        matched = screening_agent._build_requirements_checklist(
            _requirements("必须持有PMP证书", category="certificate"),
            {**_report(), "certificates": ["PMP认证"]}, "张三\nPMP认证")[0]
        missing = screening_agent._build_requirements_checklist(
            _requirements("必须持有PMP证书", category="certificate"),
            {**_report(), "certificates": ["英语六级"]}, "张三")[0]
        conflict = screening_agent._build_requirements_checklist(
            _requirements("必须持有PMP证书", category="certificate"),
            {**_report(), "certificates": ["无PMP证书"]}, "张三")[0]

        self.assertEqual(
            [(item["status"], item["evidence_state"]) for item in (matched, missing, conflict)],
            [("met", "matched"), ("manual_review", "missing"), ("not_met", "conflict")],
        )


class EvidenceSufficiencyTests(unittest.TestCase):
    """证据充分性：通用谓词不能当"已写明符合"的依据（真实 IOT 实施 JD 实测抓到的假阳性）。"""

    def _entry(self, text, resume, category="skill"):
        report = _report()
        report["certificates"] = []
        checklist = screening_agent._build_requirements_checklist(
            _requirements(text, "required", category=category), report, resume
        )
        return checklist[0]

    def test_generic_verb_is_not_evidence(self):
        """JD「熟练掌握RFID…」不能因为简历写了「熟练掌握Linux」就判已写明符合。"""
        entry = self._entry(
            "熟练掌握RFID、一二维码、模组等IOT设备的工作原理",
            "张三\n2. 熟练掌握Linux命令及系统安装，具备服务器运维经验",
        )

        self.assertEqual(entry["status"], "not_mentioned")
        self.assertEqual(entry["evidence_state"], "missing")
        self.assertEqual(entry["resume_basis"], "")

    def test_common_noun_is_not_evidence(self):
        """JD 要「输出《现场作业SOP手册》」，简历写「一种大力量手册盖」不算证据。"""
        entry = self._entry(
            "输出《现场作业SOP手册》",
            "张三\n2.两例实用型专利：一种大力量手册盖、一种陶封芯片用管脚整形治具",
        )

        self.assertEqual(entry["status"], "not_mentioned")

    def test_responsibility_verb_is_not_evidence(self):
        """JD「负责IOT硬件（通道机、PDA等）…」，简历只共用一个「负责」不算证据。"""
        entry = self._entry(
            "系统部署与联调：负责IOT硬件（通道机、PDA等）与软件系统的现场安装、部署、效果联调",
            "张三\n8. 熟悉erp系统，日常负责研发部采购管理",
        )

        self.assertEqual(entry["status"], "not_mentioned")

    def test_distinctive_term_still_counts(self):
        """正向对照：简历真写了 RFID 就必须认。"""
        entry = self._entry(
            "熟练掌握RFID、一二维码、模组等IOT设备的工作原理",
            "张三\n负责RFID读写器选型与通道机现场联调",
        )

        self.assertEqual(entry["status"], "met")
        self.assertEqual(entry["evidence_state"], "matched")

    def test_two_char_topic_words_do_not_count(self):
        """两字泛词（硬件/部署/手册/负责）一律不算证据。"""
        entry = self._entry(
            "熟悉网络布线、设备安装调试",
            "张三\n负责硬件部署与手册编写",
        )

        self.assertEqual(entry["status"], "not_mentioned")

    def test_real_resume_line_with_full_phrase_counts(self):
        """正向对照：简历里逐字写了这个短语，才算依据。"""
        entry = self._entry(
            "熟悉网络布线、设备安装调试",
            "张三\n熟悉网络布线、设备安装调试，能独立完成现场改造",
        )

        self.assertEqual(entry["status"], "met")
        self.assertIn("网络布线", entry["resume_basis"])


class GateTierPolicyTests(unittest.TestCase):
    """P1 分档：A 学历层级 / B 岗位硬门槛 / C 生活类，以及"可放宽"与破格复核。"""

    def _normalize(self, jd, report, category="education"):
        raw = dict(report)
        raw["requirements_checklist"] = screening_agent._build_requirements_checklist(
            _requirements(jd, category=category), raw, "张三\n简历正文"
        )
        return raw["requirements_checklist"][0], backend._normalize_hr_analysis(raw, jd, "张三\n简历正文")

    def test_tier_assignment(self):
        cases = [
            ("硕士及以上学历", "education", _report(), "A"),
            ("985院校本科", "education", _report(tier="二本", school="某某学院"), "B"),
            ("5年以上经验", "experience", _report(), "B"),
            ("必须持有注册会计师证书", "certificate", _report(), "B"),
            ("必须持有英语六级证书", "certificate", _report(), "C"),
        ]
        for text, category, report, expected in cases:
            with self.subTest(text=text):
                checklist = screening_agent._build_requirements_checklist(
                    _requirements(text, category=category), report, "张三"
                )
                self.assertEqual(checklist[0]["gate_tier"], expected)

    def test_c_tier_is_lighter_than_b(self):
        """语言等级不达标（C 档）：扣 5、封顶 79，不该和注会同价。"""
        _, result = self._normalize(
            "必须持有英语六级证书", {**_report(), "certificates": ["无英语六级证书"]},
            category="certificate")

        self.assertEqual(result["gate_tier_counts"]["C"], 1)
        self.assertEqual(result["hard_gate_deduction"], 5)
        self.assertEqual(result["final_score"], 79)
        self.assertEqual(result["fit_grade"], "B级（基本适配）")

    def test_missing_evidence_never_deducts(self):
        """三态·未写明：不扣分不封顶，分数原样保留。"""
        entry, result = self._normalize("必须持有注册会计师证书",
                                        {**_report(), "certificates": ["英语六级"]})

        self.assertEqual(entry["status"], "manual_review")
        self.assertEqual(result["hard_gate_deduction"], 0)
        self.assertEqual(result["final_score"], 88)
        self.assertEqual(result["capability_score"], 88)

    def test_relaxable_jd_only_deducts_without_capping(self):
        """JD 自己写"特别优秀者可放宽" → 只扣分、不封顶。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        entry, result = self._normalize("本科及以上学历，特别优秀者可放宽", report)

        self.assertTrue(entry["cap_exempt"])
        self.assertEqual(result["gate_cap_applied"], None)
        self.assertEqual(result["final_score"], 78)      # 88 - 10，不再封顶 59
        self.assertIn("可放宽", entry["resume_basis"])

    def test_strict_degree_jd_still_caps(self):
        """没有放宽措辞的学历门槛照旧封顶 59。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        entry, result = self._normalize("本科及以上学历", report)

        self.assertFalse(entry.get("cap_exempt"))
        self.assertEqual(result["final_score"], 59)
        self.assertEqual(result["gate_cap_applied"], 59)

    def test_blockers_and_override_payload(self):
        """破格复核的完整 payload：能力分、阻塞项、档位计数。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        _, result = self._normalize("本科及以上学历", report)

        self.assertEqual(result["capability_score"], 88)
        self.assertTrue(result["override_review"])
        self.assertEqual(result["recruitment_recommendation"], "破格复核")
        self.assertEqual(result["gate_cap_applied"], 59)
        blocker = result["gate_blockers"][0]

        self.assertEqual(blocker["tier"], "A")
        self.assertEqual(blocker["category"], "education")
        self.assertEqual(blocker["evidence_label"], "已写明不符合")
        self.assertIn("学历", blocker["basis"])

    def test_normal_candidate_has_no_blockers(self):
        _, result = self._normalize("本科及以上学历", _report(degree="本科", tier="985"))

        self.assertEqual(result["gate_blockers"], [])
        self.assertFalse(result["override_review"])
        self.assertIsNone(result["gate_cap_applied"])


class CapabilityDecouplingTests(unittest.TestCase):
    """能力分解耦：剔除模型对硬门槛的打分，避免同一缺口被罚两次。

    背景：提示词把学历/证书/年限列为评分首要因素 → `hard_requirements` 分项已经吸收了门槛信息；
    服务端再扣分封顶就是第二次降分。解耦后能力分 = 责任重合+技能项目+行业背景+证据加分（满分 75）
    归一到 100，`score_breakdown` 均匀时新旧值相同（22/22/22/12/10 → 88）。
    """

    def _run(self, jd, report, category="education"):
        raw = dict(report)
        raw["requirements_checklist"] = screening_agent._build_requirements_checklist(
            _requirements(jd, category=category), raw, "张三\n简历正文"
        )
        return backend._normalize_hr_analysis(raw, jd, "张三\n简历正文")

    def _report_with(self, breakdown):
        report = _report(degree="大专", tier="专科", school="某某学院")
        report["score_breakdown"] = dict(breakdown)
        return report

    def test_hard_requirements_component_is_excluded(self):
        """模型替门槛扣了 12 分（hard 22→10），能力分不该跟着掉。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 10, "responsibility_overlap": 22,
             "skills_projects": 22, "industry_background": 12, "evidence_bonus": 10}))

        self.assertEqual(result["job_fit_score"], 76)          # 含门槛分项：10+22+22+12+10
        self.assertEqual(result["capability_score"], 88)       # 解耦后：66/75
        self.assertEqual(result["capability_basis"], "ex_gate")

    def test_final_score_is_untouched_by_decoupling(self):
        """解耦只动能力分，不动门槛后分数：76 - 10 = 66 → 封顶 59。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 10, "responsibility_overlap": 22,
             "skills_projects": 22, "industry_background": 12, "evidence_bonus": 10}))

        self.assertEqual(result["hard_gate_deduction"], 10)
        self.assertEqual(result["gate_cap_applied"], 59)
        self.assertEqual(result["final_score"], 59)
        self.assertEqual(result["fit_grade"], "D级（不适配）")
        self.assertEqual(result["fit_tag"], "不匹配")          # 不粉饰：分数与标签照封顶结果
        self.assertTrue(result["override_review"])
        self.assertEqual(result["recruitment_recommendation"], "破格复核")

    def test_weak_experience_scores_lower_than_raw_total(self):
        """经历弱、只是证据齐全：解耦后能力分反而低于模型总分（方向正确）。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 22, "responsibility_overlap": 10,
             "skills_projects": 8, "industry_background": 6, "evidence_bonus": 10}))

        self.assertEqual(result["job_fit_score"], 56)
        self.assertEqual(result["capability_score"], 45)       # 34/75
        self.assertFalse(result["override_review"])

    def test_high_total_without_experience_fit_is_not_flagged(self):
        """修复误触发：总分 80 但责任/技能只有 18/18 → 不标破格。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 25, "responsibility_overlap": 18,
             "skills_projects": 18, "industry_background": 10, "evidence_bonus": 9}))

        self.assertEqual(result["job_fit_score"], 80)
        self.assertEqual(result["capability_score"], 73)       # 55/75
        self.assertEqual(result["final_score"], 59)
        self.assertFalse(result["override_review"])
        self.assertEqual(result["recruitment_recommendation"], "淘汰")

    def test_decoupled_score_now_reaches_the_override_bar(self):
        """修复漏标：旧口径能力分 76 够不到 80，解耦后 88 → 该被看见的人不再沉底。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 10, "responsibility_overlap": 22,
             "skills_projects": 22, "industry_background": 12, "evidence_bonus": 10}))

        self.assertLess(result["job_fit_score"], 80)           # 旧口径（=base）不达标
        self.assertGreaterEqual(result["capability_score"], 80)
        self.assertTrue(result["override_review"])

    def test_single_strong_dimension_is_not_enough(self):
        """责任重合满分但技能项目不达标 → 不标破格（A1 附加条件）。"""
        result = self._run("本科及以上学历", self._report_with(
            {"hard_requirements": 10, "responsibility_overlap": 25,
             "skills_projects": 13, "industry_background": 15, "evidence_bonus": 10}))

        self.assertEqual(result["capability_score"], 84)       # 总分够 80
        self.assertFalse(result["override_review"])            # 但技能项目 13 < 20

    def test_missing_breakdown_falls_back_to_model_total(self):
        """模型没给分项时退回模型总分，且不标破格（无法确认核心维度）。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        report.pop("score_breakdown")
        report["job_fit_score"] = 90
        result = self._run("本科及以上学历", report)

        self.assertEqual(result["capability_score"], 90)
        self.assertEqual(result["capability_basis"], "total")
        self.assertFalse(result["override_review"])

    def test_licensed_certificate_blocks_override(self):
        """法规执业资格不能由能力弥补：能力再高也不给"破格复核"。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        report["certificates"] = ["无注册会计师证书"]
        result = self._run("本科及以上学历，必须持有注册会计师证书", report,
                           category="certificate")

        self.assertEqual(result["capability_score"], 88)
        self.assertFalse(result["override_review"])
        self.assertNotEqual(result["recruitment_recommendation"], "破格复核")
        self.assertEqual(len(result["non_mitigable_blockers"]), 1)
        self.assertIn("注册会计师", result["non_mitigable_blockers"][0]["text"])

    def test_ordinary_certificate_can_be_waived(self):
        """PMP 这类能力证明缺了可以破格（不是执业许可）。"""
        report = _report(degree="大专", tier="专科", school="某某学院")
        report["certificates"] = ["无PMP证书"]
        # 学历 + 证书双缺口：学历 A 档封顶 59，证书 B 档
        raw = dict(report)
        raw["requirements_checklist"] = screening_agent._build_requirements_checklist(
            _requirements("本科及以上学历"), raw, "张三")
        cert_entry = screening_agent._build_requirements_checklist(
            _requirements("必须持有PMP证书", category="certificate"), raw, "张三")
        raw["requirements_checklist"] = raw["requirements_checklist"] + cert_entry
        result = backend._normalize_hr_analysis(raw, "本科及以上学历", "张三")

        self.assertTrue(cert_entry[0]["status"] == "not_met")
        self.assertEqual(result["non_mitigable_blockers"], [])
        self.assertTrue(result["override_review"])

    def test_gate_mitigability_rules(self):
        """可弥补性判定表：执业资格/驾照/届别 不破格；学历/院校/年限/语言 可破格。"""
        cases = [
            ({"category": "education", "tier": "A", "text": "本科及以上学历"}, True),
            ({"category": "education", "tier": "B", "text": "985院校本科"}, True),
            ({"category": "experience", "tier": "B", "text": "5年以上经验"}, True),
            ({"category": "certificate", "tier": "B", "text": "必须持有注册会计师证书"}, False),
            ({"category": "certificate", "tier": "B", "text": "必须持有一级建造师证书"}, False),
            ({"category": "certificate", "tier": "B", "text": "必须持有执业医师资格"}, False),
            ({"category": "certificate", "tier": "B", "text": "必须持有PMP证书"}, True),
            ({"category": "certificate", "tier": "B", "text": "必须持有CISP证书"}, True),
            ({"category": "certificate", "tier": "C", "text": "必须持有英语六级证书"}, True),
            ({"category": "certificate", "tier": "C", "text": "必须持有C1驾照"}, False),
            ({"category": "other", "tier": "C", "text": "仅限应届毕业生"}, False),
            # "毕业生"是宽词：`985院校毕业生` 仍是院校门槛（可由能力弥补），不得误判
            ({"category": "education", "tier": "B", "text": "985院校毕业生"}, True),
        ]
        for blocker, expected in cases:
            with self.subTest(text=blocker["text"]):
                self.assertEqual(backend._gate_is_mitigable(blocker), expected)


if __name__ == "__main__":
    unittest.main()
