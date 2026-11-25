"""Focused regressions for name extraction and DOCX preview rendering."""
import io
import unittest
import zipfile

from docx_preview import render_docx_html
from resume_names import candidate_name_from_text, resolve_candidate_name

DOC = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>工作经历</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>某某公司</w:t></w:r><w:r><w:t> 后端开发</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>负责接口开发</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>说明 &lt;script&gt;</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>'''


class CandidateNameTests(unittest.TestCase):
    def test_explicit_label(self):
        self.assertEqual(candidate_name_from_text('姓名：汪佳伟\n电话：13800000000'), '汪佳伟')

    def test_first_clean_line(self):
        self.assertEqual(candidate_name_from_text('汪佳伟\n男 | 29岁 | 嘉兴'), '汪佳伟')

    def test_section_heading_is_not_a_name(self):
        self.assertEqual(candidate_name_from_text('个人优势\n熟悉 Python'), '')

    def test_contact_only_text_has_no_name(self):
        self.assertEqual(candidate_name_from_text('电话 13800000000\n邮箱 test@example.com'), '')

    def test_table_and_pdf_labels_preserve_the_full_name(self):
        for text in (
            '| **姓名** | 张 三 | 电话 | 13800000000 |',
            '个人简历\n姓 名\n张 三\n联系方式',
            '基本信息\n名：张 三 电话：13800000000',
            '姓名：张 三 13800000000',
        ):
            with self.subTest(text=text):
                self.assertEqual(candidate_name_from_text(text), '张三')

    def test_english_full_name_is_not_cut_at_first_space(self):
        self.assertEqual(candidate_name_from_text('Name: Jane Doe | Email: jane@example.com'), 'Jane Doe')

    def test_blank_name_never_uses_an_adjacent_field_or_section(self):
        for text in (
            '| 姓名 | | 电话 | 13800000000 |',
            '| 姓名 | |\n| 电话 | 13800000000 |',
            '姓名：\n联系方式\n13800000000',
            '姓名：保密\n电话：13800000000',
            'Name:\nKey Skills',
        ):
            with self.subTest(text=text):
                self.assertEqual(candidate_name_from_text(text), '')
                self.assertEqual(resolve_candidate_name(text=text, filename='张三.pdf'), '张三')

    def test_only_a_candidate_name_label_matches(self):
        self.assertEqual(candidate_name_from_text('Company Name: Acme\nName: Jane Doe'), 'Jane Doe')
        self.assertEqual(candidate_name_from_text('电话：13800000000\t姓名\t张 三'), '张三')

    def test_analysis_name_fills_a_gap_without_overriding_trusted_or_labeled_names(self):
        self.assertEqual(resolve_candidate_name('张三', fallback_names=('李四',)), '张三')
        self.assertEqual(resolve_candidate_name(text='姓名：张三', fallback_names=('李四',)), '张三')
        self.assertEqual(resolve_candidate_name(text='Software Engineer', fallback_names=('李四',)), '李四')

    def test_known_name_and_filename_fallback_skip_placeholders(self):
        self.assertEqual(resolve_candidate_name('未提供', ' 李四 ', text='姓名：张三'), '李四')
        self.assertEqual(resolve_candidate_name('未识别姓名', text='个人优势', filename='【前端工程师】张三_5年.pdf'), '张三')
        self.assertEqual(resolve_candidate_name(filename='个人简历（张三）.docx'), '张三')
        self.assertEqual(resolve_candidate_name(text='SQL AWS\n个人优势', filename='个人简历_2026.pdf'), '')
        self.assertEqual(resolve_candidate_name(filename='张三_李四.docx'), '')


class DocxPreviewTests(unittest.TestCase):
    def setUp(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', DOC)
        self.html = render_docx_html(buffer.getvalue())

    def test_structure(self):
        self.assertIn('<h2>工作经历</h2>', self.html)
        self.assertIn('<strong>某某公司</strong>', self.html)
        self.assertIn('<li>负责接口开发</li>', self.html)
        self.assertIn('<table>', self.html)

    def test_escapes_untrusted_text(self):
        self.assertIn('&lt;script&gt;', self.html)
        self.assertNotIn('<script>', self.html)


if __name__ == '__main__':
    unittest.main()
