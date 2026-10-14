"""简历文案核验（deception_check）的确定性行为与防误报边界测试。

每个规则都配“反例”：反例比正例更重要——防误报是这个机制的生命线。
"""
import logging
import unittest
from unittest.mock import patch

import app as backend
import deception_check


class MetricClaimTests(unittest.TestCase):
    """量化成果必须能在原文中定位，否则提示人工核实。"""

    RESUME = "张三\n2019.03-2022.06 深圳市某科技有限公司 后端工程师\n负责订单系统重构与数据库优化。\n"

    def test_flags_metric_missing_from_resume(self):
        analysis = {"strengths": ["将接口响应时间降低60%，支撑大促稳定运行。"]}

        result = deception_check.check_resume_deception(resume_content=self.RESUME, analysis=analysis)

        codes = [item["code"] for item in result["findings"]]
        self.assertIn("metric_not_in_resume", codes)
        finding = next(item for item in result["findings"] if item["code"] == "metric_not_in_resume")
        self.assertEqual(finding["level"], "red_flag")
        self.assertIn("60%", finding["detail"])

    def test_accepts_metric_present_in_resume(self):
        resume = self.RESUME + "接口响应时间降低60%。\n"
        analysis = {"strengths": ["将接口响应时间降低60%。"]}

        result = deception_check.check_resume_deception(resume_content=resume, analysis=analysis)

        self.assertNotIn("metric_not_in_resume", [item["code"] for item in result["findings"]])

    def test_ignores_derived_duration_units(self):
        """年限是服务端按日期推导的，原文不出现“8年”属正常，绝不能报。"""
        analysis = {"work_history": {"total_years": "8年"}, "summary": "具备8年相关经验。"}

        result = deception_check.check_resume_deception(resume_content=self.RESUME, analysis=analysis)

        self.assertNotIn("metric_not_in_resume", [item["code"] for item in result["findings"]])

    def test_ignores_small_counts(self):
        analysis = {"strengths": ["完成7个版本迭代。"]}

        result = deception_check.check_resume_deception(resume_content=self.RESUME, analysis=analysis)

        self.assertNotIn("metric_not_in_resume", [item["code"] for item in result["findings"]])


class TenureSumTests(unittest.TestCase):
    """总年限与分段经历合计对账（重叠区间先合并，年份精度给容差）。"""

    RECORDS = [
        {"company_name": "甲公司", "job_title": "工程师", "start_date": "2019.03", "end_date": "2022.06"},
    ]

    def test_flags_claimed_years_far_above_records(self):
        analysis = {"work_history": {"total_years": "8年", "employment_records": self.RECORDS}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertIn("tenure_sum_mismatch", [item["code"] for item in result["findings"]])

    def test_accepts_consistent_years(self):
        analysis = {"work_history": {"total_years": "3年", "employment_records": self.RECORDS}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("tenure_sum_mismatch", [item["code"] for item in result["findings"]])

    def test_year_only_labels_get_tolerance(self):
        records = [{"company_name": "甲公司", "job_title": "工程师", "start_date": "2019", "end_date": "2022"}]
        analysis = {"work_history": {"total_years": "4年", "employment_records": records}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("tenure_sum_mismatch", [item["code"] for item in result["findings"]])

    def test_skips_when_records_missing(self):
        analysis = {"work_history": {"total_years": "8年"}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("tenure_sum_mismatch", [item["code"] for item in result["findings"]])


class StabilityConflictTests(unittest.TestCase):
    """稳定性是模型主观标签，与结构算出的在职时长对账。"""

    SHORT = [
        {"company_name": "甲公司", "start_date": "2020.01", "end_date": "2020.06"},
        {"company_name": "乙公司", "start_date": "2021.01", "end_date": "2021.06"},
        {"company_name": "丙公司", "start_date": "2022.01", "end_date": "2022.06"},
    ]
    STABLE = [
        {"company_name": "甲公司", "start_date": "2015.01", "end_date": "2018.01"},
        {"company_name": "乙公司", "start_date": "2018.02", "end_date": "2021.02"},
    ]

    def test_flags_stable_label_with_short_tenures(self):
        analysis = {"work_history": {"stability": "稳定", "employment_records": self.SHORT}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertIn("stability_conflict", [item["code"] for item in result["findings"]])

    def test_accepts_stable_label_with_long_tenures(self):
        analysis = {"work_history": {"stability": "稳定", "employment_records": self.STABLE}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("stability_conflict", [item["code"] for item in result["findings"]])

    def test_ignores_non_stable_label(self):
        analysis = {"work_history": {"stability": "偏弱", "employment_records": self.SHORT}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("stability_conflict", [item["code"] for item in result["findings"]])


class ManagementClaimTests(unittest.TestCase):
    """管理职级声明需要带人规模依据。"""

    def test_flags_management_without_team_evidence(self):
        analysis = {"work_history": {"seniority": "总监", "team_size": "未提供"}}

        result = deception_check.check_resume_deception(
            resume_content="负责产品规划与需求评审，参与跨部门沟通。", analysis=analysis
        )

        self.assertIn("management_claim_without_team", [item["code"] for item in result["findings"]])

    def test_accepts_when_team_evidence_present(self):
        analysis = {"work_history": {"seniority": "总监", "team_size": "未提供"}}

        result = deception_check.check_resume_deception(
            resume_content="带领12人研发团队，管理3个小组。", analysis=analysis
        )

        self.assertNotIn("management_claim_without_team", [item["code"] for item in result["findings"]])

    def test_ignores_when_team_size_provided(self):
        analysis = {"work_history": {"seniority": "总监", "team_size": "12人"}}

        result = deception_check.check_resume_deception(resume_content="负责产品规划。", analysis=analysis)

        self.assertNotIn("management_claim_without_team", [item["code"] for item in result["findings"]])


class SkillEvidenceTests(unittest.TestCase):
    """报告标“符合要求”的技能，其证据必须能在原文定位。"""

    def test_flags_unsupported_compliant_skill(self):
        analysis = {"skill_match": {"hard_skills": ["[符合要求] Kubernetes：负责容器编排平台建设与灰度发布"]}}
        resume = "张三\n熟悉 Python 与 MySQL，参与订单系统开发。"

        result = deception_check.check_resume_deception(resume_content=resume, analysis=analysis)

        self.assertIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])

    def test_accepts_evidence_present_in_resume(self):
        analysis = {"skill_match": {"hard_skills": ["[符合要求] Kubernetes：负责 Kubernetes集群运维"]}}
        resume = "张三\n负责 Kubernetes集群运维与容器编排平台建设。"

        result = deception_check.check_resume_deception(resume_content=resume, analysis=analysis)

        self.assertNotIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])

    def test_ignores_not_mentioned_label(self):
        analysis = {"skill_match": {"hard_skills": ["[简历未体现] Kubernetes：未提供"]}}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])


class JdCopyTests(unittest.TestCase):
    """简历与 JD 文本逐字重合（疑似把 JD 粘进简历）。"""

    JD = (
        "岗位职责：负责公司核心交易系统的后端架构设计与开发，主导微服务拆分与高并发性能优化；"
        "参与技术方案评审，推动代码质量与稳定性治理；负责线上故障排查与容量规划，"
        "保障系统在大促期间的稳定运行；负责核心链路的技术选型与难点攻关，"
        "推动研发流程规范化并沉淀技术文档；参与团队技术分享与新人培养，持续改进研发效能。"
        "任职要求：本科及以上学历，五年以上后端开发经验，精通 Java 与分布式中间件，"
        "熟悉 MySQL 调优与缓存架构，具备良好的沟通协作能力与问题定位能力，"
        "熟悉容器化部署与监控告警体系，能够独立完成系统容量评估。"
    )

    UNRELATED_RESUME = (
        "个人经历：担任幼儿园主班教师，负责班级日常教学活动的设计与组织，"
        "与家长保持定期沟通并记录幼儿成长档案；参与园内公开课评比并获得优秀奖，"
        "协助园所完成春季招生宣传与亲子活动策划，累计服务家庭约一百二十户；"
        "每学期组织两次家长开放日，配合保健医生完成晨检与传染病预防宣传，"
        "连续三年获评园级优秀教师；负责新入职教师的带教与班级环境创设，"
        "参与园本课程资源的整理与归档；每周撰写教学反思与幼儿观察记录，"
        "参与区级教研活动并承担示范课展示任务；配合园所完成安全演练与卫生检查。"
    )

    def test_flags_verbatim_copy(self):
        result = deception_check.check_resume_deception(resume_content=self.JD, job_content=self.JD)

        self.assertIn("resume_jd_overlap", [item["code"] for item in result["findings"]])

    def test_ignores_unrelated_resume(self):
        result = deception_check.check_resume_deception(
            resume_content=self.UNRELATED_RESUME, job_content=self.JD
        )

        self.assertNotIn("resume_jd_overlap", [item["code"] for item in result["findings"]])

    def test_ignores_short_texts(self):
        result = deception_check.check_resume_deception(resume_content="很短", job_content="也很短")

        self.assertNotIn("resume_jd_overlap", [item["code"] for item in result["findings"]])


class UnfalsifiablePraiseTests(unittest.TestCase):
    """成段空泛话术（有评价词、无任何数字）。"""

    def test_flags_multiple_unquantified_praise(self):
        analysis = {
            "summary": "综合素质优秀。",
            "strengths": ["具备丰富的项目管理经验。", "沟通能力出色，团队协作良好。", "责任心强，抗压能力好。"],
        }

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertIn("unfalsifiable_praise", [item["code"] for item in result["findings"]])

    def test_accepts_quantified_items(self):
        analysis = {
            "summary": "带团队完成3个项目交付。",
            "strengths": ["将转化率提升30%。", "服务12万用户。"],
        }

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        self.assertNotIn("unfalsifiable_praise", [item["code"] for item in result["findings"]])


class DateParsingTests(unittest.TestCase):
    """日期标签解析（对账规则的公共地基）。"""

    def test_month_precision(self):
        self.assertEqual(deception_check._month_index("2019.03", 0)[0], 2019 * 12 + 2)
        self.assertEqual(deception_check._month_index("2019-03", 0)[0], 2019 * 12 + 2)
        self.assertEqual(deception_check._month_index("2019年3月", 0)[0], 2019 * 12 + 2)

    def test_year_only_flag(self):
        self.assertEqual(deception_check._month_index("2020", 0), (2020 * 12, False))

    def test_present_label_uses_now(self):
        self.assertEqual(deception_check._month_index("至今", 1000), (1000, True))

    def test_missing_labels(self):
        self.assertIsNone(deception_check._month_index("未提供", 0))
        self.assertIsNone(deception_check._month_index("", 0))


class ChecklistContradictionTests(unittest.TestCase):
    """规则 12：清单说"符合"的这一条，与报告正文的否定表述是否在说同一件事。

    口径两条（本轮收窄，见 deception_check 规则 12 注释）：
      ① 关键点必须出现在清单**自己声称的依据**（resume_basis）里；
      ② 单字否定词要先过同形词（无线/非常/未来…）。
    真实数据上的误报（A 类）与真矛盾（B 类）都要有用例锁住。
    """

    RESUME = "张三\n2016.09-至今 某公司 软件实施工程师\n负责办公电脑巡检与数据库备份。\n"

    def _check(self, analysis):
        return deception_check.check_resume_deception(
            resume_content=self.RESUME, analysis=analysis
        )

    def _finding(self, result):
        return next(
            (item for item in result["findings"] if item["code"] == "claim_contradicts_report"), None
        )

    # ── B 类：真矛盾（清单凭某个关键点判符合，报告却说没有这个关键点）──
    def test_flags_met_item_negated_in_summary(self):
        """清单依据里就引了 RFID，报告却写"未体现RFID经验"——这是真矛盾。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "熟练掌握RFID、一二维码等IOT设备的工作原理",
                    "status": "met",
                    "category": "skill",
                    "resume_basis": "负责产线RFID工位机部署与一二维码标签打印",
                }
            ],
            "summary": "未体现RFID相关项目经验。",
        }

        finding = self._finding(self._check(analysis))

        self.assertIsNotNone(finding)
        self.assertEqual(finding["level"], "review")
        self.assertEqual(finding["source"], "report")
        self.assertIn("rfid", finding["evidence"].lower())
        # 证据里必须同时给出清单依据，人工才能一眼判断该信哪边
        self.assertIn("清单依据", finding["evidence"])

    def test_negation_in_list_fields_is_caught(self):
        """否定表述写在短板/风险里同样算。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "熟练掌握RFID、一二维码等IOT设备的工作原理",
                    "status": "met",
                    "resume_basis": "RFID门禁与PDA设备日常维护",
                }
            ],
            "weaknesses": ["未体现RFID相关项目经验"],
        }

        self.assertIsNotNone(self._finding(self._check(analysis)))

    def test_one_item_reports_once(self):
        """一条要求只报一次（实测里同一句会命中多个关键点）。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "3年以上软硬件现场实施或IOT项目落地经验",
                    "status": "met",
                    "resume_basis": "软硬件现场实施经历，含IOT项目落地经验",
                }
            ],
            "summary": "无3年以上软硬件现场实施或IOT项目落地经验。",
        }

        result = self._check(analysis)
        codes = [item["code"] for item in result["findings"] if item["code"] == "claim_contradicts_report"]

        self.assertEqual(len(codes), 1)

    # ── A 类：真实数据上的误报（口径修正的回归锁）──
    def test_years_gate_basis_does_not_fire(self):
        """真实金利源原形：清单按**年限**判符合（依据里没有 iot），报告否定的是**领域**。

        两处在数不同维度，不是矛盾。旧口径拿"要求原文里的词"去比，于是误报。
        """
        analysis = {
            "requirements_checklist": [
                {
                    "text": "3年以上软硬件现场实施或IOT项目落地经验",
                    "status": "met",
                    "category": "experience",
                    "resume_basis": "简历年限10年，满足岗位3年要求",
                }
            ],
            "summary": "10年经验均为办公IT运维，无IOT/一箱一码现场部署经验。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_years_gate_basis_does_not_fire_when_summary_negates_whole_requirement(self):
        """真实汪林军原形：报告把整条要求写成否定，但清单依据仍然只是年限。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "3年以上软硬件现场实施或IOT项目落地经验",
                    "status": "met",
                    "category": "experience",
                    "resume_basis": "简历年限7年，满足岗位3年要求",
                }
            ],
            "summary": "7年经验均为机械结构/设备设计，无3年以上软硬件现场实施或IOT项目落地经验。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_item_without_basis_does_not_fire(self):
        """没有依据就无法判断"是不是同一件事"——宁可漏报，不误报。"""
        analysis = {
            "requirements_checklist": [
                {"text": "熟练掌握RFID、一二维码等IOT设备的工作原理", "status": "met"}
            ],
            "summary": "无RFID相关经验。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_other_clause_negation_does_not_fire(self):
        """反例：否定词在别的分句里，不能算到这条要求头上。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "本科及以上学历",
                    "status": "met",
                    "resume_basis": "本科及以上学历，学信网可查",
                }
            ],
            "summary": "本科学历达标，但无IOT现场经验。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_generic_word_negation_does_not_fire(self):
        """反例：只共用一个通用词时不得报警。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "负责现场作业SOP手册的输出",
                    "status": "met",
                    "resume_basis": "负责输出SOP手册",
                }
            ],
            "summary": "无独立负责项目的经历。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    # ── 约束④：同形词不是否定 ──
    def test_wireless_is_not_negation(self):
        """「无线通信」里的"无"不是否定词——IOT 岗位的简历里高频出现。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "熟悉无线通信模块的调试",
                    "status": "met",
                    "resume_basis": "熟悉无线通信模块的调试与信号测试",
                }
            ],
            "summary": "无线通信模块调试经验丰富，现场适应快。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_very_negative_phrase_is_not_negation(self):
        """「非常熟悉」里的"非"不是否定词。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "熟悉无线通信模块的调试",
                    "status": "met",
                    "resume_basis": "熟悉无线通信模块的调试与信号测试",
                }
            ],
            "summary": "非常熟悉无线通信模块调试，未来可独立出差。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_not_met_item_is_ignored(self):
        """反例：清单自己就判了不符合，正文说"无"是自洽的。"""
        analysis = {
            "requirements_checklist": [
                {
                    "text": "熟练掌握RFID、一二维码等IOT设备的工作原理",
                    "status": "not_met",
                    "resume_basis": "",
                }
            ],
            "summary": "无RFID相关经验。",
        }

        self.assertIsNone(self._finding(self._check(analysis)))

    def test_missing_checklist_does_not_raise(self):
        result = self._check({"summary": "无IOT经验。"})

        self.assertEqual([item for item in result["findings"] if item["code"] == "claim_contradicts_report"], [])


class RobustnessTests(unittest.TestCase):
    """任何输入都不得抛异常，且结论数量有上限。"""

    def test_empty_resume_disables_check(self):
        result = deception_check.check_resume_deception(resume_content="")

        self.assertFalse(result["enabled"])
        self.assertEqual(result["findings"], [])

    def test_none_analysis_does_not_raise(self):
        result = deception_check.check_resume_deception(resume_content="张三", analysis=None)

        self.assertIn("findings", result)

    def test_broken_analysis_types_do_not_raise(self):
        analysis = {"strengths": 123, "work_history": "not-a-dict", "skill_match": {"hard_skills": [1, 2]}}

        result = deception_check.check_resume_deception(resume_content="张三", analysis=analysis)

        self.assertIsInstance(result["findings"], list)

    def test_one_broken_rule_does_not_block_others(self):
        """单条规则抛异常时，其余规则必须照常出结论（否则一条坏规则会静默吃掉整套核验）。"""
        resume = "张三\n2018 年 6 月毕业于某大学\n2015.07-2017.06 甲公司 工程师\n"

        with patch.object(deception_check, "_check_raw_metric_baseline", side_effect=RuntimeError("boom")):
            result = deception_check.check_resume_deception(resume_content=resume)

        self.assertEqual(result["checks_run"], 12)  # 13 条里挂了 1 条
        self.assertIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_findings_are_capped(self):
        analysis = {
            "strengths": [f"提升{i * 10}%的效率。" for i in range(1, 8)],
            "skill_match": {"hard_skills": [f"[符合要求] 工具{i}：负责某平台建设" for i in range(5)]},
        }

        result = deception_check.check_resume_deception(resume_content="张三是候选人", analysis=analysis)

        self.assertLessEqual(len(result["findings"]), 12)

    def test_findings_are_level_sorted(self):
        analysis = {
            "strengths": ["降低90%成本。"],
            "work_history": {"stability": "稳定"},
        }

        result = deception_check.check_resume_deception(resume_content="张三是候选人", analysis=analysis)

        levels = [item["level"] for item in result["findings"]]
        self.assertEqual(levels, sorted(levels, key=lambda level: {"red_flag": 0, "review": 1, "info": 2}[level]))


class RawTenureTests(unittest.TestCase):
    """原文侧：自述年限 vs 原文日期区间合计（完全不依赖 LLM 报告）。"""

    def test_flags_self_claimed_years_above_raw_periods(self):
        resume = "张三\n8 年工作经验\n2019.03-2022.06 甲公司 工程师\n负责订单系统。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_tenure_mismatch", [item["code"] for item in result["findings"]])

    def test_accepts_consistent_self_claim(self):
        resume = "张三\n3 年工作经验\n2019.03-2022.06 甲公司 工程师\n负责订单系统。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_tenure_mismatch", [item["code"] for item in result["findings"]])

    def test_excludes_internship_periods(self):
        """实习段不计入全职合计，因此自述年限不应据此被判矛盾。"""
        resume = "张三\n3 年工作经验\n2016.07-2017.06 某公司 实习生\n2019.03-2022.06 甲公司 工程师\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_tenure_mismatch", [item["code"] for item in result["findings"]])

    def test_skips_without_periods(self):
        resume = "张三\n8 年工作经验\n负责订单系统开发与维护。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_tenure_mismatch", [item["code"] for item in result["findings"]])


class RawGraduationTests(unittest.TestCase):
    """原文侧：最早全职工作早于最早毕业年份。"""

    def test_flags_work_starting_before_graduation(self):
        resume = "张三\n2018 年 6 月毕业于某大学\n2015.07-2017.06 甲公司 工程师\n负责订单系统。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_accepts_work_after_graduation(self):
        resume = "张三\n2018 年 6 月毕业于某大学\n2018.07-2021.06 甲公司 工程师\n负责订单系统。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_accepts_internship_before_graduation(self):
        resume = "张三\n2018 年 6 月毕业于某大学\n2017.01-2017.06 甲公司 实习生\n负责订单系统。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_uses_earliest_graduation_year(self):
        """本科毕业 2015、硕士毕业 2019：2016-2018 的工作是正常的，不得报。"""
        resume = "张三\n2015 年 6 月本科毕业\n2019 年 6 月硕士毕业\n2016.07-2018.06 甲公司 工程师\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_ignores_secondary_school_graduation(self):
        """只列了高中毕业时，不得把中学毕业当成全职工作起点。"""
        resume = "张三\n1998 年高中毕业\n1996.09-1998.06 某工厂 操作工\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])


class RawMetricBaselineTests(unittest.TestCase):
    """原文侧：百分比/倍数声明缺少基线（聚合阈值 3 处）。"""

    def test_flags_multiple_unbaselined_metrics(self):
        resume = (
            "张三\n负责订单系统，性能提升60%。\n"
            "优化缓存策略，响应时间降低40%。\n"
            "重构支付链路，成功率提升25%。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_accepts_baselined_metrics(self):
        resume = (
            "张三\n性能从 200ms 优化到 80ms，提升60%。\n"
            "相比去年同期，响应时间降低40%。\n"
            "重构支付链路，成功率提升25%。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_ignores_single_occurrence(self):
        resume = "张三\n负责订单系统，性能提升60%。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])


class RawDegreeWordTests(unittest.TestCase):
    """原文侧：用程度词代替数字的性能描述（聚合阈值 3 处）。"""

    def test_flags_repeated_degree_words(self):
        resume = "张三\n大幅提升系统性能。\n显著降低接口耗时。\n成倍提升订单处理效率。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_degree_word_instead_of_number", [item["code"] for item in result["findings"]])

    def test_ignores_when_numbers_present(self):
        resume = "张三\n大幅提升系统性能，QPS 从 800 提升到 2000。\n显著降低接口耗时 60%。\n成倍提升订单处理能力 3 倍。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_degree_word_instead_of_number", [item["code"] for item in result["findings"]])

    def test_ignores_degree_words_without_performance_noun(self):
        resume = "张三\n大幅调整排版。\n显著改变配色。\n成倍增加字号。\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_degree_word_instead_of_number", [item["code"] for item in result["findings"]])


class SourceTagTests(unittest.TestCase):
    """两侧结论必须可区分，否则影子期无法分账调阈值。"""

    def test_raw_finding_is_tagged_resume(self):
        resume = "张三\n2018 年 6 月毕业于某大学\n2015.07-2017.06 甲公司 工程师\n"

        result = deception_check.check_resume_deception(resume_content=resume)

        finding = next(item for item in result["findings"] if item["code"] == "raw_work_before_graduation")
        self.assertEqual(finding["source"], "resume")
        self.assertEqual(result["by_source"]["resume"], 1)

    def test_report_finding_is_tagged_report(self):
        analysis = {"strengths": ["将接口响应时间降低60%。"]}

        result = deception_check.check_resume_deception(resume_content="张三的简历正文", analysis=analysis)

        finding = next(item for item in result["findings"] if item["code"] == "metric_not_in_resume")
        self.assertEqual(finding["source"], "report")
        self.assertEqual(result["by_source"]["report"], 1)


class WiringTests(unittest.TestCase):
    """接入正确性 + 评分零影响（机制性锁：谁把核验接进扣分，这里立刻红）。"""

    RAW = {"job_fit_score": 80, "ai_risk": "none", "score_breakdown": {
        "hard_requirements": 22, "responsibility_overlap": 22, "skills_projects": 22,
        "industry_background": 10, "evidence_bonus": 4,
    }}

    HOSTILE = {
        "version": "test",
        "enabled": True,
        "checks_run": 13,
        "findings": [{"code": "resume_jd_overlap", "level": "red_flag", "title": "t", "detail": "d", "evidence": "e", "ask": "a", "source": "report"}],
        "counts": {"red_flag": 1, "review": 0, "info": 0},
        "by_source": {"resume": 0, "report": 1},
        "notice": "",
    }

    def test_credibility_attached_when_mode_on(self):
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "on"):
            result = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")

        self.assertEqual(result["credibility"]["version"], deception_check.CREDIBILITY_VERSION)

    def test_shadow_mode_hides_field_from_payload(self):
        """影子模式照常计算，但绝不把结论返回给前端（否则就不叫影子了）。"""
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "shadow"), \
                patch.object(backend.deception_check, "check_resume_deception", return_value=self.HOSTILE) as spy:
            result = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")

        self.assertEqual(spy.call_count, 1)
        self.assertNotIn("credibility", result)

    def test_findings_never_affect_score(self):
        """核验结论（含 red_flag）不得改变任何评分字段。"""
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "on"), \
                patch.object(backend.deception_check, "check_resume_deception", return_value=self.HOSTILE):
            hostile = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "on"), \
                patch.object(backend.deception_check, "check_resume_deception",
                             return_value={**self.HOSTILE, "findings": [], "counts": {"red_flag": 0, "review": 0, "info": 0}}):
            clean = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")

        for field in ("final_score", "job_fit_score", "score_breakdown", "hard_gate_deduction",
                      "requirements_checklist", "recruitment_recommendation", "fit_tag", "ai_deduction"):
            self.assertEqual(hostile[field], clean[field], field)
        self.assertEqual(hostile["credibility"], self.HOSTILE)

    def test_mode_off_skips_check(self):
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "off"), \
                patch.object(backend.deception_check, "check_resume_deception") as spy:
            result = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")

        self.assertEqual(spy.call_count, 0)
        self.assertNotIn("credibility", result)

    def test_check_failure_does_not_break_analysis(self):
        with patch.object(backend.config, "DECEPTION_CHECK_MODE", "on"), \
                patch.object(backend.deception_check, "check_resume_deception", side_effect=RuntimeError("boom")):
            result = backend._normalize_hr_analysis(dict(self.RAW), "招聘后端工程师", "某公司的简历正文")

        self.assertEqual(result["final_score"], 80)
        self.assertNotIn("credibility", result)


class ShadowLogAggregationTests(unittest.TestCase):
    """影子日志要能被 grep/awk 直接算命中率：分子、分母、规则码、版本、启用状态。"""

    FINDINGS = {
        "version": "deception-check-v1",
        "enabled": True,
        "checks_run": 11,
        "findings": [
            {"code": "raw_tenure_mismatch", "level": "red_flag"},
            {"code": "unstable_claim", "level": "review"},
        ],
        "counts": {"red_flag": 1, "review": 1, "info": 0},
        "by_source": {"resume": 1, "report": 1},
    }
    CONTEXT = {"resume_id": "r-secret", "job_id": "j-secret", "user_id": "u-secret"}

    def _capture(self, credibility, context=None):
        lines: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        logger = logging.getLogger("resume-matcher")
        handler = _Capture()
        logger.addHandler(handler)
        try:
            backend._log_credibility_shadow(credibility, context)
        finally:
            logger.removeHandler(handler)
        return lines

    def test_line_carries_every_aggregation_field(self):
        line = self._capture(self.FINDINGS, self.CONTEXT)[0]

        for marker in (
            "credibility(mode=shadow)", "version=deception-check-v1", "enabled=true",
            "ref=", "checks=11", "findings=2", "red=1", "review=1", "info=0",
            "resume_side=1", "report_side=1", "codes=raw_tenure_mismatch,unstable_claim",
        ):
            self.assertIn(marker, line)

    def test_candidate_ids_never_land_in_the_log(self):
        line = self._capture(self.FINDINGS, self.CONTEXT)[0]

        for leaked in ("r-secret", "j-secret", "u-secret"):
            self.assertNotIn(leaked, line)

    def test_same_resume_yields_a_stable_correlation_code(self):
        first = self._capture(self.FINDINGS, self.CONTEXT)[0]

        self.assertIn("ref=" + backend._shadow_correlation_key(self.CONTEXT), first)

    def test_disabled_run_is_marked(self):
        """简历原文为空时一条规则都没跑，分母必须能把它排除掉。"""
        line = self._capture({**self.FINDINGS, "enabled": False, "checks_run": 0, "findings": []})[0]

        self.assertIn("enabled=false", line)
        self.assertIn("checks=0", line)
        self.assertIn("codes=-", line)


class RawEducationTimelineTests(unittest.TestCase):
    """第十二轮：教育经历不能当成任职区间，毕业年份要能从教育段里抽出来。

    这两条都是真数据暴露的：
      · 汪林军真简历的教育段是「教育经历 / 华南农业大学 本科 … / 2015-2019」——年份**另起一行**；
      · 修好年份抽取后立刻出现假阳性：`2015-2019` 被 `_extract_raw_periods` 当成了一段工作，
        于是"最早工作早于毕业年份"必然命中（金利源的 `2012-2016` 同理）。
    """

    RESUME = (
        "汪某\n"
        "教育经历\n"
        "华南农业大学 本科 农业机械化及其自动化\n"
        "2015-2019\n"
        "工作经历\n"
        "2019.07-2020.08 甲公司 机械工程师\n"
        "2021.01-2023.05 乙公司 机械工程师\n"
    )

    def test_education_year_range_is_not_treated_as_employment(self):
        result = deception_check.check_resume_deception(resume_content=self.RESUME)

        self.assertNotIn("raw_work_before_graduation", [item["code"] for item in result["findings"]])

    def test_graduation_year_is_extracted_from_education_block(self):
        years = deception_check._extract_graduation_years(self.RESUME)

        self.assertIn(2019, years)
        self.assertNotIn(2015, years)  # 区间取后一个年份（入学年份不是毕业年份）

    def test_education_span_does_not_pad_the_tenure_total(self):
        """自述年限与经历对不上时必须还报得出来——在校年限不能拿来补齐。"""
        resume = (
            "汪某\n"
            "7 年工作经验\n"
            "教育经历\n"
            "某大学 本科 机械设计\n"
            "2015-2019\n"
            "工作经历\n"
            "2023.06-至今 甲公司 机械工程师\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_tenure_mismatch", [item["code"] for item in result["findings"]])

    def test_same_line_education_range_still_extracts(self):
        years = deception_check._extract_graduation_years("李某\n2011-2014 芜湖职业技术学院 大专\n")

        self.assertEqual(years, [2014])

    def test_year_line_with_extra_text_is_not_a_graduation_year(self):
        """教育行后面那行必须"只有日期"，否则不能当毕业年份（避免把工作行读成学历）。"""
        years = deception_check._extract_graduation_years("李某\n某大学 本科\n2019.07-2020.08 甲公司 工程师\n")

        self.assertEqual(years, [])


class RawMetricBaselineTuningTests(unittest.TestCase):
    """第十二轮：量化声明识别扩到真实写法（变化动词 + 带单位数字），并做邻近判断。"""

    def test_change_verb_with_unit_without_baseline_fires(self):
        resume = (
            "张三\n优化产线布局，缩短换型时间15天。\n"
            "改进装配工装，降低返修工时8小时。\n"
            "调整物流路线，减少搬运距离200米。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_change_verb_with_baseline_is_not_flagged(self):
        resume = (
            "张三\n换型时间由 45 分钟缩短到 30 分钟。\n"
            "返修工时从 12 小时降低到 4 小时。\n"
            "搬运距离相比去年减少 200 米。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_year_in_sentence_does_not_count_as_baseline(self):
        """「2021 年主导产线改造，效率提升 30%」里的 2021 是年份，不是对照值。"""
        resume = (
            "张三\n2021年主导产线改造，效率提升30%。\n"
            "2022年重构缓存策略，响应时间降低40%。\n"
            "2023年优化支付链路，成功率提升25%。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_pay_context_is_never_counted_as_a_metric_claim(self):
        resume = (
            "张三\n期望薪资提升至 15K。\n"
            "期望月薪提高 3K。\n"
            "薪资待遇优化 20%。\n"
        )

        result = deception_check.check_resume_deception(resume_content=resume)

        self.assertNotIn("raw_metric_without_baseline", [item["code"] for item in result["findings"]])

    def test_baseline_cue_must_be_near_the_number(self):
        """「由」这类常见字离得远就不算基线，否则整篇简历会被误判成"都已给基线"。"""
        resume = (
            "张三\n由我负责的模块，效率提升20%。\n"
            "由团队共同推进，成本降低30%。\n"
            "由部门主导立项，交付周期缩短40%。\n"
        )

        self.assertEqual(len(deception_check._collect_unbaselined_metrics(resume)), 3)


class SkillEvidenceTuningTests(unittest.TestCase):
    """第十二轮：报告只给了"行号指针"当证据时，不得据此报"技能证据找不到"。

    真实报告（金利源）写的是「…办公电脑、打印机、会议设备等软硬件日常巡检、故障排查与运维管理：
    简历第 19 行明确负责此类工作。」—— 技能项本身与原文**逐字一致**，只有那句行号指针找不到；
    而模型引用的行号还是它自己那份排版，跟真实原文行号对不上。切 on 之前必须挡住这类假阳性。
    """

    RESUME = (
        "金某\n"
        "1、负责公司办公电脑、打印机、会议设备等软硬件日常巡检、故障排查与运维管理。\n"
        "2、负责服务器及数据库日常监控、定期备份、日志巡检与异常处理。\n"
    )

    def _analysis(self, entry: str) -> dict:
        return {"skill_match": {"hard_skills": [entry]}}

    def test_line_pointer_evidence_does_not_trigger_a_finding(self):
        entry = "[符合要求]办公电脑、打印机、会议设备等软硬件日常巡检、故障排查与运维管理：简历第19行明确负责此类工作。"

        result = deception_check.check_resume_deception(
            resume_content=self.RESUME, analysis=self._analysis(entry)
        )

        self.assertNotIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])

    def test_claim_text_is_used_even_when_evidence_is_unfindable(self):
        entry = "服务器及数据库日常监控、定期备份、日志巡检与异常处理：详见项目经历。"

        result = deception_check.check_resume_deception(
            resume_content=self.RESUME, analysis=self._analysis(entry)
        )

        self.assertNotIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])

    def test_invented_skill_still_triggers_a_finding(self):
        """技能项与证据都定位不到时仍要报——这正是不该放过的情形。"""
        entry = "[符合要求]精通 Kubernetes 集群编排与 Istio 服务网格：主导过千万级流量治理。"

        result = deception_check.check_resume_deception(
            resume_content=self.RESUME, analysis=self._analysis(entry)
        )

        self.assertIn("skill_evidence_not_found", [item["code"] for item in result["findings"]])


class WrappedLineJoinTests(unittest.TestCase):
    """第十四轮（待办 12）：PDF 硬换行把一句话切成两行时，核验前要接回去。

    真实来源：王龙龙那份简历里「…结合业务发」/「展提出系统优化建议。」被切成两截，
    以及「从 320ms」/「优化到 80ms」这类真基线会被拆散 → 有误报风险。
    """

    def test_wrapped_sentence_is_joined(self):
        text = "7×12 小时处理各部门单\n据报错、登录、权限、报表异常。\n2024.9~2026.3\n1、负责系统上线\n"

        joined = deception_check._join_wrapped_lines(text)

        self.assertIn("7×12 小时处理各部门单据报错", joined)  # 半句接回
        self.assertIn("\n2024.9~2026.3", joined)  # 日期行不吞进上一句
        self.assertIn("\n1、负责系统上线", joined)  # 列表序号开头不吞

    def test_joined_text_removes_two_char_fragment(self):
        # 原文：上一行以「…系统」结尾，下一行以「优化；」开头 → 光杆「优化」不该独立成句
        text = "结合业务发展提出系统\n优化；实时与客户沟通协调。"

        joined = deception_check._join_wrapped_lines(text)

        self.assertIn("结合业务发展提出系统优化；实时与客户沟通协调。", joined)
        sentence = deception_check._sentence_at(joined, joined.index("优化"))
        self.assertTrue(sentence.startswith("结合业务发展"))  # 碎片已经不存在

    def test_wrapped_true_baseline_is_not_reported(self):
        """真基线被换行拆开时不能报——这是这条修复的精度意义所在。"""
        wrapped = "系统响应时间从 320ms\n优化到 80ms，故障率下降。\n"
        self.assertEqual(deception_check._collect_unbaselined_metrics(wrapped), [])
        self.assertEqual(deception_check._check_raw_metric_baseline(wrapped), [])

    def test_section_header_line_is_not_swallowed(self):
        text = "流程优化与落地：\n基于系统逻辑重构核心业务流程。\n"

        joined = deception_check._join_wrapped_lines(text)

        self.assertIn("\n基于系统逻辑重构核心业务流程。", joined)


class ReportTimelineOverlapTests(unittest.TestCase):
    """第十四轮（待办 11）：报告称"时间线重叠"但日期区间零重叠 → 属于报告侧假话。

    真实来源：王龙龙真报告的风险预警第一条（前半句下结论、后半句自己否掉），
    而原文四段经历 2020.7→2026.3 首尾相接、一处都不重叠。
    """

    RESUME = (
        "王某某\n"
        "2020.7~2022.10 中软国际信息科技（广州）有限公司 实施顾问\n"
        "2022.11~2023.11 广州卓尔信息科技有限公司 实施顾问\n"
        "2023.12~2024.8 奥哲软件有限公司 实施顾问\n"
        "2024.9~2026.3 宁波欧达光电有限公司 内部ERP运维工程师\n"
    )

    @staticmethod
    def _analysis(risk_points):
        return {
            "risk_points": list(risk_points),
            "work_history": {
                "employment_records": [
                    {"company": "中软国际", "start_date": "2020-07", "end_date": "2022-10"},
                    {"company": "广州卓尔", "start_date": "2022-11", "end_date": "2023-11"},
                    {"company": "奥哲软件", "start_date": "2023-12", "end_date": "2024-08"},
                    {"company": "宁波欧达", "start_date": "2024-09", "end_date": "2026-03"},
                ]
            },
        }

    def _findings(self, resume=None, analysis=None):
        result = deception_check.check_resume_deception(
            resume_content=self.RESUME if resume is None else resume,
            analysis=self._analysis(["工作经历时间线存在重叠。"]) if analysis is None else analysis,
        )
        return [item for item in result["findings"] if item["code"] == "report_timeline_overlap_unsupported"]

    def test_real_case_is_caught(self):
        """真报告原文：前半句说重叠、后半句自己说不重叠（首尾相接的四段经历）。"""
        analysis = self._analysis([
            "工作经历时间线存在重叠：宁波欧达光电（2024.09-2026.03）与奥哲软件（2023.12-2024.08）"
            "时间不重叠，但广州卓尔（2022.11-2023.11）与中软国际",
            "总工作年限约6年，但相关岗位年限不足3年",
        ])

        findings = self._findings(analysis=analysis)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["level"], "review")
        self.assertEqual(findings[0]["source"], "report")
        # 说人话：不许写「区间」「n-gram」这类术语，日期写成 HR 一眼能读的样子
        self.assertIn("没有任何两段是重叠的", findings[0]["detail"])
        self.assertIn("2020年7月到2022年10月", findings[0]["detail"])
        self.assertIn("报告自己写错了", findings[0]["detail"])
        self.assertNotIn("区间", findings[0]["title"])

    def test_evidence_marks_the_truncated_model_sentence(self):
        """证据不能甩半句话给 HR：模型这句本身就是断的，必须标出来。"""
        analysis = self._analysis([
            "工作经历时间线存在重叠：宁波欧达光电（2024.09-2026.03）与奥哲软件（2023.12-2024.08）"
            "时间不重叠，但广州卓尔（2022.11-2023.11）与中软国际",
        ])

        evidence = self._findings(analysis=analysis)[0]["evidence"]

        self.assertIn("这句在报告里本身就是断的", evidence)
        self.assertIn("与中软国际", evidence)  # 原文照引，不替模型圆场

    def test_ask_says_do_not_ask_the_candidate_and_gives_a_real_question(self):
        """这条错在报告、不在简历 → 面试该问必须先说"不用问候选人"，再给一句能照着念的话。"""
        analysis = self._analysis(["工作经历时间线存在重叠：广州卓尔（2022.11-2023.11）与中软国际"])

        ask = self._findings(analysis=analysis)[0]["ask"]

        self.assertIn("不用问候选人", ask)
        self.assertIn("空档期", ask)
        self.assertIn("2020年7月", ask)  # 具体到日期，不是"请核实该风险点"这种套话
        self.assertNotIn("请候选人", ask)

    def test_negated_claim_does_not_fire(self):
        for text in ("各段经历时间不重叠。", "工作时间线不存在重叠。", "经历无重叠，衔接正常。"):
            with self.subTest(text=text):
                self.assertEqual(self._findings(analysis=self._analysis([text])), [])

    def test_real_overlap_does_not_fire(self):
        """原文确实重叠时，报告说重叠是对的，不能报。"""
        resume = (
            "2020.7~2022.10 甲公司 工程师\n"
            "2021.3~2023.5 乙公司 工程师\n"
        )
        analysis = {
            "risk_points": ["工作经历时间线存在重叠。"],
            "work_history": {
                "employment_records": [
                    {"company": "甲公司", "start_date": "2020-07", "end_date": "2022-10"},
                    {"company": "乙公司", "start_date": "2021-03", "end_date": "2023-05"},
                ]
            },
        }

        self.assertEqual(self._findings(resume=resume, analysis=analysis), [])

    def test_single_period_does_not_fire(self):
        """只有一段可比经历时，不足以证明"零重叠"，宁可漏报。"""
        resume = "2020.7~2022.10 甲公司 工程师\n"
        analysis = {"risk_points": ["工作经历时间线存在重叠。"], "work_history": {"employment_records": []}}

        self.assertEqual(self._findings(resume=resume, analysis=analysis), [])

    def test_no_claim_does_not_fire(self):
        self.assertEqual(self._findings(analysis=self._analysis(["工作年限不足。"])), [])


class FindingWordingTests(unittest.TestCase):
    """文案纪律（第十四轮补）：面板和导出都是**纯文本**渲染 —— 文案里混进 markdown
    或套话，HR 看到的就是星号和"请候选人说明…"。

    事故来源：规则 13 首版 detail 里写了 `**没有一处重叠**`，面板把它当普通文本原样显示；
    「面试该问」也全是"请候选人说明…"这种描述句，不是能照着念的问法。
    """

    RESUME = (
        "李某某\n"
        "2010.9~2014.6 某某大学 本科 计算机科学与技术\n"
        "2013.3~2013.12 甲软件有限公司 实施顾问\n"
        "2014.1~2014.11 乙科技有限公司 实施工程师\n"
        "2015.2~2016.1 丙信息技术有限公司 项目经理\n"
        "2018.1~2026.1 丁数字技术有限公司 实施总监\n"
        "本人有约 18 年工作经验。\n"
        "负责团队管理与项目交付，沟通协调能力强，工作认真负责。\n"
        "提升交付效率30%，降低实施成本20万元，缩短上线周期15天。\n"
        "熟悉 ERP 系统实施与一箱一码现场部署，精通 Kubernetes 与 Istio。\n"
        "沟通协调能力强，工作认真负责，学习能力强，抗压能力强，执行力强，责任心强。\n"
    )

    ANALYSIS = {
        "summary": "该候选人无 IOT 现场部署经验，团队管理经验未写明，交付效率提升45%，客户满意度达98%。",
        "risk_points": [
            "工作经历时间线存在重叠：甲软件（2013.03-2013.12）与乙科技（2014.01-2014.11）",
            "总工作年限约 20 年，但分段经历合计不足。",
            "沟通能力较强，属于软性优势。",
        ],
        "strengths": [
            "沟通协调能力强",
            "工作认真负责",
            "学习能力强",
            "抗压能力强",
            "执行力强",
            "责任心强",
        ],
        "weaknesses": ["团队管理经验未写明"],
        "work_history": {
            "total_years": "约 20 年",
            "stability": "稳定",
            "seniority": "总监",
            "team_size": "未提供",
            "employment_records": [
                {"company": "甲软件", "start_date": "2013-03", "end_date": "2013-12"},
                {"company": "乙科技", "start_date": "2014-01", "end_date": "2014-11"},
                {"company": "丙信息", "start_date": "2015-02", "end_date": "2016-01"},
                {"company": "丁数字", "start_date": "2018-01", "end_date": "2026-01"},
            ],
        },
        "skill_match": {
            "hard_skills": [
                {"skill": "Kubernetes 集群运维", "status": "符合要求", "evidence": "简历第 19 行明确负责此类工作"},
            ]
        },
        "requirements_checklist": [
            {
                "requirement": "具备 IOT 现场部署经验",
                "status": "met",
                "resume_basis": "简历写明具备 IOT 现场部署经验，年限满足要求",
            }
        ],
    }

    @classmethod
    def _findings(cls):
        result = deception_check.check_resume_deception(
            resume_content=cls.RESUME,
            job_content="熟悉一箱一码现场部署与 IOT 设备调试，具备 ERP 系统实施经验，能接受出差。",
            analysis=cls.ANALYSIS,
        )
        return result["findings"]

    def test_fixture_actually_triggers_enough_rules(self):
        """防止这条测试变成空转（只跑出 1~2 条就没意义了）。"""
        codes = {item["code"] for item in self._findings()}

        self.assertGreaterEqual(len(codes), 6, "夹具触发的规则太少：%s" % sorted(codes))

    def test_no_markdown_markers_leak_to_the_panel(self):
        for item in self._findings():
            for field in ("title", "detail", "evidence", "ask"):
                value = item.get(field, "")
                for marker in ("**", "`", "##", "__", "- ["):
                    self.assertNotIn(
                        marker, value, "字段 %s 里混进了 markdown：%s" % (field, value)
                    )

    def test_every_ask_is_a_usable_interview_line(self):
        """面试该问不许是"请候选人说明…"这种描述句：要么给出能照着念的问法，
        要么明确告诉 HR 这条不用问候选人。"""
        for item in self._findings():
            ask = item["ask"]

            self.assertNotIn("请候选人", ask, "仍是套话：%s" % ask)
            self.assertIn("问", ask, "没有告诉 HR 怎么问：%s" % ask)
            if "不用问候选人" not in ask:
                self.assertTrue(
                    any(mark in ask for mark in ("『", "「", "“")),
                    "给了问法但没把原话包起来，HR 没法照着念：%s" % ask,
                )

    def test_evidence_is_either_quoted_or_labelled(self):
        """证据字段必须交代出处（原文/结构依据/报告条目），不能甩一串裸文本。"""
        for item in self._findings():
            evidence = item["evidence"]

            self.assertTrue(
                any(mark in evidence for mark in ("原文", "依据", "报告条目", "断言", "示例", "清单")),
                "证据没说清出处：%s" % evidence,
            )

    def test_no_english_field_paths_in_our_own_prose(self):
        """我们自己写的话里不许出现 summary / risk_points / employment_records 这类字段路径
        （证据里引用报告原文或简历原文时不受限制，那是原文）。"""
        internal = (
            "summary",
            "risk_points",
            "weaknesses",
            "strengths",
            "employment_records",
            "work_history",
            "skill_match",
            "requirements_checklist",
            "total_years",
            "seniority",
            "team_size",
            "hard_skills",
            "role_specific_assessment",
            "deduction_reasons",
        )
        for item in self._findings():
            for field in ("detail", "ask", "title"):
                value = item[field]
                for name in internal:
                    self.assertNotIn(name, value, "%s 里出现了字段路径 %s：%s" % (field, name, value))


if __name__ == "__main__":
    unittest.main()
