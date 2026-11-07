# coding=utf-8
"""发牌概率：把《概念库》那张概率表**从发牌本身重算一遍**。

为什么要这个工具（2026-10-11，作者拍板）：
  《概念库》第十一节的数字是 2026-10-07 用 `tests/legacy/calc_meta_*.py` 三个一次性脚本
  算出来的，口径不统一 —— 其中「全场至少一处三连神/三连好」用的是**首尾不接**的 9 处三连
  （`range(N - 2)`），而作者的本意是**11 张牌是一个圈**（11→1→2 也算一处）。
  差得很明显：三连好 90.9%（不接） vs 96.7%（成圈）。

本工具做两件事，互相印证：
  ① **精确枚举**：11 座、4 狼 / 4 神 / 3 民 的"类型排列"共 C(11,4)·C(7,4) = **11550** 种，
     每个类型排列对应同样多的具体发牌（4!·4!·3! = 3456 种），所以它们**等概率** ——
     枚举就是精确值，不用抽样。
  ② **真发牌抽样**：按**引擎自己的发牌路径**（`Game.__init__` 里的 `self.rng.shuffle(keys)`）
     真发 N 手牌，统计同一批频率，看是否收敛到 ①（顺带验证"洗牌 = 均匀"这条前提）。

用法：
    python tools/meta_odds.py              # 两种都跑（抽样 20 万手，约 1~2 秒）
    python tools/meta_odds.py --deals 1000000
    python tools/meta_odds.py --check      # 只校验《概念库》里的数字跟枚举一致（给回归用）
"""
from __future__ import annotations

import argparse
import io
import itertools
import os
import random
import re
import sys
from math import comb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

N, WOLVES, GODS, VILLAGERS = 11, 4, 4, 3
CONCEPT = os.path.join(ROOT, "content", "06_概念库.md")


# ── ① 精确枚举：所有等概率的"类型排列" ────────────────────────────────────────
def all_arrangements() -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for wolves in itertools.combinations(range(N), WOLVES):
        rest = [i for i in range(N) if i not in wolves]
        for gods in itertools.combinations(rest, GODS):
            kind = ["好"] * N
            for i in wolves:
                kind[i] = "狼"
            for i in gods:
                kind[i] = "神"
            for i in rest:
                if i not in gods:
                    kind[i] = "民"
            out.append(tuple(kind))
    return out


#: 「成圈」版：11 个窗口（首尾相接）。线性版（9 个窗口）也留着，方便跟旧数字对账。
def windows(three: bool = True) -> list[tuple[int, ...]]:
    k = 3 if three else 1
    return [tuple((s + j) % N for j in range(k)) for s in range(N)]


def has_run(o: tuple[str, ...], kind: str, starts: list[tuple[int, ...]]) -> bool:
    """某处窗口里是不是"连着 kind"。

    ⚠️ `kind="好"` 是**好人**（= 不是狼），不是字面量"好"：类型排列里好人会被进一步
    写成"神/民"，所以这里必须按"!= 狼"比（第一版按字面比，量出 0.00% 这个荒唐值）。
    """
    if kind == "好":
        return any(all(o[i] != "狼" for i in w) for w in starts)
    return any(all(o[i] == kind for i in w) for w in starts)


def exact() -> dict[str, float]:
    deals = all_arrangements()
    T = len(deals)
    circle = windows()
    linear = [tuple(range(s, s + 3)) for s in range(N - 2)]        # 首尾不接（旧口径）
    res: dict[str, float] = {}

    # 基线
    res["P(某位是狼)"] = WOLVES / N
    res["P(某位是神)"] = GODS / N
    res["P(某位是民)"] = VILLAGERS / N

    # 连好链：指定开头 k 位是好人 → 两端（k+1 与 11）里各是狼 / 至少一端是狼
    for k in (2, 3, 4, 5):
        hit = [o for o in deals if all(o[i] != "狼" for i in range(k))]
        down, up = k % N, N - 1
        res[f"{k}连好→下置位({down + 1}号)是狼"] = (
            sum(1 for o in hit if o[down] == "狼") / len(hit) if hit else 0.0)
        res[f"{k}连好→上置位({up + 1}号)是狼"] = (
            sum(1 for o in hit if o[up] == "狼") / len(hit) if hit else 0.0)
        res[f"{k}连好→至少一端是狼"] = (
            sum(1 for o in hit if o[down] == "狼" or o[up] == "狼") / len(hit) if hit else 0.0)

    # 连狼链：已知开头 k 位是狼 → 下一位是狼 / 1、3 是狼时 2 号是好人的概率
    for k in (1, 2, 3):
        hit = [o for o in deals if all(o[i] == "狼" for i in range(k))]
        res[f"{k}连狼→下一位是狼"] = (
            sum(1 for o in hit if o[k] == "狼") / len(hit) if hit else 0.0)
    both_w = [o for o in deals if o[0] == "狼" and o[2] == "狼"]
    res["1、3 是狼→2 号是好人"] = (sum(1 for o in both_w if o[1] != "狼") / len(both_w)
                                  if both_w else 0.0)

    # 藏身份检测：指定三个连续座位全是神 / 全是民 / 全是好人（含 4 连好）
    res["指定3连神"] = sum(1 for o in deals if all(o[i] == "神" for i in circle[0])) / T
    res["指定3连民"] = sum(1 for o in deals if all(o[i] == "民" for i in circle[0])) / T
    res["指定3连好"] = sum(1 for o in deals if all(o[i] != "狼" for i in circle[0])) / T
    res["指定4连好"] = sum(1 for o in deals if all(o[i] != "狼" for i in range(4))) / T
    three_good = [o for o in deals if all(o[i] != "狼" for i in circle[0])]
    res["指定3连好里至少一人是神"] = (
        sum(1 for o in three_good if any(o[i] == "神" for i in circle[0])) / len(three_good)
        if three_good else 0.0)

    # 相邻一对 / 三连狼 / “三牌必开一狼”的完整口径
    for c in ("神", "民", "狼"):
        res[f"指定一对{c}"] = sum(1 for o in deals if o[0] == c and o[1] == c) / T
        res[f"全场至少一对{c}"] = sum(
            1 for o in deals if any(o[i] == c and o[(i + 1) % N] == c for i in range(N))) / T
    res["指定3连狼"] = sum(1 for o in deals if all(o[i] == "狼" for i in circle[0])) / T
    res["指定3连至少一狼"] = sum(
        1 for o in deals if any(o[i] == "狼" for i in circle[0])) / T
    res["全场至少一处3连狼(成圈)"] = sum(1 for o in deals if has_run(o, "狼", circle)) / T
    res["全场每个3连都有狼"] = sum(
        1 for o in deals if all(any(o[i] == "狼" for i in w) for w in circle)) / T

    # 全场「至少一处三连」——**这是这一轮修的两个数**：成圈 11 个窗口 / 不接 9 个窗口
    res["全场至少一处3连神(成圈)"] = sum(1 for o in deals if has_run(o, "神", circle)) / T
    res["全场至少一处3连好(成圈)"] = sum(1 for o in deals if has_run(o, "好", circle)) / T
    res["全场至少一处3连神(不接)"] = sum(1 for o in deals if has_run(o, "神", linear)) / T
    res["全场至少一处3连好(不接)"] = sum(1 for o in deals if has_run(o, "好", linear)) / T
    return res


# ── ② 真发牌抽样（走引擎同一条路径：`random.Random(seed).shuffle(牌堆)`）──────────
def sample(count: int, seed: int = 20261011) -> dict[str, float]:
    from wolf.roles import ROLES, make_board              # noqa: PLC0415

    deck = list(make_board("eleven_knight").roles)
    kinds = {r: ("狼" if ROLES[r].camp == "wolf" else
                 "神" if ROLES[r].kind == "god" else "民") for r in set(deck)}
    rng = random.Random(seed)
    circle = windows()
    n = {k: 0 for k in (
        "指定一对神", "指定一对民", "指定一对狼",
        "全场至少一对神", "全场至少一对民", "全场至少一对狼",
        "指定3连狼", "指定3连至少一狼", "全场至少一处3连狼(成圈)",
        "全场每个3连都有狼", "全场至少一处3连神(成圈)", "全场至少一处3连好(成圈)",
    )}
    n_three_good = n_three_good_god = 0
    for _ in range(count):
        rng.shuffle(deck)
        o = tuple(kinds[r] for r in deck)
        for c in ("神", "民", "狼"):
            if o[0] == c and o[1] == c:
                n[f"指定一对{c}"] += 1
            if any(o[i] == c and o[(i + 1) % N] == c for i in range(N)):
                n[f"全场至少一对{c}"] += 1
        if all(o[i] == "狼" for i in circle[0]):
            n["指定3连狼"] += 1
        if any(o[i] == "狼" for i in circle[0]):
            n["指定3连至少一狼"] += 1
        if has_run(o, "狼", circle):
            n["全场至少一处3连狼(成圈)"] += 1
        if all(any(o[i] == "狼" for i in w) for w in circle):
            n["全场每个3连都有狼"] += 1
        if has_run(o, "神", circle):
            n["全场至少一处3连神(成圈)"] += 1
        if has_run(o, "好", circle):
            n["全场至少一处3连好(成圈)"] += 1
        if all(o[i] != "狼" for i in circle[0]):
            n_three_good += 1
            if any(o[i] == "神" for i in circle[0]):
                n_three_good_god += 1
    out = {k: v / count for k, v in n.items()}
    out["指定3连好"] = n_three_good / count
    out["指定3连好里至少一人是神"] = (n_three_good_god / n_three_good) if n_three_good else 0.0
    return out

def check_book(rows: dict[str, float]) -> list[str]:
    """校验《概念库》里的字面数字跟枚举结果一致（给回归用）。"""
    text = io.open(CONCEPT, encoding="utf-8").read()
    want = {
        "36.4%": rows["P(某位是狼)"], "27.3%": rows["P(某位是民)"],
        "44.4%": rows["2连好→下置位(3号)是狼"], "72.2%": rows["2连好→至少一端是狼"],
        "50.0%": rows["3连好→下置位(4号)是狼"], "78.6%": rows["3连好→至少一端是狼"],
        "57.1%": rows["4连好→下置位(5号)是狼"], "85.7%": rows["4连好→至少一端是狼"],
        "93.3%": rows["5连好→至少一端是狼"],
        "30.0%": rows["1连狼→下一位是狼"], "22.2%": rows["2连狼→下一位是狼"],
        "12.5%": rows["3连狼→下一位是狼"], "77.8%": rows["1、3 是狼→2 号是好人"],
        "2.42%": rows["指定3连神"], "0.61%": rows["指定3连民"],
        "21.2%": rows["指定3连好"], "10.6%": rows["指定4连好"],
        "23.3%": rows["全场至少一处3连神(成圈)"], "96.7%": rows["全场至少一处3连好(成圈)"],
        "97.1%": rows["指定3连好里至少一人是神"],
        "10.9%": rows["指定一对神"], "5.5%": rows["指定一对民"],
        "83.3%": rows["全场至少一对神"], "53.3%": rows["全场至少一对民"],
        "78.8%": rows["指定3连至少一狼"], "3.3%": rows["全场每个3连都有狼"],
    }
    bad: list[str] = []
    for lit, val in want.items():
        if lit not in text:
            bad.append(f"（表里没有 {lit}）")
            continue
        shown = float(lit.rstrip("%"))
        if abs(shown - round(val * 100, 1)) > 0.05:
            bad.append(f"{lit} 但枚举是 {val * 100:.2f}%")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", type=int, default=200_000, help="抽样手数（默认 20 万）")
    ap.add_argument("--check", action="store_true", help="只校验《概念库》的数字")
    args = ap.parse_args()

    rows = exact()
    if args.check:
        bad = check_book(rows)
        for line in bad:
            print("  ❌", line)
        print("《概念库》概率表 vs 枚举：", "✅ 一致" if not bad else f"❌ {len(bad)} 处不符")
        return 1 if bad else 0

    T = len(all_arrangements())
    print(f"=== ① 精确枚举：C(11,4)·C(7,4) = {comb(N, WOLVES)} × {comb(N - WOLVES, GODS)}"
          f" = {T} 种类型排列（等概率）===")
    for k, v in rows.items():
        print(f"  {k:<28}{v * 100:6.2f}%")

    print(f"\n=== ② 真发牌抽样（引擎同一条洗牌路径，{args.deals:,} 手）===")
    got = sample(args.deals)
    for k, v in got.items():
        print(f"  {k:<28}{v * 100:6.2f}%   枚举 {rows[k] * 100:6.2f}%"
              f"   差 {abs(v - rows[k]) * 100:+.2f} 个百分点")

    print("\n=== ③ 《概念库》第十一节核对 ===")
    bad = check_book(rows)
    for line in bad:
        print("  ❌", line)
    print("  ", "✅ 表里的数字与枚举一致" if not bad else f"❌ {len(bad)} 处不符")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
