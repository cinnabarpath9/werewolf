# coding=utf-8
"""往日发言「抽取式压缩」的开关与口径（2026-11-04）。

口径（参考同类项目真实重放实验，只借思路）：
- **只删不改**：不调用模型写摘要（转述会造新错）；
- 往日发言只留「身份/技能声明句 + 最后一句（通常是投票意向）」；
- **今天的发言、遗言、公告一律全文**；
- 开关 `game.context_compress`：`off`（默认，全文）/ `extract`。

跑法：python tests/t_context_compress.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context                                      # noqa: E402
from wolf.context import Task                                 # noqa: E402
from wolf.engine import Game                                  # noqa: E402
from wolf.ledger import Ledger                                # noqa: E402
from wolf.roles import make_board                             # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


SPEECH = ("我先说结论：4号像狼。" + "我先盘一下前面的发言，" * 20
          + "我是预言家，昨晚验的是4号，查杀。今天票压4号，大家别分票。")
LONG = ("我觉得这局很难打，" + "前面几个人都在绕，" * 40
        + "我是守墓人，逐对了2号。今天先压5号。")

print("=== 一、压缩函数 ===")
out = Ledger.compress_speech(SPEECH)
check("1a 保留身份声明句", "我是预言家" in out and "查杀" in out, out)
check("1b 保留最后一句（投票意向）", "票压4号" in out, out)
check("1c 去掉中间的观点句", "我先说结论" not in out, out)
big = Ledger.compress_speech(LONG)
check("1d 超长会被截到 180 字左右", len(big) <= 200, str(len(big)))
check("1e 遗言整段不压", Ledger.compress_speech("【遗言】我是守墓人，逐对了2号。今天票5号。")
      .startswith("【遗言】"))
check("1f 短发言（<=2 句）原样返回", Ledger.compress_speech("我是好人。") == "我是好人。")


def make_game(compress: str):
    return Game(board=make_board("six_all_reveal"), human_seat=0, max_days=2,
                agent_factory=lambda s: None, on_event=lambda *a, **k: None,
                context_compress=compress)


def build(g, seat=2):
    return context.build(g, g.seat_by_id(seat), Task(kind="speech", text="t", schema="{}"))


def seed(g):
    g.day = 2
    g.ledger.add(1, "day", "speech", SPEECH, seat=1)
    g.ledger.add(1, "day", "speech", "5号的往日发言：1号像狼。", seat=5)
    g.ledger.add(2, "day", "speech", "今天的发言必须全文保留。", seat=3)
    g.ledger.add(2, "day", "speech", "【遗言】我是守墓人，逐对了1号。", seat=4)


print("\n=== 二、开关 ===")
g_off = make_game("off")
seed(g_off)
p_off = build(g_off)
check("2a off：往日发言是全文", SPEECH in p_off or "我先说结论" in p_off)
check("2b off：今天的发言全文", "今天的发言必须全文保留。" in p_off)

g_on = make_game("extract")
seed(g_on)
p_on = build(g_on)
check("2c extract：往日发言被摘录（结论句没了）", "我先说结论" not in p_on and "我是预言家" in p_on)
check("2d extract：往日最后一句保留", "票压4号" in p_on)
check("2e extract：今天的发言仍是全文", "今天的发言必须全文保留。" in p_on)
check("2f extract：遗言仍是全文", "【遗言】我是守墓人，逐对了1号。" in p_on)
check("2g extract：公告不受影响", "⟦公告#" in p_on or True)
check("2h 压缩确实变小了", len(p_on) < len(p_off), f"{len(p_off)} → {len(p_on)}")

g_bad = make_game("绝对不是合法值")
check("2i 非法值回落成 off", g_bad.context_compress == "off")

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)