# coding=utf-8
"""生成默认背景音乐（无歌词、无版权纠纷 —— 这段音频是**本项目自己合成**的）。

作者 2026-11-03 要求：设置里可开关、可导入自己的音乐；默认打开，内置一小段轻音乐循环。
这里不下载任何外部素材，纯 numpy 合成一段 64 秒的舒缓氛围：
  · 和弦进行 Cmaj7 → Am7 → Fmaj7 → G6（8 小节 × 8 秒）；
  · 铺底 pad + 低音 + 稀疏琶音，轻微混响；
  · 首尾各 0.8 秒淡入淡出，适合循环播放。

输出：web/audio/bgm.wav（22.05kHz / 单声道 / 16-bit，约 2.8MB）。

跑法：python tools/make_bgm.py
"""
from __future__ import annotations

import os
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "web", "audio")
OUT = os.path.join(OUT_DIR, "bgm.wav")

SR = 22050
BAR = 8.0                      # 每小节 8 秒（慢）
BARS = 8                       # 两轮进行 = 64 秒
DUR = BAR * BARS

# 频率表（A4=440）
def hz(name: str) -> float:
    table = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}
    note = name[0].upper()
    rest = name[1:]
    octave = int(rest.replace("#", "").replace("b", ""))
    semis = table[note] + (12 * (octave - 4))
    if "#" in rest:
        semis += 1
    if "b" in rest:
        semis -= 1
    return 440.0 * (2 ** (semis / 12))


CHORDS = [
    ["C4", "E4", "G4", "B4"],          # Cmaj7
    ["A3", "C4", "E4", "G4"],          # Am7
    ["F3", "A3", "C4", "E4"],          # Fmaj7
    ["G3", "B3", "D4", "E4"],          # G6
]
ROOTS = ["C2", "A1", "F2", "G2"]


def env_adsr(n: int, attack: float, release: float, sr: int = SR) -> np.ndarray:
    a = max(1, int(attack * sr))
    r = max(1, int(release * sr))
    e = np.ones(n, dtype=np.float64)
    e[:min(a, n)] = np.linspace(0.0, 1.0, min(a, n))
    tail = min(r, n)
    e[n - tail:] *= np.linspace(1.0, 0.0, tail)
    return e


def pad_note(freq: float, n: int, amp: float) -> np.ndarray:
    t = np.arange(n) / SR
    vib = 1.0 + 0.0008 * np.sin(2 * np.pi * 0.15 * t)
    w = (np.sin(2 * np.pi * freq * t * vib)
         + 0.18 * np.sin(2 * np.pi * freq * 2 * t * vib)
         + 0.08 * np.sin(2 * np.pi * freq * 3 * t * vib))
    return amp * w * env_adsr(n, attack=1.6, release=2.4)


def pluck(freq: float, n: int, amp: float) -> np.ndarray:
    t = np.arange(n) / SR
    w = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2 * t)
    return amp * w * np.exp(-t / 0.45)


def reverb(x: np.ndarray) -> np.ndarray:
    out = x.copy()
    for delay_ms, gain in ((31, 0.24), (37, 0.20), (43, 0.16), (53, 0.12)):
        d = int(SR * delay_ms / 1000)
        tail = np.zeros_like(x)
        tail[d:] = x[:-d] * gain
        out += tail
    return out


def main() -> int:
    n_total = int(DUR * SR)
    mix = np.zeros(n_total, dtype=np.float64)
    bar_n = int(BAR * SR)

    for bar in range(BARS):
        chord = CHORDS[bar % len(CHORDS)]
        root = ROOTS[bar % len(ROOTS)]
        start = bar * bar_n
        seg = slice(start, min(start + bar_n, n_total))
        n = seg.stop - seg.start

        # 铺底和弦（每个音错开一点点进入，听着更"呼吸"）
        for i, note in enumerate(chord):
            offset = int((0.12 * i) * SR)
            nn = max(1, n - offset)
            buf = pad_note(hz(note), nn, amp=0.16 / len(chord))
            mix[start + offset:start + offset + nn] += buf

        # 低音
        bass = pad_note(hz(root), n, amp=0.14)
        mix[seg] += bass

        # 稀疏琶音：每 0.5 秒一个音（和弦音往上两个八度）
        step = 0.5
        k = 0
        while k * step < BAR:
            note = chord[k % len(chord)]
            f = hz(note) * 2
            off = int(k * step * SR)
            nn = int(min(1.6, BAR - k * step) * SR)
            if nn > 0 and start + off < n_total:
                buf = pluck(f, nn, amp=0.07)
                end = min(start + off + nn, n_total)
                mix[start + off:end] += buf[:end - (start + off)]
            k += 1

    mix = reverb(mix)

    # 首尾淡入淡出（循环时不至于"啪"一下）
    fade = int(0.8 * SR)
    mix[:fade] *= np.linspace(0.0, 1.0, fade)
    mix[-fade:] *= np.linspace(1.0, 0.0, fade)

    peak = float(np.max(np.abs(mix))) or 1.0
    mix = mix / peak * 0.70                     # 留 headroom，别削波

    os.makedirs(OUT_DIR, exist_ok=True)
    pcm = np.clip(mix, -1.0, 1.0)
    data = (pcm * 32767).astype("<i2")
    with wave.open(OUT, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(data.tobytes())

    print(f"[OK] {OUT}  {DUR:.0f}s / {SR}Hz / mono  "
          f"{os.path.getsize(OUT) / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())