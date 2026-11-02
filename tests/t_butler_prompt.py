# coding=utf-8
"""托管模型的两个修复（2026-11-03）：发言补强 + 思考档真的下发。

起因：存档 `game-20261103-020633.json` 里，托管座位（6 号）三句发言分别是 24 / 28 / 30 字，
而同桌 AI 座位平均 243 字。查下来：托管拿的是人类面板 packet（没有"先想清楚再说话"的脚手架），
并且固定 plain 风格 → 配置的思考档根本没发出去。

这个脚本只测纯函数（不花模型钱）：
  · `butler.augment_packet()`：只有发言类补、幂等、不碰别的任务；
  · `butler.infer_thinking_style()`：按渠道推断，未知一律 plain（宁可不发，不发错）；
  · `butler.thinking_table()`：档位表形状对、非法档位为空。

跑法：python tests/t_butler_prompt.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import butler                                       # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


PACKET = "【当前任务】第 2 天白天，轮到你发言。请说明你的判断。350 字以内。\n【输出格式】{...}"

print("=== 一、发言类补强 ===")
for kind in ("speech", "speech_plan", "sheriff_speech", "last_words"):
    got = butler.augment_packet(PACKET, kind)
    check(f"1a {kind} 补上了托管要求", "【托管要求" in got and "不少于 80 字" in got)
check("1b 补强是幂等的（调用两次不会叠两段）",
      butler.augment_packet(butler.augment_packet(PACKET, "speech"), "speech").count("【托管要求") == 1)
check("1c 投票/用药这类任务不补（不该硬塞字数）",
      butler.augment_packet(PACKET, "vote") == PACKET
      and butler.augment_packet(PACKET, "witch") == PACKET)
check("1d 空 packet 不炸也不补", butler.augment_packet("", "speech") == "")
check("1e 原 packet 内容不丢（只追加）", butler.augment_packet(PACKET, "speech").startswith(PACKET))

print("\n=== 二、按渠道推断思考参数 ===")
check("2a 火山方舟 → ark",
      butler.infer_thinking_style("https://ark.cn-beijing.volces.com/api/v3") == "ark")
check("2b 千帆 → openai", butler.infer_thinking_style("https://qianfan.baidubce.com/v2") == "openai")
check("2c OpenAI → openai", butler.infer_thinking_style("https://api.openai.com/v1") == "openai")
check("2d 未知渠道 → plain（宁可不发，不发错字段）",
      butler.infer_thinking_style("https://api.52pay.com/v1") == "plain")

print("\n=== 三、档位表 ===")
for lv in ("off", "low", "high"):
    table = butler.thinking_table(lv)
    ok = all(table.get(g, {}).get(1) == lv for g in ("speech", "decision", "aux"))
    check(f"3a {lv} 三组都设了", ok, str(table))
check("3b 空/非法档位 → 空表（跟随渠道默认）",
      butler.thinking_table("") == {} and butler.thinking_table("唬人") == {})

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)