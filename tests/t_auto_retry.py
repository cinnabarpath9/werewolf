# coding=utf-8
"""回归：引擎层自动重试（用户 2026-10-07 口径：合计最多 3 次尝试，3s→8s 退避）。

要钉死的契约：
  1. 第一次失败后**自动**再试（不发弹窗），连续成功之前不打扰用户；
  2. 合计尝试次数 = auto_retries + 1（默认 3 次），之后才走弹窗链路；
  3. 退避按配置（测试里用 0.3 / 0.8 秒，别真等 3/8 秒），带抖动但在合理区间；
  4. 退避只花剩余预算：预算不够时不再重试；
  5. 真人/mock 座位不自动重试（他们的失败不是上游问题）。
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


class Flaky:
    """前 fail_times 次抛错，之后成功。"""
    uses_model = True

    def __init__(self, fail_times: int, err: str = "上游 500"):
        self.fail_times = fail_times
        self.err = err
        self.n = 0

    async def ask(self, packet, task):
        self.n += 1
        if self.n <= self.fail_times:
            raise RuntimeError(self.err)
        return {"speech": "我来了。", "vote": 4}

    def last_meta(self):
        return {"model": "flaky"}


class Human:
    """真人座位：ask 抛错时不该被自动重试（那是真人自己的问题）。"""
    uses_model = False

    def __init__(self):
        self.n = 0

    async def ask(self, packet, task):
        self.n += 1
        raise RuntimeError("真人这边断了")

    def last_meta(self):
        return {}


async def run(agent, auto_retries=2, backoff=(0.3, 0.8), decision_timeout=30, on_stall=None):
    game = Game(board=make_board("eleven_standard"), agent_factory=lambda seat: agent,
                seed=3, max_days=1, call_timeout=5, decision_timeout=decision_timeout,
                auto_retries=auto_retries, retry_backoff=list(backoff), retry_jitter=0.0)
    events: list = []
    game.on_event = lambda kind, **data: events.append((kind, data))
    popups: list = []

    async def stall(seat, task, err, n):
        popups.append(err)
        return (on_stall or (lambda *a: "skip"))(seat, task, err, n)

    game.on_stall = stall
    started = time.time()
    reply = await game.act(game.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    return time.time() - started, reply, game, events, popups


async def main() -> int:
    print("=== 一、失败 2 次后成功：3 次尝试、不弹窗 ===")
    agent = Flaky(2)
    dt, reply, game, events, popups = await run(agent)
    check("1a 一共尝试 3 次", agent.n == 3, f"{agent.n} 次")
    check("1b 最终成功拿到回复", reply.get("speech") == "我来了。", str(reply)[:60])
    check("1c 没有弹窗打扰用户", not popups, str(popups)[:60])
    retries = [d for k, d in events if k == "retry"]
    check("1d 发了 2 条 retry 事件（手机能看到）", len(retries) == 2, str(retries)[:120])
    check("1e 事件里带上了第几次/共几次/等待秒数",
          all(set(("attempt", "total", "wait", "why")) <= set(r) for r in retries),
          str(retries[:1])[:120])
    check("1f 退避按配置 0.3s → 0.8s",
          abs(retries[0]["wait"] - 0.3) < 0.01 and abs(retries[1]["wait"] - 0.8) < 0.01,
          f"{[r['wait'] for r in retries]}")
    check("1g 总耗时 ≈ 0.3+0.8 秒（退避真的等了）", dt >= 1.0, f"{dt:.2f}s")

    print("\n=== 二、一直失败：试满 3 次才弹窗 ===")
    agent2 = Flaky(99)
    _, reply2, _, _, popups2 = await run(agent2)
    check("2a 尝试 3 次后停下", agent2.n == 3, f"{agent2.n} 次")
    check("2b 弹窗只出现 1 次（自动重试用完才问人）", len(popups2) == 1, f"{len(popups2)} 次")
    check("2c 仍然保留了失败原因", "_error" in reply2 and "上游 500" in str(reply2["_error"]),
          str(reply2.get("_error"))[:60])

    print("\n=== 三、auto_retries=0 时退回「第一次就弹窗」 ===")
    agent3 = Flaky(99)
    _, _, _, _, popups3 = await run(agent3, auto_retries=0)
    check("3a 只尝试 1 次", agent3.n == 1, f"{agent3.n} 次")
    check("3b 立刻弹窗", len(popups3) == 1, str(len(popups3)))

    print("\n=== 四、预算不够时不重试（不硬等退避）===")
    agent4 = Flaky(99)
    # 预算 0.4 秒，第一次请求本身几乎不耗时（抛错）→ 退避 0.3s 能用一次，0.8s 就用不完了
    dt4, _, _, events4, popups4 = await run(agent4, backoff=(0.3, 0.8), decision_timeout=0.4)
    retries4 = [d for k, d in events4 if k == "retry"]
    check("4a 没有把 0.8 秒的退避等完（预算 0.4 秒）", dt4 < 0.75, f"{dt4:.2f}s")
    check("4b 尝试次数被预算拦住（≤ 3 次）", agent4.n <= 3, f"{agent4.n} 次")
    check("4c 预算不够时发了 warn 或直接弹窗",
          any(k == "warn" for k, _ in events4) or popups4, f"retry={len(retries4)} warn="
          f"{sum(1 for k, _ in events4 if k == 'warn')} popup={len(popups4)}")

    print("\n=== 五、真人座位不自动重试 ===")
    agent5 = Human()
    _, _, game5, events5, popups5 = await run(agent5)
    check("5a 真人只试 1 次（不被自动重试放大）", agent5.n == 1, f"{agent5.n} 次")
    check("5b 没有 retry 事件", not [1 for k, _ in events5 if k == "retry"])
    check("5c 真人失败不走「上游弹窗」（弹窗只给模型失败用）", not popups5, str(popups5))
    check("5d 但会留一条系统提示在私有经历里（让本人知道那一手不是自己选的）",
          any("系统提示" in x for x in game5.seats[0].private_events),
          str(game5.seats[0].private_events)[:100])

    print("\n=== 六、客户端遇到挂住（读超时）：流式重试一次、非流式不硬撞 ===")
    await check_no_internal_retry_on_hang()
    return await summary()


async def check_no_internal_retry_on_hang() -> None:
    """客户端（llm.py）遇到「挂住」时怎么办 —— **两条路两种口径**（2026-10-10 晚定的）。

    ⚠️ 这一节以前只钉了"读超时**不**在客户端重试"（那是对**非流式**老路说的）。
    2026-10-10 晚请求改成**默认流式**之后，口径变成了：
      · **流式**：读超时就是我们自己的"**静默到点**"（`read=idle_timeout`，默认 50 秒没吐字节）⇒
        当成"上游抖了"，**在客户端重试**（客服口径：第二次通常就通）；
      · **非流式**：真正的读超时仍然**不在客户端硬撞**（同一家再撞还是挂），
        带一条 `hangs` 计数抛出去，交给**引擎层**换家/退避。
    旧断言只留了后半句，所以**流式改造之后这条一直是红的**（`--fast` 不跑它、`--quick` 才跑到）。
    现在两条路各钉一条 —— 顺带说明：流式那条**必须**重试，否则 11 号卡满 420 秒的坑会回来。
    """
    import httpx                                         # noqa: PLC0415
    from wolf.llm import LLMClient                       # noqa: PLC0415

    class FakeClientBase:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            pass

    # ① 流式：静默到点 → 允许客户端重试（max_retries=1 ⇒ 最多 2 次请求）
    class FakeStreamClient(FakeClientBase):
        def stream(self, *a, **k):
            self.calls += 1

            class _Ctx:
                async def __aenter__(self):
                    raise httpx.ReadTimeout("上游一个字节都没回")

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    c1 = LLMClient(base_url="http://127.0.0.1:1/v1", api_key="x", model="fake",
                   timeout=1.0, max_retries=1, stream=True)
    fc1 = FakeStreamClient()
    c1._client = fc1
    try:
        await c1.chat_json("（测试包）", {})
    except Exception as exc:                             # noqa: BLE001
        # 只有"上游读写失败"是预期的；AttributeError/TypeError 说明测试自己写错了，要炸出来
        if isinstance(exc, (AttributeError, TypeError)):
            raise
    check("6a 流式（默认）下静默到点会重试一次（1 次原始 + 1 次重试）",
          fc1.calls == 2, f"{fc1.calls} 次；usage={c1.usage}")
    check("6b 重试之后仍然记了一条 hangs 计数（诊断能看到）",
          c1.usage.get("hangs") == 1, str(c1.usage))

    # ② 非流式（老路）：真正的读超时**不在客户端硬撞**，直接交给引擎
    class FakePostClient(FakeClientBase):
        async def post(self, *a, **k):
            self.calls += 1
            raise httpx.ReadTimeout("上游一个字节都没回")

    c2 = LLMClient(base_url="http://127.0.0.1:1/v1", api_key="x", model="fake",
                   timeout=1.0, max_retries=4, stream=False)
    fc2 = FakePostClient()
    c2._client = fc2
    try:
        await c2.chat_json("（测试包）", {})
    except Exception as exc:                             # noqa: BLE001
        if isinstance(exc, (AttributeError, TypeError)):
            raise
    check("6c 非流式下读超时只发 1 次请求（不在客户端里硬撞，交给引擎换家）",
          fc2.calls == 1, f"{fc2.calls} 次")
    check("6d 非流式那一次也记了 hangs 计数", c2.usage.get("hangs") == 1, str(c2.usage))


async def summary() -> int:
    print("\n" + "=" * 74)
    bad = [x for x in RESULTS if not x[1]]
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n_, _, d in bad:
        print(f"  [FAIL] {n_}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
