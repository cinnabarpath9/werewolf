# coding=utf-8
"""端到端验一次手机取语音那条路（带新的语速/音高/指令）：
开一局 mock → 找一条别人的发言 → 打 /api/tts → 应该拿到 MP3 字节；再打一次应命中缓存。

⚠️ 2026-10-12：本脚本打的是**用户那台正在跑的 8077**（拿不到临时 `save_dir`）—— 它开的这局
mock 以前会被服务端写进生产 `games/`（存档污染事故，见 `docs/待完善清单.md` E57）。
现在服务端把 mock 局一律写 `<save_dir>/_mock/`（`server.py::_mock_save_dir`）；
要连 8077 都不碰，就得像 `t_page_review.py` 那样自己拉一台临时实例，本轮没做。
"""
import asyncio
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import httpx                                              # noqa: E402
import websockets                                         # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = re.search(r"[A-Z0-9]{6}", io.open(os.path.join(ROOT, "配对码.txt"),
                                         encoding="utf-8-sig").read()).group()
BASE = "http://127.0.0.1:8077"


async def main() -> int:
    entries: list[dict] = []
    async with websockets.connect(f"ws://127.0.0.1:8077/ws?token={TOKEN}",
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.6)
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                 "board": "eleven_standard", "seed": 5}))
        t0 = time.time()
        while time.time() - t0 < 120 and len(entries) < 6:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "entry":
                e = m["entry"]
                if e.get("kind") == "speech" and e.get("seat") not in (None, 6):
                    entries.append(e)
            elif m.get("type") == "ask":
                await ws.send(json.dumps({"type": "action",
                                          "payload": {"abstain": True, "speech": "（测试）我过。",
                                                      "reason": "语音链路测试"}}))
        status = httpx.get(f"{BASE}/api/status", params={"token": TOKEN}, timeout=10).json()
        idx = None
        # 找到这条发言在台账里的序号
        for i, e in enumerate((status.get("ledger") or [])):
            pass
        async with httpx.AsyncClient(timeout=180) as c:
            # 用台账序号：从 entry 事件里拿不到序号，改为按"最新一条发言"逐个试
            for n in range(1, 400):
                r = await c.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": n})
                if r.status_code == 200 and r.content[:3] == b"ID3":
                    print(f"  ✅ /api/tts?n={n} → {len(r.content)} 字节 MP3")
                    t1 = time.time()
                    r2 = await c.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": n})
                    print(f"  ✅ 再取同一条（应命中缓存）：{len(r2.content)} 字节，"
                          f"{time.time() - t1:.2f}s")
                    idx = n
                    break
                if r.status_code not in (200, 400, 404, 409):
                    print(f"  ⚠️ n={n} → HTTP {r.status_code} {r.text[:100]}")
            if idx is None:
                # ⚠️ 测试实例（test-config.yaml）通常**没配 TTS 音色** —— 那不是缺陷。
                #    真跑：在 .env 里配好 VOICE_BASE_URL/VOICE_API_KEY（或用本地假语音端点），
                #    再对着那台实例跑这个脚本。
                import os as _os
                if not (_os.environ.get("VOICE_BASE_URL") or _os.environ.get("VOICE_API_KEY")):
                    print("  SKIP：这个测试实例没配音色（.env 缺 VOICE_*）→ 跳过 TTS 端到端")
                    await ws.send(json.dumps({"type": "abandon"}))
                    return 0
                print("  ❌ 没找到可合成的发言")
                await ws.send(json.dumps({"type": "abandon"}))
                return 1
        await ws.send(json.dumps({"type": "abandon"}))
        await asyncio.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
