# coding=utf-8
"""测每个模型「怎么才能真正关掉思考」。

背景：`enable_thinking` 是千问的扩展参数，别的厂商不一定认。
同一个网关上，不同模型的"关思考"参数可能完全不同，猜是没用的，只能一个个试。

判断标准：**输出 token 数明显变小 + 耗时明显变短** —— 那才是真关掉了。

跑法：
    python tools/probe_thinking.py
    python tools/probe_thinking.py --models QWEN GLM DEEPSEEK
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compare_models import load_env, run_capture  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 各家可能用来关思考的参数写法，全部试一遍
VARIANTS: list[tuple[str, dict]] = [
    ("什么都不传（基线）", {}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("thinking.type=disabled", {"thinking": {"type": "disabled"}}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
]

DEFAULT_TARGETS = [
    ("QWEN", "qwen3.8-flash"),
    ("GLM", "glm-5.3-flash"),
    ("DEEPSEEK", "deepseek-v4.1-flash"),
]


async def call(base_url: str, key: str, model: str, extra: dict, packet: str) -> dict:
    import httpx
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你在参与一局中文狼人杀。只输出一个 JSON 对象。"},
            {"role": "user", "content": packet},
        ],
        "temperature": 1.0,
        "max_tokens": 1600,
    }
    payload.update(extra)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(base_url.rstrip("/") + "/chat/completions",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"},
                                     json=payload)
            elapsed = time.time() - started
            if resp.status_code >= 400:
                return {"ok": False, "elapsed": round(elapsed, 1),
                        "error": f"HTTP {resp.status_code}: {resp.text[:120]}"}
            data = resp.json()
            used = data.get("usage") or {}
            msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            return {"ok": True, "elapsed": round(elapsed, 1),
                    "prompt": int(used.get("prompt_tokens") or 0),
                    "completion": int(used.get("completion_tokens") or 0),
                    "reasoning": bool(msg.get("reasoning_content")),
                    "text": (msg.get("content") or "")[:60]}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "elapsed": round(time.time() - started, 1),
                "error": f"{type(exc).__name__}: {exc}"[:120]}


async def main() -> int:
    parser = argparse.ArgumentParser(description="测每个模型怎么关思考")
    parser.add_argument("--models", nargs="*", default=None,
                        help="平台档案名，例如 QWEN GLM DEEPSEEK")
    parser.add_argument("--seat", type=int, default=3)
    parser.add_argument("--day", type=int, default=2)
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    import yaml
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    (day, seat, packet), _game, _board = await run_capture(cfg, "nine_standard", args.seat, args.day)
    print("=" * 78)
    print(f"统一用第 {day} 天 {seat}号 的一份真实上下文（{len(packet)} 字）来比")
    print("=" * 78)

    wanted = args.models or [p for p, _ in DEFAULT_TARGETS]
    targets = [(p, m) for p, m in DEFAULT_TARGETS if p in wanted]

    summary: list[tuple[str, str, str, int, float]] = []
    for profile, model in targets:
        base_url = os.environ.get(f"{profile}_BASE_URL", "")
        key = os.environ.get(f"{profile}_API_KEY", "")
        if not (base_url and key):
            print(f"\n跳过 {profile}：.env 里没配好")
            continue
        print(f"\n{'─' * 78}\n{profile} · {model}\n{'─' * 78}")
        baseline = None
        for name, extra in VARIANTS:
            res = await call(base_url, key, model, extra, packet)
            if not res["ok"]:
                print(f"  {name:<28} ❌ {res['error']}")
                continue
            if baseline is None:
                baseline = res
            drop = ""
            if baseline is not res and baseline["completion"]:
                ratio = res["completion"] / max(baseline["completion"], 1)
                if ratio < 0.5:
                    drop = f"  ← 输出只有基线的 {ratio:.0%}，像是真关掉了 ✅"
            print(f"  {name:<28} {res['elapsed']:>6}s  输出 {res['completion']:>5} tokens"
                  f"{'  (带 reasoning_content)' if res['reasoning'] else ''}{drop}")
            if drop:
                summary.append((profile, model, name, res["completion"], res["elapsed"]))

    print("\n" + "=" * 78)
    print("结论：")
    if not summary:
        print("  没有任何变体明显降低了输出量 —— 说明这个模型本来就不思考，")
        print("  或者它的关思考参数不在上面这几个里（去它的官方文档查 reasoning/thinking 参数名）")
    else:
        for profile, model, variant, completion, elapsed in summary:
            print(f"  {profile} · {model}：用「{variant}」能关掉，"
                  f"关掉后输出 {completion} tokens / {elapsed}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
