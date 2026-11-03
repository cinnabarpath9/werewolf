# coding=utf-8
"""整手预算不够时不再硬试第二次（2026-11-03 作者反馈"跑飞"）。

背景：真局里 8 号等了 5:26 还没出结果。查下来重试本身没坏（`t_auto_retry` 23/23 过），
问题是**第一次烧满单次上限（当时 420s）之后，整手只剩 120s，还要硬试第二次** ——
几乎必失败，只是让用户多干等几分钟。

现在的口径：**剩余预算不够"完整再试一次"（少于单次上限的一半、且不足 60 秒）时，
直接不再重试**，落到"问用户（再试/跳过/放弃）"那一链。
另外本机 `config.yaml` 的单次上限已从 420s 收到 **150s**。

跑法：python tests/t_retry_budget.py
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.context import Task                                   # noqa: E402
from wolf.engine import Game                                    # noqa: E402
from wolf.roles import make_board                               # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


class Hanging:
    """永远挂住（睡很久）→ 每次尝试都会撞单次上限。"""

    uses_model = True

    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, packet, task):
        self.calls += 1
        await asyncio.sleep(30)
        return {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


class FlakyThenOk:
    """第一次失败、第二次成功 → 正常情况下应当真的重试。"""

    uses_model = True

    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, packet, task):
        self.calls += 1
        if self.calls == 1:
            return {"_error": "模拟上游波动"}
        return {"speech": "第二次成了"}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


def run_case(agent, **kw):
    events: list[tuple] = []
    g = Game(board=make_board("six_all_reveal"), human_seat=0, max_days=1,
             agent_factory=lambda seat: agent,
             on_event=lambda kind, **data: events.append((kind, data)), **kw)
    asyncio.run(g.act(g.seat_by_id(1), Task(kind="speech", text="t", schema="{}")))
    return events


print("=== 一、剩余预算不够 → 不再硬试第二次 ===")
a = Hanging()
ev = run_case(a, call_timeout=2, decision_timeout=3, auto_retries=3, on_stall=None)
retries = [d for k, d in ev if k == "retry"]
warns = "；".join(str(d.get("message") or "") for k, d in ev if k == "warn")
check("1a 只发了一次请求（没有硬试第二次）", a.calls == 1, f"calls={a.calls}")
check("1b 确实没有 retry 事件", retries == [], str(retries))
check('1c 留了一条「预算不够」的说明', "不够再完整试一次" in warns, warns[:80])

print("\n=== 二、预算够 → 照常重试（别把功能改没了）===")
b = FlakyThenOk()
ev2 = run_case(b, call_timeout=30, decision_timeout=60, auto_retries=3, on_stall=None)
check("2a 第一次失败后真的重试了", b.calls >= 2, f"calls={b.calls}")
check('2b 有 retry 事件（前端据此显示「第 N 次尝试」）',
      any(k == "retry" for k, _d in ev2), str([k for k, _d in ev2][:6]))

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)