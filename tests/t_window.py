# coding=utf-8
"""量一下"收尾窗口"到底有多长 —— 它决定 H2（收尾期间来 sync 会吞掉结局）的实际风险。

窗口 = 一局打完 → end 广播之间那一段（存档 + 逐个 aclose 客户端）。
跑法： python t_window.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import yaml                                             # noqa: E402
import server as S                                      # noqa: E402

GAMES = os.path.join(HERE, "tmp", "games")
os.makedirs(GAMES, exist_ok=True)


def room() -> S.Room:
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8")) or {}
    cfg.setdefault("game", {})
    cfg["game"]["save_dir"] = GAMES
    cfg["game"]["experience"] = False
    return S.Room(cfg, mock_default=True)


class Slow:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def aclose(self):
        await asyncio.sleep(self.delay)


async def measure(n_clients: int, delay: float) -> float:
    r = room()
    marks: dict[str, float] = {}
    orig = r.broadcast

    def spy(payload):
        if payload.get("type") == "end" and "end" not in marks:
            marks["end"] = time.time()
        return orig(payload)

    r.broadcast = spy                                  # type: ignore[method-assign]
    await r.start(mock=True, human_seat=1, seed=4)

    async def quick_run():
        marks["finished"] = time.time()
        await asyncio.sleep(0.02)

    r.game.run = quick_run                              # type: ignore[assignment]
    r.clients_llm = [Slow(delay) for _ in range(n_clients)]
    t0 = time.time()
    while time.time() - t0 < 60:
        if "end" in marks:
            break
        await asyncio.sleep(0.01)
    return marks.get("end", 0) - marks.get("finished", 0)


async def main() -> int:
    print(f"{'场景':<34}{'收尾窗口':<12}")
    for n, d, label in ((0, 0, "mock 局（没有模型客户端要关）"),
                        (9, 0.005, "真局 9 个客户端，秒关（正常情况）"),
                        (9, 1.0, "9 个客户端每个卡 1 秒"),
                        (9, 5.0, "9 个客户端每个卡满 5 秒上限")):
        dt = await measure(n, d)
        print(f"{label:<34}{dt*1000:>7.0f} ms")
    print("\n结论（2026-10-06 修好之后）：客户端是**并发**关的，所以卡死也只有一个 5 秒上限：")
    print("      9 个客户端全卡满 = 约 5 秒（修之前是每个 5 秒串行 = 45 秒）。")
    print("      另外 experience: true 时窗口里还要等每个模型写复盘笔记（几秒~几十秒）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
