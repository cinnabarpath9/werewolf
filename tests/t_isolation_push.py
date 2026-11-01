# coding=utf-8
"""信息隔离的**推送侧**复核：手机上收到的每一条台账，都必须是这个座位有权看到的。

verify_isolation.py 查的是"装进模型的信息包"，而代理 C 动过推送侧（_entry_msg / replay_history /
_drain_stale），所以这里换一条路：真人坐一个**好人**座位打完一局，把 WS 收到的东西和存档里
台账的 visible_to 逐条对账 —— 只要有一条 visible_to=[wolf] 或 [seat:N]（N≠我）的正文
出现在手机收到的消息里，就是 P0 泄漏。

跑法： python t_isolation_push.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

import websockets
from websockets.exceptions import WebSocketException   # noqa: E402

PORT = 8086
HERE = os.path.dirname(os.path.abspath(__file__))
GAMES = os.path.join(HERE, "tmp", "games")
#: ⚠️ 2026-10-12：8086 上打的是 **mock 局**，服务端把它写在 `<save_dir>/_mock/` 子目录里
#: （`server.py::_mock_save_dir`，不写存档根目录）—— 本脚本「拿本局存档对账」就要去这里找。
GAMES_MOCK = os.path.join(GAMES, "_mock")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = ""
for line in io.open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig"):
    m = re.search(r"[A-Z0-9]{6}", line)
    if m:
        TOKEN = m.group()
        break

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


def reply_for(ask: dict) -> dict:
    kind = ask.get("kind")
    legal = ask.get("legal") or []
    if kind == "speech":
        return {"speech": "（隔离测试）我过。", "claim": "无", "suspect": [], "reason": "isolation"}
    if kind == "wolf_talk":
        return {"speech": "（隔离测试）跟队友走。", "target": legal[0] if legal else None,
                "reason": "isolation"}
    if kind == "witch":
        return {"save": False, "poison": None, "reason": "isolation"}
    if kind in ("wolf_kill", "seer_check", "guard", "hunter_shoot"):
        return {"target": legal[0] if legal else None, "reason": "isolation"}
    return {"abstain": True, "reason": "isolation"}


async def play_one(seed: int) -> tuple[str, dict, list[dict], list[dict]] | None:
    """打一局；如果人类座位是狼就放弃换下一个种子（狼本来就该看到狼的私有信息）。

    ⚠️ 连上时房间还留着**上一局**的 game 对象，sync 会把上一局的台账按"上一局那个座位的阵营"
    补发一遍（上一局 1 号是狼，就会补发狼话）。所以这里先把连上后的补发单独收进 pre，
    'you' 事件之后再开始记账 —— 不然上一局的狼话会被误判成"新局泄漏"（我第一次就这么误判了）。
    """
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    entries: list[dict] = []
    early: list[dict] = []          # start 之后、you 之前收到的条目（新局的规则公示就在这里）
    pre: list[dict] = []
    you: dict = {}
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        # 先把上一局的补发收干净（最多 3 秒），这段时间收到的一律不算本局
        t_pre = time.time()
        while time.time() - t_pre < 3:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                break
            if m.get("type") == "entry":
                pre.append(m["entry"])
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 1,
                                  "board": "eleven_standard", "seed": seed}))
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            except asyncio.TimeoutError:
                continue
            t = m.get("type")
            if t == "you" and not you:
                you = m
                if "狼" in str(m.get("role")):
                    await ws.send(json.dumps({"type": "abandon"}))
                    return None
            elif t == "entry":
                # ⚠️ 开局那条【本局规则公示】是在 `you` **之前**推的（实测顺序：
                #    status → entry:规则公示 → notice → you）。只记 you 之后的条目，
                #    它就会被误判成"漏推"（2026-10-06 抓到的固定少 1 条假红）。
                #    这里先收进 early，等读到存档后**只保留确实属于本局的那几条**
                #    （拿 visible_keys 过滤，避免把上一局的补发算进来）。
                (entries if you else early).append(m["entry"])
            elif t == "ask":
                await ws.send(json.dumps({"type": "action", "payload": reply_for(m)}))
            elif t == "end":
                t1 = time.time()
                while time.time() - t1 < 3:
                    try:
                        mm = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                        if mm.get("type") == "entry":
                            entries.append(mm["entry"])
                    except asyncio.TimeoutError:
                        break
                break
    return f"seed={seed}", you, entries, pre, early


def newest_save() -> dict:
    """刚打完那一局的存档：存档根目录 + `_mock/` 子目录里最新的那一份。

    ⚠️ 2026-10-12：mock 局的存档不再写存档根目录（`server.py::_mock_save_dir`），
    所以两处都找 —— 判据没变，仍然是「这一局刚落的那份存档」。
    """
    files: list[str] = []
    for root in (GAMES, GAMES_MOCK):
        if os.path.isdir(root):
            files += [os.path.join(root, f) for f in os.listdir(root) if f.endswith(".json")]
    files.sort(key=os.path.getmtime)
    return json.load(io.open(files[-1], encoding="utf-8"))


def visible_for_human(entry: dict, seat_id: int, camp: str) -> bool:
    for who in (entry.get("visible_to") or ["public"]):
        if who == "public":
            return True
        if who == "wolf" and camp == "wolf":
            return True
        if who == f"seat:{seat_id}":
            return True
    return False


async def main() -> int:
    got = None
    for seed in (7, 8, 9, 10, 11, 12):
        print(f"—— 试 seed={seed}（要人类座位是好人，不然'狼能看到狼话'本来就合法）")
        try:
            got = await play_one(seed)
        except (WebSocketException, OSError) as exc:
            check("连通性", False, f"{type(exc).__name__}: {exc}")
            return 1
        if got:
            break
        print("   这个种子里 1 号是狼，换一个")
    if not got:
        check("拿到一局'人类是好人'的对局", False, "试了 6 个种子都是狼，放弃")
        return 1
    _label, you, entries, pre, early = got
    seat_id = int(you.get("seat") or 1)
    role = str(you.get("role") or "")
    camp = "wolf" if "狼" in role else "good"
    print(f"  人类座位：{seat_id}号 · {role}（camp={camp}）")
    print(f"  连上时补发的**上一局**台账 {len(pre)} 条（这部分按上一局那个座位的阵营过滤，")
    print(f"  所以上一局 1 号是狼时这里会看到狼话 —— 那是同一台手机自己打过的局，不算越权）")
    check("拿到一局'人类是好人'的对局", camp == "good", f"role={role}")

    save = newest_save()
    ledger = save.get("ledger") or []
    hidden = [e for e in ledger if not visible_for_human(e, seat_id, camp)]
    print(f"  存档台账 {len(ledger)} 条，其中这个座位**不该看到**的 {len(hidden)} 条")

    # 逐条对账：收到的 (day, kind, seat, text) 不允许出现在"不该看到"的集合里
    hidden_keys = {(e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) for e in hidden}
    visible_keys = {(e.get("day"), e.get("kind"), e.get("seat"), e.get("text"))
                    for e in ledger if visible_for_human(e, seat_id, camp)}

    # you 之前收到的那几条：只保留"确实在本局存档里、且这个座位有权看到"的
    # （这样既补上规则公示，又不会把上一局的补发误算进本局）
    kept = [e for e in early
            if (e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) in visible_keys]
    if kept:
        print(f"  补记 you 之前收到的 {len(kept)} 条（例如：{str(kept[0].get('text'))[:24]}…）")
    entries = entries + kept
    print(f"  本局最终记账 {len(entries)} 条")
    leaked = []
    for e in entries:
        k = (e.get("day"), e.get("kind"), e.get("seat"), e.get("text"))
        if k in hidden_keys and k not in visible_keys:
            leaked.append(e)

    check("P0 手机收到的台账里没有一条是'这个座位不该看到的'", not leaked,
          (f"泄漏 {len(leaked)} 条，例如：" + json.dumps(leaked[0], ensure_ascii=False)[:200])
          if leaked else f"对账了 {len(entries)} 条")

    # 反向：该看到的条目应该都推过来了（±回放上限）
    missing = visible_keys - {(e.get("day"), e.get("kind"), e.get("seat"), e.get("text"))
                              for e in entries}
    check("该看到的条目基本都推给了手机（缺的只可能是超过 400 条回放上限的老条目）",
          len(missing) <= max(0, len(visible_keys) - 400),
          f"缺 {len(missing)} 条 / 应见 {len(visible_keys)} 条")

    # 重连补发也不能越权
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    replayed: list[dict] = []
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        t0 = time.time()
        while time.time() - t0 < 4:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                break
            if m.get("type") == "entry":
                replayed.append(m["entry"])
    rleak = [e for e in replayed
             if (e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) in hidden_keys
             and (e.get("day"), e.get("kind"), e.get("seat"), e.get("text")) not in visible_keys]
    check("P0 重连补发（replay_history）也没有越权", not rleak,
          f"补发 {len(replayed)} 条，泄漏 {len(rleak)} 条" if rleak else f"补发 {len(replayed)} 条，无一越权")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
