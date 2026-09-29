# -*- coding: utf-8 -*-
"""验证码图片渲染：纯标准库出 PNG，不依赖 Pillow。

背景：/api/v1/auth/captcha 原先把四位数字明文随响应返回（前端自绘），任何脚本读一个
字段就能过人机校验，验证码等于不存在。改成后端出图后，答案只以 sha256 落盘，
响应里只有图片字节。

实现：
- 手写 PNG 编码（IHDR + IDAT + IEND，zlib 压缩 + CRC32），RGB8。
- 5x7 点阵字形，逐字符随机旋转 / 抖动 / 缩放，叠加干扰线与噪点。
- 渲染随机数用 SystemRandom，不可预测。
"""

import base64
import math
import random
import struct
import zlib

# 5x7 点阵数字字形（0/1 表示该像素是否点亮）
_FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}

WIDTH = 160
HEIGHT = 52
_GLYPH_W = 5
_GLYPH_H = 7
_MARGIN_X = 14
_GAP = 6

# 主色 #1b2a45 附近的深色，随机抖动
_FG_BASE = (27, 42, 69)


class _Canvas:
    """RGB 像素缓冲。"""

    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 3)
        for y in range(height):
            row = y * width * 3
            for x in range(width):
                i = row + x * 3
                self.pixels[i] = background[0]
                self.pixels[i + 1] = background[1]
                self.pixels[i + 2] = background[2]

    def _blend(self, x: int, y: int, color: tuple[int, int, int], alpha: float = 1.0) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height) or alpha <= 0:
            return
        i = (y * self.width + x) * 3
        if alpha >= 1.0:
            self.pixels[i] = color[0]
            self.pixels[i + 1] = color[1]
            self.pixels[i + 2] = color[2]
            return
        inv = 1.0 - alpha
        self.pixels[i] = int(self.pixels[i] * inv + color[0] * alpha)
        self.pixels[i + 1] = int(self.pixels[i + 1] * inv + color[1] * alpha)
        self.pixels[i + 2] = int(self.pixels[i + 2] * inv + color[2] * alpha)

    def fill_block(self, x: int, y: int, size: int, color: tuple[int, int, int]) -> None:
        for dy in range(size):
            for dx in range(size):
                self._blend(x + dx, y + dy, color)

    def line(self, x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int], alpha: float = 0.5) -> None:
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for step in range(steps + 1):
            t = step / steps if steps else 0.0
            self._blend(int(round(x0 + (x1 - x0) * t)), int(round(y0 + (y1 - y0) * t)), color, alpha)

    def dot(self, x: int, y: int, color: tuple[int, int, int], alpha: float = 0.5) -> None:
        self._blend(x, y, color, alpha)


def _draw_digit(canvas: _Canvas, char: str, center_x: float, angle: float, scale: float, color: tuple[int, int, int]) -> None:
    glyph = _FONT.get(char)
    if not glyph:
        return
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    center_y = canvas.height / 2.0
    size = max(2, int(round(scale)))
    for row_index, row in enumerate(glyph):
        for col_index, cell in enumerate(row):
            if cell != "1":
                continue
            # 点阵坐标 → 以字模中心为原点的局部坐标
            local_x = (col_index - (_GLYPH_W - 1) / 2.0) * scale
            local_y = (row_index - (_GLYPH_H - 1) / 2.0) * scale
            x = center_x + local_x * cos_a - local_y * sin_a
            y = center_y + local_x * sin_a + local_y * cos_a
            canvas.fill_block(int(round(x)), int(round(y)), size, color)


def render_png(code: str) -> bytes:
    """把验证码渲染成 PNG 字节。"""
    rng = random.SystemRandom()

    # 背景带极轻微色偏，避免"纯白"这个可被简单阈值分割的强特征
    background = (rng.randint(244, 255), rng.randint(244, 255), rng.randint(244, 255))
    canvas = _Canvas(WIDTH, HEIGHT, background)

    # 干扰线（浅灰，画在字符下层）
    for _ in range(rng.randint(3, 5)):
        tint = rng.randint(150, 205)
        canvas.line(
            rng.randint(0, WIDTH),
            rng.randint(0, HEIGHT),
            rng.randint(0, WIDTH),
            rng.randint(0, HEIGHT),
            (tint, tint, tint),
            alpha=0.55,
        )

    # 字符：逐个随机旋转 / 缩放 / 抖动
    scales = [rng.uniform(3.4, 4.6) for _ in code]
    total = int(sum(_GLYPH_W * s for s in scales) + _GAP * max(0, len(code) - 1))
    cursor = max(_MARGIN_X, (WIDTH - total) / 2.0)
    for char, scale in zip(code, scales):
        shift_x = rng.uniform(-1.5, 1.5)
        angle = math.radians(rng.uniform(-20.0, 20.0))
        color = tuple(
            max(0, min(255, channel + rng.randint(-25, 25))) for channel in _FG_BASE
        )
        _draw_digit(
            canvas,
            char,
            cursor + _GLYPH_W * scale / 2.0 + shift_x,
            angle,
            scale,
            color,
        )
        cursor += _GLYPH_W * scale + _GAP

    # 噪点（画在字符上层，少量，避免糊掉字形）
    for _ in range(rng.randint(120, 200)):
        tint = rng.randint(140, 220)
        canvas.dot(rng.randrange(WIDTH), rng.randrange(HEIGHT), (tint, tint, tint), alpha=0.6)

    return encode_png(canvas)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(canvas: _Canvas) -> bytes:
    """手写 PNG 编码：RGB8，无行过滤。"""
    raw = bytearray()
    stride = canvas.width * 3
    for y in range(canvas.height):
        raw.append(0)  # filter type: None
        raw += canvas.pixels[y * stride : (y + 1) * stride]
    header = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def render_data_uri(code: str) -> str:
    """渲染成可直接塞进 <img src> 的 data URI。"""
    return "data:image/png;base64," + base64.b64encode(render_png(code)).decode("ascii")
