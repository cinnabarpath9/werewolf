# coding=utf-8
"""量提示层三个块的字数（给人看/给我自己核对用，不进回归）。

用法：python tools/packet_size.py            # 当前状态
输出：提示词 / 打法书（整体 + 每个 frag 片段）/ 概念库（整体 + frag:meta 及各子段）
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf import rules, tabletalk                        # noqa: E402


def frags(path: str) -> list[tuple[str, int]]:
    """按 `<!-- frag:xxx -->` … `<!-- frag:end -->` 切，返回 [(名字, 字数)]。

    ⚠️ 只切**外层**片段：角色章内部的 `<!-- frag:part:open -->`…`<!-- frag:endpart -->`
    是"只在某阶段发"的子节，算进所属角色章里（第一版把它们当成新片段开始，
    于是每个角色章都变成了 0 字 + 一串 endpart）。
    """
    text = io.open(os.path.join(ROOT, path), encoding="utf-8").read()
    out: list[tuple[str, int]] = []
    cur: list[str] = []
    name = ""
    for line in text.splitlines(True):
        s = line.strip()
        if re.fullmatch(r"<!--\s*frag:endpart\s*-->", s):
            continue                                        # 子节结束：忽略，继续累加
        if re.fullmatch(r"<!--\s*frag:part:[\w\-]+\s*-->", s):
            continue                                        # 子节开始：忽略
        if re.fullmatch(r"<!--\s*frag:end\s*-->", s):
            out.append((name, len("".join(cur).strip())))
            name, cur = "", []
            continue
        m = re.fullmatch(r"<!--\s*frag:([\w:\-]+)\s*-->", s)
        if m:
            name, cur = m.group(1), []
            continue
        if name:
            cur.append(line)
    return out


cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
print("=== 提示词（每次调用都进包）===")
prompt = tabletalk.player_prompt(cfg, ROOT)
print(f"  player_prompt：{len(prompt)} 字")

print("\n=== 战术书 content/05_战术书.md ===")
play = io.open(os.path.join(ROOT, "content", "05_战术书.md"), encoding="utf-8").read()
print(f"  整份：{len(play)} 字")
for name, n in frags("content/05_战术书.md"):
    print(f"    {name:<18}{n:>7} 字")

print("\n=== 概念库 content/06_概念库.md ===")
meta = io.open(os.path.join(ROOT, "content", "06_概念库.md"), encoding="utf-8").read()
print(f"  整份：{len(meta)} 字")
inpack = re.search(r"<!-- frag:meta:begin -->(.*?)<!-- frag:meta:end -->", meta, re.S)
print(f"  frag:meta 段（进包）：{len(inpack.group(1).strip()) if inpack else 0} 字")
for name, n in frags("content/06_概念库.md"):
    print(f"    {name:<18}{n:>7} 字")

print("\n=== 规则书（按默认板子组装，每次调用都进包）===")
board = (cfg.get("game") or {}).get("board") or "eleven_wolfking"
from wolf.roles import make_board                        # noqa: E402
bd = make_board(board)
t = rules.compose(bd.roles, sheriff=len(bd.roles) >= 11,
                  first_night_quiet=bool(getattr(bd, "first_night_quiet", False)),
                  reveal_on_death=bool(getattr(bd, "reveal_on_death", False)),
                  win_rule=str(getattr(bd, "win_rule", "sides") or "sides"),
                  seer_auto_run=bool((cfg.get("game") or {}).get("seer_auto_run", True)))
print(f"  {board}：{len(t)} 字")
