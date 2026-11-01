# coding=utf-8
"""回归：换家顶上 + 替补思路摘要传到原模型（用户 2026-10-07 定的策略）。

钉死四件事：
  1. 路由：`resolve_profile` 用**目标档案**的 host/key/model，但保留这个座位的温度/上限；
  2. 替补名单跳过自己那家、跳过没配好的档案；
  3. 引擎重试时真的换家（retry 事件里带 switched_to），且第二次用的是新客户端；
  4. 换手之后，该座位的私有经历里出现"换手记录 + 替补的思路摘要"（下一手原模型能看到）。
"""
import asyncio
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import os                                                # noqa: E402

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
    """假客户端：既能当"原模型"（永远失败），也能当"替补"（返回带思考链的回复）。"""
    def __init__(self, model: str, reply: dict | None = None, fail: bool = False):
        self.model = model
        self.reply = reply or {}
        self.fail = fail
        self.calls = 0
        self.last_call: dict = {}
        self.closed = False

    def extra_for(self, task_kind, day):
        return {}

    async def chat_json(self, packet, extra=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.model} 上游挂了")
        self.last_call = {"model": self.model, "reasoning": "我认为 3 号是狼，因为他票型反复。",
                          "prompt_tokens": 100, "completion_tokens": 20, "raw": "{}"}
        return dict(self.reply)

    async def aclose(self):
        self.closed = True


class FakeAgent(LLMAgent):
    """把 LLMAgent 的客户端换成假客户端（不碰网络）。"""
    def __init__(self, primary: FakeClient, fallbacks: list):
        super().__init__(primary, 1, fallbacks=fallbacks)


async def main() -> int:
    # ⚠️ 先把 .env 读进环境：平台档案（GLM_BASE_URL / DEEPSEEK_MODEL …）都在 .env 里，
    #    不读的话 resolve_profile 解析出来是空的，测试会误判成"代码有 bug"。
    import run_game                                      # noqa: PLC0415
    run_game.load_env(os.path.join(ROOT, ".env"))
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    game_cfg = cfg.get("game") or {}
    board = make_board("eleven_standard")
    seat = None

    print("=== 一、路由：换档案但保留这一座的设定 ===")
    g = Game(board=board, agent_factory=None, seed=1, max_days=1)
    seat = g.seats[2]                                  # 3 号（config 里是 GLM）
    mine = routing.resolve(cfg, seat)
    other = routing.resolve_profile(cfg, seat, "DEEPSEEK")
    check("1a 换家后至少有一项变了（host 或 model）——你这边 GLM/DEEPSEEK 共用同一个中转站域名，"
          "所以换的是模型后端而不是域名",
          other.host != mine.host or other.model != mine.model,
          f"{mine.host}/{mine.model} → {other.host}/{other.model}")
    check("1b 温度/上限保留这一座自己的",
          other.temperature == mine.temperature and other.max_tokens == mine.max_tokens,
          f"temp {mine.temperature}→{other.temperature}、max_tokens {mine.max_tokens}→{other.max_tokens}")
    check("1c model 跟着目标档案走", other.model != mine.model and other.model,
          f"{mine.model} → {other.model}")
    check("1d extra_body 不带座位层的平台专用键",
          set(other.extra_body) <= {"enable_thinking", "reasoning_effort", "thinking", "thinking_budget"},
          str(other.extra_body))

    print("\n=== 二、替补名单：跳过自己那家、跳过没配好的 ===")
    specs = routing.fallback_specs(cfg, seat, game_cfg.get("fallback"))
    names = [n for n, _ in specs]
    check("2a 名单非空", bool(specs), str(names))
    # ⚠️ 2026-10-09 修判据：以前这里写死 GLM（那时 3 号座位就是 GLM）。
    #    用户把 glm-5.3-flash 改走火山端点后，3 号的 profile 变成 ARK，写死的判据就假红了。
    #    本意是"跳过**这个座位正在用的那家**"，所以从配置里现推，别再写死。
    _own = str(((cfg.get("llm", {}).get("seats") or {}).get(seat.id) or {}).get("profile")
               or (cfg.get("llm", {}).get("default") or {}).get("profile") or "")
    check(f"2b 不含这个座位自己用的那家（{_own}）", _own.upper() not in [n.upper() for n in names],
          f"own={_own} names={names}")
    check("2c 每家都有 host/key/model",
          all(s.base_url and s.api_key and s.model for _, s in specs),
          str([(n, s.host, s.model) for n, s in specs]))

    print("\n=== 三、引擎重试时真的换家，并把替补思路留给原模型 ===")
    primary = FakeClient("glm-5.3-flash", fail=True)
    backup = FakeClient("deepseek-v4.1-flash",
                        reply={"speech": "我投 3 号。", "vote": 3, "mind": "3 号票型可疑"})
    agent = FakeAgent(primary, [("DEEPSEEK", backup)])
    game = Game(board=board, agent_factory=lambda s: agent, seed=1, max_days=1,
                call_timeout=5, decision_timeout=30, auto_retries=2,
                retry_backoff=[0.05, 0.05], retry_jitter=0.0,
                carry_reasoning=True, carry_reasoning_chars=50)
    events: list = []
    game.on_event = lambda kind, **data: events.append((kind, data))
    game.on_stall = None
    reply = await game.act(game.seats[2], Task(kind="speech", text="说", schema="{}", legal=[]))
    retries = [d for k, d in events if k == "retry"]
    check("3a 原模型被调用过（确实失败了一次）", primary.calls >= 1, f"{primary.calls} 次")
    check("3b 换了家：第二次用的是替补客户端", backup.calls >= 1, f"替补 {backup.calls} 次")
    check("3c retry 事件里写了换到哪家",
          retries and retries[0].get("switched_to") == "DEEPSEEK",
          str(retries[:1])[:140])
    check("3d 最终拿到替补的回复", reply.get("speech") == "我投 3 号。", str(reply)[:60])
    notes = [x for x in game.seats[2].private_events if "换手记录" in x]
    check("3e 私有经历里有换手记录", bool(notes), str(notes)[:120])
    check("3f 记录里带上了替补的**思路摘要**",
          notes and "3 号是狼" in notes[0], str(notes)[:160] if notes else "")
    check("3g 记录里写明了原来是哪家、换成了哪家",
          notes and "glm-5.3-flash" in notes[0] and "DEEPSEEK" in notes[0],
          str(notes)[:160] if notes else "")
    check("3h 老客户端被关掉（不留悬挂连接）", primary.closed, str(primary.closed))

    print("\n=== 四、关掉 carry_reasoning 时只留「谁代打的」 ===")
    primary2 = FakeClient("glm-5.3-flash", fail=True)
    backup2 = FakeClient("deepseek-v4.1-flash", reply={"speech": "过。"})
    agent2 = FakeAgent(primary2, [("DEEPSEEK", backup2)])
    game2 = Game(board=board, agent_factory=lambda s: agent2, seed=1, max_days=1,
                 call_timeout=5, decision_timeout=30, auto_retries=2,
                 retry_backoff=[0.05], retry_jitter=0.0, carry_reasoning=False)
    game2.on_event = lambda kind, **data: None
    game2.on_stall = None
    await game2.act(game2.seats[2], Task(kind="speech", text="说", schema="{}", legal=[]))
    notes2 = [x for x in game2.seats[2].private_events if "换手记录" in x]
    check("4a 仍然记录换手", bool(notes2), str(notes2)[:100])
    check("4b 但不含思路摘要", notes2 and "3 号是狼" not in notes2[0], str(notes2)[:120])

    print("\n=== 五、没有替补可用时照旧（只重试同一家）===")
    primary3 = FakeClient("glm-5.3-flash", fail=True)
    agent3 = FakeAgent(primary3, [])
    game3 = Game(board=board, agent_factory=lambda s: agent3, seed=1, max_days=1,
                 call_timeout=5, decision_timeout=30, auto_retries=1,
                 retry_backoff=[0.05], retry_jitter=0.0)
    game3.on_event = lambda kind, **data: None
    game3.on_stall = None
    await game3.act(game3.seats[2], Task(kind="speech", text="说", schema="{}", legal=[]))
    check("5a 没替补时不换家（use_fallback 返回 None）", agent3.use_fallback() is None)
    check("5b 也没写换手记录",
          not [x for x in game3.seats[2].private_events if "换手记录" in x])

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n_, _, d in bad:
        print(f"  [FAIL] {n_}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
