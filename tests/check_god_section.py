# coding=utf-8
"""验"神牌通用"章节的分发（女巫/猎人/白痴都该看到，狼/预言家/村民不该看到）。

⚠️ 2026-10-08 换过两次标记词，都是"撞词"撞红的（不是分发坏了）：
   ① 「查验顺序」—— 通用章里随手一写这四个字，**每个角色**都会被判成拿到了神章；
   ② 「神牌通用」—— 村民章里本来就有一句「见神牌通用那一节」，同样全中。
   现在用**整条章节标题**做标记（只有那一章的标题是这个句子），最不容易被内容改动撞上。
"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import tabletalk                               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 神章标题（改标题就要改这里 —— 这是**故意**绑死的：标题是这一章唯一的身份证）
GOD_MARK = "被排进「查验顺序」时，要不要报身份"
views = {r: tabletalk.load_playbook(ROOT, "", r)
         for r in ("wolf", "seer", "witch", "hunter", "idiot", "villager")}
for k, v in views.items():
    has_god = GOD_MARK in v
    has_witch_hide = "也可以不上警、藏身份" in v
    print(f"  {k:<9} {len(v):>5} 字｜神牌通用章节: {has_god}｜女巫藏身份段: {has_witch_hide}")

ok = (all(GOD_MARK in views[r] for r in ("witch", "hunter", "idiot"))
      and GOD_MARK not in views["wolf"]
      and GOD_MARK not in views["villager"]
      and GOD_MARK not in views["seer"]          # 预言家自己是查验方，不需要这段
      and "也可以不上警、藏身份" in views["witch"])
print("\n判定：", "✅ 神职三方都拿到、其它角色没有" if ok else "❌ 分发不对")
raise SystemExit(0 if ok else 1)
