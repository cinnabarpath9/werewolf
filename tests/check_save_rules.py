# coding=utf-8
"""查清 C4：存档到底有没有记 rules_mode / rules_detail（内存 vs 落盘 vs 旧存档）。"""
import asyncio
import glob
import io
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import MockAgent                         # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402

files = sorted(glob.glob(os.path.join(ROOT, "games", "game-*.json")), key=os.path.getmtime)
print(f"存档总数 {len(files)}；最近 3 个的 rules 键：")
for p in files[-3:]:
    s = json.load(io.open(p, encoding="utf-8"))
    when = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    print(f"  {os.path.basename(p)}（{when}）→ {sorted(s.get('rules') or {})}")

print()
g = Game(board=make_board("eleven_wolfking"),
         agent_factory=lambda s: MockAgent(s, random.Random(1)),
         seed=1, max_days=1, rules_mode="room", rules_detail="brief")
# ⚠️ 2026-10-09：**先真跑一局**再存盘 —— 不然台账是空的，下面那条"往返一致"就是空过
#    （第一版就是这样：0 条也判 ✅）。mock 一局不到两秒。
asyncio.run(g.run())
print(f"这一局跑完：{g.day} 天、台账 {len(g.ledger.entries)} 条、赢家 {g.winner}")
print("内存里 to_dict()['rules'] 键：", sorted(g.to_dict()["rules"]))
# ⚠️ 2026-10-09 修：以前直接往**用户的 `games/`** 里写一局测试存档 ——
#    那会给用户的留档列表塞垃圾，还会把真局挤出保留窗口（见 t_save_dir_isolated.py 的来龙去脉）。
_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "save_rules_games")
os.makedirs(_tmp, exist_ok=True)
path = g.save(_tmp)
saved = json.load(io.open(path, encoding="utf-8"))
print("落到磁盘的 rules 键       ：", sorted(saved.get("rules") or {}))
print("存档文件：", os.path.basename(path))
print()
print("结论：", "落盘也带上了（C4 是我读到了旧存档 → 测试自己的问题）"
      if "rules_mode" in (saved.get("rules") or {})
      else "❌ 落盘丢了字段（真缺陷：save() 没走 to_dict 的 rules）")

# ⚠️ 2026-10-09 补：以前只 print 结论，没有退出码（假回归门）。
# ⚠️ 2026-10-09 加：顺手把 `Ledger.from_list()` 用起来 —— 它是"把存档里的台账读回来"的
#    反序列化入口，可全项目**一个调用点都没有**（项目自查抓到的死代码）。
#    与其删掉，不如在这里守住它：它必须能把 to_dict 写出去的台账**原样读回来** ——
#    存档格式一旦漂了（Entry 字段改名/漏字段），这里立刻红。
from wolf.ledger import Ledger                             # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）
_back = Ledger.from_list(saved.get("ledger") or [])
_fwd = g.ledger.entries
_rt_ok = (len(_fwd) > 10 and len(_back.entries) == len(_fwd)   # 条数必须真有东西，别空过
          and all(b.day == a.day and b.phase == a.phase and b.kind == a.kind
                  and b.text == a.text and b.seat == a.seat
                  and dict(b.data) == dict(a.data) and tuple(b.visible_to) == tuple(a.visible_to)
                  for a, b in zip(_fwd, _back.entries)))
print(f"台账往返（to_dict → Ledger.from_list）：{len(_back.entries)} 条，"
      f"{'一致 ✅' if _rt_ok else '❌ 不一致（存档格式漂了）'}")

_ok = _rt_ok and all(k in (saved.get("rules") or {})
                     for k in ("rules_mode", "rules_detail", "rules_chars", "edge_order"))
print()
print("判定：", "✅ 落盘带上了 rules 快照" if _ok else "❌ 落盘丢了字段")
raise SystemExit(0 if _ok else 1)
