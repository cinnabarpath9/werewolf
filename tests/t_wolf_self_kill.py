# coding=utf-8
"""#1：狼队的出刀目标必须是**全体存活玩家**（含狼、含自己）——「自刀」要真的能发生。

用户 2026-10-08 真局反馈（截图里的刀人面板只列了 1/3/4/5/7/8/10 六个好人）：
「狼人刀人选择里没有狼人，这导致狼人无法自刀」；随后拍板：
**「全体存活（队友也能刀，这才有刀队友骗药/做局的空间）」**。

改前 `legal` 只有好人（`good_ids`），而 `coerce_seat` 对非法目标是**静默换成随机一个好人** ——
剧本里写「刀 4 号（狼）」会表现成「4 号没事、某个好人莫名其妙死了」，
正是这个项目最怕的那种静默降级（复盘时看着像 bug）。所以本脚本的判据是**行为**：
刀口指向狼 → 死的必须就是那头狼。

查四件：
  1. 狼收到的 `legal` 名单里**有狼**（含自己）；
  2. 指向狼的刀真的落在狼身上（不是被静默换人）；
  3. 台账如实写「狼队最终决定击杀 4号」；
  4. 对照：指向好人的刀照旧正常（别把正常路改坏）；
  5. `decision_first` 开/关两条路都验（一条走"复用它早先的决定"，一条直接问出刀）。

跑法： python t_wolf_self_kill.py
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
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


class Scripted:
    """按任务类型写死回复的代理；log 记 (座位, 任务类型, 第几天, Task) —— 带 Task 才能查 legal。"""

    def __init__(self, seat_id: int, script: dict, log: list) -> None:
        self.seat_id, self.script, self.log = seat_id, script, log

    async def ask(self, packet, task):
        m = re.search(r"第 (\d+) 天", packet or "") or re.search(r"第 (\d+) 天", task.text or "")
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


#: 11 人标准板的座位表（4 狼 + 预女猎白 + 3 民）—— 4 号是狼，9 号是民
ELEVEN = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
          7: "hunter", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}


def build(script: dict, board: str = "eleven_standard", **kw):
    log: list = []
    # ⚠️ 固定种子：`seed=None` 是"每局随机"，回归就会跟着飘（第一版就是这么写的）
    game = Game(board=make_board(board), seed=7,
                agent_factory=lambda seat: Scripted(seat.id, script, log),
                on_event=lambda *a, **k: None, **kw)
    for sid, role in ELEVEN.items():
        game.seat_by_id(sid).role_key = role
    return game, log


def night_script(target: int) -> dict:
    """一整套剧本（含白天），这样能直接 `await game.run()` 把死亡真正落地。

    ⚠️ `night()` **只返回**死亡名单、从不改 `alive`（见 engine.run 里的注释：
    「死亡都在白天地落地」），所以"4 号真的出局了吗"必须跑完整局才看得到 ——
    只跑 night() 就断言 alive 是错的（第一版就这么写，红了两条）。
    """
    return {
        "wolf_intent": {"*": {"target": target, "reason": "剧本：按剧本刀"}},
        "wolf_talk": {"*": {"speech": "（剧本）就刀他", "target": target}},
        "wolf_kill": {"*": {"target": target, "reason": "剧本：按剧本刀"}},
        "seer_check": {"*": {"target": 1, "reason": "剧本"}},
        "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
        "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
        "guard": {"*": {"target": 9, "reason": "剧本"}},
        # 白天（decision_first=True 走 speech_plan/analyze，关掉时走 speech/vote）
        # ⚠️ reads 要给非空：空列表会被引擎当成"缺字段"，多补问一次发言（剧本局里只是噪音）
        "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                              "vote": None, "speech": "（剧本发言）", "claim": "无"}},
        "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "（剧本发言）", "claim": "无"}},
        "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
        "vote": {"*": {"abstain": True, "reason": "剧本"}},
        "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
        "sheriff_run": {"*": {"run": False, "reason": "剧本"}},
        "sheriff_speech": {"*": {"speech": "（剧本竞选发言）"}},
        "sheriff_vote": {"*": {"abstain": True, "reason": "剧本"}},
        "badge": {"*": {"to": None, "reason": "剧本"}},
    }


def legal_of(log: list, kind: str) -> list[int]:
    for _sid, k, _d, task in log:
        if k == kind:
            return list(getattr(task, "legal", []) or [])
    return []


async def run_night(script: dict, decision_first: bool):
    """只跑**第 1 夜**，看引擎给出的死亡名单。

    ⚠️ 为什么不跑整局：`night()` 只**返回**死亡名单，真正落地在白天的 `day_phase` ——
    而白天那套（决策+发言 / 投票）会引入一堆和本规则无关的随机性（剧本写不全字段还会触发补问），
    第一版就因此飘了（同一脚本 seed=None 时 16/16、seed=7 时 13/16）。
    本规则（刀口合法目标）完全发生在 night() 里，所以判据就钉在它的返回值上；
    "死亡落地"那条链路由 `t_rules_new.py` 的 86 项覆盖。
    """
    game, log = build(script, decision_first=decision_first, max_days=1)
    deaths = await game.night()
    return game, log, [seat for seat, _cause in deaths]


# ── 一、指向狼（自刀/刀队友）────────────────────────────────────────────────────
print("=== 一、刀口指向狼（4 号）：legal 要有狼，死的也必须是狼 ===")
for df in (True, False):
    tag = "decision_first=True（复用早先决定）" if df else "decision_first=False（直接问出刀）"
    game, log, dead = asyncio.run(run_night(night_script(4), df))

    key = "wolf_intent" if df else "wolf_kill"
    legal = legal_of(log, key)
    woke = legal_of(log, "wolf_talk") or legal
    check(f"1a[{tag}] 出刀合法目标里有 4 号（一头狼）", 4 in woke or 4 in legal,
          f"{key}.legal={legal[:6]}… wolf_talk.legal={woke[:6]}…")
    check(f"1b[{tag}] 合法目标里有**自己**（6 号以外的狼也在名单里）",
          all(w in (woke or legal) for w in (1, 2, 3)), f"{sorted(woke or legal)}")
    check(f"1c[{tag}] ⚠️ 死的就是 4 号（没被 coerce_seat 静默换成好人）", dead == [4], str(dead))
    check(f"1d[{tag}] 只有一个死者（没连带别人）", len(dead) == 1, str(dead))
    txt = "\n".join(e.text for e in game.ledger.entries)
    check(f"1e[{tag}] 台账如实写「击杀 4号」", "狼队最终决定击杀 4号" in txt,
          [l for l in txt.splitlines() if "最终决定" in l][:1])

# ── 二、对照：指向好人照旧 ──────────────────────────────────────────────────────
print("\n=== 二、对照：刀口指向好人（9 号·村民）照旧正常 ===")
game2, _log2, dead2 = asyncio.run(run_night(night_script(9), True))
check("2a 死的是 9 号", dead2 == [9], str(dead2))
check("2b 台账写「击杀 9号」",
      any("狼队最终决定击杀 9号" in e.text for e in game2.ledger.entries))

# ── 三、刀到狼之后，结算不许乱 ───────────────────────────────────────────────────
print("\n=== 三、刀到狼之后的结算（不能出现自相矛盾的状态）===")
game3, _l3, dead3 = asyncio.run(run_night(night_script(4), True))
check("3a 死亡名单只有 4 号", dead3 == [4], str(dead3))
check("3b ⚠️ 死者 4 号的身份确实是一头狼（不是被换成好人）",
      game3.seat_by_id(4).role_key == "wolf", str(game3.seat_by_id(4).role_key))
check("3c 台账写明击杀 4 号、且没有第二个人被写死",
      sum(1 for e in game3.ledger.entries if "最终决定击杀" in e.text) == 1
      and any("最终决定击杀 4号" in e.text for e in game3.ledger.entries),
      [e.text for e in game3.ledger.entries if "最终决定击杀" in e.text])
check("3d 女巫被告知的是被刀的那个人（4 号），不是别人",
      any("4号" in ev for ev in game3.seat_by_id(6).private_events),
      str([ev for ev in game3.seat_by_id(6).private_events if "4号" in ev][:1]))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
