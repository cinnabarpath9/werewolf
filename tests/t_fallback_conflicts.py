# coding=utf-8
"""回归：这轮自查发现的三条（懒建替补 / Recorder 透出换家 / 存档记录换家）。

用户的问法："代码自查看有没有冲突？" —— 所以这里既验修复，也验"不冲突"：
  · 替补没用到时**一个连接池都不该建**；换过家后 aclose 要把建过的都关掉；
  · 考试工具的 Recorder 必须能把 use_fallback 传下去（否则配了白配）；
  · transcript 与 to_dict 里能看到"这一手是换家打的"。
"""
import asyncio
import inspect
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf import routing                                 # noqa: E402
from wolf.agents import LLMAgent                         # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class FakeClient:
    def __init__(self, model: str, reply=None, fail=False):
        self.model = model
        self.reply = reply or {"speech": "好。"}
        self.fail = fail
        self.calls = 0
        self.last_call = {}
        self.closed = False

    def extra_for(self, task_kind, day):
        return {}

    async def chat_json(self, packet, extra=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.model} 挂了")
        self.last_call = {"model": self.model, "reasoning": "替补的思路在这里。",
                          "prompt_tokens": 1, "completion_tokens": 1, "raw": "{}"}
        return dict(self.reply)

    async def aclose(self):
        self.closed = True


async def main() -> int:
    import run_game
    run_game.load_env(os.path.join(ROOT, ".env"))
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    game_cfg = cfg.get("game") or {}
    board = make_board("eleven_standard")

    print("=== 一、P1：替补是懒建的（不换家就不建连接池）===")
    built = []

    def maker(tag):
        def _make():
            built.append(tag)
            return FakeClient(f"{tag}-model")
        return _make

    primary = FakeClient("primary", reply={"speech": "好的。"})
    agent = LLMAgent(primary, 1, fallbacks=[("A", maker("A")), ("B", maker("B"))])
    check("1a 建代理时一个替补都没建", not built, f"建了 {built}")
    name = agent.use_fallback()
    check("1b 换家时才建第一家", built == ["A"] and name == "A", f"{built} / {name}")
    name2 = agent.use_fallback()
    check("1c 再换才建第二家", built == ["A", "B"] and name2 == "B", f"{built} / {name2}")

    print("\n=== 二、P1b：aclose 把建过的替补都关掉（不留悬挂连接池）===")
    agent2 = LLMAgent(FakeClient("p"), 1, fallbacks=[("A", maker("A2")), ("B", maker("B2"))])
    agent2.use_fallback()
    agent2.use_fallback()
    await agent2.aclose()
    check("2a 当前客户端已关", agent2.client.closed, str(agent2.client.closed))
    check("2b 没在用的替补也已关",
          all(getattr(c, "closed", False) for c in agent2._spare if c is not agent2.client),
          str([getattr(c, "closed", None) for c in agent2._spare]))

    print("\n=== 三、生产接线：_build_fallbacks 返回的是函数，不是客户端 ===")
    import server                                        # noqa: PLC0415
    g = Game(board=board, agent_factory=None, seed=1, max_days=1)
    seat = g.seats[2]
    items = server._build_fallbacks(cfg, seat, game_cfg.get("fallback"))
    check("3a 名单非空", bool(items), str([n for n, _ in items]))
    check("3b 每一项都是可调用的出厂函数（不是已建好的客户端）",
          all(callable(m) and not hasattr(m, "chat_json") for _, m in items),
          str([(n, type(m).__name__) for n, m in items]))
    made = [m() for _, m in items]
    check("3c 出厂函数能造出真客户端（有 base_url/model）",
          all(getattr(c, "base_url", "") and getattr(c, "model", "") for c in made),
          str([c.model for c in made]))
    for c in made:
        await c.aclose()

    print("\n=== 四、P2：考试工具的 Recorder 能透出换家能力 ===")
    import importlib.util                                # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("exam_live_probe",
                                                 os.path.join(ROOT, "tools", "exam_live.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    inner = LLMAgent(FakeClient("p"), 1, fallbacks=[("A", maker("A3"))])
    rec = mod.Recorder(inner, 1, [], mine=True)
    check("4a Recorder 上有 use_fallback", callable(getattr(rec, "use_fallback", None)))
    check("4b 调它能真的换到替补（说明引擎拿得到）", rec.use_fallback() == "A")
    check("4c Recorder 也透出 model_now", bool(getattr(rec, "model_now", "")))

    print("\n=== 五、P3：存档与规则块里能看出「换家/重试」口径 ===")
    src = io.open(os.path.join(ROOT, "wolf", "engine.py"), encoding="utf-8").read()
    check("5a transcript 记录里带 fallback 字段", '"fallback":' in src)
    rules_block = src[src.index('"rules": {'):src.index('"sheriff_id"')]
    for key in ("auto_retries", "retry_backoff", "carry_reasoning", "call_timeout"):
        check(f"5b to_dict().rules 记了 {key}", key in rules_block)

    print("\n=== 六、端到端：换家那一手在 transcript 里留下痕迹 ===")
    primary3 = FakeClient("glm-x", fail=True)
    backup3 = FakeClient("ds-y", reply={"speech": "我投 2 号。", "vote": 2})
    agent3 = LLMAgent(primary3, 1, fallbacks=[("DEEPSEEK", lambda: backup3)])
    game3 = Game(board=board, agent_factory=lambda s: agent3, seed=1, max_days=1,
                 call_timeout=5, decision_timeout=30, auto_retries=2,
                 retry_backoff=[0.05], retry_jitter=0.0)
    game3.on_event = lambda kind, **data: None
    game3.on_stall = None
    await game3.act(game3.seats[0], Task(kind="speech", text="说", schema="{}", legal=[]))
    rows = [r for r in game3.transcript if r.get("fallback")]
    check("6a 换家那一次调用记了 fallback=DEEPSEEK",
          rows and rows[-1]["fallback"] == "DEEPSEEK", str([r.get("fallback") for r in game3.transcript]))
    check("6b 也记下了实际回答的模型名", rows and rows[-1]["model"] == "ds-y",
          str([r.get("model") for r in game3.transcript]))
    dump = game3.to_dict()
    check("6c 存档规则块里有 auto_retries", "auto_retries" in (dump.get("rules") or {}),
          str((dump.get("rules") or {}).get("auto_retries")))

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n_, _, d in bad:
        print(f"  [FAIL] {n_}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
