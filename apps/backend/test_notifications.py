# -*- coding: utf-8 -*-
"""通知公告 + 邮件群发 核心接口测试（unittest，零 SMTP 依赖）。"""
import io
import json
import os
import shutil
import uuid
import unittest
from unittest import mock

import app as backend
import config as backend_config
import notifications as notify_mod
import auth as auth_mod


class NotificationApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.app.config["TESTING"] = True
        backend_config.CAPTCHA_ENABLED = False
        _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test-tmp")
        os.makedirs(_base, exist_ok=True)
        cls._tmp = os.path.join(_base, "notify-" + uuid.uuid4().hex[:10])
        os.makedirs(cls._tmp, exist_ok=True)
        cls._old = {
            "ANNOUNCEMENTS_DIR": backend_config.ANNOUNCEMENTS_DIR,
            "ANNOUNCEMENT_READS_DIR": backend_config.ANNOUNCEMENT_READS_DIR,
            "EMAIL_IMAGES_DIR": backend_config.EMAIL_IMAGES_DIR,
            "EMAIL_LOGS_DIR": backend_config.EMAIL_LOGS_DIR,
        }
        backend_config.ANNOUNCEMENTS_DIR = os.path.join(cls._tmp, "announcements")
        backend_config.ANNOUNCEMENT_READS_DIR = os.path.join(cls._tmp, "reads")
        backend_config.EMAIL_IMAGES_DIR = os.path.join(cls._tmp, "images")
        backend_config.EMAIL_LOGS_DIR = os.path.join(cls._tmp, "logs")
        for d in (backend_config.ANNOUNCEMENTS_DIR, backend_config.ANNOUNCEMENT_READS_DIR,
                  backend_config.EMAIL_IMAGES_DIR, backend_config.EMAIL_LOGS_DIR):
            os.makedirs(d, exist_ok=True)
        cls.client = backend.app.test_client()
        suf = uuid.uuid4().hex[:6]
        cls.admin_name = "notifyadmin" + suf
        cls.user_name = "notifyuser" + suf
        ok, err = auth_mod.create_user(cls.admin_name, "Admin@12345", cls.admin_name + "@test.local")
        assert ok, "create admin failed: %s" % err
        auth_mod.update_user_admin(cls.admin_name, True, operator="test")
        ok, err = auth_mod.create_user(cls.user_name, "User@12345", cls.user_name + "@test.local")
        assert ok, "create user failed: %s" % err
        resp = cls.client.post("/api/v1/auth/login", json={"username": cls.admin_name, "password": "Admin@12345"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cls.admin_token = resp.get_json()["data"]["token"]
        resp = cls.client.post("/api/v1/auth/login", json={"username": cls.user_name, "password": "User@12345"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cls.user_token = resp.get_json()["data"]["token"]

    @classmethod
    def tearDownClass(cls):
        for key, val in cls._old.items():
            setattr(backend_config, key, val)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _h(self, token):
        return {"Authorization": "Bearer " + token}

    def test_announcement_lifecycle(self):
        resp = self.client.post("/api/v1/admin/announcements", json={
            "title": "系统维护公告",
            "content": "本周日 02:00-04:00 维护，期间分析服务不可用。",
            "start_mode": "now",
            "duration_value": 2,
            "duration_unit": "hour",
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        aid = resp.get_json()["data"]["announcement_id"]
        resp = self.client.get("/api/v1/admin/announcements", headers=self._h(self.admin_token))
        items = resp.get_json()["data"]["items"]
        self.assertTrue(any(i["announcement_id"] == aid and i["status"] == "active" for i in items))
        resp = self.client.get("/api/v1/notifications/announcements", headers=self._h(self.user_token))
        got = resp.get_json()["data"]["items"]
        self.assertTrue(any(i["announcement_id"] == aid for i in got), "user should see active announcement")
        resp = self.client.post("/api/v1/notifications/announcements/%s/dismiss" % aid, headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/v1/notifications/announcements", headers=self._h(self.user_token))
        self.assertFalse(any(i["announcement_id"] == aid for i in resp.get_json()["data"]["items"]))
        resp = self.client.post("/api/v1/admin/announcements/%s/cancel" % aid, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/v1/admin/announcements", headers=self._h(self.admin_token))
        item = next(i for i in resp.get_json()["data"]["items"] if i["announcement_id"] == aid)
        self.assertEqual(item["status"], "cancelled")

    def test_announcement_validation_and_delay(self):
        resp = self.client.post("/api/v1/admin/announcements", json={
            "title": "", "content": "x", "start_mode": "now", "duration_value": 1, "duration_unit": "hour"
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post("/api/v1/admin/announcements", json={
            "title": "延迟公告", "content": "稍后出现", "start_mode": "delay",
            "delay_value": 5, "delay_unit": "minute", "duration_value": 1, "duration_unit": "hour"
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 201)
        rec = notify_mod.get_announcement(resp.get_json()["data"]["announcement_id"])
        self.assertEqual(notify_mod.effective_status(rec), "scheduled")

    def _mk_announcement(self, title):
        resp = self.client.post("/api/v1/admin/announcements", json={
            "title": title, "content": "正文", "start_mode": "now",
            "duration_value": 1, "duration_unit": "hour",
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["data"]["announcement_id"]

    def test_announcement_pagination(self):
        before = notify_mod.list_announcements(page=1, size=10 ** 4)["total"]
        for i in range(3):
            self._mk_announcement("分页测试%d" % i)
        d1 = notify_mod.list_announcements(page=1, size=2)
        self.assertEqual(d1["total"], before + 3)
        self.assertEqual(len(d1["items"]), 2)
        # 第 2 页有剩余 1 条
        last_page = (before + 3 + 1) // 2
        d2 = notify_mod.list_announcements(page=last_page, size=2)
        self.assertGreaterEqual(len(d2["items"]), 1)
        # 固定窗口：page 越界返回空 items
        d3 = notify_mod.list_announcements(page=999, size=10)
        self.assertEqual(d3["items"], [])
        # size 钳制到最小值 1
        d4 = notify_mod.list_announcements(page=1, size=0)
        self.assertEqual(len(d4["items"]), 1)

    def test_announcement_delete_keeps_record(self):
        aid = self._mk_announcement("待删除公告")
        # 用户可见
        resp = self.client.get("/api/v1/notifications/announcements", headers=self._h(self.user_token))
        self.assertTrue(any(i["announcement_id"] == aid for i in resp.get_json()["data"]["items"]))
        # 删除
        resp = self.client.delete("/api/v1/admin/announcements/%s" % aid, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        # 列表不再展示
        resp = self.client.get("/api/v1/admin/announcements", headers=self._h(self.admin_token))
        self.assertFalse(any(i["announcement_id"] == aid for i in resp.get_json()["data"]["items"]))
        # 记录仍保留在磁盘（软删除标记）
        rec = notify_mod.get_announcement(aid)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.get("deleted_at"))
        # 用户端不再弹出
        resp = self.client.get("/api/v1/notifications/announcements", headers=self._h(self.user_token))
        self.assertFalse(any(i["announcement_id"] == aid for i in resp.get_json()["data"]["items"]))
        # 审计记录
        ops = auth_mod.list_admin_ops(op="announcement_delete", page=1, size=20)
        self.assertTrue(any("待删除公告" in (o.get("detail") or "") for o in ops))

    def test_email_log_detail_renders(self):
        old = (backend_config.SMTP_HOST, backend_config.SMTP_USER, backend_config.SMTP_PASSWORD)
        backend_config.SMTP_HOST = "smtp.test.local"
        backend_config.SMTP_USER = "test@test.local"
        backend_config.SMTP_PASSWORD = "test-password"
        user = auth_mod.find_user_by_username(self.user_name)
        try:
            with mock.patch("notifications.mailer.send_broadcast_email", return_value=None):
                summary = notify_mod.send_notification_email(
                    subject="历史详情测试", template_id="system_update", body="正文段落",
                    items=["要点A"], accent={"enabled": True, "text": "提示"},
                    button={"enabled": True, "url": "/", "text": "前往工作台"},
                    target="selected", user_ids=[user["user_id"]],
                    operator_id="op", operator_name="op",
                )
            detail = notify_mod.get_email_log_detail(summary["email_log_id"])
            self.assertIsNotNone(detail)
            rec = detail["record"]
            self.assertEqual(rec["subject"], "历史详情测试")
            self.assertEqual(rec["sent"], 1)
            snap = rec["compose_snapshot"]
            self.assertEqual(snap["template_id"], "system_update")
            self.assertEqual(snap["items"], ["要点A"])
            self.assertIn("本次更新", detail["html"])
            self.assertIn("前往工作台", detail["html"])
            # 不存在 id
            self.assertIsNone(notify_mod.get_email_log_detail("missing-000000000000"))
        finally:
            backend_config.SMTP_HOST, backend_config.SMTP_USER, backend_config.SMTP_PASSWORD = old

    def test_user_cannot_access_admin_announcements(self):
        resp = self.client.get("/api/v1/admin/announcements", headers=self._h(self.user_token))
        self.assertEqual(resp.status_code, 403)

    def test_email_preview(self):
        resp = self.client.post("/api/v1/admin/notifications/email/preview", json={
            "subject": "【系统更新】测试预览",
            "template_id": "system_update",
            "body": "您好：\n\n简历智选 v1.1 已发布。",
            "items": ["新增公告", "新增邮件通知"],
            "accent": {"enabled": True, "text": "请登录查看。"},
            "button": {"enabled": True, "url": "/", "text": "前往工作台"},
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        html = resp.get_json()["data"]["html"]
        self.assertIn("AI 简历智选", html)
        self.assertIn("本次更新", html)
        self.assertIn("前往工作台", html)

    def test_email_preview_escapes_xss(self):
        resp = self.client.post("/api/v1/admin/notifications/email/preview", json={
            "subject": "s", "template_id": "notice",
            "body": "<script>alert(1)</script>正常正文",
        }, headers=self._h(self.admin_token))
        html = resp.get_json()["data"]["html"]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_image_upload_preview_delete(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        resp = self.client.post("/api/v1/admin/notifications/email/image",
            data={"file": (io.BytesIO(png), "banner.png")},
            content_type="multipart/form-data",
            headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        image_id = resp.get_json()["data"]["image_id"]
        resp = self.client.get("/api/v1/admin/notifications/email/images", headers=self._h(self.admin_token))
        self.assertTrue(any(i["image_id"] == image_id for i in resp.get_json()["data"]["items"]))
        resp = self.client.post("/api/v1/admin/notifications/email/preview", json={
            "subject": "带图", "template_id": "notice", "body": "正文段落",
            "images": [{"image_id": image_id, "width": 200}],
        }, headers=self._h(self.admin_token))
        html = resp.get_json()["data"]["html"]
        self.assertIn("data:image/png;base64", html)
        self.assertIn("width=\"200\"", html)
        resp = self.client.delete("/api/v1/admin/notifications/email/image/%s" % image_id, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/v1/admin/notifications/email/images", headers=self._h(self.admin_token))
        self.assertFalse(any(i["image_id"] == image_id for i in resp.get_json()["data"]["items"]))

    def test_image_rejects_non_image(self):
        resp = self.client.post("/api/v1/admin/notifications/email/image",
            data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "evil.png")},
            content_type="multipart/form-data",
            headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 422)

    def test_recipients(self):
        resp = self.client.get("/api/v1/admin/notifications/email/recipients", headers=self._h(self.admin_token))
        data = resp.get_json()["data"]
        self.assertGreaterEqual(data["with_email"], 2)
        self.assertGreaterEqual(data["total_users"], 2)

    def test_send_requires_smtp(self):
        backend_config.SMTP_HOST = ""
        backend_config.SMTP_PASSWORD = ""
        resp = self.client.post("/api/v1/admin/notifications/email", json={
            "subject": "s", "template_id": "notice", "body": "b", "target": "all"
        }, headers=self._h(self.admin_token))
        self.assertEqual(resp.status_code, 503)
    def test_resolve_recipients_all_and_selected(self):
        # 复用 setUpClass 的用户（notifyadmin*/notifyuser* 均有邮箱），再建一个无邮箱用户
        noemail_name = "notifynoemail" + uuid.uuid4().hex[:6]
        ok, err = auth_mod.create_user(noemail_name, "User@12345", "")
        self.assertTrue(ok, err)
        noemail = auth_mod.find_user_by_username(noemail_name)
        admin = auth_mod.find_user_by_username(self.admin_name)
        user = auth_mod.find_user_by_username(self.user_name)

        # all：只含绑定邮箱的用户
        all_users = notify_mod.resolve_recipients("all", None)
        ids = [u["user_id"] for u in all_users]
        self.assertIn(admin["user_id"], ids)
        self.assertIn(user["user_id"], ids)
        self.assertNotIn(noemail["user_id"], ids)

        # selected：去重 + 跳过无邮箱/不存在 id，仅保留选中且有邮箱者
        sel = notify_mod.resolve_recipients("selected", [
            user["user_id"], admin["user_id"], noemail["user_id"],
            "ghost-0000000000", user["user_id"],  # 重复 id 只算一次
        ])
        sel_ids = [u["user_id"] for u in sel]
        self.assertEqual(sorted(sel_ids), sorted([admin["user_id"], user["user_id"]]))
        self.assertEqual(len(sel_ids), 2)

    def test_send_selected_target_with_mock_smtp(self):
        # 临时伪造 SMTP 配置（不真实发信），mock mailer 验证"指定发送只发给选中者"
        old = (backend_config.SMTP_HOST, backend_config.SMTP_USER, backend_config.SMTP_PASSWORD)
        backend_config.SMTP_HOST = "smtp.test.local"
        backend_config.SMTP_USER = "test@test.local"
        backend_config.SMTP_PASSWORD = "test-password"
        admin = auth_mod.find_user_by_username(self.admin_name)
        user = auth_mod.find_user_by_username(self.user_name)
        try:
            with mock.patch("notifications.mailer.send_broadcast_email", return_value=None) as m:
                summary = notify_mod.send_notification_email(
                    subject="指定发送测试", template_id="notice", body="正文",
                    target="selected",
                    user_ids=[user["user_id"], admin["user_id"], "ghost-0000000000", user["user_id"]],
                    operator_id="op", operator_name="op",
                )
                self.assertEqual(summary["target_count"], 2)
                self.assertEqual(summary["sent"], 2)
                self.assertEqual(summary["skipped"], 0)
                self.assertEqual(summary["failed"], 0)
                # 恰好发给两位（去重后），不存在 id 不参与
                sent_emails = sorted(call.args[0] for call in m.call_args_list)
                self.assertEqual(sent_emails, sorted([admin["email"].lower(), user["email"].lower()]))
                # 日志落盘到隔离目录
                log_path = os.path.join(backend_config.EMAIL_LOGS_DIR, summary["email_log_id"] + ".json")
                self.assertTrue(os.path.exists(log_path))
        finally:
            backend_config.SMTP_HOST, backend_config.SMTP_USER, backend_config.SMTP_PASSWORD = old


    def test_admin_ops_recorded(self):
        self.client.post("/api/v1/admin/announcements", json={
            "title": "审计公告", "content": "x", "start_mode": "now",
            "duration_value": 1, "duration_unit": "hour",
        }, headers=self._h(self.admin_token))
        ops = auth_mod.list_admin_ops(op="announcement_create", page=1, size=20)
        self.assertTrue(all(o["op"] == "announcement_create" for o in ops))
        self.assertGreaterEqual(len(ops), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
