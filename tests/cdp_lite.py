# coding=utf-8
"""最小 CDP 客户端：起一个自己的无头 Edge，连上页面，跑 Runtime.evaluate。

不依赖别人写的 harness（避免"共用别人的注入装置"带来的混淆）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def _kill_port_owners(port: int) -> None:
    """把**正在监听这个端口**的进程连同子进程一起杀掉（Windows：`taskkill /T /F`）。

    为什么需要：`subprocess.Popen.kill()` 在 Windows 上只结束父进程，Edge 的子进程
    （真正持有 `--remote-debugging-port` 的那个）会继续活着。于是下一次起 Edge 时，
    新进程发现端口被占、把参数交给**旧实例**，而旧实例里开着的还是上一次那个页面 ——
    测试连上去了，却是在测另一个页面（自测时表现为"每个按钮都没反应"）。
    """
    if os.name != "nt":
        return
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:                                      # noqa: BLE001
        return
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                pids.add(parts[4])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True,
                           timeout=15)
        except Exception:                                  # noqa: BLE001
            pass


class Page:
    def __init__(self, port: int, profile: str) -> None:
        self.port = port
        self.profile = profile
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self._id = 0

    def start(self, url: str) -> None:
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile, exist_ok=True)
        # ⚠️ 2026-10-08 修：先把**占着这个调试端口的旧 Edge**清掉再起。
        #    Windows 上 `proc.kill()` 只杀父进程，Edge 的子进程会活下来继续占着
        #    `--remote-debugging-port` —— 于是下一次 `start()` 起来的新进程会**接进那个旧实例**，
        #    而旧实例里开着的是**上一次那个页面**（实测：新 URL 根本没被打开，
        #    连上的是两小时前那个临时端口上的页面，测试于是"点了没反应"，排查了很久）。
        _kill_port_owners(self.port)
        self.proc = subprocess.Popen(
            [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
             "--disable-extensions", f"--user-data-dir={self.profile}",
             f"--remote-debugging-port={self.port}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def connect(self, want_url_part: str = "", timeout: float = 30.0) -> None:
        """找到目标页并连上。

        ⚠️ 匹配要**宽容**：实测同一条命令，Edge 有时把页面 URL 报成带 query 的完整地址，
        有时报成不带 `?token=…` 的裸地址（还会先弹一个 edge://sync-confirmation-dialog 页）
        —— 只按完整 URL 精确匹配会直接"找不到页面 target"。
        所以这里三级回退：完整匹配 → 去掉 query 的前缀匹配 → 任意 http 页面。

        ⚠️⚠️ 2026-10-08 修：最后那级"任意 http 页面"**只在调用方没指定 URL 时才用**。
        现场（自测排查了很久）：机器上同时跑着别的 Edge/别的测试实例，
        这台 Edge 想要的页面还没开出来时，回退随手抓了**别人的** http 页面 ——
        于是 `location.host` 是别人的端口、`/api/games` 回空列表、页面上每个按钮都"点了没反应"，
        看起来像前端坏了，其实是测试连错了页面，而 `p.js()` 一路都是"成功"的。
        指定了 URL 就必须**是那个 URL**（哪怕是超时报错，也比连错页面强）。
        """
        deadline = time.time() + timeout
        target = None
        base = want_url_part.split("?")[0]
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as fh:
                    items = json.load(fh)
                pages = [it for it in items if it.get("type") == "page"]
                for want in (want_url_part, base):
                    if not want:
                        continue
                    target = next((it for it in pages if want in (it.get("url") or "")), None)
                    if target:
                        break
                if not target and not want_url_part:
                    target = next((it for it in pages
                                   if (it.get("url") or "").startswith("http")), None)
                if target:
                    break
            except Exception:                                  # noqa: BLE001
                pass
            await asyncio.sleep(0.4)
        if not target:
            # 找不到就把"这台 Edge 现在开着哪些页"打出来：连错页面/页面没开出来时，
            # 这行日志是唯一的线索（自测时为了一个"点了没反应"的问题排查了很久）。
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as fh:
                    names = [(it.get("type"), (it.get("url") or "")[:90]) for it in json.load(fh)]
            except Exception as exc:                           # noqa: BLE001
                names = [("(读不到)", str(exc))]
            raise RuntimeError(f"找不到页面 target（想要 {want_url_part or '任意 http 页面'}）；"
                               f"这台 Edge 现在是：{names}")
        self.ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024)
        await self.send("Runtime.enable")
        await self.send("Page.enable")

    async def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=60))
            if raw.get("id") == mid:
                if "error" in raw:
                    raise RuntimeError(f"{method} → {raw['error']}")
                return raw.get("result") or {}

    async def js(self, expr: str, await_promise: bool = True):
        r = await self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise,
                                                "returnByValue": True})
        if r.get("exceptionDetails"):
            raise RuntimeError("JS 异常: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
        return (r.get("result") or {}).get("value")

    def kill(self) -> None:
        # ⚠️ 连**整棵进程树**一起收掉：只 kill 父进程的话，Edge 的子进程会继续占着
        #    调试端口（见 `_kill_port_owners` 的注释），下一次 start() 就会接进旧实例。
        if self.proc:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=15)
                else:
                    self.proc.kill()
            except Exception:                                  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:                              # noqa: BLE001
                    pass
        _kill_port_owners(self.port)
