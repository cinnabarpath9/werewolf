# coding=utf-8
"""新板子 `eleven_guard`（标准板把猎人换成守卫）+ 守卫章分发。

用户 2026-10-09 要的板子：4 狼 + 预言家、女巫、**守卫**、白痴 + 3 民。
设计意图（用户原话要点）：猎人和女巫都是"追人次"，而且**一旦整错好人直接没了**
（毒错/打错 = 白送狼一个人头；好人一共只有 4 个人头、狼自带三刀）；
换成守卫多出"**守夜博弈**"：狼要猜守卫守谁、守卫要猜狼刀谁，三刀 = 三次博弈。
而且守卫白天零能力、夜里很有用、不留下公开痕迹 —— 是天然的"衣服牌"。

这个脚本守四件事：
  1. 板子本身能打完整局（mock 跑一批，不抛异常、有胜负）；
  2. **不能连续两晚守同一人**（真跑 + 从守卫的私有记录里核）；
  3. 第 2 夜的候选名单里**确实排除了**第 1 夜守过的人（这是规则的执行点，
     不是"文档里写了"）—— 同时**隔一夜可以再守**（第 3 夜又能选他）；
  4. 按板子组装的规则里**有【守卫】、没有【猎人】**（换掉的那个角色不许漏进来）。
"""
from __future__ import annotations

import asyncio
import collections
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import MockAgent                               # noqa: E402
from wolf.engine import Game                                    # noqa: E402
from wolf.roles import BUILTIN_BOARDS, make_board               # noqa: E402
from _boards import make_board, retired_board      # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def guard_targets(events: list[str]) -> list[int]:
    out = []
    for e in events:
        m = re.search(r"守护了 (\d+)号", e or "")
        if m:
            out.append(int(m.group(1)))
    return out


print("=== 一、板子已退役，但测试副本结构对 ===")
check("1a eleven_guard 已从产品内置板子里退役（2026-10-30）",
      "eleven_guard" not in BUILTIN_BOARDS)
board = retired_board("eleven_guard")
roles = list(board.roles)
check("1b 11 个座位", len(roles) == 11, str(len(roles)))
check("1c 4 狼 + 预言家/女巫/守卫/白痴 + 3 民",
      collections.Counter(roles) == collections.Counter(
          ["wolf"] * 4 + ["seer", "witch", "guard", "idiot"] + ["villager"] * 3),
      str(collections.Counter(roles)))
check("1d **没有猎人**（这就是跟标准板的唯一区别）", "hunter" not in roles)

print("\n=== 二、按板子组装的规则：有守卫、没猎人 ===")
from exam_bank import rules_text_for                            # noqa: E402
text = rules_text_for("eleven_guard")
check("2a 规则里有【守卫】", "【守卫】" in text)
check("2b 规则里**没有**【猎人】（换掉的角色不许漏进来）", "【猎人】" not in text)
check("2c 守卫的关键规则都写了（不能连守 / 同守同救）",
      "不能连续两晚守同一个人" in text and "同守同救" in text)

print("\n=== 三、真跑 30 局：能打完、守卫没连守 ===")
wins: collections.Counter = collections.Counter()
days: list[int] = []
bad_games: list[int] = []
for seed in range(1, 31):
    g = Game(board=make_board("eleven_guard"), seed=seed, max_days=8, decision_first=True,
             agent_factory=lambda s: MockAgent(s, random.Random(seed * 100 + s.id)),
             on_event=lambda *a, **k: None)
    asyncio.run(g.run())
    wins[g.winner] += 1
    days.append(g.day)
    guard = next(s for s in g.seats if s.role_key == "guard")
    tg = guard_targets(list(guard.private_events))
    if any(tg[i] == tg[i + 1] for i in range(len(tg) - 1)):
        bad_games.append(seed)
check("3a 30 局都跑完且分出了结果", set(wins) <= {"wolf", "good", "draw"} and sum(wins.values()) == 30,
      str(dict(wins)))
check("3b ⚠️ 没有一局违反「不能连续两晚守同一人」", not bad_games, f"违反的种子：{bad_games}")
check("3c 局数分布正常（守卫确实在动）",
      wins["wolf"] + wins["good"] >= 25, f"平均 {sum(days) / len(days):.2f} 天")

print("\n=== 四、执行点：第 2 夜的候选名单排除了第 1 夜守过的人（隔夜又能守）===")


class Scripted:
    def __init__(self, sid, script, log):
        self.seat_id, self.script, self.log = sid, script, log

    async def ask(self, packet, task):
        self.log.append((self.seat_id, task.kind, task.text or "", list(task.legal or [])))
        t = self.script.get(task.kind)
        r = {}
        if isinstance(t, dict):
            r = t.get(self.seat_id, t.get("*", {}))
        elif t is not None:
            r = t
        # 2026-10-25：支持 callable（按当时的 legal 现算回复）。
        if callable(r):
            try:
                r = r(self.seat_id, task)
            except TypeError:
                r = r(task)
        return dict(r) if isinstance(r, dict) else {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
         7: "guard", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}
SCRIPT = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "剧本", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_explode": {"*": {"explode": False, "who": None, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    # 女巫不救（否则守卫的守人结果看不出来）；也不毒
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
    # 每晚守「当前名单里的第一个」：第 1 夜是 1 号，第 2 夜 1 号被排除、换下一个，
    # 第 3 夜 1 号又回到名单里 —— 正好用来验「不能连守 / 隔夜可再守」。
    # ⚠️ 2026-10-25：以前这里写死 target=1，靠引擎"目标不合法就随机换一个"的兜底才能继续；
    #    引擎现在对守卫**不做静默换人**（写了不合法目标 = 这一夜按空过处理并说明），
    #    所以剧本必须自己给合法目标，否则第 2 夜就弃守、第 3 夜 1 号也不会回到名单。
    "guard": lambda sid, task: {"target": (list(task.legal) or [None])[0],
                                "reason": "剧本：每晚守名单第一个"},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "剧本", "claim": "无"}},
    "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                      "vote": None, "speech": "剧本", "claim": "无"}},
    "speech": {"*": {"speech": "剧本", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "剧本", "claim": "无"}},
    "sheriff_run": {"*": {"run": False}},
    "sheriff_speech": {"*": {"speech": "剧本", "claim": "无"}},
    "sheriff_vote": {"*": {"abstain": True, "reason": "剧本"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
    "sheriff_order": {"*": {"explode": False, "reverse": False, "reason": "剧本"}},
}
log: list = []
g2 = Game(board=make_board("eleven_guard"), seed=5, max_days=3, decision_first=True,
          agent_factory=lambda s: Scripted(s.id, SCRIPT, log), on_event=lambda *a, **k: None)
for sid, role in SEATS.items():
    g2.seat_by_id(sid).role_key = role
asyncio.run(g2.run())
guard_asks = [(t, legal) for sid, k, t, legal in log if sid == 7 and k == "guard"]
check("4a 守卫每晚都被问了一次（活着的夜）", len(guard_asks) >= 2, f"{len(guard_asks)} 次")
if len(guard_asks) >= 2:
    first_legal, second_legal = guard_asks[0][1], guard_asks[1][1]
    check("4b 第 1 夜的名单里有 1 号（他还没守过）", 1 in first_legal, str(first_legal[:8]))
    check("4c ⚠️ 第 2 夜的名单里**没有** 1 号（不能连守）", 1 not in second_legal, str(second_legal[:8]))
    if len(guard_asks) >= 3:
        check("4d 第 3 夜名单里**又有** 1 号了（隔夜可以再守）", 1 in guard_asks[2][1],
              str(guard_asks[2][1][:8]))
check("4e 守卫的私有记录写清了守谁",
      guard_targets(list(g2.seat_by_id(7).private_events)) != [],
      str(list(g2.seat_by_id(7).private_events)[:2]))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
