# coding=utf-8
"""回归：**真人坐在 6 号**时整条 WS 链路（今天漏掉 P1 的根因：所有测试都拿 human_seat=0/1 跑）。

钉死三件事：
  1) 真人收到的 ask 里**不许**出现模型内部决策类型（`speech_plan` / `analyze` /
     `vote_intent` / `wolf_intent` / `reflect`）——
     那些报文前端没有对应的面板，真人收到只能点「这一手放弃」，或者干等 human_timeout（900 秒）；
  2) 每个 ask 的 kind 都必须是前端 `labelMap` 认识的键。键是**从 web/index.html 现解析**的，
     不抄一份拷贝 —— 抄了的话前端加/改面板时这个测试照样是绿的（等于没测）；
  3) 「轮到你」的报文协议：每条 ask 带**单调递增**的 ask_id，回传 ask_id 后收到的 ask_done
     带的是**同一个** id（否则排队到达的旧答案会清掉玩家正在操作的新面板）。

跑法（8086 的约定跟其它脚本一致）：
    python t_human_path.py
  · 8086 上已经有测试服务就直接复用（run_all.py 或别的脚本拉起的）；
  · 没有就按 run_all.py 那套命令自己拉一台（`server.py --port 8086 --config tests/test-config.yaml
    --mock-default`），跑完只关自己拉的那台。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

import httpx                                              # noqa: E402
import websockets                                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                              # tests/ 的上一级就是项目根
#: 跑在哪台测试实例上。默认 8086（老行为）；`run_all.py` 用 `WW_TEST_PORT` 覆盖。
#: ⚠️ 它自己"没服务就拉一台"时也用这个端口，所以拉到的是**它该用的那台**。
PORT = int(os.environ.get("WW_TEST_PORT") or 8086)
HUMAN_SEAT = 6                                            # ← 这一条是本文件存在的理由
BOARD = "eleven_standard"                                 # 11 人：会走到警长竞选那一整套面板
SEED = 6
DEADLINE = 240.0                                          # mock 局跑完的预算（秒）

#: 模型**内部决策**类型：只该在服务端/引擎里流转，永远不许推到真人手机上。
#: （它们都不在 web/index.html 的 labelMap 里 —— 也就是说前端根本没有面板能接住。）
INTERNAL_KINDS = ("speech_plan", "analyze", "vote_intent", "wolf_intent", "reflect")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def token() -> str:
    text = io.open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig").read()
    m = re.search(r"[A-Z0-9]{6}", text)
    return m.group() if m else ""


def frontend_label_kinds() -> set[str]:
    """从 web/index.html 现解析 `labelMap` 的键（= 前端真的有面板的行动类型）。

    解析失败就返回空集，由调用方判失败 —— 不能悄悄退化成「所有 kind 都通过」。
    """
    html = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    m = re.search(r"const labelMap\s*=\s*\{(.*?)\}\s*;", html, re.S)
    if not m:
        return set()
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))


def server_up() -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{PORT}/api/status",
                      params={"token": token()}, timeout=3)
        return r.status_code == 200
    except Exception:                                     # noqa: BLE001
        return False


class TestServer:
    """8086 上的测试服务：**有就复用，没有才自己拉**（跑完只关自己拉的那台）。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.log = None

    def ensure(self) -> bool:
        if server_up():
            print(f"8086 上已经有测试服务，直接复用（不再另起）。")
            return True
        logs = os.path.join(HERE, "_logs")
        os.makedirs(logs, exist_ok=True)
        self.log = open(os.path.join(logs, "humanpath-8086.out.log"), "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "server.py", "--port", str(PORT),
             "--config", os.path.join(HERE, "test-config.yaml"), "--mock-default"],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        print(f"8086 上没人，按 run_all.py 的同一套命令自己拉了一台（PID {self.proc.pid}）…")
        for _ in range(60):
            time.sleep(0.5)
            if server_up():
                return True
            if self.proc.poll() is not None:
                return False
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:                                 # noqa: BLE001
            self.proc.kill()
        if self.log:
            self.log.close()
        print("已关掉自己拉起的那台测试服务。")


def reply_for(ask: dict) -> dict:
    """真人这一手怎么答（每一种都给一个合法答案，让对局能一路走到 end）。"""
    kind = ask.get("kind")
    legal = ask.get("legal") or []
    if kind in ("speech", "sheriff_speech", "wolf_talk"):
        return {"speech": "（自动测试）我是 6 号，先说我自己的判断。", "claim": "无",
                "suspect": [], "reason": "t_human_path 自动测试"}
    if kind == "last_words":
        return {"speech": "（自动测试）这是我的遗言。", "claim": "无"}
    if kind == "witch":
        return {"save": False, "poison": None, "reason": "t_human_path 自动测试"}
    if kind == "sheriff_run":
        return {"run": True, "reason": "t_human_path：上警试试"}
    if kind in ("vote", "sheriff_vote"):
        return {"vote": legal[0]} if legal else {"abstain": True, "reason": "没得投"}
    if kind == "badge":
        return {"to": legal[0]} if legal else {"to": None, "reason": "撕掉"}
    if kind in ("wolf_kill", "seer_check", "guard", "hunter_shoot"):
        return {"target": legal[0] if legal else None, "reason": "t_human_path 自动测试"}
    return {"abstain": True, "reason": "t_human_path 自动测试"}


async def play() -> dict:
    """真人在 6 号打一局 mock，收集：所有 ask、ask_done、是否收到 end。"""
    url = f"ws://127.0.0.1:{PORT}/ws?token={token()}"
    out: dict = {"asks": [], "dones": [], "end": None, "errors": [],
                 "notes": [], "answered": []}
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": HUMAN_SEAT,
                                  "board": BOARD, "seed": SEED}))
        deadline = time.time() + DEADLINE
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            kind = m.get("type")
            if kind == "ask":
                out["asks"].append(m)
                payload = reply_for(m)
                # 客户端协议：把收到的 ask_id 原样回传（老客户端不带，这里按新协议走）
                await ws.send(json.dumps({"type": "action", "payload": payload,
                                          "ask_id": m.get("ask_id")}))
                out["answered"].append(m.get("ask_id"))
            elif kind == "ask_done":
                out["dones"].append(m)
            elif kind == "error":
                out["errors"].append(m.get("text"))
            elif kind == "notice":
                out["notes"].append(str(m.get("text")))
            elif kind == "end":
                out["end"] = m
                break
    return out


async def main() -> int:
    srv = TestServer()
    if not srv.ensure():
        check("A0 8086 上的测试服务可用（复用或自己拉起）", False, "起不来，见 _logs/humanpath-8086.out.log")
        srv.stop()
        return finish()

    label_kinds = frontend_label_kinds()
    print(f"前端 labelMap 解析到 {len(label_kinds)} 个键：{sorted(label_kinds)}\n")
    check("A1 能从 web/index.html 解析出 labelMap（>=8 个键，否则说明前端结构变了）",
          len(label_kinds) >= 8, f"{len(label_kinds)} 个")

    print(f"=== 真人坐 {HUMAN_SEAT} 号打一局 mock（{BOARD}，seed {SEED}）===")
    try:
        out = await play()
    finally:
        srv.stop()

    asks = out["asks"]
    kinds = [a.get("kind") for a in asks]
    check("A2 真人真的被问到了（整局都推给了 6 号，不是空跑）", len(asks) >= 3,
          f"{len(asks)} 次：{kinds}")
    check("A3 一局在预算内打完（收到 end）", out["end"] is not None,
          f"{DEADLINE:.0f}s 预算；错误={out['errors'][:2]}")

    bad_internal = sorted(set(k for k in kinds if k in INTERNAL_KINDS))
    check("A4 真人**没有**收到模型内部决策类型（speech_plan/analyze/vote_intent/wolf_intent/reflect）",
          not bad_internal, f"收到过：{bad_internal}；全部类型={sorted(set(kinds))}")

    unknown = sorted(set(k for k in kinds if k not in label_kinds))
    check("A5 每个 ask 的 kind 都在前端 labelMap 里（前端有面板能接住）",
          not unknown, f"前端不认识的：{unknown}；labelMap={sorted(label_kinds)}")

    ids = [a.get("ask_id") for a in asks]
    check("A6 每条 ask 都带正整数 ask_id（协议：轮到你 → 发号）",
          bool(ids) and all(isinstance(i, int) and not isinstance(i, bool) and i >= 1 for i in ids),
          str(ids[:12]))
    check("A7 ask_id 单调递增且不重复（同一条补发才允许同号）",
          ids == sorted(ids) and len(set(ids)) == len(ids), str(ids[:12]))

    dones = out["dones"]
    echo_ok = bool(dones) and all(d.get("ask_id") in ids for d in dones)
    check("A8 ask_done 回传的是**收到过的**那个 ask_id（客户端据此只清自己那条面板）",
          echo_ok, f"{len(dones)} 条 ask_done，id={[d.get('ask_id') for d in dones][:12]}")
    # 逐条对：第 N 个 ask_done 应该对应第 N 个被回答的 ask
    paired = [d.get("ask_id") for d in dones] == out["answered"][:len(dones)]
    check("A9 ask → action(ask_id) → ask_done(同一个 id) 一一对应",
          paired, f"dones={[d.get('ask_id') for d in dones][:8]} answered={out['answered'][:8]}")

    # 老客户端兼容：不带 ask_id 的回传也要能走（只发不带 id 的 ask_done，不报错）
    return finish()


def finish() -> int:
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
