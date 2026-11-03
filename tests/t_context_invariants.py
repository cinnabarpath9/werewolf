# coding=utf-8
"""上下文不变量回归（参考同类项目公开设计文档里的口径，只借思路不借代码）。

四条口径（都是他们踩过、我们也可能踩的）：
1. **过去的发言不贴当前的死亡标签**：第 2 天引用第 1 天的发言，不能因为那个人现在死了
   就改写成"死者曾说"；
2. **投票候选范围 ≠ 推理范围**：`task.legal` 只限制"你能投/杀谁"，不能把别人的历史发言过滤掉；
3. **玩家声明 ≠ 主持人确认**：跳身份写"声称"，而且不能记成公告（不带公告编号）；
4. **公布前不出现夜间结果**：夜里（天亮公告之前）的包里不能出现"昨晚出局/平安夜"。

跑法：python tests/t_context_invariants.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context                                        # noqa: E402
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


def make_game():
    return Game(board=make_board("six_all_reveal"), human_seat=0, max_days=2,
                agent_factory=lambda s: None, on_event=lambda *a, **k: None)


g = make_game()
g.day = 2
g.ledger.add(1, "day", "speech", "1号的昨日发言：我站2号。", seat=1)
g.ledger.add(1, "day", "speech", "5号的昨日发言：1号像狼。", seat=5)
g.ledger.add(2, "day", "claim", "5号 声称自己是【预言家】。", seat=5)
g.ledger.add(2, "day", "announce", "昨晚出局：1号。（不公布死因）")
g.seat_by_id(1).alive = False
g.seat_by_id(1).death_cause = "wolf"
g.seat_by_id(1).death_day = 2

task = Task(kind="vote", text="投票", schema="{}", legal=[3, 4])
pkt = context.build(g, g.seat_by_id(2), task)

print("=== 一、过去的发言不贴当前死亡标签 ===")
past_line = next((ln for ln in pkt.splitlines() if "1号的昨日发言" in ln), "")
check("1a 第 1 天的发言仍在包里（活着/死了都要能引用）", bool(past_line))
check("1b 那条发言自己没有「已出局/被刀」的当前状态标签",
      past_line and "已出局" not in past_line and "被刀" not in past_line, past_line.strip()[:50])
check("1c 存活/出局名单在独立行里（状态仍有，只是不贴在旧发言上）",
      "【已出局】" in pkt and "1号（" in pkt)

print("\n=== 二、候选范围不限制推理与引用 ===")
check("2a legal 里没有 5 号，但 5 号的昨日发言仍在包里", "5号的昨日发言" in pkt)
check("2b legal 只影响投票目标（提示里给出可投的人）", "3" in pkt and "4" in pkt)

print("\n=== 三、声称 ≠ 确认 ===")
claim_line = next((ln for ln in pkt.splitlines() if "声称自己是" in ln), "")
check("3a 身份声明写成「声称」", bool(claim_line), claim_line.strip()[:40])
check("3b 声明不是公告（不带 ⟦公告# 编号）", claim_line and "⟦公告#" not in claim_line)

print("\n=== 四、公布前不出现夜间结果 ===")
g2 = make_game()
g2.day = 1
g2.phase = "night"
pkt2 = context.build(g2, g2.seat_by_id(2), Task(kind="speech", text="t", schema="{}"))
check("4a 天亮前包里没有「昨晚出局」", "昨晚出局" not in pkt2)
check("4b 天亮前包里没有「平安夜」", "平安夜" not in pkt2)

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)