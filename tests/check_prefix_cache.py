# coding=utf-8
"""闸门：给「前缀缓存」把**我们这边该做的**守住（厂商无关的那一半）。

背景（2026-10-09 实测）：
  · 同一个座位**同一阶段内**两次调用，包的**公共前缀 16967 字 ≈ 12728 tokens = 93% 的包**
    （静态块：身份 / 通用提示词 / 规则书 / 打法书 / 概念库 / 狼队友 —— 全在**最前面**；
      动态块：当前阶段 / 存活玩家 / 任务 / 可选目标 / 输出格式 —— 全在**最后面**）。
    这个形状正是前缀缓存最想要的：只要开头那段**逐字节不变**，缓存就能命中。
  · 方舟（ModelArk）文档目录里确实有 **Context cache / 创建上下文缓存 API**，
    但开关参数与折扣我**没能取到**（文档页是 JS 渲染的，抓不到正文）→ 标"未复核"。
    所以这个闸门只管"我们这边别把前缀弄坏"，不假设厂商一定支持。

判据：
  1. **确定性**：同一个状态连建两次包，必须**逐字节相同**（有随机/时间戳就永远命中不了缓存）；
  2. **前缀长度**：同一阶段内两次调用（第 2 天 vs 第 3 天）的公共前缀 ≥ 包的 80%；
  3. **块顺序**：静态块必须都在动态块**之前**（否则前缀一早就断）；
  4. **静态段不含随时钟变的内容**：身份+规则+打法+概念那段，跨天也必须一样
     （"第 N 天"这类允许出现在动态段）。
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context, tabletalk                              # noqa: E402
from wolf.agents import MockAgent                                # noqa: E402
from wolf.context import Task                                    # noqa: E402
from wolf.engine import Game                                     # noqa: E402
from wolf.roles import make_board                                # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


g = Game(board=make_board("eleven_dream"), seed=4, max_days=6, decision_first=True,
         agent_factory=lambda s: MockAgent(s, random.Random(4 + s.id)),
         on_event=lambda *a, **k: None)
g.playbook_text = tabletalk.load_playbook(ROOT, "", "")
g.meta_text = tabletalk.load_meta_book(ROOT, "", "")
wolf = next(s for s in g.seats if s.role_key == "wolf")
seer = next(s for s in g.seats if s.role_key == "seer")


def pkt(seat, day: int, kind: str = "analyze") -> str:
    g.day = day
    return context.build(g, seat, Task(kind=kind, text="（测试）", schema="{}",
                                       legal=list(range(1, 12))))


# ① 确定性
g.day = 2
a1 = pkt(wolf, 2)
a2 = pkt(wolf, 2)
check("1a 同一状态连建两次包 → 逐字节相同（否则缓存永远命不中）", a1 == a2,
      f"{len(a1)} vs {len(a2)}")

# ② 同一阶段内的公共前缀
p2, p3 = pkt(wolf, 2), pkt(wolf, 3)
cp = 0
for x, y in zip(p2, p3):
    if x != y:
        break
    cp += 1
ratio = cp / max(1, len(p2))
check("2a 同一阶段内两次调用的公共前缀 ≥ 包的 80%", ratio >= 0.80,
      f"{cp}/{len(p2)} = {round(ratio * 100)}%（≈{round(cp / 1.333)} tokens）")

# ③ 块顺序：静态块都在动态块之前
STATIC = ("【你的身份】", "【打法书", "【概念与概率", "【你的狼队友】")
DYNAMIC = ("【当前阶段】", "【存活玩家】", "【当前任务】")
pos = {k: p2.find(k) for k in STATIC + DYNAMIC}
missing = [k for k, v in pos.items() if v < 0]
check("3a 静态块与动态块都能在包里找到", not missing, str(missing))
if not missing:
    check("3b 所有静态块都排在动态块之前",
          max(pos[k] for k in STATIC) < min(pos[k] for k in DYNAMIC),
          f"静态最末 {max(pos[k] for k in STATIC)} < 动态最先 {min(pos[k] for k in DYNAMIC)}")

# ④ 静态段跨天不变（身份 + 规则 + 打法 + 概念那段）
head1, head2 = pkt(wolf, 2), pkt(wolf, 3)
static_end = head1.find("【当前阶段】")
same = head1[:static_end] == head2[:static_end]
check("4a 静态段（身份/规则/打法/概念/队友）跨天逐字节一致", same,
      f"静态段 {static_end} 字")
# 阶段切换时**允许**变（那是设计），但要说清代价
p1 = pkt(wolf, 1)
cp_open_mid = 0
for x, y in zip(p1, head2):
    if x != y:
        break
    cp_open_mid += 1
print(f"    （提示：阶段切换那一次调用前缀会缩短到 {cp_open_mid} 字 —— "
      f"一局里大约只发生 2~3 次，属正常代价）")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
