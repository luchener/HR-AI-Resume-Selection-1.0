"""Real PDFium checks, including downscaling oversized page geometry."""
import struct
from unittest import TestCase
from unittest.mock import patch
import zlib

from db_original_render import inspect_pdf


def pdf(width=300, height=500, pages=1):
    """Generate a valid, entirely synthetic PDF without another PDF package."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Count %d /Kids [%s] >>' %
                (pages, ' '.join('%d 0 R' % (3 + i * 2) for i in range(pages)))).encode()]
    for i in range(pages):
        objects += [('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R >>' %
                     (width, height, 4 + i * 2)).encode()]
        content = b'0 0 0 rg 5 5 250 250 re f'
        objects += [b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(data)
    data += ('xref\n0 %d\n0000000000 65535 f \n' % len(offsets)).encode()
    data += b''.join(('%010d 00000 n \n' % n).encode() for n in offsets[1:])
    return data + ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' %
                   (len(offsets), start)).encode()


class OriginalRenderTests(TestCase):
    def test_real_pdf_count_png_size_and_nonwhite_pixels(self):
        png, total = inspect_pdf(pdf(), 'pdf', page=1)
        self.assertEqual(total, 1)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', png[16:24]), (600, 1000))
        offset, compressed = 8, b''
        while offset < len(png):
            length = struct.unpack('>I', png[offset:offset + 4])[0]
            if png[offset + 4:offset + 8] == b'IDAT':
                compressed += png[offset + 8:offset + 8 + length]
            offset += length + 12
        raw = zlib.decompress(compressed)
        self.assertIn(b'\0' * 100, raw)
        self.assertIn(b'\xff' * 100, raw)

    def test_huge_page_is_downscaled_below_pixel_limit(self):
        png, _ = inspect_pdf(pdf(10000, 20000), 'pdf', page=1)
        width, height = struct.unpack('>II', png[16:24])
        self.assertTrue(0 < width <= 2200 and 0 < height <= 2200)

    def test_page_cap_total_and_invalid_values(self):
        data = pdf(pages=21)
        self.assertEqual(inspect_pdf(data, 'pdf'), (b'', 21))
        self.assertTrue(inspect_pdf(data, 'pdf', page=20)[0])
        for page in (0, -1, 21, True, '1'):
            with self.subTest(page=page), self.assertRaises(ValueError):
                inspect_pdf(data, 'pdf', page=page)
        self.assertEqual(inspect_pdf(pdf(), 'pdf', page=2), (b'', 1))

    def test_docx_missing_renderer_and_broken_pdf(self):
        self.assertEqual(inspect_pdf(b'PK synthetic', 'docx'), (b'', 0))
        with patch('db_original_render._pdfium', None):
            self.assertEqual(inspect_pdf(pdf(), 'pdf'), (b'', 0))
        with self.assertRaises(Exception):
            inspect_pdf(b'%PDF invalid', 'pdf')
