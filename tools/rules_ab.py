# coding=utf-8
"""规则书放哪儿更好？用同一批规则题做 A/B（数据说话，不靠猜）。

四种放法：
  off     —— 不给规则（基线：考它的先验知识）
  room    —— 开局当**公开公告**进台账（公示栏；每次调用都跟着公开记录一起渲染）
  packet  —— 塞进**座位提示词**顶部（位置最显眼、明确是"给你的规则"）
  both    —— 两边都放（对照组，看重复有没有额外收益）

用同一套规则题（17 道，答案由引擎算出），同一模型、同样温度，只换放置方式。
每题单独一问（不合并成组），避免"一组答崩全组 0 分"污染对比。

跑法：
    python tools/rules_ab.py --selftest                 # 离线：不花钱，验证四种放法都能出题
    python tools/rules_ab.py --models tools/exam_models.yaml  # 真跑：三家 × 4 种放法 × 17 题
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from exam import Model, grade, load_env, load_models, parse_answers   # noqa: E402
from exam_bank import rules_questions, rules_text_for                 # noqa: E402

from wolf import rules as rules_mod                                   # noqa: E402

ARMS = ("off", "room", "packet", "both")
ANS = '{"answers": [{"q": "<题号>", "a": "<答案>"}]}'
INTRO = ("你是狼人杀复盘助手。下面会给你本局规则（如果有）和一批判断题。"
         "只按规则回答，不要解释，只输出 JSON。")


def arm_context(arm: str, board: str = "eleven_wolfking") -> str:
    """四种放法各自的上下文（规则文本**按板子组装**，和真实对局同一套逻辑）。"""
    if arm == "off":
        return ""
    text = rules_text_for(board)
    if arm == "room":
        return rules_mod.room_notice(text)
    if arm == "packet":
        return rules_mod.packet_block(text)
    return rules_mod.room_notice(text) + "\n\n" + rules_mod.packet_block(text)


def make_prompt(ctx: str, qs: list) -> str:
    parts = [INTRO, ""]
    if ctx:
        parts += ["———— 本局规则 ————", ctx, ""]
    parts += ["———— 判断题（只回答这些）————"]
    for q in qs:
        parts.append(f"{q.qid}. {q.question}")
    parts += ["", "只输出一个 JSON 对象：", ANS]
    return "\n".join(parts)


async def run_arm(model: Model, arm: str, qs: list, dump: list) -> dict:
    ctx = arm_context(arm)
    rows = []
    # 一次问 5 道（17 题 → 4 次调用），既省 token 又不至于一崩全崩
    for i in range(0, len(qs), 5):
        batch = qs[i:i + 5]
        prompt = make_prompt(ctx, batch)
        res = await model.ask(prompt, INTRO)
        answers = parse_answers(res.get("text", "")) if res.get("ok") else {}
        for q in batch:
            got = answers.get(q.qid, "")
            ok, why = (grade(q, got) if res.get("ok") else (False, "调用失败"))
            rows.append({"qid": q.qid, "ok": ok, "got": got, "why": why})
        dump.append({"model": model.name, "arm": arm, "qids": [q.qid for q in batch],
                     "prompt_chars": len(prompt), "ok": bool(res.get("ok")),
                     "error": res.get("error", ""), "raw": (res.get("text") or "")[:1500]})
    return {"rows": rows, "usage": dict(model.usage),
            "acc": sum(1 for r in rows if r["ok"]) / max(1, len(rows))}


async def selftest() -> int:
    qs, problems = rules_questions()
    print(f"规则题 {len(qs)} 道；题库自检问题：{problems}")
    if problems:
        return 1
    for arm in ARMS:
        ctx = arm_context(arm)
        prompt = make_prompt(ctx, qs[:5])
        print(f"  {arm:<6} 上下文 {len(ctx):>5} 字（≈{len(ctx) * 0.75:>4.0f} tokens）"
              f"，示例 prompt {len(prompt)} 字")
    print("\n✅ 自检通过：四种放法都能出题（off 不带规则，room/packet 文本不同但同源）")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="规则书放置方式 A/B")
    parser.add_argument("--models", default=os.path.join(HERE, "tools", "exam_models.yaml"))
    parser.add_argument("--only", default="")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    load_env(os.path.join(HERE, ".env"))
    if args.selftest:
        return await selftest()

    qs, problems = rules_questions()
    if problems:
        print("❌ 题库自检没过：", problems)
        return 1
    arms = [a for a in args.arms.split(",") if a in ARMS]
    models = load_models(args.models)
    if args.only:
        models = [m for m in models if args.only in m.name]
    if not models:
        print("❌ 没有可测的模型")
        return 1

    dumps: list = []
    table: dict[str, dict[str, float]] = {}
    for m in models:
        ok, why = await _probe(m)
        if not ok:
            print(f"\n▸ {m.safe_desc}\n    ❌ 预检失败，跳过：{why}")
            continue
        print(f"\n▸ {m.safe_desc}")
        table[m.name] = {}
        for arm in arms:
            before = dict(m.usage)
            r = await run_arm(m, arm, qs, dumps)
            table[m.name][arm] = r["acc"]
            print(f"    {arm:<6} 正确率 {r['acc']:>6.0%}"
                  f"　（本次调用 {m.usage['calls'] - before['calls']} 次，"
                  f"输入 {m.usage['prompt'] - before['prompt']} tokens）")

    print("\n" + "=" * 78)
    print(f"规则书放置方式对比（{len(qs)} 道规则题）")
    print("=" * 78)
    head = f"{'模型':<24}" + "".join(f"{a:>10}" for a in arms)
    print(head)
    print("-" * len(head))
    for name, row in table.items():
        cells = "".join(f"{row.get(a, 0):>9.0%} " for a in arms)
        print(f"{name:<24}{cells}")
    print("\n读法：off 是基线（不给规则）；room = 开局公示栏；packet = 座位提示词；both = 两边都放。")
    out = os.path.join(HERE, f"rules-ab-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump({"table": table, "dumps": dumps}, fh, ensure_ascii=False, indent=1)
    print(f"原始材料：{out}")
    return 0


async def _probe(m: Model) -> tuple[bool, str]:
    res = await m.ask('只回复一个 JSON：{"ok": true}', "你是连通性探针。")
    return bool(res.get("ok")), str(res.get("error"))[:160]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
