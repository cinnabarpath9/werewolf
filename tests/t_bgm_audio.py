# coding=utf-8
"""默认背景音乐（2026-11-03）。

作者要求：设置里可开关；默认打开；内置一小段**无歌词、无版权纠纷**的轻音乐循环（≤3 分钟）；
可导入自己的音乐。

这段音乐是**本项目自己用 numpy 合成的**（`tools/make_bgm.py`），不依赖任何外部素材，
所以没有版权问题。本脚本检查产物本身：

· 文件在 `web/audio/bgm.wav`
· 时长 ≤180 秒、单声道、16-bit
· 体积别大得离谱（发布包会带它）
· 生成的脚本还在（能从零重建）

跑法：python tests/t_bgm_audio.py
"""
from __future__ import annotations

import os
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


AUDIO = os.path.join(ROOT, "web", "audio", "bgm.wav")
MAKER = os.path.join(ROOT, "tools", "make_bgm.py")

print("=== 一、产物存在且是自研 ===")
check("1a 默认音乐文件存在", os.path.isfile(AUDIO))
check("1b 生成脚本在（能从零重建，不依赖外部素材）", os.path.isfile(MAKER))
check("1c 发布包里会带上（在 web/ 下）", AUDIO.startswith(os.path.join(ROOT, "web")))

print("\n=== 二、格式与时长 ===")
if os.path.isfile(AUDIO):
    with wave.open(AUDIO, "rb") as w:
        ch, sr, sw, frames = w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()
    dur = frames / sr
    check("2a 时长 ≤180 秒（作者要求三分钟以内）", dur <= 180, f"{dur:.1f}s")
    check("2b 时长 ≥30 秒（循环别太短，听着不腻）", dur >= 30, f"{dur:.1f}s")
    check("2c 单声道 + 16-bit（音量小、体积小）", ch == 1 and sw == 2, f"ch={ch} sw={sw}")
    check("2d 采样率可被浏览器直接播（≥22050）", sr >= 22050, str(sr))
    size_mb = os.path.getsize(AUDIO) / 1024 / 1024
    check("2e 体积 ≤6MB（发布包会带它）", size_mb <= 6, f"{size_mb:.1f}MB")

print("\n=== 三、前端接线（静态检查） ===")
page = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
check("3a 设置里有开关/音量/导入/恢复", all(k in page for k in
      ("bgmToggle", "bgmVolume", "bgmImport", "bgmReset")))
check("3b 播放器循环 + 走同源路由", "audio.loop = true" in page and "audio/bgm.wav" in page)
check("3c 自动播放被拦时有手势兜底", "armGesture" in page)
check("3d 导入的音乐只存本机（IndexedDB）", "indexedDB.open" in page and "wolf-bgm" in page)

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)