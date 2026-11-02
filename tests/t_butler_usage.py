# coding=utf-8
"""托管用量回填（2026-11-03）：让存档里那一手看得出"是托管打的、花了多少"。

以前人类座位在 transcript 里恒为 `model='' / prompt_tokens=0`，
所以复盘看不出托管成本。现在：
  · `WebHumanAgent._last_meta` 由 server.autopilot 在 resolve future **之前**写入；
  · `engine.act()` 照常读 `agent.last_meta()`，于是这一行带上 model / token / elapsed；
  · 行里多一个 `butler: true` 标记（复盘/统计能区分真人手打与托管）。

跑法：python tests/t_butler_usage.py
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import BaseAgent, WebHumanAgent                    # noqa: E402
from wolf.context import Task                                       # noqa: E402
from wolf.engine import Game                                        # noqa: E402
from wolf.roles import make_board                                   # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


class ButlerLike(BaseAgent):
    """假人类代理：ask 时先写 _last_meta（模拟 server.autopilot 的回填）。"""

    uses_model = False

    def __init__(self, seat_id: int) -> None:
        self.seat_id = seat_id
        self._last_meta: dict = {}

    def last_meta(self) -> dict:
        return dict(self._last_meta or {})

    async def ask(self, packet, task):
        self._last_meta = {"model": "butler-model", "prompt_tokens": 1234,
                           "completion_tokens": 56, "cached_tokens": 7,
                           "elapsed": 9.5, "butler": True, "thinking": {"low": True}}
        return {"speech": "这是托管写的一段发言。"}


print("=== 一、代理接口 ===")
check("1a BaseAgent 默认 last_meta 是空 dict", BaseAgent.last_meta(BaseAgent()) == {})
class _Room:
    human_timeout = 5
h = WebHumanAgent(1, _Room())
h._last_meta = {"model": "m"}
check("1b WebHumanAgent.last_meta 回得出、且是拷贝", h.last_meta() == {"model": "m"}
      and h.last_meta() is not h._last_meta)

print("\n=== 二、引擎把用量写进 transcript ===")
g = Game(board=make_board("six_all_reveal"), human_seat=1, max_days=1,
         agent_factory=lambda seat: ButlerLike(seat.id),
         on_event=lambda *a, **k: None)
asyncio.run(g.act(g.seat_by_id(1), Task(kind="speech", text="轮到你发言", schema="{}")))
row = g.transcript[-1]
check("2a 模型名记下来了", row.get("model") == "butler-model", str(row.get("model")))
check("2b token 记下来了", row.get("prompt_tokens") == 1234 and row.get("completion_tokens") == 56,
      f"{row.get('prompt_tokens')}/{row.get('completion_tokens')}")
check("2c 缓存命中记下来了", row.get("cached_tokens") == 7, str(row.get("cached_tokens")))
check("2d 耗时记下来了", row.get("elapsed", 0) >= 9.5, str(row.get("elapsed")))
check("2e 带 butler 标记（复盘能区分真人/托管）", row.get("butler") is True, str(row.get("butler")))

print("\n=== 三、普通真人（没托管）不会被误标 ===")
class PlainHuman(BaseAgent):
    def __init__(self, seat_id):
        self.seat_id = seat_id

    async def ask(self, packet, task):
        return {"speech": "手打的一句"}

g2 = Game(board=make_board("six_all_reveal"), human_seat=1, max_days=1,
          agent_factory=lambda seat: PlainHuman(seat.id),
          on_event=lambda *a, **k: None)
asyncio.run(g2.act(g2.seat_by_id(1), Task(kind="speech", text="t", schema="{}")))
row2 = g2.transcript[-1]
check("3a 手打的一手 butler=False、model 为空",
      row2.get("butler") is False and row2.get("model") == "",
      f"butler={row2.get('butler')} model={row2.get('model')!r}")

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)