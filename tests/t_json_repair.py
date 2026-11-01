# coding=utf-8
"""回归：JSON 温和修复（模型层审查的 P2-1）。

要钉死的行为：
  · 只去尾逗号能救的，就**只**去尾逗号 —— 不许把中文全角标点改成英文
  · 单引号 + Python 字面量能救
  · 全角括号/冒号能救
  · 正文里带双引号/中文标点的复杂串也能救（旧版"叠满替换"会把它弄坏）
"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.llm import _gentle_repair_steps, extract_json   # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


# ① 只需要去尾逗号的那种：正文里的中文标点必须原样保留
t1 = '{"speech": "投 3 号，理由：他太急。", "vote": 3,}'
try:
    got = extract_json(t1)
    check("1a 尾逗号能解析", got.get("vote") == 3, str(got))
    check("1b 正文里的中文标点和原文一字不差",
          got.get("speech") == "投 3 号，理由：他太急。", repr(got.get("speech")))
except Exception as exc:                                   # noqa: BLE001
    check("1a 尾逗号能解析", False, f"{type(exc).__name__}: {exc}")
    check("1b 正文里的中文标点和原文一字不差", False, "解析就失败了")

# ② 中文全角引号 + 尾逗号（旧版叠满替换会弄坏的那条）
t2 = '{"speech": "3号，我信他“不是狼”，先不动", "claim": "无",}'
try:
    got = extract_json(t2)
    check("2a 中文全角引号 + 尾逗号能解析", got.get("claim") == "无", str(got)[:60])
    check("2b 发言正文没被改字", "“不是狼”" in str(got.get("speech")), repr(got.get("speech")))
except Exception as exc:                                   # noqa: BLE001
    check("2a 中文全角引号 + 尾逗号能解析", False, f"{type(exc).__name__}: {exc}")
    check("2b 发言正文没被改字", False, "解析失败")

# ③ 单引号 + Python 字面量
try:
    got = extract_json("{'save': True, 'poison': None}")
    check("3a 单引号 + Python 字面量能解析",
          got == {"save": True, "poison": None}, str(got))
except Exception as exc:                                   # noqa: BLE001
    check("3a 单引号 + Python 字面量能解析", False, f"{type(exc).__name__}: {exc}")

# ④ 全角括号/冒号
try:
    got = extract_json('｛"save"：false｝')
    check("4a 全角括号冒号能解析", got == {"save": False}, str(got))
except Exception as exc:                                   # noqa: BLE001
    check("4a 全角括号冒号能解析", False, f"{type(exc).__name__}: {exc}")

# ⑤ 转义双引号（模型互相引用时的常见写法）
try:
    got = extract_json('{"vote": 3, "reason": "他说\\"我投3\\""}')
    check("5a 正文带转义双引号能解析", got.get("vote") == 3, str(got)[:70])
except Exception as exc:                                   # noqa: BLE001
    check("5a 正文带转义双引号能解析", False, f"{type(exc).__name__}: {exc}")

# ⑥ 逐步修复的顺序：前几步不能动中文标点
# ── 裸座位号：模型很爱写 `{"vote": 2号}`（值不加引号）→ JSON 非法 ──────────
# ⚠️ 实测来源（2026-10-10）：11号（小米 mimo）投警徽票时就这么写，
#    解析失败 → 自动换家到 DeepSeek 才答上。这条同时守住两个坑：
#      · `extract_json` 曾写死 `range(4)`，新加的修复步**永远跑不到**；
#      · 修复是累积的，"全角→半角"若排在前面会把中文标点改成半角 —— 那是公开发言的原文。
got = extract_json('{"vote": 2号, "reason": "6号没对跳，先把警徽给真预言家。"}')
check("裸 N号 能解析成字符串（下游 coerce_seat 会用正则抠数字）",
      got.get("vote") == "2号", str(got))
check("修 JSON 不许动中文标点（逗号还是全角的）",
      "，" in str(got.get("reason")), str(got.get("reason")))
got2 = extract_json('{"speech": "先出2号", "claim": "无"}')
check("字符串内部的 2号 一个字节都不许动",
      got2.get("speech") == "先出2号", str(got2))
got3 = extract_json('{"poison": 11号}')
check("只写编号的字段也能解析", got3.get("poison") == "11号", str(got3))

steps = _gentle_repair_steps(t1)
check("6a 第一步（只去尾逗号）保留中文标点",
      "，" in steps[0] and "：" in steps[0], steps[0])
# ⚠️ 2026-10-10：从 4 步加到 5 步 —— 第 4 步是「裸 N号 加引号」（实测小米那样写过），
#    「全角→半角」被挤到最后一步。理由：修复是**累积**的，而全角替换会改掉中文标点
#    （，→,）—— 那是要进台账当公开发言的原文，能不碰就不碰。
#    这条同时在守一个坑：`extract_json` 曾写死 `range(4)`，新加的步**永远跑不到**。
t_bare = '{"vote": 2号, "reason": "他急，先投他。"}'
steps2 = _gentle_repair_steps(t_bare)
check("6b 五步：第 4 步加引号（中文标点原样），第 5 步才是全角替换（会动标点）",
      len(steps2) == 5 and '"2号"' in steps2[3] and "，" in steps2[3] and "，" not in steps2[4],
      f"{len(steps2)} 步；第4步={steps2[3][:44]!r}；第5步={steps2[4][:44]!r}")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
