# coding=utf-8
"""进包纪律回归：「消息可信度」与「三条纪律」必须真的到达模型。

来历
----
对标《汪里个狼》Demo 时发现它给模型的消息带说话人类型（法官公告 = 绝对真、
其他玩家 = 可能假），并且明确写「每个人发言时只看得到他前面的消息」；
还硬性要求「不许同时信两套互斥的身份链、改站边必须交代被什么推翻、先自己判断别跟风」。
本项目补了前一条的「可信度」与后两条，落在：

  · `content/规则书.md` 的 `frag:core` ——【怎么读这份记录】
  · `content/打法书.md` 的 `frag:all` ——「三条纪律」

这份回归守两件事：① 文字还在进包段里（不是写到"给人看"的正文里）；
② 它们真的出现在**每张座位**的包里（好人和狼都要拿到 —— 这是通用段）。
并做一次变异自证：把命中的句子从包里抠掉，同一套断言必须变红。

跑法：python t_prompt_discipline.py      （纯本地 mock 局，不联网、不花钱）
"""
from __future__ import annotations

import asyncio
import io
import os
import random
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wolf.agents import MockAgent                       # noqa: E402
from wolf.engine import Game                            # noqa: E402
from wolf.roles import make_board                       # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []

#: 规则书里那条「怎么读记录」必须写到的关键词
RULES_MARKS = ("【怎么读这份记录】", "绝对真实", "也可能是伪装", "对账")
#: 打法书三条纪律必须写到的关键词
PLAYBOOK_MARKS = ("三条纪律（每一手发言都适用）", "先自己判断", "只认一个真预言家", "改边要交代被什么推翻")
#: 2026-10-30 新加的三条判断硬规矩（每张包都要有）
JUDGE_MARKS = ("判断的三条硬规矩（每一手都适用）", "盘双边再下结论", "证据有先后",
               "验人理由只能用昨夜之前的信息")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def frag_text(path: str, name: str) -> str:
    """取 `<!-- frag:xxx -->` … `<!-- frag:end -->` 之间的文本（只取外层）。

    `core:begin` 这种带 `:begin` 的写法，结束标记是 `<!-- frag:core:end -->`。
    """
    text = io.open(path, encoding="utf-8").read()
    if name.endswith(":begin"):
        base = name[:-6]
        pat = re.compile(r"<!--\s*frag:%s:begin\s*-->(.*?)<!--\s*frag:%s:end\s*-->"
                         % (re.escape(base), re.escape(base)), re.S)
    else:
        pat = re.compile(r"<!--\s*frag:%s\s*-->(.*?)<!--\s*frag:end\s*-->" % re.escape(name), re.S)
    m = pat.search(text)
    return m.group(1) if m else ""


async def main() -> int:
    # ── 0. 静态：文字必须在「进包段」里，不是在人看的正文里 ──────────────
    core = frag_text("content/01_规则书.md", "core:begin")
    allseg = frag_text("content/05_战术书.md", "all")
    check("0a 规则书 frag:core 含【怎么读这份记录】", "【怎么读这份记录】" in core,
          f"core 段 {len(core)} 字")
    check("0b 同段写明了公告真实 / 发言可能假 / 靠对账",
          all(k in core for k in RULES_MARKS), ",".join(k for k in RULES_MARKS if k not in core))
    check("0c 打法书 frag:all 含三条纪律", all(k in allseg for k in PLAYBOOK_MARKS),
          ",".join(k for k in PLAYBOOK_MARKS if k not in allseg))
    check("0e frag:all 含三条判断硬规矩", all(k in allseg for k in JUDGE_MARKS),
          ",".join(k for k in JUDGE_MARKS if k not in allseg))
    # 反向：这三条不许只出现在人看的部分（否则等于没进包）
    whole_playbook = io.open("content/05_战术书.md", encoding="utf-8").read()
    outside = whole_playbook.replace(allseg, "")
    check("0d 三条纪律不是只写在 frag:all 之外", "三条纪律（每一手发言都适用）" not in outside, "")
    check("0f 三条判断硬规矩也不是只写在 frag:all 之外",
          "判断的三条硬规矩（每一手都适用）" not in outside, "")

    # ── 1. 实跑一局，抓每张确定座位的包 ────────────────────────────────
    #  ⚠️ 直接 `Game(...)` 是不带内容层的（打法书/概念库/规则书由 run_game.py 注入），
    #     这里照生产的样子接上，否则抓到的包只有协调层那两千多字。
    from wolf import tabletalk                              # noqa: E402
    rng = random.Random(11)
    g = Game(board=make_board("eleven_standard"), human_seat=0, seed=11, max_days=2,
             decision_first=True, sheriff=False,
             rules_mode="packet",
             player_prompt=tabletalk.player_prompt({}, ROOT),
             playbook_text=tabletalk.load_playbook(ROOT, ""),
             meta_text=tabletalk.load_meta_book(ROOT, ""))
    captured: dict[int, str] = {}

    class Cap(MockAgent):
        async def ask(self, packet, task):
            if task.kind in ("speech_plan", "speech") and self.seat.id not in captured:
                captured[self.seat.id] = packet
            return await super().ask(packet, task)

    g.agent_factory = lambda seat: Cap(seat, rng)
    await g.run()

    check("1a 抓到了发言用的包", bool(captured), f"{len(captured)} 张座位")
    packets = list(captured.values())
    check("1b 每个包都有【怎么读这份记录】",
          all("【怎么读这份记录】" in p for p in packets),
          f"{sum('【怎么读这份记录】' in p for p in packets)}/{len(packets)}")
    check("1c 每个包都有三条纪律",
          all(k in p for p in packets for k in ("三条纪律（每一手发言都适用）", "只认一个真预言家")),
          f"{sum('三条纪律（每一手发言都适用）' in p for p in packets)}/{len(packets)}")
    check("1d 每个包都有三条判断硬规矩",
          all(k in p for p in packets for k in JUDGE_MARKS),
          f"{sum('证据有先后' in p for p in packets)}/{len(packets)}")

    # ── 1.5 时间线纪律（2026-11-03 真局复盘：悍跳狼用今天才公开的"上警/前置位"
    #        去解释昨晚为什么验人，被好人当场抓成时间线穿帮）──────────────────
    _sys_prompt = io.open(os.path.join(ROOT, "wolf", "llm.py"), encoding="utf-8").read()
    check("1e 系统提示词里有「时间线纪律」", "时间线纪律" in _sys_prompt and "昨晚" in _sys_prompt)
    _playbook = io.open(os.path.join(ROOT, "content", "04_角色书.md"), encoding="utf-8").read()
    check("1f 打法书给了固定报验句式（昨晚 + 查杀/金水）",
          "报验句式" in _playbook and "昨晚我验的是" in _playbook)
    check('1g 打法书点明「不能用今天才公开的信息」', "绝不能" in _playbook and "昨晚之前就有" in _playbook)
    check("1i 系统提示词里有「言行一致（身份口径）」",
          "言行一致" in _sys_prompt and "不跳身份" in _sys_prompt)
    _pb05 = io.open(os.path.join(ROOT, "content", "05_战术书.md"), encoding="utf-8").read()
    check("1j 打法书三条纪律里加了「言行一致」", "言行一致（身份口径" in _pb05)
    check("1h 每个发言包里都带时间线提醒",
          all("时间线" in p for p in packets),
          f"{sum('时间线' in p for p in packets)}/{len(packets)}")

    # ── 2. 变异自证：把命中的句子从包里抠掉，同一套断言必须变红 ──────────
    if packets:
        p0 = packets[0]
        mutated = (p0.replace("只认一个真预言家", "")
                     .replace("【怎么读这份记录】", "")
                     .replace("证据有先后", "")
                     .replace("盘双边再下结论", ""))
        check("2a 变异：抠掉这几句后，原来的判据不再成立",
              ("只认一个真预言家" not in mutated) and ("【怎么读这份记录】" not in mutated)
              and ("证据有先后" not in mutated) and ("盘双边再下结论" not in mutated)
              and not all(k in mutated for k in ("三条纪律", "只认一个真预言家"))
              and not all(k in mutated for k in JUDGE_MARKS),
              "证明 1b/1c/1d 不是恒真断言")
    else:
        check("2a 变异自证", False, "没有抓到包，无法变异")

    bad = [x for x in RESULTS if not x[1]]
    print("=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
