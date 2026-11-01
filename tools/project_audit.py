# coding=utf-8
"""项目自查（定期跑一次的那种，不是回归的一部分）：找"结构性的烂"。

跟 `tests/check_gates.py` 的分工：
  · `check_gates.py` 是**回归里的闸门**（每个测试脚本必须能变红）—— 它管一类问题；
  · 这个工具是**更宽的自查报告**：没读的配置键、死函数、没登记的测试、临时垃圾文件。

用法：`python tools/project_audit.py`（有问题退出码 1，方便手动加进任何自动化）。
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

#: 不是"测试"、不要求退出码的夹具/工具
NOT_TESTS = {"run_all.py", "cdp_lite.py", "extract_js.py", "fake_llm.py", "fake_asr.py",
             "fake_llm_gate.py", "check_api.py", "mutation_verify.py"}


def read(p) -> str:
    return pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    ap = argparse.ArgumentParser(description="项目自查：死配置 / 死代码 / 漏登记的测试 / 垃圾文件")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    bad: list[str] = []

    print("=== 1 配置键：config.yaml 里有、代码里从来没读 ===")
    cfg = read(os.path.join(ROOT, "config.yaml"))
    keys = {m.group(1) for m in re.finditer(r"^\s{2,}([a-z_][a-z0-9_]*):", cfg, re.M)}
    code = "".join(read(p) for p in pathlib.Path(ROOT, "wolf").glob("*.py"))
    code += read(os.path.join(ROOT, "server.py")) + read(os.path.join(ROOT, "run_game.py"))
    unread = sorted(k for k in keys
                    if f'"{k}"' not in code and f"'{k}'" not in code and f".{k}" not in code)
    print("  ", unread or "（无）")
    if unread:
        bad.append(f"{len(unread)} 个配置键没人读")

    print("\n=== 2 死函数：wolf/ 里定义了但全项目只有定义那一处 ===")
    defs: dict[str, list[str]] = {}
    for p in pathlib.Path(ROOT, "wolf").glob("*.py"):
        for m in re.finditer(r"^\s*(?:async )?def ([a-zA-Z_][a-zA-Z0-9_]*)\(", read(p), re.M):
            defs.setdefault(m.group(1), []).append(p.name)
    allsrc = code
    for d in ("tests", "tools"):
        for p in pathlib.Path(ROOT, d).glob("*.py"):
            allsrc += read(p)
    dead = [f"{k}({','.join(v)})" for k, v in defs.items()
            if k != "__init__" and not k.startswith("_") and allsrc.count(k) <= 1]
    print("  ", dead or "（无）")
    if dead:
        # 死函数不一定是 bug（有些是"兼容老接口"故意留的），所以只提示、不判红
        print("   ⚠️ 死函数只是提示：先看它有没有「兼容老接口」之类的说明，再决定删还是补测试")

    print("\n=== 3 没登记进 run_all 的测试脚本（= 永远不会被跑到）===")
    runner = read(os.path.join(ROOT, "tests", "run_all.py"))
    missing = [p.name for p in sorted(pathlib.Path(ROOT, "tests").glob("*.py"))
               if p.name not in NOT_TESTS and not p.name.startswith(("_", "tmp"))
               and p.name not in runner]
    print("  ", missing or "（无）")
    if missing:
        bad.append(f"{len(missing)} 个脚本没登记：{missing}")

    print("\n=== 4 仓库里的临时垃圾（.partial / .orig / 空目录 / 巨大文件）===")
    junk: list[str] = []
    for p in pathlib.Path(ROOT).rglob("*"):
        if any(x in p.parts for x in (".git", "games", "var")) or not p.is_file():
            continue
        if p.name.startswith(".env"):
            continue          # 用户自己的 .env / .env.bak：密钥备份，**不许动也不许报**（已被 .gitignore 挡住）
        if p.suffix in (".partial", ".orig", ".bak", ".rej") or p.name.startswith("tmp_"):
            junk.append(str(p.relative_to(ROOT)))
    print("  ", junk or "（无）")
    if junk:
        bad.append(f"{len(junk)} 个临时文件残留")

    print("\n=== 5 测试脚本的闸门（这条由 tests/check_gates.py 守）===")
    print("  跑 `python tests/check_gates.py` —— 它要求每个脚本都有「能变红」的路")

    print("\n" + ("✅ 自查通过" if not bad else "❌ " + "；".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
