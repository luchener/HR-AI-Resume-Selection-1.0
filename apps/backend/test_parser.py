"""Regression tests for untrusted PDF/DOCX parser resource limits."""
import io
import unittest
import zipfile
from unittest import mock

import parser
from pdfminer.cmapdb import CMapDB


_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body><w:p><w:r><w:t>张三 Python 工程师</w:t></w:r></w:p></w:body>'
    '</w:document>'
).encode("utf-8")


def _make_docx(document=_DOCUMENT, extras=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        for name, data in extras:
            archive.writestr(name, data)
    return stream.getvalue()


class ParserLimitTests(unittest.TestCase):
    def test_valid_docx_still_extracts_text(self):
        self.assertIn("张三", parser.extract_docx(_make_docx()))

    def test_docx_entry_count_is_bounded(self):
        extras = [(f"word/extra-{index}.xml", b"") for index in range(parser.MAX_DOCX_ENTRIES)]
        with self.assertRaises(parser.DocumentTooLarge):
            parser.extract_docx(_make_docx(extras=extras))

    def test_docx_expanded_size_and_compression_ratio_are_bounded(self):
        bomb = [(f"word/media/bomb-{index}.bin", b"\0" * (1024 * 1024)) for index in range(5)]
        with self.assertRaises(parser.DocumentTooLarge):
            parser.extract_docx(_make_docx(extras=bomb))

    def test_docx_rejects_dtd_and_entities(self):
        document = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>'
        ).encode("utf-8")
        with self.assertRaises(parser.InvalidDocument):
            parser.extract_docx(_make_docx(document=document))

    def test_deeply_nested_docx_does_not_recurse(self):
        nested = "<w:p>" * 2000 + "<w:r><w:t>deep</w:t></w:r>" + "</w:p>" * 2000
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{nested}</w:body></w:document>'
        ).encode("utf-8")
        self.assertIn("deep", parser.extract_docx(_make_docx(document=document)))

    def test_pdf_page_limit_is_enforced_before_extraction(self):
        with mock.patch.object(parser, "_pdf_page_count", return_value=parser.MAX_PDF_PAGES + 1), \
                mock.patch.object(parser, "extract_text") as extract:
            with self.assertRaises(parser.DocumentTooLarge):
                parser.extract_pdf(b"%PDF-1.7")
        extract.assert_not_called()

    def test_pdf_extraction_passes_maxpages_and_caps_text(self):
        oversized = "X" * (parser.MAX_EXTRACTED_TEXT_CHARS + 100)
        with mock.patch.object(parser, "_pdf_page_count", return_value=1), \
                mock.patch.object(parser, "extract_text", return_value=oversized) as extract:
            text = parser.extract_text_from_file(b"%PDF-1.7", "application/pdf")
        self.assertEqual(len(text), parser.MAX_EXTRACTED_TEXT_CHARS)
        self.assertEqual(extract.call_args.kwargs["maxpages"], parser.MAX_PDF_PAGES)

    def test_oversized_upload_is_rejected_before_parsing(self):
        with self.assertRaises(parser.DocumentTooLarge):
            parser.extract_text_from_file(b"X" * (parser.MAX_UPLOAD_BYTES + 1), "application/pdf")

    def test_pdf_cmap_names_cannot_escape_packaged_directory(self):
        for name in ("../cmapdb", "..\\cmapdb", "/etc/passwd", "C:\\Windows\\win.ini"):
            with self.subTest(name=name), self.assertRaises(CMapDB.CMapNotFound):
                CMapDB._load_data(name)


if __name__ == "__main__":
    unittest.main()
