"""Read-only analysis-history HTTP contracts and pure helper tests."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4, UUID
import time

import jwt

from db_analysis_markdown import (_extract_md_block as extract_md_block,
    _count_sections as count_sections, _normalize_md_for_a4cv as normalize_md_for_a4cv,
    _resume_studio_markdown as resume_studio_markdown)
from db_auth import InternalUser
from db_http_preview import create_preview_app


class AnalysisHistoryHelperTests(TestCase):
    def test_markdown_helpers_are_bounded_and_deterministic(self):
        value, source = extract_md_block("prefix\n```md\n# A\n**项目经验**\n" + '合成项目说明。' * 40 + "\n```\nsuffix")
        self.assertEqual(source, "extracted")
        normalized = normalize_md_for_a4cv(value)
        self.assertIn("## 项目经验", normalized)
        self.assertEqual(count_sections(normalized), 2)
        self.assertEqual(extract_md_block("no markdown"), (None, "none"))

    def test_resume_fallback_does_not_emit_empty_or_secret_fields(self):
        result = resume_studio_markdown({'content': "张三\n工作经历\n某公司 Java 开发"})
        self.assertEqual(result, "# 张三\n\n## 工作经历\n某公司 Java 开发\n")
        self.assertEqual(resume_studio_markdown({}), "")

    def test_optimized_draft_is_selected_only_from_step_four(self):
        example = '# 原稿\n## 工作经历\n' + '旧版项目说明。' * 100
        optimized = '# 优化稿\n## 工作经历\n' + '优化项目说明。' * 30
        report = f'## Step 3 · 修改蓝图\n```md\n{example}\n```\n## 第四步：修改后的完整简历\n```markdown\n{optimized}\n```\n## Step 5 · 最终裁决'
        self.assertEqual(extract_md_block(report.replace('\n', '\r\n'), require_step_four=True), (optimized, 'extracted'))
        self.assertEqual(extract_md_block(f'## Step 3\n```md\n{example}\n```'), (None, 'none'))
        self.assertEqual(extract_md_block(f'```md\n# Step 4\n{example}\n```', require_step_four=True), (None, 'none'))

    def test_short_editable_draft_is_accepted_but_empty_sections_are_not(self):
        short = '# 测试候选人\n\n## 工作经历\n- 核对出入库记录。\n\n## 技能\n- 库存盘点'
        empty = '# 测试候选人\n\n## 工作经历\n---\n\n## 技能\n-'
        for draft, expected in ((short, (short, 'extracted')), (empty, (None, 'none'))):
            report = f'## Step 4 · 修改后的完整简历\n```md\n{draft}\n```'
            self.assertEqual(extract_md_block(report, require_step_four=True), expected)

    def test_studio_heading_only_entries_count_as_editable_content(self):
        section = '# 测试候选人\n\n## 教育经历\n'
        for entry, accepted in (
            ('### 合成大学 · 本科 | 2022–2026', True),
            ('  ### 合成大学 · 本科 | 2022–2026', True),
            ('### | 2022–2026', False),
            ('### ---', False),
            ('#### 合成大学', False),
        ):
            with self.subTest(entry=entry):
                draft = section + entry
                report = f'## Step 4\n```md\n{draft}\n```'
                expected = (draft, 'extracted') if accepted else (None, 'none')
                self.assertEqual(extract_md_block(report, require_step_four=True), expected)

    def test_first_section_with_body_is_not_a_subtitle(self):
        for body, accepted in (
            ('## 个人简介\n擅长库存管理', True),
            ('## 个人简介\n> 引用内容\n擅长库存管理', True),
            ('## 产品经理\n> qa@example.test\n## 工作经历\n负责需求分析', True),
            ('## 产品经理\n> qa@example.test', False),
        ):
            with self.subTest(body=body):
                draft = '# 测试候选人\n\n' + body
                report = f'## Step 4\n```md\n{draft}\n```'
                expected = (draft, 'extracted') if accepted else (None, 'none')
                self.assertEqual(extract_md_block(report, require_step_four=True), expected)


class AnalysisHistoryHttpTests(TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.owner = str(uuid4())
        self.admin = str(uuid4())
        self.analysis_id = str(uuid4())
        self.secret = "analysis-history-preview-secret-with-at-least-48-bytes"
        self.user = InternalUser(user_id=UUID(self.owner), username="alice", email="alice@example.test",
                                 password_hash="PRIVATE", password_salt="PRIVATE", password_version=3,
                                 is_admin=False, is_super_admin=False, status="active", deleted_at=None,
                                 created_at=now, updated_at=now)
        self.admin_user = InternalUser(user_id=UUID(self.admin), username="root", email="admin@example.test",
                                       password_hash="PRIVATE", password_salt="PRIVATE", password_version=2,
                                       is_admin=False, is_super_admin=False, status="active", deleted_at=None,
                                       created_at=now, updated_at=now)
        self.auth = Mock(spec=["find_user_by_id", "authenticate_user"])
        self.auth.find_user_by_id.side_effect = lambda identity: self.admin_user if str(identity) == self.admin else self.user
        self.store = Mock()
        self.service = Mock()
        self.service.admin_emails = frozenset({'admin@example.test'})
        self.service.get_context.return_value = {
            "resume_id": str(uuid4()), "content": "张三\n负责 Java 服务开发", "processed": {},
            "candidate_name": "张三", "analysis": {"final_score": 88, "strengths": ["负责 Java 服务开发"]},
        }
        self.service.list_admin.return_value = {"items": [], "page": 1, "size": 20, "total": 0, "pages": 0}
        self.service.get_admin.return_value = {"analysis_id": self.analysis_id, "status": "completed",
                                               "hr_analysis": {"final_score": 88}}
        self.app = create_preview_app(self.store, self.auth, self.secret,
                                     admin_emails=["admin@example.test"],
                                     analysis_history_service=self.service)
        self.client = self.app.test_client()

    def token(self, user_id, username, version):
        now = time.time()
        return jwt.encode({"user_id": user_id, "username": username, "pwd_ver": version,
                           "iss": "resume-matcher", "jti": str(uuid4()), "iat": now, "exp": now + 3600},
                          self.secret, algorithm="HS256")

    def test_review_markers_reads_authorized_context_and_does_not_call_legacy_store(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        response = self.client.post("/api/v1/resumes/review-markers", headers=headers,
                                    json={"resume_id": str(uuid4()), "analysis_id": self.analysis_id})
        self.assertEqual(response.status_code, 200)
        self.service.get_context.assert_called_once()
        self.assertEqual(self.service.get_context.call_args.kwargs["actor_id"], self.owner)
        self.assertEqual(self.store.mock_calls, [])

    def test_user_history_list_and_detail_forward_tenant_identity(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        self.service.list_user.return_value = {"items": [], "page": 1, "size": 20, "total": 0, "pages": 0}
        self.service.get_user.return_value = {"analysis_id": self.analysis_id, "status": "completed"}
        response = self.client.get("/api/v1/analysis-history?status=completed", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.list_user.call_args.kwargs["actor_id"], self.owner)
        response = self.client.get("/api/v1/analysis-history/" + self.analysis_id, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_user.call_args.kwargs["actor_id"], self.owner)

    def test_improved_markdown_extraction_works_without_database_context(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        response = self.client.post("/api/v1/resumes/improved-markdown", headers=headers,
                                    json={"analysis_result": "```md\n# 张三\n## 技能\n" + '负责 Java 服务开发。' * 30 + "\n```"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["source"], "extracted")
        self.assertEqual(self.service.get_context.call_count, 0)

    def test_optimized_only_request_does_not_fall_back_to_original(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        response = self.client.post('/api/v1/resumes/improved-markdown', headers=headers, json={
            'resume_id': self.owner, 'require_optimized': True,
            'analysis_result': '## Step 3\n```md\n# 原始示例\n## 技能\n' + '原稿内容。' * 40 + '\n```',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data'], {'markdown': '', 'source': 'none', 'sections_detected': 0})
        self.service.get_context.assert_not_called()

    def test_optimized_indented_headings_are_normalized_in_response(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        response = self.client.post('/api/v1/resumes/improved-markdown', headers=headers, json={
            'require_optimized': True,
            'analysis_result': '## Step 4\n```md\n  # 测试稿\n\n  ## 工作经历\n  - 核对库存记录\n```',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data'], {
            'markdown': '# 测试稿\n\n## 工作经历\n  - 核对库存记录',
            'source': 'extracted',
            'sections_detected': 2,
        })

    def test_history_body_limits_do_not_expand_login_or_allow_oversized_tools(self):
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        self.assertEqual(self.client.post('/api/v1/resumes/improved-markdown', headers=headers,
            json={'analysis_result': 'x' * 20000}).status_code, 200)
        self.assertEqual(self.client.post('/api/v1/resumes/improved-markdown', headers=headers,
            json={'analysis_result': 'x' * (2 * 1024 * 1024)}).status_code, 413)
        self.assertEqual(self.client.post('/api/v1/auth/login',
            json={'username': 'alice', 'password': 'x' * 20000}).status_code, 413)

    def test_history_superadmin_policy_must_match_preview(self):
        with self.assertRaises(ValueError):
            create_preview_app(self.store, self.auth, self.secret, analysis_history_service=self.service)

    def test_admin_history_is_scoped_and_regular_user_is_denied(self):
        user_headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        self.assertEqual(self.client.get("/api/v1/admin/analyses", headers=user_headers).status_code, 403)
        admin_headers = {"Authorization": "Bearer " + self.token(self.admin, "root", 2)}
        response = self.client.get("/api/v1/admin/analyses?keyword=张三&page=1&size=20", headers=admin_headers)
        self.assertEqual(response.status_code, 200)
        self.service.list_admin.assert_called_once()
        self.assertEqual(self.service.list_admin.call_args.kwargs["actor_id"], self.admin)

    def test_service_errors_are_fixed_and_never_leak_details(self):
        self.service.get_context.side_effect = RuntimeError("PRIVATE_DATABASE_URL password=secret")
        headers = {"Authorization": "Bearer " + self.token(self.owner, "alice", 3)}
        response = self.client.post("/api/v1/resumes/review-markers", headers=headers,
                                    json={"resume_id": str(uuid4())})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_DATABASE_URL", response.get_data(as_text=True))


if __name__ == "__main__":
    import unittest
    unittest.main()
