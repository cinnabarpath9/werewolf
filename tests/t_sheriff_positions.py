# coding=utf-8
"""上警三选一：**前置位 / 后置位 / 不上警**（用户 2026-10-09 定的新规则）。

用户口径（原话要点）：
  · 上警决策从"是否上警"改成三选一：**前置位上警 / 后置位上警 / 不上警**；
  · **公示时只公示"上警"**，不写他是前置还是后置（位置到发言时才暴露）；
  · 警上发言顺序：**前置位的先说**（多个前置位按座位号），**后置位的排在前置位之后**
    （听完悍跳与预言家再总结、拉票、号召投票），同样按座位号；
  · 一般口径：预言家、悍跳狼选前置位；其他想听完再表态的人选后置位。

判据全部看**台账里竞选发言的先后**（行为），不看"问过没有"。
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
    def __init__(self, seat_id: int, script: dict, log: list) -> None:
        self.seat_id, self.script, self.log = seat_id, script, log

    async def ask(self, packet, task):
        m = re.search(r"第 (\d+) 天", task.text or "")
        day = int(m.group(1)) if m else 0
        self.log.append((self.seat_id, task.kind, day, task.text or ""))
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
    "wolf_explode": {"*": {"explode": False, "who": None, "reason": "剧本：不爆"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    # ⚠️ 女巫救：造一个"平安夜"，否则刀口那个人（9 号）夜里就死了、上不了警 ——
    #    第一版就是栽在这（发言顺序里少了 9 号、公示里也少一个上警的人）。
    "witch": {"*": {"save": True, "poison": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                          "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "analyze": {"*": {"reads": [{"seat": 1, "camp": "good", "role": "未知", "why": "剧本"}],
                      "vote": None, "speech": "（剧本发言）", "claim": "无"}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
    "sheriff_order": {"*": {"reverse": False, "reason": "剧本"}},
}


def run(run_script: dict, **kw):
    script = dict(BASE)
    script["sheriff_run"] = run_script
    script["sheriff_vote"] = {"*": {"vote": 5, "reason": "剧本：投 5 号（预言家）"}}
    script["sheriff_speech"] = {"*": {"speech": "（剧本竞选发言）", "claim": "无"}}
    log: list = []
    # ⚠️ 同上：本文件测位置机制，默认关掉"预言家锁定上警"（新规则的专项在文件末尾）。
    kw.setdefault("seer_auto_run", False)
    game = Game(board=make_board("eleven_standard"), seed=7, max_days=1,
                agent_factory=lambda seat: Scripted(seat.id, script, log),
                on_event=lambda *a, **k: None, **kw)
    for sid, role in ELEVEN.items():
        game.seat_by_id(sid).role_key = role
    asyncio.run(game.run())
    return game, log


def election_order(game) -> list[int]:
    """台账里【竞选】发言的先后顺序（这就是"警上发言顺序"）。"""
    out: list[int] = []
    for e in game.ledger.entries:
        if e.day == 1 and e.kind == "speech" and e.seat is not None and "【竞选】" in e.text:
            out.append(e.seat)
    return out


# ── 一、前置位先、后置位后（各自按座位号）────────────────────────────────────
print("=== 一、警上发言顺序：前置位 → 后置位，各自按座位号 ===")
g, log = run({3: {"run": "front"}, 5: {"run": "front"},
              7: {"run": "back"}, 9: {"run": "back"}, "*": {"run": False}})
check("1a 发言顺序 = 3、5（前置位）→ 7、9（后置位）", election_order(g) == [3, 5, 7, 9],
      str(election_order(g)))
check("1b ⚠️ 公示**只写「上警」**，不写前置/后置",
      not any(e.kind == "announce" and ("前置" in e.text or "后置" in e.text)
              for e in g.ledger.entries),
      str([e.text for e in g.ledger.entries if e.kind == "announce" and "上警" in e.text][:4]))
check("1c 公示里确实写了这四个人上警",
      sum(1 for e in g.ledger.entries
          if e.kind == "announce" and "上警" in e.text and "不上警" not in e.text) == 4,
      str([e.text for e in g.ledger.entries if e.kind == "announce" and "上警" in e.text][:5]))
check("1d 每个上警的人自己有一条私有记录写着前置/后置（复盘看得到，别人看不到）",
      all(any(e.kind == "private" and e.seat == sid and "置位" in e.text
              for e in g.ledger.entries) for sid in (3, 7)),
      str([e.text for e in g.ledger.entries if e.kind == "private" and "置位" in e.text][:4]))
check("1e 预言家（5 号）拿到的是**前置位**提示词",
      any(s == 5 and k == "sheriff_speech" and "前置位" in t for s, k, _d, t in log),
      str([t[:26] for s, k, _d, t in log if s == 5 and k == "sheriff_speech"]))
check("1f 后置位（7 号）拿到的是**后置位**提示词（并告诉他能总结拉票）",
      any(s == 7 and k == "sheriff_speech" and "后置位" in t and "拉票" in t
          for s, k, _d, t in log),
      str([t[:26] for s, k, _d, t in log if s == 7 and k == "sheriff_speech"]))

# ── 二、向后兼容：旧写法 true/false ────────────────────────────────────────────
print("\n=== 二、旧写法（true/false）仍然管用：true = 上警（按前置位算）===")
g2, _l2 = run({3: {"run": True}, 7: {"run": False}, "*": {"run": False}})
check("2a true 算上警（= 前置位），而且**只有一人上警时直接当选、不打竞选发言**",
      g2.sheriff_id == 3 and election_order(g2) == [],
      f"sheriff={g2.sheriff_id} 竞选发言={election_order(g2)}")
check("2b 只有一人上警 → 直接当选（不受前置/后置影响）", g2.sheriff_id == 3, str(g2.sheriff_id))

# ── 三、中文写法也认（模型未必听话写 front/back）────────────────────────────────
print("\n=== 三、中文写法照样认（前置位/后置位/不上警）===")
g3, _l3 = run({3: {"run": "前置位"}, 7: {"run": "后置位"}, 9: {"run": "不上警"},
               "*": {"run": "不上警"}})
check("3a 顺序仍然对：3（前置）→ 7（后置）", election_order(g3) == [3, 7], str(election_order(g3)))

# ── 四、全是后置位也不能乱序 ───────────────────────────────────────────────────
print("\n=== 四、全选后置位：按座位号从小到大 ===")
g4, _l4 = run({5: {"run": "back"}, 3: {"run": "back"}, 9: {"run": "back"}, "*": {"run": False}})
check("4a 后置位内部按座位号", election_order(g4) == [3, 5, 9], str(election_order(g4)))

# --------------------------------------------------------------------------- #
# 五、预言家**锁定上警**（作者 2026-10-10 口径：不问他、直接算候选人）
# --------------------------------------------------------------------------- #
print("\n=== 五、预言家锁定上警：不发调用、照样上警、公开不泄露 ===")

# 剧本：**其他人全都不上警** → 场上只有预言家一个候选人（这条最能说明"是锁定上警"）
game, log = run({"*": {"run": False}}, seer_auto_run=True)
_seer = 5                                     # ELEVEN 里 5 号是预言家
_asked = [s for s, k, _d, _t in log if k == "sheriff_run"]   # 本文件 log 是元组
check("5a 预言家**没有被问**上警（省掉这一次调用）",
      _seer not in _asked, f"被问的座位={sorted(set(_asked))}")
check("5b 别人照旧被问（只有他一个人免问）",
      set(_asked) == set(ELEVEN) - {_seer}, f"被问的座位={sorted(set(_asked))}")
check("5c 他照样成了候选人：场上只有他一人上警 → 直接当选",
      game.sheriff_id == _seer, f"sheriff={game.sheriff_id}")
_pub = [e.text for e in game.ledger.entries
        if e.kind == "announce" and "上警" in (e.text or "")]
check("5d 公开台账照常写「他上警」，但**不写**他是预言家（写了就是泄漏）",
      any(f"{_seer}号 上警" in t for t in _pub)
      and not any("预言家" in t for t in _pub),
      f"公开上警公告={_pub[:3]}")
# ⚠️ `view()` 要传**座位号**（int）；传 Seat 对象的话，`visible()` 里比的是
#    `f"seat:{seat_id}"` 字符串 → 永远不匹配，什么都查不到（这里踩过一次）。
_priv = [e.text for e in game.ledger.view(_seer, "good")]
check("5e 他自己能看到「为什么这轮没问你」（私有记录）",
      any("锁定上警" in t for t in _priv),
      str([t[:40] for t in _priv if "上警" in t][:2]))

# 边界：因为预言家必上警，**"无人上警→警徽流失"不再可能**，但"全员上警→流失"照旧
game2, log2 = run({"*": {"run": True}}, seer_auto_run=True)
check("5f 全员上警（含锁定的预言家）→ 警徽照样流失（这条规则没被破坏）",
      game2.sheriff_id is None, f"sheriff={game2.sheriff_id}")

# 行为断言：他和**别人**一起上警时，他排在前面、提示词写着"前置位"
game4, log4 = run({7: {"run": "back"}}, seer_auto_run=True)      # 只有 7 号（后置）陪他跑
check("5h 预言家自动进的是**前置位**名单（竞选发言排在最前、提示词写前置位）",
      election_order(game4)[:1] == [_seer]
      and any(x == _seer and k == "sheriff_speech" and "前置位" in t
              for x, k, _d, t in log4),
      f"顺序={election_order(game4)}")

# 开关：关掉之后恢复"也问他一次"
game3, log3 = run({"*": {"run": False}}, seer_auto_run=False)
_asked3 = {s for s, k, _d, _t in log3 if k == "sheriff_run"}
check("5g 关掉开关 → 预言家又被问了（开关真的管用）",
      _seer in _asked3, f"被问的座位={sorted(_asked3)}")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)

