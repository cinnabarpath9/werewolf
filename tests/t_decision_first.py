# coding=utf-8
"""回归：白天「决策+发言合并成一次调用」 + 投票前那次权威决策 + 夜里不泄露谁在行动。

用 mock 跑一局（花钱为零）。断言的都是**行为**，不盯字面写法：
  1) 每条轮次发言的正文就是「发言前那次决策调用」产出的 —— 不再多花一次 speech 调用；
  2) 投票前那次 analyze 落在该天所有发言**之后**（听完全场才定票），投票复用它；
  3) 提示词里**没有**「发言必须与投票一致 / 不要说要投别人 / 别显得还在犹豫」这类硬约束
     （用户 2026-10-07 明确：狼可以说一套投一套，好人听到新消息也该能改）；
  4) 台账里投票条目带中性的 `lean`（发言时的倾向），不做评判、不要求解释；
  5) 关掉 decision_first 就回到老流程（先发言、后投票）；
  6) 每天调用次数：合并 vs 拆回「两次调用」，用同一套 mock 实测对比并打印出来；
  7) 夜里事件不带座位号（thinking / warn / retry）。
"""
import asyncio
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
sys.path.insert(0, WW)
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(WW)

from wolf.agents import MockAgent                          # noqa: E402
from wolf.engine import Game                               # noqa: E402
from wolf.roles import make_board                          # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）
from _boards import NINE_BOARD                     # 测试专用 9 人板（产品的 9 人板已删）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class Spy(MockAgent):
    """记下调用顺序、任务原文、以及「决策那次回复里写出的发言正文」。"""

    calls: list[tuple] = []            # (座位, 任务类型, task.text)
    packets: list[tuple] = []          # (座位, 任务类型, 提示词包)
    plan_speech: list[tuple] = []      # (座位, 决策回复里的 speech 正文)

    async def ask(self, packet, task):
        reply = await super().ask(packet, task)
        Spy.calls.append((self.seat.id, task.kind, str(task.text or "")))
        Spy.packets.append((self.seat.id, task.kind, str(packet or "")))
        if task.kind == "speech_plan":
            Spy.plan_speech.append((self.seat.id, str((reply or {}).get("speech") or "")))
        return reply


class SplitGame(Game):
    """把那次合并调用**拆回**「决策 + 单独发言」（= 2026-10-07 修订前的调用次数）。

    只用来量「每天少花了多少次调用」，别在别的地方用 —— 它不是一条产品路径。
    """

    async def decide_for_speech(self, seat, day):
        plan = await Game.decide_for_speech(self, seat, day)
        plan = dict(plan)
        plan.pop("speech", None)       # 没正文 → day_phase 退回「单独问一次发言」
        return plan


async def play(decision_first: bool, cls=Game, board=NINE_BOARD) -> Game:
    import random
    Spy.calls, Spy.packets, Spy.plan_speech = [], [], []
    rng = random.Random(7)
    g = cls(board=make_board(board), human_seat=0, seed=7, max_days=4,
            decision_first=decision_first)
    g.agent_factory = lambda seat: Spy(seat, rng)
    await g.run()
    return g


def day_calls(g: Game) -> dict[int, dict[str, int]]:
    """每天的**白天轮次**调用次数（只算合并省下来的那一类）。

    ⚠️ 只统计「轮到你发言」的 speech：PK / 竞选 / 遗言也走 `kind="speech"`，
    但它们**本来就没有**决策调用（决策看不到它们的场景），不该算进「合并省了多少」。
    夜间调用两种跑法都一样，也不参与对比。
    """
    out: dict[int, dict[str, int]] = {}
    for row in g.transcript:
        kind = str(row.get("task") or "")
        if kind not in ("speech_plan", "speech", "analyze"):
            continue
        if kind == "speech" and "轮到你发言" not in str(row.get("packet") or ""):
            continue
        day = int(row.get("day") or 0)
        out.setdefault(day, {})
        out[day][kind] = out[day].get(kind, 0) + 1
    return out


def _fmt(table: dict[int, dict[str, int]]) -> str:
    return "；".join(f"第 {d} 天 {dict(sorted(v.items()))}"
                    for d, v in sorted(table.items())) or "（没有）"


def _day_of(text: str) -> int:
    import re as _re
    m = _re.search(r"第 (\d+) 天", text or "")
    return int(m.group(1)) if m else 0


async def main() -> int:
    print("=== 一、决策与发言合并成一次调用（投票前再独立决策一次）===")
    g = await play(True)
    round_speeches = [(e.seat, e.text) for e in g.ledger.entries
                      if e.kind == "speech" and (e.data or {}).get("round") == "day"]
    merged = [(sid, txt) for sid, txt in Spy.plan_speech if txt]
    check("1a 每条轮次发言的正文就是「决策+发言」那一次调用产出的（顺序与座位都对得上）",
          bool(round_speeches) and round_speeches == merged,
          f"台账 {len(round_speeches)} 条 / 决策回复带正文 {len(merged)} 条")

    separate = [(sid, t) for sid, k, t in Spy.calls if k == "speech" and "轮到你发言" in t]
    check("1b 模型座位这一局**没有**单独的轮次发言调用（0 次，合并真的省下了）",
          not separate, f"{len(separate)} 次：{separate[:2]}")

    per_day: dict[int, dict] = {}
    for idx, (_sid, kind, text) in enumerate(Spy.calls):
        d = _day_of(text)
        if kind == "speech_plan":
            per_day.setdefault(d, {"plan": [], "an": []})["plan"].append(idx)
        elif kind == "analyze":
            per_day.setdefault(d, {"plan": [], "an": []})["an"].append(idx)
    bad_days = [d for d, v in per_day.items()
                if v["plan"] and v["an"] and min(v["an"]) < max(v["plan"])]
    check("1c 投票前那次决策落在该天所有发言**之后**（听完全场才定票）",
          bool(per_day) and not bad_days,
          f"检查了 {sorted(per_day)} 天，异常：{bad_days}")
    n_analyze = sum(1 for _s, k, _t in Spy.calls if k == "analyze")
    check("1d 每天投票前都有一次 analyze（全员并发）", n_analyze > 0, f"{n_analyze} 次")

    votes = [e for e in g.ledger.entries if e.kind == "vote" and (e.data or {}).get("round") == 1]
    pref = [e for e in votes if (e.data or {}).get("prefetched")]
    check("1e 第一轮投票全部复用**投票前**那次决策（不再多花一次调用）",
          bool(votes) and len(pref) == len(votes), f"{len(pref)}/{len(votes)}")
    # ⚠️ 2026-10-08 起这条同时是**P0 泄漏回归**：复用分支只许写「这一票来自本轮那次决策」，
    #    不许把决策层的私有理由（prompt 里明写「只有你自己看得到」）抄进**公开**投票条目。
    #    ⚠️ 同日 P3-4 又改了措辞：原文案「（本轮的投票决策，见自己的私有记录）」对**别人**
    #    是句空话、投票者**本人**也读不到（私有理由只进 mind 与存档），所以改成中性的
    #    「（本轮已定票）」。判据跟着改成**两种说法都认**，但"不许夹带私有理由"一个字没松。
    private = [str((v or {}).get("reason") or "") for v in (g.day_decisions or {}).values()]
    private = [x for x in private if len(x) >= 6]
    leaked = [(e.seat, x) for e in pref for x in private if x in e.text]
    check("1f 复用决策的票只写来路（不夹带决策层的私有理由）",
          bool(pref) and all(("决策" in e.text or "本轮已定票" in e.text) for e in pref)
          and not leaked,
          f"来路：{pref[0].text if pref else ''}；泄漏：{leaked[:2]}")

    print("\n=== 二、发言不再被要求「与投票一致」（狼可以说一套投一套）===")
    plan_pkts = [p for _s, k, p in Spy.packets if k == "speech_plan"]
    all_pkts = [p for _s, _k, p in Spy.packets]
    check("2a 合并那次调用的提示词同时要决策字段和 speech 正文",
          bool(plan_pkts) and all('"speech"' in p and '"vote"' in p and '"reads"' in p
                                  for p in plan_pkts),
          f"{len(plan_pkts)} 份提示词")
    banned = ("不要说要投别人", "发言必须与这个决定一致", "已经决定今天投",
              "也不要显得你还在犹豫")
    hit = [x for p in all_pkts for x in banned if x in p]
    check("2b 提示词里没有旧口径那几句硬约束（不许再要求「必须与决定一致」）",
          not hit, f"命中的旧口径句子：{sorted(set(hit))}")
    check("2c 写明了 vote 只是**此刻的倾向**、投票时还能再改",
          bool(plan_pkts) and all("倾向" in p and ("可以改" in p or "可以变" in p)
                                  for p in plan_pkts), "")
    wolf_ids = [s.id for s in g.seats if s.camp == "wolf"]
    wolf_pkts = [p for sid, k, p in Spy.packets if k == "speech_plan" and sid in wolf_ids]
    check("2d 狼的那份提示词写明了「心口不一」是战术（不是禁令）",
          bool(wolf_pkts) and all("心口不一" in p for p in wolf_pkts)
          and not any("不许" in p and "心口不一" in p for p in wolf_pkts),
          f"{len(wolf_pkts)} 头狼的提示词")

    lean_votes = [e for e in votes if "lean" in (e.data or {})]
    check("2e 投票条目带上中性的 lean（发言时的倾向，供复盘看「嘴上说谁、实际投谁」）",
          bool(lean_votes) and len(lean_votes) == len(votes),
          f"{len(lean_votes)}/{len(votes)} 条带 lean；例："
          f"{[(e.seat, e.data.get('lean'), e.data.get('target')) for e in votes[:4]]}")
    check("2f 没有要求解释「为什么改口」、也没把不一致标成异常",
          not any(("改口" in p or "改票理由" in p or "changed_from" in p) for p in all_pkts)
          and not any("changed_from" in (e.data or {}) for e in votes), "")
    check("2g 决策本身**不在**公开台账里（只有那个座位自己知道）",
          not any(e.kind in ("analyze", "speech_plan") for e in g.ledger.entries), "")
    check("2h 读牌/判断照旧写进座位记忆（后面几天能用）",
          any(("天读牌" in (s.mind or "")) or ("发言前的判断" in (s.mind or ""))
              for s in g.seats if not s.is_human), "")

    print("\n=== 三、关掉就回到老流程 ===")
    g2 = await play(False)
    kinds2 = [k for _s, k, _t in Spy.calls]
    check("3a 关掉后没有 analyze", "analyze" not in kinds2, str(kinds2[:6]))
    check("3b 关掉后仍然是「单独发言」那条老路（照旧可用）",
          "speech" in kinds2, f"speech×{kinds2.count('speech')}")
    i_sp = next((i for i, k in enumerate(kinds2) if k == "speech"), None)
    i_vo = next((i for i, k in enumerate(kinds2) if k == "vote"), None)
    check("3c 老流程：先发言、后投票",
          i_sp is not None and i_vo is not None and i_sp < i_vo, f"speech@{i_sp} vote@{i_vo}")
    v2 = [e for e in g2.ledger.entries if e.kind == "vote"]
    check("3d 老流程的票没有 prefetched 标记",
          bool(v2) and not any((e.data or {}).get("prefetched") for e in v2), f"{len(v2)} 票")

    print("\n=== 四、每天调用次数：合并前 vs 合并后（mock 实测，不是估算）===")
    g3 = await play(True, cls=SplitGame)
    merged_tbl, split_tbl = day_calls(g), day_calls(g3)
    print(f"  合并后（本次口径）：{_fmt(merged_tbl)}")
    print(f"  拆回两次调用（旧口径）：{_fmt(split_tbl)}")

    def total(tbl: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in tbl.values():
            for k, n in v.items():
                out[k] = out.get(k, 0) + n
        return out

    tm, ts = total(merged_tbl), total(split_tbl)
    print(f"  合计：合并后 {tm} ／ 旧口径 {ts}（只算白天轮次：speech_plan/speech/analyze）")
    check("4a 合并后**一次**单独的轮次发言调用都没有（轮次 speech 计数为 0）",
          tm.get("speech", 0) == 0, f"轮次 speech={tm.get('speech', 0)}")
    check("4b 旧口径里每人发言都要多一次 speech 调用（拆回去确实多花了）",
          ts.get("speech", 0) > 0 and ts.get("speech", 0) == ts.get("speech_plan", 0),
          f"speech={ts.get('speech', 0)} / speech_plan={ts.get('speech_plan', 0)}")
    check("4c 白天总调用次数：合并后更少（省下的正是那些发言调用）",
          sum(tm.values()) < sum(ts.values()) and (sum(ts.values()) - sum(tm.values())) > 0,
          f"{sum(tm.values())} < {sum(ts.values())}，省 {sum(ts.values()) - sum(tm.values())} 次"
          f"（合并后 speech_plan 共 {tm.get('speech_plan', 0)} 次，每人省 1 次）")

    # 用户那套板子（11 人 + 警长）也量一遍：报告里要报的是"他那一局每天几次"
    print("\n  11 人板（user 在玩的那套，含警长竞选）同样量一遍：")
    m11_tbl = day_calls(await play(True, board="eleven_standard"))
    s11_tbl = day_calls(await play(True, cls=SplitGame, board="eleven_standard"))
    print(f"    合并后：{_fmt(m11_tbl)}")
    print(f"    旧口径：{_fmt(s11_tbl)}")
    m11, s11 = total(m11_tbl), total(s11_tbl)
    print(f"    合计：合并后 {m11} ／ 旧口径 {s11}")
    check("4d 11 人板同样每人省掉一次发言调用",
          m11.get("speech", 0) == 0 and s11.get("speech", 0) == s11.get("speech_plan", 0)
          and sum(m11.values()) < sum(s11.values()),
          f"合并后白天 {sum(m11.values())} 次 vs 旧口径 {sum(s11.values())} 次")

    print("\n=== 五、夜里不泄露「谁在行动」（服务端报文）===")
    src = io.open(os.path.join(WW, "server.py"), encoding="utf-8").read()
    check("5a server.py 有 _thinking_msg / _is_night", "_thinking_msg" in src and "_is_night" in src)
    check("5b 夜里的报文 seat 为 None 且带 night 标记",
          '{"type": "thinking", "seat": None, "night": True}' in src)
    # ⚠️ 2026-10-10：`_thinking_msg` 多了一个 `kind` 参数（狼专属任务在**白天**也要脱敏 ——
    #   公示上警名单后点名"X号 正在思考"等于公开"这号是狼"）。断言跟着改，别再比对旧签名。
    check("5c packet 事件走的是 _thinking_msg（并把 task 一起传进去做脱敏判据）",
          'self._thinking_msg(data["seat"], str(data.get("task") or ""))' in src)
    check("5i 白天的狼专属任务也脱敏：SECRET_ASK_KINDS 覆盖 wolf_explode",
          "SECRET_ASK_KINDS" in src and '"wolf_explode"' in src)
    check("5m stall 弹窗也按 SECRET_ASK_KINDS 脱敏（白天狼任务不点名）",
          'secret = night or str(task.kind) in self.SECRET_ASK_KINDS' in src)
    check("5j 脱敏后的标签是「狼队正在思考」（不点名、也不说夜间行动中误导）",
          '狼队正在思考' in src)
    _html = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "web", "index.html"), encoding="utf-8").read()
    check("5k 前端优先用服务端给的标签（m.label）",
          "startThinkingTimer(m.night ? null : m.seat, m.label)" in _html
          and "THINK.label" in _html)
    check("5l 前端台账有上限（长局不再堆几千个节点 → 手机卡死）",
          "FEED_MAX" in _html and "trimFeed()" in _html and "content-visibility" in _html)
    check("5d 失败提示不点名（夜间 + 白天狼专属任务都走同一张脱敏表）",
          'who = "有人" if secret else' in src
          and 'str(data.get("task_kind") or "") in self.SECRET_ASK_KINDS' in src)
    check("5e retry 事件也有转发分支（以前被静默丢掉，前端那条提示是死的）",
          'elif event == "retry":' in src and 'msg["seat"] = None' in src)
    html = io.open(os.path.join(WW, "web/index.html"), encoding="utf-8").read()
    check("5f 前端夜里显示「夜间行动中」而不是座位号",
          "夜间行动中" in html and "m.night ? null : m.seat" in html)
    check("5g 前端有「思考提示」开关（可完全关掉）",
          "btnThink" in html and "SHOW_THINK" in html and "setShowThink" in html)

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
