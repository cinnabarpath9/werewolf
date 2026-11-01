# coding=utf-8
"""复核：真人超时（human_timeout）那条路**要补发一条 ask_done**（审查发现 P2-2 的后半截）。

现场：真人的一手是 `WebHumanAgent.ask` 等出来的，超时后它只广播一条 notice、
把 `pending_ask` 清掉 —— 手机那块行动面板会一直赖在底部，玩家回来点它只会收到
「现在不是你行动的时间。」。`server.py` 的 `ServerHumanAgent` 补了那条 ask_done。

做法：自己现挑空闲端口拉一台临时实例，**把 human_timeout 改成 3 秒**（临时 config），
开一局 mock、坐在 6 号但**什么都不点**，看会不会收到：
  ① 一条 notice（太久没动…）
  ② 一条 ask_done，而且 ask_id 就是刚被放掉的那一条。
跑法： python t_human_timeout.py（约 20 秒）
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
    except Exception:                                      # noqa: BLE001
        pass

import websockets                                          # noqa: E402
import yaml                                                # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp_review")
GAMES = os.path.join(TMP, "games")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def free_port() -> int:
    for _ in range(30):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("挑不到空闲端口")


def make_config() -> str:
    """测试配置 + human_timeout=3（否则要等 900 秒，这条就没法自动验了）。"""
    os.makedirs(GAMES, exist_ok=True)
    cfg = yaml.safe_load(io.open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8"))
    cfg.setdefault("game", {})["save_dir"] = GAMES
    cfg["game"]["human_timeout"] = 3
    cfg["game"]["experience"] = False
    path = os.path.join(TMP, "timeout-config.yaml")
    io.open(path, "w", encoding="utf-8").write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    return path


def start_server(port: int, config: str):
    os.makedirs(os.path.join(HERE, "_logs"), exist_ok=True)
    log_path = os.path.join(HERE, "_logs", f"humantimeout-{port}.out.log")
    log = io.open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "server.py", "--port", str(port),
                             "--config", config, "--mock-default"],
                            cwd=WW, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, log_path, log
    raise RuntimeError(f"临时实例没起来（看 {log_path}）")


def token_of(log_path: str) -> str:
    for _ in range(40):
        try:
            text = io.open(log_path, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        m = re.search(r"\[token\] 本实例认的配对码 = ([A-Z0-9]{6})", text)
        if m:
            return m.group(1)
        time.sleep(0.25)
    return ""


async def probe(port: int, token: str) -> dict:
    """开一局、坐 6 号、**什么都不点**，看超时那一路回了什么。"""
    out: dict = {"ask": None, "notice": "", "done": None, "types": []}
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}",
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_dream", "seed": 3}))
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            except asyncio.TimeoutError:
                if out["done"] is not None:
                    break
                continue
            t = m.get("type")
            out["types"].append(t)
            if t == "ask" and out["ask"] is None:
                out["ask"] = m
            elif t == "notice" and "弃权" in str(m.get("text") or ""):
                out["notice"] = str(m.get("text"))
            elif t == "ask_done" and out["done"] is None:
                out["done"] = m
                break
    return out


def main() -> int:
    if not os.path.isdir(TMP):
        os.makedirs(TMP, exist_ok=True)
    cfg_path = make_config()
    port = free_port()
    proc, log_path, log = start_server(port, cfg_path)
    try:
        token = token_of(log_path)
        if not token:
            check("能读到这一台实例的配对码", False, "日志里没有 [token] 那行")
            return 1
        out = asyncio.run(probe(port, token))
        ask = out["ask"] or {}
        print(f"     收到的 ask：ask_id={ask.get('ask_id')!r} kind={ask.get('kind')!r}")
        print(f"     超时 notice：{out['notice'][:60]!r}")
        print(f"     随后的 ask_done：{out['done']}")
        check("① 超时那条路仍然有「按弃权处理」的 notice（老行为没丢）",
              "弃权" in out["notice"], out["notice"][:60])
        check("② ⚠️ 超时后补发了一条 ask_done（面板才能收掉；以前没有）",
              isinstance(out["done"], dict), str(out["done"])[:100])
        check("③ 那条 ask_done 带的就是**被放掉的那条** ask_id（前端才对得上号）",
              bool(out["done"]) and isinstance(ask.get("ask_id"), int)
              and out["done"].get("ask_id") == ask.get("ask_id"),
              f"ask={ask.get('ask_id')!r} → done={(out['done'] or {}).get('ask_id')!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                                  # noqa: BLE001
            proc.kill()
        log.close()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
