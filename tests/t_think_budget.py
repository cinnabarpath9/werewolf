# coding=utf-8
"""回归：**不许砍在思考中途**（用户口径：单次最多少让它想 420 秒 —— 2026-10-10 晚从 300 提到 420）。

三条要钉死：
  1. 单次上限 = config 的 call_timeout（现在是 420 秒），不是 150；
  2. 重试**只拿剩余预算** —— 第一手花掉多少，后面就少多少，一整手总时长不超过 decision_timeout；
  3. 预算用完时不发新请求（不会"砍了又从头来一遍"）。
"""
import asyncio
import sys
import time

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import io                                                # noqa: E402
import os                                                # noqa: E402

import yaml                                              # noqa: E402

from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class HangingModel:
    """上游挂住：永远不返回。"""
    uses_model = True
    calls = 0

    async def ask(self, packet, task):
        type(self).calls += 1
        await asyncio.sleep(3600)
        return {}

    def last_meta(self):
        return {"model": "hang"}


class FailFastThenSlow:
    """第一次马上失败（模拟 500），第二次挂住 —— 用来验"重试只拿剩余预算"。"""
    uses_model = True

    def __init__(self):
        self.n = 0

    async def ask(self, packet, task):
        self.n += 1
        if self.n == 1:
            raise RuntimeError("上游 500")
        await asyncio.sleep(3600)
        return {}

    def last_meta(self):
        return {"model": "flaky"}


async def act_once(agent, call_timeout: float, decision_timeout: float) -> tuple[float, dict, int]:
    game = Game(board=make_board("eleven_standard"), agent_factory=lambda seat: agent,
                seed=2, max_days=1, call_timeout=call_timeout,
                decision_timeout=decision_timeout)
    game.on_event = lambda *a, **k: None
    stalls: list = []

    async def on_stall(seat, task, err, n):
        stalls.append(err)
        return "retry" if len(stalls) < 3 else "skip"

    game.on_stall = on_stall
    task = Task(kind="speech", text="说句话", schema="{}", legal=[])
    started = time.time()
    reply = await game.act(game.seats[0], task)
    return time.time() - started, reply, agent.n if hasattr(agent, "n") else HangingModel.calls, game.transcript


async def main() -> int:
    print("=== 一、config 里的口径（用户要求：够宽、别砍在思考中途）===")
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    game_cfg = cfg.get("game") or {}
    # ⚠️ 2026-10-10：口径曾从 300 提到 420（豆包实测 244 秒贴墙、千问顶穿 300 → 换家）。
    # ⚠️ 2026-11-03（作者拍板）：单次上限**改回 150 秒** —— 420 太宽，用户看到的是
    #    「等了七八分钟没动静」；同时新增"剩余预算不够再完整试一次就不再硬试"的口径。
    #    这里守的仍是**不变式**：① 单次上限不能小到砍在思考中途（≥120）；
    #    ② 整手预算要明显大于单次上限（挂一次还有余额换家）—— 不写死具体秒数。
    _call = float(game_cfg.get("call_timeout", 0))
    check("1a call_timeout 够宽（≥120，别砍在思考中途）", _call >= 120, str(_call))
    # ⚠️ 2026-10-07 改口径：整手预算必须**大于**单次上限，否则一次挂住就把预算吃光，
    #    重试与"换一家顶上"根本不会发生（9号 连挂 4 次都没换模型就是这么来的）。
    #    这里断言**不变式**（差值够跑一次替补），不再写死 300 —— 写死的话改口径就假红。
    _call = float(game_cfg.get("call_timeout", 0))
    _dec = float(game_cfg.get("decision_timeout", 0))
    check("1b decision_timeout > call_timeout（挂一次也还有余额换家）",
          _dec > _call and _dec - _call >= 60, f"call={_call} decision={_dec} 余量={_dec - _call}")

    print("\n=== 二、单次上限真的按配置走（用 1 秒做实验，别真等 300 秒）===")
    HangingModel.calls = 0
    dt, reply, n, tr = await act_once(HangingModel(), call_timeout=1.0, decision_timeout=1.2)
    check("2a 挂住的调用被砍在 1 秒附近", 0.9 <= dt <= 1.6, f"实际 {dt:.2f}s")
    check("2b 失败原因写明是「按上限判失败」",
          "上限判失败" in str(reply.get("_error")) or "也等完了" in str(reply.get("_error")),
          str(reply.get("_error"))[:70])
    check("2c 一整手不超过总预算（1.2 秒 + 一点余量）", dt <= 1.9, f"实际 {dt:.2f}s")
    tries = [float(r.get("elapsed") or 0) for r in tr]
    check("2d 第一次就拿满了上限（没被提前砍）", tries and tries[0] >= 0.9,
          f"每次尝试耗时 {['%.2f' % t for t in tries]}")
    check("2e 后续尝试只能拿剩余时间（越来越短）",
          len(tries) <= 1 or all(b < a + 1e-6 for a, b in zip(tries, tries[1:])),
          f"{['%.2f' % t for t in tries]}")

    print("\n=== 三、第一次秒失败时，重试只拿剩余预算 ===")
    agent = FailFastThenSlow()
    dt2, reply2, _, tr2 = await act_once(agent, call_timeout=1.0, decision_timeout=1.4)
    check("3a 秒失败之后确实重试了（发了 ≥2 次请求）", agent.n >= 2, f"发了 {agent.n} 次")
    tries2 = [float(r.get("elapsed") or 0) for r in tr2]
    check("3d 重试的总时长也没超过整手预算", sum(tries2) <= 1.4 + 0.35,
          f"各次 {['%.2f' % t for t in tries2]}，合计 {sum(tries2):.2f}s / 预算 1.4s")
    check("3b 总时长仍不超过总预算", dt2 <= 2.0, f"实际 {dt2:.2f}s")
    check("3c 第二次拿的是剩余时间（不会又给满 1 秒 → 总时长会超）",
          dt2 <= 1.4 + 0.5, f"{dt2:.2f}s vs 预算 1.4s")

    print("\n=== 四、真人座位完全不受这套上限影响 ===")
    class Human:
        uses_model = False

        async def ask(self, packet, task):
            await asyncio.sleep(1.2)
            return {"speech": "我想好了。"}

        def last_meta(self):
            return {}

    game = Game(board=make_board("eleven_standard"), agent_factory=lambda seat: Human(),
                seed=2, max_days=1, call_timeout=0.5, decision_timeout=0.5)
    game.on_event = lambda *a, **k: None
    t0 = time.time()
    got = await game.act(game.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    dt3 = time.time() - t0
    check("4a 真人想满 1.2 秒都没被砍", dt3 >= 1.1 and not got.get("_error"), f"{dt3:.2f}s")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n_, _, d in bad:
        print(f"  [FAIL] {n_}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
