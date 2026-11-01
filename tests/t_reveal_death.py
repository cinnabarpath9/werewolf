# coding=utf-8
"""回归：新板子的两条**胜负/信息公开**口径（2026-10-30 加）。

这一轮产品从「五张 11 人板」改成「四张板子」：默认 11 人狼王版 + **12 人预女猎白** +
**9 人屠城（暗牌）** + **6 人屠城（明牌）**。新东西有三样，必须各钉一条：

  1. **屠城**（`win_rule: all`）：狼要杀光**所有好人**才赢 —— 杀光神职**不算**、
     杀光村民**也不算**。这条写错就等于把 9 / 6 人板的胜负条件讲错了。
  2. **出局亮身份**（`reveal_on_death`，只有 6 人明牌板开着）：
     任何人出局时公开身份牌；**死因照旧不公示**。
  3. 这两条都是**板子级**的：暗牌板子一个字都不许出现（发错对象 = 给模型假情报）。

跑法：`python tests/t_reveal_death.py`
"""
from __future__ import annotations

import asyncio
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import rules                                          # noqa: E402
from wolf.agents import MockAgent                               # noqa: E402
from wolf.context import Task, build                            # noqa: E402
from wolf.engine import Game                                    # noqa: E402
from wolf.roles import make_board, role_name                   # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(extra)) if extra else ""))


async def play(board_name: str, seed: int = 5) -> Game:
    rng = random.Random(seed)
    g = Game(board=make_board(board_name), seed=seed,
             agent_factory=lambda seat: MockAgent(seat, rng),
             on_event=lambda *a, **k: None)
    await g.run()
    return g


async def main() -> int:
    reveal_board = make_board("six_all_reveal")
    dark_boards = {n: make_board(n) for n in ("eleven_wolfking", "twelve_standard", "nine_all")}

    print("=== 一、开关只挂在 6 人明牌板 ===")
    check("1a six_all_reveal 带「出局亮身份」开关", reveal_board.reveal_on_death is True)
    check("1b 另外三张板子都是暗牌",
          all(not b.reveal_on_death for b in dark_boards.values()),
          {n: b.reveal_on_death for n, b in dark_boards.items()})
    check("1c 6 人板是屠城（杀光所有好人）", reveal_board.win_rule == "all")
    check("1d 9 人板也是屠城", make_board("nine_all").win_rule == "all")
    check("1e 12 人板是屠边", make_board("twelve_standard").win_rule == "sides")

    print("\n=== 二、规则文本按板子发（写错 = 给模型假情报）===")
    t_reveal = rules.compose(reveal_board.roles, sheriff=False, win_rule=reveal_board.win_rule,
                             reveal_on_death=reveal_board.reveal_on_death)
    check("2a 明牌板的规则里有【出局亮身份】", "出局亮身份" in t_reveal)
    check("2b 明牌板的规则里胜负是**屠城**", "屠城" in t_reveal and "屠边：狼人杀光全部神职" not in t_reveal)
    for name, b in dark_boards.items():
        t = rules.compose(b.roles, sheriff=len(b.roles) >= 11, win_rule=b.win_rule,
                          reveal_on_death=b.reveal_on_death)
        check(f"2c[{name}] 暗牌板没有「出局亮身份」这段", "出局亮身份" not in t)
    t_nine = rules.compose(make_board("nine_all").roles, win_rule="all")
    check("2d 9 人屠城板写的是屠城、不是屠边",
          "屠城" in t_nine and "屠边：狼人杀光全部神职" not in t_nine)

    print("\n=== 三、明牌板整局：每个出局者都亮身份 ===")
    g = await play("six_all_reveal")
    dead = [s for s in g.seats if not s.alive]
    check("3a 这一局确实有人出局（否则下面几条会空过）", len(dead) > 0, f"{len(dead)} 人出局")
    announces = [e for e in g.ledger.entries if "亮明身份" in e.text]
    check("3b 每个出局者各有一条亮身份公告", len(announces) == len(dead),
          f"公告 {len(announces)} 条 / 出局 {len(dead)} 人")
    for s in dead:
        want = f"{s.id}号 出局，亮明身份：【{role_name(s.role_key)}】。"
        hit = [e for e in g.ledger.entries if e.text == want]
        check(f"3c {s.id}号（{role_name(s.role_key)}）的公告文案与引擎里的牌一致，且是公开条目",
              len(hit) == 1 and all("public" in e.visible_to for e in hit),
              f"找到 {len(hit)} 条")
    cause_words = ("被狼人杀害", "被女巫毒死", "被投票放逐", "被猎人开枪", "被狼王带走")
    check("3d 亮身份公告里**不写死因**（只亮身份）",
          not any(w in e.text for e in announces for w in cause_words))
    # 【已出局】状态摘要也要带身份（每个包都会带这一段）
    dead_list = dict(g.dead_list())
    check("3e dead_list 给每个出局者带上身份",
          all(role_name(s.role_key) in dead_list.get(s.id, "") for s in dead),
          str(dead_list))
    # 公开信息：每个座位的包里都看得到（含还活着的人）
    probe = Task(kind="inspect", text="（自检）", schema="{}", legal=[])
    packets = {s.id: build(g, s, probe) for s in g.seats}
    check("3f 亮身份公告进了**每个座位**的包（公开信息）",
          all("亮明身份" in p for p in packets.values()),
          [sid for sid, p in packets.items() if "亮明身份" not in p])

    print("\n=== 四、暗牌板对照：同一条死亡路径、不亮身份 ===")
    g2 = await play("nine_all")
    check("4a 暗牌板整局没有「亮明身份」公告",
          not any("亮明身份" in e.text for e in g2.ledger.entries))
    check("4b 暗牌板的 dead_list 不带身份",
          all("·" not in why for _sid, why in g2.dead_list()), str(g2.dead_list()))
    # 直接 kill 一条：公告只由板子开关决定（不依赖白天流程 / 是否被放逐）
    g3 = Game(board=make_board("six_all_reveal"), agent_factory=None, seed=3, max_days=1,
              on_event=lambda *a, **k: None)
    g3.day = 2
    g3.kill(3, "wolf")
    lit = [e for e in g3.ledger.entries if "亮明身份" in e.text]
    check("4c 明牌板：直接 kill 也会亮身份（夜里路径同样生效）",
          len(lit) == 1 and role_name(g3.seat_by_id(3).role_key) in lit[0].text, str(lit))
    g4 = Game(board=make_board("nine_all"), agent_factory=None, seed=3, max_days=1,
              on_event=lambda *a, **k: None)
    g4.day = 2
    g4.kill(3, "wolf")
    check("4d 暗牌板：同一条 kill 路径不亮身份（变异对照）",
          not any("亮明身份" in e.text for e in g4.ledger.entries))

    print("\n=== 五、屠城：杀光一边**不算赢**，杀光所有好人才算 ===")
    # 5a) 9 人板：把三个神全杀掉 → 屠边条件成立，但屠城板不许判胜
    g5 = Game(board=make_board("nine_all"), agent_factory=None, seed=3, max_days=1,
              wolf_control=False, on_event=lambda *a, **k: None)
    g5.day = 3
    gods = [s.id for s in g5.seats if s.role.kind == "god"]
    villagers = [s.id for s in g5.seats if s.role.kind == "villager"]
    for sid in gods:
        g5.kill(sid, "vote")
    check("5a 9 人屠城板：神全死光**不算赢**（屠边的条件对屠城板无效）",
          g5.check_win() is None, f"winner={g5.winner}")
    # 5b) 再把村民也杀光 → 好人全灭，狼胜
    for sid in villagers:
        g5.kill(sid, "vote")
    check("5b 9 人屠城板：所有好人死光 → 狼胜，公告是「好人全部出局」",
          g5.check_win() == "wolf"
          and any("好人全部出局" in e.text for e in g5.ledger.entries),
          f"winner={g5.winner}")
    # 5c) 只杀光村民、神还活着 → 也不算
    g6 = Game(board=make_board("six_all_reveal"), agent_factory=None, seed=4, max_days=1,
              wolf_control=False, on_event=lambda *a, **k: None)
    g6.day = 3
    for sid in [s.id for s in g6.seats if s.role.kind == "villager"]:
        g6.kill(sid, "vote")
    check("5c 6 人屠城板：村民死光但神还活着 → 没结束",
          g6.check_win() is None, f"winner={g6.winner}")

    print()
    if BAD:
        print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
        return 1
    print(f"共 {N} 项：通过 {N}，失败 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
