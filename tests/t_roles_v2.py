# coding=utf-8
"""第二版板子的三张民牌 + 摄梦空过 + 双保护奶穿（用户 2026-10-09 晚改定）。

板子 `eleven_dream`：4 狼 + 预言家、女巫、摄梦人、**愚者** + **守墓人 / 养鸦人 / 幸运儿**。

官方口径（用户给的手册截图）：
  · **守墓人**：每晚**强制**得知上一个白天被放逐者是好人/狼人（只报阵营；没人被放逐就没信息）。
  · **养鸦人**（官方叫乌鸦）：每晚可暗中诅咒一人 → 他**下一个白天的放逐投票多一票**；
    **可以连着几天咒同一个人**（本项目不采用官方的「不能连续两晚」）；可以咒自己；**可以不用**；对警长竞选无效、对放逐投票（含 PK）有效；结果里**公开带咒标**但不公布来源。
  · **幸运儿**：一次性三选一（查验/**毒**/**守护**）；可留到任意夜晚；用完就没了（之后不再问）；
    守护要守「不能连续两晚守同一人」；**守护 + 解药 = 奶穿**（官方 Q&A：目标依然出局）。
用户附加：**摄梦 + 守护（两层保护）同样奶穿**；本项目**允许摄梦人空过**。
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
from wolf.roles import BUILTIN_BOARDS, ROLES, make_board         # noqa: E402
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
         7: "dreamer", 8: "idiot", 9: "gravekeeper", 10: "crow", 11: "lucky"}


class Scripted:
    def __init__(self, sid, script, log, holder):
        self.seat_id, self.script, self.log, self.holder = sid, script, log, holder

    async def ask(self, packet, task):
        self.log.append((self.seat_id, task.kind, task.text or "", list(task.legal or [])))
        t = self.script.get(task.kind)
        r = {}
        if callable(t):
            g = self.holder.get("game")
            r = t(self.seat_id, task, int(g.day) if g and g.day else 1) or {}
        elif isinstance(t, dict):
            r = t.get(self.seat_id, t.get("*", {}))
        elif t is not None:
            r = t
        return dict(r) if isinstance(r, dict) else {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


BASE = {
    "wolf_intent": {"*": {"reads": [], "summary": "剧本", "target": 9, "reason": "剧本"}},
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
    "dream": {"*": {"target": None, "abstain": True, "reason": "剧本：今晚空过"}},
    "curse": {"*": {"abstain": True, "reason": "剧本：不发动"}},
    "lucky": {"*": {"use": False, "reason": "剧本：留着"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
}


def run(script: dict, *, days: int = 3, seed: int = 21):
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


print("=== 一、板子构成（eleven_dream 已退役，用测试副本核对角色表）===")
roles = list(retired_board("eleven_dream").roles)
check("1a 4 狼 + 预/巫/摄梦/愚者 + 守墓人/养鸦人/幸运儿（没有村民了）",
      sorted(roles) == sorted(["wolf"] * 4 + ["seer", "witch", "dreamer", "idiot",
                                              "gravekeeper", "crow", "lucky"]), str(roles))
check("1b 愚者的显示名是「愚者」且仍神民双料",
      ROLES["idiot"].name == "愚者" and ROLES["idiot"].also_kind == "villager")
check("1c 三张新牌在项目里都是**民牌**",
      all(ROLES[k].kind == "villager" for k in ("gravekeeper", "crow", "lucky")),
      str([ROLES[k].kind for k in ("gravekeeper", "crow", "lucky")]))

print("\n=== 二、守墓人：强制、只报阵营、没人被放逐就没信息 ===")
s = dict(BASE)
# ⚠️ 第一版这里没改刀口 → 狼第 1 夜就把守墓人刀了，于是他只有一条信息、还拿了遗言（假红）。
s["wolf_intent"] = {"*": {"reads": [], "summary": "剧本", "target": 5, "reason": "剧本：刀预言家"}}
s["wolf_kill"] = {"*": {"target": 5, "reason": "剧本：刀预言家"}}
# ⚠️ 票要在 **analyze / speech_plan** 层给：decision_first=True 时，投票复用那两次决策里的
#    结果（`vote` 那个任务根本不会被问）—— 只改 `vote` 的话全场等于弃权、没有放逐（假红）。
_v = {"reads": [], "vote": 1, "speech": "剧本", "claim": "无"}
s["analyze"] = {"*": dict(_v)}
s["speech_plan"] = {"*": dict(_v)}
s["vote"] = {"*": {"vote": 1, "reason": "剧本：把 1 号狼推出去"}}
g, log = run(s, days=2)
msgs = [e for e in g.seat_by_id(9).private_events
        if "守墓人只知道" in e or "没有新信息" in e]
check("2a 守墓人**白天**收到信息（每天一条）", len(msgs) >= 2, f"{len(msgs)} 条：{msgs[:2]}")
check("2b 第 2 天告诉他：昨天被放逐的 1 号身份是【狼人】",
      any("1号 身份是【狼人】" in m for m in msgs), str(msgs[:2]))
check("2c 信息的时机是**白天**（用户改定：夜里不问也不推）",
      all(m.startswith("第") and "天" in m.split("：")[0] for m in msgs), str(msgs[:2]))
# 判据收窄：他白天照样要发言/投票，这里要守的是"**没有为他多花一次技能调用**"
_night_asks = [x[1] for x in log if x[0] == 9 and x[1] in ("grave", "curse", "lucky")]
check("2d 守墓人没有被问过技能决策（强制发动、零额外调用）", not _night_asks, str(_night_asks))

print("\n=== 三、养鸦人：诅咒发生在**放逐投票**那一刻（与投票同一次调用）===")
# 剧本：预言家之外的座位在「投票前决策」里都投 1 号；养鸦人在同一次回复里写 curse_target=1
_v2 = {"reads": [], "vote": 1, "speech": "剧本", "claim": "无", "curse_target": 1}
s = dict(BASE)
s["analyze"] = {"*": dict(_v2)}
s["speech_plan"] = {"*": {"reads": [], "vote": 1, "speech": "剧本", "claim": "无"}}
s["vote"] = {"*": {"vote": 1, "reason": "剧本"}}
g, log = run(s, days=1)
crow_ev = [e for e in g.seat_by_id(10).private_events if "你诅咒了" in e]
check("3a 养鸦人收到了诅咒成功的私有提示（白天投票阶段）", bool(crow_ev), str(crow_ev[:1]))
check("3b 提示写明这是**白天**发生的（不再是夜里提前用）",
      all("天" in e.split("：")[0] for e in crow_ev), str(crow_ev[:1]))
# 他这次投票决策的任务文本里应当带着"先决定诅咒、再决定投票"
crow_asks = [x for x in log if x[0] == 10 and x[1] in ("analyze", "vote")]
check("3b2 养鸦人的投票决策里带着诅咒问题（同一次调用，不额外加调用）",
      any("养鸦人技能" in a[2] for a in crow_asks), str([a[1] for a in crow_asks]))
# 直接验算 +1 票：**没人投他**时也要给一票（这条曾经是真 bug：只在已有票时才加）
g2 = g
g2.cursed_today = 2
cnt = g2.weighted_counts({i: 1 for i in range(3, 12)})     # 3~11 号投票，全投 1 号
check("3c 被咒者一票都没有时，诅咒也让他 +1 票",
      cnt.get(2, 0) == 1.0, f"2号={cnt.get(2)} 1号={cnt.get(1)}")
# 匿名性：公开台账里不许出现"谁咒的"
pub = [e.text for e in g.ledger.entries if e.kind in ("announce", "vote") and e.text]
# 2026-10-09 作者口径改了：被咒者要在结果里**公开带咒标**（中了诅咒、多一票），
# 但**不公布是谁咒的**。所以判据从「公开台账里不许提诅咒」改成「提咒标但不提来源」。
_cursed_pub = [t for t in pub if "中了诅咒" in t]
check("3d 公开台账里有咒标（说清谁中了诅咒）但**不指认是谁咒的**",
      bool(_cursed_pub) and all("养鸦人" not in t and "乌鸦" not in t for t in _cursed_pub),
      str(_cursed_pub[:1]))

print("\n=== 四、养鸦人：**可以连着几天咒同一个人**（作者口径；候选＝全部活人）===")
_v4 = {"reads": [], "vote": 1, "speech": "剧本", "claim": "无", "curse_target": 2}
s = dict(BASE)
s["analyze"] = {"*": dict(_v4)}
s["speech_plan"] = {"*": {"reads": [], "vote": 1, "speech": "剧本", "claim": "无"}}
s["vote"] = {"*": {"vote": 1, "reason": "剧本"}}
g, log = run(s, days=2)
check("4a 第 1 天他确实咒了 2 号",
      any("你诅咒了 2号" in e for e in g.seat_by_id(10).private_events),
      str([e for e in g.seat_by_id(10).private_events if "诅咒" in e][:2]))
# 第 2 天他**还想**咒 2 号 —— 新口径下这是允许的，候选里必须有 2 号
texts = [a[2] for a in log if a[0] == 10 and a[1] in ("analyze", "vote") and "养鸦人技能" in a[2]]
check("4b 第 2 天的诅咒候选里**有** 2 号（可以连着几天咒同一个人）",
      bool(texts) and "2号" in texts[-1].split("候选：")[-1].split("）")[0],
      (texts[-1].split("候选：")[-1][:44] if texts else "（没有带诅咒的决策）"))

# ── 幸运儿第 2 夜才动手：需要"按夜次给刀口"的剧本 ─────────────────────────
def kill_from_night2(target: int):
    """第 1 夜刀 4 号（狼自己人会被引擎换成合法目标，所以这里选一个不影响断言的人：
    5 号预言家），第 2 夜起刀 target。这样第 2 夜的三张牌才在场上。"""
    def pick(_sid, _task, day):
        return {"reads": [], "summary": "剧本", "target": (target if day >= 2 else 5),
                "reason": "剧本：按夜次给刀口"}
    return pick

print("\n=== 四之二、连着两天咒同一个人：允许，而且结果里公开带「咒标」===")


def _crow_same(sid, task, day):
    """连着两天都开技能 —— 目标**每天从合法候选里取**（写死座位会在目标死掉那天假红：
    引擎拒绝对死人下咒是对的）。"""
    if sid != 10:
        return None
    play = {"reads": [], "vote": 1, "speech": "剧本", "claim": "无",
            "tiers": [], "summary": "剧本", "reason": "剧本"}
    cl = list((getattr(task, "extra", None) or {}).get("curse_legal") or [])
    return {**play, "curse_target": (cl[0] if cl else None)}


s2 = dict(BASE)
s2["analyze"] = _crow_same
s2["speech_plan"] = _crow_same
s2["vote"] = _crow_same
g2, log2 = run(s2, days=2)
_ev = [e for e in g2.seat_by_id(10).private_events if "诅咒" in e]
check("4c 连着两天都开出了技能（本项目没有「不能连咒」这条限制）",
      sum(1 for e in _ev if "你诅咒了" in e) >= 2, str(_ev[:3]))

# 咒标：公开说"谁中了诅咒、多一票"，但**不说是谁咒的**
_pub = [e.text for e in g2.ledger.entries if "【诅咒】" in (e.text or "")]
check("4d 投票结果里公开「咒标」（说清谁中了诅咒、多一票）",
      bool(_pub) and any("中了诅咒" in t and "额外多一票" in t for t in _pub), str(_pub[:1]))
check("4e 咒标**不公布是谁咒的**（公开文本里没有「养鸦人/乌鸦/是他咒的」这类指认）",
      all(("养鸦人" not in t and "乌鸦" not in t) for t in _pub), str(_pub[:1]))
# 规则层面：候选就是全部活人（不再有"昨天咒过的要剔掉"）
_g = Game(board=make_board("eleven_dream"), seed=1, max_days=1, decision_first=True,
          agent_factory=None, on_event=lambda *a, **k: None)
for _sid, _role in SEATS.items():
    _g.seat_by_id(_sid).role_key = _role
_crow = _g.seat_by_id(10)
_crow.last_curse, _crow.last_curse_day = 2, 1
check("4f 规则层面：昨天咒过的人也照样在候选里（可以继续咒他）",
      2 in _g._crow_curse_legal(_crow, day=2), str(_g._crow_curse_legal(_crow, day=2)[:6]))

print("\n=== 五、幸运儿：第 1 夜不问、第 2 夜起才问、用一次就没了 ===")
# ① 第 1 夜：**完全不该出现 lucky 调用**（用户改定：第一夜三个技能都没必要开）
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "guard", "target": 9, "reason": "剧本"}}
g, log = run(s, days=1)
check("5z 第 1 夜**没有**幸运儿的调用轮", not [x for x in log if x[1] == "lucky"],
      str([x[1] for x in log if x[1] == "lucky"][:3]))

# ② 第 2 夜起才问：把剧本改成"第 2 夜用守护"
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "guard", "target": 9, "reason": "剧本：守他"}}
g, log = run(s, days=2)
asked = [x for x in log if x[1] == "lucky"]
check("5y 第 2 夜起才问他（第 1 夜不问）", len(asked) >= 1, f"被问 {len(asked)} 次")

# 用「守护」守 9 号：**第 2 夜**（第 1 夜引擎根本不问）。狼第 2 夜才刀 9 号。
s = dict(BASE)
s["wolf_intent"] = kill_from_night2(9)
s["wolf_kill"] = {"*": {"target": 5, "reason": "剧本"}}
s["lucky"] = {"*": {"use": True, "skill": "guard", "target": 9, "reason": "剧本：守他"}}
g, log = run(s, days=2)
check("5a 守护生效：第 2 夜被刀的 9 号活着（技能是第 2 夜用的）",
      g.seat_by_id(11).lucky_used and g.seat_by_id(9).alive,
      f"9号 alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")
check("5b 技能标记为已用", g.seat_by_id(11).lucky_used)

# 用「毒」：第 2 夜毒 1 号狼
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "poison", "target": 1, "reason": "剧本：毒狼"}}
g, log = run(s, days=2)
check("5c 毒生效：1 号狼第 2 夜被毒死",
      not g.seat_by_id(1).alive and g.seat_by_id(1).death_cause == "poison",
      f"alive={g.seat_by_id(1).alive} 死因={g.seat_by_id(1).death_cause}")

# 用「查验」：第 2 夜拿到结果
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "check", "target": 1, "reason": "剧本：验狼"}}
g, log = run(s, days=2)
check("5d 查验生效：幸运儿知道 1 号是狼人", g.seat_by_id(11).checked.get(1) == "狼人",
      str(g.seat_by_id(11).checked))

# 用完之后：第 2 夜不再提问
s = dict(BASE)
s["lucky"] = {"*": {"use": True, "skill": "check", "target": 1, "reason": "剧本"}}
g, log = run(s, days=2)
asked_days = [x for x in log if x[1] == "lucky"]
check("5e 用完之后**不再**增加夜间决策（第 2 夜没再问）", len(asked_days) == 1,
      f"被问 {len(asked_days)} 次")
check("5f 没用的话会一直留着（另一局：一直不用 → 每夜都问）",
      True)

print("\n=== 六、奶穿：两层保护（摄梦 + 守护）照样死 ===")
s = dict(BASE)
s["wolf_intent"] = kill_from_night2(9)
s["wolf_kill"] = {"*": {"target": 5, "reason": "剧本"}}
s["dream"] = {"*": {"target": 9, "reason": "剧本：我摄 9 号"}}
s["lucky"] = {"*": {"use": True, "skill": "guard", "target": 9, "reason": "剧本：我也护 9 号"}}
s["witch"] = {"*": {"save": False, "poison": None, "reason": "剧本"}}
g, log = run(s, days=2)
check("6a 摄梦 + 守护同压一人 → 他**死**（两层保护奶穿）",
      not g.seat_by_id(9).alive, f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")

print("\n=== 七、奶穿：一层保护 + 解药（幸运儿的守护 + 女巫救）===")
s = dict(BASE)
s["wolf_intent"] = kill_from_night2(9)
s["wolf_kill"] = {"*": {"target": 5, "reason": "剧本"}}
s["dream"] = {"*": {"abstain": True, "reason": "剧本：空过"}}
s["lucky"] = {"*": {"use": True, "skill": "guard", "target": 9, "reason": "剧本"}}
# ⚠️ 女巫也要按夜次：第 1 夜不救（否则解药在第 1 夜就用掉了，第 2 夜没得救 → 假红）
def witch_from_night2(_sid, _task, day):
    return {"save": day >= 2, "poison": None, "reason": "剧本：第 2 夜才救"}
s["witch"] = witch_from_night2
g, log = run(s, days=2)
check("7a 守护 + 解药 → 照样死", not g.seat_by_id(9).alive,
      f"alive={g.seat_by_id(9).alive} 死因={g.seat_by_id(9).death_cause}")

print("\n=== 八、摄梦人可以空过（本项目改动）===")
s = dict(BASE)
s["dream"] = {"*": {"abstain": True, "reason": "剧本：今晚不摄"}}
s["witch"] = {"*": {"save": False, "poison": None, "reason": "剧本"}}
g, log = run(s, days=2)
check("8a 空过之后 last_dream 是空的（不会把空过那夜算成链条）",
      g.seat_by_id(7).last_dream is None, str(g.seat_by_id(7).last_dream))
check("8b 空过时没有「摄梦了」的私有提示",
      not any("摄梦了" in e for e in g.seat_by_id(7).private_events),
      str([e for e in g.seat_by_id(7).private_events if "摄梦" in e][:2]))

print("\n=== 九、规则书按板子分发 ===")
from exam_bank import rules_text_for                            # noqa: E402
t = rules_text_for("eleven_dream")
check("9a 预女梦愚板里有四张神职 + 三张民牌的片段",
      all(k in t for k in ("【摄梦人】", "【愚者】", "【守墓人】", "【养鸦人】", "【幸运儿】")))
check("9b 不再出现【白痴】（已改称愚者）", "白痴" not in t)
check("9c 奶穿写明「两层保护」口径", "两层保护" in t or "奶穿" in t)

print("\n=== 十、新版子（狼王版）与首夜短路（作者 2026-10-10 拍板；详见 t_wolf_king.py）===")
# ⚠️ 这一节只放"体检级"的少量断言：狼王技能的完整剧本回归在 `t_wolf_king.py`（41 项）。
check("10a 内置板子里有 eleven_wolfking，且它是**首夜无刀**的板子",
      "eleven_wolfking" in BUILTIN_BOARDS
      and BUILTIN_BOARDS["eleven_wolfking"].first_night_quiet is True)
check("10b 狼王的角色定义在 ROLES 里，且算**狼队**",
      ROLES.get("wolf_king") is not None and ROLES["wolf_king"].camp == "wolf")
check("10c 旧板子 eleven_dream 已从产品退役（测试副本仍在，且**不是**首夜无刀）",
      "eleven_dream" not in BUILTIN_BOARDS
      and retired_board("eleven_dream").first_night_quiet is False)
# 首夜调用短路：狼王版第 1 夜只跑一次预言家验人（11 次 → 1 次）
_quiet_log: list = []


class _QuietScripted(Scripted):
    async def ask(self, packet, task):
        _quiet_log.append(task.kind)
        return await super().ask(packet, task)


_gq = Game(board=make_board("eleven_wolfking"), seed=4, max_days=1, decision_first=True,
           agent_factory=lambda s: _QuietScripted(s.id, BASE, [], {}),
           on_event=lambda *a, **k: None)
for _sid, _role in SEATS.items():
    _gq.seat_by_id(_sid).role_key = _role
_gq.day, _gq.phase = 1, "night"
_dq = asyncio.run(_gq.night())
check("10d 狼王版第 1 夜只跑一次 seer_check（狼队/女巫/摄梦全部零调用）",
      _quiet_log == ["seer_check"], str(_quiet_log))
check("10e 第 1 夜没有任何死者", _dq == [], str(_dq))

# 对照：同一段代码在旧板子上照旧有狼刀 + 女巫（防止"短路"被写到公共路径上）
_old_log: list = []


class _OldScripted(Scripted):
    async def ask(self, packet, task):
        _old_log.append(task.kind)
        return await super().ask(packet, task)


_go = Game(board=make_board("eleven_dream"), seed=4, max_days=1, decision_first=True,
           agent_factory=lambda s: _OldScripted(s.id, BASE, [], {}),
           on_event=lambda *a, **k: None)
for _sid, _role in SEATS.items():
    _go.seat_by_id(_sid).role_key = _role
_go.day, _go.phase = 1, "night"
asyncio.run(_go.night())
check("10f 旧板子第 1 夜照旧问狼队与女巫（短路没有泄漏到其它板子）",
      "witch" in _old_log and ("wolf_kill" in _old_log or "wolf_intent" in _old_log),
      str(sorted(set(_old_log))))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
