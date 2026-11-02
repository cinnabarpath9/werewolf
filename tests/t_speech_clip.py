# coding=utf-8
"""托管发言走真实调用链路 + 发言长度上限（2026-11-03）。

作者两个问题：
  ① 托管能不能直接复用真实模型调用的提示链路（读规则书/打法书，正常通读上下文）？
     → 能：`butler.build_speech_call()` 用 `context.build()`（全系统唯一 packet 装配器）重拼，
       任务说明按 AI 座位 `speech_plan` 的口径（先想清楚→再写发言）。
  ② 发言字数上限多少？会不会撞墙？
     → 提示词 350 字；**硬上限 800**，且按句号截断（以前 `[:600]` 会切在半句中间）。

跑法：python tests/t_speech_clip.py
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import butler                                        # noqa: E402
from wolf.context import Task                                  # noqa: E402
from wolf.engine import Game, clip_speech                      # noqa: E402
from wolf.roles import make_board                              # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


print("=== 一、截断：按句号，不从半句中间切 ===")
long = "第一句话讲立场。" * 200
clipped = clip_speech(long)
check("1a 超过 800 字会被截", len(clipped) <= 802, str(len(clipped)))
check("1b 截在句末（不是半个字）", clipped.endswith("。……"), clipped[-8:])
check("1c 短文本原样返回", clip_speech("短句。") == "短句。")
check("1d 没有句号时硬截、带省略号", clip_speech("字" * 1000).endswith("……"))
check("1e 空值不炸", clip_speech("") == "" and clip_speech(None) == "")

print("\n=== 二、托管发言复用真实 packet ===")
g = Game(board=make_board("eleven_wolfking"), seed=1, max_days=1,
         agent_factory=lambda seat: None, on_event=lambda *a, **k: None,
         rules_mode="packet", rules_detail="brief",
         playbook_text="· 打法书：先给结论再给理由。", meta_text="· 概念库：查杀=验出的狼。")
seat = g.seat_by_id(1)
seat.role_key = "seer"
g.day, g.phase = 2, "day"
task, packet = butler.build_speech_call(g, seat, 1)
check("2a 任务按 speech_plan 口径（含决策+发言两件事）",
      "先想清楚" in task.text and "speech" in task.text)
check("2b schema 里同时要 reads / vote / speech",
      all(k in task.schema for k in ('"reads"', '"vote"', '"speech"')))
check("2c packet 走的是唯一装配器（含身份块）", "【你的身份】" in packet, packet[:40])
check("2d packet 带规则/打法（不是人类面板那句短提示）",
      any(k in packet for k in ("【规则", "规则书", "打法")), "没找到规则/打法块")
check("2e packet 带发言顺序/进度块", "【发言顺序与进度】" in packet)
check("2f 字数要求是 350 字以内", "350" in packet)
check("2g 明确要求别用「X号。」开头", "X号" in packet)

print("\n=== 三、人类/托管发言落账也走同一截断 ===")
seen: list[str] = []


class Human:
    uses_model = False

    async def ask(self, packet, task):
        return {"speech": long, "claim": "无"}

    def last_meta(self):
        return {}


class Scripted:
    def __init__(self, seat_id: int) -> None:
        self.seat_id = seat_id

    uses_model = False

    async def ask(self, packet, task):
        if task.kind in ("speech", "speech_plan"):
            return {"reads": [], "vote": None, "speech": "短发言。", "claim": "无"}
        if task.kind == "vote":
            return {"abstain": True, "reason": "剧本"}
        if task.kind == "analyze":
            return {"reads": [], "vote": None, "summary": "剧本"}
        return {}

    def last_meta(self):
        return {}


def make_agent(seat):
    return Human() if seat.id == 1 else Scripted(seat.id)


g2 = Game(board=make_board("six_all_reveal"), human_seat=1, max_days=1,
          agent_factory=make_agent, on_event=lambda *a, **k: None)
g2.day, g2.phase = 1, "day"
g2.sheriff_enabled = False
asyncio.run(g2.day_phase([]))
led = [e for e in g2.ledger.entries if e.kind == "speech" and e.seat == 1]
check("3a 台账里的发言被按句截断（transcript 保留原文，供复盘）",
      bool(led) and len(led[-1].text) <= 900 and led[-1].text.endswith("……"),
      str(len(led[-1].text) if led else "没落账"))

print("\n" + "=" * 62)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)