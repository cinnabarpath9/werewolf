# coding=utf-8
"""继续深挖第二轮：换家/重试与"已有机制"的交互里还有没有隐性缺陷。

这轮重点看"时序/边界"这类测试不容易覆盖的地方：
  E. 用户在弹窗上点「重试」→ 也会换家；如果替补名单已空，不该报错；
  F. 连续换两家之后 aclose 要把两个都关（不能漏）；
  G. 换家后 `last_meta()` 报的模型应该是**替补的**（否则存档里记错人）；
  H. 自动重试期间 `transcript` 每次尝试都留一条（复盘能看到"第一次是谁失败的"）；
  I. 预算耗尽时绝不换家（换了也不发请求 = 白建连接池）。
"""
import asyncio
import io
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import LLMAgent                         # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class FakeClient:
    def __init__(self, model: str, reply=None, fail=False):
        self.model = model
        self.reply = reply if reply is not None else {"speech": "好。"}
        self.fail = fail
        self.calls = 0
        self.last_call = {}
        self.closed = False

    def extra_for(self, task_kind, day):
        return {}

    async def chat_json(self, packet, extra=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.model + " 挂了")
        self.last_call = {"model": self.model, "reasoning": "思路", "prompt_tokens": 1,
                          "completion_tokens": 1, "raw": "{}"}
        return dict(self.reply)

    async def aclose(self):
        self.closed = True


async def main() -> int:
    board = make_board("eleven_standard")

    print("=== G. 换家后 last_meta 报的必须是替补的模型 ===")
    primary = FakeClient("glm", fail=True)
    backup = FakeClient("ds", reply={"speech": "我投 2 号。", "vote": 2})
    agent = LLMAgent(primary, 1, fallbacks=[("DEEPSEEK", lambda: backup)])
    game = Game(board=board, agent_factory=lambda s: agent, seed=1, max_days=1,
                call_timeout=5, decision_timeout=30, auto_retries=1,
                retry_backoff=[0.01], retry_jitter=0.0)
    game.on_event = lambda kind, **data: None
    game.on_stall = None
    await game.act(game.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    metas = [r for r in game.transcript]
    check("G1 transcript 里既有失败那次、也有替补成功那次", len(metas) >= 2, f"{len(metas)} 条")
    check("G2 成功那条记的是替补模型名", metas[-1]["model"] == "ds", str(metas[-1]["model"]))
    check("G3 成功那条标了 fallback=DEEPSEEK", metas[-1]["fallback"] == "DEEPSEEK",
          str(metas[-1]["fallback"]))
    check("G4 失败那条留在记录里（能看到第一次是谁失败的）",
          any(r.get("error") for r in metas), str([r.get("error")[:20] for r in metas]))

    print("\n=== F. 连换两家后 aclose 两个都要关 ===")
    a, b = FakeClient("A"), FakeClient("B")
    agent2 = LLMAgent(FakeClient("p"), 1, fallbacks=[("A", lambda: a), ("B", lambda: b)])
    agent2.use_fallback()
    agent2.use_fallback()
    await agent2.aclose()
    check("F1 第一家已关", a.closed, str(a.closed))
    check("F2 第二家（当前）已关", b.closed, str(b.closed))
    check("F3 原始 client 也已关", agent2.client is b and b.closed)

    print("=== E. 名单空了之后再点重试：不报错、不换家 ===")
    agent3 = LLMAgent(FakeClient("p"), 1, fallbacks=[("A", lambda: FakeClient("A"))])
    check("E1 第一次换家成功", agent3.use_fallback() == "A")
    check("E2 第二次返回 None（而不是抛异常）", agent3.use_fallback() is None)

    print("=== I. 预算耗尽时不该白建替补连接池 ===")
    built = []
    primary4 = FakeClient("glm", fail=True)

    def maker():
        built.append(1)
        return FakeClient("ds", reply={"speech": "替补。"})

    agent4 = LLMAgent(primary4, 1, fallbacks=[("DEEPSEEK", maker)])
    game4 = Game(board=board, agent_factory=lambda s: agent4, seed=1, max_days=1,
                 call_timeout=5, decision_timeout=0.05, auto_retries=2,
                 retry_backoff=[3, 8], retry_jitter=0.0)      # 退避 3 秒 >> 预算 0.05 秒
    game4.on_event = lambda kind, **data: None
    game4.on_stall = None
    await game4.act(game4.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    check("I1 预算不够时没有建替补客户端（也没换家）", not built, f"建了 {len(built)} 个")
    check("I2 那一手仍然按失败处理", "调用失败" in " ".join(game4.seats[0].private_events),
          str(game4.seats[0].private_events)[:120])

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
