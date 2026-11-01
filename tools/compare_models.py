# coding=utf-8
"""同一个局面、同一份上下文，发给不同模型，比较它们「怎么说、花多久、烧多少 token」。

做法是可控的：先用固定种子跑一局离线对局，**抓取某个座位在某一天的真实上下文**，
然后把这**同一份** packet 原样发给每个模型。这样比的就是模型，不是局面。

跑法：
    python tools/compare_models.py                        # 默认比一组
    python tools/compare_models.py --day 3 --seat 5       # 换成第 3 天 5 号发言
    python tools/compare_models.py --probe-reasoning      # 测网关认哪些"推理强度"参数
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from wolf import routing  # noqa: E402
from wolf.agents import MockAgent  # noqa: E402
from wolf.engine import Game  # noqa: E402
from wolf.llm import LLMError, extract_json  # noqa: E402
from wolf.roles import make_board  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (显示名, 用哪个平台档案拿地址和 key, 模型名, 额外参数)
DEFAULT_COMBOS = [
    ("qwen3.8-flash 关思考", "QWEN", "qwen3.8-flash", {"enable_thinking": False}),
    ("qwen3.8-flash 开思考", "QWEN", "qwen3.8-flash", {"enable_thinking": True}),
    ("qwen3.8-max   关思考", "QWEN", "qwen3.8-max", {"enable_thinking": False}),
    ("deepseek-v4.1-flash", "DEEPSEEK", "deepseek-v4.1-flash", {}),
    ("glm-5.3-flash", "GLM", "glm-5.3-flash", {}),
]

#: 测"推理强度"参数：网关到底认哪个
REASONING_COMBOS = [
    ("不传任何参数", {}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("enable_thinking=true", {"enable_thinking": True}),
    ("thinking_budget=512", {"enable_thinking": True, "thinking_budget": 512}),
    ("reasoning_effort=low", {"reasoning_effort": "low"}),
    ("reasoning_effort=high", {"reasoning_effort": "high"}),
]


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


async def run_capture(cfg: dict, board_name: str, seat: int, day: int):
    seen: list[tuple[int, int, str]] = []
    state = {"day": 0}

    def on_event(kind, **data):
        if kind == "phase":
            state["day"] = data["day"]
        elif kind == "packet" and data.get("task") == "speech":
            seen.append((state["day"], data["seat"], data["packet"]))

    board = make_board(board_name, (cfg.get("game") or {}).get("boards"))
    rng = random.Random(7)
    game = Game(board=board, seed=20261005, agent_factory=lambda s: MockAgent(s, rng),
                on_event=on_event)
    await game.run()

    exact = [p for p in seen if p[0] == day and p[1] == seat]
    pool = exact or [p for p in seen if p[1] == seat] or seen
    if not pool:
        raise SystemExit("没抓到任何发言上下文")
    chosen = pool[len(pool) // 2]                     # 取中间那一份，比第一份更真实
    return chosen, game, board


def spec_for(profile: str, model: str, extra: dict) -> routing.SeatLLM:
    base_url = os.environ.get(f"{profile}_BASE_URL", "")
    api_key = os.environ.get(f"{profile}_API_KEY", "")
    if not (base_url and api_key):
        raise SystemExit(f"平台档案 {profile} 没配好（检查 .env）")
    return routing.SeatLLM(base_url=base_url, api_key=api_key, model=model,
                           timeout=120, max_tokens=1600, extra_body=extra)


async def ask(label: str, spec: routing.SeatLLM, packet: str) -> dict:
    client = routing.build_client(spec, label=label)
    started = time.time()
    result = {"label": label, "model": spec.model}
    try:
        reply = await client.chat_json(packet)
        result.update(ok=True, reply=reply, elapsed=round(time.time() - started, 1),
                      usage=client.usage)
    except LLMError as exc:
        result.update(ok=False, error=str(exc)[:200], elapsed=round(time.time() - started, 1))
    except Exception as exc:                          # noqa: BLE001
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200], elapsed=0)
    finally:
        await client.aclose()
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="同一份上下文，比不同模型")
    parser.add_argument("--board", default="eleven_wolfking")
    parser.add_argument("--seat", type=int, default=3)
    parser.add_argument("--day", type=int, default=2)
    parser.add_argument("--probe-reasoning", action="store_true",
                        help="测网关认哪些推理强度参数（用 qwen3.8-flash）")
    parser.add_argument("--repeat", type=int, default=1, help="每个组合重复几次（看稳定性）")
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    (day, seat, packet), game, board = await run_capture(cfg, args.board, args.seat, args.day)
    print("=" * 78)
    print(f"用的是第 {day} 天 · {seat}号 的一次真实发言上下文")
    print(f"这份上下文 {len(packet)} 字（约 {len(packet) * 0.6:.0f} tokens）—— 含全场发言叠加")
    print("=" * 78)

    combos = ([(f"qwen3.8-flash · {name}", "QWEN", "qwen3.8-flash", extra)
               for name, extra in REASONING_COMBOS]
              if args.probe_reasoning else DEFAULT_COMBOS)

    results = []
    for label, profile, model, extra in combos:
        for i in range(args.repeat):
            tag = label if args.repeat == 1 else f"{label} #{i + 1}"
            try:
                spec = spec_for(profile, model, extra)
            except SystemExit as exc:
                print(f"跳过 {tag}：{exc}")
                continue
            res = await ask(tag, spec, packet)
            results.append(res)
            used = res.get("usage") or {}
            if res.get("ok"):
                reply = res["reply"]
                print(f"\n▸ {tag}")
                print(f"   {res['elapsed']}s · 输入 {used.get('prompt_tokens', 0)} / "
                      f"输出 {used.get('completion_tokens', 0)} tokens")
                print(f"   发言：{reply.get('speech', '（没有 speech 字段）')}")
                if reply.get("claim") and str(reply["claim"]) not in ("无", "None"):
                    print(f"   声称：{reply['claim']}")
                if reply.get("reason"):
                    print(f"   它的理由：{str(reply['reason'])[:120]}")
            else:
                print(f"\n▸ {tag}\n   ❌ {res['error']}")

    print("\n" + "=" * 78)
    print("汇总：")
    print(f"{'组合':<28}{'耗时':>8}{'输入':>8}{'输出':>8}   格式")
    for res in results:
        used = res.get("usage") or {}
        if res.get("ok"):
            reply = res.get("reply") or {}
            fields = [k for k in ("speech", "claim", "suspect", "reason") if k in reply]
            fmt = "齐全" if len(fields) >= 3 else f"只有 {','.join(fields) or '空'}"
            print(f"{res['label']:<28}{res['elapsed']:>7}s{used.get('prompt_tokens', 0):>8}"
                  f"{used.get('completion_tokens', 0):>8}   {fmt}")
        else:
            print(f"{res['label']:<28}{res['elapsed']:>7}s{'—':>8}{'—':>8}   ❌ 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
