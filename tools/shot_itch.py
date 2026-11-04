# coding=utf-8
"""用 Edge headless + CDP 截 itch 用的 5 张图（当前版本，不摆旧图）。

产物：dist-itch/screenshots/01-lobby.png … 05-settlement.png
要求：8077 上有服务（离线演练即可）；Edge 在默认路径。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist-itch", "screenshots")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
def _token() -> str:
    import re
    txt = open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig").read()
    m = re.search(r"[A-Z0-9]{6}", txt)
    return m.group(0) if m else ""


URL = "http://127.0.0.1:8077/?token=" + _token()
PORT = 9333


async def main() -> int:
    import websockets

    os.makedirs(OUT, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="wolf-shot-")
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}", "--window-size=1440,900",
        "--hide-scrollbars", "--no-first-run", "--disable-gpu", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
                tabs = json.loads(r.read().decode())
            page = next((t for t in tabs if t.get("type") == "page"), None)
            if page:
                ws_url = page["webSocketDebuggerUrl"]
                break
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(0.4)
    if not ws_url:
        print("❌ Edge 没起来（CDP 端口无响应）")
        proc.terminate()
        return 1

    async with websockets.connect(ws_url, max_size=40 * 1024 * 1024) as ws:
        seq = 0

        async def call(method: str, **params):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    return msg.get("result") or {}

        async def js(expr: str):
            r = await call("Runtime.evaluate", expression=expr, returnByValue=True,
                           awaitPromise=True)
            return r.get("result", {}).get("value")

        async def shot(name: str):
            r = await call("Page.captureScreenshot", format="png")
            data = base64.b64decode(r["data"])
            path = os.path.join(OUT, name)
            with open(path, "wb") as fh:
                fh.write(data)
            print(f"  ✅ {name}  {len(data)//1024} KB")

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                   deviceScaleFactor=1, mobile=False)
        await call("Page.navigate", url=URL)
        await asyncio.sleep(4)
        gate = await js("document.body.innerText.includes('输入配对码')")
        print("  配对门还在？", gate)

        # ① 大厅
        await shot("01-lobby.png")

        # ② 选板子（顺便强制离线演练，别烧真模型）
        await js("document.getElementById('btnPlay').click()")
        await asyncio.sleep(1.5)
        await js("(function(){var m=document.getElementById('pickerMock');"
                 "if(m && !m.checked) m.click(); })()")
        await asyncio.sleep(0.4)
        await shot("02-boards.png")

        # ③ 对局中（真人座位会停在行动面板；没 key 自动离线演练）
        await js("document.getElementById('btnConfirmStart').click()")
        await asyncio.sleep(4)
        await shot("03-game.png")

        # ④ 放弃当前局（两下确认），改观战一局到结算
        await js("document.getElementById('btnAbandon').click()")
        await asyncio.sleep(0.5)
        await js("document.getElementById('btnAbandon').click()")
        await asyncio.sleep(2.5)
        await js("document.getElementById('btnPlay').click()")
        await asyncio.sleep(1.2)
        await js("(function(){var m=document.getElementById('pickerMock');"
                 "if(m && !m.checked) m.click(); })()")
        await asyncio.sleep(0.3)
        await js("document.getElementById('btnSpectate').click()")
        await asyncio.sleep(4)
        await shot("04-spectate.png")
        # ⑤ 等观战局打完（mock 全 AI 一般几十秒内）
        done = False
        for _ in range(90):
            done = await js("!!document.querySelector('[data-panel=\"settlement\"]')")
            if done:
                break
            await asyncio.sleep(1)
        print("  结算面板出现了？", done)
        await asyncio.sleep(0.6)
        await shot("05-settlement.png")

        # ⑥ 设置页（模型 API / 语音 / 背景音乐）
        await js("document.getElementById('btnSettings').click()")
        await asyncio.sleep(1.2)
        await shot("06-settings.png")

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:                                         # noqa: BLE001
        proc.kill()
    shutil.rmtree(profile, ignore_errors=True)
    print(f"\n产物：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))