# coding=utf-8
"""2026-10-30 外部审查那一轮修掉的问题的回归（每条都能变红）。

覆盖：
  1. PK 轮诅咒票只在**候选人**里加（以前能把非候选人放逐）
  2. 摄梦人 target 写糊时**不随机**（以前会随机摄人、可能误判"连摄两晚"）
  3. 幸运儿 target 写糊时**不消耗**技能（以前会随机毒/守/验一个活人）
  4. 投给**已经出局**的人 → 记无效票（以前静默改投活人）
  5. 狼王声称「狼人」时**不再**重复附狼人章（继承章也算"自己的章"）
  6. `Game(seed="abc")` 不崩；`coerce_seat("-3")` 不被抠成 3
  7. `save(stem="../x")` 写不出存档目录

跑法：`cd tests && python t_audit_1030.py`
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context                                  # noqa: E402
from wolf.engine import Game                              # noqa: E402
from wolf.roles import make_board                         # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

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

BASE = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "剧本", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
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
    "dream": {"*": {"target": 10, "reason": "剧本"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
    "lucky": {"*": {"use": False, "reason": "剧本"}},
}


class Scripted:
    def __init__(self, sid, script, holder):
        self.seat_id, self.script, self.holder = sid, script, holder

    async def ask(self, packet, task):
        t = self.script.get(task.kind) or {}
        if callable(t):
            t = t(self.seat_id, task, self.holder.get("game")) or {}
        elif isinstance(t, dict):
            t = t.get(self.seat_id, t.get("*", {}))
        return dict(t) if isinstance(t, dict) else {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


def run(script: dict, *, board: str = "eleven_dream", days: int = 1, seed: int = 7,
        remap: bool = True, decision_first: bool = True):
    holder: dict = {}
    g = Game(board=make_board(board), seed=seed, max_days=days,
             decision_first=decision_first,
             agent_factory=lambda s: Scripted(s.id, script, holder),
             on_event=lambda *a, **k: None)
    holder["game"] = g
    if remap and board == "eleven_dream":
        for sid, role in SEATS.items():
            g.seat_by_id(sid).role_key = role
    asyncio.run(g.run())
    return g


print("=== 1. PK 轮诅咒票：只在候选人里加 ===")
g = Game(board=make_board("eleven_dream"), seed=1, agent_factory=None, on_event=lambda *a, **k: None)
g.cursed_today = 9
check("1a 首轮（不传 candidates）被咒者哪怕 0 票也多一票",
      g.weighted_counts({}) == {9: 1.0}, str(g.weighted_counts({})))
check("1b PK 轮候选人里没有被咒者 → 不加那票",
      g.weighted_counts({}, candidates=[1, 2]) == {}, str(g.weighted_counts({}, candidates=[1, 2])))
check("1c PK 轮被咒者本人是候选人 → 照样加",
      g.weighted_counts({}, candidates=[9]) == {9: 1.0})

print("\n=== 2. 摄梦人 target 写糊：不随机 ===")
s = dict(BASE)
s["dream"] = {"*": {"target": "abc", "reason": "剧本"}}
g = run(s, days=1)
dreamer = g.seat_by_id(7)
check("2a last_dream 没有被随机填上", dreamer.last_dream is None, repr(dreamer.last_dream))
check("2b 私有经历如实写「没给出可摄的目标」",
      any("没给出可摄的目标" in e for e in dreamer.private_events),
      str(list(dreamer.private_events)[-2:]))

print("\n=== 3. 幸运儿 target 写糊：不消耗技能 ===")
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "poison", "target": "abc"}}
g = run(s, days=2, remap=False, decision_first=False)
lucky = next(x for x in g.seats if x.role_key == "lucky")
check("3a lucky_used 仍是 False（一次性技能没被浪费）", lucky.lucky_used is False)
check("3b 私有经历写明「没有消耗」",
      any("没有消耗" in e for e in lucky.private_events), str(list(lucky.private_events)[-2:]))

print("\n=== 4. 投给已经出局的人 → 无效票（不静默改投）===")
s = dict(BASE)
s["dream"] = {"*": {"target": 10, "reason": "剧本"}}
s["vote"] = {1: {"vote": 9, "reason": "剧本"}, "*": {"abstain": True, "reason": "剧本"}}
g = run(s, days=1, decision_first=False)
check("4a 9 号确实已经出局", not g.seat_by_id(9).alive)
texts = [e.text for e in g.ledger.entries]
check("4b 台账里有一条「投给了已经出局的 9号 —— 这一票无效」",
      any("投给了已经出局的 9号" in t and "无效" in t for t in texts),
      str([t for t in texts if "投给" in t][-3:]))
check("4c 没有把它记成投给了别的活人",
      not any(t == "投给 9号" for t in texts))

print("\n=== 5. 狼王声称「我是狼人」：不再重复附狼人章 ===")
g = Game(board="eleven_wolfking", seed=3, agent_factory=None, on_event=lambda *a, **k: None)
king = next(x for x in g.seats if x.role_key == "wolf_king")
g.ledger.add(1, "day", "claim", "我是狼人", seat=king.id,
             data={"claim": "我是狼人"}, visible_to=("public",))
task = context.Task(kind="speech", text="发言", schema="{}", legal=[i for i in range(1, 12)])
packet = context.build(g, king, task)
check("5a 包里没有「备着演狼人用」那一章", "备着演狼人用" not in packet)
g.ledger.add(1, "day", "claim", "我是预言家", seat=king.id,
             data={"claim": "我是预言家"}, visible_to=("public",))
packet2 = context.build(g, king, task)
check("5b 声称预言家时**照常**附预言家章（对照，证明 5a 不是恒真）",
      "备着演预言家用" in packet2 or "预言家" in packet2)

print("\n=== 6. 边界输入 ===")
try:
    g = Game(board=make_board("eleven_dream"), seed="abc", agent_factory=None, on_event=lambda *a, **k: None)
    check("6a seed 写成非数字字符串不崩", True)
except Exception as exc:                                  # noqa: BLE001
    check("6a seed 写成非数字字符串不崩", False, f"{type(exc).__name__}: {exc}")
g = Game(board=make_board("eleven_dream"), seed=1, agent_factory=None, on_event=lambda *a, **k: None)
check("6b coerce_seat('-3') 不会被抠成 3", g.coerce_seat("-3", [1, 2, 3], default=None) is None,
      repr(g.coerce_seat("-3", [1, 2, 3], default=None)))

print("\n=== 7. save(stem) 不能写出存档目录 ===")
# ⚠️ 用 tests/tmp 下的固定目录，不用 TemporaryDirectory：某些沙箱里删临时目录会 PermissionError
tmp = os.path.join(HERE, "tmp", "audit-1030-save")
os.makedirs(tmp, exist_ok=True)
g = Game(board=make_board("eleven_dream"), seed=1, agent_factory=None, on_event=lambda *a, **k: None)
path = g.save(tmp, stem="../outside")
inside = os.path.dirname(os.path.abspath(path)) == os.path.abspath(tmp)
check("7a 存到了 save_dir 里（没跑到上一级）", inside, path)
check("7b 文件名只剩 basename", os.path.basename(path) == "outside.json", os.path.basename(path))

print("\n=== 8. 前端语音队列：非 playing 的出口也要放行（静态判据）===")
# ⚠️ 真实的死锁要真浏览器才验得出来（`t_voice_queue` 把 VOICE.speak 换成了桩）。
#    这里守"代码没被改回去"：speak 只在 playing 时等 ended，其余出口当场 resolve。
_html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
check("8a speak 只在 'playing' 时等 ended", "how !== 'playing'" in _html and "doneResolve()" in _html)
check("8b stop() 也放行等待者（切后台不再永久 busy）",
      "removeAttribute('src')" in _html and "endResolve" in _html)
check("8c 语音队列泵有 busy 复位", "VOICE_Q.busy = false" in _html)

print("\n" + "=" * 74)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
print("=" * 74)
raise SystemExit(1 if BAD else 0)
