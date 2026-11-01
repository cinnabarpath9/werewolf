# coding=utf-8
"""联机（多真人座位）回归：纯本地，不联网、不写项目文件。

守的是什么
----------
2026-10-31 加「一台主机跑服务，朋友用浏览器加入、各自坐一个座位」。它最容易静默坏的是：

  ① 引擎只认一个 `human_seat`：多个人坐下之后，第二个人被当成 AI（不提问、偷偷替他做决定）；
  ② 多个真人同时被问（投票/狼队决策是 gather 并发）：共用一个 `awaiting` future →
     后开的面板把先开的取消掉，有人永远收不到问题；
  ③ 提问/台账/身份没有按座位路由：A 的面板发给了 B，或者狼队夜聊漏给了另一个狼队友；
  ④ 访客令牌被 `.upper()` 弄坏（`secrets.token_urlsafe` 分大小写）；
  ⑤ 联机码 == 配对码：访客能顺手拿到管理权限。

这里用临时目录 + 假连接把这几条各钉一遍。

跑法：python tests/check_multiplayer.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import server as srv                      # noqa: E402
from wolf.engine import Game              # noqa: E402
from wolf.roles import make_board         # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {detail}" if detail else ""))


def make_room(tmp: str):
    old_here = srv.HERE
    srv.HERE = tmp
    try:
        cfg = {"game": {"save_dir": os.path.join(tmp, "games"), "human_seat": 6,
                        "experience": False, "board": "six_all_reveal"}}
        room = srv.Room(cfg, mock_default=True)
    finally:
        srv.HERE = old_here
    return room


class FakeWS:
    def __init__(self, name: str):
        self.name = name


async def main() -> int:
    print("\n=== 一、引擎：多个真人座位 ===")
    board = make_board("six_all_reveal")
    g = Game(board=board, human_seats={1, 3}, seed=7)
    check("1a human_seats={1,3} → 只有 1、3 号是真人",
          {s.id for s in g.seats if s.is_human} == {1, 3},
          str([s.id for s in g.seats if s.is_human]))
    g2 = Game(board=board, human_seat=4, seed=7)
    check("1b 旧入口 human_seat=4 仍然只让 4 号是真人",
          {s.id for s in g2.seats if s.is_human} == {4})
    g3 = Game(board=board, human_seats=set(), human_seat=0, seed=7)
    check("1c human_seats=空 → 全 AI（主机当观众）",
          not any(s.is_human for s in g3.seats))

    tmp = tempfile.mkdtemp(prefix="mp-")
    room = make_room(tmp)
    print("\n=== 二、房间：联机码 / 座位 / 访客令牌 ===")
    check("2a 联机码是 6 位且不等于配对码",
          len(room.join_code) == 6 and room.join_code != room.env_token,
          f"{room.join_code} vs {room.env_token}")
    check("2b 主机默认坐 config 的 human_seat（6 号）",
          room.player("host")["seat"] == 6, str(room.player("host")["seat"]))

    room.players["g1"] = {"pid": "g1", "name": "阿狸", "seat": None,
                          "online": False, "host": False}
    ok, why = room._claim_seat("g1", 2)
    check("2c 访客能坐 2 号", ok, why)
    ok2, why2 = room._claim_seat("host", 2)
    check("2d 同一个座位不能被两个人坐", not ok2, why2)
    ok3, _ = room._claim_seat("g1", 13)
    check("2e 座位号只能 1~12", not ok3)
    ok4, _ = room._release_seat("g1")
    check("2f 能让出座位", ok4 and room.player("g1")["seat"] is None)

    token = "AbC-123_xyz"
    room.guest_tokens[token] = "g1"
    check("2g 访客令牌大小写敏感（不能 upper）", room.identify(token) == ("guest", "g1"),
          str(room.identify(token)))
    check("2h 主机用配对码认成 host", room.identify(room.env_token) == ("host", "host"))
    check("2i 假令牌认不出来", room.identify("NOPE") is None)
    host_view = room.room_public("host", is_host=True)
    guest_view = room.room_public("g1", is_host=False)
    check("2j 联机码只回给主机", host_view.get("join_code") == room.join_code
          and guest_view.get("join_code") == "")

    print("\n=== 三、并发提问：每个座位一个 future，回答只解自己那一手 ===")
    room.game = Game(board=board, human_seats={1, 2}, seed=3)
    room.human_seat = 1
    room.seat_owner = {1: "host", 2: "g1"}
    room.player("host")["seat"] = 1
    room.player("g1")["seat"] = 2
    room.multiplayer = True
    room.running = True
    ws1, ws2 = FakeWS("host"), FakeWS("guest")
    room.clients = {ws1: asyncio.Queue(), ws2: asyncio.Queue()}
    room.client_info = {ws1: {"role": "host", "pid": "host"},
                        ws2: {"role": "guest", "pid": "g1"}}
    sent: dict[int, list] = {id(ws1): [], id(ws2): []}

    def fake_send(ws, msg):
        sent[id(ws)].append(msg)
    room.send_to = fake_send

    f1 = room.ask(1, {"type": "ask", "kind": "vote", "text": "1号投票"})
    f2 = room.ask(2, {"type": "ask", "kind": "vote", "text": "2号投票"})
    check("3a 两个座位各拿到一个不同的 future", f1 is not f2 and not f1.done() and not f2.done())
    check("3b 两条待回答记录都在", set(room.pending_asks) == {1, 2},
          str(sorted(room.pending_asks)))
    check("3c 提问只发给对应座位",
          [m.get("seat") for m in sent[id(ws1)]] == [1]
          and [m.get("seat") for m in sent[id(ws2)]] == [2],
          f"host={[m.get('seat') for m in sent[id(ws1)]]} guest={[m.get('seat') for m in sent[id(ws2)]]}")

    a1 = room.pending_asks[1]["ask_id"]
    await room.handle(ws2, {"type": "action", "ask_id": a1, "payload": {"vote": 1}})
    check("3d 拿别人的 ask_id 回答不了（只提示，不解 future）",
          not f1.done() and not f2.done() and any("上一条问题" in str(m.get("text"))
                                                 for m in sent[id(ws2)]))
    await room.handle(ws1, {"type": "action", "ask_id": a1, "payload": {"vote": 2}})
    check("3e 1 号回答只解 1 号那一手", f1.done() and not f2.done())
    check("3f ask_done 也只回给 1 号",
          [m.get("type") for m in sent[id(ws1)]].count("ask_done") == 1
          and "ask_done" not in [m.get("type") for m in sent[id(ws2)]])
    a2 = room.pending_asks[2]["ask_id"]
    await room.handle(ws2, {"type": "action", "ask_id": a2, "payload": {"vote": 1}})
    check("3g 2 号随后回答解掉自己那一手", f2.done() and f2.result() == {"vote": 1})

    print("\n=== 四、台账按座位过滤：狼队夜聊只给狼，公共发言给所有人 ===")
    room.game = Game(board=board, human_seats={1, 2}, seed=3)
    room.game.seats[0].role_key = "wolf"
    room.game.seats[1].role_key = "seer"
    room.human_seat = 1
    room.seat_owner = {1: "host", 2: "g1"}
    room.player("host")["seat"] = 1
    room.player("g1")["seat"] = 2
    room.clients = {ws1: asyncio.Queue(), ws2: asyncio.Queue()}
    room.client_info = {ws1: {"role": "host", "pid": "host"},
                        ws2: {"role": "guest", "pid": "g1"}}
    sent[id(ws1)].clear()
    sent[id(ws2)].clear()
    entries = []
    entries.append(room.game.ledger.add(1, "day", "speech", "大家早", seat=3))
    entries.append(room.game.ledger.add(1, "night", "wolf_talk", "今晚刀 2 号", seat=1,
                                        visible_to=["wolf"]))
    entries.append(room.game.ledger.add(1, "night", "seer_check", "2 号是好人", seat=2,
                                        visible_to=["seat:2"]))
    for i, e in enumerate(entries):
        room.broadcast_entry(e, i)
    host_texts = [m["entry"]["text"] for m in sent[id(ws1)] if m.get("type") == "entry"]
    guest_texts = [m["entry"]["text"] for m in sent[id(ws2)] if m.get("type") == "entry"]
    check("4a 公共发言两边都收到", "大家早" in host_texts and "大家早" in guest_texts,
          f"host={host_texts} guest={guest_texts}")
    check("4b 狼队夜聊只给坐狼的 1 号", "今晚刀 2 号" in host_texts
          and "今晚刀 2 号" not in guest_texts)
    check("4c 预言家私有信息只给坐 2 号的访客", "2 号是好人" in guest_texts
          and "2 号是好人" not in host_texts)

    print("\n=== 五、按座位生成的 status：身份牌与联机码都不越权 ===")
    hst = room.status_for(1, host=True)
    gst = room.status_for(2, host=False)
    check("5a 主机 status 有联机码，访客没有",
          hst.get("join_code") == room.join_code and gst.get("join_code") is None)
    check("5b 各自看到自己的身份", hst.get("your_role") and gst.get("your_role")
          and hst.get("your_role") != gst.get("your_role"),
          f"{hst.get('your_role')} / {gst.get('your_role')}")
    check("5c 座位表带 owner（前端显示谁坐几号）",
          any((s.get("owner") == "g1") for s in gst.get("seats", [])))
    spectator = room.status_for(0, host=False)
    check("5d 观众拿不到身份牌", "your_role" not in spectator and "your_mates" not in spectator)

    print("\n=== 六、主机换小板子：超出座位数的玩家自动让出 ===")
    room.running = False
    room.players["g1"]["seat"] = 11
    await room.handle(ws1, {"type": "board", "board": "six_all_reveal"})
    check("6a 换成 6 人板后，坐 11 号的访客自动让出",
          room.player("g1")["seat"] is None, str(room.player("g1")["seat"]))
    check("6b 6 号仍在板子范围内（不会被误清）",
          room.player("host")["seat"] == 1, str(room.player("host")["seat"]))
    check("6c 大厅板子已记住", room.lobby_board == "six_all_reveal", room.lobby_board)

    bad = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in RESULTS:
        if not any(name == b for b in bad):
            continue
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
