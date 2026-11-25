"""Bounded bytes-only PDF rendering; no legacy configuration or file access.

PDFium calls are serialized because the native library is not thread safe.
This is a local rehearsal renderer, not an untrusted-document process sandbox.
"""
import math
from contextlib import closing
import struct
from threading import Lock
import zlib

try:
    import pypdfium2 as _pdfium
except ImportError:
    _pdfium = None

_LOCK = Lock()
MAX_PAGES = 20
MAX_SIDE = 2200


def _chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)


def _png(bitmap):
    width, height = int(bitmap.width), int(bitmap.height)
    channels, stride = int(bitmap.n_channels), int(bitmap.stride)
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and channels in (1, 3, 4)
            and width * channels <= stride <= MAX_SIDE * 4 + 16):
        raise ValueError('Unsupported bitmap')
    raw = bytes(bitmap.buffer)
    if len(raw) != stride * height:
        raise ValueError('Invalid bitmap buffer')
    rows = bytearray()
    for y in range(height):
        row = bytearray(raw[y * stride:y * stride + width * channels])
        if str(bitmap.mode).upper().startswith('BGR'):
            row[0::channels], row[2::channels] = row[2::channels], row[0::channels]
        if channels == 4:
            row = bytearray(value for i, value in enumerate(row) if i % 4 != 3)
        rows += b'\0' + row
    header = struct.pack('>IIBBBBB', width, height, 8, 0 if channels == 1 else 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(rows)) + _chunk(b'IEND', b'')


def inspect_pdf(data, ext, *, page=None):
    """Return (PNG or empty bytes, page count); absent renderer yields zero.

    Only pages 1..20 can be rendered. Metadata reports the actual total.
    Corrupt documents and unsupported geometry fail closed at the service.
    """
    if ext != 'pdf' or _pdfium is None:
        return b'', 0
    if page is not None and (type(page) is not int or not 1 <= page <= MAX_PAGES):
        raise ValueError('Invalid original page')
    with _LOCK, closing(_pdfium.PdfDocument(data)) as doc:
        total = len(doc)
        if page is None or page > total:
            return b'', total
        with closing(doc[page - 1]) as pdf_page:
            width, height = map(float, pdf_page.get_size())
            if not all(math.isfinite(x) and 0 < x <= 1e7 for x in (width, height)):
                raise ValueError('Unsupported page geometry')
            # Leave rounding margin, and allow scales BELOW one for huge pages.
            scale = min(2.0, (MAX_SIDE - 2) / max(width, height))
            with closing(pdf_page.render(scale=scale)) as bitmap:
                return _png(bitmap), total
