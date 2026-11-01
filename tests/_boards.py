# coding=utf-8
"""测试专用的补充板子（**不进产品**，不会出现在手机的板子列表里）。

为什么需要它：
  2026-10-09 用户决定**把 9 人板从产品里删掉**（`nine_standard` 已不在 `BUILTIN_BOARDS`）。
  但有一批用例测的是**引擎机制**、而且专门依赖"9 人、**没有警长竞选**"这个场景：
    · `t_rules_new.py`      遗言 / 警徽 / PK / 夜次这些规则
    · `t_night_calls.py`    "每晚被问了几次调用"
    · `t_sheriff_order_choice.py`  警长定发言顺序（需要**能**开竞选的板子做对照）
    · `t_wolf_explode.py`   自爆的两个窗口（其中一个专门断言"9 人板没有竞选 → 不问自爆"）
    · `t_decision_first.py` "决策在前"的复用行为
  把它们的场景硬改成 11 人（会多出警长竞选）等于**换了被测对象**，不是删板子该有的后果。
  所以在这里定义一张测试专用的 9 人板，用例引用它。

2026-10-30 又退役了四张 11 人板（eleven / eleven_standard / eleven_dream / eleven_guard）：
  它们同样只从**产品**下架，引擎机制（守卫 / 摄梦人 / 狼王 / 猎人）仍然支持，回归要用。
  本模块因此提供一个 `make_board()` **包装**：先认退役板子（副本在
  `wolf/retired_boards.py`），其余原样交给 `wolf.roles.make_board`。
  测试文件只要 `from _boards import make_board` 就能继续用旧板名，**产品代码不用改**。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wolf.roles import BoardDef                                  # noqa: E402
from wolf.roles import make_board as _product_make_board         # noqa: E402
from wolf.retired_boards import RETIRED_BOARDS, retired_board    # noqa: E402

#: 测试用 9 人板：3 狼 + 预言家、女巫、猎人 + 3 民（= 产品里已删的 `nine_standard`）
NINE_BOARD = BoardDef(
    name="nine_test",
    roles=["wolf", "wolf", "wolf", "seer", "witch", "hunter",
           "villager", "villager", "villager"],
    desc="测试专用 9 人板（产品里已删）：3 狼 + 预女猎 + 3 民",
)


def make_board(name, custom: dict | None = None):
    """**测试用**板子解析：退役板子给一份副本，其余交给产品实现。

    ⚠️ 不要在 `wolf/`、`server.py`、`web/` 里 import 本函数 —— 那是测试垫片。
    """
    if isinstance(name, str) and not (custom and name in custom) and name in RETIRED_BOARDS:
        return retired_board(name)
    return _product_make_board(name, custom)


__all__ = ["NINE_BOARD", "RETIRED_BOARDS", "make_board", "retired_board"]
