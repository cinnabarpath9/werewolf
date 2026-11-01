# coding=utf-8
"""覆盖闸门：**默认板子上的每个角色，打法书与规则书里都必须有它的一章**。

为什么要有这个：2026-10-09 换板子（守卫→摄梦人、三张民牌换成守墓人/养鸦人/幸运儿）时，
规则书的片段补了、**打法书的章节忘了补** —— 结果那三张牌在包里只能读「通用 + 阵营通用」，
等于**拿着技能却没有打法**。这种"新角色忘了写书"的事靠人盯不可靠，所以做成闸门。

判据：
  1. `config.yaml` 的默认板子里，每个角色在 **规则书** 有 `frag:role:<key>:begin`；
  2. 同一个角色在 **打法书** 有 `frag:role:<key>`（狼人允许只有阵营章之外的通用章 → 也要有）；
  3. 反向检查：打法书里不许出现"板子上没有、而且也不是通用角色"的章节
     （留着守卫/猎人章是**故意**的：别的板子还在用）。
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                     # noqa: E402

from wolf.roles import BUILTIN_BOARDS, ROLES                     # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
board_name = (cfg.get("game") or {}).get("board") or "eleven_wolfking"
board = BUILTIN_BOARDS[board_name]
sys.path.insert(0, HERE)
from _content import playbook_raw  # noqa: E402
book = playbook_raw()
from _content import read as _read_content, RULES as _RULES  # noqa: E402
rules = _read_content(_RULES)
play_frags = set(re.findall(r"<!-- frag:role:([a-z_]+) -->", book))
rule_frags = set(re.findall(r"<!-- frag:role:([a-z_]+):begin -->", rules))

print(f"=== 默认板子 {board_name}：角色 {len(set(board.roles))} 种 ===")
missing_rules, missing_play = [], []
for key in dict.fromkeys(board.roles):
    name = ROLES[key].name
    in_rules = key in rule_frags
    in_play = key in play_frags
    print(f"    {name:<6}({key:<12}) 规则书={'✅' if in_rules else '❌'}  打法书={'✅' if in_play else '❌'}")
    if not in_rules:
        missing_rules.append(name)
    if not in_play:
        missing_play.append(name)

check(f"1 默认板子每个角色都有**规则书**片段", not missing_rules, f"缺：{missing_rules}")
check(f"2 默认板子每个角色都有**打法书**章节（否则只能读通用章、技能白拿）",
      not missing_play, f"缺：{missing_play}")

print("\n=== 反向：打法书里的角色章，是不是都有对应角色定义 ===")
unknown = sorted(k for k in play_frags if k not in ROLES)
check("3 打法书没有「角色表里不存在」的章节", not unknown, str(unknown))
extra = sorted(k for k in play_frags if k not in set(board.roles))
print(f"    （不在默认板子但有章节的：{extra} —— 这些是给**别的板子**用的，属正常）")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
