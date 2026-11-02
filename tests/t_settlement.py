# coding=utf-8
"""对局结算页的数据（2026-11-03，作者要求）。

结算页只给三样：**场上座位、身份分布、哪方胜利**；发言/票型去复盘看。
服务端只在 `end` 报文里带 `settlement`（中途给出去就是作弊面）。

跑法：python tests/t_settlement.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                                  # noqa: E402
from wolf.roles import make_board                             # noqa: E402

import server                                                 # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


g = Game(board=make_board("six_all_reveal"), seed=1, max_days=1,
         agent_factory=lambda seat: None, on_event=lambda *a, **k: None)
for sid, role in {1: "wolf", 2: "wolf", 3: "seer", 4: "witch", 5: "villager", 6: "villager"}.items():
    g.seat_by_id(sid).role_key = role
g.seat_by_id(2).alive = False
g.seat_by_id(2).death_cause = "vote"
g.winner = "good"
g.day = 3

p = server.settlement_payload(g)

print("=== 一、座位与身份 ===")
check("1a 每个座位都有一行", len(p["seats"]) == 6, str(len(p["seats"])))
check("1b 身份名是中文", {r["role"] for r in p["seats"]} == {"狼人", "预言家", "女巫", "村民"},
      str({r["role"] for r in p["seats"]}))
check("1c 阵营/类别都在（结算页配色与分布要用）",
      all(r["camp"] in ("狼人", "好人") and r["kind"] in ("wolf", "god", "villager") for r in p["seats"]))
check("1d 出局座位带死因、存活座位为空",
      [r for r in p["seats"] if r["seat"] == 2][0]["death_cause"] == "vote"
      and [r for r in p["seats"] if r["seat"] == 1][0]["death_cause"] == "")

print("\n=== 二、胜负与分布 ===")
check("2a 胜负在载荷里", p["winner"] == "good", p["winner"])
check("2b 天数在载荷里", p["day"] == 3, str(p["day"]))
check("2c 板子在载荷里", p["board"] == "six_all_reveal", p["board"])
check("2d 分布计数：狼 2 / 神 2 / 民 2",
      p["counts"] == {"wolf": 2, "god": 2, "villager": 2}, str(p["counts"]))

print("\n=== 三、11 人板的分布 ===")
g2 = Game(board=make_board("eleven_wolfking"), seed=1, max_days=1,
          agent_factory=lambda seat: None, on_event=lambda *a, **k: None)
p2 = server.settlement_payload(g2)
check("3a 11 人板：4 狼 / 4 神 / 3 民",
      p2["counts"] == {"wolf": 4, "god": 4, "villager": 3}, str(p2["counts"]))
check("3b 狼王身份名出现在结算里", any(r["role"] == "狼王" for r in p2["seats"]))

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)