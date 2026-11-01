# coding=utf-8
"""ask_id 的**前后端协议闭环**：ask(ask_id=N) → action{ask_id:N} → ask_done{ask_id:N}。

为什么单独立一条：断线重连时"旧问题的答案"和"新问题的面板"会撞在一起（见
web/index.html 的 `case 'ask_done'`），前端靠 ask_id 对号入座才没把新面板清掉。
前端那一半在 tests/t_page_stale.py 第一节验（喂报文 + 看面板）；
这里验**服务端真的把同一个 ask_id 收下又原样带回来** —— 两边对不上，修复就是白做。

做法：现挑一个空闲端口起一台临时实例（不占 8086），开一局 mock，等"轮到你"那条 ask，
回一手带 ask_id 的 action，看 ask_done 回来的 ask_id 是不是同一个。

跑法： python t_ask_id.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass

import websockets                                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def read_token() -> str:
    for line in io.open(os.path.join(WW, "配对码.txt"), encoding="utf-8-sig"):
        m = re.search(r"[A-Z0-9]{6}", line)
        if m:
            return m.group()
    return ""


def start_server(port: int):
    log = io.open(os.path.join(HERE, "_logs", f"askid-{port}.out.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "server.py", "--port", str(port),
                             "--config", os.path.join(HERE, "test-config.yaml"), "--mock-default"],
                            cwd=WW, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.5)
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, log
    raise RuntimeError("临时实例没起来（看 _logs/askid-*.out.log）")


async def round_trip(port: int, token: str) -> dict:
    """开一局 → 等 ask → 回一手 → 收 ask_done。返回观察到的报文。"""
    out: dict = {"asks": [], "done": None, "second_ask": None, "closed": False}
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}",
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_dream", "seed": 3}))
        t0 = time.time()
        answered = False
        # ⚠️ 循环条件不能只看 ask_done：mock 局里第二条 ask **紧跟着**就来
        #    （实测 ask_id=1 witch / 2 speech / 3 vote 几乎同时），
        #    一拿到 ask_done 就收工的话永远看不到第二条 —— 那条断言就成了"跳过的空测"。
        #    这里等到"有第二条 ask 了"或者"这一局结束了"或者超时为止。
        while time.time() - t0 < 45:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                if out["second_ask"] is not None or out["closed"]:
                    break
                continue
            if m.get("type") == "ask":
                if not answered:
                    out["asks"].append(m)
                    answered = True
                    # 原样回传 ask_id（前端在 renderAsk 里就是这么发的）
                    await ws.send(json.dumps({"type": "action", "ask_id": m.get("ask_id"),
                                              "payload": {"abstain": True,
                                                          "speech": "（测试）我过。",
                                                          "reason": "ask_id 协议联调"}}))
                elif out["second_ask"] is None and m.get("ask_id") != out["asks"][0].get("ask_id"):
                    out["second_ask"] = m
            elif m.get("type") == "ask_done":
                if out["done"] is None:
                    out["done"] = m
                elif out["second_ask"] is not None:
                    break
            elif m.get("type") == "end":
                out["closed"] = True
                if out["second_ask"] is not None or out["done"] is not None:
                    break
    return out


async def stale_action_probe(port: int, token: str) -> dict:
    """⑤：拿到一条真 ask 之后，先发一条**旧编号**的 action，再发正确编号的。

    期望：旧编号那条被丢掉（当前 pending_ask 不动、且回一条 notice），
    紧接着正确编号那条正常把这一手答掉（ask_done 带的是正确编号）。
    """
    out: dict = {"kept_pending": None, "noticed": False, "fresh_ok": False, "observed": []}
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}",
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_dream", "seed": 5}))
        ask = None
        t0 = time.time()
        while time.time() - t0 < 90 and ask is None:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "ask" and isinstance(m.get("ask_id"), int):
                ask = m
        if ask is None:
            out["observed"].append("没等到带编号的 ask")
            return out
        stale_id = ask["ask_id"] - 1                 # 一条不存在的旧编号（一定是"过期的那条"）
        await ws.send(json.dumps({"type": "action", "ask_id": stale_id,
                                  "payload": {"abstain": True, "reason": "这是过期的一手"}}))
        # 等一会儿：看它有没有被当成答案收下
        deadline = time.time() + 4
        done_for_stale = None
        while time.time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                break
            out["observed"].append(m.get("type"))
            if m.get("type") == "notice" and "已忽略" in str(m.get("text") or ""):
                out["noticed"] = True
            if m.get("type") == "ask_done":
                done_for_stale = m
        out["kept_pending"] = done_for_stale is None
        # 正确的那一手：必须还能答
        await ws.send(json.dumps({"type": "action", "ask_id": ask["ask_id"],
                                  "payload": {"abstain": True, "speech": "（测试）我过。",
                                              "reason": "正确编号这一手"}}))
        deadline = time.time() + 6
        while time.time() < deadline and not out["fresh_ok"]:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                break
            out["observed"].append(m.get("type"))
            if m.get("type") == "ask_done" and m.get("ask_id") == ask["ask_id"]:
                out["fresh_ok"] = True
    return out


def main() -> int:
    token = read_token()
    port = free_port()
    proc, log = start_server(port)
    print(f"临时实例：http://127.0.0.1:{port}（现挑的空闲端口，不占 8086）")
    try:
        out = asyncio.run(round_trip(port, token))
        first = out["asks"][0] if out["asks"] else {}
        print(f"     收到的 ask：kind={first.get('kind')!r} ask_id={first.get('ask_id')!r}")
        print(f"     收到的 ask_done：{out['done']}")
        check("① 服务端给每条 ask 都带上了单调递增的 ask_id（整数）",
              isinstance(first.get("ask_id"), int), repr(first.get("ask_id")))
        check("② 收到 action 之后回了 ask_done",
              isinstance(out["done"], dict), str(out["done"])[:100])
        check("③ ⚠️ ask_done 里的 ask_id 和刚回答的那条**完全一致**（前端才能对号入座）",
              bool(out["done"]) and out["done"].get("ask_id") == first.get("ask_id"),
              f"ask={first.get('ask_id')!r} → done={(out['done'] or {}).get('ask_id')!r}")
        if out["second_ask"] is not None:
            check("④ 下一条 ask 的 ask_id 是**新的**（不会复用，否则对号入座会串）",
                  out["second_ask"].get("ask_id") != first.get("ask_id"),
                  f"{first.get('ask_id')!r} → {out['second_ask'].get('ask_id')!r}")
        else:
            # ⚠️ 2026-10-08 改（审查发现的 P3-4）：这里以前是 `check(..., True, "skipped")`
            #    —— **永远不可能失败**的断言等于没测。要么真的等到第二条，要么就是这一局
            #    没走到第二轮（那说明这一局的形状变了，得知道），所以现在按失败处理。
            check("④ 下一条 ask 的 ask_id 是**新的**（不会复用，否则对号入座会串）",
                  False, f"90 秒内没等到第二条 ask（这一局只走了一轮？收到 {len(out['asks'])} 条）")

        # ⑤ 迟到 / 串号的 action 不许顶掉当前问题（2026-10-08 自查 P2-1 的回归）
        #    现场：断线时在**旧**面板上点了确认，服务端早把那条 ask 放掉、引擎已经问了新的；
        #    补发上去的旧编号 action 以前会直接把新问题的 future 解掉 —— 玩家那一票投到别处去。
        stale = asyncio.run(stale_action_probe(port, token))
        check("⑤ 迟到的旧编号 action 被**丢弃**（不动当前 future，且回一条人话）",
              stale.get("kept_pending") is True and stale.get("noticed") is True,
              f"面板还在={stale.get('kept_pending')} 有提示={stale.get('noticed')}"
              f"｜{str(stale.get('observed'))[:120]}")
        check("⑤-b 丢弃之后**正确编号**的那一手照样能答（没把路堵死）",
              stale.get("fresh_ok") is True, str(stale.get("observed"))[:120])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                                     # noqa: BLE001
            proc.kill()
        log.close()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(main())
