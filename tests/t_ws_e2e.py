# coding=utf-8
"""走真实 WS 链路打一整局 mock（收尾窗口/结局广播/中途重连补发/人类弃权）。

对 8086 上的临时测试实例打（临时 save_dir、mock 代理、不花模型钱）。
跑法： python t_ws_e2e.py
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
from websockets.exceptions import WebSocketException   # 包级懒加载不暴露这个

PORT = 8086
# ⚠️ 默认板子不能写已退役的板子（2026-10-30 起产品只留四张：
#    eleven_wolfking / twelve_standard / nine_all / six_all_reveal）。写错的话，不带参数跑
#    本脚本会直接开局失败（报「没有这个板子」），看起来像服务坏了。默认跟 run_all.py 传的一致。
BOARD = sys.argv[1] if len(sys.argv) > 1 else "eleven_wolfking"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "games")
#: ⚠️ 2026-10-12：本脚本打的是 **mock 局**，服务端把它写在 `<save_dir>/_mock/` 子目录里
#: （`server.py::_mock_save_dir`）—— E5b / E11 两条断言盯着这件事。
GAMES_MOCK = os.path.join(GAMES, "_mock")
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


def reply_for(ask: dict, first_vote_seen: list[bool]) -> dict:
    kind = ask.get("kind")
    legal = ask.get("legal") or []
    if kind == "speech":
        return {"speech": "（自动测试）我过，先听后面的。", "claim": "无", "suspect": [],
                "reason": "e2e 自动测试"}
    if kind == "wolf_talk":
        return {"speech": "（自动测试）跟队友走。", "target": legal[0] if legal else None,
                "reason": "e2e 自动测试"}
    if kind == "witch":
        return {"save": False, "poison": None, "reason": "e2e 自动测试"}
    # --- 警长 / 遗言（11 人及以上才会出现）---
    if kind == "sheriff_run":
        return {"run": True, "reason": "e2e：上警试试"}
    if kind == "sheriff_speech":
        return {"speech": "（自动测试）我上警：我会带队排狼。", "claim": "无",
                "reason": "e2e 自动测试"}
    if kind == "sheriff_vote":
        return {"vote": legal[0]} if legal else {"abstain": True, "reason": "e2e"}
    if kind == "last_words":
        return {"speech": "（自动测试）这是我的遗言：好人跟我走。", "claim": "无"}
    if kind == "badge":
        return {"to": legal[0]} if legal else {"to": None, "reason": "e2e：撕掉"}
    if kind == "vote" and not first_vote_seen[0]:
        first_vote_seen[0] = True
        return {"abstain": True, "reason": "e2e：故意弃权，验证'尊重人类弃权'"}
    if kind in ("wolf_kill", "seer_check", "guard", "hunter_shoot"):
        return {"target": legal[0] if legal else None, "reason": "e2e 自动测试"}
    return {"abstain": True, "reason": "e2e 自动测试"}


async def main() -> int:
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    msgs: list[dict] = []
    print(f"连 {url}（板子 {BOARD}）")
    # ⚠️ 存档目录可能还不存在（run_all.py 收尾会删掉 tests/tmp，之后单独跑本脚本就会
    #    FileNotFoundError —— 看起来像服务坏了，其实只是没人建这个目录）。
    os.makedirs(GAMES, exist_ok=True)
    os.makedirs(GAMES_MOCK, exist_ok=True)
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "sync"}))
            n_before = len([f for f in os.listdir(GAMES) if f.endswith(".json")])
            n_before_mock = len([f for f in os.listdir(GAMES_MOCK) if f.endswith(".json")])
            await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 1,
                                      "board": BOARD, "seed": 11}))

            # 不是你的回合也发一手：应当被明确拒绝（而不是默默吞掉）
            await ws.send(json.dumps({"type": "action", "payload": {"vote": 2}}))

            first_vote = [False]
            resync_done = False
            deadline = time.time() + 180
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(raw)
                msgs.append(m)
                if m.get("type") == "ask":
                    await ws.send(json.dumps({"type": "action",
                                              "payload": reply_for(m, first_vote)}))
                if (not resync_done and m.get("type") == "entry"
                        and len([x for x in msgs if x.get("type") == "entry"]) == 5):
                    # 中途"切走再回来"：另开一条连接，看能不能补发历史
                    resync_done = True
                    asyncio.get_running_loop().create_task(resync_probe())
                if m.get("type") == "end":
                    # 继续收 4 秒，把 end 后面那几条收尾 status 收干净（不然 E2/E3 会误判）
                    t1 = time.time()
                    while time.time() - t1 < 4:
                        try:
                            msgs.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0)))
                        except asyncio.TimeoutError:
                            break
                    break
            else:
                check("E0 一局在 180 秒内打完", False, "超时")
    except (WebSocketException, OSError) as exc:
        check("E0 能连上 WS", False, f"{type(exc).__name__}: {exc}")
        return finish()

    kinds = [m.get("type") for m in msgs]
    check("E1 一局打完了（收到 end）", "end" in kinds, f"消息类型统计={ {k: kinds.count(k) for k in set(kinds)} }")
    end_at = next((i for i, m in enumerate(msgs) if m.get("type") == "end"), None)
    if end_at is not None:
        after = [m for m in msgs[end_at:] if m.get("type") == "status"]
        check("E2 end 之后的 status 里没有 running=True（收尾窗口不再骗人）",
              all(m.get("running") is False for m in after) and bool(after),
              f"{[(m.get('running'), m.get('cleaning')) for m in after]}")
        fin = [m for m in msgs if m.get("type") == "status"][-1]
        check("E3 最后一条 status 是 running=False + cleaning=False（回到开局面板）",
              fin.get("running") is False and fin.get("cleaning") is False,
              f"running={fin.get('running')} cleaning={fin.get('cleaning')}")
        m_end = msgs[end_at]
        check("E4 end 里带 winner/day/save/abandoned", "winner" in m_end and "day" in m_end,
              json.dumps({k: m_end.get(k) for k in ("winner", "day", "abandoned", "save")},
                         ensure_ascii=False)[:200])
        check("E5 存档落在**测试目录**里，没写进用户的 games/",
              bool(m_end.get("save")) and os.path.abspath(GAMES).lower() in
              os.path.abspath(str(m_end.get("save"))).lower(), str(m_end.get("save")))
        check("E5b ⚠️ 这一局是 mock 局 → 存档落在 `_mock/` 子目录（不挤真局的保留窗口）",
              bool(m_end.get("save")) and os.path.abspath(GAMES_MOCK).lower() in
              os.path.abspath(str(m_end.get("save"))).lower(), str(m_end.get("save")))

    you = [m for m in msgs if m.get("type") == "you"]
    check("E6 收到 you（座位/身份/阵营）", bool(you), json.dumps(you[0], ensure_ascii=False)[:120] if you else "")
    entries = [m for m in msgs if m.get("type") == "entry"]
    check("E7 台账条目有推给手机", len(entries) > 10, f"{len(entries)} 条")
    asks = [m for m in msgs if m.get("type") == "ask"]
    check("E8 至少被问过一次行动（人类座位真的在参与）", bool(asks),
          f"{len(asks)} 次；kinds={[a.get('kind') for a in asks]}")
    notes = [str(m.get("text")) for m in msgs if m.get("type") == "notice"]
    check("E9 不是你的回合乱发一手 → 明确提示'现在不是你行动的时间'",
          any("不是你行动的时间" in t for t in notes), str(notes)[:220])
    check("E10 弃权没有被服务端偷偷改成随机出票（弃权后被放进了台账/公告）",
          any(("弃权" in t) for t in notes) or any(
              "弃权" in json.dumps(m, ensure_ascii=False) for m in entries),
          "在公告/台账里找到了'弃权'字样")

    # --- 11 人及以上：警长竞选必须真的走到手机上（新面板的入口）---
    ask_kinds = [a.get("kind") for a in asks]
    if BOARD in ("eleven_wolfking", "twelve_standard"):
        check("E12 手机收到了警长竞选提问（sheriff_run）", "sheriff_run" in ask_kinds,
              f"收到过的行动类型={sorted(set(ask_kinds))}")
        check("E13 收到过竞选发言/投警长里的至少一种",
              ("sheriff_speech" in ask_kinds) or ("sheriff_vote" in ask_kinds),
              f"{sorted(set(ask_kinds))}")

    # --- 6 人明牌板：已出局座位要在 status 里带出亮出来的身份（手机顶栏靠它）---
    if BOARD == "six_all_reveal":
        lit = [d for m in msgs if m.get("type") == "status"
               for d in (m.get("dead") or []) if d.get("role")]
        check("E14 6 人明牌板：status.dead 里带出局者亮出的身份", bool(lit), str(lit[:4]))

    # ⚠️ 2026-10-12：mock 局的存档落 `<save_dir>/_mock/` 子目录（`server.py::_mock_save_dir`），
    #    所以"测试存档只增加在临时目录里"这条要**两个位置一起**看：`_mock/` +1、根目录不多。
    n_after = len([f for f in os.listdir(GAMES) if f.endswith(".json")])
    n_after_mock = len([f for f in os.listdir(GAMES_MOCK) if f.endswith(".json")])
    check("E11 测试存档只增加在临时目录的 `_mock/` 里（存档根目录一份都没多）",
          n_after_mock >= n_before_mock + 1 and n_after == n_before,
          f"_mock/ {n_before_mock} → {n_after_mock}；存档根目录 {n_before} → {n_after}")

    return finish()


async def resync_probe() -> None:
    """第二条连接 = 手机切走再回来：sync 之后应当补发历史条目。"""
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    got: list[dict] = []
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "sync"}))
            t0 = time.time()
            while time.time() - t0 < 4:
                try:
                    got.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5)))
                except asyncio.TimeoutError:
                    break
    except Exception as exc:                            # noqa: BLE001
        check("R1 第二条连接（=手机切回来）能连上并补发历史", False, f"{type(exc).__name__}: {exc}")
        return
    replayed = [m for m in got if m.get("type") == "entry"]
    check("R1 手机切走再回来：sync 后补发了历史台账（不会看起来像重开了一局）",
          len(replayed) >= 4, f"补发 {len(replayed)} 条，共收到 {len(got)} 条")


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
