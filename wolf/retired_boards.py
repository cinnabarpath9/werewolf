# coding=utf-8
"""**已退役的内置板子**（2026-10-30 作者拍板：产品只保留四张板子）。

这里不是产品的一部分：
  · 手机面板不列、`run_game.py --list-boards` 不列、`make_board()` **不认**这些名字
    （产品代码里写 `board: eleven_dream` 会照常报「没有这个板子」）；
  · 它只给**回归测试与考核题库**用：那些脚本要复现"摄梦人 / 守卫 / 狼王在 11 人场里"
    这类机制，而引擎本身仍然支持这些角色（`config.yaml` 的 `game.boards` 里自己写一张
    就能开）。如果连定义都删掉，测试就只能靠复制粘贴，反而更容易漂。

⚠️ 想给玩家重新开放其中一张：别 import 这个模块 —— 按 `docs/板子备份_已删除.md`
里写的办法，在 `config.yaml` 的 `game.boards` 写一份自定义板子（或把它贴回
`wolf/roles.py::BUILTIN_BOARDS`）。
"""
from __future__ import annotations

from .roles import BoardDef


def _b(name: str, roles: list[str], desc: str, win_rule: str = "sides",
       first_night_quiet: bool = False) -> BoardDef:
    return BoardDef(name=name, roles=roles, win_rule=win_rule, desc=desc,
                    first_night_quiet=first_night_quiet)


RETIRED_BOARDS: dict[str, BoardDef] = {
    "eleven": _b("eleven", ["wolf", "wolf", "wolf", "wolf",
                            "seer", "witch", "hunter", "guard",
                            "villager", "villager", "villager"],
                 "11 人：4 狼 + 预言家 + 女巫 + 猎人 + 守卫 + 3 民（2026-10-30 退役）"),
    "eleven_standard": _b("eleven_standard",
                          ["wolf", "wolf", "wolf", "wolf",
                           "seer", "witch", "hunter", "idiot",
                           "villager", "villager", "villager"],
                          "11 人（标准）：4 狼 + 预言家、女巫、猎人、白痴 + 3 民（2026-10-30 退役）"),
    "eleven_dream": _b("eleven_dream",
                       ["wolf", "wolf", "wolf", "wolf",
                        "seer", "witch", "dreamer", "idiot",
                        "gravekeeper", "crow", "lucky"],
                       "11 人（预女梦愚版）：4 狼 + 预言家、女巫、摄梦人、愚者 + 三张民牌"
                       "（2026-10-30 退役）"),
    "eleven_guard": _b("eleven_guard",
                       ["wolf", "wolf", "wolf", "wolf",
                        "seer", "witch", "guard", "idiot",
                        "villager", "villager", "villager"],
                       "11 人（守卫版）：4 狼 + 预言家、女巫、守卫、白痴 + 3 民（2026-10-30 退役）"),
}


def retired_board(name: str) -> BoardDef:
    """取一张退役板子的**副本**（测试/题库专用；产品代码不要调它）。"""
    if name not in RETIRED_BOARDS:
        raise KeyError(f"没有这张退役板子：{name}（退役的：{'、'.join(RETIRED_BOARDS)}）")
    base = RETIRED_BOARDS[name]
    return BoardDef(name=base.name, roles=list(base.roles), win_rule=base.win_rule,
                    desc=base.desc, first_night_quiet=bool(base.first_night_quiet),
                    reveal_on_death=bool(base.reveal_on_death))
