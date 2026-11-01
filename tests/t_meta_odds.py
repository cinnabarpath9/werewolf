# coding=utf-8
"""闸门：**《概念库》那张概率表必须能从发牌本身重算出来**。

为什么要有它（2026-10-11，作者拍板）：
  那张表的数字是 2026-10-07 用三个一次性脚本（`tests/legacy/calc_meta_*.py`）算的，
  口径不统一：『全场至少一处三连神/三连好』用的是**首尾不接**的 9 处三连，
  而作者的本意是 **11 张牌是一个圈**（11→1→2 也算一处）。差了 6 个百分点
  （三连好 90.9% vs 96.7%）—— 这正是"数字钉死成字面量"看不出来的那类错。

判据（全部用 `tools/meta_odds.py` 的**精确枚举**，11550 种等概率类型排列，不是抽样）：
  1. 表里的 19 个数字逐个跟枚举结果对上（±0.05 个百分点）；
  2. 最要紧的那两个「全场至少一处三连」必须是**成圈**口径（23.3% / 96.7%），
     并且表里同时写清"不接"口径是多少（19.4% / 90.9%），别让读的人猜；
  3. 顺带确认抽样（走引擎自己的洗牌路径）跟枚举收敛 —— 这是"发牌数据测试"那一半。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from meta_odds import ROOT as _ROOT, check_book, exact, sample   # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


rows = exact()
print("=== 一、精确枚举（C(11,4)·C(7,4) = 11550 种等概率类型排列）===")
print(f"  成圈三连神 {rows['全场至少一处3连神(成圈)'] * 100:.2f}% / "
      f"三连好 {rows['全场至少一处3连好(成圈)'] * 100:.2f}%")
print(f"  不接三连神 {rows['全场至少一处3连神(不接)'] * 100:.2f}% / "
      f"三连好 {rows['全场至少一处3连好(不接)'] * 100:.2f}%")

print("\n=== 二、《概念库》第十一节的数字与枚举一致 ===")
bad = check_book(rows)
check(f"2a 表里的 19 个数字逐个对得上枚举（±0.05 个百分点）", not bad, "；".join(bad))

import io                                                        # noqa: E402
text = io.open(os.path.join(_ROOT, "content", "06_概念库.md"), encoding="utf-8").read()
check("2b 两个『全场至少一处三连』用的是**成圈**口径（23.3% / 96.7%）",
      "23.3%" in text and "96.7%" in text)
check("2c 表里同时写清了『首尾不接』口径（19.4% / 90.9%），不让读者猜",
      "19.4%" in text and "90.9%" in text and "成圈" in text)

print("\n=== 三、真发牌抽样（走引擎自己的洗牌路径）跟枚举收敛 ===")
got = sample(60_000)
for key in ("全场至少一处3连神(成圈)", "全场至少一处3连好(成圈)"):
    diff = abs(got[key] - rows[key]) * 100
    check(f"3 {key}：抽样 {got[key] * 100:.2f}% vs 枚举 {rows[key] * 100:.2f}%（差 {diff:.2f} 个点）",
          diff < 0.5)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
