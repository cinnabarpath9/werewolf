# coding=utf-8
"""走手机那条链路，把语音从头验到尾。

它做的事和手机浏览器完全一样：
  1. 用配对码连 WebSocket，开一局（默认 mock，不花钱）
  2. 收到每一条"该配音的发言"（entry.say = true）就去 /api/tts 取音频
     —— 这正是手机端播放的逻辑
  3. 把取回来的音频丢给 /api/asr，看能不能听回原话（TTS→ASR 往返）
  4. 顺便统计：多少条、多少字节、每句多久、缓存有没有命中

为什么要用它：语音这块的坑全在"接口形状"上（TTS 要用 tts_v2、ASR 要 data URI、
音色和模型绑定）。换 key、换音色、换服务之后跑一遍这个，比开一局人肉听快得多。

跑法：
    python tools/voice_check.py                 # 默认 mock 局，验链路
    python tools/voice_check.py --seat 6 --board eleven_standard
    python tools/voice_check.py --no-asr        # 只验 TTS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass

import httpx  # noqa: E402
import websockets  # noqa: E402
# ⚠️ 必须显式导入：`websockets.exceptions` 在这个版本里不是自动可见的属性，
# 写在 except 子句里会在**抛异常那一刻**才炸成 AttributeError，
# 结果 --help 都会崩、连不上服务时的友好提示也永远不生效（踩过两次）。
from websockets.exceptions import WebSocketException  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_token(given: str) -> str:
    if given.strip():
        return given.strip().upper()
    try:
        text = open(os.path.join(HERE, "配对码.txt"), encoding="utf-8-sig").read()
        m = re.search(r"[A-Z0-9]{6}", text, re.I)
        return m.group().upper() if m else ""
    except OSError:
        return ""


def auto_action(msg: dict) -> dict:
    """轮到我时随便给个合法动作（只为了让对局能往下走）。

    ⚠️ 字段名必须跟引擎一致，而且**每一种提问都要认**：落在最后那个通用分支上的种类
    会回一个引擎不读的字段 → 那条链路在"手机+语音"模式里等于永远没被走到。
    2026-10-06 补齐警长/遗言/警徽这 5 种（`ws_autoplay.py` 已修，这个脚本当时漏了）。
    """
    legal = [int(x) for x in (msg.get("legal") or [])]
    pick = legal[0] if legal else None
    kind = msg.get("kind", "")
    if kind == "speech":
        return {"speech": "我是普通村民，昨晚没什么信息，先听听后面的人怎么说。"}
    if kind == "sheriff_speech":
        return {"speech": "我上警：我会带队排狼。"}
    if kind == "last_words":
        return {"speech": "这是我的遗言：好人跟我走。"}
    if kind == "sheriff_run":
        return {"run": True}
    if kind == "witch":
        # ⚠️ 字段名必须跟引擎一致：引擎读的是 `save`（不是 heal）。
        # 写错的话这条路永远"不救"，解药分支一辈子验不到。
        return {"save": False, "poison": None}
    if kind == "hunter_shoot":
        return {"shoot": pick} if pick else {"shoot": None}
    if kind == "badge":
        return {"to": pick} if pick is not None else {"to": None}
    if kind == "wolf_talk":
        # ⚠️ 狼队讨论要求回 speech（引擎 required=("speech",)）。只回一个数字字段的话，
        #    引擎会兜底成"（我这轮没意见，跟队友走。）"—— 这条链路等于永远没被走到，
        #    而且不报错（本文件第 60 行自己写的就是这个教训，上一版漏了 wolf_talk）。
        return {"speech": "先刀一个看起来最像神的。", "target": pick}
    field = {"vote": "vote", "sheriff_vote": "vote", "wolf_kill": "target",
             "seer_check": "target", "guard": "target"}.get(kind, "target")
    return {field: pick} if pick is not None else {}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8077/ws")
    ap.add_argument("--token", default="")
    ap.add_argument("--seat", type=int, default=6)
    ap.add_argument("--board", default="")
    ap.add_argument("--no-asr", action="store_true", help="只验 TTS，不做往返识别")
    ap.add_argument("--max-clips", type=int, default=6, help="最多验几条发言（省时间/省钱）")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    token = read_token(args.token)
    if not token:
        print("❌ 没拿到配对码：先起服务（python server.py），或用 --token 指定")
        return 1
    http = args.url.replace("wss://", "https://").replace("ws://", "http://")
    http = http.split("/ws")[0]
    sep = "&" if "?" in args.url else "?"
    ws_url = f"{args.url}{sep}token={token}"
    headers = {"X-Wolf-Client": "panel"}

    stats = {"say": 0, "fetched": 0, "bytes": 0, "secs": 0.0, "cached": 0, "err": 0}
    first_clip: dict = {}
    started = {"done": False}
    print(f"连 {ws_url}")
    print(f"HTTP 取音频走 {http}/api/tts")
    print("-" * 74)

    async with httpx.AsyncClient(timeout=180) as hc, \
            websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:

        async def pump():
            nonlocal first_clip          # 没有这行，下面赋值会让它变成局部变量
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "status" and not msg.get("running") and not started["done"]:
                    started["done"] = True
                    payload = {"type": "start", "mock": True, "human_seat": args.seat, "seed": 21}
                    if args.board:
                        payload["board"] = args.board
                    print(f"→ 开局（我坐 {args.seat} 号）")
                    await ws.send(json.dumps(payload))
                elif kind == "status" and msg.get("running") and not started["done"]:
                    # 上一局还没结束（比如上次这个脚本中途崩了）——先把那局收掉，
                    # 否则新局开不起来，脚本会一直干等。
                    print("→ 有一局还在进行，先把它收掉（等于点「放弃本局」）")
                    await ws.send(json.dumps({"type": "abandon"}))
                elif kind == "you":
                    print(f"→ 我的身份：{msg['role']}（{msg['camp']}）")
                elif kind == "entry":
                    e = msg["entry"]
                    if not e.get("say"):
                        continue
                    stats["say"] += 1
                    if stats["fetched"] >= args.max_clips:
                        continue
                    who = f"{e['seat']}号"
                    t0 = time.time()
                    r = await hc.get(f"{http}/api/tts", headers=headers,
                                     params={"token": token, "n": e.get("n", -1)})
                    dt = time.time() - t0
                    if r.status_code == 200 and r.content:
                        stats["fetched"] += 1
                        stats["bytes"] += len(r.content)
                        stats["secs"] += dt
                        if dt < 0.4:
                            stats["cached"] += 1
                        tag = "（缓存）" if dt < 0.4 else ""
                        print(f"   🔊 {who} {len(r.content):>7}B {dt:5.1f}s {tag}  {e['text'][:34]}…")
                        if not first_clip:
                            first_clip = {"audio": r.content, "text": e["text"], "seat": e["seat"]}
                            # 立刻再取一次，验证缓存
                            t1 = time.time()
                            r2 = await hc.get(f"{http}/api/tts", headers=headers,
                                              params={"token": token, "n": e.get("n", -1)})
                            if r2.status_code == 200 and time.time() - t1 < 0.4:
                                stats["cached"] += 1
                    else:
                        stats["err"] += 1
                        try:
                            why = r.json().get("error")
                        except Exception:             # noqa: BLE001
                            why = (r.text or "")[:100]
                        print(f"   ❌ {who} HTTP {r.status_code} {why}")
                elif kind == "ask":
                    await ws.send(json.dumps({"type": "action", "payload": auto_action(msg)}))
                elif kind == "end":
                    print("-" * 74)
                    print(f"🏁 对局结束：{msg['winner']}（第 {msg['day']} 天）")
                    return 0
                elif kind == "error":
                    print(f"❌ 服务端报错：{msg['text']}")
                    return 1
            return 0

        try:
            rc = await asyncio.wait_for(pump(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("（对局没在超时前结束，但语音已经验过，继续看结果）")
            rc = 0

        print()
        print("=" * 74)
        print(f"TTS：该配音的发言 {stats['say']} 条，实取 {stats['fetched']} 条，"
              f"共 {stats['bytes'] / 1024:.0f} KB，耗时 {stats['secs']:.1f}s"
              f"（平均 {stats['secs'] / max(1, stats['fetched']):.1f}s/句），"
              f"失败 {stats['err']} 条，缓存命中 {stats['cached']} 次")
        if stats["say"] == 0:
            print("⚠️ 这一局没有产生可配音的发言，换个板子或加大 --timeout 再试")
        if stats["fetched"] and not args.no_asr and first_clip:
            print()
            print("ASR 往返（把刚才合成的音频再听写回来）：")
            t0 = time.time()
            r = await hc.post(f"{http}/api/asr", headers={**headers, "Content-Type": "audio/mpeg"},
                              params={"token": token, "legal": "1,2,3,4,5,7,8,9,10"},
                              content=first_clip["audio"])
            dt = time.time() - t0
            if r.status_code == 200:
                j = r.json()
                got = (j.get("text") or "").strip()
                print(f"   原文：{first_clip['text'][:70]}")
                print(f"   识别：{got[:70]}   （{dt:.1f}s）")
                core = re.sub(r"[^\w\u4e00-\u9fff]", "", first_clip["text"])[:8]
                hit = core and core in re.sub(r"[^\w\u4e00-\u9fff]", "", got)
                print(f"   {'✅ 往返一致，语音链路通' if hit else '⚠️ 识别结果与原文有差异，看上面两行'}")
                if j.get("seat"):
                    print(f"   （顺带解析出的号码：{j['seat']}）")
            else:
                print(f"   ❌ HTTP {r.status_code} {r.text[:150]}")
                stats["err"] = stats.get("err", 0) + 1
    # ⚠️ 退出码要如实反映"语音链路到底通没通"：以前不管 TTS 全挂还是压根没配到音，
    #    最后都 return 0 —— CI 里等于白过。
    if stats.get("err"):
        print(f"\n❌ 有 {stats['err']} 处语音错误，这一轮不算通过。")
        return 1
    if stats.get("say") == 0:
        print("\n❌ 这一局没有任何发言需要配音，语音链路没被真正验证过。")
        return 1
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (OSError, WebSocketException) as exc:
        print(f"连不上服务：{exc}\n先起服务：python server.py")
        raise SystemExit(1)
