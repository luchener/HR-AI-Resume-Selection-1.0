import io
import os
import tempfile
import unittest
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import app as backend
import llm as llm_module
import prompts as prompts_module
import auth as auth_module
from test_helpers import build_anonymous_resume_docx
from testsupport import isolated_data_dirs


class HrAnalysisTests(unittest.TestCase):
    def setUp(self):
        # 数据目录整体隔离：临时目录 + 结束后原样恢复所有模块级路径。
        # 旧写法（手写赋值 store/auth 的目录常量，tearDown 只 rmtree 不恢复）会把
        # auth.USERS_DIR 永久留在已删除的目录上，导致后续模块 create_user 崩掉；见 testsupport.py。
        self.dirs = isolated_data_dirs("hr_analysis")
        self.addCleanup(self.dirs.restore)
        backend._HR_ANALYSIS_CACHE.clear()
        backend.config.ALLOW_CUSTOM_AI_CONFIG = True
        self.addCleanup(setattr, backend.config, "ALLOW_CUSTOM_AI_CONFIG", False)
        user, _err = auth_module.create_user("tester", "password123")
        self.user_id = user["user_id"]
        self.token = auth_module.generate_jwt(user["user_id"], user["username"])
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = backend.app.test_client()

    def test_resume_and_job_upload_do_not_call_llm(self):
        file_bytes = build_anonymous_resume_docx()

        with patch.object(backend.llm, "call_llm", side_effect=AssertionError("LLM called")):
            upload = self.client.post(
                "/api/v1/resumes/upload",
                data={"file": (io.BytesIO(file_bytes), "anonymous-resume.docx")},
                content_type="multipart/form-data",
                headers=self.headers,
            )
            self.assertEqual(upload.status_code, 200)
            resume_id = upload.get_json()["resume_id"]

            job = self.client.post(
                "/api/v1/jobs/upload",
                json={
                    "resume_id": resume_id,
                    "job_descriptions": ["AI 产品经理\n负责 AI Agent 产品\n要求 5 年产品经验"],
                },
                headers=self.headers,
            )
            self.assertEqual(job.status_code, 200)

    def test_resume_upload_accepts_octet_stream_docx(self):
        response = self.client.post(
            "/api/v1/resumes/upload",
            data={
                "file": (
                    io.BytesIO(build_anonymous_resume_docx()),
                    "anonymous-resume.docx",
                    "application/octet-stream",
                )
            },
            content_type="multipart/form-data",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.get_json()["extracted_characters"], 500)

    def test_hr_analysis_enforces_formula_and_fixed_fields(self):
        resume_id = backend.store.save_resume("Python AI 产品经理，8 年经验", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘 AI 产品经理，要求 5 年经验", {}, self.user_id)
        model_result = {
            "candidate_name": "张三",
            "job_fit_score": 88,
            "ai_risk": "light",
            "ai_deduction": 7,
            "summary": "核心经历匹配，表达略显模板化。",
            "basic_screening": {
                "native_place": "浙江杭州",
                "age": "35",
                "gender": "男",
                "work_location": "上海",
                "salary_expectation": "30K+",
            },
            "education_history": [
                {
                    "degree": "硕士",
                    "school_name": "复旦大学",
                    "school_tier": "985/211",
                    "major": "软件工程",
                    "graduation_year": "2015",
                }
            ],
            "work_history": {"total_years": "8 年", "relevant_years": "6 年", "stability": "稳定"},
            "skill_match": {"hard_skills": ["AI 产品", "Python 熟练"], "project_match_points": ["AI Agent 项目对应岗位任务"], "soft_skills": ["跨部门协作"]},
            "bonus_items": ["量化提升 30%"],
            "strengths": ["8 年 AI 产品经验，超过岗位要求的 5 年门槛。"],
            "weaknesses": ["未提供商业化成果证据。"],
            "risk_points": ["无明显风险"],
            "recruitment_recommendation": "优先面试",
            "fit_tag": "高匹配",
            "deduction_reasons": ["部分表述较为同质化"],
        }

        with patch.object(backend.screening_agent, "run_screening_agent", return_value=model_result) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()["data"]["hr_analysis"]
        # AI 美化默认 readonly：只作风险提示，不扣分（88 分不再被减到 81）
        self.assertEqual(result["final_score"], 88)
        self.assertEqual(result["ai_deduction"], 0)
        self.assertEqual(result["ai_deduction_suggested"], 7)
        self.assertEqual(result["ai_risk"], "light")
        self.assertEqual(result["ai_risk_level"], "轻微AI美化")
        self.assertIn("未扣分", result["ai_risk_note"])
        self.assertEqual(result["fit_grade"], "A级（良好适配）")
        self.assertEqual(result["job_fit_percentage"], 88)
        self.assertEqual(result["recruitment_recommendation"], "优先面试")
        self.assertEqual(result["strengths"], model_result["strengths"])
        self.assertEqual(result["fit_tag"], "高匹配")
        self.assertEqual(result["education_history"][0]["degree"], "硕士")
        self.assertEqual(result["education_history"][0]["school_name"], "复旦大学")
        self.assertEqual(result["education_history"][0]["school_tier"], "985/211")
        self.assertEqual(result["education_history"][0]["major"], "软件工程")
        self.assertEqual(response.get_json()["data"]["candidate_name"], "张三")
        self.assertIn("# Python AI 产品经理", response.get_json()["data"]["studio_markdown"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["current_date"], backend.datetime.now().strftime("%Y-%m"))
        self.assertIn("AI 产品经理", call.call_args.kwargs["job_content"])
        self.assertIn("Python AI 产品经理", call.call_args.kwargs["resume_content"])

    def test_hr_analysis_feeds_sanitized_resume_to_agent(self):
        """AI 分析入口必须吃清洗后的文本：PDF 解析残留不得进入模型。"""
        polluted = (
            "Rj\n\nR\n\nQ\n\nG q n P\n\nY 2 9 W\n\n912 cf79 e4 c4712351 H\n\n"
            "候选人甲\n\n男 | 年龄：32岁 |\n\ncandidate@example.com\n\n个人优势\n\n"
            "1.熟悉常见通讯协议及网络协议，具备良好的技术理解能力\n\n"
            "2.熟练掌握 Linux 命令及系统安装，具备服务器运维经验\n\n"
            "3.熟悉办公设备操作，能够高效完成日常技术支持工作\n\n"
            "4.具备较强的责任心与耐心，适应频繁出差的工作安排"
        )
        resume_id = backend.store.save_resume(polluted, {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘运维工程师，要求 3 年经验", {}, self.user_id)

        with patch.object(backend.screening_agent, "run_screening_agent", return_value={"candidate_name": "候选人甲", "final_score": 70}) as call:
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        sent = call.call_args.kwargs.get("resume_content") or ""
        self.assertIn("候选人甲", sent, "真中文必须原样进入模型")
        self.assertIn("个人优势", sent)
        for junk in ("Rj", "G q n P", "Y 2 9 W", "912 cf79"):
            self.assertNotIn(junk, sent, f"残留不得进入模型：{junk!r}")

    def test_candidate_name_never_returns_section_heading(self):
        """清洗掉解析残留后章节标题会变成首行，不得被当成姓名。"""
        for heading in ("个人优势", "工作经历", "教育背景", "自我评价", "核心技能", "求职意向", "个人信息"):
            content = heading + "\n\n1.熟悉常见通讯协议及网络协议\n\n2.熟练掌握 Linux 命令\n\n3.责任心强，适应出差"
            self.assertEqual(
                backend._candidate_name_from_resume({"content": content, "processed": {}}, {}),
                "未识别姓名",
                f"章节标题被当成姓名：{heading}",
            )

    def test_normalized_report_protocol_contains_all_frontend_fields(self):
        raw = {
            "candidate_name": "协议候选人",
            "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 18, "industry_background": 10, "evidence_bonus": 7},
            "job_fit_score": 75,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "协议回归测试。",
            "basic_screening": {},
            "work_history": {"employment_records": []},
            "skill_match": {},
            "certificates": [],
            "bonus_items": [],
            "strengths": ["有相关经历"],
            "weaknesses": ["部分信息未提供"],
            "risk_points": ["无明显风险"],
            "role_specific_assessment": ["不适用"],
            "deduction_reasons": [],
            "recruitment_recommendation": "储备观察",
            "fit_tag": "部分匹配",
        }
        result = backend._normalize_hr_analysis(raw, "招聘后端工程师", "协议候选人\nPython 后端工程师")
        expected_fields = {
            "candidate_name", "final_score", "fit_grade", "job_fit_score", "job_fit_percentage",
            "ai_risk", "ai_risk_level", "ai_risk_label", "ai_deduction", "summary",
            "basic_screening", "work_history", "skill_match", "certificates", "bonus_items",
            "strengths", "weaknesses", "risk_points", "role_specific_assessment",
            "deduction_reasons", "recruitment_recommendation", "fit_tag", "score_breakdown",
        }
        self.assertTrue(expected_fields.issubset(result.keys()))
        self.assertIsInstance(result["basic_screening"], dict)

    def test_ai_risk_is_hint_only_by_default(self):
        """默认 readonly：AI 美化只作风险提示，不参与扣分（分数只由能力分与硬门槛决定）。"""
        raw = {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 20, "responsibility_overlap": 18,
                "skills_projects": 16, "industry_background": 10, "evidence_bonus": 8,
            },
            "ai_risk": "medium",
            "ai_deduction": 18,
            "summary": "疑似模板化表达。",
            "basic_screening": {},
            "work_history": {"employment_records": []},
            "skill_match": {},
            "certificates": [],
            "bonus_items": [],
            "strengths": ["有相关经历"],
            "weaknesses": ["部分信息未提供"],
            "risk_points": ["无明显风险"],
            "role_specific_assessment": ["不适用"],
            "deduction_reasons": ["多处跨段落通用话术"],
            "recruitment_recommendation": "储备观察",
            "fit_tag": "部分匹配",
        }

        result = backend._normalize_hr_analysis(raw, "招聘后端工程师", "张三\n后端工程师")

        self.assertEqual(sum(raw["score_breakdown"].values()), 72)
        self.assertEqual(result["final_score"], 72)  # 未被扣 18 分
        self.assertEqual(result["ai_deduction"], 0)
        self.assertEqual(result["ai_deduction_suggested"], 18)  # 建议值照实记录
        self.assertEqual(result["ai_risk_level"], "中度AI美化")  # 档位仍然展示
        self.assertEqual(result["deduction_reasons"], ["多处跨段落通用话术"])
        self.assertIn("未扣分", result["ai_risk_note"])

    def test_ai_risk_enforce_mode_restores_old_behaviour(self):
        """开关兜底：AI_RISK_DEDUCTION_MODE=enforce 时恢复按档位区间扣分（并夹取到区间内）。"""
        raw = {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 20, "responsibility_overlap": 18,
                "skills_projects": 16, "industry_background": 10, "evidence_bonus": 8,
            },
            "ai_risk": "medium",
            "ai_deduction": 12,  # 低于 medium 下限 15 → 应被抬到 15
            "summary": "疑似模板化表达。",
            "basic_screening": {},
            "work_history": {"employment_records": []},
            "skill_match": {},
            "certificates": [],
            "bonus_items": [],
            "strengths": [],
            "weaknesses": [],
            "risk_points": [],
            "role_specific_assessment": [],
            "deduction_reasons": ["多处跨段落通用话术"],
            "recruitment_recommendation": "储备观察",
            "fit_tag": "部分匹配",
        }

        with patch.object(backend.config, "AI_RISK_DEDUCTION_MODE", "enforce"):
            result = backend._normalize_hr_analysis(raw, "招聘后端工程师", "张三\n后端工程师")

        self.assertEqual(result["ai_deduction"], 15)
        self.assertEqual(result["final_score"], 57)
        self.assertEqual(result["ai_deduction_suggested"], 15)
        self.assertEqual(result["ai_risk_note"], "")
        self.assertIsInstance(result["work_history"], dict)
        self.assertIsInstance(result["skill_match"], dict)
        self.assertIsInstance(result["employment_records"] if "employment_records" in result else result["work_history"].get("employment_records", []), list)

    def test_credibility_is_attached_only_when_mode_on(self):
        """核验面板的前置契约：`on` 时 payload 挂上 credibility，`shadow` 时不挂（不写、不进缓存）。"""
        stub = {
            "candidate_name": "张三",
            "job_fit_score": 78,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "核心经历匹配。",
            "basic_screening": {},
            "work_history": {"total_years": "8 年", "relevant_years": "6 年", "employment_records": []},
            "skill_match": {},
            "certificates": [],
            "bonus_items": [],
            "strengths": ["有相关经历"],
            "weaknesses": ["部分信息未提供"],
            "risk_points": ["无明显风险"],
            "role_specific_assessment": ["不适用"],
            "deduction_reasons": [],
            "recruitment_recommendation": "储备观察",
            "fit_tag": "部分匹配",
        }
        resume_id = backend.store.save_resume(
            "张三\n2018.07-至今 某公司 软件实施工程师\n负责现场设备安装调试。", {}, self.user_id
        )
        job_id = backend.store.save_job(resume_id, "招聘实施工程师，要求 3 年现场实施经验", {}, self.user_id)

        with patch.object(backend.screening_agent, "run_screening_agent", return_value=stub):
            with patch.object(backend.config, "DECEPTION_CHECK_MODE", "on"):
                on_result = self.client.post(
                    "/api/v1/resumes/hr-analysis",
                    json={"resume_id": resume_id, "job_id": job_id},
                    headers=self.headers,
                ).get_json()["data"]["hr_analysis"]
            with patch.object(backend.config, "DECEPTION_CHECK_MODE", "shadow"):
                shadow_result = self.client.post(
                    "/api/v1/resumes/hr-analysis",
                    json={"resume_id": resume_id, "job_id": job_id},
                    headers=self.headers,
                ).get_json()["data"]["hr_analysis"]

        credibility = on_result.get("credibility")
        self.assertIsInstance(credibility, dict)
        self.assertEqual(credibility["version"], "deception-check-v3")
        self.assertTrue(credibility["enabled"])
        self.assertEqual(credibility["checks_run"], 13)
        self.assertEqual(
            set(credibility.keys()),
            {"version", "enabled", "checks_run", "findings", "counts", "by_source", "notice"},
        )
        # 影子模式：字段根本不出现（HR 看不到，也不会进缓存）
        self.assertNotIn("credibility", shadow_result)
        # 核验绝不参与评分：同一份输入，挂不挂核验字段，分数与等级完全一致
        self.assertEqual(on_result["final_score"], shadow_result["final_score"])
        self.assertEqual(on_result["fit_grade"], shadow_result["fit_grade"])
        self.assertEqual(on_result["score_breakdown"], shadow_result["score_breakdown"])

    def test_normalizer_uses_ai_score_breakdown_sum(self):
        raw = {
            "candidate_name": "张三",
            "job_fit_score": 0,
            "score_breakdown": {
                "hard_requirements": 18,
                "responsibility_overlap": 20,
                "skills_projects": 17,
                "industry_background": 8,
                "evidence_bonus": 6,
            },
            "ai_risk": "none",
            "ai_deduction": 0,
        }

        result = backend._normalize_hr_analysis(raw)

        self.assertEqual(result["job_fit_score"], 69)
        self.assertEqual(result["final_score"], 69)
        self.assertEqual(result["score_breakdown"]["skills_projects"], 17)

    def test_normalizer_caps_recommendation_and_tag_to_final_score(self):
        raw = {
            "candidate_name": "张三",
            "job_fit_score": 47,
            "ai_risk": "none",
            "ai_deduction": 0,
            "recruitment_recommendation": "优先面试",
            "fit_tag": "高匹配",
        }

        result = backend._normalize_hr_analysis(raw)

        self.assertEqual(result["recruitment_recommendation"], "淘汰")
        self.assertEqual(result["fit_tag"], "不匹配")

    def test_normalizer_calculates_gaps_from_employment_records(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "work_history": {
                    "employment_records": [
                        {
                            "company_name": "甲公司",
                            "job_title": "工程师",
                            "start_date": "2019年1月",
                            "end_date": "2020年6月",
                        },
                        {
                            "company_name": "乙公司",
                            "job_title": "高级工程师",
                            "start_date": "2020/09",
                            "end_date": "2022.03",
                        },
                        {
                            "company_name": "丙公司",
                            "job_title": "技术负责人",
                            "start_date": "2022-04",
                            "end_date": "至今",
                        },
                    ],
                    "employment_gap_notes": "该阶段在职备考",
                    "employment_gaps": "模型自行计算的内容不会被采用",
                },
            }
        )

        history = result["work_history"]
        self.assertEqual(
            [record["company_name"] for record in history["employment_records"]],
            ["丙公司", "乙公司", "甲公司"],
        )
        self.assertEqual(history["employment_records"][1]["duration"], "1 年 7 个月")
        self.assertEqual(
            history["employment_gaps"],
            "共 1 段，累计 2 个月：2020-07 至 2020-08（2 个月）；简历说明：该阶段在职备考",
        )

    def test_normalizer_marks_imprecise_employment_dates(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "work_history": {
                    "employment_records": [
                        {
                            "company_name": "甲公司",
                            "job_title": "工程师",
                            "start_date": "2019",
                            "end_date": "2021",
                        }
                    ]
                },
            }
        )

        self.assertEqual(
            result["work_history"]["employment_gaps"],
            "部分经历缺少精确月份，无法完整核算空窗期",
        )

    def test_normalizer_extracts_all_employment_records_from_resume_text(self):
        resume_content = """
工作经历
华强方特（芜湖）智能技术有限公司 机械工程师
2023.06-至今
内容：舞台桁架与游乐设备设计
苏州法特迪科技股份有限公司 机械工程师
2021.01-2023.05
内容：芯片测试夹具设计
广州白云电器设备股份有限公司 机械结构工程师
2019.07-2020.08
内容：车间装配指导与设备故障排查
教育经历
华南农业大学 本科
2015-2019
"""
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "work_history": {
                    "employment_records": [
                        {
                            "company_name": "模型返回的重复公司",
                            "job_title": "机械工程师",
                            "start_date": "2023-06",
                            "end_date": "至今",
                        }
                    ]
                },
            },
            resume_content=resume_content,
        )

        history = result["work_history"]
        self.assertEqual(
            [record["company_name"] for record in history["employment_records"]],
            [
                "华强方特（芜湖）智能技术有限公司",
                "苏州法特迪科技股份有限公司",
                "广州白云电器设备股份有限公司",
            ],
        )
        self.assertEqual(
            [record["job_title"] for record in history["employment_records"]],
            ["机械工程师", "机械工程师", "机械结构工程师"],
        )
        self.assertEqual(history["employment_records"][1]["duration"], "2 年 5 个月")
        self.assertEqual(
            history["employment_gaps"],
            "共 1 段，累计 4 个月：2020-09 至 2020-12（4 个月）",
        )

    def test_normalizer_keeps_legacy_gap_text_without_records(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "work_history": {"employment_gaps": "2021 年有 3 个月空窗"},
            }
        )

        self.assertEqual(result["work_history"]["employment_records"], [])
        self.assertEqual(result["work_history"]["employment_gaps"], "2021 年有 3 个月空窗")

    def test_normalizer_removes_salary_risk_without_job_budget(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "risk_points": [
                    "期望薪资可能高于岗位预算",
                    "缺少 RFID 项目经验，影响现场联调",
                ],
            },
            "要求三年以上现场实施经验，可接受出差",
        )

        self.assertEqual(result["risk_points"], ["缺少 RFID 项目经验，影响现场联调"])

    def test_compact_analysis_text_removes_pdf_watermark_noise(self):
        source = "\n".join(
            [
                "G q n P",
                "N -0t6 0 G",
                "912cf79e4c4712351HN0t60GFBXwY29WPqaWOGqnPHQMRRj",
                "Linux",
                "负责现场通讯设备的安装、调试、配置与上线验收",
            ]
        )

        compacted = backend._compact_analysis_text(source, 8000)

        self.assertNotIn("G q n P", compacted)
        self.assertNotIn("912cf79", compacted)
        self.assertIn("Linux", compacted)
        self.assertIn("负责现场通讯设备", compacted)

    def test_normalizer_replaces_legacy_skill_labels(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "skill_match": {
                    "hard_skills": [
                        "[直接匹配] Python：5年经验",
                        "[可迁移] Java：后端经验",
                        "[缺失] Kubernetes：简历无证据",
                    ]
                },
            }
        )

        self.assertEqual(
            result["skill_match"]["hard_skills"],
            [
                "[符合要求] Python：5年经验",
                "[相关经验] Java：后端经验",
                "[简历未体现] Kubernetes：简历无证据",
            ],
        )

    def test_llm_accepts_reasoning_content_when_content_is_empty(self):
        class FakeCompletions:
            def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                reasoning_content='完成分析，最终结果：{"ok": true}',
                            )
                        )
                    ]
                )

        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
        finally:
            llm_module._client = previous

    def test_llm_enables_deepseek_thinking_for_quality_mode(self):
        class FakeCompletions:
            def __init__(self):
                self.request = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
                )

        completions = FakeCompletions()
        previous = llm_module._client
        previous_fingerprint = llm_module._client_fingerprint
        llm_module._client = None
        llm_module._client_fingerprint = ""
        try:
            # 服务端配置改成调用时解析后，这里改为桩掉配置解析与客户端构造，
            # 不再依赖模块级常量（那正是"改配置要重启"的根源）。
            with patch.object(llm_module.system_config, "resolve_llm", return_value={
                "api_key": "test-key",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "timeout": 300,
                "ready": True,
                "source": "ui",
            }), patch.object(
                llm_module,
                "_get_client",
                return_value=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            ):
                result = llm_module.call_llm(
                    "输出 JSON", expect_json=True, max_tokens=100, thinking=True
                )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(completions.request["extra_body"], {"thinking": {"type": "enabled"}})
        finally:
            llm_module._client = previous
            llm_module._client_fingerprint = previous_fingerprint

    def test_llm_parses_reasoning_json_when_content_has_prose(self):
        class FakeCompletions:
            def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="我已经完成分析。",
                                reasoning_content='最终结果：{"ok": true}',
                            )
                        )
                    ]
                )

        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
        finally:
            llm_module._client = previous

    def test_llm_retries_when_first_json_is_truncated(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                content = '{"ok":' if self.calls == 1 else '{"ok": true}'
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        completions = FakeCompletions()
        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        try:
            self.assertEqual(llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50), {"ok": True})
            self.assertEqual(completions.calls, 2)
        finally:
            llm_module._client = previous

    def test_llm_uses_ai_repair_after_two_invalid_responses(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                content = '{"ok":' if len(self.calls) < 3 else '{"ok": true}'
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                )

        completions = FakeCompletions()
        previous = llm_module._client
        llm_module._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        try:
            self.assertEqual(
                llm_module.call_llm("输出 JSON", expect_json=True, max_tokens=50),
                {"ok": True},
            )
            self.assertEqual(len(completions.calls), 3)
        finally:
            llm_module._client = previous

    def test_lenient_json_accepts_trailing_commas(self):
        self.assertEqual(
            llm_module.parse_json_lenient('{"items": ["a",],}'),
            {"items": ["a"]},
        )

    def test_candidate_name_falls_back_to_resume_heading(self):
        self.assertEqual(
            backend._candidate_name_from_resume(
                {"content": "李四\n联系方式\n13800000000", "processed": {}},
                {"candidate_name": "未提供"},
            ),
            "李四",
        )

    def test_hr_analysis_returns_error_when_model_returns_invalid_json(self):
        resume_id = backend.store.save_resume("Python 后端工程师，5 年经验，FastAPI 开发", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘 Python 后端工程师，要求 3 年经验，FastAPI", {}, self.user_id)

        with patch.object(backend.llm, "call_llm", side_effect=ValueError("invalid JSON")):
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("AI 分析结果无法解析", response.get_json()["detail"])

    def test_batch_hr_analysis_keeps_successful_results(self):
        resume_ids = [
            backend.store.save_resume("Python 后端工程师，5 年经验", {}, self.user_id),
            backend.store.save_resume("Java 后端工程师，3 年经验", {}, self.user_id),
        ]
        job_id = backend.store.save_job(
            resume_ids[0],
            "招聘后端工程师，要求 Python、API 和数据库经验",
            {},
            self.user_id,
        )
        model_result = {
            "job_fit_score": 80,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "岗位经验基本匹配。",
            "strengths": ["具备后端开发经验"],
            "weaknesses": ["项目数据未提供"],
            "risk_points": ["无明显风险"],
        }

        def analyze_candidate(*, resume_content="", **_kwargs):
            if "Java 后端工程师，3 年经验" in resume_content:
                raise ValueError("invalid candidate output")
            return model_result

        def mock_extract(*args, **_kwargs):
            return {"requirements": [{"id": "req-1", "text": "Python 经验", "category": "skill", "hard": True, "logic": "required"}], "extraction_failed": False}

        with patch.object(backend.screening_agent, "run_screening_agent", side_effect=analyze_candidate) as call:
            with patch.object(backend.screening_agent, "_extract_requirements", side_effect=mock_extract):
                response = self.client.post(
                    "/api/v1/resumes/hr-analysis",
                    json={"resume_ids": resume_ids, "job_id": job_id},
                    headers=self.headers,
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(len(data["batch_analyses"]), 1)
        self.assertEqual(len(data["batch_failures"]), 1)
        self.assertEqual(data["batch_analyses"][0]["resume_id"], resume_ids[0])
        self.assertEqual(data["batch_failures"][0]["resume_id"], resume_ids[1])
        self.assertEqual(call.call_count, 2)

    def test_batch_backstops_shared_requirements_exactly_once(self):
        """批量共享同一份 requirements 并发跑：兜底必须在外层一次性完成，线程内只读。

        否则两个线程同时判断"这条门槛没人覆盖"就会各补一条 → 重复扣分、清单重影。
        """
        resume_ids = [
            backend.store.save_resume("候选人甲，3 年经验", {}, self.user_id),
            backend.store.save_resume("候选人乙，3 年经验", {}, self.user_id),
        ]
        job_id = backend.store.save_job(
            resume_ids[0],
            "岗位要求：\n1.硕士及以上学历\n2.5年以上后端开发经验",
            {},
            self.user_id,
        )
        model_result = {
            "job_fit_score": 80,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "岗位经验基本匹配。",
            "strengths": ["具备后端开发经验"],
            "weaknesses": ["项目数据未提供"],
            "risk_points": ["无明显风险"],
        }
        snapshots: list[list[str]] = []

        def analyze_candidate(*, precomputed_requirements=None, **_kwargs):
            snapshots.append([item["text"] for item in precomputed_requirements["requirements"]])
            return model_result

        with patch.object(backend.screening_agent, "run_screening_agent", side_effect=analyze_candidate), \
                patch.object(backend.screening_agent, "_extract_requirements",
                             side_effect=lambda *args, **_kwargs: {"requirements": [], "extraction_failed": False}):
            response = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_ids": resume_ids, "job_id": job_id},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(snapshots), 2)
        for snapshot in snapshots:
            # 兜底补出的两条门槛在**每个**线程看到的都是同一份，没有重影
            self.assertEqual(len(snapshot), 2)
            self.assertEqual(len(set(snapshot)), 2)
            self.assertEqual(snapshot, snapshots[0])

    def test_batch_hr_analysis_maps_all_candidates_from_one_model_call(self):
        resume_ids = [
            backend.store.save_resume("候选人甲，Python 5 年", {}, self.user_id),
            backend.store.save_resume("候选人乙，Java 4 年", {}, self.user_id),
            backend.store.save_resume("候选人丙，Go 3 年", {}, self.user_id),
        ]
        job_id = backend.store.save_job(resume_ids[0], "招聘后端工程师", {}, self.user_id)

        def analysis(name: str, score: int) -> dict:
            return {
                "candidate_name": name,
                "job_fit_score": score,
                "ai_risk": "none",
                "ai_deduction": 0,
                "summary": f"{name}的岗位证据已完成分析。",
                "strengths": ["具备相关开发经验"],
                "weaknesses": ["量化成果未提供"],
                "risk_points": ["无明显风险"],
            }

        barrier = Barrier(3)
        comparison_payload = {
            "ranking": [
                {"rank": 1, "name": "候选人甲", "score": 85, "difference": "Python 经验最深"},
                {"rank": 2, "name": "候选人乙", "score": 72, "difference": "Java 经验匹配"},
                {"rank": 3, "name": "候选人丙", "score": 65, "difference": "经验相对较浅"},
            ],
            "pairwise": [],
            "recommendation": "优先面试：候选人甲",
        }

        def analyze_candidate(*, resume_content="", **_kwargs):
            barrier.wait(timeout=2)
            if "候选人甲" in resume_content:
                return analysis("候选人甲", 85)
            if "候选人乙" in resume_content:
                return analysis("候选人乙", 72)
            return analysis("候选人丙", 65)

        def mock_extract(*args, **_kwargs):
            return {"requirements": [{"id": "req-1", "text": "后端开发经验", "category": "experience", "hard": True, "logic": "required"}], "extraction_failed": False}

        with patch.object(backend.screening_agent, "run_screening_agent", side_effect=analyze_candidate) as call:
            with patch.object(backend.screening_agent, "_extract_requirements", side_effect=mock_extract):
                with patch.object(backend.llm, "call_llm", return_value=comparison_payload) as compare_call:
                    response = self.client.post(
                        "/api/v1/resumes/hr-analysis",
                        json={"resume_ids": resume_ids, "job_id": job_id},
                        headers=self.headers,
                    )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(call.call_count, 3)
        self.assertEqual(len(data["batch_analyses"]), 3)
        self.assertEqual(data["batch_failures"], [])
        # 3 位候选人时触发跨候选人对比，结果透传到 comparison 字段
        self.assertEqual(compare_call.call_count, 1)
        self.assertEqual(data["comparison"]["recommendation"], "优先面试：候选人甲")
        self.assertEqual(len(data["comparison"]["ranking"]), 3)
        self.assertEqual(
            [item["candidate_name"] for item in data["batch_analyses"]],
            ["候选人甲", "候选人乙", "候选人丙"],
        )
        self.assertEqual(
            [item["hr_analysis"]["final_score"] for item in data["batch_analyses"]],
            [85, 72, 65],
        )

    def test_ai_connection_uses_request_scoped_config(self):
        ai_config = {
            "provider": "deepseek",
            "api_key": "sk-browser-only",
            "base_url": "https://api.deepseek.com/",
            "model": "deepseek-chat",
        }
        with patch.object(backend.llm, "call_llm", return_value="OK") as call:
            response = self.client.post(
                "/api/v1/ai/test",
                json={"ai_config": ai_config},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["data"]["ok"])
        runtime_config = call.call_args.kwargs["runtime_config"]
        self.assertEqual(runtime_config["api_key"], "sk-browser-only")
        self.assertEqual(runtime_config["base_url"], "https://api.deepseek.com")
        self.assertEqual(call.call_args.kwargs["max_tokens"], 8)

    def test_ai_connection_rejects_invalid_compatible_url(self):
        response = self.client.post(
            "/api/v1/ai/test",
            json={
                "ai_config": {
                    "provider": "custom",
                    "api_key": "sk-test",
                    "base_url": "file:///tmp/model",
                    "model": "custom-chat",
                }
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("HTTP", response.get_json()["detail"])

    def test_hr_cache_is_isolated_by_request_model_config(self):
        resume_id = backend.store.save_resume("Python 工程师，5 年经验", {}, self.user_id)
        job_id = backend.store.save_job(resume_id, "招聘 Python 工程师", {}, self.user_id)
        model_result = {
            "job_fit_score": 76,
            "ai_risk": "none",
            "summary": "岗位经验匹配。",
        }
        first_config = {
            "provider": "deepseek",
            "api_key": "sk-first",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
        }
        second_config = {
            **first_config,
            "api_key": "sk-second",
        }

        with patch.object(backend.screening_agent, "run_screening_agent", return_value=model_result) as call:
            first = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": first_config},
                headers=self.headers,
            )
            cached = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": first_config},
                headers=self.headers,
            )
            isolated = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={"resume_id": resume_id, "job_id": job_id, "ai_config": second_config},
                headers=self.headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(isolated.status_code, 200)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(
            [item.kwargs["runtime_config"]["api_key"] for item in call.call_args_list],
            ["sk-first", "sk-second"],
        )

    def test_request_ai_config_rejected_when_custom_disabled(self):
        previous = backend.config.ALLOW_CUSTOM_AI_CONFIG
        backend.config.ALLOW_CUSTOM_AI_CONFIG = False
        try:
            response = self.client.post(
                "/api/v1/ai/test",
                json={
                    "ai_config": {
                        "provider": "deepseek",
                        "api_key": "sk-forbidden",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-chat",
                    }
                },
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 403)
            self.assertIn("自定义", response.get_json()["detail"])

            analysis = self.client.post(
                "/api/v1/resumes/hr-analysis",
                json={
                    "resume_id": "r-none",
                    "job_id": "j-none",
                    "ai_config": {"provider": "deepseek", "api_key": "sk-forbidden"},
                },
                headers=self.headers,
            )
            self.assertEqual(analysis.status_code, 403)
        finally:
            backend.config.ALLOW_CUSTOM_AI_CONFIG = previous

    def test_normalizer_carries_requirements_checklist(self):
        result = backend._normalize_hr_analysis(
            {
                "job_fit_score": 70,
                "ai_risk": "none",
                "requirements_checklist": [
                    {
                        "id": "req-1",
                        "text": "本科及以上学历",
                        "category": "education",
                        "logic": "required",
                        "status": "manual_review",
                        "resume_basis": "某大学本科",
                    }
                ],
            }
        )

        checklist = result["requirements_checklist"]
        self.assertEqual(len(checklist), 1)
        self.assertEqual(checklist[0]["status"], "manual_review")
        self.assertEqual(checklist[0]["resume_basis"], "某大学本科")

    def test_find_employment_overlaps_reports_conflict(self):
        records = [
            {"company_name": "甲公司", "job_title": "工程师", "start_date": "2020-01", "end_date": "2020-12", "duration": "1 年"},
            {"company_name": "乙公司", "job_title": "工程师", "start_date": "2020-06", "end_date": "2021-06", "duration": "1 年 1 个月"},
        ]

        overlaps = backend._find_employment_overlaps(records)

        self.assertEqual(len(overlaps), 1)
        self.assertIn("甲公司", overlaps[0]["message"])
        self.assertIn("乙公司", overlaps[0]["message"])
        self.assertGreaterEqual(overlaps[0]["months"], 7)

    def test_find_employment_overlaps_ignores_sequential_periods(self):
        records = [
            {"company_name": "甲公司", "job_title": "工程师", "start_date": "2020-01", "end_date": "2020-06", "duration": "6 个月"},
            {"company_name": "乙公司", "job_title": "工程师", "start_date": "2020-07", "end_date": "2020-12", "duration": "6 个月"},
        ]

        overlaps = backend._find_employment_overlaps(records)

        self.assertEqual(overlaps, [])

    def test_find_employment_overlaps_ignores_same_month_handover(self):
        """回归（YG 2026-10-14 发现）：当月辞职、当月入职下一家，不能被判成"任职时间重叠"。

        日期是月份精度，两段区间端点相接（上一段到 2026-10、下一段从 2026-10 开始）
        就是正常交接。旧实现用 `overlap_start <= overlap_end`，会把这一格算成
        "重叠 1 个月"并弹到前端 `EmploymentOverlapWarning` 上。
        """
        records = [
            {"company_name": "甲公司", "job_title": "工程师", "start_date": "2020-07", "end_date": "2026-10", "duration": "6 年 4 个月"},
            {"company_name": "乙公司", "job_title": "工程师", "start_date": "2026-10", "end_date": "至今", "duration": "1 个月"},
        ]

        overlaps = backend._find_employment_overlaps(records)

        self.assertEqual(overlaps, [], "同月离职并入职属于正常交接，不是时间冲突")

    def test_find_employment_overlaps_still_reports_real_overlap_after_tightening(self):
        """边界收紧后别收死：真正相交（错开一个月）必须继续报，且月份数要对。"""
        records = [
            {"company_name": "甲公司", "job_title": "工程师", "start_date": "2020-07", "end_date": "2026-11", "duration": "6 年 4 个月"},
            {"company_name": "乙公司", "job_title": "工程师", "start_date": "2026-10", "end_date": "2027-10", "duration": "1 年"},
        ]

        overlaps = backend._find_employment_overlaps(records)

        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["months"], 2, "2026-10 与 2026-11 两个整月相交")
        self.assertEqual(overlaps[0]["start_date"], "2026-10")
        self.assertEqual(overlaps[0]["end_date"], "2026-11")

    def test_prompt_states_same_month_handover_is_not_overlap(self):
        """同一个误报的另一半源头在提示词：模型自己往 risk_points 里写"经历时间重叠"。

        代码里的 `_find_employment_overlaps` 修好了，提示词也得把口径说清，
        否则模型仍可能把「当月辞职、当月入职」判成矛盾。这条守住那句话不被删掉。
        """
        prompt = prompts_module.PROMPT_HR_RECRUITMENT_ANALYSIS
        self.assertIn("当月辞职、当月入职下一家", prompt)
        self.assertIn("不算时间重叠", prompt)


class HardGateScoreTests(unittest.TestCase):
    """A/B：硬性门槛确定性判定（学历层级/年限/证书）与服务端扣分/封顶。"""

    def _normalize(self, raw: dict) -> dict:
        return backend._normalize_hr_analysis(raw, job_content="测试岗位", resume_content="")

    def test_education_below_jd_req_is_not_met_and_capped(self):
        raw = {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 25,
                "responsibility_overlap": 25,
                "skills_projects": 25,
                "industry_background": 15,
                "evidence_bonus": 10,
            },
            "job_fit_score": 100,
            "ai_risk": "none",
            "ai_deduction": 0,
            "requirements_checklist": [
                {"id": "r1", "text": "本科及以上学历", "category": "education", "logic": "required", "status": "not_met", "resume_basis": "岗位要求本科及以上学历，简历最高学历为专科"},
            ],
        }
        result = self._normalize(raw)
        self.assertEqual(result["final_score"], 59)          # 学历硬门槛不达标 → 淘汰级封顶
        self.assertEqual(result["hard_gate_deduction"], 10)  # 扣除 10 分
        self.assertEqual(result["fit_grade"], "D级（不适配）")

    def test_experience_below_jd_req_is_not_met_and_capped(self):
        raw = {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 22,
                "responsibility_overlap": 20,
                "skills_projects": 20,
                "industry_background": 10,
                "evidence_bonus": 5,
            },
            "job_fit_score": 77,
            "ai_risk": "none",
            "ai_deduction": 0,
            "requirements_checklist": [
                {"id": "r1", "text": "5年以上软件开发经验", "category": "experience", "logic": "required", "status": "not_met", "resume_basis": "岗位要求5年以上经验，简历明确年限为3年"},
            ],
        }
        result = self._normalize(raw)
        self.assertEqual(result["hard_gate_deduction"], 8)   # 年限属 B 档：每条 8 分
        self.assertEqual(result["final_score"], 69)          # 77 - 8 = 69，正好压在 B 档封顶位
        self.assertEqual(result["gate_tier_counts"], {"A": 0, "B": 1, "C": 0})

    def test_multiple_hard_fails_use_per_tier_caps(self):
        """多条不达标：按档各自累计——A 每条 10（上限 30）、B 每条 8（上限 24）、C 每条 5（上限 15）。"""
        raw = {
            "candidate_name": "张三",
            "score_breakdown": {
                "hard_requirements": 25,
                "responsibility_overlap": 20,
                "skills_projects": 20,
                "industry_background": 10,
                "evidence_bonus": 5,
            },
            "job_fit_score": 80,
            "ai_risk": "none",
            "ai_deduction": 0,
            "requirements_checklist": [
                {"id": "r1", "text": "必须持有PMP证书", "category": "certificate", "logic": "required", "status": "not_met", "resume_basis": "简历已写明无PMP证书"},
                {"id": "r2", "text": "5年以上经验", "category": "experience", "logic": "required", "status": "not_met", "resume_basis": "年限3年"},
                {"id": "r3", "text": "本科及以上学历", "category": "education", "logic": "required", "status": "not_met", "resume_basis": "最高学历专科"},
                {"id": "r4", "text": "必须持有CISP证书", "category": "certificate", "logic": "required", "status": "not_met", "resume_basis": "简历已写明无CISP证书"},
                {"id": "r5", "text": "8年以上经验", "category": "experience", "logic": "required", "status": "not_met", "resume_basis": "年限3年"},
            ],
        }
        result = self._normalize(raw)

        # A 档 1 条 = 10；B 档 4 条 = 8×4 = 32 → 受 B 档上限 24 约束
        self.assertEqual(result["gate_tier_counts"], {"A": 1, "B": 4, "C": 0})
        self.assertEqual(result["hard_gate_deduction"], 34)
        self.assertEqual(result["final_score"], 46)          # 80 - 34 = 46

    def test_checklist_not_met_inside_full_analysis_flow(self):
        """整条链路：screening_agent 生成 checklist → normalize 扣分。用单测函数直接验证判定逻辑。"""
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "硕士及以上学历", "category": "education", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "education_history": [{"degree": "专科", "school_name": "某职业学院", "school_tier": "专科", "major": "计算机", "graduation_year": "2020"}],
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n专科，某职业学院\n计算机")
        self.assertEqual(len(checklist), 1)
        self.assertEqual(checklist[0]["status"], "not_met")
        self.assertIn("硕士", checklist[0]["resume_basis"])

    def test_education_meets_level_stays_manual_review(self):
        """学历层级达标（本科 vs 本科）→ 不扣分，保留人工待核实（真实性无法自动验证）。"""
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "本科及以上学历", "category": "education", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "education_history": [{"degree": "本科", "school_name": "复旦大学", "school_tier": "985", "major": "计算机", "graduation_year": "2020"}],
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n本科，复旦大学\n计算机")
        self.assertEqual(checklist[0]["status"], "manual_review")
        self.assertIn("真实性", checklist[0]["resume_basis"])

    def test_experience_not_met_inside_checklist(self):
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "5年以上软件开发经验", "category": "experience", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "work_history": {"total_years": "3年", "relevant_years": "2年"},
            "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 20, "industry_background": 10, "evidence_bonus": 5},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n三年软件实施经验")
        self.assertEqual(len(checklist), 1)
        self.assertEqual(checklist[0]["status"], "not_met")
        self.assertIn("3", checklist[0]["resume_basis"])

    def test_certificate_missing_inside_checklist(self):
        """三态：简历没提到 PMP → 未写明，交人工（不扣分、不封顶）。"""
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "必须持有PMP证书", "category": "certificate", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "certificates": ["英语六级"],
            "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 20, "industry_background": 10, "evidence_bonus": 5},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n英语六级")

        self.assertEqual(checklist[0]["status"], "manual_review")
        self.assertEqual(checklist[0]["evidence_state"], "missing")
        self.assertEqual(checklist[0]["evidence_label"], "未写明或无法核对，需人工确认")
        self.assertIn("未写明", checklist[0]["resume_basis"])

    def test_certificate_explicitly_absent_inside_checklist(self):
        """三态：简历自己写明"无PMP证书" → 明确不符合，才判 not_met。"""
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "必须持有PMP证书", "category": "certificate", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "certificates": ["英语六级", "无PMP证书"],
            "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 20, "industry_background": 10, "evidence_bonus": 5},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\n英语六级")

        self.assertEqual(checklist[0]["status"], "not_met")
        self.assertEqual(checklist[0]["evidence_state"], "conflict")
        self.assertEqual(checklist[0]["evidence_label"], "已写明不符合")

    def test_certificate_present_is_met(self):
        import screening_agent

        requirements = {"requirements": [{"id": "r1", "text": "必须持有PMP证书", "category": "certificate", "hard": True, "logic": "required"}]}
        report = {
            "candidate_name": "张三",
            "certificates": ["PMP认证 项目管理专业认证"],
            "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 20, "industry_background": 10, "evidence_bonus": 5},
        }
        checklist = screening_agent._build_requirements_checklist(requirements, report, "张三\nPMP认证")
        self.assertEqual(checklist[0]["status"], "met")


class AgentValidationRuleSixTests(unittest.TestCase):
    """C：Agent 自校规则 6（分数-证据一致性）：高分但岗位硬性要求大多无简历证据 → 检出疑点。"""

    def test_rule6_triggers_when_high_score_without_evidence(self):
        import screening_agent

        report = {
            "candidate_name": "张三",
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
            "job_fit_score": 100,
            "ai_risk": "none",
            "ai_deduction": 0,
            "strengths": ["精通资深Python"],
        }
        requirements = {
            "requirements": [
                {"id": "r1", "text": "必须掌握Java", "category": "skill", "hard": True, "logic": "required"},
                {"id": "r2", "text": "必须掌握C++", "category": "skill", "hard": True, "logic": "required"},
            ]
        }
        resume = "张三\n产品经理\n负责需求分析"
        findings = screening_agent._precheck_findings(report, requirements, resume)
        rules = {item["rule"] for item in findings}
        self.assertIn(6, rules)
        for item in findings:
            if item["rule"] == 6:
                self.assertIn("硬性要求维度得分偏高", item["problem"])

    def test_rule6_not_triggered_when_evidence_present(self):
        import screening_agent

        report = {
            "candidate_name": "张三",
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
            "job_fit_score": 100,
            "ai_risk": "none",
            "ai_deduction": 0,
            "strengths": ["有产品经验"],
        }
        requirements = {
            "requirements": [
                {"id": "r1", "text": "5年以上产品经验", "category": "experience", "hard": True, "logic": "required"},
            ]
        }
        resume = "张三\n5年以上产品经验\n负责 AI Agent 产品，主导需求分析"
        findings = screening_agent._precheck_findings(report, requirements, resume)
        rules = {item["rule"] for item in findings}
        self.assertNotIn(6, rules)

    def test_reflect_checks_six_rules(self):
        import screening_agent

        report = {
            "candidate_name": "张三",
            "score_breakdown": {"hard_requirements": 25, "responsibility_overlap": 25, "skills_projects": 25, "industry_background": 15, "evidence_bonus": 10},
            "job_fit_score": 100,
            "ai_risk": "none",
            "ai_deduction": 0,
        }
        result = screening_agent._self_reflect(
            report,
            job_content="测试岗位",
            resume_content="张三\n本科\n5年经验",
            requirements={"requirements": []},
            experiences=[],
            current_date="2026-08",
            runtime_config=None,
            budget={"calls": 0},
        )
        self.assertEqual(result[1]["checked_rules"], 6)


class GateCoverageTests(unittest.TestCase):
    """“门槛到底跑没跑”必须是可见数据，而不是只能靠人抽查。

    旧行为：JD 写着硬门槛、模型却一条都没标成门槛时，清单是空的，
    分数照给、报告照出，输出里没有任何地方说"这次一条门槛都没跑"。
    """

    RAW = {
        "candidate_name": "张三",
        "summary": "s",
        "job_fit_score": 90,
        "score_breakdown": {
            "basic_info_score": 20, "skill_match_score": 30, "experience_score": 20,
            "education_score": 12, "potential_score": 8,
        },
        "ai_risk_level": "low",
    }

    def test_warning_when_markers_exist_but_no_gate_ran(self):
        checklist = [{"id": "r1", "text": "沟通能力", "category": "skill", "status": "met"}]

        summary = backend._gate_coverage_summary({"markers_found": 3, "added": 0, "upgraded": 0}, checklist, 0)

        self.assertEqual(summary["gate_items"], 0)
        self.assertIn("没有任何门槛参与判定", summary["warning"])

    def test_no_warning_when_gates_ran(self):
        checklist = [{"id": "r1", "text": "硕士及以上学历", "category": "education", "status": "not_met"}]

        summary = backend._gate_coverage_summary({"markers_found": 3, "added": 2, "upgraded": 1}, checklist, 1)

        self.assertEqual(summary["warning"], "")
        self.assertEqual(summary["gate_items"], 1)
        self.assertEqual(summary["gate_failed"], 1)
        self.assertEqual(summary["backstop_added"], 2)
        self.assertEqual(summary["upgraded"], 1)

    def test_missing_coverage_data_degrades_to_zero(self):
        empty = {
            "markers_found": 0, "backstop_added": 0, "upgraded": 0,
            "gate_items": 0, "gate_failed": 0, "warning": "",
        }

        self.assertEqual(backend._gate_coverage_summary(None, [], 0), empty)
        self.assertEqual(backend._gate_coverage_summary("bad", [], 0), empty)

    def test_payload_carries_gate_coverage(self):
        raw = dict(self.RAW)
        raw["gate_coverage"] = {"markers_found": 4, "added": 1, "upgraded": 2}
        raw["requirements_checklist"] = [
            {"id": "gate-backstop-1", "text": "本科及以上学历", "category": "education",
             "logic": "required", "status": "not_met", "resume_basis": "学历不够",
             "gate_dimension": "degree", "source": "gate_backstop"},
        ]

        result = backend._normalize_hr_analysis(raw, "招聘后端工程师", "某公司简历正文")

        self.assertEqual(result["gate_coverage"]["markers_found"], 4)
        self.assertEqual(result["gate_coverage"]["backstop_added"], 1)
        self.assertEqual(result["gate_coverage"]["upgraded"], 2)
        self.assertEqual(result["gate_coverage"]["gate_items"], 1)
        self.assertEqual(result["gate_coverage"]["gate_failed"], 1)
        self.assertEqual(result["gate_coverage"]["warning"], "")
        # 兜底补出来的门槛留痕，HR 能看到这条不是模型抽的
        self.assertEqual(result["requirements_checklist"][0]["source"], "gate_backstop")


class IncompleteAnalysisTests(unittest.TestCase):
    """分析根本没跑成时，结论字段不能冒充一个判定结论。

    现场：图片型 PDF 抽不出文字 → 分析层抛"简历内容不能为空" → 兜底报告 0 分 →
    服务端算出 "0 分 / D级 / 淘汰 / 不匹配" 当作正常分析返回，失败原因还不在 payload 里。
    """

    def _failed_raw(self):
        return {
            "candidate_name": "未提供",
            "job_fit_score": 0,
            "ai_risk": "none",
            "ai_deduction": 0,
            "summary": "AI 分析未能生成有效报告。",
            "agent_meta": {
                "version": "screening-agent-v1",
                "validation_passed": False,
                "validation_issues": ["简历内容不能为空"],
                "llm_calls": 0,
            },
        }

    def test_failed_analysis_is_not_reported_as_eliminated(self):
        result = backend._normalize_hr_analysis(self._failed_raw(), "招聘后端工程师", "")

        self.assertTrue(result["analysis_incomplete"])
        self.assertEqual(result["analysis_incomplete_reason"], "简历内容不能为空")
        self.assertEqual(result["fit_grade"], "无法评估")
        self.assertEqual(result["recruitment_recommendation"], "待补充材料")
        self.assertEqual(result["fit_tag"], "无法评估")

    def test_reason_is_visible_in_the_payload(self):
        result = backend._normalize_hr_analysis(self._failed_raw(), "招聘后端工程师", "")

        self.assertEqual(result["agent_meta"]["validation_issues"], ["简历内容不能为空"])
        self.assertFalse(result["agent_meta"]["validation_passed"])

    def test_a_real_low_score_is_not_marked_incomplete(self):
        """真跑过并给出低分（有分项）不能被当成"没跑成"。"""
        raw = self._failed_raw()
        raw["score_breakdown"] = {
            "basic_info_score": 5, "skill_match_score": 3, "experience_score": 2,
            "education_score": 0, "potential_score": 0,
        }

        result = backend._normalize_hr_analysis(raw, "招聘后端工程师", "某公司简历正文")

        self.assertNotIn("analysis_incomplete", result)
        self.assertNotEqual(result["recruitment_recommendation"], "待补充材料")


class AuthoritativeRankingTests(unittest.TestCase):
    """批量对比的名次与分数由系统决定，模型只提供"差异点"描述。

    旧实现按姓名回填分数有三个坑：重名互相覆盖（只剩最后一条）、
    模型漏写某位候选人时不补回、姓名对不上时填 0 分（90 分的候选人被排到最后）。
    """

    def test_every_candidate_appears_even_with_duplicate_names(self):
        items = [
            {"candidate_name": "未提供", "final_score": 88, "incomplete": False},
            {"candidate_name": "未提供", "final_score": 42, "incomplete": False},
        ]

        rows = backend._build_authoritative_ranking(items, [{"name": "未提供", "difference": "甲的差异"}])

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["score"] for row in rows], [88, 42])
        self.assertEqual([row["rank"] for row in rows], [1, 2])

    def test_missing_or_renamed_candidates_keep_their_real_scores(self):
        items = [
            {"candidate_name": "甲", "final_score": 90, "incomplete": False},
            {"candidate_name": "乙", "final_score": 70, "incomplete": False},
        ]

        # 模型把名字写错、还漏了乙
        rows = backend._build_authoritative_ranking(items, [{"name": "甲甲", "difference": "x"}])

        self.assertEqual([row["name"] for row in rows], ["甲", "乙"])
        self.assertEqual([row["score"] for row in rows], [90, 70])
        self.assertNotIn(0, [row["score"] for row in rows])

    def test_incomplete_analyses_rank_last_without_a_fake_score(self):
        items = [
            {"candidate_name": "甲", "final_score": 60, "incomplete": False},
            {"candidate_name": "乙", "final_score": None, "incomplete": True},
        ]

        rows = backend._build_authoritative_ranking(items, [])

        self.assertEqual(rows[0]["name"], "甲")
        self.assertEqual(rows[1]["name"], "乙")
        self.assertIsNone(rows[1]["score"])
        self.assertTrue(rows[1]["incomplete"])


if __name__ == "__main__":
    unittest.main()
