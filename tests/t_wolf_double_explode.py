# coding=utf-8
"""11 人狼王板新规（作者 2026-11-03）：

1. **狼王禁止自爆** —— 任何"要不要自爆"的决策都不再问狼王（少一次调用）；
2. **单狼自爆不吞警徽** —— 竞选挂起，当天结束；第二天公布夜里信息后，剩下的狼再决策一次；
3. **双狼自爆才吞警徽** —— 第二次自爆成立 → 警徽流失（本局没有警长），当天结束；
4. 不爆 → **恢复竞选**：已发过言的不重复，然后投票定警徽；
5. 其他板子**保持原规则**（自爆吞警徽、狼王能爆）。

本脚本用剧本代理跑整局，断言行为（谁被问、警徽在不在、竞选有没有恢复），不看"问过没有"。
跑法：python tests/t_wolf_double_explode.py
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
from wolf.roles import make_board                             # noqa: E402

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
        self.log.append((self.seat_id, task.kind, task))
        table = self.script.get(task.kind)
        reply = {}
        if isinstance(table, dict):
            reply = table.get(self.seat_id, table.get("*", {}))
        elif table is not None:
            reply = table
        return dict(reply) if isinstance(reply, dict) else {}

    async def aclose(self) -> None:
        return None


# 11 人狼王板的角色：3 小狼 + 狼王 + 预/女/梦/愚/墓/鸦/幸
ROLES = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf_king", 5: "seer", 6: "witch",
         7: "dreamer", 8: "idiot", 9: "gravekeeper", 10: "crow", 11: "lucky"}
KING_SEAT = 4
WOLVES = [1, 2, 3, 4]

BASE = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "（剧本）刀 9", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
    "dream": {"*": {"target": 11, "reason": "剧本"}},
    "lucky": {"*": {"use": False, "skill": "", "target": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "wolf_king_take": {"*": {"target": 9, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                      "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
    "sheriff_speech": {"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
    "sheriff_vote": {"*": {"vote": 5, "reason": "剧本：投 5 号"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
    "sheriff_order": {"*": {"reverse": False, "reason": "剧本"}},
}


def run(explode_script: dict | None, second_explode: dict | None, **kw):
    script = dict(BASE)
    script["sheriff_run"] = {5: {"run": "front"}, 1: {"run": "front"}, "*": {"run": False}}
    script["wolf_explode"] = explode_script or {"*": {"explode": False}}
    # 第二次自爆：按"第 2 天"区分（同一个 kind，剧本里用 day 判断不了，这里换一个代理）
    log: list = []

    class Second(Scripted):
        async def ask(self, packet, task):
            self.log.append((self.seat_id, task.kind, task))
            if task.kind == "wolf_explode" and "第 2 天" in (task.text or ""):
                table = second_explode or {"*": {"explode": False}}
                reply = table.get(self.seat_id, table.get("*", {}))
                return dict(reply) if isinstance(reply, dict) else {}
            return await super().ask(packet, task)

    script.update(kw.pop("script_over", {}))
    game = Game(board=make_board("eleven_wolfking"), seed=7, max_days=3,
                agent_factory=lambda seat: Second(seat.id, script, log),
                decision_first=True, on_event=lambda *a, **k: None, **kw)
    for sid, role in ROLES.items():
        game.seat_by_id(sid).role_key = role
    asyncio.run(game.run())
    return game, log


def text_has(game, *words) -> bool:
    return any(all(w in e.text for w in words) for e in game.ledger.entries if e.kind == "announce")


# ── 一、狼王不参与自爆决策 ────────────────────────────────────────
print("=== 一、狼王不参与任何自爆决策（省一次调用）===")
g, log = run({"*": {"explode": False}}, None)
asked = sorted({s for s, k, _t in log if k == "wolf_explode"})
check("1a 团队自爆决策只问三头小狼，不问狼王", asked == [1, 2, 3], str(asked))
check("1b 狼王的发言决策里没有 explode 字段",
      not any(k == "speech_plan" and s == KING_SEAT and "explode" in (_t.schema or "")
              for s, k, _t in log),
      str([(_t.schema or "")[:60] for s, k, _t in log if k == "speech_plan" and s == KING_SEAT][:1]))
check("1c 小狼的发言决策里仍然有 explode（只是狼王没有）",
      any(k == "speech_plan" and s == 1 and "explode" in (_t.schema or "") for s, k, _t in log))
check("1d 狼王的上警决策里也没有 explode",
      not any(k == "sheriff_run" and s == KING_SEAT and "explode" in (_t.schema or "")
              for s, k, _t in log))

# ── 二、单狼自爆：不吞警徽，第二天恢复竞选 ─────────────────────────
print("\n=== 二、单爆不吞警徽 → 第二天恢复竞选、选出警长 ===")
g, log = run({1: {"explode": True, "who": 1}, 2: {"explode": True, "who": 1},
              3: {"explode": True, "who": 1}, "*": {"explode": False}}, None)
check("2a 1 号当场出局（死因自爆）",
      g.seat_by_id(1).alive is False and g.seat_by_id(1).death_cause == "explode",
      str(g.seat_by_id(1).death_cause))
check("2b 单爆公告**没有**写「警徽流失」",
      not text_has(g, "自爆", "警徽流失"),
      str([e.text[:50] for e in g.ledger.entries if "自爆" in e.text and e.kind == "announce"][:2]))
check("2c 第二天确实问了剩下的狼「要不要再来一次」",
      any(k == "wolf_explode" and "第 2 天" in (t.text or "") for _s, k, t in log))
check("2d 已发过言的候选人不重复：竞选恢复公告写明", text_has(g, "警长竞选恢复"))
check("2e 警徽最后落在 5 号（唯一存活候选人）", g.sheriff_id == 5, str(g.sheriff_id))
check("2f 全剧没有出现「双狼自爆」", not text_has(g, "双狼自爆"))

# ── 三、双爆：吞警徽 ──────────────────────────────────────────────
print("\n=== 三、双爆吞警徽 ===")
g, log = run({1: {"explode": True, "who": 1}, 2: {"explode": True, "who": 1},
              3: {"explode": True, "who": 1}, "*": {"explode": False}},
             {2: {"explode": True}, 3: {"explode": True}})
check("3a 第二只狼也出局了（双爆成立）",
      sum(1 for s in WOLVES if not g.seat_by_id(s).alive) >= 2,
      str([(s, g.seat_by_id(s).alive) for s in WOLVES]))
check("3b 公告写明「双狼自爆」+ 警徽被吞", text_has(g, "双狼自爆") and text_has(g, "警徽"))
check("3c 本局没有警长", g.sheriff_id is None, str(g.sheriff_id))
check("3d 没有恢复竞选的公告", not text_has(g, "警长竞选恢复"))

# ── 四、其他板子保持旧规则（自爆吞警徽）──────────────────────────
print("\n=== 四、其他板子不受影响（自爆仍吞警徽）===")
script = dict(BASE)
script["sheriff_run"] = {5: {"run": "front"}, 1: {"run": "front"}, "*": {"run": False}}
script["wolf_explode"] = {1: {"explode": True, "who": 1}, "*": {"explode": False}}
log2: list = []
g2 = Game(board=make_board("six_all_reveal"), seed=7, max_days=2,
          agent_factory=lambda seat: Scripted(seat.id, script, log2),
          on_event=lambda *a, **k: None)
# 6 人明牌板没有狼王：1/2 狼、3 预、4 女、5/6 民
for sid, role in {1: "wolf", 2: "wolf", 3: "seer", 4: "witch", 5: "villager", 6: "villager"}.items():
    g2.seat_by_id(sid).role_key = role
asyncio.run(g2.run())
check("4a 6 人板自爆照旧吞警徽（没有警长）", g2.sheriff_id is None, str(g2.sheriff_id))
check("4b 6 人板没有「双狼自爆」的说法", not any("双狼自爆" in e.text for e in g2.ledger.entries))

# ── 五、上警轮里就自爆：第二天要补问没表态的人 ───────────────────
print("\n=== 五、上警轮里早爆：次日补问、竞选不残缺 ===")
s = dict(BASE)
# 1 号狼在自己"要不要上警"的决策里就爆 —— 此时 2~11 号还没被问
s["sheriff_run"] = {1: {"run": True, "explode": True}, 5: {"run": "front"},
                    2: {"run": "front"}, "*": {"run": False}}
g, log = run(None, None, script_over=s)
day1_run = {sid for sid, k, _t in log
            if k == "sheriff_run" and "第 1 天" in (_t.text or "")}
check("5a 第 1 天确实只问到 1 号就爆了（剧本前提成立）", day1_run == {1}, str(day1_run))
check("5b 第二天补问了没表态的人",
      len({sid for sid, k, _t in log
           if k == "sheriff_run" and "补问" in (_t.text or "")}) >= 8,
      str(sorted({sid for sid, k, _t in log if k == "sheriff_run"})))
check("5c 补问公告写明「还没表态的人继续」",
      text_has(g, "恢复上警阶段") and text_has(g, "还没表态"))
check("5d 竞选没有因为残缺而直接流失（选出了警长）", g.sheriff_id is not None, str(g.sheriff_id))
check("5e 台账里没有「恢复竞选时已没有候选人」", not text_has(g, "没有候选人"))

print("\n" + "=" * 64)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)