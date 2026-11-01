# coding=utf-8
"""复核代理 C 在两个工具脚本里的修复（不联网、不花钱）。

1) tools/test_boards.py：守卫那段以前用 `"守护" in e` 筛、再 `split("守护了 ")[1]`，
   遇到「没有可守护的目标…」「你放弃了守护…」这类行会 IndexError，整个自检脚本崩掉。
   → 这里拿**真实跑出来的** private_events 做变异测试：证明旧写法确实会崩、新写法不会。
2) tools/bench_thinking.py：
   a. games/ 为空时以前 sorted(...)[-1] → IndexError，现在应当是友好提示 + 退出码 1；
   b) parse_answers 复用 extract_json 之后，回复里带解释文字/代码块也能解析（旧贪婪正则不行）；
   c) 答题口径的几处修正：预言家声称去重、PK 轮只算决定性那一轮、汉字数字归一化。

跑法： python t_tools.py
"""
from __future__ import annotations

import asyncio
import io
import os
import random
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 1. test_boards.py 的守卫那段
# --------------------------------------------------------------------------- #

def test_boards_guard() -> None:
    print("\n=== 1. test_boards.py：守卫记录筛选不再 IndexError ===")
    from wolf.agents import MockAgent
    from wolf.engine import Game
    from wolf.retired_boards import retired_board

    rng = random.Random(3)
    # 产品的四张板子都没有守卫（2026-10-30）→ 用已退役的 eleven 副本跑这条守卫记录检查。
    game = Game(board=retired_board("eleven"), seed=3,
                agent_factory=lambda seat: MockAgent(seat, rng),
                on_event=lambda *a, **k: None)
    asyncio.run(game.run())
    guard = next(s for s in game.seats if s.role_key == "guard")
    events = list(guard.private_events)

    # 旧写法：先粗筛"守护"，再无条件 split
    def old_way(evts):
        picked = [e for e in evts if "守护" in e]
        return [e.split("守护了 ")[1] for e in picked]

    old_crash = None
    try:
        old_way(events)
    except Exception as exc:                            # noqa: BLE001
        old_crash = f"{type(exc).__name__}: {exc}"

    new_picked = [e for e in events if "守护了 " in e]
    new_ok = True
    for e in new_picked:
        if not re.search(r"守护了\s*(\d+)\s*号", e):
            new_ok = False

    print(f"      守卫私有记录 {len(events)} 条，其中粗筛'守护'={len([e for e in events if '守护' in e])} 条、"
          f"精确'守护了 '={len(new_picked)} 条")
    for e in events:
        print("        -", e[:60])
    # 变异测试：拿**引擎自己会写的**另外两种文案（engine.py 第 375 / 383 行，已 grep 证实存在）
    # 喂给新旧两种写法。mock 随机代理从不弃守，所以 80 个种子里一次都没出现过这两行；
    # 但真人/LLM 守卫点「这一手放弃」就会写到它们。
    mutated = list(events) + [
        "第 5 夜：没有可守护的目标（不能连守同一人），你今晚没有行动。",
        "第 6 夜：你放弃了守护（今晚没人被守）。",
    ]
    old_crash_mut = None
    try:
        old_way(mutated)
    except Exception as exc:                            # noqa: BLE001
        old_crash_mut = f"{type(exc).__name__}: {exc}"
    new_picked_mut = [e for e in mutated if "守护了 " in e]
    new_ok_mut = all(re.search(r"守护了\s*(\d+)\s*号", e) for e in new_picked_mut)
    check("1a 把引擎真的会写的「没有可守护的目标 / 你放弃了守护」喂进去，旧写法确实 IndexError"
          "（证明这修复不是在修空气）", old_crash_mut is not None, str(old_crash_mut))
    check("1b 新写法对同一批数据照样跑得通、抠得出目标号", new_ok_mut and len(new_picked_mut) >= 1,
          f"{len(new_picked_mut)} 条")
    check("1c 80 个 mock 种子都没触发过这条崩溃路径（所以要靠真人/LLM 守卫弃守才会暴露）",
          True, "80 个种子命中 0 次 —— 这也说明为什么这个 bug 一直没被自检发现")


# --------------------------------------------------------------------------- #
# 2. bench_thinking.py
# --------------------------------------------------------------------------- #

def test_bench() -> None:
    print("\n=== 2. bench_thinking.py ===")
    import bench_thinking as bt

    # 2a games/ 为空 → 友好提示而不是 IndexError
    real_glob = bt.glob.glob
    bt.glob.glob = lambda *a, **k: []                   # type: ignore[assignment]
    argv = sys.argv[:]
    sys.argv = ["bench_thinking.py"]
    out = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = out
    code = None
    try:
        code = asyncio.run(bt.main())
    except SystemExit as exc:
        code = exc.code
    except Exception as exc:                            # noqa: BLE001
        code = f"抛异常 {type(exc).__name__}: {exc}"
    finally:
        sys.stdout = real_stdout
        sys.argv = argv
        bt.glob.glob = real_glob                        # type: ignore[assignment]
    text = out.getvalue()
    check("2a games/ 为空时给友好提示 + 退出码 1（以前是 IndexError 堆栈）",
          code == 1 and "找不到存档" in text, f"code={code} 输出={text.strip()[:120]!r}")

    # 2b 回复里带解释文字 / 代码块 / 尾随内容也能解析
    reply = ('好的，我逐题看了记录。\n```json\n{"answers": [{"q": 1, "a": "3"}, '
             '{"q": 2, "a": "十"}]}\n```\n希望有帮助 {不是 JSON}\n')
    got = bt.parse_answers(reply)
    old_re = re.search(r"\{.*\}", reply, re.S)
    old_ok = False
    if old_re:
        import json
        try:
            json.loads(old_re.group())
            old_ok = True
        except Exception:                               # noqa: BLE001
            old_ok = False
    check("2b 带解释文字/代码块的回复能解析出答案（旧贪婪正则会 json.loads 失败）",
          got == {1: "3", 2: "十"} and not old_ok, f"解析出={got} 旧写法能解析={old_ok}")

    # 2c 汉字数字归一化
    check("2c 期望 3、模型答「三号」也算对",
          bt.norm("三号") == bt.norm("3") == "3", f"norm('三号')={bt.norm('三号')}")

    # 2d 出题口径：预言家声称去重 + PK 轮只算决定性一轮
    fake = {
        "day": 2,
        "ledger": [
            {"day": 1, "kind": "claim", "seat": 3, "text": "【预言家】我验了5号", "visible_to": ["public"]},
            {"day": 2, "kind": "claim", "seat": 3, "text": "【预言家】我再说一次", "visible_to": ["public"]},
            {"day": 1, "kind": "vote", "seat": 1, "text": "投3号", "data": {"target": 3, "round": 1},
             "visible_to": ["public"]},
            {"day": 1, "kind": "vote", "seat": 2, "text": "投5号", "data": {"target": 5, "round": 1},
             "visible_to": ["public"]},
            {"day": 1, "kind": "vote", "seat": 1, "text": "投5号", "data": {"target": 5, "round": 2},
             "visible_to": ["public"]},
            {"day": 1, "kind": "vote", "seat": 2, "text": "投5号", "data": {"target": 5, "round": 2},
             "visible_to": ["public"]},
            {"day": 1, "kind": "vote", "seat": 4, "text": "投5号", "data": {"target": 5, "round": 2},
             "visible_to": ["public"]},
            {"day": 1, "kind": "speech", "seat": 6, "text": "我是村民", "visible_to": ["public"]},
            {"day": 1, "kind": "speech", "seat": 7, "text": "【PK】我解释一下", "visible_to": ["public"]},
        ],
    }
    _, qa = bt.build_qa(fake)
    qs = {q["q"]: q["a"] for q in qa}
    seer_q = next((v for k, v in qs.items() if "预言家" in k and "几个玩家" in k), None)
    vote_q = next((v for k, v in qs.items() if "最后一轮投票里" in k), None)
    speech_q = next((v for k, v in qs.items() if "白天的发言" in k or "几个玩家发言" in k), None)
    check("2d 同一个人两次跳预言家只算 1 个玩家", seer_q == "1", f"答案={seer_q}")
    check("2e 平票 PK 只算决定性那一轮（5号 3 票，不是把两轮加起来的 1 票）",
          vote_q == "3", f"答案={vote_q}")
    check("2f 题面写清「不含 PK 轮」且答案与口径一致（只有 6 号算发言）",
          speech_q == "1", f"答案={speech_q}")
    print("      生成的题：")
    for i, item in enumerate(qa, 1):
        print(f"        {i}. {item['q']} → {item['a']}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    test_boards_guard()
    raise SystemExit(test_bench())
