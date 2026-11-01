# coding=utf-8
"""回归：多个座位同时卡住时，**一次点击要释放全部等待者**（引擎审查的 P2-2）。

背景：引擎在投票/狼刀那里用 asyncio.gather 并发问模型，中转站一抖就是一整排同时失败。
旧实现每次 on_stall 都新建一个 future 覆盖上一个 → 用户点一次只有最后一个座位收到，
其余要干等满 stall_timeout（默认 1800 秒）。

这里直接拿 server.Room 的 on_stall/handle 做（不起服务、不联网）：
  · 3 个等待者并发进来（stall_timeout 设短一点，2 秒）
  · 中途发一次 `stall_decision: skip`
  · 要求：三个都在**远小于超时**的时间里拿到 "skip"，且 room 的 stall 状态被正确清空
  · 再来一轮，谁都不点 → 三个都按超时拿到 "skip"
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import server as srv                                     # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class FakeSeat:
    def __init__(self, sid: int) -> None:
        self.id = sid
        self.agent = None


class FakeTask:
    kind = "wolf_kill"


def make_room(timeout: float):
    """借一个 Room 实例，但不跑它的 __init__ 里那些重活。"""
    room = srv.Room.__new__(srv.Room)
    room.stall = None
    room.stall_decision = None
    room.stall_waiters = 0
    room.stall_timeout = timeout
    room.broadcast = lambda *a, **k: None
    return room


async def main() -> int:
    # ---- 第一轮：三个人同时卡住，用户点一次「跳过」----
    room = make_room(30.0)
    t0 = time.time()
    tasks = [asyncio.create_task(room.on_stall(FakeSeat(i), FakeTask(), "HTTP 503", 1))
             for i in (3, 4, 5)]
    await asyncio.sleep(0.2)
    check("1a 三个等待者共享同一个 future（不是各建一个）",
          room.stall_decision is not None and room.stall_waiters == 3,
          f"waiters={room.stall_waiters}")
    await room.handle(None, {"type": "stall_decision", "action": "skip"})
    got = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    dt = time.time() - t0
    check("1b 一次点击释放全部三个等待者，且都拿到 skip",
          got == ["skip", "skip", "skip"], f"{got}（用时 {dt:.1f}s）")
    check("1c 没有等满超时（说明不是各自超时兜底的）", dt < 5, f"{dt:.2f}s")
    check("1d 状态清空干净", room.stall is None and room.stall_decision is None
          and room.stall_waiters == 0,
          f"stall={room.stall} future={room.stall_decision} waiters={room.stall_waiters}")

    # ---- 第二轮：谁都不点，全部按超时 skip ----
    room2 = make_room(1.0)
    t1 = time.time()
    got2 = await asyncio.wait_for(
        asyncio.gather(*[room2.on_stall(FakeSeat(i), FakeTask(), "HTTP 503", 1)
                         for i in (3, 4)]), timeout=6)
    check("2a 没人点按钮时全部按超时跳过", got2 == ["skip", "skip"], str(got2))
    check("2b 超时后状态清空", room2.stall is None and room2.stall_waiters == 0,
          f"stall={room2.stall} waiters={room2.stall_waiters}")
    check("2c 每个等待者最多等一个 stall_timeout（不是叠加）", time.time() - t1 < 3.5,
          f"{time.time() - t1:.2f}s")

    # ---- 第三轮：点击落在"已经没人等"之后，不能抛异常 ----
    room3 = make_room(1.0)
    await asyncio.wait_for(room3.on_stall(FakeSeat(3), FakeTask(), "HTTP 503", 1), timeout=4)
    try:
        await room3.handle(None, {"type": "stall_decision", "action": "skip"})
        check("3a 弹窗过期后点按钮不抛异常", True, "只广播了一句提示")
    except Exception as exc:                               # noqa: BLE001
        check("3a 弹窗过期后点按钮不抛异常", False, f"{type(exc).__name__}: {exc}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
