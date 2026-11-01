# coding=utf-8
"""新默认板子 `eleven_wolfking`（狼王版 + 首夜无刀）——作者 2026-10-10 拍板。

规格见 `docs/HANDOFF.md` §56。这一份把四条最容易写错的东西**真跑一遍**：

  1. **首夜无刀**：第 1 夜只跑一次 `seer_check`（狼队/女巫/摄梦人/幸运儿全部跳过）；
  2. **狼王带人**：任何死因都能带（含**被毒**、含**自爆**）、**不能带狼队友**、带谁公开指定；
  3. **结算次序**：死亡结算 → 狼王带人 → **再判屠边** ——
     哪怕好人把「最后一狼（狼王）」票出去，他临死带走某一边最后一张好人牌，**照样算狼队屠边成功**；
  4. **自爆带人并进同一次决策**（`take` 字段）⇒ 不多花一次调用。

外加：接线闸门（规则书按板子发 / 打法书角色章继承 / 前端面板认得 `wolf_king_take`）。
"""
from __future__ import annotations

import asyncio
import io
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


#: 狼王版的座位表（3 狼 + 狼王 + 4 神 + 3 民）
SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf_king",
         5: "seer", 6: "witch", 7: "dreamer", 8: "idiot",
         9: "gravekeeper", 10: "crow", 11: "lucky"}


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
    "wolf_explode": {"*": {"explode": False, "who": None, "take": None, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "wolf_king_take": {"*": {"take": None, "reason": "剧本：不带人"}},
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
    "lucky": {"*": {"use": False, "reason": "剧本：留着"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
}


def build(board: str = "eleven_wolfking", script: dict | None = None, *,
          seats: dict | None = None, days: int = 2, seed: int = 21, **kw):
    log: list = []
    holder: dict = {}
    kw.setdefault("decision_first", True)
    g = Game(board=make_board(board), seed=seed, max_days=days,
             agent_factory=lambda s: Scripted(s.id, script if script is not None else BASE, log, holder),
             on_event=lambda *a, **k: None, **kw)
    holder["game"] = g
    for sid, role in (seats or SEATS).items():
        g.seat_by_id(sid).role_key = role
    return g, log


def kill_off(g, ids, cause="wolf", day=2):
    """把若干座位直接标成「已经出局」（做剧本局面用）。"""
    for sid in ids:
        s = g.seat_by_id(sid)
        s.alive = False
        s.death_day = day
        s.death_cause = cause


print("=== 一、板子与角色定义 ===")
roles = list(BUILTIN_BOARDS["eleven_wolfking"].roles)
check("1a 3 狼 + 狼王 + 预/巫/摄梦/愚者 + 守墓人/养鸦人/幸运儿",
      sorted(roles) == sorted(["wolf"] * 3 + ["wolf_king", "seer", "witch", "dreamer",
                                              "idiot", "gravekeeper", "crow", "lucky"]),
      str(roles))
check("1b 板子带「首夜无刀」开关（且只有它带）",
      BUILTIN_BOARDS["eleven_wolfking"].first_night_quiet is True
      and retired_board("eleven_dream").first_night_quiet is False)
kw = ROLES["wolf_king"]
check("1c 狼王是**狼队**（屠边/胜负按狼算）", kw.camp == "wolf" and kw.kind == "wolf",
      f"camp={kw.camp} kind={kw.kind}")
check("1d 狼王不是神也不是民（不会污染屠边账）", kw.also_kind is None)
check("1e 旧的 eleven_dream 已从产品退役（测试副本仍在）", "eleven_dream" not in BUILTIN_BOARDS)

print("\n=== 二、首夜无刀：第 1 夜只跑一次预言家验人 ===")


async def night1_kinds(board: str) -> list[str]:
    g, log = build(board)
    g.day = 1
    g.phase = "night"
    await g.night()
    return [k for _sid, k, _t, _l in log]


k_new = asyncio.run(night1_kinds("eleven_wolfking"))
check("2a 狼王版第 1 夜**只有** seer_check（一次调用）", k_new == ["seer_check"], str(k_new))
k_old = asyncio.run(night1_kinds("eleven_dream"))
check("2b 旧板子（预女梦愚）第 1 夜照旧有狼刀与女巫用药（对照组）",
      ("wolf_intent" in k_old or "wolf_kill" in k_old) and "witch" in k_old, str(k_old))

g, log = build("eleven_wolfking")
g.day = 1
g.phase = "night"
_deaths = asyncio.run(g.night())
check("2c 第 1 夜没有任何死者（首夜无刀 ⇒ 也不可能有银水）", _deaths == [], str(_deaths))
w_ev = [e for e in g.seat_by_id(4).private_events if "首夜无刀" in e]
check("2d 狼王（以及所有狼）拿到「首夜无刀」的私有说明", bool(w_ev), str(w_ev[:1]))
check("2e 女巫拿到「第一夜不问」的私有说明，台账里也有一条（人类座位只看台账）",
      any("首夜无刀" in e for e in g.seat_by_id(6).private_events)
      and any("首夜无刀" in (e.text or "") and getattr(e, "seat", None) == 6
              for e in g.ledger.entries),
      str(list(g.seat_by_id(6).private_events)[:2]))
check("2f 摄梦人也拿到说明（她第 2 夜起才动手）",
      any("第 2 夜" in e for e in g.seat_by_id(7).private_events))

print("\n=== 三、狼王带人：结算次序（本次改动的核心）===")
# 局面：狼王（4号）是**最后一狼**，场上好人还剩 愚者8/守墓人9/养鸦人10/幸运儿11；
# 神边只剩愚者一张 ⇒ 狼王临死带走 8 号 = 神边清空 ⇒ 按屠边结算**狼胜**。
g, log = build(script={**BASE, "wolf_king_take": {"*": {"take": 8, "reason": "带走最后一张神"}}})
g.day = 2
g.phase = "day"
kill_off(g, [1, 2, 3], day=2)          # 另外三头狼已经出局
kill_off(g, [5, 6, 7], day=2)          # 预言家/女巫/摄梦人已经出局
g.check_win()
check("3a 摆局之后还没结束（狼王还在、两边都没清空）", g.winner is None, str(g.winner))
asyncio.run(g.exile(2, 4, 5.0))
check("3b 狼王被放逐 → 他临死带走 8 号（愚者）", not g.seat_by_id(8).alive,
      f"8号 alive={g.seat_by_id(8).alive} 死因={g.seat_by_id(8).death_cause}")
check("3c 死因记为「被狼王带走」", g.seat_by_id(8).death_cause == "king",
      str(g.seat_by_id(8).death_cause))
check("3d **最后一狼被票出去 + 狼王带走那一边最后一张 = 狼队屠边成功（狼胜）**",
      g.winner == "wolf", f"winner={g.winner}")
check("3e 公告里写清了「亮出狼王身份、带走谁」（公开指定）",
      any("亮出【狼王】身份" in (e.text or "") and "带走 8号" in (e.text or "")
          for e in g.ledger.entries),
      str([e.text for e in g.ledger.entries if "狼王" in (e.text or "")][:1]))
take_asks = [x for x in log if x[1] == "wolf_king_take"]
check("3f 他那一口只问**一次**（king_done 闸门）", len(take_asks) == 1, f"{len(take_asks)} 次")
check("3g 名单里**只有好人**（不能带走狼队友）",
      bool(take_asks) and all(g.seat_by_id(i).camp != "wolf" for i in take_asks[0][3]),
      str(take_asks[0][3] if take_asks else "（没有这次调用）"))
alive_before = sorted(g.alive_ids())
asyncio.run(g.wolf_king_take(g.seat_by_id(4), where="再来一次", daytime=True))
check("3h 重复触发是空操作（技能只有一次）", sorted(g.alive_ids()) == alive_before,
      f"{alive_before} → {sorted(g.alive_ids())}")

print("\n=== 四、狼王夜里被毒也能带人（作者明确：推翻官方例外）===")
g, log = build(script={**BASE, "wolf_king_take": {"*": {"take": 5, "reason": "被毒也要带一个"}}},
               wolf_control=False)
g.day = 2
g.phase = "day"
asyncio.run(g.day_phase([(4, "poison")]))
check("4a 狼王被毒死也带走了一人（5号预言家）",
      not g.seat_by_id(4).alive and not g.seat_by_id(5).alive
      and g.seat_by_id(5).death_cause == "king",
      f"4号={g.seat_by_id(4).death_cause} 5号={g.seat_by_id(5).death_cause}")
check("4b 那一口是在**判胜负之前**落的（否则「狼王死＝狼队崩」会先结算）",
      any(x[1] == "wolf_king_take" for x in log), str([x[1] for x in log][:12]))

print("\n=== 五、自爆 = 白狼王式：带人并进同一次决策 ===")
king_plan = {"reads": [], "vote": None, "speech": "剧本", "claim": "无",
             "explode": True, "take": 5, "reason": "剧本：自爆带人"}
s = dict(BASE)
s["speech_plan"] = {4: king_plan,                       # ⚠️ 键是**整数座位号**（剧本表按 int 查）
                    "*": {"reads": [], "vote": None, "speech": "剧本", "claim": "无"}}
g, log = build(script=s)
g.day = 1
g.phase = "day"
g.sheriff_enabled = False        # 抽掉警长，让流程直接走到 4 号发言前的决策
asyncio.run(g.day_phase([]))
check("5a 狼王自爆当场出局", not g.seat_by_id(4).alive
      and g.seat_by_id(4).death_cause == "explode",
      f"alive={g.seat_by_id(4).alive} cause={g.seat_by_id(4).death_cause}")
check("5b 自爆同时带走 5 号（白狼王式）", not g.seat_by_id(5).alive,
      f"5号 alive={g.seat_by_id(5).alive} 死因={g.seat_by_id(5).death_cause}")
check("5c **没有为这一口多花一次调用**（目标来自同一次决策的 take）",
      not [x for x in log if x[1] == "wolf_king_take"])
check("5d 自爆标记打上了（今天不再发言投票、直接进夜）",
      g.exploded_today == 4, str(g.exploded_today))
check("5e 公告写明自爆 + 带走谁",
      any("自爆" in (e.text or "") for e in g.ledger.entries)
      and any("带走 5号" in (e.text or "") for e in g.ledger.entries))

print("\n=== 六、团队自爆窗口也带上那一口 ===")
g2, log2 = build(script={**BASE, "wolf_explode": {
    4: {"explode": True, "who": 4, "take": 5, "reason": "剧本"},
    "*": {"explode": True, "who": 4, "take": None, "reason": "剧本"}}})
g2.day = 1
exploded = asyncio.run(g2.wolf_explode_round(1, [5, 6]))
check("6a 团队决策过半 → 狼王爆了", exploded and not g2.seat_by_id(4).alive)
check("6b 团队决策里的 take 也被用上了（不多花调用）",
      not g2.seat_by_id(5).alive and not [x for x in log2 if x[1] == "wolf_king_take"],
      f"5号 alive={g2.seat_by_id(5).alive}")

print("\n=== 七、首夜无刀的第一天公告 ===")
g3, _log3 = build()
g3.day = 1
g3.sheriff_enabled = False
asyncio.run(g3.day_phase([]))
ann = [e.text for e in g3.ledger.entries if e.kind == "announce"]
check("7a 第一天的平安夜公告写明是「首夜无刀」造成的（不是守中、不是用药）",
      any("首夜无刀" in t and "平安夜" in t for t in ann), str(ann[:2]))

print("\n=== 八、接线：规则书 / 打法书 / 前端 ===")
from exam_bank import rules_text_for                            # noqa: E402

t_new = rules_text_for("eleven_wolfking")
t_old = rules_text_for("eleven_dream")
check("8a 狼王版规则里有【狼王】与【首夜无刀】",
      "【狼王】" in t_new and "首夜无刀" in t_new)
check("8b 狼王版规则里**没有**【守卫】/【猎人】（不发本局没有的角色）",
      "【守卫】" not in t_new and "【猎人】" not in t_new)
check("8c 旧板子（预女梦愚）**不会**收到「首夜无刀」/【狼王】（发错对象是硬伤）",
      "首夜无刀" not in t_old and "【狼王】" not in t_old)

from wolf import tabletalk                                      # noqa: E402

from _content import playbook_raw  # noqa: E402
book = playbook_raw()
king_view = tabletalk.filter_role(book, "wolf_king", "wolf", "open")
check("8d 狼王的打法书包里有「狼王」章", "狼王（狼队王牌" in king_view)
check("8e 狼王的打法书包里也有**狼人整章**（角色章继承，否则他只有一张牌的知识）",
      "穿身份" in king_view or "倒钩" in king_view, f"len={len(king_view)}")
only = tabletalk.filter_role_only(book, "wolf_king")
check("8f 但「穿身份」参考章里不再重复附整章狼人（同一章不许进包两遍）",
      "狼王（狼队王牌" in only and "## 狼人" not in only, f"len={len(only)}")

html = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
m = re.search(r"const labelMap\s*=\s*\{(.*?)\}\s*;", html, re.S)
check("8g 前端 labelMap 里有 wolf_king_take（真人狼王有面板可点）",
      bool(m) and "wolf_king_take" in m.group(1))
check("8h 前端 fieldMap 把 wolf_king_take 映射到 take 字段",
      "wolf_king_take: 'take'" in html)

print("\n=== 九、整局 mock（新板子）跑通 + 狼王若出局则用过技能 ===")


def mock_game(seed: int):
    g = Game(board=make_board("eleven_wolfking"), seed=seed, max_days=8, decision_first=True,
             agent_factory=lambda s: MockAgent(s, random.Random(seed * 100 + s.id)),
             on_event=lambda *a, **k: None)
    asyncio.run(g.run())
    return g


res = [mock_game(s) for s in (3, 5, 7)]
check("9a 三个种子都能打完（不炸、有结果）",
      all(r.winner in ("wolf", "good", "draw") for r in res),
      str([r.winner for r in res]))
kings = [(r.seat_by_id(i).alive, r.seat_by_id(i).king_done, r.winner)
         for r in res for i in range(1, 12) if r.seat_by_id(i).role_key == "wolf_king"]
check("9b 每局都有且只有一张狼王", len(kings) == 3, str(kings))
check("9c 出局的狼王都用掉了那一口（king_done=True，没有白死）",
      all(kd for alive, kd, _w in kings if not alive), str(kings))
check("9d 狼王**不能**带队友：任何一局里队友都没被他带走",
      all(not (r.seat_by_id(i).death_cause == "king" and r.seat_by_id(i).camp == "wolf")
          for r in res for i in range(1, 12)),
      str([(i, r.seat_by_id(i).camp, r.seat_by_id(i).death_cause)
           for r in res for i in range(1, 12) if r.seat_by_id(i).death_cause == "king"]))

print("\n=== 十、狼人控场的「夜间反击能力」口径（2026-10-11 扩表，原 E31）===")
# 背景：引擎原来只认 `seer/witch/hunter`，而**本板的四神是预/女/摄梦/愚者** ——
# 于是"只剩摄梦人 + 幸运儿（毒还没用）"时会提前判狼胜。作者 2026-10-11 拍板扩表：
# 摄梦人（连摄能杀人）与**技能还没用掉的幸运儿**也要算成反击能力。


def control_case(alive_ids: tuple[int, ...], *, lucky_used: bool = False):
    """造一个"狼已压过好人、但**两边都没被屠干净**"的局面，返回 (能不能反击, check_win)。

    ⚠️ 构造要点：屠边优先于控场判 —— 只留一张神或只留一张民都会先触发屠边（那不是控场）。
    所以每个用例都保证**神边、民边各至少一张**，这样 check_win() 的 wolf 只可能来自控场。
    """
    g, _log = build(days=1)
    for sid in range(1, 12):
        g.seat_by_id(sid).alive = sid in alive_ids
    if 11 in alive_ids:
        g.seat_by_id(11).lucky_used = lucky_used
    return g.good_can_still_kill(), g.check_win()


# 狼王版座位：1~3 狼 / 4 狼王 / 5 预言家 / 6 女巫 / 7 摄梦人 / 8 愚者 / 9 守墓人 / 10 养鸦人 / 11 幸运儿
WOLVES4 = (1, 2, 3, 4)
IDIOT, GK, LUCKY, DREAMER, WITCH = 8, 9, 11, 7, 6

can, winner = control_case(WOLVES4 + (DREAMER, GK))
check("10a 只剩「摄梦人」也算有夜间反击 ⇒ 不判狼控场", can and winner != "wolf",
      f"can_kill={can} winner={winner}")
can, winner = control_case(WOLVES4 + (IDIOT, LUCKY))
check("10b 只剩「技能还没用掉的幸运儿」也算 ⇒ 不判狼控场", can and winner != "wolf",
      f"can_kill={can} winner={winner}")
can, winner = control_case(WOLVES4 + (IDIOT, LUCKY), lucky_used=True)
check("10c 幸运儿的技能**已经用掉** ⇒ 算无反击，判狼控场（回到老口径的那一半）",
      (not can) and winner == "wolf", f"can_kill={can} winner={winner}")
can, winner = control_case(WOLVES4 + (IDIOT, WITCH, GK))
check("10d 女巫即使两瓶药都用完也算（这条**没有**收紧，保持原口径）",
      can and winner != "wolf", f"can_kill={can} winner={winner}")
can, winner = control_case(WOLVES4 + (IDIOT, GK))
check("10e 预/女/猎/摄梦全没了、幸运儿也没了 ⇒ 判狼控场", (not can) and winner == "wolf",
      f"can_kill={can} winner={winner}")
# 老板子（带猎人）不受影响：猎人活着照样算反击
g2, _l2 = build(board="eleven_standard", days=1,
                seats={1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
                       7: "hunter", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"})
for sid in range(1, 12):
    g2.seat_by_id(sid).alive = sid in (1, 2, 3, 4, 7, 9)
check("10f 老板子上「猎人活着」照样算反击（扩表没有动到老口径）",
      g2.good_can_still_kill() and g2.check_win() != "wolf",
      f"can_kill={g2.good_can_still_kill()} winner={g2.check_win()}")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
