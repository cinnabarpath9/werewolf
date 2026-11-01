# coding=utf-8
"""考核题库闸门：`rules_questions()` 必须**对每个板子**都能跑通、且零告警。

为什么要有它（2026-10-09 复查实测）：
  `add()` 里**没有** `board` 参数，但 R16 一直在传 `board="eleven"` ——
  于是 `rules_questions()` 一跑到 R16 就
  `TypeError: add() got an unexpected keyword argument 'board'`：
  **整份规则题直接崩掉**，而 `tools/exam_live.py`（模型考试）拿不到任何规则题，
  却没有任何东西报警（题库不在回归里）。这个闸门就是补这个窟窿。

判据：
  1. 默认板子 + 每个内置板子都能跑出规则题（不抛异常）；
  2. 每题都带 `rules:<板子>` 上下文，且板子名是合法的（否则规则文本取不到）；
  3. 题库自检的 `problems` 为空（题库里有 if/else 现算校验，那些是它自己的断言）；
  4. 题量不少于 15（防止有人误删一大片）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.roles import BUILTIN_BOARDS                        # noqa: E402

import exam_bank                                             # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


# 题库是**固定卷面**：题面里的数字是按 11 / 12 人板写的，所以只按它自己声明支持的那几张板子跑
# （2026-10-30 产品只留四张 6/9/11/12 人板；6 / 9 人板人数跟题面对不上，不能自动并进来）。
boards: list[str | None] = [None] + list(exam_bank.EXAM_BOARD_NAMES)
print(f"=== 逐板子跑题库自检（{len(boards)} 个）===")
for b in boards:
    tag = b or "（默认板子）"
    try:
        qs, warns = exam_bank.rules_questions(b)
    except Exception as exc:                                 # noqa: BLE001
        check(f"1[{tag}] rules_questions 能跑", False, f"{type(exc).__name__}: {exc}")
        continue
    check(f"1[{tag}] 跑通（{len(qs)} 道题，告警 {len(warns)} 条）", len(warns) == 0, str(warns[:2]))
    check(f"2[{tag}] 题量 ≥15", len(qs) >= 15, str(len(qs)))
    legal_boards = set(exam_bank.EXAM_BOARD_NAMES) | set(BUILTIN_BOARDS)
    bad_ctx = [q.qid for q in qs
               if not str(q.context).startswith("rules:")
               or str(q.context).split(":", 1)[1] not in legal_boards]
    check(f"3[{tag}] 每题的 rules:<板子> 上下文都合法", not bad_ctx, str(bad_ctx[:4]))

print("\n=== 题库里那些「引擎现算」的题真的被算过 ===")
qs, _ = exam_bank.rules_questions(None)
checked = [q for q in qs if q.engine_checked]
manual = [q for q in qs if not q.engine_checked]
check("4a 至少有 5 道是引擎现算的", len(checked) >= 5, f"{len(checked)} 道现算 / {len(manual)} 道人工核对")
check("4b 人工核对的题都在 why 里写明了来源（便于规则改动时复核）",
      all(q.why for q in manual), str([q.qid for q in manual if not q.why][:4]))
new = [q.qid for q in qs if q.qid in ("R21", "R22", "R23", "R24")]
check("4c 新机制四题（摄梦连摄/奶穿/养鸦人/守墓人）都在", len(new) == 4, str(new))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
