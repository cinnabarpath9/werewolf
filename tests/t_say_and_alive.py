# coding=utf-8
"""守住两件用户报过的现象（2026-10-08 真局）：

  · **#2 狼队夜间发言没语音**：真人坐狼时，狼队夜聊（kind=`night`、只狼可见）
    在手机上只有字、没有声音。口径改成"真人坐狼才配"（没人在狼队里配了也没人听、纯花钱）。
  · **#8 顶部存活不及时划掉**：1、2 夜里出局后，顶部座位条还是"活着"的样子 ——
    因为夜间死亡只发一条 `announce`，而前端**只在 status 里**读存活；
    下一次 status 要等到阶段切换，中间一直显示旧状态。

本脚本查四节：
  1. `_say_ok` 五种组合的口径（纯函数，离线）；
  2. `_entry_msg` 产出的报文里 `say` 对不对（真 Ledger.Entry，走真代码）；
  3. **行为判据**：喂一条 `announce` 进 `Room.on_event` → 必须补推一条 `status`，
     且那条 status 的 `seats` 里已经能看出"谁死了"；喂 `speech` 则**不该**多推 status；
  4. 结构闸（+变异自证）：三个调用点都必须走 `_say_ok`，不许再各写一份判据。

跑法： python t_say_and_alive.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                                  # noqa: E402
from wolf.ledger import Ledger, WOLF                          # noqa: E402
from wolf.roles import make_board                             # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

import server as srv                                          # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


# ── 一、口径 ────────────────────────────────────────────────────────────────────
print("=== 一、_say_ok：哪条该配音 ===")
ok = srv._say_ok
check("1a 公开发言（speech）→ 配", ok("speech", 3, ("public",), "") is True)
check("1b PK 发言（pk_speech）→ 配", ok("pk_speech", 3, ("public",), "good") is True)
check("1c 狼队夜聊 + 真人坐狼 → 配（这是本次新增的那条）",
      ok("night", 9, (WOLF,), "wolf") is True)
check("1d 狼队夜聊 + 真人坐好人 → 不配", ok("night", 9, (WOLF,), "good") is False)
check("1e 狼队夜聊 + 没有真人（camp 空）→ 不配（配了没人听，纯花钱）",
      ok("night", 9, (WOLF,), "") is False)
check("1f 公告 / 投票 / 私有信息一律不配",
      not any(ok(k, 3, ("public",), "wolf") for k in ("announce", "vote", "private", "claim", "night")))
check("1g 没有座位号的一律不配（无源可配）",
      not any(ok(k, None, (WOLF,), "wolf") for k in ("speech", "pk_speech", "night")))

# ── 二、报文形状 ────────────────────────────────────────────────────────────────
print("\n=== 二、_entry_msg 的 say 字段（真 Ledger.Entry 走真代码）===")
led = Ledger()
e_pub = led.add(1, "day", "speech", "我是好人。", seat=3)
e_wolf = led.add(1, "night", "night", "9号：我建议刀 4号。", seat=9, visible_to=(WOLF,))
e_ann = led.add(1, "day", "announce", "昨晚出局：1号。", seat=None)

room = srv.Room.__new__(srv.Room)                    # 只要 _entry_msg，不走 __init__
me_wolf = types.SimpleNamespace(id=6, camp="wolf")
me_good = types.SimpleNamespace(id=6, camp="good")
check("2a 公开语音：say=True", room._entry_msg(e_pub, 0, me_good)["entry"]["say"] is True)
check("2b 真人坐狼：队友夜聊 say=True",
      room._entry_msg(e_wolf, 1, me_wolf)["entry"]["say"] is True)
check("2c 真人坐好人：看不到夜聊，即使看得到也不配",
      room._entry_msg(e_wolf, 1, me_good)["entry"]["say"] is False)
own = led.add(1, "night", "night", "6号：我跟票。", seat=6, visible_to=(WOLF,))
check("2d 自己说的话不配音（哪怕真人坐狼）",
      room._entry_msg(own, 2, me_wolf)["entry"]["say"] is False)
check("2e 公告没有座位号 → 不配", room._entry_msg(e_ann, 3, me_wolf)["entry"]["say"] is False)

# ── 三、行为判据：公告之后必须补一条 status（#8）────────────────────────────────
print("\n=== 三、夜间出局公告之后，服务端必须补推 status（顶部才能划掉）===")
tmp = tempfile.mkdtemp(prefix="sayalive-")
cfg = {"game": {"save_dir": tmp, "keep_games": 0, "human_seat": 6, "experience": False}}
room2 = srv.Room(cfg, mock_default=True)
room2.game = Game(board=make_board("eleven_dream"), agent_factory=None, seed=1, max_days=1)
room2.human_seat = 6
sent: list[dict] = []
room2.broadcast = lambda msg, *a, **k: sent.append(msg)

victim = room2.game.seat_by_id(1)
victim.alive = False                                  # 模拟"夜里 1 号出局"
ann = room2.game.ledger.add(1, "day", "announce", "昨晚出局：1号。（不公布死因）")
room2.on_event("ledger", entry=ann)
kinds = [m.get("type") for m in sent]
check("3a 公告本身推了", kinds.count("entry") == 1, str(kinds))
check("3b ⚠️ 紧接着补推了一条 status（这就是顶部划不掉的根因）",
      "status" in kinds, str(kinds))
st = next((m for m in sent if m.get("type") == "status"), None)
seat1 = next((s for s in (st or {}).get("seats", []) if s["id"] == 1), None)
check("3c 那条 status 里 1 号已经是 dead（前端据此划线）",
      bool(seat1) and seat1["alive"] is False, str(seat1))
check("3d 1 号不在存活列表里", 1 not in ((st or {}).get("alive") or []),
      str((st or {}).get("alive")))

sent.clear()
sp = room2.game.ledger.add(1, "day", "speech", "我先过。", seat=3)
room2.on_event("ledger", entry=sp)
check("3e 普通发言**不该**多推 status（免得每句话都刷一遍）",
      [m.get("type") for m in sent] == ["entry"], str([m.get("type") for m in sent]))

# ── 四、结构闸（不是空过：判据对旧写法必须判红）────────────────────────────────
print("\n=== 四、结构闸：三个调用点共用 _say_ok ===")
src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("4a server.py 里没有残留的 `kind not in voice.SPEECH_KINDS` 判据",
      "not in voice.SPEECH_KINDS" not in src,
      str([l.strip() for l in src.splitlines() if "not in voice.SPEECH_KINDS" in l]))
check("4b _say_ok 被三处调用（推送 / 留档 / /api/tts）", src.count("_say_ok(") >= 4,
      f"出现 {src.count('_say_ok(')} 次（含定义 1 次）")
import io  # noqa: E402  （只用于下面那段变异自证的可读性，无实际 IO）
fake = "if kind not in voice.SPEECH_KINDS or not seat:\n    continue\n"
check("4c 变异自证：判据对『旧写法』判红",
      "not in voice.SPEECH_KINDS" in fake)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
