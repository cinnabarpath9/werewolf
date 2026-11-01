# coding=utf-8
"""回归：`call_timeout`（单次硬上限）**只砍要调模型的座位**，不许砍真人座位。

背景（2026-10-07 复查发现的真冲突）：第一版 `one_call()` 把 150 秒上限一律套上，
真人座位也被砍 —— 而真人自己的 `human_timeout` 是 900 秒。后果是你在手机上多想一会儿，
引擎就判"调用失败、按随机处理"，还往你座位上写一条"系统提示：你这次决策调用失败"。
"""
import asyncio
import sys
import time

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class SlowHuman:
    """真人代理：不调模型，就是慢慢想（这里用 1.6 秒代表"真人思考"）。"""
    uses_model = False

    def __init__(self, delay: float):
        self.delay = delay

    async def ask(self, packet, task):
        await asyncio.sleep(self.delay)
        return {"speech": "我想好了。", "vote": 3}

    def last_meta(self):
        return {}


class SlowModel:
    """模型代理：调模型，但上游挂住了。"""
    uses_model = True

    def __init__(self, delay: float):
        self.delay = delay

    async def ask(self, packet, task):
        await asyncio.sleep(self.delay)
        return {"speech": "（本不该返回）"}

    def last_meta(self):
        return {"model": "fake-model"}


async def run(agent, call_timeout: float, decision_timeout: float) -> tuple[float, dict]:
    game = Game(board=make_board("eleven_standard"), agent_factory=lambda seat: agent,
                seed=2, max_days=1, call_timeout=call_timeout,
                decision_timeout=decision_timeout)
    game.on_event = lambda *a, **k: None
    task = Task(kind="speech", text="说句话", schema="{}", legal=[])
    started = time.time()
    reply = await game.act(game.seats[0], task)
    return time.time() - started, reply


async def main() -> int:
    print("=== 一、真人座位：call_timeout=0.5，但真人想了 1.6 秒 ===")
    dt, reply = await run(SlowHuman(1.6), call_timeout=0.5, decision_timeout=0.5)
    check("1a 真人没有被砍（跑满 1.6 秒）", dt >= 1.5, f"实际 {dt:.2f}s")
    check("1b 真人的回复是正常的（没被当成失败）",
          reply.get("speech") == "我想好了。" and not reply.get("_error"), str(reply)[:80])

    print("\n=== 二、模型座位：同样的上限必须砍 ===")
    dt2, reply2 = await run(SlowModel(1.6), call_timeout=0.5, decision_timeout=0.5)
    check("2a 模型被砍在 0.5 秒附近", dt2 < 1.2, f"实际 {dt2:.2f}s")
    check("2b 记下了失败原因", "单次调用超过" in str(reply2.get("_error")), str(reply2)[:80])

    print("\n=== 三、关掉上限（0）时两边都不砍 ===")
    dt3, reply3 = await run(SlowHuman(1.2), call_timeout=0, decision_timeout=0)
    check("3a 真人照常跑完", dt3 >= 1.1 and not reply3.get("_error"), f"{dt3:.2f}s")
    dt4, reply4 = await run(SlowModel(1.2), call_timeout=0, decision_timeout=0)
    check("3b 模型也照常跑完（上限=0 表示不限制）",
          dt4 >= 1.1 and not reply4.get("_error"), f"{dt4:.2f}s")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
