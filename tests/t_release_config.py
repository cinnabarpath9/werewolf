# coding=utf-8
"""发布包里的 config.yaml 必须是**能解析的 YAML**（2026-11-04 事故回归）。

事故
----
`release_rules.SANITIZE_TEXT["config.yaml"]` 的替换串没带缩进：
`public_https_base: ""` 被顶到第 0 列，于是从这一行起 `yaml.safe_load` 直接
`ParserError: expected <block end>` —— **1.2.15 ~ 1.2.18 的所有发布包都中了**：
别人下载后 `python server.py` 起不来（自己机器上看不出来，因为源码 config 是好的）。

现在的闸门
----------
1. `sanitize_config()` 末尾**强制再解析一次**，解析不了就当场报错（不打包）；
2. 本回归：清洗结果可解析、缩进还在、阵容为空、兜底档案合法；
3. `tools/check_release.py` 也会对打好的 zip 再验一次（打包后再兜底）。

跑法：python tests/t_release_config.py
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                      # noqa: E402
from tools.release_rules import ALLOWED_DEFAULT_PROFILES, sanitize_config   # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


raw = io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8-sig").read()
text, cfg = sanitize_config(raw)

print("=== 一、清洗结果 ===")
check("1a 清洗后的 config.yaml 能解析", isinstance(cfg, dict) and bool(cfg))
try:
    parsed = yaml.safe_load(text)
    ok_parse = isinstance(parsed, dict)
    err = ""
except Exception as exc:                                         # noqa: BLE001
    parsed, ok_parse, err = {}, False, f"{type(exc).__name__}: {exc}"
check("1b 原始文本也能解析（清洗前就是合法 YAML）", ok_parse, err or "ok")

tree = parsed.get("game") or {}
check("1c public_https_base 仍是 game 下的键（缩进没被吃掉）",
      "public_https_base" in tree, str(list(tree)[-3:]))
check("1d llm.seats 为空", (parsed.get("llm") or {}).get("seats") == {},
      str((parsed.get("llm") or {}).get("seats")))
prof = str(((parsed.get("llm") or {}).get("default") or {}).get("profile") or "").upper()
check("1e 兜底档案在允许名单里", prof in ALLOWED_DEFAULT_PROFILES, prof)

print("\n=== 二、缩进回归（这条当初就是坏的）===")
bad_lines = [ln for ln in text.splitlines()
             if ln.lstrip().startswith("public_https_base:") and not ln.startswith("  ")]
check("2a public_https_base 行有 2 空格缩进", not bad_lines, str(bad_lines[:2]))

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)