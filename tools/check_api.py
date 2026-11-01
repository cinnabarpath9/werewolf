# coding=utf-8
"""检查每个座位的 API 配置能不能真的调通。

不玩对局，只按 config.yaml 的路由，对每个"平台+模型"打一发最小请求。

跑法：
    python tools/check_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from wolf import routing  # noqa: E402
from wolf.llm import LLMError  # noqa: E402
from wolf.roles import make_board  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = (
    "（连通性测试，随便回一句即可）\n"
    "【当前任务】确认你能收到请求。\n"
    "【输出格式】只输出一个 JSON 对象：{\"ok\": true}"
)


class FakeSeat:
    def __init__(self, seat_id: int, role_key: str) -> None:
        self.id = seat_id
        self.role_key = role_key
        self.is_human = False


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
    # ⚠️ 这个脚本**每次运行都会打真实请求（花钱）**，而它以前连 argparse 都没有：
    # `--help` 被当空气、照样把整套连通性探测跑完（审计时就这么误触过一次）。
    # 现在必须有 --help / 要显式 --yes 才真打，默认只做"要打几发"的预告。
    import argparse
    ap = argparse.ArgumentParser(description="逐个平台试连通性（会真实调用、按量计费）")
    ap.add_argument("--yes", action="store_true", help="确认执行（不加它只打印计划，不发请求）")
    ap.add_argument("--repeat", type=int, default=1, help="每个座位打几发（默认 1）")
    args = ap.parse_args()

    load_env(os.path.join(HERE, ".env"))

    cfg_path = os.path.join(HERE, "config.yaml")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    game_cfg = cfg.get("game") or {}

    board = make_board(str(game_cfg.get("board") or "eleven_wolfking"), game_cfg.get("boards"))
    seats = [FakeSeat(i + 1, role) for i, role in enumerate(board.roles)]

    print("=" * 78)
    print(f"板子：{board.name}（{len(board.roles)} 个座位）")
    print(f"预计真实请求：约 {len(board.roles) * max(1, args.repeat)} 发（按平台计费）")
    if not args.yes:
        print("⚠️ 没加 --yes，只打印计划、不发请求。确认要跑就：python tools/check_api.py --yes")
        return 0
    print("=" * 78)

    # 同一个"平台+模型"只测一次
    combos: dict[str, list[int]] = {}
    spec_by_key = {}
    for seat in seats:
        spec = routing.resolve(cfg, seat)
        # 同一个"平台+模型"只测一次；没配好的座位按缺的东西归并成一组
        key = spec.key if spec.ready else f"未配置::{spec.base_url}|{spec.model}"
        combos.setdefault(key, []).append(seat.id)
        spec_by_key[key] = spec

    failures = 0
    for key, seat_ids in combos.items():
        spec = spec_by_key[key]
        who = "、".join(f"{i}号" for i in seat_ids)
        if not spec.ready:
            missing = [n for n, v in (("Base URL", spec.base_url), ("模型名", spec.model),
                                      ("API Key", spec.api_key)) if not v]
            print(f"\n❌ {who}  （平台 {spec.host or '未配置'}，模型 {spec.model or '未配置'}）")
            # 提示要按"出问题的那个座位"给，不能一律按 1 号 —— 分平台配置时会指错地方
            seat_for_hint = next((s for s in seats if s.id in seat_ids), seats[0])
            print(f"   缺 {'/'.join(missing)} —— 去填 {routing.env_hint(cfg, seat_for_hint)}")
            failures += 1
            continue

        print(f"\n▸ {who}  平台 {spec.host}  模型 {spec.model}  key …{spec.api_key[-6:]}")
        client = routing.build_client(spec, label=key)
        started = time.time()
        try:
            reply = await client.chat_json(PROBE)
            elapsed = time.time() - started
            used = client.usage
            print(f"   ✅ 通了（{elapsed:.1f}s，输入 {used['prompt_tokens']} / "
                  f"输出 {used['completion_tokens']} tokens）")
            print(f"   它回的是：{str(reply)[:120]}")
        except LLMError as exc:
            failures += 1
            text = str(exc)
            print(f"   ❌ 失败：{text[:300]}")
            if "401" in text or "invalid_api_key" in text.lower():
                print("      401 = key 不对，或者 key 跟平台的地区不匹配")
            elif "404" in text:
                print("      404 = base_url 或模型名不对（注意 base_url 要带 /v1）")
            elif "429" in text:
                print("      429 = 撞限流了，等一会儿再试，或者换一条 key")
            elif "timeout" in text.lower():
                print("      超时：检查网络，或者把 config.yaml 的 llm.default.timeout 调大")
        except Exception as exc:                     # noqa: BLE001
            failures += 1
            print(f"   ❌ 异常：{type(exc).__name__}: {exc}")
        finally:
            await client.aclose()

    print("\n" + "=" * 78)
    if failures:
        print(f"❌ 有 {failures} 组配置没通。改完 .env 再跑一次这个脚本即可（不用重启服务）。")
        return 1
    print("✅ 所有平台都通了，可以开真实对局了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
