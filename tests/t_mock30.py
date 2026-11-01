# coding=utf-8
"""30 局 mock 回归：确认引擎没被改坏，并复核"狼胜率 ~90% 是随机打法决定的"这个结论。

跑法： python t_mock30.py
不写任何存档、不联网、不花钱（MockAgent 是本地随机代理）。
"""
from __future__ import annotations

import io
import os
import random
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wolf.agents import MockAgent      # noqa: E402
from wolf.engine import Game           # noqa: E402
from wolf.roles import make_board      # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 30


def play(seed: int, wolf_control: bool = True, decision_first: bool = False) -> tuple[str, int]:
    rng = random.Random(seed)
    game = Game(board=make_board("eleven_standard"), seed=seed,
                agent_factory=lambda seat: MockAgent(seat, rng),
                on_event=lambda *a, **k: None,
                wolf_control=wolf_control, decision_first=decision_first)
    import asyncio
    asyncio.run(game.run())
    return str(game.winner), int(game.day)


def play_all(decision_first: bool) -> tuple[dict, list, list]:
    """跑 N 局（可选「决策+发言合并」那条路），返回 (每种子胜者, 天数列表, 异常列表)。"""
    winners: dict = {}
    days: list[int] = []
    errs: list[str] = []
    for seed in range(N):
        try:
            w, d = play(seed, True, decision_first)
        except Exception:                              # noqa: BLE001
            errs.append(f"seed={seed}\n{traceback.format_exc()}")
            continue
        winners[seed] = w
        days.append(d)
    return winners, days, errs


def main() -> int:
    print(f"板子 eleven_standard（4 狼 + 预/女/猎/白痴 + 3 民），{N} 个种子，纯 mock 随机代理")
    on, off = {}, {}
    days = []
    errs = []
    for seed in range(N):
        try:
            w, d = play(seed, True)
        except Exception:                              # noqa: BLE001
            errs.append(f"seed={seed}\n{traceback.format_exc()}")
            continue
        on[seed] = w
        days.append(d)
        try:
            off[seed] = play(seed, False)[0]
        except Exception:                              # noqa: BLE001
            errs.append(f"seed={seed} (wolf_control=False)\n{traceback.format_exc()}")
    tally = {}
    for w in on.values():
        tally[w] = tally.get(w, 0) + 1
    print(f"  结果分布：{tally}")
    print(f"  狼胜率：{tally.get('wolf', 0)}/{len(on)} = {tally.get('wolf', 0)/max(1,len(on)):.0%}")
    print(f"  平均天数：{sum(days)/max(1,len(days)):.2f}（最长 {max(days) if days else '-'}）")
    diff = {s: (on[s], off.get(s)) for s in on if off.get(s) != on[s]}
    print(f"  关掉 wolf_control 后结果不同的种子：{diff if diff else '无（完全一致）'}")

    # 「决策+发言合并成一次调用」那条路（decision_first=True）也跑一遍：
    # 它多走了一次 analyze（投票前决策），是产品**默认**的那条路（config 里 decision_first: true）
    # —— 只跑老流程的话，合并后的分支等于没被这 30 个种子压过。
    print(f"\n  再跑一遍 N={N}：decision_first=True（决策+发言合并的那条默认路）")
    df_winners, df_days, df_errs = play_all(True)
    df_tally: dict = {}
    for w in df_winners.values():
        df_tally[w] = df_tally.get(w, 0) + 1
    print(f"    结果分布：{df_tally}")
    print(f"    狼胜率：{df_tally.get('wolf', 0)}/{len(df_winners)} = "
          f"{df_tally.get('wolf', 0)/max(1,len(df_winners)):.0%}")
    print(f"    平均天数：{sum(df_days)/max(1,len(df_days)):.2f}"
          f"（最长 {max(df_days) if df_days else '-'}）")

    if errs or df_errs:
        print(f"  ❌ 有 {len(errs) + len(df_errs)} 个种子抛异常：")
        for e in (errs + df_errs)[:2]:
            print(e)
    ok = (not errs and not df_errs and len(on) == N and len(df_winners) == N
          and tally.get("wolf", 0) >= 24 and df_tally.get("wolf", 0) >= 24)
    print("\n" + ("✅ 两种跑法各 30 局全部跑完、无异常（狼胜率见上面两行；随机代理下狼本来就占优）"
                  if ok else "❌ 与预期不符（见上）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
