# coding=utf-8
"""用 WebSocket 自动打一局，用来验证「手机端」这条链路（不需要真人点按钮）。

跑法（先起着 server.py）：
    python tools/ws_autoplay.py --seat 1 --mock
它做的事和手机页面完全一样：连 WS → 请求开局 → 收到 ask 就按规则自动回一个动作 → 直到 end。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

import websockets
from websockets.exceptions import WebSocketException   # 包级懒加载不暴露 websockets.exceptions

# Windows 控制台默认 GBK，打印 🏁 会 UnicodeEncodeError 打断脚本
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass


def auto_action(ask: dict) -> dict:
    kind = ask.get("kind")
    legal = ask.get("legal") or []
    pick = legal[0] if legal else None
    if kind == "speech":
        return {"speech": "（脚本代打）我先听着，感觉前面有人急着推票。",
                "claim": "无", "suspect": [], "reason": "自动脚本"}
    if kind == "wolf_talk":
        return {"speech": "（脚本代打）我跟票。", "target": pick, "reason": "自动脚本"}
    if kind == "witch":
        return {"save": False, "poison": None, "reason": "自动脚本"}
    if kind == "hunter_shoot":
        # 以前这里和 guard 共用一行、恒返回 shoot=None，于是自动打局永远走不到"猎人开枪"那条路径。
        # 现在真的开一枪，让这条链路也能被覆盖。
        return {"shoot": pick, "reason": "自动脚本（开枪打一个合法目标）"}
    if kind == "guard":
        return {"target": pick, "reason": "自动脚本"}
    # ---- 警长 / 遗言（11 人及以上才有）----
    # ⚠️ 这几条以前落在下面的通用分支里：引擎读的是 `run` / `speech` / `vote` / `to`，
    #    而通用分支回的是 `target` —— 等于这些链路在自动打局里**根本没被走到**
    #    （不报错，所以看不出漏；HANDOFF §7 却把它列成验证配方之一）。
    if kind == "sheriff_run":
        return {"run": True, "reason": "自动脚本（上警）"}
    if kind == "sheriff_speech":
        return {"speech": "（脚本代打）我上警：我会带队排狼。", "claim": "无",
                "reason": "自动脚本"}
    if kind == "sheriff_vote":
        return {"vote": pick, "reason": "自动脚本"}
    if kind == "last_words":
        return {"speech": "（脚本代打）这是我的遗言：好人跟我走。", "claim": "无"}
    if kind == "badge":
        return {"to": pick, "reason": "自动脚本（传警徽）"}
    field = {"vote": "vote", "wolf_kill": "target", "seer_check": "target"}.get(kind, "target")
    return {field: pick, "reason": "自动脚本"}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8077/ws")
    parser.add_argument("--token", default="", help="配对码；不填就自动读项目里的 配对码.txt")
    parser.add_argument("--seat", type=int, default=1)
    parser.add_argument("--board", default="eleven_wolfking",
                        help="eleven_wolfking/twelve_standard/nine_all/six_all_reveal（或 config 里的自定义板子）")
    parser.add_argument("--mock", action="store_true", default=True)
    parser.add_argument("--real", action="store_true", help="用真实模型跑（会花钱）")
    parser.add_argument("--abandon-after", type=int, default=0,
                        help="轮到我第 N 次行动时，按「放弃本局」，用来测这个按钮")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    asked = {"count": 0}
    entries = {"count": 0}
    flag = {"started": False, "abandoned": False}

    # WS 现在也要配对码（不然任何网页都能连上来看你的底牌、替你出牌）。
    # 不传 --token 就自动读项目里的 配对码.txt。
    token = args.token.strip().upper()
    if not token:
        code_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "配对码.txt")
        try:
            text = open(code_path, encoding="utf-8-sig").read()
            m = re.search(r"[A-Z0-9]{6}", text, re.I)
            token = (m.group().upper() if m else "")
        except OSError:
            token = ""
    if not token:
        print("❌ 没拿到配对码：服务没起过，或者用 --token 手动指定")
        return 1
    sep = "&" if "?" in args.url else "?"
    url = f"{args.url}{sep}token={token}"

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:

        async def pump():
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "status" and not msg.get("running") and not flag["started"]:
                    flag["started"] = True
                    print(f"→ 开局：板子={args.board}，mock={not args.real}，我坐 {args.seat} 号")
                    await ws.send(json.dumps({"type": "start", "mock": not args.real,
                                              "human_seat": args.seat, "board": args.board,
                                              "seed": 7}))
                elif kind == "you":
                    print(f"→ 我的身份：{msg['role']}（{msg['camp']}）"
                          + (f"，狼队友 {msg['mates']}" if msg.get("mates") else ""))
                elif kind == "entry":
                    entries["count"] += 1
                    e = msg["entry"]
                    who = f"{e['seat']}号：" if e.get("seat") else ""
                    print(f"   [{e['day']}天·{e['kind']}] {who}{e['text']}")
                elif kind == "ask":
                    asked["count"] += 1
                    if args.abandon_after and not flag["abandoned"] \
                            and asked["count"] >= args.abandon_after:
                        flag["abandoned"] = True
                        print(f"→ 第 {asked['count']} 次轮到我，模拟点击「放弃本局」")
                        await ws.send(json.dumps({"type": "abandon"}))
                        continue
                    payload = auto_action(msg)
                    print(f"→ 轮到我（{msg['kind']}），自动回：{payload}")
                    await ws.send(json.dumps({"type": "action", "payload": payload}))
                elif kind == "end":
                    print(f"\n🏁 结束：{msg['winner']}（第 {msg['day']} 天）"
                          + ("，标记为放弃" if msg.get("abandoned") else "")
                          + f"，收到 {entries['count']} 条消息，轮到我 {asked['count']} 次")
                    print(f"   存档：{msg.get('save')}")
                    return 0
                elif kind == "error":
                    print(f"❌ 服务端报错：{msg['text']}")
                    return 1
        return await asyncio.wait_for(pump(), timeout=args.timeout)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except asyncio.TimeoutError:
        print("超时：对局没在预期时间内结束")
        raise SystemExit(1)
    except (OSError, WebSocketException) as exc:
        print(f"连不上服务：{exc}")
        print("先起服务：python server.py")
        raise SystemExit(1)
