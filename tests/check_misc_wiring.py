# coding=utf-8
"""小验证：秒数格式化 + 存档口径字段 + 考试工具的接线。"""
import io
import sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import _secs                             # noqa: E402

print("=== 一、_secs（别再印出“超过 0 秒”）===")
for v in (150, 0.5, 30.0, 300, "x"):
    print(f"  _secs({v!r}) = {_secs(v)!r}")

print("\n=== 二、to_dict 里有没有记下新口径 ===")
src = io.open(ROOT + r"\wolf\engine.py", encoding="utf-8").read()
block = src[src.index("def to_dict"):src.index("def save")]
for key in ("edge_order", "rules_mode", "rules_chars", "prompt_layers", "card_name"):
    print(f"  {key:<15}: {key in block}")

print("\n=== 三、exam_live 有没有跟上上限/提示层 ===")
ex = io.open(ROOT + r"\tools\exam_live.py", encoding="utf-8").read()
for key in ("call_timeout", "decision_timeout", "playbook", "meta_book", "edge_order"):
    print(f"  {key:<16}: {key in ex}")

# ⚠️ 2026-10-09 补：以前只 print 一堆 True/False，没有退出码（假回归门）。
# ⚠️ 2026-10-09 修判据：`prompt_layers` 是**旧设计的字段**，现在提示层体量记的是
#    `player_prompt_chars / playbook_chars / meta_chars`（都在 to_dict 的 rules 快照里，
#    `card_name` 在顶层）。旧判据一直要求一个早就不存在的键 —— 属于"脚本过时"，不是引擎缺字段。
_want_engine = ("edge_order", "rules_mode", "rules_chars", "player_prompt_chars",
                "playbook_chars", "meta_chars", "card_name")
_want_exam = ("call_timeout", "decision_timeout", "playbook", "meta_book", "edge_order")
_ok = (
    all(k in block for k in _want_engine)
    and all(k in ex for k in _want_exam)
    and all(_secs(v) for v in (150, 0.5, 30.0, 300, "x"))
)
print()
print("判定：", "✅ 接线齐全" if _ok else "❌ 有字段/接线漏了")
raise SystemExit(0 if _ok else 1)
