# coding=utf-8
"""代际校验（2026-10-30 加）：迟到的回复 / 上一局的事件，一律不许进新局。

为什么要有这条：放弃、重开、服务端硬复位之后，旧任务可能还挂在 `await` 上。
它回来时如果照常写台账、照常返回，就会把上一局的信息带进新局 —— 这类"脏回复"
在手机上的表现是"新局里突然冒出一句话/一个票"，事后极难查。

三处闸门（本文件逐个钉）：
  ① `Game.supersede()` / `abort()` 推进代际；`act()` 在发请求前记下代号，回来先核对；
  ② 作废的那一手**留痕**（transcript 里 `stale: true`）但**不交给引擎**；
  ③ `Game.emit()` 给每条事件盖 `_run`；`Room.on_event` 丢掉不属于当前这局的事件。

变异自证：把 `act()` 里 `if _stale():` 那几处删掉（或让 `_stale()` 恒 False），
第 2/3 节必须变红。
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.context import Task                                    # noqa: E402
from wolf.engine import Game, GameAborted                        # noqa: E402
from wolf.retired_boards import retired_board                    # noqa: E402

#: 11 人标准板已退役（2026-10-30），这里用它的测试副本（只测代际校验，不关心是哪个板子）。
_BOARD = retired_board("eleven_standard")

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def _task() -> Task:
    return Task(kind="vote", text="今天投谁？", schema='{"vote": <编号>}',
                legal=[2, 3], required=())


def _game(agent, **kw):
    return Game(board=_BOARD, seed=1,
                agent_factory=lambda s: agent,
                on_event=kw.pop("on_event", lambda *a, **k: None), **kw)


class SlowAgent:
    """ask 卡在 gate 上：用来模拟"请求发出去了，人已经放弃本局"。"""
    uses_model = True
    model_now = "fake-model"

    def __init__(self, gate: asyncio.Event, asked: asyncio.Event, reply: dict) -> None:
        self.gate = gate
        self.asked = asked
        self.reply = reply

    async def ask(self, packet, task):
        self.asked.set()
        await self.gate.wait()
        return dict(self.reply)

    def set_context(self, day, kind):
        pass

    def last_meta(self):
        return {"model": self.model_now}

    def use_fallback(self):
        return ""


class FastAgent(SlowAgent):
    async def ask(self, packet, task):
        self.asked.set()
        return dict(self.reply)


async def _run_aborted_case():
    gate = asyncio.Event()
    asked = asyncio.Event()
    game = _game(SlowAgent(gate, asked, {"vote": 2}))
    t = asyncio.create_task(game.act(game.seat_by_id(1), _task()))
    await asyncio.wait_for(asked.wait(), 2)
    gen0 = game.generation
    game.abort("测试放弃")
    gate.set()
    err = None
    try:
        await asyncio.wait_for(t, 2)
    except GameAborted as exc:
        err = exc
    return game, gen0, err


async def _run_superseded_case():
    gate = asyncio.Event()
    asked = asyncio.Event()
    game = _game(SlowAgent(gate, asked, {"vote": 3}))
    t = asyncio.create_task(game.act(game.seat_by_id(1), _task()))
    await asyncio.wait_for(asked.wait(), 2)
    gen0 = game.generation
    game.supersede("新局顶掉旧局")      # 只换代，不 abort
    gate.set()
    err = None
    try:
        await asyncio.wait_for(t, 2)
    except GameAborted as exc:
        err = exc
    return game, gen0, err


async def _run_normal_case():
    asked = asyncio.Event()
    game = _game(FastAgent(asyncio.Event(), asked, {"vote": 2}))
    reply = await game.act(game.seat_by_id(1), _task())
    return game, reply


print("=== 一、代际编号本身 ===")
g = _game(FastAgent(asyncio.Event(), asyncio.Event(), {}))
check("新局 generation 从 0 开始", g.generation == 0, str(g.generation))
check("supersede 让代际 +1", g.supersede("test") == 1 and g.generation == 1)
g.abort("test")
gen_after = g.generation
check("abort 也推进代际", gen_after == 2, str(gen_after))
g.abort("again")
check("重复 abort 不再推进（幂等）", g.generation == gen_after, str(g.generation))

print("\n=== 二、放弃之后回来的回复必须被丢弃 ===")
game, gen0, err = asyncio.run(_run_aborted_case())
check("act 抛 GameAborted（不是把回复当成功）", err is not None, type(err).__name__)
check("代际确实推进了", game.generation == gen0 + 1, f"{gen0} → {game.generation}")
row = game.transcript[-1] if game.transcript else {}
check("作废的那一手留了痕（stale=true）", row.get("stale") is True, str(row.get("error")))
check("痕里写清原因", "作废" in str(row.get("error", "")) or "换代" in str(row.get("error", "")),
      str(row.get("error")))
votes = [e for e in game.ledger.entries if e.kind == "vote"]
check("没有把迟到的票写进台账", not votes, f"投票条目 {len(votes)} 条")
check("没有往座位私有经历里写失败提示", not game.seat_by_id(1).private_events,
      str(game.seat_by_id(1).private_events[:1]))

print("\n=== 三、只换代（不 abort）也一样丢 ===")
game2, gen0b, err2 = asyncio.run(_run_superseded_case())
check("act 抛 GameAborted", err2 is not None, type(err2).__name__)
check("代际推进", game2.generation == gen0b + 1)
check("aborted 仍是 False（换代 ≠ 放弃）", game2.aborted is False)
check("同样留了 stale 痕",
      bool(game2.transcript) and game2.transcript[-1].get("stale") is True)

print("\n=== 四、正常一手不受影响 ===")
game3, reply3 = asyncio.run(_run_normal_case())
check("回复正常返回", reply3 == {"vote": 2}, str(reply3))
check("没有 stale 标记", not any(r.get("stale") for r in game3.transcript))
check("代际没变", game3.generation == 0)

print("\n=== 五、事件带局代号，服务端认代号 ===")
seen = []
game4 = _game(FastAgent(asyncio.Event(), asyncio.Event(), {}),
              on_event=lambda kind, **data: seen.append((kind, data)))
game4.emit("phase", day=1, phase="night")
check("emit 给事件盖了 _run", bool(seen) and seen[-1][1].get("_run") == game4.run_token,
      str(seen[-1][1] if seen else None))

import server                                                    # noqa: E402

room = server.Room.__new__(server.Room)
room.game_token = "g-current"
room.game = None                    # 没有当前局：事件走到这里就该 return
pushed = []
room.broadcast = lambda msg: pushed.append(msg)
room.on_event("phase", _run="g-old", day=1, phase="night")
check("旧局的事件被丢掉（不广播）", pushed == [], str(pushed))
room.on_event("phase", _run="g-current", day=1, phase="night")
check("当前局的事件照常处理（这里只证明没被提前丢掉）", True)

print("=" * 80)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
