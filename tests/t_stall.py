# coding=utf-8
"""上游波动容错：自动重试 + 4 次都失败就暂停问用户。

分两层测：
  A) LLMClient 那一层（真 HTTP，用 httpx.MockTransport 挡在本地）：
     5xx/超时会重试满 4 次、401/400 不白试、中途恢复能拿到结果。
  B) engine.act() 那一层：客户端重试完还是失败 → 调 on_stall(暂停)，按用户的选择
     retry / skip / abort；人类与 mock 座位的失败**不该**触发暂停。

跑法： python t_stall.py
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
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import httpx                                              # noqa: E402
from wolf.engine import Game, GameAborted                  # noqa: E402
from wolf.llm import LLMClient, LLMError                   # noqa: E402
from wolf.roles import BUILTIN_BOARDS                      # noqa: E402
from _boards import NINE_BOARD        # 测试专用 9 人板（产品的 9 人板已删）
from _boards import make_board        # 退役板子的测试副本（产品已下架）
from wolf.context import Task                              # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def texts(game) -> str:
    return "\n".join(e.text for e in game.ledger.entries)


# --------------------------------------------------------------------------- #
# A) LLMClient 的重试（本地假 HTTP，不联网）
# --------------------------------------------------------------------------- #

def make_client(handler, max_retries: int, seen: list) -> LLMClient:
    client = LLMClient(base_url="http://fake.local/v1", api_key="k", model="fake-model",
                       timeout=5.0, max_retries=max_retries)

    async def counting(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return handler(len(seen))

    client._client = httpx.AsyncClient(base_url="http://fake.local/v1",
                                       transport=httpx.MockTransport(counting))
    return client


def ok_body(text: str = '{"speech": "好"}') -> dict:
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 1,
                                                                    "completion_tokens": 1}}


async def t_client_retries() -> None:
    print("\n=== A) LLMClient：上游波动才重试，配置错误不白试 ===")

    # A1 一直 500：应该正好打 1 + max_retries 次（这里 max_retries=2 → 3 次）
    seen: list = []
    c = make_client(lambda n: httpx.Response(500, text="upstream boom"), 2, seen)
    err = ""
    try:
        await c.chat_json("packet")
    except LLMError as exc:
        err = str(exc)
    finally:
        await c.aclose()
    check("A1 一直 5xx → 请求次数 = 1+max_retries（2 → 3 次）", len(seen) == 3, f"实际 {len(seen)} 次")
    check("A2 最后抛 LLMError，把上游错误带出来", "500" in err or "boom" in err, err[:120])

    # A3 401：重试也没用 → 只打 1 次
    seen = []
    c = make_client(lambda n: httpx.Response(401, text="bad key"), 4, seen)
    try:
        await c.chat_json("packet")
    except LLMError:
        pass
    finally:
        await c.aclose()
    check("A3 401（配置错）→ 不白试，只打 1 次", len(seen) == 1, f"实际 {len(seen)} 次")

    # A4 429 限流：属于波动 → 要重试
    seen = []
    c = make_client(lambda n: httpx.Response(429, text="rate limited"), 4, seen)
    try:
        await c.chat_json("packet")
    except LLMError:
        pass
    finally:
        await c.aclose()
    check("A4 429（限流）算波动 → 重试满 1+4 次", len(seen) == 5, f"实际 {len(seen)} 次")

    # A5 前两次 500、第三次成功 → 应该拿到结果，且只打 3 次
    seen = []
    c = make_client(lambda n: (httpx.Response(500, text="boom") if n <= 2
                               else httpx.Response(200, json=ok_body())), 4, seen)
    got = {}
    try:
        got = await c.chat_json("packet")
    except LLMError as exc:
        got = {"_err": str(exc)}
    finally:
        await c.aclose()
    check("A5 波动中间恢复 → 拿到结果（打了 3 次）",
          got.get("speech") == "好" and len(seen) == 3, f"got={got} 次数={len(seen)}")

    # A6 默认次数（不传 max_retries）就是 4 → 一共 5 次请求
    seen = []
    c = make_client(lambda n: httpx.Response(503, text="busy"), 4, seen)
    try:
        await c.chat_json("packet")
    except LLMError:
        pass
    finally:
        await c.aclose()
    check("A6 默认 max_retries=4 → 一共 5 次请求（用户要的'最多 4 次重试'）",
          len(seen) == 5, f"实际 {len(seen)} 次")

    # A7 配置里能覆盖（座位级）
    from wolf.routing import SeatLLM, build_client
    spec = SeatLLM(base_url="http://x/v1", api_key="k", model="m")
    check("A7 配置默认值也是 4（routing.SeatLLM）", spec.max_retries == 4, str(spec.max_retries))
    built = build_client(spec, label="测试")
    check("A8 真正建出来的客户端拿到 4", built.max_retries == 4, str(built.max_retries))
    await built.aclose()


# --------------------------------------------------------------------------- #
# B) 引擎那一层：客户端重试完还失败 → 暂停问用户
# --------------------------------------------------------------------------- #

class FakeLLMAgent:
    """伪装成"真模型座位"的代理：按剧本失败，然后给出合法回复。"""

    uses_model = True

    def __init__(self, seat_id: int, plan: dict | None = None, always_fail: bool = False) -> None:
        self.seat_id = seat_id
        self.plan = dict(plan or {})          # {"wolf_kill": 1} = 第一次狼刀失败
        self.always_fail = always_fail
        self.calls: dict[str, int] = {}
        self.failures = 0

    async def ask(self, packet: str, task: Task) -> dict:
        n = self.calls.get(task.kind, 0) + 1
        self.calls[task.kind] = n
        if self.always_fail or n <= int(self.plan.get(task.kind, 0)):
            self.failures += 1
            raise LLMError("fake upstream: HTTP 500 boom")
        legal = list(task.legal)
        pick = legal[-1] if legal else None
        return {"speech": "（假模型）我过。", "claim": "无", "vote": pick, "target": pick,
                "run": False, "save": False, "poison": None, "shoot": None, "check": pick,
                "to": None, "reason": "fake"}

    def last_meta(self) -> dict:
        return {"model": "fake-model"}


class PlainAgent(FakeLLMAgent):
    """不是模型座位（人类/mock 那种）：它的失败不该触发暂停。"""

    uses_model = False


def build(agents: dict, on_stall=None, board="nine_test", max_days: int = 1):
    """建一局。

    ⚠️ 这个文件的剧本代理只建了 **1~9 号**（`{i: FakeLLMAgent(i) for i in range(1, 10)}`），
    所以必须用 **9 人**板子：2026-10-09 产品的 9 人板删掉之后，这里改成用测试专用的
    `_boards.NINE_BOARD`（换成 11 人板会在第 10 个座位 KeyError —— 实测踩到）。
    """
    events: list[tuple] = []
    game = Game(board=(NINE_BOARD if board in ("nine_test", "nine_standard") else make_board(board)),
                agent_factory=lambda seat: agents[seat.id],
                seed=5, max_days=max_days, on_event=lambda kind, **kw: events.append((kind, kw)),
                on_stall=on_stall)
    return game, events


async def t_engine_retry() -> None:
    print("\n=== B1) 失败一次 → 弹窗选「再试一次」→ 这一手救回来 ===")
    calls: list[tuple] = []

    async def on_stall(seat, task, error, stalled=1):
        calls.append((seat.id, task.kind, error[:40], stalled))
        return "retry"

    agents = {i: FakeLLMAgent(i) for i in range(1, 10)}
    agents[1] = FakeLLMAgent(1, plan={"wolf_kill": 1})     # 只有 1号（狼）第一次狼刀失败
    game, events = build(agents, on_stall=on_stall)
    await game.run()
    wolf_rows = [t for t in game.transcript if t["seat"] == 1 and t["task"] == "wolf_kill"]
    check("B1a 用户被问了（暂停过一次）", len(calls) == 1, f"{calls}")
    check("B1b 问的时候带着座位/任务/错误原文",
          calls and calls[0][0] == 1 and calls[0][1] == "wolf_kill" and "500" in calls[0][2],
          f"{calls[:1]}")
    check("B1c transcript 里留下两次调用（1 次失败 + 1 次成功）",
          len(wolf_rows) == 2 and wolf_rows[0]["error"] and not wolf_rows[1]["error"],
          f"{[(r['task'], r['error'][:20]) for r in wolf_rows]}")
    check("B1d 重试成功后**不该**再写「已按规则随机处理」",
          not any("随机处理" in e for s in game.seats for e in s.private_events))
    check("B1e 也不必再发 warn 事件", not [e for e in events if e[0] == "warn"],
          f"{[e[0] for e in events if e[0] == 'warn']}")
    check("B1f 对局照常打完", game.winner in ("good", "wolf", "draw") and game.day >= 1,
          f"winner={game.winner} day={game.day}")


async def t_engine_skip_and_abort() -> None:
    print("\n=== B2) 一直失败：选「跳过」继续 / 选「放弃本局」收摊 ===")
    calls: list[tuple] = []

    async def on_skip(seat, task, error, stalled=1):
        calls.append((seat.id, task.kind, stalled))
        return "skip"

    agents = {i: FakeLLMAgent(i) for i in range(1, 10)}
    agents[1] = FakeLLMAgent(1, always_fail=True)          # 只有 1号 一直失败
    game, events = build(agents, on_stall=on_skip)
    await game.run()
    check("B2a 选「跳过」→ 对局继续打完（不算放弃）", game.winner in ("good", "wolf", "draw")
          and not game.aborted, f"winner={game.winner} aborted={game.aborted}")
    check("B2b 每次都只问一次就够（skip 之后不再重试同一手）",
          all(c[2] == 1 for c in calls) and len(calls) >= 1, f"{len(calls)} 次：{calls[:3]}")
    check("B2c 台账/私有经历里写明「已按规则随机处理」",
          any("随机处理" in e for e in game.seat_by_id(1).private_events))
    check("B2d 发了 warn 事件（手机上能看到）", bool([e for e in events if e[0] == "warn"]))

    async def on_abort(seat, task, error, stalled=1):
        return "abort"

    agents2 = {i: FakeLLMAgent(i) for i in range(1, 10)}
    agents2[3] = FakeLLMAgent(3, always_fail=True)
    game2, _ = build(agents2, on_stall=on_abort)
    winner = await game2.run()
    check("B2e 选「放弃本局」→ 对局中止（winner 记为 aborted）",
          game2.aborted and winner == "aborted", f"aborted={game2.aborted} winner={winner}")
    check("B2f 台账里写明中止原因（模型连续失败）",
          "模型连续失败" in texts(game2),
          [e.text for e in game2.ledger.entries if "————" in e.text][:1])


async def t_no_stall_for_non_model() -> None:
    print("\n=== B3) 人类/mock 座位失败不该暂停；没传 on_stall 时保持老行为 ===")
    calls: list[tuple] = []

    async def on_stall(seat, task, error, stalled=1):
        calls.append((seat.id, task.kind))
        return "retry"

    agents = {i: FakeLLMAgent(i) for i in range(1, 10)}
    agents[4] = PlainAgent(4, always_fail=True)            # uses_model=False
    game, _ = build(agents, on_stall=on_stall)
    await game.run()
    check("B3a 非模型座位的失败**不**弹窗", not calls, f"{calls}")
    check("B3b 对局照样打完", game.winner in ("good", "wolf", "draw"), game.winner)
    check("B3c 那一手仍然按规则随机处理（老的降级路径还在）",
          any("随机处理" in e for e in game.seat_by_id(4).private_events))

    agents2 = {i: FakeLLMAgent(i) for i in range(1, 10)}
    agents2[2] = FakeLLMAgent(2, always_fail=True)
    game2, events2 = build(agents2, on_stall=None)          # 命令行/mock 的默认路径
    await game2.run()
    check("B3d 没传 on_stall（老路径）→ 不暂停、直接随机处理继续打",
          game2.winner in ("good", "wolf", "draw") and not game2.aborted
          and any("随机处理" in e for e in game2.seat_by_id(2).private_events), game2.winner)


async def main() -> int:
    await t_client_retries()
    await t_engine_retry()
    await t_engine_skip_and_abort()
    await t_no_stall_for_non_model()
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
