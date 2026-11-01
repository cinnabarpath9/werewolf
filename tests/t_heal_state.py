# coding=utf-8
"""复核 C 顺手修的女巫面板逻辑（healState）：拿引擎**真实**写出来的 ask.text 验。

⚠️ 关键事实：engine.py 第 408 行的 extra 只给了 `victim` / `has_poison`，**没有 `has_heal`**
   （C 的注释写成"engine 那边在补这个字段"，其实现在还没有），
   所以线上真正起作用的是"从 ask.text 里读「你的解药：还在 / 已用完」"那条回退路径。
"""
import io
import json
import os
import re
import subprocess
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")
text = io.open(PAGE, encoding="utf-8").read()

m = re.search(r"function healState\(ask\) \{.*?\n\}", text, re.S)
if not m:
    print("❌ 页面里找不到 healState")
    raise SystemExit(1)
fn = m.group()
print("从页面里抠出来的函数：")
print(fn)
print()

# engine.py 第 403 行真实拼出来的文案（两种状态）
REAL_LEFT = ("现在是第 2 天夜晚。今晚 5号 被狼人袭击。\n"
             "你的解药：还在；毒药：还在。\n"
             "请注意：同一夜不能同时使用解药和毒药。请决定你的行动。")
REAL_USED = ("现在是第 3 天夜晚。今晚 5号 被狼人袭击。\n"
             "你的解药：已用完；毒药：还在。\n"
             "请注意：同一夜不能同时使用解药和毒药。请决定你的行动。")
REAL_NOBODY = ("现在是第 4 天夜晚。今晚没有人被袭击。\n"
               "你的解药：已用完；毒药：已用完。\n"
               "请注意：同一夜不能同时使用解药和毒药。请决定你的行动。")

CASES = [
    ("engine 真实文案：解药还在", {"text": REAL_LEFT}, True),
    ("engine 真实文案：解药用完", {"text": REAL_USED}, False),
    ("engine 真实文案：都用完", {"text": REAL_NOBODY}, False),
    ("有 has_heal 字段时以字段为准（true）", {"text": REAL_USED, "extra": {"has_heal": True}}, True),
    ("有 has_heal 字段时以字段为准（false）", {"text": REAL_LEFT, "extra": {"has_heal": False}}, False),
    ("两样都判断不出来 → null（不许瞎禁用）", {"text": "轮到你用药了。"}, None),
]

js = f"""
{fn}
const CASES = {json.dumps([[c[0], c[1], c[2]] for c in CASES], ensure_ascii=False)};
let bad = 0;
for (const [name, ask, want] of CASES) {{
  const got = healState(ask);
  const ok = (got === want);
  if (!ok) bad++;
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + '  → ' + JSON.stringify(got) + '（期望 ' + JSON.stringify(want) + '）');
}}
console.log('\\n失败 ' + bad + ' 项');
process.exit(bad ? 1 : 0);
"""
jsfile = os.path.join(HERE, "t_heal_state.js")
io.open(jsfile, "w", encoding="utf-8").write(js)
r = subprocess.run(["node", jsfile], capture_output=True, text=True, encoding="utf-8")
print(r.stdout)
if r.stderr:
    print("stderr:", r.stderr[:500])
print("exit =", r.returncode)
