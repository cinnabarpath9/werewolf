# coding=utf-8
"""言行一致：说了「不跳身份」就别落"声称身份"那一笔（2026-11-03 真局复盘）。

真局里 4 号写：「我是好人，守墓人，这一轮先不跳身份。」——嘴上说不跳，身份照报，
引擎还把 `claim` 记成公开的「声称自己是【守墓人】」。这条把"自相矛盾"从模型侧和引擎侧一起堵掉：

  · 提示词：说了「先不跳身份 / 不报身份」就不要报身份，`claim` 留空（二选一）；
  · 引擎：`claim_allowed()` —— 发言里出现"不跳/不报/不亮身份"这类说法时，`claim` **不落账**，
    但**发言原话一个字不动**（只不记"声称"）。

跑法：python tests/t_claim_consistency.py
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game, claim_allowed                     # noqa: E402
from wolf.roles import make_board                               # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


print("=== 一、纯函数判定 ===")
check("1a 正常报身份 → 允许落账", claim_allowed("我是预言家，昨晚验了2号金水。", "预言家"))
check("1b 说「先不跳身份」又说自己是守墓人 → 不落账",
      not claim_allowed("我是好人，守墓人，这一轮先不跳身份。", "守墓人"))
check("1c 「不报身份 / 不亮身份 / 暂不跳」同样不落账",
      not claim_allowed("这轮我不报身份，先听。", "女巫")
      and not claim_allowed("我不亮身份。", "猎人")
      and not claim_allowed("暂不跳，后面再说。", "预言家"))
check("1d claim 是「无」/空 → 不落账（本来就没有声称）",
      not claim_allowed("我是好人。", "无") and not claim_allowed("我是好人。", ""))
check("1e 发言里没提「不跳」但 claim 有值 → 照常落账",
      claim_allowed("我先报身份：我是预言家。", "预言家"))
check("1f 「本来不想跳…但我必须跳了，我是预言家」→ 照常落账（别误伤）",
      claim_allowed("本来不想跳的，但我必须跳了：我是预言家，昨晚验的3号查杀。", "预言家"))
check("1g 引用别人说「不想跳身份」→ 自己 claim 照常落账",
      claim_allowed("3号说不想跳身份，但我是女巫，昨晚救了2号。", "女巫"))


class Scripted:
    """按座位给剧本；座位 2 故意自相矛盾，座位 3 正常报身份。"""

    def __init__(self, seat_id: int) -> None:
        self.seat_id = seat_id
        self.uses_model = False

    async def ask(self, packet, task):
        if task.kind in ("speech", "speech_plan"):
            if self.seat_id == 2:
                return {"reads": [], "vote": None,
                        "speech": "我是好人，守墓人，这一轮先不跳身份。先抛结论：我站7号。",
                        "claim": "守墓人"}
            if self.seat_id == 3:
                return {"reads": [], "vote": None,
                        "speech": "我先报身份：我是预言家，昨晚验的2号是查杀。",
                        "claim": "预言家"}
            return {"reads": [], "vote": None, "speech": "我是好人，先听。", "claim": "无"}
        if task.kind == "analyze":
            return {"reads": [], "vote": None, "summary": "剧本"}
        if task.kind == "vote":
            return {"abstain": True, "reason": "剧本"}
        return {}

    def last_meta(self):
        return {}


print("\n=== 二、整局路径：矛盾的那一笔不落账、正常那一步照记 ===")
g = Game(board=make_board("six_all_reveal"), human_seat=0, max_days=1,
         agent_factory=lambda seat: Scripted(seat.id), on_event=lambda *a, **k: None)
for sid, role in {1: "wolf", 2: "wolf", 3: "seer", 4: "witch", 5: "villager", 6: "villager"}.items():
    g.seat_by_id(sid).role_key = role
g.day, g.phase = 1, "day"
g.sheriff_enabled = False
asyncio.run(g.day_phase([]))

claims = {}
for e in g.ledger.entries:
    if e.kind == "claim" and e.seat is not None:
        claims.setdefault(e.seat, []).append(e.text)
speeches = {e.seat: e.text for e in g.ledger.entries if e.kind == "speech" and e.seat is not None}

check("2a 2 号（说不跳身份却报了守墓人）没有 claim 记录", 2 not in claims, str(claims.get(2)))
check("2b 2 号的发言原话一字没删（守墓人还在里面）",
      "守墓人" in speeches.get(2, "") and "先不跳身份" in speeches.get(2, ""),
      speeches.get(2, "")[:40])
check("2c 3 号（正常报预言家）的 claim 照常落账", bool(claims.get(3)), str(claims.get(3)))

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)