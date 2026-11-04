# coding=utf-8
"""生成 itch.io 的封面图（630×500）+ 竖版小图（可选）。

用法：python tools/make_itch_cover.py
输入：dist-itch/screenshots/01-lobby.png（没有就退化成纯色背景）
输出：dist-itch/cover-630x500.png
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist-itch")
SRC = os.path.join(OUT, "screenshots", "01-lobby.png")
W, H = 630, 500

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",     # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:                              # noqa: BLE001
                continue
    return ImageFont.load_default()


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    if os.path.isfile(SRC):
        # ⚠️ 2026-11-05：以前整张压暗 55%，月亮被抹平、封面发死黑。
        # 现在取大厅**右侧**（月亮那一块）当底，只轻微压暗，左侧另加渐变给字留位置。
        im = Image.open(SRC).convert("RGB")
        crop = im.crop((int(im.width * 0.58), 0, im.width, int(im.height * 0.78)))
        bg = crop.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(3))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (10, 12, 26)), 0.28)
    else:
        bg = Image.new("RGB", (W, H), (8, 10, 22))

    d = ImageDraw.Draw(bg, "RGBA")
    # 左侧渐变：字区压到接近纯黑，右侧保留月亮
    for x in range(W):
        a = int(232 * max(0.0, 1 - x / 430) ** 1.2)
        d.line([(x, 0), (x, H)], fill=(8, 10, 22, a))
    # 上下压暗：顶部盖掉状态条，底部让信息条不突兀
    d.rectangle([(0, 0), (W, 44)], fill=(8, 10, 22, 246))
    for y in range(H):
        a = int(200 * max(0.0, (y - 230) / (H - 230)) ** 1.3)
        d.line([(0, y), (W, y)], fill=(8, 10, 22, a))

    d.text((48, 84), "A MIDNIGHT DEDUCTION", font=font(13), fill=(196, 176, 128))
    d.text((44, 108), "狼人杀", font=font(84), fill=(238, 232, 218))
    d.text((48, 214), "你坐一席，剩下的交给 AI", font=font(22), fill=(214, 206, 190))
    d.text((48, 248), "11 人局 · 真模型现场发言 · 完整复盘", font=font(17), fill=(150, 156, 172))

    # 底部信息条
    d.rectangle([(0, H - 84), (W, H)], fill=(12, 14, 26))
    d.text((48, H - 66), "WINDOWS · 免费下载", font=font(15), fill=(232, 214, 160))
    d.text((48, H - 42), "离线演练不花钱 · 真模型用你自己的 key", font=font(14), fill=(168, 174, 190))

    path = os.path.join(OUT, "cover-630x500.png")
    bg.save(path, optimize=True)
    print(f"✅ {path}（{os.path.getsize(path)//1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())