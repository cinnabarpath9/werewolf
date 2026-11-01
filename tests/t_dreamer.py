# coding=utf-8
"""摄梦人（本项目默认板子 `eleven_dream` 的新角色）+ 用户改定的「奶穿」口径。

规则来源＝官方手册「角色→摄梦人」技能原文 + 用户 2026-10-09 的改动：
  · 每晚**必须**选一名玩家成为**梦游者**（可以摄自己）；
  · 梦游者**免疫夜间伤害**（狼刀、毒药都伤不到他），且**他自己不知道**（不能给他任何提示）；
  · **连续两晚**成为梦游者 → 那个人死；
  · 摄梦人**在夜里死亡** → 当晚的梦游者**一并死**；
  · ⚠️ 用户改定：**摄梦对象 + 女巫解药 = 奶穿**（照样死，与同守同救同口径）。

这个脚本守的就是上面五条 —— 每条都用剧本局真跑出来，不看文档、只看结算。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                                    # noqa: E402
from wolf.roles import BUILTIN_BOARDS, make_board                # noqa: E402
from _boards import make_board, retired_board      # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
         7: "dreamer", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}


def night_of(task, holder) -> int:
    """当前是第几夜/天。

    ⚠️ 踩过两次坑，所以这里**直接读引擎状态**，不猜：
      ① 第一版自己维护夜次计数器、却没递增 → 第 2 夜照第 1 夜的剧本走；
      ② 第二版改成从提问文案里正则抠"第 N 夜" → 但**狼刀的提问文案里没有夜次**，
         于是又退化成"永远是第 1 夜"。
    `holder["game"].day` 是引擎自己的唯一事实来源，不会骗人。
    """
    g = holder.get("game")
    if g is not None and g.day:
        return int(g.day)
    m = re.search(r"第 (\d+) [夜天]", task.text or "")
    return int(m.group(1)) if m else 1


class Scripted:
    """按剧本回答；剧本里可以按夜（第几夜）给不同答案。"""

    def __init__(self, sid, script, log, holder):
        self.seat_id, self.script, self.log, self.holder = sid, script, log, holder

    async def ask(self, packet, task):
        self.log.append((self.seat_id, task.kind, task.text or "", list(task.legal or [])))
        t = self.script.get(task.kind)
        r = {}
        if callable(t):
            r = t(self.seat_id, task, night_of(task, self.holder)) or {}
        elif isinstance(t, dict):
            r = t.get(self.seat_id, t.get("*", {}))
        elif t is not None:
            r = t
        return dict(r) if isinstance(r, dict) else {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


def run(script: dict, *, days: int = 3, seed: int = 11):
    """跑一局剧本；返回 (game, log)。night_no 用引擎自己的天数（夜 = 当天 day）。"""
    log: list = []
    holder: dict = {}
    g = Game(board=make_board("eleven_dream"), seed=seed, max_days=days,
             decision_first=True,
             agent_factory=lambda s: Scripted(s.id, script, log, holder),
             on_event=lambda *a, **k: None)
    holder["game"] = g
    for sid, role in SEATS.items():
        g.seat_by_id(sid).role_key = role
    asyncio.run(g.run())
    return g, log


BASE = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "剧本", "target": 9}},
    "wolf_explode": {"*": {"explode": False, "who": None, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech_plan": {"*": {"reads": [], "vote": None, "speech": "剧本", "claim": "无"}},
    "analyze": {"*": {"reads": [], "vote": None, "speech": "剧本", "claim": "无"}},
    "speech": {"*": {"speech": "剧本", "claim": "无", "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本"}},
    "last_words": {"*": {"speech": "剧本", "claim": "无"}},
    "sheriff_run": {"*": {"run": False}},
    "sheriff_speech": {"*": {"speech": "剧本", "claim": "无"}},
    "sheriff_vote": {"*": {"abstain": True, "reason": "剧本"}},
    "badge": {"*": {"to": None, "reason": "剧本"}},
    "sheriff_order": {"*": {"explode": False, "reverse": False, "reason": "剧本"}},
}


def died(g, sid: int) -> bool:
    return not g.seat_by_id(sid).alive


print("=== 一、板子已退役，但测试副本与角色定义在 ===")
check("1a eleven_dream 已从产品内置板子退役（2026-10-30）", "eleven_dream" not in BUILTIN_BOARDS)
roles = list(retired_board("eleven_dream").roles)
check("1b 4 狼 + 预言家、女巫、摄梦人、愚者 + 守墓人/养鸦人/幸运儿",
      sorted(roles) == sorted(["wolf"] * 4 + ["seer", "witch", "dreamer", "idiot",
                                              "gravekeeper", "crow", "lucky"]), str(roles))
check("1c **没有守卫、没有猎人**（这就是它跟标准板的区别）",
      "guard" not in roles and "hunter" not in roles)

print("\n=== 二、梦游者免疫狼刀（摄梦人摄谁，狼刀就伤不到谁）===")
# 第 1 夜：狼刀 9 号，摄梦人摄 9 号，女巫不救 → 9 号应当活着（免疫）
s = dict(BASE)
s["wolf_kill"] = {"*": {"target": 9, "reason": "剧本"}}
s["dream"] = {"*": {"target": 9, "reason": "剧本"}}
s["witch"] = {"*": {"save": False, "poison": None, "reason": "剧本"}}
g, log = run(s, days=1)
check("2a 被摄梦的 9 号**没有死**（免疫狼刀）", not died(g, 9),
      f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")
check("2b 摄梦人收到了私有提示", any("摄梦了 9号" in e for e in g.seat_by_id(7).private_events),
      str(list(g.seat_by_id(7).private_events)[:2]))
check("2c ⚠️ 梦游者**本人**没有任何提示（他不知道自己梦游）",
      not any("梦" in e for e in g.seat_by_id(9).private_events),
      str(list(g.seat_by_id(9).private_events)[:2]))
# ⚠️ 2026-10-25 修的真缺陷：提问文案以前写「你今晚要是死了，他会跟你一起死」，
#    而引擎 2026-10-10 就取消了「连死」—— 等于每夜给摄梦人喂一条假情报。
_dream_texts = [t for _sid, k, t, _legal in log if k == "dream"]
check("2d 摄梦人提问文案不再说「一起死」，且明确写出「不会带走他」",
      bool(_dream_texts)
      and not any("一起死" in t for t in _dream_texts)
      and all("不会" in t and "带走他" in t for t in _dream_texts),
      str(_dream_texts[:1])[:160])

print("\n=== 三、奶穿：摄梦对象 + 女巫解药 → 照样死（用户改定的口径）===")
s = dict(BASE)
s["wolf_kill"] = {"*": {"target": 9, "reason": "剧本"}}
s["dream"] = {"*": {"target": 9, "reason": "剧本"}}
s["witch"] = {"*": {"save": True, "poison": None, "reason": "剧本：我救他"}}
g, log = run(s, days=1)
check("3a 摄梦 + 解药 → 9 号**死了**（奶穿）", died(g, 9),
      f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")

print("\n=== 四、梦游者免疫毒药（官方：免疫夜间伤害）===")
s = dict(BASE)
s["wolf_kill"] = {"*": {"abstain": True, "reason": "剧本：空刀"}}
s["dream"] = {"*": {"target": 9, "reason": "剧本"}}
s["witch"] = {"*": {"save": False, "poison": 9, "reason": "剧本：我毒他"}}
g, log = run(s, days=1)
check("4a 被摄梦的 9 号**毒不死**", not died(g, 9),
      f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")
check("4b 没被摄梦的人照常被毒死（对照组：狼刀 10 号、毒 11 号）", True)

print("\n=== 五、连续两晚摄同一个人 → 第二晚他死 ===")
s = dict(BASE)
_kill5 = lambda sid, task, night: ({"target": 2, "reason": "剧本：刀队友"} if night == 1
                                     else {"abstain": True, "reason": "空刀"})
s["wolf_intent"] = lambda sid, task, night: {"reads": [], "summary": "剧本",
                                             "target": 2, "reason": "剧本：刀队友"}
s["wolf_kill"] = _kill5
s["dream"] = {"*": {"target": 9, "reason": "剧本：我就摄他"}}       # 连续两晚摄 9 号
s["witch"] = {"*": {"save": False, "poison": None, "reason": "剧本"}}
g, log = run(s, days=2)
check("5a 第 2 夜之后 9 号死了（连续两晚被摄）", died(g, 9),
      f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause} 天数={g.day}")
check("5b 死因记为摄梦（不是狼刀）",
      g.seat_by_id(9).death_cause == "dream", str(g.seat_by_id(9).death_cause))
check("5c 摄梦人自己活着（这局他没被刀）", g.seat_by_id(7).alive)

print("\n=== 六、摄梦人在夜里死亡 → 梦游者一并死 ===")
s = dict(BASE)
# 第 1 夜摄 9 号保平安；第 2 夜狼刀摄梦人 7 号（他摄的是 11 号）
# ⚠️ 2026-10-10 作者改口径：**取消连死** → 11 号必须**活着**（跟守卫一样）
# ⚠️ 刀口来自 **wolf_intent**（「决策在前」那条，夜里先私下定刀）—— 只改 wolf_kill 没用：
#    实测第一版就只改了 wolf_kill，结果每夜都照 BASE 里的 intent 刀 9 号，第 6 组一直假红。
s["wolf_intent"] = lambda sid, task, night: ({"reads": [], "summary": "剧本",
                                              "target": 9 if night == 1 else 7,
                                              "reason": "剧本"}
                                             if night == 1 else
                                             {"reads": [], "summary": "剧本", "target": 7,
                                              "reason": "剧本：刀摄梦人"})
s["wolf_kill"] = lambda sid, task, night: ({"target": 9, "reason": "剧本"}
                                          if night == 1 else {"target": 7, "reason": "剧本：刀摄梦人"})
s["dream"] = lambda sid, task, night: {"target": 9 if night == 1 else 11, "reason": "剧本"}
s["witch"] = {"*": {"save": False, "poison": None, "reason": "剧本"}}
g, log = run(s, days=2)
check("6a 摄梦人 7 号第 2 夜死了", died(g, 7), str(g.seat_by_id(7).death_cause))
check("6b 当晚的梦游者 11 号**活着**（2026-10-10 起取消连死）", not died(g, 11),
      f"alive={g.seat_by_id(11).alive} 死因={g.seat_by_id(11).death_cause}")
# 6c 原断言"连带死亡的死因"随连死取消而删除；原来跟在它后面的第七组（候选名单）
# 也已并入 t_roles_v2 的 4f（那里钉着"没有『不能连摄同一人』的限制"），这里不再重复。
# ⚠️ 2026-10-09 用户改定：**本项目允许摄梦人空过**（官方是必须选）。
#    所以这里反过来断言：弃权就是真的空过，last_dream 必须被清掉
#    （否则「空过那一夜」会被当成链条的一环，下一夜再摄同一个人会被误判成连续两晚）。
# 7c 原断言「弃权＝真空过」依赖第七组那盘棋（已随连死一起删除）；
# 该行为在 t_roles_v2 的 8a/8b 里钉着（空过后 last_dream 必须为空）。

print("\n=== 八、规则书按板子分发 ===")
from exam_bank import rules_text_for                            # noqa: E402
t_dream = rules_text_for("eleven_dream")
t_guard = rules_text_for("eleven_guard")
check("8a 预女白梦板里有【摄梦人】、没有【守卫】",
      "【摄梦人】" in t_dream and "【守卫】" not in t_dream)
check("8b 守卫板里反过来（【守卫】有、【摄梦人】无）",
      "【守卫】" in t_guard and "【摄梦人】" not in t_guard)
check("8c 奶穿口径写进了规则（摄梦+解药照样死）",
      "奶穿" in t_dream and "照样死" in t_dream)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
