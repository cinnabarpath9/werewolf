# coding=utf-8
"""验：新加的两块（发言顺序 + 身份校验/身份高低）真的进了包与决策提示词。"""
import asyncio
import io
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
sys.path.insert(0, WW)
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(WW)

from wolf.agents import MockAgent                            # noqa: E402
from wolf.engine import Game                                 # noqa: E402
from wolf.roles import make_board                            # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


async def main() -> int:
    rng = random.Random(3)
    g = Game(board=make_board("eleven_standard"), human_seat=0, seed=3, max_days=2,
             decision_first=True, sheriff=True)
    captured: list[str] = []

    class Cap(MockAgent):
        async def ask(self, packet, task):
            # ⚠️ 2026-10-07 起「发言前的决策」与「发言」是**同一次调用**（`speech_plan`），
            #    所以"轮到某人开口时它看到了什么"要看这一次的包；
            #    `speech` 只剩退回路径（合并那次没给出正文时才补问），两个都收着最稳。
            if task.kind in ("speech_plan", "speech"):
                captured.append(packet)
            return await super().ask(packet, task)

    g.agent_factory = lambda seat: Cap(seat, rng)
    await g.run()

    pkt = captured[-1] if captured else ""
    i = pkt.find("【发言顺序与进度】")
    check("1a 白天的信息包里有【发言顺序与进度】", i >= 0, f"位置 {i}")
    block = pkt[i:i + 600] if i >= 0 else ""
    check("1b 写明了本轮发言顺序", "本轮发言顺序：" in block, block[:120].replace("\n", " "))
    check("1c 写明了谁是第几个发言 / 已经说过了",
          ("你是第" in block) or ("已经说过了" in block), "")
    check("1d 写明了「只能听到已经发过言的人」这条时间线规则",
          "只能听到" in block and "不可能知道" in block, "")
    check("1e 写明了「别把别人先说过的东西当成自己的新发现」",
          "新发现" in block, "")

    plans = [c for c in g.transcript if c.get("task") in ("speech_plan", "analyze")]
    check("2a 决策调用（发言前/投票前）真的发生了",
          bool(plans), f"{len(plans)} 次")
    body = str(plans[0].get("packet") or "") if plans else ""
    j = body.find("【身份校验")
    check("2b 决策提示词里有【身份校验】", j >= 0, f"位置 {j}")
    seg = body[j:j + 900] if j >= 0 else ""
    check("2c 要求先盘「可能是好人的理由 / 可能是狼的理由」",
          "好人的理由" in seg and "狼的理由" in seg, "")
    check("2d 有身份高低分档（铁好/高/中/低/可疑）",
          all(k in seg for k in ("铁好", "可疑")), "")
    check("2e 写明「铁好和高身份不能打、打铁好就是狼的逻辑漏洞」",
          "不能打" in seg and "逻辑漏洞" in seg, "")
    check("2f 写明「证据强时不要为了客观去盘反面」+ 残局才考虑深水倒钩",
          "不要" in seg and "超深水倒钩" in seg, "")
    check("2g schema 里有 tiers 字段（身份高低是结构化产出）",
          '"tiers"' in body or "tiers" in body, "")

    # 身份高低要真的交到「说话那一手」上。
    # ⚠️ 2026-10-07 起「发言前的决策」与「发言」合并成**同一次调用**，所以模型是拿着
    #    同一份提示词既给档位、又把话说出来的 —— 这里要看的是**合并那次**的提示词。
    merged = [str(c.get("packet") or "") for c in g.transcript if c.get("task") == "speech_plan"]
    check("3a 合并调用（决策+发言）的提示词里同时要了身份高低和说出口的话",
          any(("身份高低" in p and '"speech"' in p) for p in merged),
          f"{len(merged)} 份合并提示词")
    check("3b 合并调用的提示词里写明了「铁好和高身份不能打 / 打它本身就是狼的逻辑漏洞」",
          any(("铁好" in p and "不能打" in p and "逻辑漏洞" in p) for p in merged), "")

    # 退回路径（合并那次没给出正文 → 引擎补问一次发言）：那一手也得看见自己刚给的档位，
    # 否则「铁好/高不能打」只写在决策里、真开口时又忘了。
    class NoSpeech(MockAgent):
        async def ask(self, packet, task):
            reply = await super().ask(packet, task)
            if task.kind == "speech_plan":
                reply = dict(reply)
                reply.pop("speech", None)          # 模拟「模型只给了决策、没写正文」
            return reply

    g2 = Game(board=make_board("eleven_dream"), human_seat=0, seed=5, max_days=1,
              decision_first=True, sheriff=False)
    g2.agent_factory = lambda seat: NoSpeech(seat, random.Random(5))
    await g2.run()
    fallback = [str(c.get("packet") or "") for c in g2.transcript if c.get("task") == "speech"]
    with_tiers = [p for p in fallback if "身份高低" in p and "铁好/高身份" in p]
    check("3c 退回路径（补问一次发言）也把身份高低的铁律喂进去了",
          bool(fallback) and len(with_tiers) == len(fallback),
          f"{len(with_tiers)}/{len(fallback)} 次退回发言带档位")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
