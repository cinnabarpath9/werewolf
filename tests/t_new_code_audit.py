# coding=utf-8
"""复查收尾：把"新增代码 vs 原代码"的接线项再机械扫一遍，并跑关键回归。

这一轮的复查发现（详见 HANDOFF §22）：
  P1  call_timeout 把真人座位也砍了（会顶掉 human_timeout=900 秒）→ 已修
  P3  失败文案 `int(call_timeout)` 在 0.5 秒时印成"超过 0 秒" → 已修（_secs）
  P3  存档没记 edge_order / 提示层 / 两个超时 → 已补进 to_dict().rules
  P3  tools/exam_live.py 没吃生产的上限与提示层 → 已补（考试成绩才跟真局可比）
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


print("=== 一、全部 .py 语法 + Game(...) 关键字无重复 ===")
bad_syntax = []
for base, _d, names in os.walk(ROOT):
    if any(x in base for x in ("__pycache__", "var", "games", "legacy", "_logs")):
        continue
    for n in names:
        if not n.endswith(".py"):
            continue
        path = os.path.join(base, n)
        try:
            tree = ast.parse(io.open(path, encoding="utf-8-sig").read())
        except SyntaxError as exc:
            bad_syntax.append(f"{os.path.relpath(path, ROOT)}:{exc.lineno}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Game":
                keys = [k.arg for k in node.keywords if k.arg]
                dup = sorted({k for k in keys if keys.count(k) > 1})
                if dup:
                    bad_syntax.append(f"{os.path.relpath(path, ROOT)}:{node.lineno} 重复参数 {dup}")
check("1a 没有语法错误 / 重复关键字", not bad_syntax, "；".join(bad_syntax[:4]))

print("\n=== 二、真人座位不被 call_timeout 砍（P1 修复点）===")
eng = read("wolf/engine.py")
seg = eng[eng.index("async def one_call"):eng.index("if not isinstance(got, dict)")]
check("2a 上限由 uses_model 把关（行为另见 t_call_timeout_human 6/6）",
      "uses_model" in seg and "if uses_model" in seg,
      "命中：" + str(re.findall(r"if uses_model[^\n]*", seg))[:90])
check("2c 重试只拿剩余预算（不会把 300 秒翻倍）",
      "budget = min(budget" in seg and "deadline - time.time()" in seg)
check("2b 失败文案用 _secs（不再 int()）", "_secs(self.call_timeout)" in seg and "int(self.call_timeout)" not in seg)

print("\n=== 三、存档口径（to_dict().rules）===")
rules_block = eng[eng.index('"rules": {'):eng.index('"sheriff_id"')]
for key in ("edge_order", "player_prompt_chars", "playbook_chars", "meta_chars",
            "call_timeout", "decision_timeout", "rules_mode", "rules_chars"):
    check(f"3 {key} 已记录", key in rules_block)

print("\n=== 四、考试工具跟生产一致（P3 修复点）===")
ex = read("tools/exam_live.py")
for key in ("call_timeout", "decision_timeout", "player_prompt", "playbook_text",
            "meta_text", "edge_order"):
    check(f"4 exam_live 传了 {key}", key in ex)

print("\n=== 五、签名变过的函数：调用方都跟上了（AST 精确核对）===")
import subprocess                                        # noqa: E402
sign = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "check_call_signatures.py")],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
tail = (sign.stdout or "").strip().splitlines()[-1:] or [""]
check("5a 所有调用点的参数都能对上签名（没有过时调用）", sign.returncode == 0, tail[0].strip())
check("5b voice.tts 的 seat 是可选参数",
      "def tts(" in read("wolf/voice.py") and "seat" in read("wolf/voice.py").split("def tts(")[1][:220])

print("\n=== 六、新字段/新开关都被用上（没有“写了没人用”）===")
allsrc = "".join(read(p) for p in ("wolf/engine.py", "wolf/context.py", "wolf/tabletalk.py",
                                   "server.py", "run_game.py"))
for name in ("playbook_text", "meta_text", "player_prompt", "edge_order",
             "call_timeout", "decision_timeout"):
    check(f"6 {name} 至少被读一次", allsrc.count(name) >= 2, f"{allsrc.count(name)} 次")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n, _, d in bad:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
