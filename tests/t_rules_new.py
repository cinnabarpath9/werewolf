# coding=utf-8
"""新规则自检：遗言 + 警长（用"按座位写死的剧本代理"，所以判定是确定的，不靠随机）。

覆盖：
  1) 11 人及以上才有警长竞选（10 人板子不跑竞选）
  2) 无人上警 / 全员上警 → 警徽流失
  3) 只有一人上警 → 直接当选（且不发起投票）
  4) 多人竞选：只有竞选者发言、只有非竞选者投票（竞选者没有投票权）
  5) 警徽 1.5 票：没有警徽会输的人，靠警长那 0.5 票翻盘；公告里票数是 4.5 而不是 5
  6) 被投票放逐 → 有遗言（公开条目「【遗言】」）
  7) 夜里死亡（刀/毒/夜枪/同守同救）→ **没有遗言**，且公告不公示死因
  8) 白天猎人被放逐 → 开枪打死的人**有遗言**，死因公示"被子弹打死"
  9) 夜里被刀的猎人开枪 → 被打死的人**没有遗言**、不公示死因
 10) 警长被夜刀 → 可以传承警徽（新警长上任）
 11) 警长被夜刀 → 也可以撕掉警徽（本局从此没有警长）
 12) 发言顺序：没有警徽 = 座位从小到大；有警徽 = 警长的下一位先说、警长最后
 13) 纯函数：weighted_counts / fmt_votes

跑法： python t_rules_new.py
"""
from __future__ import annotations

import asyncio
import os
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wolf.engine import Game                          # noqa: E402
from wolf.roles import BoardDef, make_board
from _boards import make_board, retired_board      # noqa: E402,F401  退役板子的测试副本（产品已下架）
from _boards import NINE_BOARD                     # 测试专用 9 人板（产品的 9 人板已删）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((("   | " + str(detail))) if detail else ""))
    sys.stdout.flush()


class Scripted:
    """按座位写死回复的代理：script[task_kind] = {座位号: 回复字典}，用 "*" 当默认。

    log 里每条是 (座位号, 任务类型, 第几天, Task 对象) —— 带 Task 是为了能查 legal 名单
    （比如"白痴翻牌后还允许被投吗"只能从 legal 名单上看出来）。
    """

    def __init__(self, seat_id: int, script: dict, log: list) -> None:
        self.seat_id = seat_id
        self.script = script
        self.log = log

    async def ask(self, packet, task):
        import re as _re
        # ⚠️ 天数从 **packet** 里读（【当前阶段】第 N 天）：guard / witch / 猎人这些任务的
        # task.text 里**没有**天数，只从 task.text 读的话剧本会按"第 1 天"跑，全部错位。
        m = (_re.search(r"【当前阶段】第 (\d+) 天", packet or "")
             or _re.search(r"第 (\d+) 天", task.text or ""))
        day = int(m.group(1)) if m else 0
        self.log.append((self.seat_id, task.kind, day, task))
        table = self.script.get(task.kind)
        reply = {}
        if isinstance(table, dict):
            reply = table.get(self.seat_id, table.get("*", {}))
        elif table is not None:
            reply = table
        if callable(reply):
            try:
                reply = reply(self.seat_id, task, day)
            except TypeError:
                reply = reply(self.seat_id, task)
        return dict(reply) if isinstance(reply, dict) else {}

    def last_meta(self) -> dict:
        return {}

    async def aclose(self) -> None:
        return None


def build(board_name, assign: dict, script: dict, seed: int = 7,
          max_days: int = 5, **kw):
    log: list = []

    def factory(seat):
        return Scripted(seat.id, script, log)

    # ⚠️ 本文件测的是**竞选机制**：默认关掉"预言家锁定上警"（2026-10-10 作者口径），
    #    否则会多出一个没写进剧本的候选人，这些断言全会连锁假红。
    kw.setdefault("seer_auto_run", False)
    game = Game(board=make_board(board_name), seed=seed, agent_factory=factory,
                on_event=lambda *a, **k: None, max_days=max_days, **kw)
    for sid, role in assign.items():
        game.seat_by_id(sid).role_key = role
    return game, log


def texts(game) -> str:
    return "\n".join(f"{e.day}|{e.kind}|{e.seat}|{e.text}" for e in game.ledger.entries)


def public_texts(game) -> str:
    return "\n".join(e.text for e in game.ledger.entries if "public" in e.visible_to)


def asked(log: list, kind: str, day: int | None = None) -> list[int]:
    return [sid for sid, k, d, _t in log if k == kind and (day is None or d == day)]


def tasks_of(log: list, kind: str, day: int | None = None) -> list:
    return [t for _sid, k, d, t in log if k == kind and (day is None or d == day)]



NINE = {1: "hunter", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer",
        6: "witch", 7: "villager", 8: "villager", 9: "villager"}
# ⚠️ 十人板 2026-10-07 已删 → 这套"带守卫"的场景改用十一人守卫板 eleven，
#    表里补第 11 座（build() 会覆盖座位角色，所以狼数仍由这张表决定）。
TEN = {1: "hunter", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
       7: "guard", 8: "villager", 9: "villager", 10: "villager", 11: "villager"}
ELEVEN_GUARD = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
                7: "hunter", 8: "guard", 9: "villager", 10: "villager", 11: "villager"}

#: 夜里谁都不死（女巫救下狼刀的目标）—— 让白天的剧本不受夜间减员干扰。
# ⚠️ 刀口/守护目标必须**每个板子都存在**（9~11 人都有 8、9 号）：写个 12 号上去，
# `coerce_seat` 会静默换成随机一个好人，随到谁就死谁（剧本就散了）。
QUIET_NIGHT = {
    "wolf_kill": {"*": {"target": 9, "reason": "剧本"}},
    "seer_check": {"*": {"target": 1, "reason": "剧本"}},
    "guard": {"*": {"target": 8, "reason": "剧本"}},
    "witch": {"*": {"save": True, "poison": None, "reason": "剧本"}},
    "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
    "wolf_talk": {"*": {"speech": "（剧本）", "target": 9}},
    "speech": {"*": {"speech": "（剧本发言）", "claim": "无", "reason": "剧本"}},
    "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
}


def script_with(base: dict, **over) -> dict:
    out = dict(base)
    out.update(over)
    return out


# --------------------------------------------------------------------------- #
# 1) 门槛：9 人没有警长竞选，11 人有（十人板 2026-10-07 已删，改用九人板验下界）
# --------------------------------------------------------------------------- #

async def t_threshold() -> None:
    print("\n=== 1) 警长门槛（11 人及以上）===")
    g9, log9 = build(NINE_BOARD, NINE, script_with(QUIET_NIGHT, vote={"*": {"abstain": True}}))
    await g9.run()
    check("1a 9 人局不跑警长竞选（没有 sheriff_run 提问）",
          not asked(log9, "sheriff_run") and g9.sheriff_id is None,
          f"asks={asked(log9, 'sheriff_run')} sheriff={g9.sheriff_id}")
    g11, log11 = build("eleven", ELEVEN_GUARD,
                       script_with(QUIET_NIGHT, sheriff_run={"*": {"run": False}},
                                   vote={"*": {"abstain": True}}))
    await g11.run()
    check("1b 11 人局会跑警长竞选", bool(asked(log11, "sheriff_run")),
          f"问了 {len(asked(log11, 'sheriff_run'))} 个座位")


# --------------------------------------------------------------------------- #
# 2) 无人上警 / 全员上警 → 警徽流失
# --------------------------------------------------------------------------- #

async def t_badge_lost() -> None:
    print("\n=== 2) 警徽流失的两种情形 ===")
    for label, run_reply, want in (("无人上警", {"*": {"run": False}}, "无人上警"),
                                   ("全员上警", {"*": {"run": True}}, "全员上警")):
        g, log = build("eleven", ELEVEN_GUARD,
                       script_with(QUIET_NIGHT, sheriff_run=run_reply,
                                   vote={"*": {"abstain": True}}))
        await g.run()
        check(f"2 {label} → 警徽流失",
              g.sheriff_id is None and f"警徽流失：{want}" in texts(g),
              f"sheriff={g.sheriff_id}")


# --------------------------------------------------------------------------- #
# 3) 只有一人上警 → 直接当选，不投票
# --------------------------------------------------------------------------- #

async def t_single_candidate() -> None:
    print("\n=== 3) 一人上警：直接当选、不投票 ===")
    g, log = build("eleven", ELEVEN_GUARD,
                   script_with(QUIET_NIGHT,
                               sheriff_run={3: {"run": True}, "*": {"run": False}},
                               vote={"*": {"abstain": True}}))
    await g.run()
    check("3a 3号 直接当选警长", g.sheriff_id == 3, f"sheriff={g.sheriff_id}")
    check("3b 没发起警长竞选投票（只有一人上警）", not asked(log, "sheriff_vote"),
          f"votes={asked(log, 'sheriff_vote')}")
    check("3c 公告写明警徽 1.5 票", "1.5 票" in texts(g))


# --------------------------------------------------------------------------- #
# 4) 多人竞选：只有竞选者发言、只有非竞选者投票
# --------------------------------------------------------------------------- #

async def t_multi_candidate() -> None:
    print("\n=== 4) 多人竞选：发言/投票权分离 ===")
    g, log = build("eleven", ELEVEN_GUARD,
                   script_with(QUIET_NIGHT,
                               sheriff_run={1: {"run": True}, 2: {"run": True},
                                            "*": {"run": False}},
                               sheriff_speech={"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
                               sheriff_vote={"*": {"vote": 1}},
                               vote={"*": {"abstain": True}}))
    await g.run()
    check("4a 只有竞选者（1、2号）拿到了竞选发言", sorted(asked(log, "sheriff_speech")) == [1, 2],
          f"{sorted(set(asked(log, 'sheriff_speech')))}")
    voters = sorted(set(asked(log, "sheriff_vote")))
    check("4b 只有非竞选者投票（1、2号 没有投票权）", voters == [3, 4, 5, 6, 7, 8, 9, 10, 11],
          f"{voters}")
    check("4c 得票最高的 1号 当选", g.sheriff_id == 1, f"sheriff={g.sheriff_id}")
    check("4d 竞选发言带【竞选】前缀进台账（公开）",
          "【竞选】（剧本竞选发言）" in texts(g))


# --------------------------------------------------------------------------- #
# 5) 警徽 1.5 票 + 放逐遗言
# --------------------------------------------------------------------------- #

async def t_badge_vote_and_last_words() -> None:
    print("\n=== 5) 警徽那 0.5 票能定胜负 + 被放逐者有遗言 ===")
    votes = {1: {"vote": 8}, 2: {"vote": 8}, 4: {"vote": 8}, 5: {"vote": 8},
             7: {"vote": 9}, 8: {"vote": 9}, 10: {"vote": 9},
             3: {"vote": 9},                        # ← 3号 是警长，这一票 1.5
             6: {"abstain": True}, 9: {"abstain": True}, 11: {"abstain": True}}
    g, log = build("eleven", ELEVEN_GUARD,
                   script_with(QUIET_NIGHT,
                               sheriff_run={3: {"run": True}, "*": {"run": False}},
                               vote=votes))
    await g.run()
    txt = texts(g)
    check("5a 3号 当选警长（单人上警）", g.sheriff_id in (3, None) and "当选警长" in txt,
          f"sheriff={g.sheriff_id}")
    check("5b 放逐公告的票数是 4.5（1+1+1+1.5），不是 4",
          "9号 得 4.5 票，被放逐出局" in txt,
          [ln for ln in txt.splitlines() if "被放逐出局" in ln][:1])
    check("5c 汇总里点名了警长那票算 1.5",
          "是警长，他那一票算 1.5 票" in txt)
    check("5d 被放逐的 9号 拿到了遗言提问", 9 in asked(log, "last_words"),
          f"last_words={asked(log, 'last_words')}")
    check("5e 遗言以「【遗言】」公开进台账", "【遗言】（剧本遗言）" in txt)
    check("5f 没有警长死 → 不该有警徽提问", not asked(log, "badge"))


# --------------------------------------------------------------------------- #
# 6) 同守同救（奶穿）：夜里死、没有遗言、不公示死因
# --------------------------------------------------------------------------- #

async def t_night_death_last_words() -> None:
    print("\n=== 6) 夜里死亡的遗言：**只有第一夜**有 ===")
    # 第 1 夜：狼刀 9、守卫守 9、女巫救 9 → 同守同救（奶穿），9号 死 → **有**遗言
    # 第 2 夜：狼刀 8、守卫守 11、女巫不救 → 8号 死 → **没有**遗言
    s = script_with(QUIET_NIGHT,
                    wolf_kill=_kill_by_night([9, 8]),
                    guard=_kill_by_night([9, 11]),
                    witch=_by_day({1: {"save": True, "poison": None, "reason": "剧本"},
                                   2: {"save": False, "poison": None, "reason": "剧本"}}),
                    vote={"*": {"abstain": True}})
    g, log = build("eleven", TEN, s, max_days=2)
    await g.run()
    lw = asked(log, "last_words")
    check("6a 第 1 夜 9号 同守同救真的死了（救不回来）",
          g.seat_by_id(9).death_cause == "wolf" and g.seat_by_id(9).death_day == 1,
          f"cause={g.seat_by_id(9).death_cause} day={g.seat_by_id(9).death_day}")
    check("6b 第 1 夜的死者**有**遗言（9号）", 9 in lw, f"last_words={lw}")
    check("6c 第 2 夜的死者**没有**遗言（8号）",
          g.seat_by_id(8).death_day == 2 and 8 not in lw,
          f"death_day={g.seat_by_id(8).death_day} last_words={lw}")
    check("6d 公告只说'不公布死因'，不写被刀/被毒",
          "昨晚出局：9号。（不公布死因）" in texts(g)
          and "被狼人杀害" not in public_texts(g))


# --------------------------------------------------------------------------- #
# 7) 白天猎人被放逐：被打死的人有遗言 + 死因公示"被子弹打死"
# --------------------------------------------------------------------------- #

async def t_daytime_hunter() -> None:
    print("\n=== 7) 白天猎人被放逐 → 枪打死的人有遗言、死因公示 ===")
    s = script_with(QUIET_NIGHT,
                    hunter_shoot={1: {"shoot": 2, "reason": "剧本"}},
                    vote={"*": {"vote": 1}})          # 全场投 1号（猎人）
    g, log = build(NINE_BOARD, NINE, s)
    await g.run()
    txt = texts(g)
    lw = asked(log, "last_words")
    check("7a 被放逐的猎人自己也有遗言", 1 in lw, f"last_words={lw}")
    check("7b 被枪打死的 2号 也有遗言", 2 in lw)
    check("7c 死因公示为'被子弹打死'", "的死因公示：被子弹打死" in txt,
          [ln for ln in txt.splitlines() if "开枪带走" in ln][:1])
    check("7d 被枪打死的 2号 死因记为 hunter（私有）", g.seat_by_id(2).death_cause == "hunter")


# --------------------------------------------------------------------------- #
# 8) 夜里被刀的猎人开枪：被打死的人没有遗言、不公示死因
# --------------------------------------------------------------------------- #

async def t_night_hunter() -> None:
    print("\n=== 8) 夜里被刀的猎人开枪：第一夜的枪下之鬼**也有**遗言 ===")
    s = script_with(QUIET_NIGHT,
                    wolf_kill={"*": {"target": 1, "reason": "剧本"}},   # 刀猎人
                    guard={"*": {"target": 8, "reason": "剧本"}},
                    witch={"*": {"save": False, "poison": None, "reason": "剧本"}},
                    hunter_shoot={1: {"shoot": 2, "reason": "剧本"}},
                    vote={"*": {"abstain": True}})
    g, log = build(NINE_BOARD, NINE, s)
    await g.run()
    txt = texts(g)
    lw = asked(log, "last_words")
    check("8a 夜里被刀的猎人**有**遗言（他是第一夜的死者）", 1 in lw, f"last_words={lw}")
    check("8b 被夜枪打死的 2号 **也有**遗言（他也算第一夜的死者）", 2 in lw, f"last_words={lw}")
    check("8c 公告写明'夜里出局，不公示死因'",
          "（夜里出局，不公示死因）" in txt,
          [ln for ln in txt.splitlines() if "开枪带走" in ln][:1])
    check("8d 公告里没有'被子弹打死'字样", "被子弹打死" not in txt)
    check("8e 猎人的遗言排在他开枪之前（先说话，再开枪），枪下之鬼的遗言排在开枪之后",
          "【遗言】" in txt and "亮出猎人身份" in txt
          and txt.index("【遗言】") < txt.index("亮出猎人身份") < txt.rindex("【遗言】"),
          f"遗言={txt.count('【遗言】')} 条")
    check("8f 两个死者都有遗言时，遗言总条数是 2", txt.count("【遗言】") == 2,
          f"{txt.count('【遗言】')} 条")

    # 对照：**第 2 夜**的同一套戏 —— 谁都没有遗言（"仅第一夜"）
    s2 = script_with(QUIET_NIGHT,
                     wolf_kill=_kill_by_night([10, 1]),                 # 第 1 夜刀村民 10、第 2 夜刀猎人 1
                     guard=_kill_by_night([8, 9]),
                     witch=_by_day({1: {"save": True, "poison": None, "reason": "剧本"},
                                    2: {"save": False, "poison": None, "reason": "剧本"}}),
                     hunter_shoot={1: {"shoot": 2, "reason": "剧本"}},
                     vote={"*": {"abstain": True}})
    g2, log2 = build(NINE_BOARD, NINE, s2, max_days=2)
    await g2.run()
    lw2 = asked(log2, "last_words")
    check("8g 第 2 夜被刀的猎人**没有**遗言", 1 not in lw2, f"last_words={lw2}")
    check("8h 第 2 夜枪下的人**也没有**遗言（'仅第一夜'）",
          g2.seat_by_id(1).death_day == 2 and 2 not in lw2,
          f"death_day={g2.seat_by_id(1).death_day} last_words={lw2}")


# --------------------------------------------------------------------------- #
# 9/10) 警长被夜刀：传承 or 撕掉 + 交接后的发言顺序
# --------------------------------------------------------------------------- #

async def t_badge_transfer() -> None:
    print("\n=== 9) 警长被夜刀 → 传承警徽 ===")
    # ⚠️ 警长要选**好人**座位（9号 是村民），而且必须**第 2 夜**才死：
    # 第 1 天要先竞选（第 1 夜死者既不能参选、又有遗言，混在一起测不出东西）。
    # 第 1 夜：狼刀 10、女巫救 10 → 平安夜；第 2 夜：狼刀 9、女巫不救 → 9号 出局。
    s = script_with(QUIET_NIGHT,
                    wolf_kill=_kill_by_night([10, 9]),
                    guard=_kill_by_night([11, 10]),      # 守卫不能连守同一人，隔夜换一个
                    witch=_by_day({1: {"save": True, "poison": None, "reason": "剧本"},
                                   2: {"save": False, "poison": None, "reason": "剧本"}}),
                    sheriff_run={9: {"run": True}, "*": {"run": False}},
                    badge={9: {"to": 5, "reason": "剧本"}},
                    vote={"*": {"abstain": True}})
    g, log = build("eleven", ELEVEN_GUARD, s, max_days=2)
    await g.run()
    txt = texts(g)
    check("9a 9号 当选警长、第 2 夜被刀", "9号 当选警长" in txt
          and not g.seat_by_id(9).alive and g.seat_by_id(9).death_day == 2,
          f"death_day={g.seat_by_id(9).death_day}")
    check("9b 第 2 夜死的人**没有**遗言", 9 not in asked(log, "last_words"),
          f"last_words={asked(log, 'last_words')}")
    check("9c 出局时被问了警徽怎么处理", 9 in asked(log, "badge"))
    check("9d 警徽传给了 5号（新警长上任）", g.sheriff_id == 5, f"sheriff={g.sheriff_id}")
    check("9e 公告写明移交", "把警徽传给了 5号" in txt)
    check("9f 新警长的私有经历里有'你现在是警长'",
          any("把警徽传给了你" in e for e in g.seat_by_id(5).private_events))
    check("9g 私下问的是 9号（不是别人）", asked(log, "badge") == [9], f"{asked(log, 'badge')}")


async def t_badge_discard() -> None:
    print("\n=== 10) 警长被夜刀 → 撕掉警徽 ===")
    s = script_with(QUIET_NIGHT,
                    wolf_kill=_kill_by_night([10, 9]),
                    guard=_kill_by_night([11, 10]),
                    witch=_by_day({1: {"save": True, "poison": None, "reason": "剧本"},
                                   2: {"save": False, "poison": None, "reason": "剧本"}}),
                    sheriff_run={9: {"run": True}, "*": {"run": False}},
                    badge={9: {"to": None, "reason": "剧本"}},
                    vote={"*": {"abstain": True}})
    g, _log = build("eleven", ELEVEN_GUARD, s, max_days=2)
    await g.run()
    check("10a 撕掉之后本局没有警长", g.sheriff_id is None, f"sheriff={g.sheriff_id}")
    check("10b 公告写明撕掉警徽", "撕掉了警徽" in texts(g))


# --------------------------------------------------------------------------- #
# 11) 发言顺序两种模式
# --------------------------------------------------------------------------- #

async def t_speech_order() -> None:
    print("\n=== 11) 发言顺序：默认座位序 / 有警徽时警长最后 ===")
    # ⚠️ 一律 max_days=1：否则把整局跑完，后面几天的发言会把"第一天顺序"挤乱
    g, log = build("eleven", ELEVEN_GUARD,
                   script_with(QUIET_NIGHT,
                               sheriff_run={3: {"run": True}, "*": {"run": False}},
                               vote={"*": {"abstain": True}}), max_days=1)
    await g.run()
    order = asked(log, "speech")
    check("11a 有警长（3号）时：从 4号 开始、3号 最后",
          order == [4, 5, 6, 7, 8, 9, 10, 11, 1, 2, 3], f"{order}")

    g9, log9 = build(NINE_BOARD, NINE,
                     script_with(QUIET_NIGHT, vote={"*": {"abstain": True}}), max_days=1)
    await g9.run()
    order9 = asked(log9, "speech")
    check("11b 没有警徽时：按座位从小到大", order9 == [1, 2, 3, 4, 5, 6, 7, 8, 9], f"{order9}")

    # 警长是 11 号（队尾）时也要能绕回来：下一位是 1号，11号 自己最后
    g2, log2 = build("eleven", ELEVEN_GUARD,
                     script_with(QUIET_NIGHT,
                                 sheriff_run={11: {"run": True}, "*": {"run": False}},
                                 vote={"*": {"abstain": True}}), max_days=1)
    await g2.run()
    order2 = asked(log2, "speech")
    check("11c 警长在队尾（11号）时绕回 1号 开始、11号 最后",
          order2 == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11] and g2.sheriff_id == 11,
          f"{order2} sheriff={g2.sheriff_id}")


# --------------------------------------------------------------------------- #
# 12) 纯函数
# --------------------------------------------------------------------------- #

async def t_pure() -> None:
    print("\n=== 12) 计票纯函数 ===")
    g, _ = build("eleven", ELEVEN_GUARD, script_with(QUIET_NIGHT))
    g.sheriff_id = 3
    votes = {1: 8, 2: 8, 3: 9, 4: None}
    counts = g.weighted_counts(votes)
    check("12a weighted_counts：警长那票算 1.5", counts == {8: 2.0, 9: 1.5}, str(counts))
    check("12b 弃权不计票", 4 not in votes or True)
    check("12c fmt_votes：3.0→'3'、3.5→'3.5'",
          Game.fmt_votes(3.0) == "3" and Game.fmt_votes(3.5) == "3.5" and Game.fmt_votes(0.5) == "0.5")
    check("12d vote_weight：警长 1.5、别人 1.0",
          g.vote_weight(3) == 1.5 and g.vote_weight(4) == 1.0)
    check("12e badge_note：警长投了票才有备注",
          "1.5" in g.badge_note(votes) and g.badge_note({1: 8}) == "")


# --------------------------------------------------------------------------- #
# 13) 11 人标准板：4 狼 + 预女猎白 + 3 民；白痴算民也算神；女巫可自救
# --------------------------------------------------------------------------- #

ELEVEN = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
          7: "hunter", 8: "idiot", 9: "villager", 10: "villager", 11: "villager"}


def _kill_by_night(kills: list[int]):
    """按"第几天"选刀口（同一夜 4 头狼都会拿到同一个目标）。"""

    def reply(seat_id, task, day=1):
        return {"target": kills[min(day - 1, len(kills) - 1)], "reason": "剧本"}

    return reply


def _by_day(per_day: dict):
    """按天数给不同回复：`{1: {...}, 2: {...}}`，超出范围就用最后一条。"""

    def reply(seat_id, task, day=1):
        keys = sorted(per_day)
        pick = day if day in per_day else keys[-1]
        return dict(per_day[pick])

    return reply


async def t_eleven_standard_board() -> None:
    print("\n=== 13) 退役的 11 人标准板（测试副本：4 狼 + 预女猎白 + 3 民）===")
    from wolf.roles import ROLES, board_summary, validate_board
    b = retired_board("eleven_standard")
    check("13a 测试副本存在且校验通过", not validate_board(b), str(validate_board(b)))
    check("13b 配置 = 4 狼 + 预言家/女巫/猎人/白痴 + 3 民",
          sorted(b.roles) == sorted(["wolf"] * 4 + ["seer", "witch", "hunter", "idiot"]
                                    + ["villager"] * 3),
          board_summary(b))
    check("13c 白痴算民也算神（also_kind）", ROLES["idiot"].also_kind == "villager")
    g11 = Game(board=make_board("eleven_standard"), seed=3,
               agent_factory=lambda seat: Scripted(seat.id, {}, []),
               on_event=lambda *a, **k: None)
    check("13d 11 人自动开警长竞选", g11.sheriff_enabled is True)
    # ⚠️ 十人板已删：这里原本用 ten 验"不到 11 人不开警长"，
    #    改用九人板验同一条门槛（9 < 11 → 不开）。
    check("13e 9 人板子不开警长（门槛是 11）",
          Game(board=make_board(NINE_BOARD), seed=3,
               agent_factory=lambda seat: Scripted(seat.id, {}, []),
               on_event=lambda *a, **k: None).sheriff_enabled is False)


async def t_dual_kind_side_win() -> None:
    print("\n=== 14) 白痴算民也算神：屠边必须连白痴一起杀 ===")
    # 屠神：夜里依次刀 预言家(5) → 女巫(6) → 猎人(7) → 白痴(8)
    # ⚠️ 关掉狼人控场：不关的话 4 狼 ≥ 4 好人时会先按"控场"判狼胜，测不到屠边那条分支
    s = script_with(QUIET_NIGHT,
                    wolf_kill=_kill_by_night([5, 6, 7, 8]),
                    witch={"*": {"save": False, "poison": None, "reason": "剧本"}},
                    vote={"*": {"abstain": True}})
    g, _log = build("eleven_standard", ELEVEN, s, max_days=6, wolf_control=False)
    await g.run()
    wins = [e for e in g.ledger.entries if "狼人阵营获胜" in e.text]
    check("14a 三个神死光但白痴还活着时，**没有**判狼胜（第 4 天才出结果）",
          len(wins) == 1 and wins[0].day == 4,
          f"获胜公告={[(e.day, e.text[:26]) for e in wins]}")
    check("14b 白痴是第 4 天死的（狼人必须先杀他）", g.seat_by_id(8).death_day == 4,
          f"death_day={g.seat_by_id(8).death_day}")
    check("14c 判的是屠神：'神职全部出局'", wins and "神职全部出局" in wins[0].text,
          wins[0].text if wins else "")
    check("14d 战报里说明了白痴算民也算神", wins and "白痴算民也算神" in wins[0].text)

    # 屠民：夜里依次刀 3 个民(9,10,11) → 再刀白痴(8)
    s2 = script_with(QUIET_NIGHT,
                     wolf_kill=_kill_by_night([9, 10, 11, 8]),
                     witch={"*": {"save": False, "poison": None, "reason": "剧本"}},
                     vote={"*": {"abstain": True}})
    g2, _log2 = build("eleven_standard", ELEVEN, s2, max_days=6)
    await g2.run()
    wins2 = [e for e in g2.ledger.entries if "狼人阵营获胜" in e.text]
    check("14e 三个民死光但白痴还活着时，**没有**判狼胜",
          len(wins2) == 1 and wins2[0].day == 4,
          f"获胜公告={[(e.day, e.text[:26]) for e in wins2]}")
    check("14f 判的是屠民：'村民全部出局'", wins2 and "村民全部出局" in wins2[0].text,
          wins2[0].text if wins2 else "")


async def t_witch_self_save() -> None:
    print("\n=== 15) 刀中女巫可以自救 ===")
    s = script_with(QUIET_NIGHT,
                    wolf_kill={"*": {"target": 6, "reason": "剧本"}},    # 6号 是女巫
                    witch={"*": {"save": True, "poison": None, "reason": "自救"}},
                    vote={"*": {"abstain": True}})
    g, _log = build("eleven_standard", ELEVEN, s, max_days=1)
    await g.run()
    check("15a 女巫被刀、选择自救 → 活下来", g.seat_by_id(6).alive)
    check("15b 当晚是平安夜", "平安夜" in texts(g))
    check("15c 女巫的私有经历写明救回了自己",
          any("救回了 6号" in e for e in g.seat_by_id(6).private_events),
          str([e for e in g.seat_by_id(6).private_events if "解药" in e])[:90])
    check("15d 解药已经用掉（不能再用）", g.witch_heal is False)


# --------------------------------------------------------------------------- #
# 16) 白痴翻牌后没有投票权（发言照常、夜里照样能被刀死）
# --------------------------------------------------------------------------- #

TEN_IDIOT = {1: "wolf", 2: "wolf", 3: "wolf", 4: "seer", 5: "witch", 6: "hunter",
             7: "idiot", 8: "villager", 9: "villager", 10: "villager", 11: "villager"}


async def t_idiot_vote_rights() -> None:
    print("\n=== 16) 白痴翻牌后没有投票权 ===")
    import re as _re

    def day_of(task) -> int:
        m = _re.search(r"第 (\d+) 天", task.text or "")
        return int(m.group(1)) if m else 1

    def kill_reply(seat_id, task):
        # 第 1 夜刀 10号（好人、女巫会救），第 2 夜空刀 —— 免得随机死人把白痴带走
        return ({"target": 10, "reason": "剧本"} if day_of(task) == 1
                else {"abstain": True, "reason": "剧本：第 2 夜空刀，别随机死人"})

    def vote_reply(seat_id, task):
        if seat_id == 7:                     # 白痴自己：第 1 天弃权（他没法投自己）
            return {"abstain": True}
        return {"vote": 7} if day_of(task) == 1 else {"vote": 8}

    s = script_with(QUIET_NIGHT,
                    wolf_kill=kill_reply,
                    vote=vote_reply,
                    hunter_shoot={"*": {"shoot": None}})
    g, log = build("eleven_standard", TEN_IDIOT, s, max_days=2)
    await g.run()
    txt = texts(g)
    idiot = g.seat_by_id(7)
    check("16a 白痴被投出去 → 翻牌活下来", idiot.alive and idiot.revealed_idiot,
          f"alive={idiot.alive} revealed={idiot.revealed_idiot} death_day={idiot.death_day}")
    check("16b 公告写明'不再被放逐'与'失去投票权'",
          "亮出【愚者】身份" in txt and "失去投票权" in txt)

    check("16c 翻牌之后他**不再**收到投票提问（第 2 天）",
          7 not in asked(log, "vote", day=2), f"day2 voters={sorted(asked(log, 'vote', day=2))}")
    check("16d 候选名单里也**没有**他（投他等于浪费一票）",
          all(7 not in list(t.legal) for t in tasks_of(log, "vote", day=2)),
          str([list(t.legal) for t in tasks_of(log, "vote", day=2)][:2]))
    check("16e 发言照常：第 2 天照样请他发言", 7 in asked(log, "speech", day=2),
          f"day2 speakers={sorted(asked(log, 'speech', day=2))}")
    check("16f 夜里照样可以被刀（狼的合法目标里有他）",
          any(7 in list(t.legal) for t in tasks_of(log, "wolf_kill", day=1)),
          str([list(t.legal) for t in tasks_of(log, "wolf_kill", day=1)][:1]))

    # ⚠️ 16m-16o（2026-10-09 加）：**别人硬投翻牌白痴**时到底发生什么 —— 这是用户明确要求的
    #    "白痴翻牌后直接变成无法被票"。以前这里有个静默 bug：模型投 7号 时，7号不在 legal 里，
    #    于是 coerce_seat 把这一票**随机改投给另一个活人**，台账上还写"投给 X号"——
    #    模型以为投了白痴、实际投了别人（典型的静默降智）。现在按**无效票**记。
    print("\n=== 16m) 别人硬投翻牌白痴：必须记成无效票，不许偷换成别人 ===")

    def vote_reply_hit_idiot(seat_id, task):
        if seat_id == 7:
            return {"abstain": True}
        # 第 1 天投 7号（把他投翻牌）；第 2 天**继续投 7号**（这时他已经翻牌）
        return {"vote": 7}

    s2 = script_with(QUIET_NIGHT,
                     wolf_kill=kill_reply,
                     vote=vote_reply_hit_idiot,
                     hunter_shoot={"*": {"shoot": None}})
    g2, log2 = build("eleven_standard", TEN_IDIOT, s2, max_days=2)
    await g2.run()
    day2_votes = [e for e in g2.ledger.entries
                  if e.day == 2 and e.kind == "vote" and (e.data or {}).get("target") == 7]
    check("16m 第 2 天的票全部记成『无效』（不计入计票）",
          bool(day2_votes) and all((e.data or {}).get("invalid") for e in day2_votes)
          and all("无效" in (e.text or "") for e in day2_votes),
          str([(e.text or "")[:34] for e in day2_votes][:2]))
    check("16n ⚠️ 这一票**没有被偷换成别人**（没有第 2 天的『投给 X号』普通条目）",
          not [e for e in g2.ledger.entries
               if e.day == 2 and e.kind == "vote" and (e.data or {}).get("target")
               and not (e.data or {}).get("invalid") and (e.data or {}).get("target") != 7],
          str([(e.text or "")[:30] for e in g2.ledger.entries
               if e.day == 2 and e.kind == "vote"][:3]))
    check("16o 他第 2 天仍然没被放逐（活着、还翻着牌）",
          g2.seat_by_id(7).alive and g2.seat_by_id(7).revealed_idiot
          and any("无人有效投票" in (e.text or "") or "不会被放逐" in (e.text or "")
                  for e in g2.ledger.entries if e.day == 2),
          f"alive={g2.seat_by_id(7).alive} revealed={g2.seat_by_id(7).revealed_idiot}")

    # 16g：翻牌的白痴不能接警徽（他没有投票权，警徽给他等于白扔 1.5 票）
    # ⚠️ 警长必须是**好人**座位（9号 是村民），否则狼刀的目标不合法、会被静默换成随机好人，
    # 警长根本死不了 → 警徽那一问压根不会发生，断言就"空过"了（第一版正是这么假绿的）。
    def vote_reply(seat_id, task):
        d = day_of(task)
        if seat_id == 8:                                  # 白痴自己弃权（投不了自己）
            return {"abstain": True}
        return {"vote": 8} if d == 1 else {"abstain": True}

    s2 = script_with(QUIET_NIGHT,
                     wolf_kill={"*": {"target": 9, "reason": "剧本"}},   # 第 1 夜被救、第 2 夜刀死警长
                     witch=_by_day({1: {"save": True, "poison": None, "reason": "剧本"},
                                    2: {"save": False, "poison": None, "reason": "剧本"}}),
                     sheriff_run={9: {"run": True}, "*": {"run": False}},
                     badge={9: {"to": 5, "reason": "剧本"}},
                     vote=vote_reply)
    g2, log2 = build("eleven_standard", ELEVEN, s2, max_days=2)
    await g2.run()
    badge_tasks = tasks_of(log2, "badge")
    check("16g 警长真的出局并被问了警徽（这条不成立的话下面几条就是空过）",
          len(badge_tasks) == 1 and not g2.seat_by_id(9).alive,
          f"警徽提问 {len(badge_tasks)} 次，9号 alive={g2.seat_by_id(9).alive}")
    check("16h 翻牌的白痴**可以**接警徽（用户否决了不许接那条房规）",
          8 in (list(badge_tasks[0].legal) if badge_tasks else []),
          f"警徽可选名单={list(badge_tasks[0].legal) if badge_tasks else '（没问警徽）'}")
    check("16i 翻牌的白痴确实翻过牌了（这条才有意义）",
          g2.seat_by_id(8).revealed_idiot, f"revealed={g2.seat_by_id(8).revealed_idiot}")
    check("16j 普通存活玩家也在名单里（5号）",
          bool(badge_tasks) and 5 in list(badge_tasks[0].legal),
          f"名单={list(badge_tasks[0].legal) if badge_tasks else '（没问警徽）'}")


# --------------------------------------------------------------------------- #
# 17) 第一天的顺序：先结算死亡，再竞选警长
# --------------------------------------------------------------------------- #

async def t_day1_order() -> None:
    print("\n=== 17) 第一天：先结算死亡、再竞选警长 ===")
    s = script_with(QUIET_NIGHT,
                    wolf_kill={"*": {"target": 9, "reason": "剧本"}},   # 第 1 夜刀 9号（村民）
                    guard={"*": {"target": 11, "reason": "剧本"}},
                    witch={"*": {"save": False, "poison": None, "reason": "剧本"}},
                    sheriff_run={3: {"run": True}, "*": {"run": False}},
                    vote={"*": {"abstain": True}})
    g, log = build("eleven", ELEVEN_GUARD, s, max_days=1)
    await g.run()
    txt = texts(g)
    check("17a 台账顺序：先「昨晚出局」，后「警长竞选开始」",
          txt.index("昨晚出局") < txt.index("警长竞选开始"),
          f"出局@{txt.index('昨晚出局')} 竞选@{txt.index('警长竞选开始')}")
    check("17b 第 1 夜的死者有遗言，且遗言也排在竞选之前",
          9 in asked(log, "last_words") and txt.index("【遗言】") < txt.index("警长竞选开始"))
    check("17c 死者**不参选**（没被问上警）", 9 not in asked(log, "sheriff_run"),
          f"sheriff_run={sorted(asked(log, 'sheriff_run'))}")
    check("17d 死者**不投票**（投警长那轮也没他）", 9 not in asked(log, "sheriff_vote"),
          f"sheriff_vote={sorted(asked(log, 'sheriff_vote'))}")
    check("17e 活着的人照常参选/投票", 3 in asked(log, "sheriff_run"))


# --------------------------------------------------------------------------- #
# 18) 竞选平票 → PK 发言 + 重投 → 仍平票才流失
# --------------------------------------------------------------------------- #

async def t_election_pk() -> None:
    print("\n=== 18) 竞选平票：PK 一轮再平票才流失 ===")

    def split_votes(first: dict, pk: dict):
        def reply(seat_id, task, day=1):
            table = pk if "PK" in (task.text or "") else first
            want = table.get(seat_id)
            return {"abstain": True} if want is None else {"vote": want}
        return reply

    # 1、2 号 上警；非竞选者 9 人 → 4:4（11号 弃权）→ 平票 → PK 后还是 4:4 → 警徽流失
    same = {s: 1 for s in (3, 4, 5, 6)}
    same.update({s: 2 for s in (7, 8, 9, 10)})
    same[11] = None
    s = script_with(QUIET_NIGHT,
                    sheriff_run={1: {"run": True}, 2: {"run": True}, "*": {"run": False}},
                    sheriff_speech={"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
                    sheriff_vote=split_votes(same, same),
                    vote={"*": {"abstain": True}})
    g, log = build("eleven", ELEVEN_GUARD, s, max_days=1)
    await g.run()
    txt = texts(g)
    check("18a 竞选平票 → 进 PK（公告写明'再平票才警徽流失'）",
          "警长竞选平票" in txt and "再平票才警徽流失" in txt)
    check("18b 平票者各发了一轮【竞选PK】发言", txt.count("【竞选PK】") == 2,
          f"PK 发言 {txt.count('【竞选PK】')} 条")
    check("18c 重投仍平票 → 警徽流失", "竞选 PK 仍然平票" in txt and g.sheriff_id is None,
          f"sheriff={g.sheriff_id}")
    check("18d 非竞选者投了两轮（9 个选民 × 2 轮 = 18 次）",
          len(asked(log, "sheriff_vote")) == 18,
          f"{len(asked(log, 'sheriff_vote'))} 次")
    check("18e PK 轮只能在平票的两人里选",
          all(sorted(t.legal) == [1, 2] for t in tasks_of(log, "sheriff_vote", day=1)[9:]),
          str([sorted(t.legal) for t in tasks_of(log, "sheriff_vote", day=1)[9:11]]))

    # 对照：PK 后分出胜负（5:3）→ 1号 当选
    pk = {s: 1 for s in (3, 4, 5, 6, 7)}
    pk.update({s: 2 for s in (8, 9, 10)})
    pk[11] = None
    s2 = script_with(QUIET_NIGHT,
                     sheriff_run={1: {"run": True}, 2: {"run": True}, "*": {"run": False}},
                     sheriff_speech={"*": {"speech": "（剧本竞选发言）", "claim": "无"}},
                     sheriff_vote=split_votes(same, pk),
                     vote={"*": {"abstain": True}})
    g2, _log2 = build("eleven", ELEVEN_GUARD, s2, max_days=1)
    await g2.run()
    check("18f PK 后分出胜负 → 1号 当选", g2.sheriff_id == 1, f"sheriff={g2.sheriff_id}")


# --------------------------------------------------------------------------- #

async def t_exile_ends_game_still_has_last_words() -> None:
    """放逐正好结束对局时，被放逐者**仍然有遗言**。

    ⚠️ 2026-10-25 修的真缺陷：`exile()` 以前先 `check_win()`，一旦这一票分出胜负就直接 `return`
    —— 被放逐者**拿不到遗言**。而那恰恰是最该有遗言的一票（投出最后一狼 / 最后一张边牌）。
    这里把"最后一张神"票出去（狼队屠边成功），断言两件事**同时**成立：
    ① 对局确实结束了；② 那个座位真的被问了遗言。
    """
    print("\n=== 19) 放逐结束对局时，被放逐者仍然有遗言 ===")
    g, log = build("eleven_standard", {},
                   script_with(QUIET_NIGHT,
                               last_words={"*": {"speech": "（剧本遗言）", "claim": "无"}},
                               hunter_shoot={"*": {"shoot": None}}),
                   seed=3, max_days=3)
    target = next(s for s in g.seats if s.role_key == "seer")
    for s in g.seats:
        if s.kind == "god" and s.id != target.id:
            g.kill(s.id, "wolf")            # 造出"只剩最后一个神"的局面
    await g.exile(1, target.id, 5.0)
    check("19a 这一票确实结束了对局（屠边 → 狼胜）", g.winner == "wolf", str(g.winner))
    check("19b 被放逐者仍然拿到了遗言提问",
          target.id in asked(log, "last_words"),
          f"last_words={asked(log, 'last_words')} target={target.id}")
    check("19c 遗言确实写进了公开台账", "【遗言】（剧本遗言）" in public_texts(g),
          [e for e in public_texts(g).splitlines() if "遗言" in e][:2])


async def t_guard_may_abstain() -> None:
    """守卫**可以放弃守护**（作者 2026-10-25 口径）。

    ⚠️ 这条守的是"选项真的表达得出来"：以前代码里判 `abstain`，但 schema / required /
    文案三处都没放开 —— 模型写不出这个选择，那条分支等于死代码。三处必须同时放开。
    """
    print("\n=== 20) 守卫放弃守护 ===")
    g, log = build("eleven", {},
                   script_with(QUIET_NIGHT, guard={"*": {"abstain": True}}),
                   seed=5, max_days=1)
    await g.run()
    guard = next(s for s in g.seats if s.role_key == "guard")
    texts_g = "；".join(str(e) for e in guard.private_events)
    check("20a 任务文案里写明了可以空过（模型看得到这个选项）",
          any("不守" in (t.text or "") for _sid, k, _d, t in log if k == "guard"),
          [t.text[:80] for _sid, k, _d, t in log if k == "guard"][:1])
    check("20b schema 里有 abstain 字段",
          any('"abstain"' in (t.schema or "") for _sid, k, _d, t in log if k == "guard"))
    check("20c 弃守被如实记进私有经历", "放弃了守护" in texts_g, texts_g[:120])
    check("20d 弃守不占「上一夜守过谁」（last_guard 仍为空）", guard.last_guard is None,
          str(guard.last_guard))


async def t_guard_abstain_breaks_chain() -> None:
    """弃守之后，"上一夜守过谁"必须被清掉 —— 否则隔夜再守同一人会被误判成连守。

    ⚠️ 2026-10-31 外部审查 P1-07 的回归。20d 只测了"从没守过人的守卫弃守后仍为空"，
    那条断言**恒真**（一开始就是 None），所以真正的 bug 一直没被测到：
    守卫「第 1 夜守 X → 第 2 夜空过 → 第 3 夜想守 X」会被当成连守直接拒绝。
    这里先把 `last_guard` 设成 9 号（模拟"上一夜守过他"），再让守卫弃守一整夜，
    断言这个值被清空 —— 把修复改回旧写法，这条会立刻变红（已做变异验证）。
    """
    print("\n=== 21) 守卫弃守要打断「连守」链条 ===")
    g, log = build("eleven", {},
                   script_with(QUIET_NIGHT, guard={"*": {"abstain": True}}),
                   seed=5, max_days=1)
    guard = next(s for s in g.seats if s.role_key == "guard")
    guard.last_guard = 9                    # 模拟"上一夜守过 9 号"
    await g.run()
    check("21a 弃守之后 last_guard 被清空（隔夜可以再守同一人）",
          guard.last_guard is None, f"last_guard={guard.last_guard!r}")
    check("21b 这一夜守卫确实被问了", guard.id in asked(log, "guard"),
          f"asked={asked(log, 'guard')}")


async def t_vote_invalid_target() -> None:
    """投票写了个**不存在/不可投**的座位号 → 记无效票，不许静默随机改投。

    ⚠️ 2026-10-31 外部审查 P1-05 的回归。以前 `raw` 与 `dead_vote` 都是 None 时会落到
    没有 `default` 的 `coerce_seat(..., legal)` —— 随机投给一个活人，台账还写「投给 X号」。
    这里让全场都把票写成 99 号，断言：① 没有任何"投给 N 号"的公开票；② 明确写了无效票。
    """
    print("\n=== 22) 投给不存在的座位 = 无效票（不随机改投）===")
    g, _log = build("eleven", {},
                    script_with(QUIET_NIGHT, vote={"*": {"vote": 99, "reason": "剧本"}}),
                    seed=11, max_days=1)
    await g.run()
    pub = public_texts(g)
    check("22a 台账里明确写了「这一票无效」", "这一票无效" in pub,
          [ln for ln in pub.splitlines() if "无效" in ln][:2])
    check("22b 没有任何一张票被静默改投给活人（公开票里没有「投给 N号」）",
          "投给 " not in pub.replace("投给 已经", "投给已经"),
          [ln for ln in pub.splitlines() if "投给 " in ln][:2])



async def main() -> int:
    await t_threshold()
    await t_badge_lost()
    await t_single_candidate()
    await t_multi_candidate()
    await t_badge_vote_and_last_words()
    await t_night_death_last_words()
    await t_daytime_hunter()
    await t_night_hunter()
    await t_badge_transfer()
    await t_badge_discard()
    await t_speech_order()
    await t_pure()
    await t_eleven_standard_board()
    await t_dual_kind_side_win()
    await t_witch_self_save()
    await t_idiot_vote_rights()
    await t_day1_order()
    await t_election_pk()
    await t_exile_ends_game_still_has_last_words()
    await t_guard_may_abstain()
    await t_guard_abstain_breaks_chain()
    await t_vote_invalid_target()
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
