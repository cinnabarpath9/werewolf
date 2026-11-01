# coding=utf-8
"""查一件事：手机上开局固定发 seed=0，而引擎把 0 当"固定种子" —— 会不会每局发牌都一样？

如果一样，那就是个真 bug（README 承诺"每局重新洗牌"）。三种口径都试：
  · 引擎直接 Game(seed=0) ×2
  · 引擎 Game(seed=None) ×2
  · 服务器 Room.start(mock=True) ×2（手机走的就是这条，seed 恒为 0）
"""
import asyncio
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from check_api import load_env                           # noqa: E402

load_env(os.path.join(ROOT, ".env"))
from wolf.agents import MockAgent                         # noqa: E402
from wolf.engine import Game                              # noqa: E402
from wolf.roles import make_board                         # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）


def deal_of(seed):
    g = Game(board=make_board("eleven_standard"),
             agent_factory=lambda s: MockAgent(s, random.Random(seed)), seed=seed, max_days=1)
    return "".join(s.role_key[0] for s in g.seats)


print("=== 引擎口径 ===")
a, b = deal_of(0), deal_of(0)
print(f"  seed=0  两次发牌：{a} / {b} → {'相同（0 被当固定种子）' if a == b else '不同'}")
c, d = deal_of(None), deal_of(None)
print(f"  seed=None 两次发牌：{c} / {d} → {'相同' if c == d else '不同（每局重洗）'}")


async def via_room(times: int = 2) -> list[str]:
    import server as srv
    cfg = srv.load_config(os.path.join(ROOT, "config.yaml")) if hasattr(srv, "load_config") else None
    import yaml
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    # ⚠️ 同上（见 t_deal_shuffle.py 里那段说明）：`start_safe` 会真落盘，
    #    用生产 config 就是往用户的 `games/` 里写测试局 → 把真局挤出保留窗口。
    cfg.setdefault("game", {}).update({
        "save_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "deal_seed_games"),
        "keep_games": 0, "save_budget_mb": 0})
    out = []
    for _ in range(times):
        room = srv.Room(cfg, mock_default=True)
        room.broadcast = lambda *a, **k: None
        res = await room.start_safe(True, 6, 0, "eleven_wolfking")
        if not res.get("ok"):
            out.append(f"start 失败：{res.get('error')}")
            continue
        out.append("".join(s.role_key[0] for s in room.game.seats))
        room.running = False
        await room.abandon()
    return out


print("\n=== 服务器口径（手机走这条，seed 恒 0）===")
runs = asyncio.run(via_room(3))
for i, r in enumerate(runs, 1):
    print(f"  第 {i} 局：{r}")
print("  判定：", "⚠️ 三局发牌完全相同 —— 每局重洗没生效" if len(set(runs)) == 1 else "✅ 每局不同")

# ⚠️ 2026-10-09 补：以前只 print 一句"⚠️/✅"，没有退出码（假回归门）。
_ok = (len(set(runs)) > 1) and not any(r.startswith("start 失败") for r in runs)
print()
print("判定：", "✅ 每局重新洗牌" if _ok else "❌ 发牌没重洗（或开局就失败了）")
raise SystemExit(0 if _ok else 1)
