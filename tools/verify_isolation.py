# coding=utf-8
"""独立防全知验证器（对**真实发出的 packet** 双向核对，不信装配器自述）。

来历：借 DND 跑团项目的 P5-Q/U11 三条口径（2026-11-05）——
1. 不信 `context.build` 的自述，抓它真正产出的文本逐座位查；
2. **双向**：不该看到的看到 = 泄漏；**该看到的没看到 = 覆盖缺口**（否则"把包清空"也能装作通过）；
3. 守卫要能自证会红：`--mutate` 故意造一次泄漏，必须被抓住。

用法：
    python tools/verify_isolation.py            # 跑一局 mock（11 人板），有违规 exit=1
    python tools/verify_isolation.py --mutate   # 故意泄漏一条，验证器必须抓到（exit=0 才算守卫有效）

它检查什么（每个座位、每次调用抓到的 packet）：
- 公告（public）→ 每个人都要有；
- 自己的私有（seat:N）→ 本人要有；
- **别人的私有（seat:M）→ 本人绝不能有**；
- 狼队私有（wolf）→ 狼要有；好人绝不能有。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context                                          # noqa: E402
from wolf.agents import MockAgent                                 # noqa: E402
from wolf.context import Task                                     # noqa: E402
from wolf.engine import Game                                      # noqa: E402
from wolf.roles import make_board                                 # noqa: E402

NEEDLE = 40          # 取前 40 字做匹配（避免 800 字截断导致误判）


def _needle(text: str) -> str:
    return str(text or "").strip()[:NEEDLE]


def run_game(board: str = "eleven_wolfking", mutate: bool = False):
    """跑一局 mock，抓住每次真实 packet + 抓取瞬间的台账。"""
    records: list[dict] = []
    orig = context.build

    def spy(state, seat, task: Task):
        pkt = orig(state, seat, task)
        entries = list(state.ledger.entries)
        rec = {
            "seat": seat.id, "camp": seat.camp, "day": state.day, "phase": state.phase,
            "packet": pkt,
            "public": [e for e in entries if "public" in e.visible_to],
            "own": [e for e in entries if f"seat:{seat.id}" in e.visible_to],
            "others": [e for e in entries
                       if any(str(v).startswith("seat:") for v in e.visible_to)
                       and f"seat:{seat.id}" not in e.visible_to],
            "wolf": [e for e in entries if "wolf" in e.visible_to],
        }
        if mutate and rec["others"]:
            # 故意把别人的一条私有塞进这个包 → 验证器必须报"泄漏"
            leak = rec["others"][0].text
            rec["packet"] = pkt + "\n" + str(leak)
        records.append(rec)
        return pkt

    context.build = spy
    try:
        g = Game(board=make_board(board), human_seat=0, max_days=1,
                 agent_factory=lambda seat: MockAgent(seat, random.Random(seat.id)),
                 on_event=lambda *a, **k: None, sheriff=None, seed=7)
        asyncio.run(g.run())
        seats = len(g.seats)
    finally:
        context.build = orig
    return records, seats





def check(records: list[dict]) -> list[str]:
    bad: list[str] = []
    for r in records:
        tag = f"{r['seat']}号 d{r['day']}/{r['phase']}"
        pkt = r["packet"]
        for e in r["public"]:
            n = _needle(e.text)
            if n and n not in pkt:
                bad.append(f"覆盖缺口 public  {tag}  缺：{n}")
        for e in r["own"]:
            n = _needle(e.text)
            if n and n not in pkt:
                bad.append(f"覆盖缺口 own     {tag}  缺：{n}")
        # ⚠️ mock 会产出**多条一模一样的私有文本**（如「（你的选择）前置位上警。」），
        #    按文本查会把"自己的那条"误判成"别人的" → 只查**全局唯一**的文本。
        owned = {_needle(e.text) for e in r["public"] + r["own"]}
        for e in r["others"]:
            n = _needle(e.text)
            if n and n not in owned and n in pkt:
                bad.append(f"泄漏 others      {tag}  出现：{n}")
        for e in r["wolf"]:
            n = _needle(e.text)
            if not n:
                continue
            if r["camp"] == "wolf":
                if n not in pkt:
                    bad.append(f"覆盖缺口 wolf    {tag}  缺：{n}")
            else:
                if n not in owned and n in pkt:
                    bad.append(f"泄漏 wolf        {tag}  出现：{n}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--board", default="eleven_wolfking")
    args = ap.parse_args()

    records, seats = run_game(args.board, mutate=args.mutate)
    print(f"抓取 {len(records)} 个 packet（{seats} 座）")
    bad = check(records)
    if args.mutate:
        if bad:
            print(f"✅ 变异被抓住（{len(bad)} 条）：")
            for line in bad[:5]:
                print("   " + line)
            return 0
        print("❌ 变异没被抓住 —— 守卫失效，不是'没问题'")
        return 1
    if bad:
        print(f"❌ {len(bad)} 条违规：")
        for line in bad[:20]:
            print("   " + line)
        return 1
    print("✅ 没有泄漏、也没有覆盖缺口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())