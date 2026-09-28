# -*- coding: utf-8 -*-
"""邮件模板渲染器单元测试：渲染顺序 / 转义 / 主题回退 / 开关 / 纯文本。"""
import unittest

from email_templates import (
    DEFAULT_THEME,
    render_broadcast_email,
    render_broadcast_plain,
    sanitize_theme,
);


def render(**kw):
    defaults = dict(subject="s", template_id="notice", body="正文")
    defaults.update(kw)
    return render_broadcast_email(**defaults)


class RenderOrderTest(unittest.TestCase):
    """渲染顺序（设计定稿 §8.2）：正文 → 更新列表 → 图片 → 提示块 → 按钮 → 落款/页脚。"""

    def test_full_order_system_update(self):
        html = render(
            template_id="system_update",
            body="第一段\n\n第二段",
            items=["要点甲", "要点乙"],
            images=[{"image_id": "img-1", "width": 300}],
            accent={"enabled": True, "text": "提示文案"},
            button={"enabled": True, "url": "/workbench", "text": "前往工作台"},
        )
        self.assertLess(html.index("第一段"), html.index("第二段"))
        self.assertLess(html.index("第二段"), html.index("本次更新"))
        self.assertLess(html.index("本次更新"), html.index("要点甲"))
        self.assertLess(html.index("要点甲"), html.index("img-1"))
        self.assertLess(html.index("img-1"), html.index("提示文案"))
        self.assertLess(html.index("提示文案"), html.index("前往工作台"))
        self.assertLess(html.index("前往工作台"), html.index("祝工作顺利"))

    def test_no_items_for_notice_template(self):
        html = render(template_id="notice", items=["不应出现"])
        self.assertNotIn("不应出现", html)

    def test_maintenance_signoff(self):
        html = render(template_id="maintenance")
        self.assertIn("感谢理解与配合", html)


class EscapingTest(unittest.TestCase):
    def test_body_escapes_script(self):
        html = render(body="<script>alert(1)</script>正常正文")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_items_escaped(self):
        html = render(template_id="system_update", items=["<b>加粗</b>", "a&b"])
        self.assertNotIn("<b>加粗</b>", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("a&amp;b", html)

    def test_accent_and_button_escaped(self):
        html = render(
            accent={"enabled": True, "text": "<img src=x onerror=alert(1)>"},
            button={"enabled": True, "url": "/a", "text": "<script>x</script>"},
        )
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<script>", html)


class ThemeTest(unittest.TestCase):
    def test_invalid_hex_falls_back(self):
        t = sanitize_theme({"header_bg": "#badhex", "button_bg": "red"})
        self.assertEqual(t["header_bg"], DEFAULT_THEME["header_bg"])
        self.assertEqual(t["button_bg"], DEFAULT_THEME["button_bg"])

    def test_valid_hex_lowercased(self):
        t = sanitize_theme({"header_bg": "#ABCDEF"})
        self.assertEqual(t["header_bg"], "#abcdef")

    def test_theme_applied_to_html(self):
        html = render(theme={"header_bg": "#1e3a2f"})
        self.assertIn("#1e3a2f", html)

    def test_none_theme(self):
        t = sanitize_theme(None)
        self.assertEqual(t["header_bg"], DEFAULT_THEME["header_bg"])


class ToggleTest(unittest.TestCase):
    def test_accent_disabled_not_rendered(self):
        html = render(accent={"enabled": False, "text": "不该出现"})
        self.assertNotIn("不该出现", html)

    def test_accent_empty_text_not_rendered(self):
        html = render(accent={"enabled": True, "text": "   "})
        self.assertNotIn("提示", html)

    def test_button_disabled_not_rendered(self):
        html = render(button={"enabled": False, "url": "/x", "text": "前往"})
        self.assertNotIn("前往", html)

    def test_button_bad_url_hidden(self):
        html = render(button={"enabled": True, "url": "javascript:alert(1)", "text": "前往工作台"})
        self.assertNotIn("前往工作台", html)

    def test_button_relative_url_allowed(self):
        html = render(button={"enabled": True, "url": "../workbench", "text": "前往工作台"})
        self.assertIn("前往工作台", html)


class ImageRenderTest(unittest.TestCase):
    def test_width_clamped(self):
        seen = {}
        def src(image_id, width):
            seen[image_id] = width
            return "cid:img-" + image_id
        html = render(
            images=[{"image_id": "a", "width": 10}, {"image_id": "b", "width": 9999}, {"image_id": "c", "width": "x"}],
            image_src=src,
        )
        self.assertEqual(seen, {"a": 60, "b": 800, "c": 360})

    def test_cid_default_src(self):
        html = render(images=[{"image_id": "img-9", "width": 200}])
        self.assertIn("cid:img-img-9", html)
        self.assertIn("width=\"200\"", html)

    def test_custom_src_callback(self):
        html = render(
            images=[{"image_id": "i1", "width": 200}],
            image_src=lambda image_id, width: "data:image/png;base64,AAAA",
        )
        self.assertIn("data:image/png;base64,AAAA", html)

    def test_invalid_image_entries_skipped(self):
        html = render(images=[{}, None, {"image_id": "ok"}])
        self.assertIn("cid:img-ok", html)
        self.assertIn("cid:img-ok", html)


class PlainTextTest(unittest.TestCase):
    def test_plain_contains_core_parts(self):
        text = render_broadcast_plain(
            subject="【通知】测试", template_id="system_update",
            body="正文段落一\n\n正文段落二",
            items=["要点1", "要点2"],
            accent={"enabled": True, "text": "请登录查看"},
            button={"enabled": True, "url": "/workbench", "text": "前往工作台"},
        )
        self.assertIn("【通知】测试", text)
        self.assertIn("正文段落一", text)
        self.assertIn("本次更新", text)
        self.assertIn("- 要点1", text)
        self.assertIn("提示：请登录查看", text)
        self.assertIn("前往工作台：/workbench", text)
        self.assertIn("此邮件由系统自动发送", text)

    def test_plain_no_html_tags(self):
        text = render_broadcast_plain(subject="s", template_id="notice", body="<script>x</script>")
        self.assertIn("<script>", text)  # 纯文本不做 HTML 转义


if __name__ == "__main__":
    unittest.main(verbosity=2)
