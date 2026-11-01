# coding=utf-8
"""确认"狼座位"和"好人座位"的包内容不同（玄学书按阵营分发）。"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 2026-10-09 修：以前拿 `"."` 当项目根 —— 但 runner 是**在 tests/ 里**
#    启动这些脚本的，于是读不到 content/ 下的书、判据全假。（以前没有退出码，
#    所以这个 cwd 依赖一直没人发现；补上闸门后立刻暴露。）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context, tabletalk                      # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

meta = tabletalk.load_meta_book(ROOT, "", "")
prompt = tabletalk.player_prompt({"game": {}}, ROOT)
g = Game(board=make_board("eleven_standard"), agent_factory=None, seed=3, max_days=1,
         player_prompt=prompt, meta_text=meta)
wolf_seat = next(s for s in g.seats if s.role_key == "wolf")
good_seat = next(s for s in g.seats if s.role_key == "villager")
task = Task(kind="speech", text="说句话", schema="{}", legal=[])
pw = context.build(g, wolf_seat, task)
pg = context.build(g, good_seat, task)

print(f"狼座位（{wolf_seat.id}号）包：{len(pw)} 字")
print(f"   有屠边账章节: {'狼队：先算屠边账' in pw}　有穿衣服: {'穿衣服' in pw}"
      f"　有好人轮次章节: {'悄悄死了' in pw}")
print(f"好人座位（{good_seat.id}号）包：{len(pg)} 字")
print(f"   有屠边账章节: {'狼队：先算屠边账' in pg}　有轮次校准章节: {'悄悄死了' in pg}")
print(f"差 {abs(len(pw) - len(pg))} 字（各自只拿自己那边的章节）")

# ⚠️ 2026-10-09 补：以前这里只 print，没有退出码 —— 判据再对也永远不会让回归变红。
_wolf_ok = ("狼队：先算屠边账" in pw) and ("悄悄死了" not in pw)
_good_ok = ("悄悄死了" in pg) and ("狼队：先算屠边账" not in pg)
print()
print("判定：", "✅ 两边各拿各的" if (_wolf_ok and _good_ok) else
      "❌ 阵营分发不对（狼章/好人章串了）")
raise SystemExit(0 if (_wolf_ok and _good_ok) else 1)
