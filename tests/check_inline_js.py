# coding=utf-8
"""内联 JS 语法闸门：改前端之后**秒级**抓出语法错误。

为什么需要它（2026-10-09 实测踩到）：
  改 `web/index.html` 时，一次替换**吃掉了上一个函数结尾的 `}`** ——
  页面于是整块 JS 语法错误、所有控件都不渲染。而这种错**只有"真浏览器"那一档测试**才发现
  （`t_page_*` / `t_frontend_*` 在全量档），`--fast` / `--quick` 全是绿的 ——
  也就是说：**日常回归会假装一切正常，直到跑全量或你打开手机才发现页面是白的。**

做法：抽出内联 JS（复用 `tests/extract_js.py` 的解析），再用 `node --check` 做语法体检。
没装 node 就 SKIP（不假装通过，也不误报失败）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


node = shutil.which("node")
if not node:
    print("  ⏭ SKIP：这台机器上没有 node，做不了 JS 语法体检（改成人工跑全量档）")
    raise SystemExit(0)

# ① 抽内联 JS（extract_js.py 会把结果写到 tests/index_inline.js）
r = subprocess.run([sys.executable, os.path.join(HERE, "extract_js.py")],
                   capture_output=True, text=True, encoding="utf-8", cwd=HERE)
inline = os.path.join(HERE, "index_inline.js")
check("1a 内联 JS 抽出来了", r.returncode == 0 and os.path.isfile(inline),
      (r.stdout or r.stderr or "").strip().splitlines()[-1] if (r.stdout or r.stderr) else "")
if not os.path.isfile(inline):
    print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
    raise SystemExit(1)

# ② 语法体检
chk = subprocess.run([node, "--check", inline], capture_output=True, text=True,
                     encoding="utf-8", errors="replace")
check("2a 内联 JS 语法通过（node --check）", chk.returncode == 0,
      (chk.stderr or "").strip().splitlines()[0] if chk.stderr else "")

# ③ 再抓一个常见事故：**大括号是否配平**（语法检查已覆盖，但这条信息更好读）
src = open(inline, encoding="utf-8").read()
check("3a 大括号数量配平", src.count("{") == src.count("}"),
      f"{{ ×{src.count('{')}  }} ×{src.count('}')}")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
