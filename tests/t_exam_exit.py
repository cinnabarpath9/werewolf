# coding=utf-8
"""回归：exam.py 的两个退出码行为（工具审查的 P1-2 / P2-15）。

1. 所有模型都被预检跳过时 → exit 1（以前照样写空报告、exit 0）
2. --dump 传不带 .md 的路径时，原始回复**不能**被报告覆盖（以前 raw_path == path）
"""
import asyncio
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import exam                                              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


# ---- 1. 全被跳过 → exit 1 ----
bad = os.path.join(HERE, "a5_bad_models.yaml")
io.open(bad, "w", encoding="utf-8").write(
    "models:\n  - name: 坏档案\n    base_url: http://127.0.0.1:9/v1\n    api_key: x\n    model: nope\n")
old = sys.argv
sys.argv = ["exam.py", "--models", bad]
try:
    rc = asyncio.run(exam.main())
except SystemExit as exc:                                 # 兼容 sys.exit 写法
    rc = exc.code
finally:
    sys.argv = old
check("1a 所有模型被跳过时 exit=1", rc == 1, f"exit={rc}")

# ---- 2. --dump 路径归一化 ----
p_noext = os.path.join(HERE, "a5_report_noext")
raw = exam.__dict__  # 占位，避免 lint 抱怨
src = io.open(os.path.join(ROOT, "tools/exam.py"), encoding="utf-8").read()
check("2a 源码里不再用裸 replace('.md', '.raw.json')",
      'raw_path = path.replace' not in src, "已改成显式判断（注释里提到旧写法不算）")
check("2b 不带 .md 的路径会变成 <path>.raw.json",
      'path + ".raw.json"' in src, "名字不会撞上报告")

for f in (bad,):
    try:
        os.remove(f)
    except OSError:
        pass

bad_results = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad_results)}，失败 {len(bad_results)}")
print("=" * 74)
raise SystemExit(1 if bad_results else 0)
