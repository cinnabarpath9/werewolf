# coding=utf-8
"""变异测试：把新规则**故意改坏**，看 t_rules_new.py 会不会红。

项目的老教训（HANDOFF §8 第 10 条）：改了检查工具/测试之后必须做变异测试，
否则很可能写出一堆"永远不会失败"的假断言。这里用 monkeypatch 在进程内改坏 4 个地方，
每个都要求指定的几条断言**必须变红**；要是它还绿，说明那条断言是废的。

跑法： python t_mutation_new.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import wolf.engine as E                                   # noqa: E402
from wolf.roles import ROLES                              # noqa: E402
import t_rules_new as T                                   # noqa: E402

OUT: list[tuple[str, bool, str]] = []


def verdict(name: str, ok: bool, detail: str = "") -> None:
    OUT.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


async def run_cases(names: list[str]) -> list[str]:
    """跑指定的几个场景，返回失败的断言名。

    ⚠️ 场景本身也可能**抛异常**（比如某条断言用 `.index()` 找一句不存在的话）：
    那也是"被抓到"的一种，所以这里兜住异常并记成一条 `崩溃:` 条目，不能让整个变异跑挂掉。
    """
    T.RESULTS.clear()
    for n in names:
        try:
            await getattr(T, n)()
        except Exception as exc:                              # noqa: BLE001
            T.RESULTS.append((f"崩溃:{n}", False, f"{type(exc).__name__}: {exc}"))
    return [r[0] for r in T.RESULTS if not r[1]]


async def main() -> int:
    print("=" * 78)
    print("变异 1：让「白痴算民也算神」失效（also_kind 抹掉）")
    print("=" * 78)
    real_idiot = ROLES["idiot"]
    ROLES["idiot"] = dataclasses.replace(real_idiot, also_kind=None)
    try:
        fails = await run_cases(["t_eleven_standard_board", "t_dual_kind_side_win"])
    finally:
        ROLES["idiot"] = real_idiot
    verdict("变异 1 被抓到（屠神/屠民必须连白痴一起杀 的断言变红）",
            any("13c" in f or "14a" in f or "14c" in f or "14e" in f or "14f" in f for f in fails),
            f"变红的断言={fails}")

    print()
    print("=" * 78)
    print("变异 2：竞选平票不再 PK（砍掉 PK 发言那一轮）")
    print("=" * 78)
    real_pk = E.Game._election_pk_speeches

    async def no_pk(self, tied):                              # noqa: ANN001
        return None

    E.Game._election_pk_speeches = no_pk                      # type: ignore[assignment]
    try:
        fails = await run_cases(["t_election_pk"])
    finally:
        E.Game._election_pk_speeches = real_pk                # type: ignore[assignment]
    verdict("变异 2 被抓到（'平票者各发一轮【竞选PK】发言' 的断言变红）",
            any("18b" in f for f in fails), f"变红的断言={fails}")

    print()
    print("=" * 78)
    print("变异 3：警徽不算 1.5 票（vote_weight 恒为 1）")
    print("=" * 78)
    real_w = E.Game.vote_weight
    E.Game.vote_weight = lambda self, seat_id: 1.0            # type: ignore[assignment]
    try:
        fails = await run_cases(["t_badge_vote_and_last_words", "t_pure"])
    finally:
        E.Game.vote_weight = real_w                           # type: ignore[assignment]
    verdict("变异 3 被抓到（'9号 得 4.5 票' 与纯函数断言变红）",
            sum(1 for f in fails if f.startswith(("5b", "5c", "12a", "12d", "12e"))) >= 3,
            f"变红的断言={fails}")

    print()
    print("=" * 78)
    print("变异 4：谁的遗言都给（把 last_words 变成不写台账的空操作 / 再变成乱给）")
    print("=" * 78)
    real_lw = E.Game.last_words

    async def silent_lw(self, seat):                          # noqa: ANN001
        return None

    E.Game.last_words = silent_lw                             # type: ignore[assignment]
    try:
        fails = await run_cases(["t_night_death_last_words", "t_badge_vote_and_last_words",
                                 "t_daytime_hunter", "t_night_hunter", "t_day1_order"])
    finally:
        E.Game.last_words = real_lw                           # type: ignore[assignment]
    verdict("变异 4 被抓到（'有遗言/遗言进台账/遗言排在竞选前' 的断言变红）",
            sum(1 for f in fails if f.startswith(("6b", "5d", "5e", "7a", "7b", "8a", "8e", "17b", "崩溃")))
            >= 4,
            f"变红的断言={fails}")

    print()
    print("=" * 78)
    print("对照组：不改坏东西时，同一批场景必须全绿")
    print("=" * 78)
    fails = await run_cases(["t_eleven_standard_board", "t_dual_kind_side_win", "t_election_pk",
                             "t_badge_vote_and_last_words", "t_pure", "t_night_death_last_words",
                             "t_daytime_hunter", "t_night_hunter", "t_day1_order"])
    verdict("对照：原样跑这批场景 0 失败", not fails, f"失败={fails}")

    bad = [x for x in OUT if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(OUT)} 项：通过 {len(OUT) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
