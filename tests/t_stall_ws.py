# coding=utf-8
"""真 WS 端到端：上游波动 → 客户端重试 + 引擎自动重试（换家）→ 暂停弹窗 → 我点按钮 → 对局继续。

跑法（会自己拉起"假上游"和一台测试服务，全部本地、不花钱）：
    python t_stall_ws.py

⚠️ 次数不再写死，改成**从配置算**（2026-10-07 改口径后吃过一次假红）：
    一手 = (1 + llm.default.max_retries) 次请求      ← 客户端层
    弹窗 = (1 + game.auto_retries) 手               ← 引擎层（每手之间退避 3s→8s）
  默认 5 × 3 = 15 次才弹窗。

覆盖两局：
  局一 fail-first=一手  ：第一手全失败 → 弹窗 → 点「再试一次」→ 成功 → 打完
  局二 fail-first=两手  ：弹窗 → 点「再试一次」→ 又失败一手 → **第二次弹窗** → 点「跳过」→ 打完
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

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx                                              # noqa: E402
import websockets                                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_PORT = 8099
SRV_PORT = 8087
CFG = os.path.join(HERE, "stall-config.yaml")
PY = sys.executable

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def token() -> str:
    text = open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig").read()
    return re.search(r"[A-Z0-9]{6}", text).group()


def _cfg_tries() -> tuple[int, int]:
    """返回 (一手的请求次数, 打满几手才弹窗)。

    从配置现算，别写死：客户端 `max_retries` 在 stall-config.yaml，
    引擎 `auto_retries` 在生产 config.yaml 的 game 段（服务端读它，缺省 2）。
    """
    import yaml                                           # noqa: PLC0415
    with io.open(os.path.join(HERE, "stall-config.yaml"), encoding="utf-8") as fh:
        sllm = ((yaml.safe_load(fh) or {}).get("llm") or {}).get("default") or {}
    client_tries = int(sllm.get("max_retries", 4)) + 1
    prod = os.path.join(ROOT, "config.yaml")
    auto = 2
    try:
        with io.open(prod, encoding="utf-8") as fh:
            auto = int(((yaml.safe_load(fh) or {}).get("game") or {}).get("auto_retries", 2))
    except Exception:                                     # noqa: BLE001
        pass
    return client_tries, max(1, auto + 1)


PER_HAND, HANDS = _cfg_tries()
NEED_FIRST_HAND = PER_HAND * HANDS          # 第一手彻底打满 → 弹窗
NEED_TWO_HANDS = PER_HAND * HANDS * 2       # 连续两手都打满 → 弹两次窗


def fake_stats() -> dict:
    return httpx.get(f"http://127.0.0.1:{FAKE_PORT}/stats", timeout=10).json()


def fake_reset(fail_first: int) -> None:
    httpx.post(f"http://127.0.0.1:{FAKE_PORT}/reset", json={"fail_first": fail_first}, timeout=10)


class Proc:
    def __init__(self, args: list[str], env: dict | None = None, cwd: str = HERE) -> None:
        _ld = os.path.join(HERE, "_logs")
        os.makedirs(_ld, exist_ok=True)
        self.log = open(os.path.join(_ld, f"stall_{args[1].split(os.sep)[-1]}.log"),
                        "w", encoding="utf-8")
        self.p = subprocess.Popen([PY, "-u", *args], cwd=cwd, stdout=self.log,
                                  stderr=subprocess.STDOUT, env=env)

    def stop(self) -> None:
        try:
            self.p.terminate()
            self.p.wait(timeout=8)
        except Exception:                                     # noqa: BLE001
            self.p.kill()
        self.log.close()


def reply_for(ask: dict) -> dict:
    kind = ask.get("kind")
    legal = ask.get("legal") or []
    if kind in ("speech", "wolf_talk", "sheriff_speech"):
        return {"speech": "（测试）我过。", "claim": "无", "reason": "ws 测试"}
    if kind == "last_words":
        return {"speech": "（测试）遗言。", "claim": "无"}
    if kind == "witch":
        return {"save": False, "poison": None, "reason": "ws 测试"}
    if kind in ("sheriff_run",):
        return {"run": False, "reason": "ws 测试"}
    if kind in ("vote", "sheriff_vote"):
        return {"vote": legal[0]} if legal else {"abstain": True}
    if kind == "badge":
        return {"to": None, "reason": "ws 测试"}
    return {"target": legal[0] if legal else None, "reason": "ws 测试"}


async def one_game(fail_first: int, decisions: list[str], tag: str) -> dict:
    """打一局：每次弹窗按 decisions 里的下一个选项回答（用完了就「跳过」）。返回收集到的材料。"""
    fake_reset(fail_first)
    url = f"ws://127.0.0.1:{SRV_PORT}/ws?token={token()}"
    out: dict = {"stalls": [], "ends": [], "notices": [], "cleared": [], "asks": 0,
                 "status_at_stall": None, "types": {}, "phase": "",
                 "phase_at_stall": None, "retries": []}
    todo = list(decisions)
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await ws.send(json.dumps({"type": "start", "mock": False, "human_seat": 1,
                                  "board": "eleven_wolfking", "seed": 7}))
        deadline = time.time() + 420
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            kind = m.get("type")
            out["types"][kind] = out["types"].get(kind, 0) + 1
            if kind == "ask":
                out["asks"] += 1
                await ws.send(json.dumps({"type": "action", "payload": reply_for(m)}))
            elif kind == "notice":
                out["notices"].append(m.get("text", ""))
            elif kind == "phase":
                # 记下「卡住的那一刻是白天还是夜里」：服务端夜里不许点名座位（见下面 S2b）
                out["phase"] = m.get("phase")
            elif kind == "retry":
                # 引擎的自动重试：服务端必须**转发**给手机（以前这个事件被静默丢掉）
                out["retries"].append({"msg": m, "phase": out["phase"]})
            elif kind == "stall":
                out["stalls"].append(m)
                if out["status_at_stall"] is None:
                    # 暂停期间状态必须还是"对局进行中"（不能被看门狗当成死局清掉）
                    st = httpx.get(f"http://127.0.0.1:{SRV_PORT}/api/status",
                                   params={"token": token()}, timeout=10).json()
                    out["status_at_stall"] = {"running": st.get("running"),
                                              "cleaning": st.get("cleaning"),
                                              "stall": bool(st.get("stall"))}
                if out["phase_at_stall"] is None:
                    out["phase_at_stall"] = out["phase"]
                await ws.send(json.dumps({"type": "stall_decision",
                                          "action": todo.pop(0) if todo else "skip"}))
            elif kind == "stall_cleared":
                out["cleared"].append(m.get("action"))
            elif kind == "end":
                out["ends"].append(m)
                break
            elif kind == "error":
                out["notices"].append("ERR:" + str(m.get("text")))
    return out


async def main() -> int:
    print("拉起假上游（8099）与测试服务（8087）…")
    print(f"口径：一手 {PER_HAND} 次请求，打满 {HANDS} 手（共 {NEED_FIRST_HAND} 次）才弹窗")
    fake = Proc([os.path.join(HERE, "fake_llm.py"), "--port", str(FAKE_PORT),
                 "--fail-first", str(NEED_FIRST_HAND)])
    env = dict(os.environ)
    env["FAKE_KEY"] = "fake-key-for-test"
    srv = Proc([os.path.join(ROOT, "server.py"), "--port", str(SRV_PORT), "--config", CFG],
               env=env, cwd=ROOT)
    try:
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                if httpx.get(f"http://127.0.0.1:{FAKE_PORT}/stats", timeout=2).status_code == 200 \
                        and httpx.get(f"http://127.0.0.1:{SRV_PORT}/api/status",
                                      params={"token": token()}, timeout=2).status_code == 200:
                    break
            except Exception:                                  # noqa: BLE001
                continue
        print("两边都起来了。\n")

        print("=" * 78)
        print(f"局一：第一手就一直撞波动（{NEED_FIRST_HAND} 次）→ 弹窗 → 点「再试一次」")
        print("=" * 78)
        r1 = await one_game(NEED_FIRST_HAND, ["retry"], "retry")
        st = fake_stats()
        check("S1 手机收到了「对局暂停」弹窗", len(r1["stalls"]) >= 1, f"{len(r1['stalls'])} 次")
        first = r1["stalls"][0] if r1["stalls"] else {}
        night = r1["phase_at_stall"] == "night"
        check("S2 弹窗里带任务与错误原文，能看出是哪一步卡了",
              bool(first.get("task")) and "boom" in str(first.get("error")),
              f"task={first.get('task')} err={str(first.get('error'))[:60]}")
        # ⚠️ 2026-10-07 改口径：**夜里的弹窗不许点名座位**。夜里会卡住的只可能是
        #    狼/预言家/女巫/守卫，「N号 的模型连续失败」等于公开「这个座位有夜间身份」。
        #    白天照旧点名（发言/投票顺序本来就是公开的）。
        check("S2b 夜里弹窗不点名座位、白天照旧点名（按卡住那一刻的阶段判）",
              (first.get("seat") is None and first.get("night") is True
               and "有人" in " ".join(r1["notices"])) if night
              else isinstance(first.get("seat"), int),
              f"卡住时阶段={r1['phase_at_stall']} seat={first.get('seat')} "
              f"night={first.get('night')} notices={r1['notices'][:2]}")
        check(f"S3 打满一手（客户端 {PER_HAND} 次 × 引擎 {HANDS} 手 = {NEED_FIRST_HAND} 次）才弹窗",
              st["failed"] == NEED_FIRST_HAND,
              f"failed={st['failed']} 期望={NEED_FIRST_HAND} requests={st['requests']}")
        # 引擎的自动重试要**看得见**：服务端以前没有 retry 分支，事件被静默丢掉 ——
        # 手机只看到「X号 正在思考…」一动不动，那条「N 秒后自动重试」的提示是死的。
        retries = r1["retries"]
        check("S3b 手机收到了 retry 报文（自动重试不再被服务端丢掉）",
              len(retries) >= 2,
              f"{len(retries)} 条；类型统计="
              f"{ {k: v for k, v in r1['types'].items() if k in ('retry', 'stall', 'notice')} }")
        check("S3c retry 报文带齐第几次/共几次/等几秒/原因（前端要显示倒计时）",
              bool(retries) and all(set(("attempt", "total", "wait", "why")) <= set(x["msg"])
                                    for x in retries),
              str(retries[:1])[:160])
        night_retries = [x for x in retries if x["phase"] == "night"]
        check("S3d 夜里的 retry 也不点名座位（只说有人在重试）",
              bool(night_retries) and all(x["msg"].get("seat") is None
                                          and x["msg"].get("night") is True
                                          for x in night_retries),
              f"夜里 {len(night_retries)} 条；示例="
              f"{ {k: night_retries[0]['msg'].get(k) for k in ('seat', 'night', 'attempt', 'total', 'wait')} if night_retries else None }")
        check("S4 暂停期间服务端状态仍是'对局进行中'（没被当成死局清掉）",
              r1["status_at_stall"] == {"running": True, "cleaning": False, "stall": True},
              str(r1["status_at_stall"]))
        check("S5 点了「再试一次」→ 服务端回 stall_cleared(retry)",
              r1["cleared"] and r1["cleared"][0] == "retry", str(r1["cleared"]))
        check("S6 重试之后对局继续并打完（收到 end）", len(r1["ends"]) == 1,
              str(r1["ends"][:1]))
        check(f"S7 失败次数就停在 {NEED_FIRST_HAND}（重试成功后没再失败）",
              st["failed"] == NEED_FIRST_HAND and st["ok"] > 0,
              f"failed={st['failed']} ok={st['ok']}")
        check("S8 第一局只弹了一次窗", len(r1["stalls"]) == 1, f"{len(r1['stalls'])} 次")

        print()
        print("=" * 78)
        print(f"局二：点「再试一次」但又失败一手（{NEED_FIRST_HAND} 次）→ **第二次弹窗** → 点「跳过」")
        print("=" * 78)
        r2 = await one_game(NEED_TWO_HANDS, ["retry", "skip"], "retry2")
        check("S9 反复重试仍然失败 → 又弹了一次窗（可以反复重试）",
              len(r2["stalls"]) >= 2, f"{len(r2['stalls'])} 次")
        check("S10 弹窗里能看到'第几次卡住'（stalled 字段）",
              all(s.get("stalled") for s in r2["stalls"]),
              str([s.get("stalled") for s in r2["stalls"]]))
        check("S11 第二次选了「跳过」→ stall_cleared 里能看到 retry 和 skip",
              r2["cleared"][:2] == ["retry", "skip"], str(r2["cleared"]))
        st2 = fake_stats()
        check(f"S12 失败次数累积到 {NEED_TWO_HANDS}（两次各 {NEED_FIRST_HAND} 次）",
              st2["failed"] >= NEED_TWO_HANDS, f"failed={st2['failed']}")
        check("S13 跳过之后对局照常打完", len(r2["ends"]) == 1, str(r2["ends"][:1]))
    finally:
        srv.stop()
        fake.stop()
        print("\n已停掉假上游与测试服务。")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
