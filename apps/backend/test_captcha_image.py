# -*- coding: utf-8 -*-
"""验证码图片渲染测试（test_captcha_image.py）。

不依赖任何图像库：PNG 结构用 zlib + struct 独立解码校验（不是调用被测代码自证），
像素统计用来兜住"全黑 / 全白 / 没画上字"这类回归。

运行：python -m unittest test_captcha_image -v
"""

import base64
import struct
import unittest
import zlib

import captcha_image


def _decode_png(data: bytes) -> dict:
    """最小 PNG 解码器：只支持 RGB8 / filter=0，用于校验编码结果。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 签名错误"
    offset = 8
    chunks = []
    width = height = depth = color_type = -1
    idat = b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"{tag!r} CRC 校验失败"
        chunks.append(tag.decode("ascii"))
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", payload[:10])
        elif tag == b"IDAT":
            idat += payload
        offset += 12 + length
    raw = zlib.decompress(idat)
    return {
        "width": width,
        "height": height,
        "depth": depth,
        "color_type": color_type,
        "chunks": chunks,
        "raw": raw,
    }


class CaptchaImageTests(unittest.TestCase):
    def test_png_is_structurally_valid(self):
        png = captcha_image.render_png("7305")
        info = _decode_png(png)
        self.assertEqual(info["chunks"][0], "IHDR")
        self.assertEqual(info["chunks"][-1], "IEND")
        self.assertIn("IDAT", info["chunks"])
        self.assertEqual((info["width"], info["height"]), (captcha_image.WIDTH, captcha_image.HEIGHT))
        self.assertEqual(info["depth"], 8)
        self.assertEqual(info["color_type"], 2)  # RGB
        # 每行 1 字节 filter + width*3 字节像素，且 filter 恒为 0
        stride = info["width"] * 3
        self.assertEqual(len(info["raw"]), info["height"] * (stride + 1))
        for y in range(info["height"]):
            self.assertEqual(info["raw"][y * (stride + 1)], 0)

    def test_dark_pixel_ratio_in_sane_band(self):
        """全白（没画上）/ 全黑（背景坏了）都必须被抓出来。"""
        for code in ("0000", "1234", "9999", "5678"):
            png = captcha_image.render_png(code)
            raw = _decode_png(png)["raw"]
            stride = captcha_image.WIDTH * 3
            dark = 0
            total = captcha_image.WIDTH * captcha_image.HEIGHT
            for y in range(captcha_image.HEIGHT):
                row = raw[y * (stride + 1) + 1 : y * (stride + 1) + 1 + stride]
                for x in range(captcha_image.WIDTH):
                    i = x * 3
                    luminance = row[i] * 0.299 + row[i + 1] * 0.587 + row[i + 2] * 0.114
                    if luminance < 140:
                        dark += 1
            ratio = dark / total
            self.assertGreater(ratio, 0.02, f"{code} 几乎没有深色像素：{ratio:.3f}")
            self.assertLess(ratio, 0.45, f"{code} 深色像素过多（背景坏了？）：{ratio:.3f}")

    def test_render_is_randomized(self):
        """同一答案两次渲染必须不同：防"抓一次图就给全网复用"。"""
        first = captcha_image.render_png("4321")
        second = captcha_image.render_png("4321")
        self.assertNotEqual(first, second)

    def test_data_uri_round_trip(self):
        uri = captcha_image.render_data_uri("8080")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        payload = base64.b64decode(uri.split(",", 1)[1])
        self.assertEqual(_decode_png(payload)["width"], captcha_image.WIDTH)

    def test_font_covers_all_digits(self):
        self.assertEqual(set(captcha_image._FONT), set("0123456789"))
        for char, glyph in captcha_image._FONT.items():
            self.assertEqual(len(glyph), 7, char)
            for row in glyph:
                self.assertEqual(len(row), 5, char)
                self.assertTrue(set(row) <= {"0", "1"}, char)

    def test_unknown_char_is_ignored(self):
        """字形表里没有的字符只跳过、不抛异常（防御性）。"""
        png = captcha_image.render_png("1x2y")
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
