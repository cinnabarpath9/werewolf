# coding=utf-8
"""#4：警长每天可以决定发言方向（顺序 / 逆序）—— 用户 2026-10-08 要求。

规则口径（用户原话「警长可以自选顺序或逆序」）：
  · **顺序** = 从警长的**下一位**开始按座位号往上数，警长**最后**发言；
  · **逆序** = 从警长的**上一位**开始按座位号往下数，警长**照样最后**发言；
  · 没有警徽 → 退回默认（按座位号从小到大），这一天也不问任何人；
  · `game.sheriff_picks_order: false` → 完全不问，永远是老口径（警长下一位先说）。
代价：**有警徽的局每天多一次调用**（警长座位的决策档，与 `sheriff_run` 同级）。

本脚本用剧本代理跑整局，断言"台账里第 1 天发言的座位顺序"就是规则说的那个顺序 ——
这是**行为判据**：只断言"问过 sheriff_order"会漏掉"问了但没照做"。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                                  # noqa: E402
from wolf.roles import BoardDef, make_board
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）
from _boards import NINE_BOARD                     # 测试专用 9 人板（产品的 9 人板已删）

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


class Scripted:
    def __init__(self, seat_id: int, script: dict, log: list) -> None:
        self.seat_id, self.script, self.log = seat_id, script, log

    async def ask(self, packet, task):
        m = re.search(r"第 (\d+) 天", task.text or "")
        day = int(m.group(1)) if m else 0
        self.log.append((self.seat_id, task.kind, day, task))
        table = self.script.get(task.kind)
        reply = {}
        if isinstance(table, dict):
            reply = table.get(self.seat_id, table.get("*", {}))
        elif table is not None:
            reply = table
        return dict(reply) if isinstance(reply, dict) else {}

    def last_meta(self) -> dict:
        return {}

    async def aclose(self) -> None:
        return None


ELEVEN = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
          7: "hunter", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}

NINE = {1: "wolf", 2: "wolf", 3: "wolf", 4: "seer", 5: "witch", 6: "hunter",
        7: "villager", 8: "villager", 9: "villager"}

BASE = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "（剧本）刀 9", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "witch": {"*": {"save": True, "poison": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                      "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
    "sheriff_speech": {"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
    "sheriff_vote": {"*": {"abstain": True, "reason": "剧本"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
}


def build(board, assign: dict, script: dict, **kw):
    log: list = []
    # ⚠️ 本文件测的是**竞选机制**：默认关掉"预言家锁定上警"（2026-10-10 作者口径），
    #    否则会多出一个没写进剧本的候选人，这些断言全会连锁假红。
    kw.setdefault("seer_auto_run", False)
    game = Game(board=make_board(board), seed=7, max_days=1,
                agent_factory=lambda seat: Scripted(seat.id, script, log),
                on_event=lambda *a, **k: None, **kw)
    for sid, role in assign.items():
        game.seat_by_id(sid).role_key = role
    return game, log


def day1_order(game) -> list[int]:
    """第 1 天**轮次发言**的座位顺序（把警长竞选那一段的竞选发言剔掉）。

    竞选发言的 kind 也是 `speech`，所以按"竞选公告 / 当选 / 流失"切一刀；
    **没有警长竞选的板子（9 人）**就整段都算轮次发言 —— 第一版漏了这条，9 人那条判据空过（[]）。
    """
    entries = [e for e in game.ledger.entries if e.day == 1 and e.phase == "day"]
    has_election = any(e.kind == "announce" and "警长竞选" in e.text for e in entries)
    out: list[int] = []
    started = not has_election
    for e in entries:
        if e.kind == "announce" and ("当选警长" in e.text or "警徽流失" in e.text):
            started = True
            continue
        if started and e.kind == "speech" and e.seat is not None:
            out.append(e.seat)
    return out


def run(board, assign: dict, script: dict, **kw) -> tuple:
    game, log = build(board, assign, script, **kw)
    asyncio.run(game.run())
    return game, log


def scripted(eleven=True, **over) -> dict:
    s = dict(BASE)
    s["sheriff_run"] = ({3: {"run": True}, "*": {"run": False}} if eleven
                        else {"*": {"run": False}})
    s.update(over)
    return s


# ── 一、顺序 ────────────────────────────────────────────────────────────────────
print("=== 一、警长选「顺序」：从下一位往上数，警长最后 ===")
g, log = run("eleven_standard", ELEVEN, scripted(sheriff_order={"*": {"reverse": False}}))
check("1a 问过警长（而且只问警长一个人）",
      sorted(set(s for s, k, _d, _t in log if k == "sheriff_order")) == [3],
      str(sorted(set(s for s, k, _d, _t in log if k == "sheriff_order"))))
check("1b 发言顺序 = 4,5,6,7,8,9,10,11,1,2,3（警长最后）",
      day1_order(g) == [4, 5, 6, 7, 8, 9, 10, 11, 1, 2, 3], str(day1_order(g)))
check("1c 台账公告写明「顺序」并列出顺序",
      any("决定今天**顺序**发言" in e.text for e in g.ledger.entries),
      str([e.text[:50] for e in g.ledger.entries if "决定今天" in e.text][:1]))

# ── 二、逆序 ────────────────────────────────────────────────────────────────────
print("\n=== 二、警长选「逆序」：从上一位往下数，警长照样最后 ===")
g2, log2 = run("eleven_standard", ELEVEN, scripted(sheriff_order={"*": {"reverse": True}}))
check("2a 发言顺序 = 2,1,11,10,9,8,7,6,5,4,3（警长最后，方向反了）",
      day1_order(g2) == [2, 1, 11, 10, 9, 8, 7, 6, 5, 4, 3], str(day1_order(g2)))
check("2b 台账公告写明「逆序」",
      any("决定今天**逆序**发言" in e.text for e in g2.ledger.entries),
      str([e.text[:50] for e in g2.ledger.entries if "决定今天" in e.text][:1]))
check("2c ⚠️ 两种方向的**座位集合相同**（只是次序反了，不许漏人/多人）",
      sorted(day1_order(g2)) == sorted(day1_order(g)) == list(range(1, 12)),
      f"{sorted(day1_order(g2))}")

# ── 三、没有警徽 ────────────────────────────────────────────────────────────────
print("\n=== 三、没有警徽（9 人板）：不问、按座位号从小到大 ===")
g3, log3 = run(NINE_BOARD, NINE, scripted(eleven=False))
check("3a 一次都没问 sheriff_order", not [x for x in log3 if x[1] == "sheriff_order"])
check("3b 顺序就是 1..9", day1_order(g3) == list(range(1, 10)), str(day1_order(g3)))

# ── 四、开关关掉 ────────────────────────────────────────────────────────────────
print("\n=== 四、sheriff_picks_order=false：不问，退回老口径 ===")
g4, log4 = run("eleven_standard", ELEVEN,
               scripted(sheriff_order={"*": {"reverse": True}}), sheriff_picks_order=False)
check("4a 开关关着就不问", not [x for x in log4 if x[1] == "sheriff_order"])
check("4b 顺序退回「警长下一位先说」（无视剧本里的逆序）",
      day1_order(g4) == [4, 5, 6, 7, 8, 9, 10, 11, 1, 2, 3], str(day1_order(g4)))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
