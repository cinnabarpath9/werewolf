# coding=utf-8
"""验"边界口径"：同一夜里"屠边完成"与"最后一狼被抓出来"同时成立时算谁赢。

用户定的口径：**屠边优先 → 狼胜**（edge_order: sides_first，默认）。
场景：狼刀掉最后的神（猎人/女巫），而对方死前那一枪/那一毒带走了最后一狼。
"""
import asyncio
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def build(edge: str, board: str = "eleven_standard") -> Game:
    g = Game(board=make_board(board), agent_factory=None, seed=1, max_days=1, edge_order=edge)
    g.on_event = lambda *a, **k: None
    return g


def kill(g: Game, seat_id: int, cause: str = "wolf") -> None:
    seat = g.seat_by_id(seat_id)
    g.alive.remove(seat)
    seat.alive = False
    seat.death_day = g.day
    seat.death_cause = cause


print("=== 场景：狼刀掉最后一个神，而她/他死前带走了最后一狼 ===")
for edge, want in (("sides_first", "wolf"), ("last_wolf_first", "good")):
    g = build(edge)
    wolves = [s.id for s in g.seats if s.role_key == "wolf"]
    gods = [s.id for s in g.seats if s.role_key in ("seer", "witch", "hunter", "idiot")]
    # 先杀掉除"最后一个神"以外的所有神与民，再把狼杀到只剩 1 匹
    for sid in gods[:-1]:
        kill(g, sid)
    for sid in [s.id for s in g.seats if s.role_key == "villager"]:
        kill(g, sid)
    for sid in wolves[:-1]:
        kill(g, sid)
    # 最后一步：同时发生 —— 最后一个神死（屠边完成）+ 最后一狼死
    kill(g, gods[-1])
    kill(g, wolves[-1])
    got = g.check_win()
    check(f"{edge}：同时成立时判 → {'狼胜' if want == 'wolf' else '好人胜'}", got == want,
          f"实际 {got}（神边剩 {len([s for s in g.alive if s.kind != 'wolf'])} 人、"
          f"狼剩 {len([s for s in g.alive if s.kind == 'wolf'])} 匹）")

print("\n=== 只有狼被抓出来（没屠边）时仍然是好人胜 ===")
for edge in ("sides_first", "last_wolf_first"):
    g = build(edge)
    wolves = [s.id for s in g.seats if s.role_key == "wolf"]
    for sid in wolves:
        kill(g, sid)
    check(f"{edge}：只剩最后一狼死（未屠边）→ 好人胜", g.check_win() == "good", g.winner or "")

print("\n=== 只有屠边完成（狼还活着）时仍然是狼胜 ===")
for edge in ("sides_first", "last_wolf_first"):
    g = build(edge)
    for sid in [s.id for s in g.seats if s.role_key in ("seer", "witch", "hunter", "idiot")]:
        kill(g, sid)
    check(f"{edge}：屠神完成 → 狼胜", g.check_win() == "wolf", g.winner or "")

print("\n=== 控场口径：活着但**没牌**的神不算「还能反击」（作者 2026-10-25 定稿）===")


def prepare(keep_roles: set[str], board: str = "eleven") -> Game:
    """只留下这些角色（其余按"夜里出局"清掉），用来单独验 `good_can_still_kill()`。"""
    g = build("sides_first", board=board)
    for s in list(g.seats):
        if s.role_key not in keep_roles:
            kill(g, s.id)
    return g


_g = prepare({"wolf", "witch", "seer"})
_g.witch_poison = False
check("空药女巫 + 只验人的预言家 ⇒ 不算还有反击能力", _g.good_can_still_kill() is False,
      f"witch_heal={_g.witch_heal} witch_poison={_g.witch_poison}")
_g.witch_poison = True
check("女巫毒药还在 ⇒ 算还有反击能力", _g.good_can_still_kill() is True)
_g2 = prepare({"wolf", "hunter"})
check("猎人活着（枪还在）⇒ 算", _g2.good_can_still_kill() is True)
_g3 = prepare({"wolf", "dreamer"}, board="eleven_dream")
check("摄梦人活着（能连摄杀人）⇒ 算", _g3.good_can_still_kill() is True)
_g4 = prepare({"wolf", "seer"})
check("只剩预言家 ⇒ 不算（验人不杀人）", _g4.good_can_still_kill() is False)

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
