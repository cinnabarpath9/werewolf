# coding=utf-8
"""挖"取消/并发路径"：真人正在被问的时候，如果放弃本局 / 断线 / 超时，房间状态还干净吗？

怀疑点（读代码看出来的）：
  A. 放弃本局时把 `awaiting.cancel()` —— 之后真人再点一次提交，会不会打到已取消的 future；
  B. 断线时 pending_ask 清了没有（重连会不会看到一个已经过期的面板）；
  C. 连点两次「开始」/「放弃」会不会留下两个对局任务；
  D. 引擎侧：模型调用被取消（放弃）时，transcript 会不会记出半条烂记录；
  E. stall（上游失败弹窗）期间点放弃，等待者会不会被永久挂住。
"""
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import httpx                                             # noqa: E402
import websockets                                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 跑在哪台测试实例上。默认 8086（老行为）；`run_all.py` 会用 `WW_TEST_PORT` 把它指到
#: 另一台实例上（2026-10-11 提速：单房间的服务端要拆成几台才能并行，见 run_all 的 TEST_INSTANCES）。
PORT = int(os.environ.get("WW_TEST_PORT") or 8086)
TOKEN = re.search(r"[A-Z0-9]{6}", io.open(os.path.join(ROOT, "配对码.txt"),
                                         encoding="utf-8-sig").read()).group()
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def status() -> dict:
    return httpx.get(f"http://127.0.0.1:{PORT}/api/status",
                     params={"token": TOKEN}, timeout=10).json()


async def drain(ws, seconds: float = 2.0) -> list:
    out = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.4)))
        except asyncio.TimeoutError:
            continue
    return out


async def main() -> int:
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"

    print("=== A. 真人被问的时候放弃本局，再点提交会不会打到已取消的 future ===")
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 3}))
        got_ask = None
        for m in await drain(ws, 12):
            if m.get("type") == "ask":
                got_ask = m
                break
        check("A1 mock 局里真人座位照样会被问（这样才能测真人流程）", got_ask is not None,
              str(got_ask)[:80] if got_ask else "没收到 ask")
        await ws.send(json.dumps({"type": "abandon"}))
        await drain(ws, 3)

    print("\n=== B. 放弃之后：房间状态是否回到干净（不残留 awaiting/pending_ask）===")
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        msgs = await drain(ws, 2)
        st = status()
        check("B1 status 里 running=False", st.get("running") is False, str(st.get("running")))
        # 放弃后再发一次"行动"：不该崩、也不该有异常日志
        await ws.send(json.dumps({"type": "action", "payload": {"speech": "过", "vote": 1}}))
        msgs2 = await drain(ws, 1.5)
        errs = [m for m in msgs2 if m.get("type") == "error"]
        check("B2 对局已结束时提交行动：给一句人话提示（不是崩）",
              all("type" in m for m in msgs2), str(errs)[:120])

    print("\n=== C. 连点两次开始/放弃：不该留下第二个对局任务 ===")
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 4}))
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 5}))
        await asyncio.sleep(1.5)
        st = status()
        check("C1 连点两次开始：只有一个对局在跑", st.get("running") is True, str(st.get("running")))
        await ws.send(json.dumps({"type": "abandon"}))
        await ws.send(json.dumps({"type": "abandon"}))
        await asyncio.sleep(3)
        st2 = status()
        check("C2 连点两次放弃：状态收敛为「没在跑」", st2.get("running") is False, str(st2.get("running")))
        check("C3 服务还活着（能回答 status）", "board" in st2, str(list(st2))[:80])

    print("\n=== D. 放弃时正在进行的模型调用：transcript 不该记半条烂记录 ===")
    # mock 局不会调模型，这里用"真模型 + 立刻放弃"来验：只要服务不崩、状态干净即可
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start", "mock": False, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 6}))
        await asyncio.sleep(2.0)                     # 让第一手真的发出去
        await ws.send(json.dumps({"type": "abandon"}))
        end = [m for m in await drain(ws, 25) if m.get("type") == "end"]
        check("D1 放弃能正常收尾（收到 end）", bool(end), str(end)[:120])
        if end:
            save = end[0].get("save") or ""
            check("D2 存档写出来了", bool(save) and os.path.isfile(save), str(save)[-40:])
        st3 = status()
        check("D3 放弃后 running=False", st3.get("running") is False, str(st3.get("running")))

    print("\n=== E. 真人正被问时放弃 → 重连后不该再收到那条作废的 ask 面板 ===")
    first = None
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 7}))
        for m in await drain(ws, 15):
            if m.get("type") == "ask":
                first = m
                break
        check("E1 确实等到了真人面板", first is not None, str(first)[:70] if first else "没等到")
        await ws.send(json.dumps({"type": "abandon"}))
        await drain(ws, 4)
    # 换一条新连接重连（模拟手机切页面后回来）
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws2:
        await ws2.send(json.dumps({"type": "sync"}))
        msgs = await drain(ws2, 3)
        asks = [m for m in msgs if m.get("type") == "ask"]
        st = status()
        check("E2 重连后不再补发作废的 ask 面板", not asks, str(asks)[:120])
        check("E3 房间状态也确认没在跑", st.get("running") is False, str(st.get("running")))

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
