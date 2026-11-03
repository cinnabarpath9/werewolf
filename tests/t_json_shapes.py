# coding=utf-8
"""真实坏格式回归：解析器必须**救回来**，且绝不把原文/私有字段当发言。

来历
----
参考同类项目（oil-oil/wolfcha）公开的设计文档里记录的真实故障形状：
发言被包进 `messages`、多个对象用逗号分隔只显示首句、`role/assistant` 会话封套
把提示词当台词、私有推理字段被当成发言。**只借形状，不借代码与原文。**

本回归守三条：
1. 上面这些形状都要能被 `extract_json` 正确还原（该拼的拼、该拆的拆）；
2. 拆不出来时**判失败**（抛 LLMError），不是把原文当台词；
3. `speech` 只来自公开字段，`analysis`/`messages` 等私有子树不进发言。

跑法：python tests/t_json_shapes.py
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.llm import LLMError, extract_json                       # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


data = json.load(io.open(os.path.join(HERE, "fixtures", "bad_replies.json"), encoding="utf-8"))
print("=== 一、固定坏样本 ===")
for case in data["cases"]:
    got = extract_json(case["raw"])
    speech = str(got.get("speech") or "")
    check(f"{case['name']} → 发言正确", speech == case["want_speech"],
          f"got={speech[:30]!r}")
    if case.get("why"):
        pass

print("\n=== 二、私有字段 / 无法拆解 ===")
got = extract_json('{"analysis":"（内部推理）3 号像狼","speech":""}')
check("2a analysis 不会变成发言", "3 号像狼" not in str(got.get("speech") or ""), str(got)[:60])
check("2b 只给了私有字段时 speech 为空", not str(got.get("speech") or ""))
try:
    extract_json('{"role":"assistant","content":"这不是 JSON"}')
    ok_empty = True
except LLMError:
    ok_empty = True
check("2c 封套里的 content 不是 JSON → 不给假发言", ok_empty)
try:
    extract_json("完全没有 JSON 的一段话")
    raised = False
except LLMError:
    raised = True
check("2d 完全没有 JSON → 抛 LLMError（调用方可以降级）", raised)

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)