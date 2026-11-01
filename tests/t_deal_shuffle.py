# coding=utf-8
"""回归：**每局重新洗牌**（0 = 随机发牌）必须真的成立。

这是一条实测出来的真 bug（不是理论风险）：
  · config.yaml 写「seed: 0 = 每次随机发牌（每局身份都会重排）」
  · 手机开局固定发 seed=0
  · 而引擎那层的语义是"显式给的 0 就是固定种子"（`seed if seed is not None else None`）
  → 从手机开的每一局**发牌完全相同**（实测三局都是 vvwwwwiswvh）。
边界上把 0 归一成 None 之后，这里钉死行为：
  · seed=0（或不传）→ 多次开局发牌必须不同
  · seed=7 → 两次开局发牌必须**相同**（可复现）
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from check_api import load_env                           # noqa: E402

load_env(os.path.join(ROOT, ".env"))
import server as srv                                     # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
# ⚠️ 存档目录**必须**改到临时目录（2026-10-08 踩到，代价是用户的一局真局）：
#    `Room.start_safe` 是真会落盘一局的 —— 拿生产 config 建 Room，就等于每跑一次回归
#    往用户的 `games/` 里塞 3 局 mock（本文件 3 次开局）。这些 mock 局会把**真局挤出**
#    `game.keep_games: 100` 的保留窗口，而 `prune_saves()` 是"从最旧的开始删"。
#    实测：某天真局存档因此消失，`games/` 里剩下 172 个 mock/测试局。
#    `keep_games: 0` = 这个临时目录不做清理（跑完由 run_all 的收尾把 tests/tmp 删掉）。
_TMP_GAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "deal_shuffle_games")
CFG.setdefault("game", {}).update({"save_dir": _TMP_GAMES, "keep_games": 0, "save_budget_mb": 0})


async def deals(seed: int, times: int) -> list[str]:
    out = []
    for _ in range(times):
        room = srv.Room(CFG, mock_default=True)
        room.broadcast = lambda *a, **k: None
        res = await room.start_safe(True, 6, seed, "eleven_wolfking")
        if not res.get("ok"):
            out.append(f"失败:{res.get('error')}")
            continue
        out.append("".join(s.role_key[0] for s in room.game.seats))
        await room.abandon()
    return out


async def main() -> int:
    zero = await deals(0, 3)
    check("1a seed=0 开局三次，发牌**不完全相同**（每局重洗）",
          len(set(zero)) > 1, str(zero))
    check("1b 三次里没有出现 start 失败", not any(x.startswith("失败") for x in zero), str(zero))

    seven = await deals(7, 2)
    check("2  seed=7 两次发牌相同（显式种子要能复现）",
          len(set(seven)) == 1 and not seven[0].startswith("失败"), str(seven))

    # 引擎那层保留"0 是合法固定种子"（测试/复现用），别被顺手改掉
    import random                                          # noqa: E402

    from wolf.engine import Game                           # noqa: E402
    from wolf.retired_boards import retired_board          # 这张 11 人板已退役，测试用它的副本

    def engine_deal(seed):
        g = Game(board=retired_board("eleven_standard"), agent_factory=None, seed=seed, max_days=1)
        return "".join(s.role_key[0] for s in g.seats)

    check("3  引擎层：显式 seed=0 仍是固定种子（给测试用）",
          engine_deal(0) == engine_deal(0), engine_deal(0))
    check("4  引擎层：seed=None 才随机", engine_deal(None) != engine_deal(None)
          or engine_deal(None) != engine_deal(None), "两次不同即可")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
