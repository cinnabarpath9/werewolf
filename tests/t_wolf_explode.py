# coding=utf-8
"""#13：狼人自爆（用户 2026-10-08 加的变版规则）。

规则口径（照用户原话实现）：
  · 时机：**上警名单已经确定、竞选发言还没开始** —— 四狼各做一次决策，**过半数**才爆；
  · 谁爆：优先听狼队自己写的 `who`（多数提名者），没人指名就用座位号最小的那头同意狼；
  · 后果：自爆者**当场出局并亮明狼人身份**、**警徽直接流失**（竞选作废）、
    今天的发言与投票**全部跳过**；**当夜照常刀人**；
  · `game.wolf_explode: false` → 完全不开这个窗口（一次也不问）。

本脚本用剧本代理跑整局，断言的是**行为**（死了谁、警徽还在不在、今天有没有发言/投票、
当晚有没有照常刀人），不是"问过没有" —— 只断言问过会漏掉"问了但没照做"。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                                  # noqa: E402
from wolf.roles import make_board                             # noqa: E402
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

BASE = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "（剧本）刀 9", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                      "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
    "sheriff_speech": {"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
    "sheriff_vote": {"*": {"vote": 3, "reason": "剧本：投 3 号当警长"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
    "sheriff_order": {"*": {"reverse": False, "reason": "剧本"}},
}


def run(explode_script: dict | None, script_over: dict | None = None, **kw):
    """跑一整局（max_days=2）。

    `explode_script` = 四狼对**团队自爆决策**（上警名单公布后那一轮）的回答（None = 都不爆）；
    `script_over` = 再覆盖别的剧本（例如让狼警长在"定方向"那一次里自爆）；
    `**kw` = 传给 Game 的参数（例如 `decision_first=True`）。
    """
    script = dict(BASE)
    script["sheriff_run"] = {3: {"run": True}, 5: {"run": True}, "*": {"run": False}}
    script["wolf_explode"] = explode_script if explode_script is not None else {"*": {"explode": False}}
    script.update(script_over or {})
    log: list = []
    game = Game(board=make_board("eleven_standard"), seed=7, max_days=2,
                agent_factory=lambda seat: Scripted(seat.id, script, log),
                on_event=lambda *a, **k: None, **kw)
    for sid, role in ELEVEN.items():
        game.seat_by_id(sid).role_key = role
    asyncio.run(game.run())
    return game, log


def day1_speeches(game) -> list[int]:
    """第 1 天**轮次发言**的座位（竞选发言 kind 也是 speech，所以从'上警名单公告'之后算起）。"""
    out: list[int] = []
    started = False
    for e in game.ledger.entries:
        if e.day != 1 or e.phase != "day":
            continue
        if e.kind == "announce" and ("上警" in e.text or "自爆" in e.text or "当选警长" in e.text
                                     or "警徽流失" in e.text):
            started = True
            continue
        if started and e.kind == "speech" and e.seat is not None:
            out.append(e.seat)
    return out


def day1_round_speeches(game) -> list[int]:
    """第 1 天的**轮次发言**。

    ⚠️ 竞选发言 / PK / 遗言**也**用 kind=speech 记账（文案前缀区分，见 engine 里的说明），
    所以这里要把它们排掉：第一夜死者的遗言在竞选之前就发生过了（它不算"今天的轮次发言"）。
    """
    return [e.seat for e in game.ledger.entries
            if e.day == 1 and e.kind == "speech" and e.seat is not None
            and not any(x in (e.text or "") for x in ("【竞选】", "【PK】", "【遗言】"))]


def day1_exile_votes(game) -> list[int]:
    """第 1 天的**放逐票**（不算投警长那一轮）。"""
    return [e.seat for e in game.ledger.entries
            if e.day == 1 and e.kind == "vote" and e.seat is not None
            and not (e.data or {}).get("sheriff_vote")]


def day1_votes(game) -> list[int]:
    return [e.seat for e in game.ledger.entries if e.day == 1 and e.kind == "vote" and e.seat is not None]


# ── 一、多数同意 → 爆（吞警徽、今天不再发言投票、当晚照常刀人）────────────────────
print("=== 一、四狼都同意、点名 3 号自爆 ===")
g, log = run({1: {"explode": True, "who": 3}, 2: {"explode": True, "who": 3},
              3: {"explode": True, "who": 3}, 4: {"explode": True, "who": 3}})
askers = sorted(set(s for s, k, _d, _t in log if k == "wolf_explode"))
check("1a 问了活着的四头狼（每头一次；这是本规则的全部代价）", askers == [1, 2, 3, 4], str(askers))
check("1b ⚠️ 3 号当场出局", g.seat_by_id(3).alive is False)
check("1c 死因记为自爆", g.seat_by_id(3).death_cause == "explode", str(g.seat_by_id(3).death_cause))
check("1d ⚠️ 警徽流失（sheriff_id 仍是 None，竞选作废）", g.sheriff_id is None, str(g.sheriff_id))
check("1e 台账公开写明自爆 + 警徽流失",
      any(e.kind == "announce" and "自爆" in e.text and "警徽流失" in e.text
          for e in g.ledger.entries),
      str([e.text[:40] for e in g.ledger.entries if "自爆" in e.text and e.kind == "announce"][:1]))
check("1f ⚠️ 今天**没有轮次发言**（全部跳过）", day1_speeches(g) == [], str(day1_speeches(g)))
check("1g ⚠️ 今天**没有投票**（全部跳过）", day1_votes(g) == [], str(day1_votes(g)))
check("1h **当夜照常刀人**：夜里仍然出了刀（台账里有击杀记录）",
      sum(1 for e in g.ledger.entries if "最终决定击杀" in e.text) >= 1,
      str([e.text for e in g.ledger.entries if "最终决定击杀" in e.text]))
check("1i 自爆之后游戏继续到第 2 天（没有卡死）", g.day >= 2, f"day={g.day}")
check("1j 自爆者的私有记录里留了一笔（复盘用）", "自爆" in (g.seat_by_id(3).mind or ""),
      (g.seat_by_id(3).mind or "")[-40:])

# ── 二、没过半 → 不爆（竞选照常走完）────────────────────────────────────────────
print("\n=== 二、只有两头狼同意（4 头里 2 头，没过半）→ 不爆 ===")
g2, log2 = run({1: {"explode": True, "who": 1}, 2: {"explode": True, "who": 1},
                3: {"explode": False}, 4: {"explode": False}})
check("2a 3 号没事（没被拿去爆）", g2.seat_by_id(3).alive is True)
check("2b 竞选照常走完：有人当选警长", g2.sheriff_id is not None, str(g2.sheriff_id))
check("2c 今天有轮次发言", len(day1_speeches(g2)) >= 1, str(day1_speeches(g2)[:5]))
check("2d 狼队私有记录写了决策过程（谁同意谁不同意）",
      any(e.kind == "private" and "自爆决策" in e.text for e in g2.ledger.entries))

# ── 三、开关关掉 → 一次也不问 ───────────────────────────────────────────────────
print("\n=== 三、wolf_explode=false：完全不开这个窗口 ===")
g3, log3 = run({1: {"explode": True, "who": 1}, 2: {"explode": True, "who": 1},
                3: {"explode": True, "who": 1}, 4: {"explode": True, "who": 1}},
               wolf_explode=False)
check("3a 一次都没问 wolf_explode", not [x for x in log3 if x[1] == "wolf_explode"])
check("3b 一切照旧：有人当选警长、今天有发言",
      g3.sheriff_id is not None and len(day1_speeches(g3)) >= 1,
      f"sheriff={g3.sheriff_id} 发言数={len(day1_speeches(g3))}")

# ── 四、谁爆听狼队指名的 ────────────────────────────────────────────────────────
print("\n=== 四、四狼都同意、但点名 1 号自爆（不是座位号最小的同意者说了算）===")
g4, _l4 = run({1: {"explode": True, "who": 1}, 2: {"explode": True, "who": 1},
               3: {"explode": True, "who": 1}, 4: {"explode": True, "who": 1}})
check("4a 出局的是被点名的 1 号", g4.seat_by_id(1).alive is False and g4.seat_by_id(3).alive is True,
      f"1号 alive={g4.seat_by_id(1).alive} 3号 alive={g4.seat_by_id(3).alive}")

# ── 五、前端必须认识这个新面板 ──────────────────────────────────────────────────
print("\n=== 五、前端面板（真人坐狼时要点得到）===")
html = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
check("5a 前端 labelMap 里有 wolf_explode（否则真人狼只会看到'轮到你行动'）",
      bool(re.search(r"\bwolf_explode\s*:", html)))
check("5b 前端有 wolf_explode 的面板分支", "ask.kind === 'wolf_explode'" in html)

# ── 六、自爆**不额外占调用**：它就是"发言前那次决策"里的第一个字段 ───────────────
#   用户 2026-10-09 纠正过一版：我早先做成"每头狼每次发言多问一次"（+3 次/天）是错的。
#   正确口径：白天每个角色发言前本来就有一次高思考调用，狼就在这一次里**先输出要不要自爆**，
#   不自爆再走"怎么打、怎么发言"。**每局只多上警名单公布后那一轮四狼决策（+4 次）**。
print("\n=== 六、自爆不额外占调用（只有上警后那一轮 +4 次）===")
NINE = {1: "wolf", 2: "wolf", 3: "wolf", 4: "seer", 5: "witch",
        6: "hunter", 7: "villager", 8: "villager", 9: "villager"}


def run_nine(explode: dict, speech_plan: dict | None = None, **kw):
    """9 人板（无警长，省掉竞选那一套）：白天发言顺序 = 1..9，狼是 1、2、3。"""
    script = dict(BASE)
    script["wolf_explode"] = explode
    if speech_plan:
        script["speech_plan"] = speech_plan
    log: list = []
    game = Game(board=make_board(NINE_BOARD), seed=7, max_days=2,
                decision_first=True,          # 走"发言前决策"那条路（自爆就在那一次里）
                agent_factory=lambda seat: Scripted(seat.id, script, log),
                on_event=lambda *a, **k: None, **kw)
    for sid, role in NINE.items():
        game.seat_by_id(sid).role_key = role
    asyncio.run(game.run())
    return game, log


def kinds_of(log: list, seat: int) -> list[str]:
    return [k for s, k, _d, _t in log if s == seat]


def texts_of(log: list, seat: int, kind: str) -> list[str]:
    # 注意：Scripted 记的第 4 项是 **Task 对象**（要看 schema/text 才拿得到）。
    out = []
    for s, k, _d, t in log:
        if s == seat and k == kind:
            out.append((getattr(t, "text", "") or "") + " " + str(getattr(t, "schema", "")))
    return out


g6, log6 = run_nine({"*": {"explode": False}})
for who, wolfish in ((1, True), (4, False)):
    plans = texts_of(log6, who, "speech_plan")
    joined = " ".join(plans)
    check(f"6{'a' if wolfish else 'b'} {'狼' if wolfish else '好人'}（{who} 号）的发言决策里"
          f"{'**有**' if wolfish else '**没有**'}自爆这一项",
          ("自爆" in joined) == wolfish and ("explode" in joined) == wolfish,
          (plans[0][:60] if plans else "（没有 speech_plan 调用）"))
check("6c ⚠️ 发言回合**不多花调用**：狼和好人都只有 1 次 speech_plan",
      kinds_of(log6, 1).count("speech_plan") == 1 and kinds_of(log6, 4).count("speech_plan") == 1,
      f"1号={kinds_of(log6, 1)}")
check("6d 9 人板没有竞选 → 一次 wolf_explode 都不问（只有开竞选那局才有那一轮）",
      not [1 for _s, k, _d, _t in log6 if k == "wolf_explode"],
      str(sorted(set(k for _s, k, _d, _t in log6))[:9]))

g7, log7 = run_nine({"*": {"explode": False}},
                    # 狼 1 号在自己"发言前决策"里直接填 explode=true（同一次调用，不发第二次言）
                    {1: {"explode": True,
                         "reads": [{"seat": 2, "camp": "good", "role": "未知", "why": "剧本"}],
                         "vote": None, "speech": "", "stance": "", "claim": "无",
                         "summary": "剧本", "reason": "剧本：我在发言决策里选了自爆"}})
check("7a ⚠️ 1 号（第一个发言的狼）在**发言决策里**选自爆 → 当场出局",
      g7.seat_by_id(1).alive is False and g7.seat_by_id(1).death_cause == "explode",
      f"alive={g7.seat_by_id(1).alive} cause={g7.seat_by_id(1).death_cause}")
check("7b ⚠️ 今天后面的发言全部跳过（1 号爆了之后没人发言）",
      day1_speeches(g7) == [], str(day1_speeches(g7)))
check("7c ⚠️ 今天没有投票", day1_votes(g7) == [], str(day1_votes(g7)))
check("7d 当晚照常刀人", sum(1 for e in g7.ledger.entries if "最终决定击杀" in e.text) >= 1,
      str([e.text for e in g7.ledger.entries if "最终决定击杀" in e.text]))
check("7e 1 号那次发言**没有多花第二次调用**（只有 1 次 speech_plan）",
      kinds_of(log7, 1).count("speech_plan") == 1, str(kinds_of(log7, 1)))

# ── 八、警长是狼：团队轮没爆，它在"定发言方向"那一次决策里爆 ──────────────────
print("\n=== 八、警长是狼：在「定发言方向」的同一次决策里选自爆 ===")
g8, log8 = run({3: {"explode": False, "who": None, "reason": "剧本：团队轮不爆"}},
               script_over={"sheriff_order": {3: {"explode": True, "reverse": False,
                                                   "reason": "剧本：定方向时爆"}}})
orders = texts_of(log8, 3, "sheriff_order")
check("8a 3 号（狼警长）拿到的「定方向」调用里带着自爆这一项（同一次调用）",
      bool(orders) and "explode" in orders[0], (orders[0][:70] if orders else "（没问）"))
check("8b ⚠️ 它在这一次里选了自爆 → 没有「决定今天顺序/逆序」那条公告",
      not any(e.kind == "announce" and "决定今天" in e.text for e in g8.ledger.entries),
      str([e.text[:40] for e in g8.ledger.entries if e.kind == "announce" and "决定今天" in e.text]))
check("8c 3 号当场出局（亮身份）",
      g8.seat_by_id(3).alive is False and g8.seat_by_id(3).death_cause == "explode",
      f"alive={g8.seat_by_id(3).alive} cause={g8.seat_by_id(3).death_cause}")
check("8d 今天**轮次发言与放逐投票**全部跳过（竞选那一段在自爆前就结束了，不算）",
      day1_round_speeches(g8) == [] and day1_exile_votes(g8) == [],
      f"轮次发言={day1_round_speeches(g8)[:6]} 放逐票={day1_exile_votes(g8)[:6]}")
check("8e 「定方向」那次调用没有多花第二次（只有 1 次 sheriff_order）",
      kinds_of(log8, 3).count("sheriff_order") == 1, str(kinds_of(log8, 3)))
check("8f 当晚照常刀人", sum(1 for e in g8.ledger.entries if "最终决定击杀" in e.text) >= 1)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
