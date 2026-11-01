# coding=utf-8
"""夜里到底调用了谁 + 死掉的座位还会不会被调用（调用清单自检）。

跑法： python t_night_calls.py
离线、不联网、不花钱：脚本化 mock 代理 + 真 MockAgent，两种都是零模型调用。

这份自检回答用户提的两个问题：
  ① 死掉的座位是不是真的不再被调用了（含「翻牌白痴不再投票」）；
  ② 女巫两瓶药都用完之后，夜里还会不会被调用。
外加把**夜里那份调用清单**钉死 —— 特别是有守卫的板子（`eleven`）：守卫活着，每晚都要调用。

判定全部基于**行为**（谁在什么时候被调用了、调用那一刻他活着没有），不盯代码写法。
"""
from __future__ import annotations

import asyncio
import difflib
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
sys.path.insert(0, WW)
os.chdir(WW)

from wolf.agents import MockAgent                       # noqa: E402
from wolf.engine import Game                            # noqa: E402
from wolf.roles import BoardDef, make_board
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）
from _boards import NINE_BOARD                     # 测试专用 9 人板（产品的 9 人板已删）

RESULTS: list[tuple[str, bool, str]] = []

#: 夜里会发生的任务类型（白天那些不算）
NIGHT_KINDS = ("wolf_intent", "wolf_talk", "wolf_kill", "seer_check", "guard", "witch")
#: 允许**故意**叫已经出局的座位：遗言 / 警徽传承撕掉 / 猎人死后那一枪
#: （后两个是角色技能，本来就发生在死后 —— 不是漏检查）
DEAD_OK = ("last_words", "badge", "hunter_shoot")
#: 只有模型座位自己做的内部决策 —— 真人座位绝不该被问
INTERNAL_KINDS = ("analyze", "speech_plan", "wolf_intent")
#: 投票类任务 —— 翻牌白痴没有投票权，不该被叫去投
VOTE_KINDS = ("vote", "sheriff_vote")
#: 带**候选名单**（legal）的投票/决策任务 —— 翻牌白痴也不该出现在这些名单里
POOL_KINDS = VOTE_KINDS + ("analyze",)
#: 夜里**该**被调用的角色 → 允许的任务类型。
#: 没列进来的角色（村民 / 白痴 / 猎人）夜里做不了事 → 一次都不该被叫。
NIGHT_ROLE_KINDS = {
    "wolf": ("wolf_intent", "wolf_talk", "wolf_kill"),
    "seer": ("seer_check",),
    "witch": ("witch",),
    "guard": ("guard",),
}

OFF = object()          # 剧本哨兵：这个 kind 没写 → 交回 MockAgent


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


@dataclass
class Call:
    """一次调用：谁、什么任务、第几天、什么阶段、那一刻他活着没有。"""
    seat: int
    kind: str
    day: int
    phase: str            # night / day（从上下文包的【当前阶段】读）
    task: object
    alive: bool
    idiot: bool           # 调用那一刻已经翻牌了？
    human: bool
    packet: str


def _phase_of(packet: str, text: str) -> tuple[int, str]:
    m = re.search(r"【当前阶段】第 (\d+) 天 · (夜晚|白天)", packet or "")
    if m:
        return int(m.group(1)), ("night" if m.group(2) == "夜晚" else "day")
    m = re.search(r"第 (\d+) 天", packet or "") or re.search(r"第 (\d+) 天", text or "")
    return (int(m.group(1)) if m else 0), "?"


def _alive_in_packet(packet: str) -> list[int]:
    """从上下文包的【存活玩家】那一行抠出名单（用来验「死人没有出现在别人眼里」）。"""
    m = re.search(r"【存活玩家】(.+)", packet or "")
    return [int(x) for x in re.findall(r"(\d+)号", m.group(1))] if m else []


class CountingMock(MockAgent):
    """MockAgent + 计数（离线的脚本化 mock）。

    剧本里写了的 kind 按剧本回；没写的交回 MockAgent（随机但合法）。
    每一条调用都记下**当时**这个座位是不是还活着 —— 「死人不该再被调用」这条判定就靠它。
    """

    def __init__(self, seat, rng, script=None, log=None, game=None):
        super().__init__(seat, rng)
        self.script = dict(script or {})
        self.log: list[Call] = log if log is not None else []
        self.game = game
        #: 违规留痕：双药都用完还被问 / 死人被问 / 翻牌白痴被叫去投票
        self.potion_leak: list[tuple] = []
        self.dead_leak: list[tuple] = []
        self.idiot_vote_leak: list[tuple] = []

    async def ask(self, packet, task):
        day, phase = _phase_of(packet, task.text)
        alive = bool(self.seat.alive)
        idiot = bool(self.seat.revealed_idiot)
        self.log.append(Call(self.seat.id, task.kind, day, phase, task,
                             alive, idiot, bool(self.seat.is_human), packet))
        if not alive and task.kind not in DEAD_OK:
            self.dead_leak.append((self.seat.id, task.kind, day, phase))
        if idiot and task.kind in VOTE_KINDS:
            self.idiot_vote_leak.append((self.seat.id, task.kind, day))
        if (task.kind == "witch" and self.game is not None
                and not (self.game.witch_heal or self.game.witch_poison)):
            self.potion_leak.append((self.seat.id, day))
        table = self.script.get(task.kind, OFF)
        if table is OFF:
            return await super().ask(packet, task)
        if isinstance(table, dict):
            reply = table.get(self.seat.id, table.get("*", {}))
        else:
            reply = table
        if callable(reply):
            reply = reply(self.seat.id, task, day)
        return dict(reply or {})


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def by_day(table: dict):
    """按天数给回复（没写的那天沿用最后一条）—— 跟 t_rules_new 的剧本写法一致。"""
    def reply(seat_id, task, day=1):
        keys = sorted(table)
        return dict(table[day if day in table else keys[-1]])
    return reply


def make_game(board, assign: dict, script: dict, *, seed: int = 7, human_seat: int = 0,
              decision_first: bool = True, max_days: int = 3, log: list | None = None,
              skip_when_unanimous: bool = True):
    """建一局：座位角色按 `assign` 写死（这样剧本才是确定的），代理是 CountingMock。"""
    log = [] if log is None else log
    rng = random.Random(seed)
    game = Game(board=make_board(board), human_seat=human_seat, seed=seed, max_days=max_days,
                decision_first=decision_first,
                wolf_talk_skip_when_unanimous=skip_when_unanimous,
                on_event=lambda *a, **k: None)
    for sid, role in assign.items():
        game.seat_by_id(sid).role_key = role
    holders: list[CountingMock] = []

    def factory(seat):
        agent = CountingMock(seat, rng, script, log, game)
        holders.append(agent)
        return agent

    game.agent_factory = factory
    game.holders = holders                                  # 测试自己拿着，方便查违规留痕
    return game, log


def counts(log: list[Call], kinds=None, day=None, phase=None) -> dict[str, int]:
    out: Counter = Counter(c.kind for c in log
                           if (kinds is None or c.kind in kinds)
                           and (day is None or c.day == day)
                           and (phase is None or c.phase == phase))
    return dict(out)


def seats_of(log: list[Call], kind: str, day=None) -> list[int]:
    return sorted(c.seat for c in log if c.kind == kind and (day is None or c.day == day))


def night_counts(log: list[Call], day=None) -> dict[str, int]:
    """夜间各任务类型的调用次数（六种夜间任务之外的都不算）。"""
    out = {k: 0 for k in NIGHT_KINDS}
    out.update(counts(log, kinds=NIGHT_KINDS, day=day))
    return {k: out[k] for k in NIGHT_KINDS}


def per_seat_night(log: list[Call]) -> dict[int, dict[str, int]]:
    out: dict[int, Counter] = {}
    for c in log:
        if c.kind in NIGHT_KINDS:
            out.setdefault(c.seat, Counter())[c.kind] += 1
    return {sid: dict(v) for sid, v in sorted(out.items())}


def only_kinds(log: list[Call], seat: int) -> set[str]:
    return {c.kind for c in log if c.seat == seat}


def leaks(game) -> list[str]:
    bad: list[str] = []
    for agent in getattr(game, "holders", []):
        for item in agent.dead_leak:
            bad.append(f"死人被调用：{item}")
        for item in agent.idiot_vote_leak:
            bad.append(f"翻牌白痴被叫去投票：{item}")
        for item in agent.potion_leak:
            bad.append(f"女巫双药都用完还被问：{item}")
    return bad


# --------------------------------------------------------------------------- #
# 剧本 / 角色表
# --------------------------------------------------------------------------- #

#: 统一剧本：夜里谁也不死（女巫救下狼刀目标会被剧本否掉 —— 这里是她不用药），白天全员弃权。
QUIET_SCRIPT = {
    "wolf_intent": {"*": {"target": 9, "reason": "剧本", "summary": "剧本判断"}},
    "wolf_talk": {"*": {"speech": "（剧本）跟队友走。", "target": 9}},
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "guard": {"*": {"target": 8, "reason": "剧本"}},
    "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
    "sheriff_run": {"*": {"run": False, "reason": "剧本"}},
    "sheriff_speech": {"*": {"speech": "（剧本竞选发言）", "claim": "无", "reason": "剧本"}},
    "sheriff_vote": {"*": {"vote": 1, "reason": "剧本"}},
    "vote": {"*": {"abstain": True, "reason": "剧本：弃权"}},
    "badge": {"*": {"to": None, "reason": "剧本：撕掉"}},
}

#: 11 人**守卫**板（eleven）：4 狼 + 预/女/猎/守 + 3 民
ELEVEN_GUARD = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
                7: "hunter", 8: "guard", 9: "villager", 10: "villager", 11: "villager"}
#: 11 人**标准**板（eleven_standard）：4 狼 + 预/女/猎/白痴 + 3 民（**没有守卫**）
ELEVEN_STD = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
              7: "hunter", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}
#: 9 人标准板：3 狼 + 预/女/猎 + 3 民

NINE = {1: "wolf", 2: "wolf", 3: "wolf", 4: "seer", 5: "witch", 6: "hunter",
        7: "villager", 8: "villager", 9: "villager"}


# --------------------------------------------------------------------------- #
# A. 夜里的调用清单
# --------------------------------------------------------------------------- #

async def t_night_manifest() -> None:
    print("\n=== A. 夜里的调用清单（按角色，直接驱动 night()）===")
    # ⚠️ 2026-10-09 起：**四狼刀口一致时跳过夜聊**（作者批准的成本折中）。
    #    QUIET_SCRIPT 里四头狼的 wolf_intent 都写 9 号 → 一致 → wolf_talk = 0。
    want_guard = {"wolf_intent": 4, "wolf_talk": 0, "wolf_kill": 0,
                  "seer_check": 1, "guard": 1, "witch": 1}
    want_std = {"wolf_intent": 4, "wolf_talk": 0, "wolf_kill": 0,
                "seer_check": 1, "guard": 0, "witch": 1}
    for label, board, assign, want in (
            ("eleven（有守卫）", "eleven", ELEVEN_GUARD, want_guard),
            ("eleven_standard（无守卫）", "eleven_standard", ELEVEN_STD, want_std)):
        g, log = make_game(board, assign, QUIET_SCRIPT, seed=3, decision_first=True)
        g.day = 1
        g.phase = "night"
        await g.night()
        got = night_counts(log)
        print(f"    {label}：{got}")
        check(f"A1 {label}：夜里各任务的调用次数与清单一致", got == want, f"{got} != {want}")

        per = per_seat_night(log)
        idle = [s.id for s in g.seats if s.role_key in ("villager", "idiot", "hunter")]
        check(f"A2 {label}：村民/白痴/猎人夜里一次都没被调用",
              all(per.get(sid) is None for sid in idle),
              f"{[(sid, per.get(sid)) for sid in idle]}")
        guard_seat = next((s for s in g.seats if s.role_key == "guard"), None)
        if guard_seat is None:
            check(f"A3 {label}：这块板子没有守卫 → 夜里没有 guard 调用",
                  got["guard"] == 0 and 8 not in per,
                  f"8号 是 {g.seat_by_id(8).role_key}，夜里被调用 {per.get(8)}")
        else:
            check(f"A3 {label}：守卫活着 → 每晚调用他一次（用户清单里漏掉的就是这条）",
                  per.get(guard_seat.id, {}).get("guard") == 1,
                  f"{guard_seat.id}号（守卫）夜里被调用 {per.get(guard_seat.id)}")
        seer_seat = next(s for s in g.seats if s.role_key == "seer")
        witch_seat = next(s for s in g.seats if s.role_key == "witch")
        check(f"A4 {label}：预言家 1 次、女巫 1 次（有药时）",
              per.get(seer_seat.id, {}).get("seer_check") == 1
              and per.get(witch_seat.id, {}).get("witch") == 1,
              f"预言家 {per.get(seer_seat.id)}／女巫 {per.get(witch_seat.id)}")
        wolf_ids = [s.id for s in g.seats if s.camp == "wolf"]
        check(f"A5 {label}：刀口一致时每头狼夜里只 1 次（私下决策；夜聊被跳过、出刀复用决策）",
              all(per.get(i) == {"wolf_intent": 1} for i in wolf_ids),
              f"{[(i, per.get(i)) for i in wolf_ids]}")

    # --- A8/A9/A10：跳过规则本身 ---
    _s4 = dict(QUIET_SCRIPT)
    _s4["wolf_intent"] = {1: {"target": 9, "reason": "剧本", "summary": "剧本"},
                          2: {"target": 9, "reason": "剧本", "summary": "剧本"},
                          3: {"target": 9, "reason": "剧本", "summary": "剧本"},
                          4: {"target": 8, "reason": "剧本", "summary": "剧本"}}   # 4 号想刀别人
    g4, log4 = make_game("eleven", ELEVEN_GUARD, _s4, seed=3, decision_first=True)
    g4.day = 1
    g4.phase = "night"
    await g4.night()
    _c4 = night_counts(log4)
    check("A8 有一头狼想刀别人 → 照常开夜聊（4 次）",
          _c4["wolf_talk"] == 4, f"{_c4}")

    # 一致时的"跳过"记录：只给狼看（公开等于告诉好人「狼队意见一致」= 泄漏）
    g5, log5 = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True)
    g5.day = 1
    g5.phase = "night"
    await g5.night()
    _skip_notes = [e for e in g5.ledger.entries if "跳过夜聊" in (e.text or "")]
    _wolf_seat = g5.seat_by_id(1)
    _good_seat = next(x for x in g5.seats if x.camp != "wolf")
    check("A9 跳过夜聊时留了一条记录，而且**只给狼看**",
          bool(_skip_notes)
          and not any(e in g5.ledger.view(_good_seat, "good") for e in _skip_notes)
          and any(e in g5.ledger.view(_wolf_seat, "wolf") for e in _skip_notes),
          f"记录 {len(_skip_notes)} 条；好人能看到 "
          f"{[e.text[:20] for e in _skip_notes if e in g5.ledger.view(_good_seat, 'good')]}")

    # 关掉开关 → 一致也照常讨论
    g6, log6 = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True,
                         skip_when_unanimous=False)
    g6.day = 1
    g6.phase = "night"
    await g6.night()
    check("A10 关掉折中开关 → 一致也照常开夜聊（4 次）",
          night_counts(log6)["wolf_talk"] == 4, f"{night_counts(log6)}")

    # 关掉「决策在前」= 老流程：讨论 + 出刀各一次（还是每头狼 2 次，但组合不同）
    g2, log2 = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=False)
    g2.day = 1
    g2.phase = "night"
    await g2.night()
    got2 = night_counts(log2)
    print(f"    eleven（decision_first=False）：{got2}")
    check("A6 关掉决策在前：狼是「讨论 1 + 出刀 1」（没有 wolf_intent）",
          got2 == {"wolf_intent": 0, "wolf_talk": 4, "wolf_kill": 4,
                   "seer_check": 1, "guard": 1, "witch": 1}, f"{got2}")

    # 只剩一头狼：讨论跳过、出刀复用决策 → 只有 1 次
    g3, log3 = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True)
    g3.day = 2
    g3.phase = "night"
    for sid in (2, 3, 4):
        g3.kill(sid, "vote")                                 # 只留 1 号狼
    await g3.night()
    per3 = per_seat_night(log3)
    print(f"    只剩 1 头狼：{night_counts(log3)}；1号={per3.get(1)}")
    check("A7 只剩一头狼：讨论跳过、出刀复用决策 → 这头狼夜里只被调用 1 次",
          per3.get(1) == {"wolf_intent": 1}, f"{per3.get(1)}")
    check("A8 死了的狼不再被调用（出刀/讨论里都没有 2、3、4号）",
          all(per3.get(i) is None for i in (2, 3, 4)), f"{[per3.get(i) for i in (2, 3, 4)]}")


# --------------------------------------------------------------------------- #
# B. 女巫：两瓶药都用完 → 那一夜不再调用
# --------------------------------------------------------------------------- #

async def t_witch_idle() -> None:
    print("\n=== B. 女巫两瓶药都用完 → 夜里不再调用她 ===")
    # --- B1/B2：直接驱动 night()，把药的状态摆到边界上 ---
    g, log = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True)
    g.day = 4
    g.phase = "night"
    g.witch_heal = False
    g.witch_poison = False
    await g.night()
    witch = g.seat_by_id(6)
    check("B1 两瓶药都用完 → 这一夜没有任何任务发给女巫",
          seats_of(log, "witch") == [], f"{seats_of(log, 'witch')}")
    check("B2 不是整晚都不调用了：狼/预言家/守卫照旧（四狼一致 → 夜聊仍被跳过）",
          night_counts(log) == {"wolf_intent": 4, "wolf_talk": 0, "wolf_kill": 0,
                                "seer_check": 1, "guard": 1, "witch": 0},
          f"{night_counts(log)}")
    idle_line = "第 4 夜：你的两瓶药都已经用完了，今晚没有可做的行动。"
    check("B3 私有经历里留了一条中性事实（写给女巫自己看）",
          idle_line in witch.private_events, f"{witch.private_events[-2:]}")
    hits = [e for e in g.ledger.entries if "两瓶药都已经用完了" in e.text]
    check("B4 台账里那条只对女巫自己可见（别的阵营看不到）",
          len(hits) == 1 and hits[0].visible_to == ("seat:6",)
          and hits[0].visible(6, "good") and not hits[0].visible(1, "wolf")
          and not hits[0].visible(9, "good"),
          f"{[(e.seat, e.visible_to) for e in hits]}")
    check("B5 渲染出来也对：女巫看得到这条、别人看不到",
          "两瓶药都已经用完了" in g.ledger.render(6, "good", g.alive_ids(), g.dead_list(), g.day)
          and "两瓶药都已经用完了" not in g.ledger.render(
              9, "good", g.alive_ids(), g.dead_list(), g.day),
          "")
    check("B6 不写狼刀目标（她已经没有行动，别白送好人一条夜间情报）",
          not any("第 4 夜：狼人今晚击杀的是" in e for e in witch.private_events),
          f"{witch.private_events[-3:]}")

    # --- 边界：**还剩一瓶**就必须照旧问（别把「还没用」当成「用掉了」）---
    for heal, poison, label in ((True, False, "解药还在、毒药用完了"),
                                (False, True, "解药用完了、毒药还在")):
        gp, logp = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True)
        gp.day = 2
        gp.phase = "night"
        gp.witch_heal = heal
        gp.witch_poison = poison
        await gp.night()
        check(f"B7 {label} → 照旧问她（只有**两瓶都空**才不问）",
              seats_of(logp, "witch") == [6], f"{seats_of(logp, 'witch')}")
        calls = [c for c in logp if c.kind == "witch"]
        txt = str(calls[0].task.text) if calls else ""
        want_txt = ("解药：还在" if heal else "解药：已用完")
        want_txt += "；" + ("毒药：还在" if poison else "毒药：已用完")
        check(f"B8 {label} → 任务文案说的是实情（没把「还在」当成「已用完」）",
              want_txt in txt, f"文案：{txt[:70]}")

    g9, log9 = make_game("eleven", ELEVEN_GUARD, QUIET_SCRIPT, seed=3, decision_first=True)
    g9.day = 1
    g9.phase = "night"
    await g9.night()
    check("B9 开局两瓶都在 → 必须问（别一上来就跳过）",
          seats_of(log9, "witch") == [6], f"{seats_of(log9, 'witch')}")

    # --- B10 起：整局跑一遍：第 1 夜用解药、第 2 夜用毒药、第 3 夜就该静默 ---
    script = dict(QUIET_SCRIPT)
    script["wolf_kill"] = by_day({1: {"target": 7, "reason": "剧本"},
                                  2: {"target": 8, "reason": "剧本"},
                                  3: {"target": 9, "reason": "剧本"}})
    script["seer_check"] = by_day({1: {"target": 1, "reason": "剧本"},
                                   2: {"target": 2, "reason": "剧本"},
                                   3: {"target": 3, "reason": "剧本"}})
    script["witch"] = by_day({1: {"save": True, "poison": None, "reason": "剧本：用解药"},
                              2: {"save": False, "poison": 7, "reason": "剧本：用毒药"},
                              3: {"save": False, "poison": None, "reason": "第 3 夜不该被问"}})
    g2, log2 = make_game(NINE_BOARD, NINE, script, seed=11,
                         decision_first=False, max_days=3)
    await g2.run()
    print(f"    整局（9 人板、3 夜）：女巫被调用的天={seats_of(log2, 'witch')}；"
          f"winner={g2.winner} day={g2.day}")
    check("B10 整局跑到第 3 夜（剧本确定：屠民边完成 → 狼胜）",
          g2.winner == "wolf" and g2.day == 3, f"winner={g2.winner} day={g2.day}")
    check("B11 第 1 夜用解药、第 2 夜用毒药 → 这两夜都问了她；第 3 夜不再问",
          seats_of(log2, "witch", day=1) == [5] and seats_of(log2, "witch", day=2) == [5]
          and seats_of(log2, "witch", day=3) == [],
          f"第1夜={seats_of(log2, 'witch', day=1)} 第2夜={seats_of(log2, 'witch', day=2)} "
          f"第3夜={seats_of(log2, 'witch', day=3)}")
    check("B12 两瓶药的状态真的被扣掉了（沿用现有字段，没有第二套账）",
          g2.witch_heal is False and g2.witch_poison is False,
          f"witch_heal={g2.witch_heal} witch_poison={g2.witch_poison}")
    check("B13 第 3 夜只少了女巫这一条：狼照旧讨论+出刀、预言家照旧验人",
          night_counts(log2, day=3) == {"wolf_intent": 0, "wolf_talk": 3, "wolf_kill": 3,
                                        "seer_check": 1, "guard": 0, "witch": 0},
          f"{night_counts(log2, day=3)}")
    w2 = g2.seat_by_id(5)
    check("B14 前面两夜照旧告诉她谁被刀了（正常路径没被改坏）",
          any("第 1 夜：狼人今晚击杀的是 7号" in e for e in w2.private_events)
          and any("第 2 夜：狼人今晚击杀的是 8号" in e for e in w2.private_events),
          f"{[e for e in w2.private_events if '击杀' in e]}")
    check("B15 第 3 夜只说药没了，不说谁被刀",
          "第 3 夜：你的两瓶药都已经用完了，今晚没有可做的行动。" in w2.private_events
          and not any("第 3 夜：狼人今晚击杀的是" in e for e in w2.private_events),
          f"{[e for e in w2.private_events if e.startswith('第 3 夜')]}")
    check("B16 整局里没有出现过「双药都用完还被问女巫」的调用",
          leaks(g2) == [], f"{leaks(g2)[:3]}")


# --------------------------------------------------------------------------- #
# C. 死了的座位，后续所有阶段都不再被调用
# --------------------------------------------------------------------------- #

async def t_dead_seats() -> None:
    print("\n=== C. 死者不再被调用（含警长竞选 / 发言 / 投票）===")
    script = dict(QUIET_SCRIPT)
    script["wolf_kill"] = by_day({1: {"target": 9, "reason": "剧本"},
                                  2: {"target": 10, "reason": "剧本"},
                                  3: {"target": 11, "reason": "剧本"}})
    script["seer_check"] = by_day({1: {"target": 1, "reason": "剧本"},
                                   2: {"target": 2, "reason": "剧本"},
                                   3: {"target": 3, "reason": "剧本"}})
    # 守卫守的人必须**避开**狼刀目标，否则剧本的死人不确定（守住了就不死）
    script["guard"] = by_day({1: {"target": 8, "reason": "剧本"},
                              2: {"target": 11, "reason": "剧本"},
                              3: {"target": 1, "reason": "剧本"}})
    # 竞选：1、2 号上警，而且**剧本故意让已经出局的 9号 也上警** ——
    # 引擎必须压根不问死人（9号 连竞选者名单都不该进）
    script["sheriff_run"] = {1: {"run": True, "reason": "剧本"}, 2: {"run": True, "reason": "剧本"},
                             9: {"run": True, "reason": "剧本：他已经出局了"},
                             "*": {"run": False, "reason": "剧本"}}
    script["sheriff_vote"] = {"*": {"vote": 1, "reason": "剧本"}}
    # 11 人守卫板（没有白痴）：3 个村民死光 → 屠民边真的完成 → 第 3 天判狼胜
    # ⚠️ 不能用 eleven_standard：那里的**白痴也算民**，刀光 9/10/11 号屠民边还没完成，
    #    局会继续打下去，剧本的死人就对不上了。
    # 11 人板 → 第 1 天有警长竞选（全员不上警 → 警徽流失），死者必须既不参选也不投票
    g, log = make_game("eleven", ELEVEN_GUARD, script, seed=7,
                       decision_first=False, max_days=3)
    await g.run()
    dead = {s.id: s.death_day for s in g.seats if not s.alive}

    def after_death(c: Call) -> list[int]:
        """这次调用发生时，哪些座位**已经**出局了（死亡是在当天白天阶段一开场结算的）。"""
        return [sid for sid, d in dead.items()
                if c.day > d or (c.day == d and c.phase == "day")]

    print(f"    winner={g.winner} day={g.day}；出局：{dead}")
    check("C1 剧本局跑完（3 夜依次刀 9/10/11 号 → 屠民边完成，狼胜）",
          g.winner == "wolf" and sorted(dead) == [9, 10, 11], f"winner={g.winner} dead={dead}")
    check("C2 死者只在**第一夜**拿到遗言：last_words 只问过 9号，且在第 1 天",
          seats_of(log, "last_words") == [9]
          and all(c.day == 1 for c in log if c.kind == "last_words"),
          f"last_words={[(c.seat, c.day) for c in log if c.kind == 'last_words']}")
    after = [(c.seat, c.kind, c.day) for c in log
             if c.seat in after_death(c) and c.kind not in DEAD_OK]
    check("C3 死者出局之后，没有任何一条调用发给他们（遗言/警徽/猎人那一枪除外）",
          after == [], f"{after[:6]}")
    check("C4 死者不参选、也不投警长的票",
          9 not in seats_of(log, "sheriff_run") and 9 not in seats_of(log, "sheriff_vote"),
          f"sheriff_run={sorted(set(seats_of(log, 'sheriff_run')))}／"
          f"sheriff_vote={sorted(set(seats_of(log, 'sheriff_vote')))}")
    check("C5 全局：没有任何「死者还被调用」的留痕（遗言/警徽/猎人那一枪除外）",
          leaks(g) == [], f"{leaks(g)[:3]}")

    # 死者不能出现在任何「他死后才发生」的候选名单里（被投 / 被刀 / 被验 / 被毒 / 被守）
    bad = []
    for c in log:
        for sid in after_death(c):
            if sid in (getattr(c.task, "legal", None) or []):
                bad.append((sid, c.kind, c.day, c.phase))
    check("C6 死者不在任何后续任务的合法目标里（票/刀/验/毒/守都不该点到他）",
          bad == [], f"{bad[:6]}")

    # 死者也不该出现在别人（和自己）上下文包的【存活玩家】里
    shown = [(c.seat, c.kind, c.day) for c in log
             if c.phase == "day" and any(sid in _alive_in_packet(c.packet)
                                         for sid in after_death(c))]
    check("C7 死者在后来那些上下文包的【存活玩家】名单里不再出现",
          shown == [], f"{shown[:4]}")


# --------------------------------------------------------------------------- #
# D. 翻牌白痴：不投票，但发言照常
# --------------------------------------------------------------------------- #

async def t_idiot_no_vote() -> None:
    print("\n=== D. 白痴翻牌之后：不再投票、仍能发言 ===")
    script = dict(QUIET_SCRIPT)
    script["wolf_intent"] = by_day({1: {"target": 9, "reason": "剧本", "summary": "剧本"},
                                    2: {"target": 10, "reason": "剧本", "summary": "剧本"}})

    def analyze(seat_id, task, day=1):
        """第 1 天全员投 8号（白痴）→ 他翻牌；第 2 天全员投 11号。"""
        if day == 1:
            who = 9 if seat_id == 8 else 8
        else:
            who = 1 if seat_id == 11 else 11
        return {"reads": [{"seat": 1, "camp": "wolf", "role": "未知", "why": "剧本"}],
                "summary": "剧本判断", "tiers": [{"seat": 1, "tier": "可疑",
                                                  "good_reason": "", "wolf_reason": "剧本"}],
                "vote": who, "reason": "剧本"}

    script["analyze"] = analyze
    g, log = make_game("eleven_standard", ELEVEN_STD, script, seed=5,
                       decision_first=True, max_days=2)
    await g.run()
    idiot = g.seat_by_id(8)
    print(f"    winner={g.winner} day={g.day}；8号 alive={idiot.alive} "
          f"revealed_idiot={idiot.revealed_idiot}")
    check("D1 第 1 天被全额票出去的 8号（白痴）翻牌活了下来",
          idiot.alive and idiot.revealed_idiot
          and any("亮出【愚者】身份" in e.text for e in g.ledger.entries),
          f"alive={idiot.alive} revealed={idiot.revealed_idiot}")
    check("D2 翻牌前他是照常被问决策的（第 1 天有 analyze）",
          8 in seats_of(log, "analyze", day=1), f"{seats_of(log, 'analyze', day=1)}")
    check("D3 翻牌之后不再被叫去投票（vote）",
          8 not in seats_of(log, "vote", day=2), f"第2天 voters={seats_of(log, 'vote', day=2)}")
    check("D4 翻牌之后也不再被叫去做投票前决策（analyze）",
          8 not in seats_of(log, "analyze", day=2), f"{seats_of(log, 'analyze', day=2)}")
    check("D5 翻牌之后发言照常（第 2 天照样请他发言 / 做发言前决策）",
          8 in seats_of(log, "speech_plan", day=2), f"{seats_of(log, 'speech_plan', day=2)}")
    bad = [(c.kind, c.day) for c in log
           if c.day >= 2 and c.kind in POOL_KINDS and 8 in (getattr(c.task, "legal", None) or [])]
    check("D6 翻牌白痴不再进投票候选名单（第 2 天起谁都不能投他）", bad == [], f"{bad[:6]}")
    check("D7 白痴没有被放逐（翻牌不是出局）：他没有遗言",
          8 not in seats_of(log, "last_words"), f"{seats_of(log, 'last_words')}")
    check("D8 全局：没有「翻牌白痴被叫去投票」的留痕", leaks(g) == [], f"{leaks(g)[:3]}")


# --------------------------------------------------------------------------- #
# E. 真人座位：不被叫去做模型内部决策
# --------------------------------------------------------------------------- #

async def t_human_seat() -> None:
    print("\n=== E. 真人座位只被问自己的行动，不做模型内部决策 ===")
    g, log = make_game("eleven_standard", ELEVEN_STD, QUIET_SCRIPT, seed=3, human_seat=1,
                       decision_first=True, max_days=2)
    await g.run()
    human_kinds = only_kinds(log, 1)
    print(f"    1号（真人，狼）被问过的任务：{sorted(human_kinds)}")
    check("E1 真人座位没有被叫去做内部决策（analyze / speech_plan / wolf_intent）",
          not (human_kinds & set(INTERNAL_KINDS)), f"{sorted(human_kinds & set(INTERNAL_KINDS))}")
    check("E2 真人座位照常被问自己的行动：讨论 / 出刀 / 发言 / 投票",
          {"wolf_talk", "wolf_kill", "speech", "vote"} <= human_kinds,
          f"{sorted(human_kinds)}")
    check("E3 全局：所有内部决策调用都只发给模型座位",
          all(not c.human for c in log if c.kind in INTERNAL_KINDS),
          f"{[(c.seat, c.kind) for c in log if c.kind in INTERNAL_KINDS and c.human][:4]}")
    check("E4 真人座位死了之后也不会被叫（除了遗言/警徽）", leaks(g) == [], f"{leaks(g)[:3]}")


# --------------------------------------------------------------------------- #
# F. 真 MockAgent 整局：全局不变量 + 实测调用次数
# --------------------------------------------------------------------------- #

async def t_real_mock() -> None:
    print("\n=== F. 真 MockAgent 整局（不写剧本）：全局不变量 + 实测次数 ===")
    for label, board, seed, human, role1 in (
            ("eleven_standard", "eleven_standard", 11, 0, None),
            ("eleven（有守卫）", "eleven", 5, 0, None),
            ("eleven_standard（真人坐 1 号狼）", "eleven_standard", 3, 1, "wolf"),
            # seed=0：这一局**白痴真的会翻牌**（实测 12 局里 0/7/9/10/11 会翻），
            # 用来让「翻牌白痴不投票」这条在真 mock 局里也有覆盖（不只是 D 那个剧本局）
            ("eleven_standard（seed=0，白痴会翻牌）", "eleven_standard", 0, 0, None)):
        log: list[Call] = []
        rng = random.Random(seed)
        g = Game(board=make_board(board), human_seat=human, seed=seed, max_days=6,
                 decision_first=True, on_event=lambda *a, **k: None)
        if role1:
            g.seat_by_id(1).role_key = role1
        holders: list[CountingMock] = []

        def factory(seat, _log=log, _rng=rng, _g=g, _h=holders):
            agent = CountingMock(seat, _rng, {}, _log, _g)
            _h.append(agent)
            return agent

        g.agent_factory = factory
        g.holders = holders
        await g.run()
        night = counts(log, kinds=NIGHT_KINDS)
        roles = {s.id: s.role_key for s in g.seats}
        print(f"    {label}（seed={seed}）：winner={g.winner} day={g.day}；"
              f"总调用 {len(log)} 次")
        print(f"      白天：{dict(sorted((k, v) for k, v in counts(log).items() if k not in NIGHT_KINDS))}")
        print(f"      夜里：{night}")
        print(f"      夜里各座位：" + "；".join(f"{sid}号({roles[sid]})={v}"
                                                for sid, v in per_seat_night(log).items()))
        check(f"F1 {label}：全局没有「死人被调用」（遗言/警徽/猎人那一枪除外）",
              leaks(g) == [], f"{leaks(g)[:3]}")
        revealed = [s.id for s in g.seats if s.revealed_idiot]
        idiot_votes = sum(len(a.idiot_vote_leak) for a in holders)
        if revealed:
            print(f"      白痴翻牌：{revealed} → 翻牌后投票调用 {idiot_votes} 次")
        check(f"F1b {label}：翻牌白痴一次都没被叫去投票"
              f"（本局翻牌座位：{revealed or '无'}）",
              idiot_votes == 0, f"{[(a.idiot_vote_leak) for a in holders if a.idiot_vote_leak]}")
        bad_night = [(c.seat, roles[c.seat], c.kind, c.day) for c in log
                     if c.kind in NIGHT_KINDS
                     and c.kind not in NIGHT_ROLE_KINDS.get(roles[c.seat], ())]
        check(f"F2 {label}：夜里的调用只落在有夜间技能的角色身上"
              f"（村民/白痴/猎人一次都没有）",
              bad_night == [], f"{bad_night[:4]}")
        # 每头狼每夜最多 2 次（私下决策 + 讨论；出刀复用决策）
        wolf_calls: Counter = Counter()
        for c in log:
            if c.kind in ("wolf_intent", "wolf_talk") or (c.kind == "wolf_kill"
                                                          and roles[c.seat] == "wolf"):
                wolf_calls[(c.day, c.seat)] += 1
        top = wolf_calls.most_common(1)
        check(f"F3 {label}：每头狼每夜 ≤2 次（实测最多 "
              f"{top[0][1] if top else 0} 次）",
              all(v <= 2 for v in wolf_calls.values()),
              f"{[k for k, v in wolf_calls.items() if v > 2]}")
        # 每晚每种夜间技能至多一次（狼那三类除外：那是按人头算的）
        per_night = Counter((c.day, c.kind) for c in log
                            if c.kind in ("seer_check", "guard", "witch"))
        check(f"F4 {label}：预言家/守卫/女巫每晚最多被调用一次",
              all(v <= 1 for v in per_night.values()), f"{[k for k, v in per_night.items() if v > 1]}")
        check(f"F5 {label}：女巫调用次数 ≤ 她活着的夜数（有药才问）",
              night.get("witch", 0) <= g.day, f"witch={night.get('witch', 0)} day={g.day}")
        guard_seat = next((s for s in g.seats if s.role_key == "guard"), None)
        if guard_seat is not None:
            check(f"F6 {label}：带守卫的板子上，守卫活着就每晚都被调用（不是只调一次）",
                  night.get("guard", 0) >= 1, f"guard={night.get('guard', 0)}")

        # 逐夜覆盖：活着的夜间角色，那一夜**必须**恰好被调用一次
        # （活着与否从那一夜任意一份上下文包的【存活玩家】读出来 —— 夜里这份名单是固定的）
        alive_by_night: dict[int, set[int]] = {}
        calls_by_night: Counter = Counter()
        for c in log:
            if c.kind in NIGHT_KINDS:
                alive_by_night.setdefault(c.day, set(_alive_in_packet(c.packet)))
                calls_by_night[(c.day, c.kind)] += 1
        detail = {}
        for sid, role in sorted(roles.items()):
            kind = {"guard": "guard", "seer": "seer_check", "witch": "witch"}.get(role)
            if not kind:
                continue
            nights = sorted(d for d, alive in alive_by_night.items() if sid in alive)
            got = [calls_by_night[(d, kind)] for d in nights]
            detail[role] = (sid, nights, got)
            check(f"F7 {label}：{sid}号（{role}）活着的每一夜都**恰好**被调用一次",
                  bool(nights) and all(n == 1 for n in got),
                  f"夜={nights} 次数={got}")
        print(f"      逐夜覆盖：{detail}")
        # --- F8：台账里不许有"谁都看不到"的条目 -------------------------------
        # ⚠️ 2026-10-10 实测踩到：`visible_to=(seat.id,)` 写的是**整数**，而台账的
        #    `visible()` 只认 "public" / "wolf" / "seat:N"（字符串）—— 写整数不报错，
        #    但那条记录**对所有人（包括他自己）都不可见**，等于白写。
        #    这条闸门按"可见性形态"查，比逐处 review 可靠。
        _known = ("public", "wolf")
        _bad_entries = []
        for _e in g.ledger.entries:
            for _who in (_e.visible_to or ()):
                if isinstance(_who, str) and (_who in _known or _who.startswith("seat:")):
                    break
            else:
                _bad_entries.append((_e.kind, _e.seat, repr(_e.visible_to), (_e.text or "")[:28]))
        check(f"F8 {label}：台账里没有「谁都看不到」的条目"
              f"（visible_to 只认 public / wolf / seat:N）",
              _bad_entries == [], f"{_bad_entries[:3]}")

        if night.get("wolf_intent", 0) or night.get("wolf_talk", 0):
            print("      夜里的 (天, 任务) 明细：" + str(dict(sorted(
                (k, v) for k, v in calls_by_night.items() if k[1].startswith("wolf")))))


# --------------------------------------------------------------------------- #
# G. 公开台账里不许出现决策层的私有理由（P0 泄漏回归）
# --------------------------------------------------------------------------- #

#: 三条**故意写得像真泄漏**的私有理由：它们在 prompt 里都标着「只有你自己看得到」，
#: 一旦出现在 `"public" in visible_to` 的条目里，就是泄漏（服务端会推给真人前端、
#: 也会进其他座位的包 —— 实测泄漏过狼的内部计划原话）。
SECRET_ANALYZE = "私有决策理由：我按倒钩计划打，今晚女巫的解药已经用掉，明天再刀真预言家"
SECRET_SPEECH = "私有发言前判断：先把 3号 做出去，等明天收网的时候再跳身份"
SECRET_WOLF_TALK = "队友听好：今晚刀 9号，明早我跳预言家给 2号 发金水"


def lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（判「有没有整段抄过去」，不写死句子）。"""
    if not a or not b:
        return 0
    return difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b)).size


async def t_private_reason_not_public() -> None:
    print("\n=== G. 决策层的私有理由不许进公开台账（P0 泄漏回归）===")

    def analyze(seat_id, task, day=1):
        return {"reads": [{"seat": 1, "camp": "wolf", "role": "未知", "why": "剧本"}],
                "summary": "剧本判断",
                "tiers": [{"seat": 1, "tier": "可疑", "good_reason": "", "wolf_reason": "剧本"}],
                "vote": 8 if seat_id == 9 else 9, "reason": SECRET_ANALYZE}

    def speech_plan(seat_id, task, day=1):
        return {"reads": [{"seat": 1, "camp": "wolf", "role": "未知", "why": "剧本"}],
                "summary": "剧本判断", "protect": [], "attack": [9], "claim": "无",
                "stance": "剧本立场", "role_plan": "剧本打算",
                "vote": 8 if seat_id == 9 else 9, "reason": SECRET_SPEECH,
                "speech": "（剧本发言）我先说这么多。"}

    script = dict(QUIET_SCRIPT)
    script["analyze"] = analyze
    script["speech_plan"] = speech_plan
    # ⚠️ 2026-10-09：四狼一致会**跳过夜聊** → 这段泄漏测试就变成"测空气"。
    #    所以让 3 号狼的刀口与别人不同，夜聊才会真的发生。
    script["wolf_intent"] = {1: {"target": 9, "reason": SECRET_ANALYZE, "summary": "剧本"},
                             2: {"target": 9, "reason": SECRET_ANALYZE, "summary": "剧本"},
                             3: {"target": 8, "reason": SECRET_ANALYZE, "summary": "剧本"}}
    script["wolf_talk"] = {"*": {"speech": SECRET_WOLF_TALK, "target": 9}}
    script["last_words"] = {"*": {"speech": "（剧本遗言）", "claim": "无"}}
    script["seer_check"] = {"*": {"target": 1, "reason": "剧本"}}
    g, log = make_game(NINE_BOARD, NINE, script, seed=13,
                       decision_first=True, max_days=1)
    await g.run()

    public = [e for e in g.ledger.entries if "public" in e.visible_to]
    votes = [e for e in public if e.kind == "vote"]
    print(f"    公开条目 {len(public)} 条（其中投票 {len(votes)} 条）；"
          f"私有决策 {len(g.day_decisions)} 条")
    check("G1 这一局真的走到了「复用投票前决策」那条路（不然这条回归是空过）",
          bool(votes) and all((e.data or {}).get("prefetched") for e in votes),
          f"投票 {len(votes)} 条，prefetched={[bool((e.data or {}).get('prefetched')) for e in votes][:4]}")

    # ① 逐条投票：正文里不许出现**这个座位自己**的私有决策理由
    bad = []
    for e in votes:
        why = str((g.day_decisions.get(e.seat) or {}).get("reason") or "")
        if why and lcs_len(e.text, why) >= 20:
            bad.append((e.seat, lcs_len(e.text, why), e.text[:40]))
    check("G2 公开投票条目不含该座位决策层的私有理由（判据：最长公共子串 < 20 字）",
          bad == [], f"{bad[:3]}")
    reasons = lambda d: [str((v or {}).get("reason") or "") for v in (d or {}).values()]
    check("G3 反过来确认：私有理由**还在**存档的 decisions 里（不是被整段删了）",
          bool(g.day_decisions) and all(r == SECRET_ANALYZE for r in reasons(g.day_decisions))
          and bool(g.speech_decisions)
          and all(r == SECRET_SPEECH for r in reasons(g.speech_decisions))
          and bool(g.wolf_decisions)
          and all(r == SECRET_ANALYZE for r in reasons(g.wolf_decisions)),
          f"day_decisions={len(g.day_decisions)} 条／"
          f"speech_decisions={len(g.speech_decisions)} 条／wolf_decisions={len(g.wolf_decisions)} 条")
    rows = [r for r in g.transcript if r.get("task") == "analyze"]
    check("G3b 调用留痕（transcript）里照样留着那句私有理由（复盘用）",
          bool(rows) and all(str((r.get("reply") or {}).get("reason") or "") == SECRET_ANALYZE
                             for r in rows),
          f"{len(rows)} 条 analyze 留痕")

    # ② 更宽一圈：三条私有理由的原文，一个字都不许出现在任何公开条目里
    leaks = [(e.kind, e.seat, s[:2]) for e in public
             for s in (SECRET_ANALYZE, SECRET_SPEECH, SECRET_WOLF_TALK)
             if lcs_len(e.text, s) >= 20]
    check("G4 所有公开条目（公告/发言/投票/遗言）都不含那三条私有理由",
          leaks == [], f"{leaks[:3]}")

    # ③ 狼队内部的条目仍然是**狼队可见**（别被误伤成公开）
    wolf_seat = next(s.id for s in g.seats if s.camp == "wolf")
    good_seat = next(s.id for s in g.seats if s.camp != "wolf")
    wolf_entries = [e for e in g.ledger.entries if SECRET_WOLF_TALK in e.text]
    check("G5 狼队讨论原文进了台账，但 visible_to 只有狼队（不是 public）",
          bool(wolf_entries) and all(e.visible_to == ("wolf",) for e in wolf_entries)
          and all(e.visible(wolf_seat, "wolf") for e in wolf_entries),
          f"{[(e.seat, e.visible_to) for e in wolf_entries]}")
    check("G6 狼队内部信息：狼看得到、好人看不到（隔离没被这次修复碰坏）",
          any(SECRET_WOLF_TALK in e.text for e in g.ledger.view(wolf_seat, "wolf"))
          and not any(SECRET_WOLF_TALK in e.text for e in g.ledger.view(good_seat, "good")),
          f"狼视角 {len(g.ledger.view(wolf_seat, 'wolf'))} 条／"
          f"好人视角 {len(g.ledger.view(good_seat, 'good'))} 条")
    night_entries = [e for e in g.ledger.entries if e.kind == "night"]
    check("G7 夜间条目（夜间信息 / 你的私有信息）里没有一条对好人是可见的",
          bool(night_entries) and all(not e.visible(good_seat, "good")
                                      for e in night_entries),
          f"{[(e.seat, e.visible_to) for e in night_entries][:3]}")
    check("G8 内部决策本身（analyze / speech_plan）不进公开台账",
          not any(e.kind in ("analyze", "speech_plan") for e in g.ledger.entries),
          f"{sorted({e.kind for e in g.ledger.entries})}")

    # ④ P3-2（2026-10-08）：复用**狼刀**决策时，同样只许写来路。
    #    `decide_wolf_round` 的提示词明写「只有你自己看得到」，而那条出刀提议
    #    `visible_to=(WOLF,)` —— 虽不是 public，也是**台账原文**：队友的包、存档、
    #    前端复盘全都读得到，等于把私密那句话原文转发给全队友并永久留在台账里。
    props = [e for e in g.ledger.entries if "提议击杀" in e.text]
    print(f"    出刀提议 {len(props)} 条，prefetched="
          f"{[bool((e.data or {}).get('prefetched')) for e in props]}")
    check("G9 这一局真的走到了「复用狼刀决策」那条路（不然这条回归是空过）",
          bool(props) and any((e.data or {}).get("prefetched") for e in props),
          f"{[(e.seat, e.text[:22]) for e in props[:3]]}")
    leak = [(e.seat, lcs_len(e.text, SECRET_ANALYZE), e.text[:40])
            for e in props if lcs_len(e.text, SECRET_ANALYZE) >= 10]
    check("G10 出刀提议的正文不含 wolf_intent.reason 原文（判据：最长公共子串 < 10 字）",
          leak == [], f"{leak[:3]}")
    pre_props = [e for e in props if (e.data or {}).get("prefetched")]
    check("G11 复用的那条只写来路（「沿用今晚早先的决定」），不带理由",
          bool(pre_props) and all("沿用今晚早先的决定" in e.text for e in pre_props)
          and all("：" not in e.text.split("沿用今晚早先的决定")[1][:6] for e in pre_props),
          f"{[e.text[:46] for e in pre_props[:3]]}")
    # 私有理由照旧留在存档字段里（不是被整段删掉）
    check("G12 反过来确认：wolf_decisions 里的私有理由还在（复盘看得到）",
          bool(g.wolf_decisions)
          and all(str((v or {}).get("reason") or "") == SECRET_ANALYZE
                  for v in g.wolf_decisions.values()),
          f"{len(g.wolf_decisions)} 条")

    # ⑤ P3-4（2026-10-08）：公开投票条目里那句「见自己的私有记录」两头不讨好 ——
    #    对**别人**说是空话（他们手里没有你的记录），投票者**本人**读台账也找不到
    #    （私有理由只进 mind 的 summary/reads 与存档）。改成中性说法 + 把原话记进
    #    它自己的推理笔记（下一轮被追问「你为什么投他」时自己看得到）。
    check("G13 公开投票条目不再出现「私有记录」这种指向不明的说法",
          all("私有记录" not in e.text for e in votes),
          f"{[e.text[:40] for e in votes[:3]]}")
    check("G14 中性说法落地（复用决策那条写成「本轮已定票」）",
          bool(votes) and all("本轮已定票" in e.text for e in votes),
          f"{[e.text[:40] for e in votes[:3]]}")
    mind_hit = [s.id for s in g.seats if "我投" in s.mind and "理由" in s.mind]
    check("G15 该理由被记进投票者**自己的**推理笔记（mind），它自己看得到原话",
          bool(mind_hit)
          and all(SECRET_ANALYZE[:24] in g.seat_by_id(sid).mind for sid in mind_hit),
          f"座位 {mind_hit}；例：{g.seat_by_id(mind_hit[0]).mind[-70:] if mind_hit else ''}")
    check("G16 记进 mind 不等于公开：公开条目里仍然一个字都没有",
          all(lcs_len(e.text, SECRET_ANALYZE) < 10 for e in public))


# --------------------------------------------------------------------------- #

async def main() -> int:
    await t_night_manifest()
    await t_witch_idle()
    await t_dead_seats()
    await t_idiot_no_vote()
    await t_human_seat()
    await t_real_mock()
    await t_private_reason_not_public()
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 76)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 76)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
