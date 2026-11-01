# coding=utf-8
"""验玄学书的**按阵营分发**：狼只看狼的章节、好人只看好人的。"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 2026-10-09 修：以前拿 `"."` 当项目根 —— 但 runner 是**在 tests/ 里**
#    启动这些脚本的，于是读不到 content/ 下的书、判据全假。（以前没有退出码，
#    所以这个 cwd 依赖一直没人发现；补上闸门后立刻暴露。）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import tabletalk                               # noqa: E402

full = tabletalk.load_meta_book(ROOT, "", "")
wolf = tabletalk.load_meta_book(ROOT, "", "wolf")
good = tabletalk.load_meta_book(ROOT, "", "good")

print(f"全文       {len(full):>5} 字 ≈{int(len(full) * 0.75):>5} token")
print(f"狼视角     {len(wolf):>5} 字 ≈{int(len(wolf) * 0.75):>5} token"
      f"　含屠边账: {'屠边账' in wolf}　含穿衣服: {'穿衣服' in wolf}"
      f"　不含好人章节: {'轮次对不上' not in wolf}")
print(f"好人视角   {len(good):>5} 字 ≈{int(len(good) * 0.75):>5} token"
      f"　含轮次校准: {'轮次' in good}　含空衣服: {'空衣服' in good}"
      f"　不含屠边账: {'屠边账' not in good}")
print(f"标记残留: {'<!-- frag' in wolf or '<!-- frag' in good}")
print(f"共用章节都在: {'链压逻辑' in wolf and '链压逻辑' in good and '藏身份检测' in wolf}")

# ⚠️ 2026-10-09 补：以前只 print，没有退出码（假回归门）。
# ⚠️ 2026-10-09 修判据：以前拿"屠边账"这个**词**当"狼章"的标记，但好人章里本来就有一句
#    交叉引用（「狼队开刀：先算屠边账（第六节）…」，写给好人看狼会怎么想）→ 恒判红。
#    改成认**章节标题**（只有狼那一章有）。
_WOLF_CH = "狼队：先算屠边账"
_ok = (
    (_WOLF_CH in wolf) and (_WOLF_CH not in good)
    and ("轮次" in good)
    and ("<!-- frag" not in wolf) and ("<!-- frag" not in good)
    and ("链压逻辑" in wolf and "链压逻辑" in good and "藏身份检测" in wolf)
)
print()
print("判定：", "✅ 玄学书按阵营分发正确" if _ok else "❌ 分发/标记有问题")
raise SystemExit(0 if _ok else 1)
