# coding=utf-8
"""板子与人数自检：6~12 人板子、自定义板子校验、守卫技能、屠城/屠边。

跑法：
    python tools/test_boards.py
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wolf.agents import MockAgent  # noqa: E402
from wolf.engine import Game  # noqa: E402
from wolf.retired_boards import retired_board  # noqa: E402
from wolf.roles import (BUILTIN_BOARDS, ROLES, board_summary, make_board,  # noqa: E402
                        role_name, validate_board)

FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ✅ " if ok else "  ❌ ") + message)
    if not ok:
        FAILURES.append(message)


async def play(board, seed: int = 7) -> Game:
    rng = random.Random(seed)
    game = Game(board=board, seed=seed,
                agent_factory=lambda seat: MockAgent(seat, rng),
                on_event=lambda *a, **k: None)
    await game.run()
    return game


async def main() -> int:
    print("=" * 78)
    print("一、内置板子")
    print("=" * 78)
    for name, board in BUILTIN_BOARDS.items():
        issues = validate_board(board)
        check(not issues, f"{name:<14} {board_summary(board)}"
                          + ("" if not issues else f"  ← {issues}"))

    print()
    print("=" * 78)
    print("二、坏板子必须被拦下")
    print("=" * 78)
    bad_cases = {
        "没有狼": ["seer", "witch", "villager", "villager"],
        "狼比好人多": ["wolf", "wolf", "wolf", "seer", "villager"],
        "角色名写错": ["wolf", "wolf", "seer", "witch", "村民"],
        "屠边但没有神": ["wolf", "wolf", "villager", "villager", "villager", "villager"],
        "人太少": ["wolf", "seer", "witch", "villager"],
        "win_rule 写错": ["wolf", "wolf", "seer", "witch", "villager", "villager"],
    }
    for label, roles in bad_cases.items():
        win_rule = "banana" if label == "win_rule 写错" else "sides"
        issues = validate_board(make_board("自定义", {"自定义": {"roles": roles, "win_rule": win_rule}}))
        check(bool(issues), f"{label:<12} 被拦下：{issues[0] if issues else '（居然没拦）'}")

    print()
    print("=" * 78)
    print("三、每张内置板子都能打完一局（离线代理）")
    print("=" * 78)
    # ⚠️ 2026-10-06 按用户要求删掉了 6/7/8/12 人场、2026-10-07 删 10 人、2026-10-09 删 9 人板。
    #    这里以前写死 `("nine_standard", "eleven", "eleven_standard")` —— 9 人板一删，
    #    整份自检**从第三节起直接 KeyError 崩掉**（前两节跑完了、后面两节一节没跑，
    #    看起来却像"板子自检通过"）。现在改成**遍历 `BUILTIN_BOARDS`**：
    #    加板子/删板子都不用再动这里。
    for name, board in BUILTIN_BOARDS.items():
        game = await play(board)
        wolves = sum(1 for s in game.seats if ROLES[s.role_key].camp == "wolf")
        check(len(game.seats) == len(board.roles) and game.winner in ("good", "wolf", "draw"),
              f"{name:<16}{len(game.seats):>2} 人 / {wolves} 狼 · 第 {game.day} 天结束 · 结果 {game.winner}"
              f" · 存活 {game.alive_ids()}")

    print()
    print("=" * 78)
    print("四、自定义板子 + 屠城规则")
    print("=" * 78)
    custom = {"我的8人": {"roles": ["wolf", "wolf", "wolf", "seer", "witch", "hunter",
                                    "villager", "villager"], "win_rule": "all",
                          "desc": "屠城测试"}}
    board = make_board("我的8人", custom)
    check(not validate_board(board), f"自定义板子可用：{board_summary(board)}")
    game = await play(board)
    check(game.win_rule == "all", f"屠城规则生效：win_rule={game.win_rule}，结果 {game.winner}")

    print()
    print("=" * 78)
    print("五、守卫技能（已退役的 eleven 板副本；产品的四张板子都没有守卫）")
    print("=" * 78)
    game = await play(retired_board("eleven"), seed=3)
    guard = next((s for s in game.seats if s.role_key == "guard"), None)
    check(guard is not None, "退役 eleven 板（测试副本）里有守卫")
    if guard:
        # ⚠️ 只有"真的守了人"那条才能抠出目标号。以前这里筛的是 `"守护" in e`，
        # 而引擎还会写「没有可守护的目标…」「你放弃了守护…」—— 那些行没有"守护了 "，
        # 下一行 split("守护了 ")[1] 直接 IndexError，把整个自检脚本崩掉（连板子自检都不跑了）。
        guard_events = [e for e in guard.private_events if "守护了 " in e]
        check(len(guard_events) >= 1,
              f"守卫确实行动过 {len(guard_events)} 次；例如：{guard_events[0] if guard_events else '（无）'}")
        # 不能连续两晚守同一人
        targets = []
        for e in guard_events:
            m = re.search(r"守护了\s*(\d+)\s*号", e)
            if m:
                targets.append(int(m.group(1)))
        check(len(targets) == len(guard_events),
              f"每条守卫记录都能抠出目标号：{targets}")
        repeated = any(targets[i] == targets[i + 1] for i in range(len(targets) - 1))
        check(not repeated, f"守卫没有连续两晚守同一个人：{targets}")

    print()
    print("=" * 78)
    print("六、2026-10-30 的三张新板子（12 人预女猎白 / 9 人屠城 / 6 人屠城明牌）")
    print("=" * 78)
    check(BUILTIN_BOARDS.get("twelve_standard") is not None
          and len(BUILTIN_BOARDS["twelve_standard"].roles) == 12
          and BUILTIN_BOARDS["twelve_standard"].win_rule == "sides",
          "twelve_standard：12 人、屠边")
    check(BUILTIN_BOARDS.get("nine_all") is not None
          and len(BUILTIN_BOARDS["nine_all"].roles) == 9
          and BUILTIN_BOARDS["nine_all"].win_rule == "all",
          "nine_all：9 人、屠城")
    check(BUILTIN_BOARDS.get("six_all_reveal") is not None
          and len(BUILTIN_BOARDS["six_all_reveal"].roles) == 6
          and BUILTIN_BOARDS["six_all_reveal"].win_rule == "all"
          and BUILTIN_BOARDS["six_all_reveal"].reveal_on_death is True,
          "six_all_reveal：6 人、屠城、出局亮身份")
    # 退役板子必须真的从产品里消失（名字还能被测试副本解析，但 make_board 不认）
    gone = [n for n in ("eleven", "eleven_standard", "eleven_dream", "eleven_guard")
            if n in BUILTIN_BOARDS]
    check(not gone, f"四张旧板子已从 BUILTIN_BOARDS 退役：{gone or '已全部下架'}")

    # 6 人明牌板真打一局：每个出局的人都要有一条「亮明身份」公告，而且只亮身份不亮死因
    game6 = await play(make_board("six_all_reveal"), seed=5)
    reveals = [e.text for e in game6.ledger.entries if "亮明身份" in e.text]
    dead = [s.id for s in game6.seats if not s.alive]
    revealed_seats = {int(re.search(r"^(\d+)号", t).group(1)) for t in reveals
                      if re.match(r"^\d+号", t)}
    check(len(dead) > 0 and revealed_seats == set(dead),
          f"6 人明牌板：{len(dead)} 个出局者都亮出了身份（公告 {len(reveals)} 条）")
    check(not any(("被狼人杀害" in t or "被毒死" in t or "被放逐" in t)
                  for t in reveals),
          "亮身份公告里没有混进死因（死因另有公告）")
    roles_shown = {role_name(s.role_key) for s in game6.seats if not s.alive}
    check(all(any(f"【{rn}】" in t for t in reveals) for rn in roles_shown),
          f"亮出来的身份跟引擎里的牌一致：{sorted(roles_shown)}")
    # 暗牌板子（默认）不许出现「亮明身份」
    game_dark = await play(make_board("nine_all"), seed=5)
    check(not any("亮明身份" in e.text for e in game_dark.ledger.entries),
          "9 人暗牌板没有出现「亮明身份」公告（暗牌就是不亮）")

    print()
    if FAILURES:
        print(f"❌ 自检失败 {len(FAILURES)} 项")
        return 1
    print("✅ 板子与技能自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
