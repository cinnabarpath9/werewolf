# coding=utf-8
"""对局结果统计的闸门（2026-10-31 加）。

为什么单测它：这条链路**只该带摘要出去**——一旦有人把 `transcript` 塞进摘要，
就等于把整局发言和思考链传给了别人。所以这里逐字段验：
  ① 摘要里没有发言/思考/名字；
  ② 同一局多人上报只算一局；
  ③ 超过上限的条目直接拒；
  ④ 胜率按局算，不按上报条数算。

跑法：python tests/t_reports.py（纯本地，0.1s）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wolf import reports                             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


SAVE = {
    "board": "eleven_wolfking", "day": 4, "winner": "good",
    "seats": [
        {"id": 1, "role": "wolf", "alive": False, "is_human": False},
        {"id": 2, "role": "witch", "alive": True, "is_human": True},
        {"id": 3, "role": "seer", "alive": True, "is_human": False},
    ],
    "transcript": [{"seat": 1, "reasoning": "这是很长的思考链", "reply": {"speech": "这是发言"}}],
}


def main() -> int:
    tmp = os.path.join(HERE, "tmp", "reports_test")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    print("=== 一、摘要只带白名单字段 ===")
    item = reports.summary_from_save(SAVE, "game-20261031-120000.json", "guest")
    blob = json.dumps(item, ensure_ascii=False)
    check("1a 有胜负/天数/座位数/角色表",
          item["winner"] == "good" and item["days"] == 4 and item["seats"] == 3
          and item["roles"]["1"] == "wolf", blob[:80])
    check("1b 胜方存活数算对（好人胜 → 数好人）", item["winner_alive"] == 2, str(item["winner_alive"]))
    check("1c 不带动过场：没有发言/思考/名字",
          "这是发言" not in blob and "思考链" not in blob and "transcript" not in blob, "")
    check("1d 上限之内（< 4KB）", len(blob.encode()) < 4096, str(len(blob.encode())))

    print("\n=== 二、去重：同一局多人上报只记一条 ===")
    ok1, _ = reports.append(tmp, item)
    again = dict(item, reporter="guest")           # 同 game_id + reporter
    ok2, why = reports.append(tmp, again)
    host = dict(item, reporter="host")             # 不同 reporter
    ok3, _ = reports.append(tmp, host)
    check("2a 第一条写入成功", ok1)
    check("2b 同一 (局, 上报者) 第二条被拒", ok2 is False and "收过" in why, why)
    check("2c 换一个上报者可以再记（同局不同人）", ok3 is True)

    print("\n=== 三、胜率按「局」算，不按上报条数算 ===")
    other = reports.summary_from_save(dict(SAVE, winner="wolf", day=2), "game-20261031-130000.json", "host")
    reports.append(tmp, other)
    stats = reports.aggregate(reports.load(tmp))
    check("3a 两个不同局 → 2 局（不是 3 条）", stats["games"] == 2, str(stats))
    check("3b 好人 1 局、狼 1 局 → 各 50%",
          stats["wolf"] == 1 and stats["good"] == 1 and stats["good_rate"] == 0.5, str(stats))
    check("3c 平均天数 (4+2)/2 = 3.0", stats["avg_days"] == 3.0, str(stats["avg_days"]))
    check("3d 按板子分组有 eleven_wolfking 一栏", "eleven_wolfking" in stats["by_board"], "")

    print("\n=== 四、坏数据不会写进去 ===")
    check("4a 缺 game_id 拒收", reports.append(tmp, {"winner": "good"})[0] is False)
    check("4b 缺 winner 拒收", reports.append(tmp, {"game_id": "x"})[0] is False)
    huge = dict(item, game_id="game-huge.json", pad="x" * 5000)
    check("4c 超过 4KB 拒收", reports.append(tmp, huge)[0] is False)
    shutil.rmtree(tmp, ignore_errors=True)

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
