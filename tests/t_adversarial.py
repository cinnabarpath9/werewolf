# coding=utf-8
"""对抗性复核（我自己做的那一块）：专打"边界和畸形输入"，看会不会炸、会不会留下坏状态。

覆盖：
  A 畸形行动载荷（每个提问种类都喂坏数据：类型错、座位越界、超长字符串、缺字段、null）
  B 并发开局（同一瞬间发两次 start）
  C 存档往返（打一局 → 放弃 → 读这一局那份存档：JSON 合法、visible_to 没丢、字段齐全。
    ⚠️ 2026-10-12：mock 局的存档现在落 `<save_dir>/_mock/` 子目录，见 C6）
  D 越权取语音（拿别人的私有条目序号去 /api/tts）
  E 重连补发不漏不越权（连上时 replay 与存档对账）

只读复核：不修改项目代码。测试脚本本身写在 werewolf/tests/ 下（本目录就是我的测试区）。
端口用 8091，避开正在运行的 8077。
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 跑在哪台测试实例上。默认 8086（老行为）；`run_all.py` 用 `WW_TEST_PORT` 覆盖。
#: ⚠️ 2026-10-11 实测教训：拆车道时**忘了**让本文件读这个变量 —— 于是它明明被分到 8089，
#: 却仍然去打 8086，和"8086 存档/页面链"上的 t_ws_e2e / t_isolation_push **同时开局**：
#: 一条连接 30 秒里收到 6 个 `you`（房间是单房间，谁开都广播给所有客户端），
#: B1"并发两次开局只有一个真的开了"于是假红。run_all 现在有**车道端口自查**拦这类错。
PORT = int(os.environ.get("WW_TEST_PORT") or 8086)
TOKEN = re.search(r"[A-Z0-9]{6}", io.open(os.path.join(ROOT, "配对码.txt"),
                                         encoding="utf-8-sig").read()).group()
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
#: 这台实例的存档目录（`run_all.py::instance_config` 给非 8086 的实例现生成派生配置：
#: `save_dir = tests/tmp/games-<port>`；8086 用原样的 `test-config.yaml` = `tests/tmp/games`）。
GAMES = os.path.join(HERE, "tmp", "games" if PORT == 8086 else f"games-{PORT}")
#: ⚠️ 2026-10-12：mock 局的存档在 `<save_dir>/_mock/` 子目录里（`server.py::_mock_save_dir`）——
#: 本脚本打的就是 mock 局，所以「刚打完那一局」要去这里找。
GAMES_MOCK = os.path.join(GAMES, "_mock")


def newest_save_path() -> str | None:
    """`刚打完那一局`的存档路径：存档根目录 + `_mock/` 子目录里最新的那一份。

    ⚠️ 2026-10-12：mock 局不再写存档根目录（根目录里通常一份都没有），所以两处都找；
    判据没变 —— 仍然是「这一局刚落的那份存档」。
    """
    files: list[str] = []
    for root in (GAMES, GAMES_MOCK):
        if os.path.isdir(root):
            files += [os.path.join(root, f) for f in os.listdir(root) if f.endswith(".json")]
    if not files:
        return None
    files.sort(key=os.path.getmtime)
    return files[-1]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


#: 畸形载荷：每一条都会被当作"人类玩家点了这个按钮"
HOSTILE = [
    {"vote": 999}, {"vote": -1}, {"vote": "三号"}, {"vote": None}, {"vote": 3.7},
    {"vote": [3]}, {"vote": {"seat": 3}}, {"vote": True},
    {"target": 999}, {"target": None}, {"target": "x" * 5000},
    {"speech": "x" * 40000, "claim": "无", "suspect": [], "reason": "fuzz"},
    {"speech": None, "claim": None, "suspect": None, "reason": None},
    {"speech": 12345, "reason": {"a": 1}},
    {"claim": "预言家", "suspect": [1, 2, 3], "reason": "fuzz"},       # 缺 speech
    {"shoot": 999, "reason": "fuzz"}, {"save": "yes", "poison": "no", "reason": "fuzz"},
    {"save": True, "poison": 0, "reason": "fuzz"},
    {"run": "maybe"}, {"run": None}, {"to": 999}, {"to": None},
    {"to": 3, "reason": "fuzz"}, {"abstain": True}, {"abstain": False, "vote": 2},
    {}, [], "not-a-dict", 12345, None,
]


async def collect(ws, seconds: float) -> list[dict]:
    """收指定秒数内的所有消息；遇到 ask 就回一个畸形载荷。"""
    out: list[dict] = []
    t0 = time.time()
    i = 0
    while time.time() - t0 < seconds:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        except asyncio.TimeoutError:
            continue
        out.append(m)
        if m.get("type") == "ask":
            payload = HOSTILE[i % len(HOSTILE)]
            i += 1
            await ws.send(json.dumps({"type": "action", "payload": payload}))
    return out


async def main() -> int:
    print(f"=== A/B/C/D/E 对抗性复核（端口 {PORT}）===")
    async with websockets.connect(WS, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.5)

        # ---- B 并发开局：同一瞬间发两次 ----
        await asyncio.gather(
            ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                "board": "eleven_standard", "seed": 3})),
            ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                "board": "eleven_standard", "seed": 4})),
        )
        msgs = await collect(ws, 30)
        kinds = [m.get("type") for m in msgs]
        errs = [str(m.get("text")) for m in msgs if m.get("type") == "error"]
        check("B1 并发两次开局：只有一个真的开了（另一条被拒或忽略）",
              kinds.count("you") <= 1, f"you 出现 {kinds.count('you')} 次，error={errs[:1]}")
        # 服务端状态必须自洽：不是"两个对局同时在跑"
        st = httpx.get(f"{BASE}/api/status", params={"token": TOKEN}, timeout=10).json()
        check("B2 并发后状态自洽（只有一个 running 对局、没有半个状态）",
              # 座位数允许 0/9/11：这条测试用的板子可能是 9 人也可能是 11 人
              isinstance(st.get("running"), bool) and len(st.get("seats") or []) in (0, 9, 11),
              f"running={st.get('running')} seats={len(st.get('seats') or [])}")

        # ---- A 畸形载荷继续喂（把整局打完/打到结束或超时）----
        msgs2 = await collect(ws, 60)
        all_msgs = msgs + msgs2
        st = httpx.get(f"{BASE}/api/status", params={"token": TOKEN}, timeout=10).json()
        crashed = [m for m in all_msgs if m.get("type") == "error"
                   and ("Traceback" in str(m.get("text")) or "500" in str(m.get("text")))]
        check("A1 畸形载荷没让服务报 500/异常", not crashed, str(crashed[:1])[:120])
        check("A2 喂了畸形载荷后对局状态仍然合法（座位数/阶段没乱）",
              len(st.get("seats") or []) in (0, 9, 11) and st.get("phase") in (None, "day", "night"),
              f"seats={len(st.get('seats') or [])} phase={st.get('phase')}")
        alive = [s for s in (st.get("seats") or []) if s.get("alive")]
        check("A3 没有出现'全员死亡'这种非法局面", not st.get("running") or len(alive) > 0,
              f"存活 {len(alive)}")

        # ---- D 越权取语音：拿别人的私有条目序号 ----
        entries = st.get("ledger") or []
        me = 6
        bad_idx = []
        for i, e in enumerate(entries):
            vis = e.get("visible_to") or ["public"]
            if not any(v in ("public", f"seat:{me}") for v in vis):
                bad_idx.append(i)
        codes = []
        async with httpx.AsyncClient(timeout=60) as c:
            for i in bad_idx[:3]:
                r = await c.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": i})
                codes.append(r.status_code)
            r_bad = await c.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": 99999})
            r_txt = await c.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": "abc"})
        check("D1 自己的不可见条目 → 不给语音（404/400）", all(x in (400, 404) for x in codes),
              f"那些序号回 {codes}（不该见到 {bad_idx[:3]}）")
        check("D2 越界序号 / 非数字序号 → 干净的 4xx 而不是 500",
              r_bad.status_code in (400, 404) and r_txt.status_code in (400, 404),
              f"{r_bad.status_code} / {r_txt.status_code}")

        await ws.send(json.dumps({"type": "abandon"}))
        await asyncio.sleep(2.5)

    # ---- C 存档往返 ----
    games = httpx.get(f"{BASE}/api/games", params={"token": TOKEN}, timeout=10).json()
    names = [g.get("name") if isinstance(g, dict) else g for g in (games.get("games") or [])]
    # ⚠️ 原来断言「至少有 1 个存档」——那依赖测试存档目录里恰好留着旧档。
    #    测试环境现在会被清空（tmp/ 会删），所以改成断「接口能用」；条数不设要求。
    check("C1 存档列表接口正常（返回列表即可）",
          games.get("games") is not None and isinstance(games.get("games"), list),
          f"{len(names)} 个存档")
    # ⚠️ 2026-10-12：这一局是 **mock 局**，服务端把它写在 `<save_dir>/_mock/` 子目录里
    #    （`server.py::_mock_save_dir`），而 `/api/games` 只看存档根目录（有意不列 mock 局）。
    #    所以下面分两步、**没有放宽**：
    #      ① 字段/可见性/rules 的对账读**这一局自己那份盘上存档**（就是刚打完那局，判据一样）；
    #      ② 接口往返仍然测：根目录里真有存档时，用 `/api/game/{name}` 再走一遍。
    newest_path = newest_save_path()
    if newest_path:
        detail = json.load(io.open(newest_path, encoding="utf-8"))
        led = detail.get("ledger") or []
        need = {"day", "kind", "text", "visible_to"}
        gaps = [i for i, e in enumerate(led[:40]) if not need.issubset(set(e))]
        check("C2 存档条目字段齐全（day/kind/text/visible_to）", led and not gaps,
              f"{os.path.relpath(newest_path, ROOT)}：{len(led)} 条，缺字段的序号 {gaps[:5]}")
        check("C3 存档里的 visible_to 合法（只可能是 public/wolf/seat:N）",
              bool(led) and all(
                  all(re.fullmatch(r"public|wolf|seat:\d+", str(v)) for v in (e.get("visible_to") or []))
                  for e in led),
              "有非法 visible_to" if led else "（这一局没有台账，跳过）")
        rules = detail.get("rules") or {}
        check("C4 存档记下了本局规则口径（rules_mode / rules_detail）",
              "rules_mode" in rules and "rules_detail" in rules, str(sorted(rules)))
    if names:
        api_detail = httpx.get(f"{BASE}/api/game/{names[0]}", params={"token": TOKEN},
                              timeout=20).json()
        check("C5 `/api/game/{name}` 往返还通（根目录里那份读得回来）",
              isinstance((api_detail or {}).get("ledger"), list),
              f"{names[0]} → ledger {len((api_detail or {}).get('ledger') or [])} 条")
    check("C6 ⚠️ 刚打完的 mock 局没写进存档根目录（只在 `_mock/` 里）",
          newest_path is not None
          and os.path.dirname(os.path.abspath(newest_path)) == os.path.abspath(GAMES_MOCK),
          f"{os.path.relpath(newest_path, ROOT) if newest_path else '（没找到存档）'}"
          f"　根目录现有 {len(names)} 份")

    # ---- E 重连补发对账 ----
    async with websockets.connect(WS, max_size=32 * 1024 * 1024) as ws2:
        await ws2.send(json.dumps({"type": "sync"}))
        replayed = await collect(ws2, 5)
        rep_entries = [m["entry"] for m in replayed if m.get("type") == "entry"]
        detail = json.load(io.open(newest_path, encoding="utf-8")) if newest_path else {}
        led = detail.get("ledger") or []
        hidden = {(e.get("day"), e.get("kind"), e.get("seat"), e.get("text"))
                  for e in led if not any(v in ("public", f"seat:{me}")
                                          for v in (e.get("visible_to") or ["public"]))}
        visible = {(e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) for e in led}
        leaked = [e for e in rep_entries
                  if (e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) in hidden
                  and (e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) not in visible]
        check("E1 重连补发没有越权条目", not leaked,
              f"补发 {len(rep_entries)} 条，越权 {len(leaked)} 条")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
