# coding=utf-8
"""深挖：换家/重试这批新代码里可能藏着的缺陷（先复现，再修）。

怀疑清单（都是从代码读出来的，不是猜）：
  A. 退避抖动用了**游戏自己的 rng** —— 会扰动发牌/随机兜底的随机流；
  B. 换手记录在「替补也失败」时照样写 —— 会写出一条「由 X 代打」的假记录；
  C. `retry_backoff: 3`（写成标量）→ `list(3)` 直接 TypeError，**开局崩**；
  D. `fallback: DEEPSEEK`（忘了写列表）→ 按字符迭代 → 静默没有替补。
"""
import asyncio
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import routing                                 # noqa: E402
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
        self.last_call = {"model": self.model, "reasoning": "替补思路", "prompt_tokens": 1,
                          "completion_tokens": 1, "raw": "{}"}
        return dict(self.reply)

    async def aclose(self):
        self.closed = True


def dealt(game) -> list:
    return [s.role_key for s in game.seats]


async def main() -> int:
    board = make_board("eleven_standard")

    print("=== A. 退避抖动不该扰动游戏自己的随机流 ===")
    g1 = Game(board=board, agent_factory=None, seed=12345, max_days=1)
    g2 = Game(board=board, agent_factory=None, seed=12345, max_days=1)
    a1, a2 = g1.rng.random(), g1.rng.random()
    # g2 之间抽两次抖动（模拟"发生过重试"），再看它自己的随机流是否被偷走
    g2._retry_wait(1, 0.0)
    g2._retry_wait(2, 0.0)
    b1, b2 = g2.rng.random(), g2.rng.random()
    check("A1 抖动不吃游戏随机流（同种子抽签序列一致）",
          (a1, a2) == (b1, b2), f"{a1:.4f},{a2:.4f} vs {b1:.4f},{b2:.4f}")
    check("A2 引擎有独立的抖动随机源（_jitter_rng）", hasattr(g2, "_jitter_rng"))
    check("A3 抖动仍在配置区间内（第 1 次 3 秒档、第 2 次 8 秒档，各 ±30%）",
          2.1 <= g1._retry_wait(1, 0.0) <= 3.9 and 5.6 <= g1._retry_wait(2, 0.0) <= 10.4,
          str([round(g1._retry_wait(i, 0.0), 2) for i in (1, 2)]))

    print("\n=== B. 替补也失败时，不该写「由 X 代打」的假记录 ===")
    primary = FakeClient("glm", fail=True)
    backup = FakeClient("ds", fail=True)
    agent = LLMAgent(primary, 1, fallbacks=[("DEEPSEEK", backup)])
    game = Game(board=board, agent_factory=lambda s: agent, seed=1, max_days=1,
                call_timeout=5, decision_timeout=30, auto_retries=2,
                retry_backoff=[0.01], retry_jitter=0.0)
    game.on_event = lambda kind, **data: None
    game.on_stall = None
    await game.act(game.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    events = list(game.seats[0].private_events)
    notes = [x for x in events if "换手记录" in x]
    check("B1 替补也失败时，没有「代打成功」的假记录",
          not any("代打" in x for x in notes), str(notes)[:160])
    check("B2 仍然留了「这一手调用失败」的痕迹",
          any("调用失败" in x for x in events), str(events)[:160])

    print("\n=== C. retry_backoff 写成标量/字符串不该炸 ===")
    for value, want in ((3, [3.0]), ("3,8", [3.0, 8.0]), ([3, 8], [3.0, 8.0])):
        try:
            g = Game(board=board, agent_factory=None, seed=1, max_days=1, retry_backoff=value)
            got = g.retry_backoff
            check(f"C retry_backoff={value!r} 被规范化成 {want}", got == want, str(got))
        except Exception as exc:                          # noqa: BLE001
            check(f"C retry_backoff={value!r} 不该抛异常", False, f"{type(exc).__name__}: {exc}")

    print("\n=== D. fallback 写成字符串（忘写列表）时按一家处理 ===")
    g = Game(board=board, agent_factory=None, seed=1, max_days=1)
    try:
        specs = routing.fallback_specs({"llm": {}, "game": {}}, g.seats[0], "DEEPSEEK")
        names = [n for n, _ in specs]
        check("D1 字符串被当成一家、而不是被拆成字符", names in ([], ["DEEPSEEK"]), str(names))
    except Exception as exc:                              # noqa: BLE001
        check("D1 字符串不该抛异常", False, f"{type(exc).__name__}: {exc}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
