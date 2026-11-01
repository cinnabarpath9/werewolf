# coding=utf-8
"""把一局里**每一次模型调用**逐条记下来（谁、什么类型、第几天、哪个阶段）。

用法：`python tools/call_structure.py`（默认跑一局 mock，把调用清单按天/阶段打印出来）。
用途：核对话轮与调用结构、算一局的调用成本、确认"某张牌到底加不加调用"。

用途：回答"第一夜到底是 7 次还是 8 次""上警是 10 次还是 11 次"这类问题 ——
不靠猜，靠实测出来的清单。
"""
import asyncio
import collections
import random
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from wolf.agents import MockAgent
from wolf.engine import Game
from wolf.roles import make_board

#: 调用类型 → 人话（用来分组统计）
LABEL = {
    "wolf_intent": "狼：定刀口（决策）",
    "wolf_talk": "狼：夜聊讨论",
    "wolf_kill": "狼：出刀（老流程才用）",
    "wolf_explode": "狼：是否自爆",
    "seer_check": "预言家：查验",
    "dream": "摄梦人：摄谁",
    "witch": "女巫：用药",
    "lucky": "幸运儿：要不要用技能",
    "sheriff_run": "上警决定",
    "sheriff_speech": "上警发言",
    "sheriff_vote": "警徽投票",
    "sheriff_order": "警长定顺逆",
    "speech_plan": "发言前决策 + 发言正文（合并）",
    "analyze": "投票前决策（含鸦的诅咒）",
    "vote": "投票（PK 重投 / 关掉决策在前时的老流程）",
    "speech": "单独发言（PK 发言 / 真人 / 退回路径）",
    "last_words": "遗言",
    "badge": "警徽去向",
    "hunter_shoot": "猎人开枪",
}

calls: list[tuple[int, str, str, int]] = []          # (day, phase, kind, seat)
PACKET_LEN: list[int] = []


class Spy:
    def __init__(self, seat, rng):
        self.inner = MockAgent(seat, rng)
        self.seat = seat

    async def ask(self, packet, task):
        g = HOLDER[0]
        calls.append((int(g.day or 0), str(getattr(g, "phase", "") or ""), task.kind, self.seat.id))
        return await self.inner.ask(packet, task)

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


HOLDER: list = [None]
g = Game(board=make_board("eleven_wolfking"), seed=5, max_days=3, decision_first=True,
         agent_factory=lambda s: Spy(s, random.Random(5 * 11 + s.id)),
         on_event=lambda *a, **k: None)
HOLDER[0] = g
asyncio.run(g.run())

print(f"  一局（{len(g.seats)} 人，全模型座位，day={g.day}，赢家={g.winner}）")
print(f"  总调用 {len(calls)} 次\n")
by_day: dict[tuple[int, str], list[str]] = collections.defaultdict(list)
for day, phase, kind, seat in calls:
    by_day[(day, phase)].append(f"{seat}号/{kind}")

for (day, phase), items in sorted(by_day.items()):
    kinds = collections.Counter(i.split("/")[1] for i in items)
    total = len(items)
    head = "、".join(f"{LABEL.get(k, k)}×{v}" for k, v in kinds.most_common())
    print(f"  ── 第 {day} 天 {phase}（{total} 次）")
    print(f"     {head}")
    if phase == "night":
        print(f"     明细：{items}")
    print()
