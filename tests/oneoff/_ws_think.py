# coding=utf-8
"""决定性测试：连真 WS 开一局 mock，看服务端到底有没有发 `thinking` 事件。"""
import asyncio
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.abspath("."), "tests"))
sys.stdout.reconfigure(encoding="utf-8")
import websockets

URL = "ws://127.0.0.1:8077/ws?token=<配对码>"


async def main():
    kinds = collections.Counter()
    thinking = []
    async with websockets.connect(URL, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_dream", "seed": 0}))
        end = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            kinds[m.get("type")] += 1
            if m.get("type") == "thinking":
                thinking.append(m)
            if m.get("type") == "ask":
                await ws.send(json.dumps({"type": "action",
                                          "payload": {"abstain": True, "reason": "测试"}}))
        await ws.send(json.dumps({"type": "abandon"}))
    print("  收到的事件类型统计：", dict(kinds))
    print(f"  thinking 事件 {len(thinking)} 条：")
    for m in thinking[:6]:
        print(f"     {json.dumps(m, ensure_ascii=False)[:120]}")


asyncio.run(main())
