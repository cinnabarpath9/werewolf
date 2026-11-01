# coding=utf-8
"""真模型冒烟测试：整局只让一个座位用真模型，其余用离线代理。

比"全真模型打一局"便宜几十倍，但能验证最关键的一件事：
**游戏里那份长上下文（身份 + 台账 + 任务 + 输出格式）真模型能不能吃下、能不能回合规 JSON。**

跑法：
    python tools/smoke_real.py --board nine_standard --seat 1
    python tools/smoke_real.py --board eleven_standard --seat 1 --show-packet
"""

from __future__ import annotations

import argparse
import io
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from wolf import persona, routing  # noqa: E402
from wolf.agents import LLMAgent, MockAgent  # noqa: E402
from wolf.engine import Game  # noqa: E402
from wolf.roles import ROLES, board_summary, make_board  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


async def main() -> int:
    parser = argparse.ArgumentParser(description="真模型冒烟测试")
    parser.add_argument("--board", default="eleven_wolfking",
                        help="eleven_wolfking（默认）/ twelve_standard / nine_all / six_all_reveal（或 config 里的自定义板子）")
    parser.add_argument("--seat", type=int, default=1, help="哪个座位用真模型")
    parser.add_argument("--profile", default="", help="指定用哪个平台档案（例如 DEEPSEEK）")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--show-packet", action="store_true", help="打印第一次的真实上下文")
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    game_cfg = cfg.get("game") or {}

    board = make_board(args.board, game_cfg.get("boards"))
    rng = random.Random(args.seed)
    clients = []
    shown = {"done": False}

    if args.profile:
        os.environ["WOLF_SEAT_PROFILES"] = f"{args.seat}:{args.profile}"

    def factory(seat):
        if seat.id != args.seat:
            return MockAgent(seat, rng)
        spec = routing.resolve(cfg, seat)
        if not spec.ready:
            raise SystemExit(f"❌ {seat.id}号 拿不到配置：检查 {routing.env_hint(cfg, seat)}")
        print(f"→ {seat.id}号 用真模型：{spec.host}/{spec.model}（key ****{spec.api_key[-4:]}）")
        # ⚠️ 思考档跟生产走：不设的话会退到代码兜底默认，冒烟测出来的
        #    延迟/token 量跟真局不可比（见 exam_live 里 _prod_thinking 的说明）。
        try:
            _llm = ((yaml.safe_load(io.open(os.path.join(HERE, "config.yaml"),
                                             encoding="utf-8")) or {}).get("llm") or {})
            spec.thinking = dict(_llm.get("thinking") or {}) or spec.thinking
        except Exception:                     # noqa: BLE001
            pass
        client = routing.build_client(spec, label=f"{seat.id}号/{spec.model}")
        clients.append(client)
        return LLMAgent(client, seat.id)

    def on_event(kind, **data):
        if (kind == "packet" and data["seat"] == args.seat
                and args.show_packet and not shown["done"]):
            shown["done"] = True
            print("\n" + "-" * 78)
            print(data["packet"])
            print("-" * 78 + "\n")
        elif kind == "warn":
            print(f"  ⚠️ {data['seat']}号 调用失败：{data['message']}")

    game = Game(board=board, seed=args.seed, agent_factory=factory, on_event=on_event)
    personas = persona.load(cfg, HERE)
    for seat in game.seats:
        if seat.id in personas:
            seat.persona = personas.get(seat.id)
            seat.card_name = personas.names().get(seat.id, "")

    print(f"板子：{board_summary(board)}，只有 {args.seat}号 用真模型\n")
    winner = await game.run()
    for client in clients:
        await client.aclose()

    mine = [row for row in game.transcript if row["seat"] == args.seat]
    print("\n" + "=" * 78)
    print(f"结果：{winner}（第 {game.day} 天）· {args.seat}号 是 "
          f"{ROLES[game.seat_by_id(args.seat).role_key].name}")
    print(f"{args.seat}号 真模型调用 {len(mine)} 次：")
    for row in mine:
        reply = row.get("reply") or {}
        what = reply.get("speech") or reply.get("reason") or str(reply)[:60]
        print(f"  第{row['day']}天 {row['task']:<12} {row['elapsed']:>5}s  "
              f"{row['prompt_tokens']:>5}/{row['completion_tokens']:<4} tokens  {str(what)[:60]}")

    errors = [r for r in mine if r.get("error")]
    total_in = sum(r["prompt_tokens"] for r in mine)
    total_out = sum(r["completion_tokens"] for r in mine)
    print(f"\n合计：{len(mine)} 次调用，输入 {total_in}，输出 {total_out} tokens，"
          f"失败 {len(errors)} 次")
    if errors:
        print("❌ 有调用失败：")
        for row in errors[:3]:
            print("   " + row["error"][:160])
        return 1
    print("✅ 游戏里的长上下文 + JSON 契约，真模型能吃能回")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
