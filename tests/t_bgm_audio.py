# coding=utf-8
"""默认背景音乐（2026-11-03 第二版：改用公有领域的现成录音）。

作者要求：别自己合成了 —— 去公共音乐领域找**无版权纠纷、可商用、舒缓、无歌词、评价高**的曲子，
时长十分钟以内，文件 ≤10MB，音质尽量好。

选定：**Erik Satie — Gymnopédie No. 1**（作品 1888 年，公有领域），
演奏/录音 Teknopazzo（Wikimedia Commons，**CC0 1.0**）；
由 ffmpeg 从原始 Ogg FLAC（3:24.8）转码为 **MP3 192kbps / 44.1kHz / 立体声**。
来源与许可见 `web/audio/CREDITS.md`。

本脚本只做**离线校验**（不联网、不依赖第三方库）：
· 文件在 `web/audio/bgm.mp3`、许可见 CREDITS；
· 时长 ≤600 秒、体积 ≤10MB；
· 能解析出合法 MP3 帧（说明不是改名的假文件）。

跑法：python tests/t_bgm_audio.py
"""
from __future__ import annotations

import os
import struct
import sys

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


AUDIO = os.path.join(ROOT, "web", "audio", "bgm.mp3")
CREDITS = os.path.join(ROOT, "web", "audio", "CREDITS.md")

# MPEG1 Layer3 的码率/采样率表
_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_SRATES = [44100, 48000, 32000]


def mp3_info(path: str) -> tuple[float, int, int, int]:
    """返回 (时长秒, 采样率, 声道, 码率kbps)。按首帧头 + 文件大小估算（CBR 足够准）。"""
    data = open(path, "rb").read()
    off = 0
    if data[:3] == b"ID3":                       # 跳过 ID3v2
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
               ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        off = 10 + size
        if data[5] & 0x10:                       # footer
            off += 10
    idx = data.find(b"\xff", off)
    if idx < 0 or idx + 4 > len(data):
        raise ValueError("找不到 MP3 帧头")
    hdr = struct.unpack(">I", data[idx:idx + 4])[0]
    if (hdr >> 21) & 0x7FF != 0x7FF:
        raise ValueError("帧头同步字不对")
    br_idx = (hdr >> 12) & 0xF
    sr_idx = (hdr >> 10) & 0x3
    pad = (hdr >> 9) & 0x1
    ch = 1 if ((hdr >> 6) & 0x3) == 3 else 2
    bitrate = _BITRATES[br_idx] * 1000
    sr = _SRATES[sr_idx]
    if not bitrate or not sr:
        raise ValueError("码率/采样率非法")
    frame_len = 144 * bitrate // sr + pad
    frames = (len(data) - idx) / frame_len
    return frames * 1152 / sr, sr, ch, bitrate // 1000


print("=== 一、文件与许可 ===")
check("1a 默认音乐文件存在（mp3）", os.path.isfile(AUDIO))
check("1b 版权说明在（来源/作者/许可）", os.path.isfile(CREDITS))
cred = open(CREDITS, encoding="utf-8").read() if os.path.isfile(CREDITS) else ""
check("1c 明确写了 CC0 / 公有领域", ("CC0" in cred) and ("公有领域" in cred or "Public domain" in cred))
check("1d 写了来源链接", "commons.wikimedia.org" in cred)
check("1e 写了曲目与演奏者", "Satie" in cred and "Teknopazzo" in cred)
check("1f 旧的合成曲已移除（避免两首并存）",
      not os.path.isfile(os.path.join(ROOT, "web", "audio", "bgm.wav")))

print("\n=== 二、时长 / 体积 / 格式（作者口径：≤10 分钟、≤10MB、音质尽量好） ===")
if os.path.isfile(AUDIO):
    try:
        dur, sr, ch, kbps = mp3_info(AUDIO)
    except ValueError as exc:
        # 坏文件/改名假文件：走 FAIL，而不是让整个脚本 traceback 崩掉（审查 P2）
        check("2* 能解析出合法 MP3 帧", False, str(exc))
        dur, sr, ch, kbps = 0, 0, 0, 0
    else:
        check("2* 能解析出合法 MP3 帧", True)
    size_mb = os.path.getsize(AUDIO) / 1024 / 1024
    check("2a 时长 ≤600 秒（十分钟以内）", dur <= 600, f"{dur:.1f}s")
    check("2b 体积 ≤10MB", size_mb <= 10, f"{size_mb:.2f}MB")
    check("2c 纯器乐（钢琴曲，CREDITS 已注明）", "钢琴" in cred or "piano" in cred.lower())
    check("2d 码率 ≥128kbps（音质别太差）", kbps >= 128, f"{kbps}kbps")
    check("2e 采样率 44.1kHz（浏览器通吃）", sr == 44100, str(sr))
    check("2f 立体声", ch == 2, str(ch))

print("\n=== 三、前端接线（静态检查） ===")
page = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
check("3a 设置里有开关/音量/导入/恢复",
      all(k in page for k in ("bgmToggle", "bgmVolume", "bgmImport", "bgmReset")))
check("3b 默认曲目指向 bgm.mp3", "audio/bgm.mp3" in page)
check("3c 循环播放", "audio.loop = true" in page)
check("3d 自动播放被拦时有手势兜底", "armGesture" in page)
check("3e 导入的音乐只存本机（IndexedDB）", "indexedDB.open" in page and "wolf-bgm" in page)

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)