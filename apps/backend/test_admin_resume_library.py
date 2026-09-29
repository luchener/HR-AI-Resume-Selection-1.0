# -*- coding: utf-8 -*-
"""用户简历库（仅超级管理员）单元测试（test_admin_resume_library.py）

验证 docs/ARCHITECTURE.md「用户简历库」：
- 超级管理员可列出全部用户的简历、可查看原文详情
- 普通管理员 / 普通用户 / 匿名分别 403 / 403 / 401
- 列表不泄漏原文；详情接口没有任何下载语义（无 Content-Disposition、无下载路由）
- 每次查看与删除都写管理操作审计（resume_view / resume_delete）
- 手动删除与用户自助删除同语义：原文物理删除 + 引用它的归档移入回收站
- 30 天自动清理由 retention 统一负责（本套件只验证「已被删掉的在库里也看不到」）

运行：python -m unittest test_admin_resume_library -v
"""

import io
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

# ── 目录隔离：很多模块的 *_DIR 是 import 时快照，只改 config.DATA_DIR 不够 ──
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp-admin-library")
os.environ["ENV"] = "local"

import config  # noqa: E402
import auth  # noqa: E402
import store  # noqa: E402
import resume_original  # noqa: E402

_DIR_NAMES = (
    "users",
    "password_resets",
    "resumes",
    "jobs",
    "archives",
    "invite_requests",
    "invite_codes",
    "invite_codes_audit",
    "login_failures",
    "rate_limits",
    "admin_ops",
    "usage",
    "resume_originals",
)


def _isolate():
    """把 config / auth / store 三层里所有 *_DIR 常量按名字映射到临时目录。"""
    config.DATA_DIR = _TMP
    for name in _DIR_NAMES:
        os.makedirs(os.path.join(_TMP, name), exist_ok=True)
    for mod in (config, auth, store):
        for attr in [a for a in dir(mod) if a.endswith("_DIR")]:
            base = os.path.basename(getattr(mod, attr) or "")
            if base in _DIR_NAMES:
                setattr(mod, attr, os.path.join(_TMP, base))
    auth._INDEX_FILE = os.path.join(auth.USERS_DIR, "_index.json")
    auth._INDEX_LOCK = os.path.join(auth.USERS_DIR, "_index.lock")
    store.ARCHIVE_INDEX_PATH = os.path.join(store.ARCHIVES_DIR, "_index.json")
    config.PRUNE_TOUCH_FILE = os.path.join(_TMP, ".last_prune")
    config.LOG_DIR = os.path.join(_TMP, "logs")
    os.makedirs(config.LOG_DIR, exist_ok=True)


_isolate()

# 超级管理员 = 邮箱命中白名单；固定成测试域名，避免依赖本机 .env
_SUPER_EMAIL = "super_lib@example.com"
auth.ADMIN_EMAILS = {_SUPER_EMAIL}

import retention  # noqa: E402
import app as backend  # noqa: E402

backend.config.CAPTCHA_ENABLED = False


def _clear(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        return
    for name in os.listdir(path):
        try:
            os.remove(os.path.join(path, name))
        except OSError:
            pass


class AdminResumeLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = backend.app.test_client()
        _clear(auth.USERS_DIR)
        sup, err = auth.create_user("super_lib", "password123", _SUPER_EMAIL)
        assert sup, f"super admin create failed: {err}"
        cls.sup_token = auth.generate_jwt(sup["user_id"], sup["username"])
        mid, merr = auth.create_user("mid_lib", "password123", "mid_lib@example.com")
        assert mid, f"mid admin create failed: {merr}"
        mid["is_admin"] = True
        auth._write_json(os.path.join(auth.USERS_DIR, f"{mid['user_id']}.json"), mid)
        cls.mid_token = auth.generate_jwt(mid["user_id"], mid["username"])
        u1, e1 = auth.create_user("user_alpha", "password123", "alpha@example.com")
        u2, e2 = auth.create_user("user_beta", "password123", "beta@example.com")
        assert u1 and u2, f"{e1} {e2}"
        cls.u1, cls.u2 = u1, u2
        cls.normal_token = auth.generate_jwt(u1["user_id"], u1["username"])

    def setUp(self):
        for path in (store.RESUMES_DIR, store.ARCHIVES_DIR, auth.ADMIN_OPS_DIR):
            _clear(path)
        self.r1 = store.save_resume("张三\nPython 后端工程师", {}, self.u1["user_id"])
        self.r2 = store.save_resume("李四\n产品经理", {}, self.u2["user_id"])

    def _get(self, url, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get(url, headers=headers)

    def _delete(self, url, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.delete(url, headers=headers)

    # ── 权限门 ────────────────────────────────────────────────────────
    def test_super_admin_lists_all_users_resumes(self):
        resp = self._get("/api/v1/admin/resumes?size=50", self.sup_token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()["data"]
        self.assertEqual(data["total"], 2)
        owners = {item["owner_username"] for item in data["items"]}
        self.assertEqual(owners, {"user_alpha", "user_beta"})
        ids = {item["resume_id"] for item in data["items"]}
        self.assertEqual(ids, {self.r1, self.r2})
        for item in data["items"]:
            self.assertGreater(item["chars"], 0)
            self.assertEqual(item["residue_lines_removed"], 0, "正常简历不得被剔除任何行")
            self.assertNotIn("content", item, "列表不得返回原文内容")

    def test_normal_admin_forbidden(self):
        resp = self._get("/api/v1/admin/resumes", self.mid_token)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("仅超级管理员", resp.get_json()["detail"])

    def test_normal_user_forbidden(self):
        resp = self._get("/api/v1/admin/resumes", self.normal_token)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_unauthorized(self):
        resp = self._get("/api/v1/admin/resumes")
        self.assertEqual(resp.status_code, 401)

    def test_normal_admin_cannot_view_or_delete(self):
        self.assertEqual(self._get(f"/api/v1/admin/resumes/{self.r1}", self.mid_token).status_code, 403)
        self.assertEqual(self._delete(f"/api/v1/admin/resumes/{self.r1}", self.mid_token).status_code, 403)
        self.assertIsNotNone(store.get_resume(self.r1), "越权请求不得造成任何删改")

    # ── 只读查看 ──────────────────────────────────────────────────────
    def test_detail_returns_content_without_download_semantics(self):
        resp = self._get(f"/api/v1/admin/resumes/{self.r1}", self.sup_token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()["data"]
        self.assertIn("Python", data["content"])
        self.assertEqual(data["owner_username"], "user_alpha")
        self.assertEqual(data["chars"], len(data["content"]))
        self.assertIsNone(resp.headers.get("Content-Disposition"), "详情接口不得带下载语义")

    def test_no_download_route_exists(self):
        """
        简历库不得有任何下载/导出入口；原件只能逐页取图，且路由必须是只读 GET。

        「不可下载」= 不存在 download / export / file / raw / attachment 语义的路由，
        且原件路由只挂 GET（页面图片另见响应头断言：inline，无 attachment）。
        """
        rules = [r.rule for r in backend.app.url_map.iter_rules() if "resumes" in r.rule]
        banned = [
            r for r in rules
            if "download" in r or "export" in r or "file" in r or "raw" in r or "attachment" in r
        ]
        self.assertEqual(banned, [], f"简历相关路由不得包含下载/导出入口：{banned}")
        original_rules = sorted(r for r in rules if "original" in r)
        self.assertEqual(
            original_rules,
            [
                "/api/v1/admin/resumes/<resume_id>/original",
                "/api/v1/admin/resumes/<resume_id>/original/pages/<int:page>",
            ],
            f"原件路由集合发生变化，请确认仍未引入下载入口：{rules}",
        )
        for rule in backend.app.url_map.iter_rules():
            if rule.rule in original_rules:
                self.assertFalse(
                    set(rule.methods or set()) - {"GET", "HEAD", "OPTIONS"},
                    f"{rule.rule} 必须是只读路由",
                )

    def _seed_original(self, resume_id):
        """按上传链路的顺序落一份原件：先存文件，再把元信息写进简历记录。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "sample_en.pdf")
        if not os.path.exists(path):
            self.skipTest("缺少 testdata/sample_en.pdf")
        with open(path, "rb") as handle:
            meta = resume_original.save(resume_id, handle.read(), "王龙龙-运维工程师.pdf")
        if meta:
            store.set_resume_original(resume_id, meta)
        return meta

    def test_original_requires_super_admin(self):
        resume_original.save(self.r1, b"%PDF-1.4 fake", "a.pdf")
        for token, expected in ((self.mid_token, 403), (self.normal_token, 403), (None, 401)):
            self.assertEqual(
                self._get(f"/api/v1/admin/resumes/{self.r1}/original", token).status_code, expected
            )
            self.assertEqual(
                self._get(f"/api/v1/admin/resumes/{self.r1}/original/pages/1", token).status_code, expected
            )

    def test_original_pages_are_inline_png_without_download(self):
        meta = self._seed_original(self.r1)
        self.assertTrue(meta, "原件应留存成功")

        resp = self._get(f"/api/v1/admin/resumes/{self.r1}/original", self.sup_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertTrue(data["available"])
        self.assertEqual(data["ext"], "pdf")
        self.assertEqual(data["name"], "王龙龙-运维工程师.pdf")
        self.assertEqual(data["page_count"], 2)
        self.assertEqual(data["sha256"], meta["sha256"])
        self.assertIsNone(resp.headers.get("Content-Disposition"), "元信息接口不得带下载语义")

        page = self._get(f"/api/v1/admin/resumes/{self.r1}/original/pages/1", self.sup_token)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("Content-Type"), "image/png")
        self.assertEqual(page.headers.get("Content-Disposition"), "inline")
        # 全局 after_request 对 /api/v1/* 统一覆盖为 no-store（比自定义值更严）
        self.assertIn("no-store", page.headers.get("Cache-Control") or "")
        self.assertEqual(page.headers.get("Accept-Ranges"), "none")
        self.assertEqual(page.headers.get("X-Original-Page"), "1")
        self.assertEqual(page.headers.get("X-Original-Pages"), "2")
        self.assertTrue(page.get_data().startswith(b"\x89PNG\r\n\x1a\n"), "必须返回图片字节")

        self.assertEqual(
            self._get(f"/api/v1/admin/resumes/{self.r1}/original/pages/9", self.sup_token).status_code,
            404,
        )
        # 列表摘要给出快捷标记，省掉一次探测请求
        items = self._get("/api/v1/admin/resumes?size=50", self.sup_token).get_json()["data"]["items"]
        row = next(item for item in items if item["resume_id"] == self.r1)
        self.assertTrue(row["original_available"])
        # 每次查看都写审计：谁、哪份、第几页
        ops = auth.list_admin_ops(op="resume_original_view")
        details = " ".join(op["detail"] for op in ops)
        self.assertIn("stage=meta", details)
        self.assertIn("stage=page", details)

    def test_upload_keeps_original_file(self):
        """上传链路必须留存原件，且用户自助删除时原件同步清除。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "sample_en.pdf")
        if not os.path.exists(path):
            self.skipTest("缺少 testdata/sample_en.pdf")
        with open(path, "rb") as handle:
            data = handle.read()
        resp = self.client.post(
            "/api/v1/resumes/upload",
            data={"file": (io.BytesIO(data), "王龙龙-运维工程师.pdf")},
            headers={"Authorization": f"Bearer {self.normal_token}"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertTrue(payload.get("original_saved"), "上传应留存原始文件")
        resume_id = payload["resume_id"]
        record = store.get_resume(resume_id, user_id=self.u1["user_id"]) or {}
        self.assertEqual((record.get("original") or {}).get("name"), "王龙龙-运维工程师.pdf")
        self.assertEqual((record.get("original") or {}).get("ext"), "pdf")
        self.assertTrue(resume_original.exists(resume_id))

        deleted = self._delete(f"/api/v1/resumes/{resume_id}", self.normal_token)
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        self.assertFalse(resume_original.exists(resume_id), "删除简历必须同步删除原件")

    def test_original_unavailable_for_history_resumes(self):
        resp = self._get(f"/api/v1/admin/resumes/{self.r2}/original", self.sup_token)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["data"]["available"])
        self.assertEqual(
            self._get(f"/api/v1/admin/resumes/{self.r2}/original/pages/1", self.sup_token).status_code,
            404,
        )
        items = self._get("/api/v1/admin/resumes?size=50", self.sup_token).get_json()["data"]["items"]
        row = next(item for item in items if item["resume_id"] == self.r2)
        self.assertFalse(row["original_available"])
        # 没有原件可看时不应产生查看审计
        self.assertEqual(auth.list_admin_ops(op="resume_original_view"), [])

    def test_detail_404_for_unknown_resume(self):
        resp = self._get("/api/v1/admin/resumes/no-such-resume", self.sup_token)
        self.assertEqual(resp.status_code, 404)

    def test_view_is_audited(self):
        self._get(f"/api/v1/admin/resumes/{self.r1}", self.sup_token)
        ops = auth.list_admin_ops(op="resume_view")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["operator_name"], "super_lib")
        self.assertEqual(ops[0]["target_username"], "user_alpha")
        self.assertIn(self.r1, ops[0]["detail"])

    # ── 手动删除 ──────────────────────────────────────────────────────
    def test_delete_removes_original_and_trashes_archives(self):
        archive_id = store.save_archive(
            user_id=self.u1["user_id"],
            resume_id=self.r1,
            job_id="job-1",
            candidate_name="张三",
            final_score=88,
            fit_tag="匹配",
            recruitment_recommendation="建议面试",
            job_title="后端工程师",
        )
        resp = self._delete(f"/api/v1/admin/resumes/{self.r1}", self.sup_token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["data"]["archived_to_trash"], 1)
        self.assertIsNone(store.get_resume(self.r1), "原文应被物理删除")
        self.assertIsNone(store.get_resume(self.r1, user_id=self.u1["user_id"]))
        self.assertEqual(store.get_archive(archive_id, self.u1["user_id"])["status"], "trashed")
        ops = auth.list_admin_ops(op="resume_delete")
        self.assertEqual(len(ops), 1)
        self.assertIn("trashed_archives=1", ops[0]["detail"])

    def test_delete_then_absent_from_library(self):
        self._delete(f"/api/v1/admin/resumes/{self.r2}", self.sup_token)
        data = self._get("/api/v1/admin/resumes", self.sup_token).get_json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual([i["resume_id"] for i in data["items"]], [self.r1])

    def test_delete_404_for_unknown_resume(self):
        self.assertEqual(self._delete("/api/v1/admin/resumes/nope", self.sup_token).status_code, 404)

    # ── 检索与分页 ────────────────────────────────────────────────────
    def test_keyword_and_user_filter(self):
        by_name = self._get("/api/v1/admin/resumes?keyword=李四", self.sup_token).get_json()["data"]
        self.assertEqual([i["resume_id"] for i in by_name["items"]], [self.r2])
        by_owner = self._get("/api/v1/admin/resumes?keyword=user_alpha", self.sup_token).get_json()["data"]
        self.assertEqual([i["resume_id"] for i in by_owner["items"]], [self.r1])
        by_user = self._get(
            f"/api/v1/admin/resumes?user_id={self.u2['user_id']}", self.sup_token
        ).get_json()["data"]
        self.assertEqual([i["resume_id"] for i in by_user["items"]], [self.r2])

    def test_pagination(self):
        data = self._get("/api/v1/admin/resumes?page=2&size=1", self.sup_token).get_json()["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["page"], 2)

    def test_user_self_delete_also_removes_from_library(self):
        """用户自助删除原文后，库里同步消失（同一份数据，不是副本）。"""
        resp = self._delete(f"/api/v1/resumes/{self.r1}", self.normal_token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = self._get("/api/v1/admin/resumes", self.sup_token).get_json()["data"]
        self.assertEqual([i["resume_id"] for i in data["items"]], [self.r2])
        self.assertIsNone(store.get_resume(self.r1))

    def test_retention_purge_also_removes_from_library(self):
        """30 天保留期自动清理后，库里同步消失（走同一条 store.delete_resume）。"""
        path = os.path.join(store.RESUMES_DIR, f"{self.r1}.json")
        old = store._read_json(path)
        old["created_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        store._write_json(path, old)
        report = retention.purge()
        self.assertEqual(report["resumes"], 1)
        data = self._get("/api/v1/admin/resumes", self.sup_token).get_json()["data"]
        self.assertEqual([i["resume_id"] for i in data["items"]], [self.r2])

    # ── PDF 解析残留（pdfminer 字形码）：姓名与原文标注 ─────────────────
    _JUNK = (
        "Rj\n\nR\n\nQ\n\nH\n\nG q n P\n\nO\n\nP q a W\n\nY 2 9 W\n\n"
        "w\n\nX\n\nF B\n\nN -0t6 0 G\n\nM\n\n名：王龙龙\n\n汪林军\n\n"
        "男 | 年龄：31岁 |\n\n个人优势\n\n"
    )

    def _save_junk_resume(self, user_id):
        return store.save_resume(self._JUNK, {}, user_id)

    def _archive(self, user_id, resume_id, name="汪林军"):
        return store.save_archive(
            user_id=user_id,
            resume_id=resume_id,
            job_id=f"job-{resume_id[:8]}",
            candidate_name=name,
            final_score=40,
            fit_tag="一般",
            recruitment_recommendation="待定",
            job_title="Java 开发",
        )

    def test_candidate_name_skips_pdf_junk_lines(self):
        """残留操作符行不得被当成姓名；能认出被拆开的「名：王龙龙」。"""
        self.assertEqual(
            backend._candidate_name_from_resume({"content": self._JUNK, "processed": {}}, {}),
            "王龙龙",
        )

    def test_candidate_name_never_returns_operator_line(self):
        junk_only = "Rj\n\nR\n\nQ\n\nH\n\nG q n P\n\nO\n\nP q a W\n\nY 2 9 W\n"
        self.assertEqual(
            backend._candidate_name_from_resume({"content": junk_only, "processed": {}}, {}),
            "未识别姓名",
        )

    def test_content_suspect_flags_junk_but_not_clean_text(self):
        self.assertTrue(backend._content_suspect(self._JUNK))
        self.assertFalse(backend._content_suspect("张三\nPython 后端工程师\n5 年经验\n负责订单系统"))
        self.assertFalse(backend._content_suspect(""))

    def test_list_prefers_archive_name_over_garbled_content(self):
        """原文被解析残留污染时，列表用归档里的分析姓名，而不是乱码首行。"""
        rid = self._save_junk_resume(self.u1["user_id"])
        self._archive(self.u1["user_id"], rid)
        items = self._get("/api/v1/admin/resumes?size=50", self.sup_token).get_json()["data"]["items"]
        row = next(i for i in items if i["resume_id"] == rid)
        self.assertEqual(row["candidate_name"], "汪林军")
        self.assertTrue(row["content_suspect"])
        self.assertGreater(row["residue_lines_removed"], 0, "列表应报告自动剔除的残留行数")

    def test_list_never_shows_section_heading_as_name(self):
        """清洗后章节标题成为首行时，列表不得把它显示为候选人姓名。"""
        rid = store.save_resume(
            "个人优势\n\n1.熟悉常见通讯协议及网络协议\n\n2.熟练掌握 Linux 命令及系统安装\n\n3.责任心强，适应出差",
            {},
            self.u1["user_id"],
        )
        items = self._get("/api/v1/admin/resumes?size=50", self.sup_token).get_json()["data"]["items"]
        row = next(i for i in items if i["resume_id"] == rid)
        self.assertEqual(row["candidate_name"], "未识别姓名")

    def test_detail_flags_suspect_and_uses_archive_name(self):
        rid = self._save_junk_resume(self.u2["user_id"])
        self._archive(self.u2["user_id"], rid)
        data = self._get(f"/api/v1/admin/resumes/{rid}", self.sup_token).get_json()["data"]
        self.assertEqual(data["candidate_name"], "汪林军")
        self.assertTrue(data["content_suspect"])
        self.assertEqual(data["archived_count"], 1)
        # 详情返回的正文必须已自动过滤：乱码行消失、中文完整
        self.assertGreater(data["residue_lines_removed"], 0)
        self.assertNotIn("Rj", data["content"])
        self.assertNotIn("G q n P", data["content"])
        self.assertIn("王龙龙", data["content"])
        self.assertIn("个人优势", data["content"])
        self.assertEqual(data["chars"], len(data["content"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
