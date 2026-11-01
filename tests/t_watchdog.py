# coding=utf-8
"""「放弃本局」的硬复位兜底：引擎不肯退出时，用户也必须能回到主页面。

这是 server.py（代理 C 的文件）里那条历史坑的兜底路径：
`放弃` 之后 running 一直是 True、手机永远卡在"对局进行中，等别人发言…"，只能重启服务。
跑法： python t_watchdog.py
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

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


def make_room() -> S.Room:
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8")) or {}
    cfg.setdefault("game", {})
    cfg["game"]["save_dir"] = os.path.join(HERE, "tmp", "games")
    cfg["game"]["experience"] = False
    return S.Room(cfg, mock_default=True)


async def main() -> int:
    os.makedirs(os.path.join(HERE, "tmp", "games"), exist_ok=True)
    room = make_room()
    events: list[dict] = []
    orig = room.broadcast

    def spy(p):
        events.append(dict(p))
        return orig(p)

    room.broadcast = spy                                  # type: ignore[method-assign]

    r = await room.start(mock=True, human_seat=1, seed=21)
    check("W1 开局成功", bool(r.get("ok")), str(r))

    # 一个"怎么掐都不肯退出"的引擎：**只吞掉第一次 cancel**，之后照常退出
    # （真吞到底的话，测试自己结束时 asyncio 也会挂住 —— 我第一次就是这么把自己挂死的）
    async def stubborn():
        first = True
        while True:
            try:
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                if not first:
                    raise
                first = False
                continue

    room.game.run = stubborn                               # type: ignore[assignment]
    await asyncio.sleep(0.2)

    res = await room.abandon()
    check("W2 放弃本局返回 ok", bool(res.get("ok")), str(res))
    await asyncio.sleep(0.5)
    check("W3 放弃瞬间还在收尾/运行中（不是立刻就复位）", room.running is True or room.cleaning is True,
          f"running={room.running} cleaning={room.cleaning}")

    t0 = time.time()
    while time.time() - t0 < 8:
        if not room.running and room.game is None:
            break
        await asyncio.sleep(0.1)
    dt = time.time() - t0
    check("W4 3 秒兜底之后状态被强行清干净（用户能回到主页面）",
          room.running is False and room.game is None and room.awaiting is None,
          f"用时 {dt:.1f}s running={room.running} game={'有' if room.game else 'None'}")
    check("W5 手机收到过'已强制复位'的提示",
          any("强制复位" in str(e.get("text")) for e in events),
          str([e.get("text") for e in events if e.get("type") == "notice"])[:200])

    r2 = await room.start(mock=True, human_seat=1, seed=22)
    check("W6 复位之后还能正常开新局", bool(r2.get("ok")), str(r2))
    if r2.get("ok"):
        room.game.abort()
        room.task.cancel()
        await asyncio.sleep(0.4)

    room2 = make_room()
    res = await room2.abandon()
    check("W7 没有对局时点'放弃'→ 明确提示，而不是默默成功",
          not res.get("ok") and "没有正在进行" in str(res.get("error")), str(res))

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
