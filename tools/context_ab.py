# coding=utf-8
"""往日发言压缩的 A/B 量测（离线部分：省了多少字/ token）。

用法
----
    python tools/context_ab.py                 # 最近 8 局
    python tools/context_ab.py --games 20      # 指定局数
    python tools/context_ab.py --save games/game-xxx.json

只测**台账渲染**这一块（历史部分）：
- `off`：往日发言全文（现状）；
- `extract`：往日发言只留"身份声明窗口 + 最后一句"。

输出每天两版的字数与估计 token（中文 ≈ 1.5 字/token），以及总节省比例。

⚠️ 这不是"棋力"量测。棋力要看：
   ① 事实错误（需要模型判定）② 票型命中 ③ 人类读感。
   那三项要跑真模型，脚本另外提供 `--dump` 把两版提示包写出来，方便人工/模型对照。
   默认**不开** `game.context_compress`，等这三项在真实存档上量过再决定。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.ledger import Ledger                                  # noqa: E402


def chars_tokens(text: str) -> tuple[int, int]:
    n = len(text)
    return n, max(1, round(n / 1.5))


def measure(path: str) -> list[dict]:
    data = json.load(io.open(path, encoding="utf-8"))
    rows = data.get("ledger") or []
    if not rows:
        return []
    led = Ledger.from_list(rows)
    days = sorted({int(r.get("day") or 0) for r in rows})
    seats = sorted({int(r["seat"]) for r in rows if r.get("seat")}) or [1]
    seat = seats[0]
    out = []
    for day in days:
        if day < 2:
            continue
        full = led.render(seat, "good", seats, [], day, compress="off")
        extr = led.render(seat, "good", seats, [], day, compress="extract")
        c1, t1 = chars_tokens(full)
        c2, t2 = chars_tokens(extr)
        out.append({"day": day, "chars_full": c1, "chars_extract": c2,
                    "tok_full": t1, "tok_extract": t2,
                    "save": round(1 - c2 / c1, 3) if c1 else 0})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=8)
    ap.add_argument("--save", default="")
    ap.add_argument("--dump", action="store_true", help="把 off/extract 两版提示包写到 dist-ab/")
    args = ap.parse_args()

    if args.save:
        saves = [args.save]
    else:
        saves = sorted(glob.glob(os.path.join(ROOT, "games", "game-*.json")),
                       key=os.path.getmtime)[-args.games:]

    total_full = total_extr = 0
    per_game = []
    for p in saves:
        try:
            rows = measure(p)
        except Exception as exc:                                # noqa: BLE001
            print(f"跳过 {os.path.basename(p)}：{type(exc).__name__}: {exc}")
            continue
        if not rows:
            continue
        per_game.append((os.path.basename(p), rows))
        for r in rows:
            total_full += r["tok_full"]
            total_extr += r["tok_extract"]

    print(f"== 压缩 A/B（{len(per_game)} 局，只看台账/历史部分）==\n")
    for name, rows in per_game:
        print(name)
        for r in rows:
            print(f"  第 {r['day']} 天：{r['chars_full']:>6} 字 / {r['tok_full']:>5} tok"
                  f"  →  {r['chars_extract']:>6} 字 / {r['tok_extract']:>5} tok"
                  f"   省 {r['save']:.0%}")
        print()

    if total_full:
        cut = 1 - total_extr / total_full
        print(f"合计：{total_full} tok → {total_extr} tok，**省 {cut:.1%}**")

    if args.dump:
        outdir = os.path.join(ROOT, "dist-ab")
        os.makedirs(outdir, exist_ok=True)
        for name, _rows in per_game[:1]:
            data = json.load(io.open(os.path.join(ROOT, "games", name), encoding="utf-8"))
            led = Ledger.from_list(data.get("ledger") or [])
            seats = sorted({int(r["seat"]) for r in (data.get("ledger") or []) if r.get("seat")}) or [1]
            day = max(int(r.get("day") or 0) for r in (data.get("ledger") or []))
            for mode in ("off", "extract"):
                txt = led.render(seats[0], "good", seats, [], day, compress=mode)
                io.open(os.path.join(outdir, f"{os.path.splitext(name)[0]}-{mode}.txt"),
                        "w", encoding="utf-8").write(txt)
        print(f"两版提示包已写到 {outdir}/（给人工/模型对照用）")

    print("\n⚠️ 棋力三项（事实错误 / 票型命中 / 读感）需要真模型重放，未在此脚本里自动跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())