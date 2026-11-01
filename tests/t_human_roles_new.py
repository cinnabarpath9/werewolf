# coding=utf-8
"""人类玩家在**第二版板子的三张新牌**上的路径回归（真人手机端最容易被忽略的一段）。

为什么单独写一个：
  `t_human_path.py` 钉的是 `eleven_standard` + 固定座位 6（女巫），
  它只证明"女巫/投票那一套面板没问题" —— **新角色（摄梦人/养鸦人/幸运儿）的真人提问
  从来没被测过**。而实测就抓到一个真问题：`lucky` 走通用选座面板 → 前端只发 `{target}`，
  引擎读不到 `use`/`skill` → **真人在手机端永远用不出幸运技能**。
  所以这里既测引擎侧的答复解析（HumanAgent 分支），也测前端真有对应的面板。

判据：
  1. HumanAgent 对 `dream` / `curse` / `lucky` 三种提问都能吃下真人输入，返回引擎认的字段；
  2. 「空过/不用」这条路真的走得通（不是返回了却被引擎忽略）；
  3. 前端 `labelMap` 有三种 kind，且 `lucky` **有专用面板**（两步：技能 → 目标），
     不是退化成通用选座；
  4. 引擎侧：幸运儿连续两晚想守同一个人时，会给一条**说清楚原因**的私有提示。
"""
from __future__ import annotations

import asyncio
import inspect
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import HumanAgent                                  # noqa: E402
from wolf.context import Task                                       # noqa: E402
from wolf.engine import Game                                        # noqa: E402
from wolf.roles import make_board                                   # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def feed(agent: HumanAgent, lines: list[str]) -> None:
    """把真人输入喂给 HumanAgent（它读 stdin）。"""
    it = iter(lines)
    async def fake_input(_prompt: str) -> str:
        return next(it, "").strip()
    agent._input = fake_input                                   # type: ignore[assignment]


def ask(agent: HumanAgent, kind: str, legal: list[int], **kw) -> dict:
    task = Task(kind=kind, text=f"（测试）{kind}", schema="{}", legal=legal, **kw)
    return asyncio.run(agent.ask("（测试用 packet，不显示）", task))


def ask_extra(agent: HumanAgent, kind: str, legal: list[int], extra: dict) -> dict:
    """带 extra 的提问（养鸦人的投票任务就是这么带诅咒选项的）。"""
    task = Task(kind=kind, text=f"（测试）{kind}", schema="{}", legal=legal, extra=extra)
    return asyncio.run(agent.ask("（测试用 packet，不显示）", task))


print("=== 一、HumanAgent 吃真人输入（问什么答什么）===")
a = HumanAgent(seat_id=10, show_packet=False)

# 养鸦人：诅咒不再是夜间提问，而是**投票任务里附带的一段**（用户 2026-10-09 改定时机）
feed(a, ["y", "3", "7"])
r = ask_extra(a, "vote", [3, 5, 7], {"curse": True, "curse_legal": [1, 3, 5, 7]})
check("1a 养鸦人：投票时顺带诅咒 3 号、自己投 7 号 → {vote:7, curse_target:3}",
      r.get("vote") == 7 and r.get("curse_target") == 3, str(r))

feed(a, ["n", "5"])
r = ask_extra(a, "vote", [3, 5, 7], {"curse": True, "curse_legal": [1, 3, 5, 7]})
check("1b 养鸦人：不用诅咒、只投票 → {vote:5} 且没有 curse_target",
      r.get("vote") == 5 and not r.get("curse_target"), str(r))

feed(a, [""])
r = ask(a, "dream", [1, 3, 5])
check("1c 摄梦人：直接回车 = 空过（abstain）", r.get("abstain") is True, str(r))

feed(a, ["9"])
r = ask(a, "dream", [1, 3, 5, 9])
check("1d 摄梦人：输入 9 → {target: 9}", r.get("target") == 9, str(r))

feed(a, ["y", "3", "7"])
r = ask(a, "lucky", [3, 7])
check("1e 幸运儿：y + 3(守护) + 7号 → {use:true, skill:'guard', target:7}",
      r.get("use") is True and r.get("skill") == "guard" and r.get("target") == 7, str(r))

feed(a, ["n"])
r = ask(a, "lucky", [3, 7])
check("1f 幸运儿：n → {use:false}（留到以后）", r.get("use") is False, str(r))

feed(a, ["y", "2", "5"])
r = ask(a, "lucky", [3, 5])
check("1g 幸运儿：2 → 毒（数字键映射对）", r.get("skill") == "poison", str(r))

print("\n=== 二、引擎真的按这份答复执行（不是收下就丢）===")
SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf", 5: "seer", 6: "witch",
         7: "dreamer", 8: "idiot", 9: "gravekeeper", 10: "crow", 11: "lucky"}


class Scripted:
    def __init__(self, sid, script, game=None):
        self.seat_id, self.script, self.game = sid, script, game

    async def ask(self, packet, task):
        t = self.script.get(task.kind)
        if callable(t):
            # ⚠️ 支持"按夜次给答案"的剧本（3 个参数）：`t_roles_v2.py` 也这么做。
            #    只吃 2 个参数的旧写法照样能用 —— 用签名判断，别靠 try/except 吞 TypeError
            #    （吞掉的话，脚本报错会静默退化成"引擎兜底随机"，测试还看着像规则问题）。
            try:
                n = len(inspect.signature(t).parameters)
            except (TypeError, ValueError):
                n = 2
            day = int(getattr(self.game, "day", 1) or 1) if self.game else 1
            return (t(self.seat_id, task, day) if n >= 3 else t(self.seat_id, task)) or {}
        return dict(t or {}) if isinstance(t, dict) else {}

    def last_meta(self):
        return {}

    async def aclose(self):
        return None


def run(script: dict, days: int = 2, seed: int = 33):
    g_holder: list = [None]
    g = Game(board=make_board("eleven_dream"), seed=seed, max_days=days, decision_first=True,
             agent_factory=lambda s: Scripted(s.id, script, g_holder[0]),
             on_event=lambda *a, **k: None)
    for sid, role in SEATS.items():
        g.seat_by_id(sid).role_key = role
    asyncio.run(g.run())
    return g


BASE = {
    "wolf_intent": {"target": 9, "reason": "剧本"},
    "wolf_kill": {"target": 9, "reason": "剧本"},
    "wolf_talk": {"speech": "剧本", "target": 9},
    "wolf_explode": {"explode": False, "who": None, "reason": "剧本"},
    "seer_check": {"target": 1, "reason": "剧本"},
    "witch": {"save": False, "poison": None, "reason": "剧本"},
    "hunter_shoot": {"shoot": None, "reason": "剧本"},
    "speech_plan": {"reads": [], "vote": None, "speech": "剧本", "claim": "无"},
    "analyze": {"reads": [], "vote": None, "speech": "剧本", "claim": "无"},
    "speech": {"speech": "剧本", "claim": "无"},
    "vote": {"abstain": True, "reason": "剧本"},
    "last_words": {"speech": "剧本", "claim": "无"},
    "sheriff_run": {"run": False},
    "sheriff_speech": {"speech": "剧本", "claim": "无"},
    "sheriff_vote": {"abstain": True},
    "badge": {"to": None, "reason": "剧本"},
    "sheriff_order": {"explode": False, "reverse": False},
    "dream": {"abstain": True},
    "curse": {"abstain": True},
    "lucky": {"use": False},
}

s1 = dict(BASE)
s1["lucky"] = lambda sid, task: ({"use": True, "skill": "poison", "target": 1}
                                 if sid == 11 else {"use": False})
g = run(s1, days=2)
check("2a 幸运儿选「毒 1 号」→ 1 号第 2 夜被毒死（引擎真的执行了；第 1 夜引擎不问）",
      not g.seat_by_id(1).alive and g.seat_by_id(1).death_cause == "poison",
      f"alive={g.seat_by_id(1).alive} cause={g.seat_by_id(1).death_cause}")

s2 = dict(BASE)
s2["lucky"] = lambda sid, task: ({"use": True, "skill": "guard", "target": 9}
                                 if sid == 11 else {"use": False})
# ⚠️ 刀口要**按夜次**：幸运儿第 1 夜不问（新规则），狼若第 1 夜就刀 9 号，
#    9 号会在被守护之前就死掉 → 假红。第 1 夜刀 5 号（预言家），第 2 夜才刀 9 号。
s2["wolf_intent"] = lambda sid, task, day: {"reads": [], "summary": "剧本",
                                            "target": (9 if day >= 2 else 5),
                                            "reason": "剧本：按夜次给刀口"}
g = run(s2, days=2)
check("2b 幸运儿选「守护 9 号」→ 被刀的 9 号活下来",
      g.seat_by_id(9).alive, f"alive={g.seat_by_id(9).alive}")

print("\n=== 三、官方「守护不能连守」在本项目自动满足（技能只有一次）===")
s3 = dict(BASE)
# ⚠️ 2026-10-30：target 必须给**活着**的人。以前这里写 9 号（第 1 夜就被狼刀了），
#    而旧代码在"target 不合法"时会随机改摄/改守 —— 也就是说这条断言一直靠随机兜底在绿。
#    现在不合法就**不消耗**技能，所以这里改成第 2 夜仍活着的 10 号。
s3["lucky"] = lambda sid, task: ({"use": True, "skill": "guard", "target": 10}
                                 if sid == 11 else {"use": False})
g = run(s3, days=3)
asks = [e for e in g.seat_by_id(11).private_events if "幸运技能" in e or "没有使用" in e]
# 第一版这里断言"第二夜换人会收到提示" —— 那是**我自己写错的判据**：
# 幸运儿的技能是**一次性**的，第二夜根本不会再问，也就不存在"连守"。
check("3a 幸运儿只被问过一夜（用完就没了 → 官方连守限制自动满足）",
      sum(1 for e in g.seat_by_id(11).private_events if "幸运技能" in e) == 1, str(asks))
# ⚠️ 判据要盯**代码行**：`lucky_last_guard` 现在只出现在"我删掉了它"的注释里，
#    直接查整文件字符串会把注释也算进去（第一版就踩了这个假红）。
# ⚠️ 路径必须用 ROOT 拼**绝对路径**：runner 是以 tests/ 为工作目录跑脚本的，
#    写成 "wolf/engine.py" 单跑能过、全量就 FileNotFoundError（实测踩到）。
_code_hits = [ln.strip() for ln in io.open(os.path.join(ROOT, "wolf", "engine.py"), encoding="utf-8")
              if "lucky_last_guard" in ln and not ln.strip().startswith("#")]
check("3b 引擎**代码**里不再有 lucky_last_guard 这类死逻辑（注释里说明可以）",
      not _code_hits, str(_code_hits[:2]))

print("\n=== 四、前端面板：三种 kind 都得有界面，幸运儿必须是专用面板 ===")
html = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
m = re.search(r"const labelMap\s*=\s*\{(.*?)\}\s*;", html, re.S)
keys = set(re.findall(r"(\w+)\s*:", m.group(1))) if m else set()
# ⚠️ `curse` 2026-10-09 起**不再是独立的夜间提问**（挪进投票面板），所以 labelMap 里没有它是对的。
for k in ("dream", "lucky"):
    check(f"4[{k}] labelMap 里有 {k}", k in keys, str(sorted(keys)[:8]))
check("4[curse] 养鸦人的诅咒改成**投票面板里的一段**（不再有夜间面板）",
      "curse" not in keys and "(ask.extra || {}).curse" in html,
      str(sorted(keys)[:10]))
check("4b 幸运儿有**专用面板**（不是退化成通用选座）",
      "ask.kind === 'lucky'" in html and "确认用掉这次机会" in html)
check("4c 幸运儿面板给了「留到以后（今晚不用）」这条路", "留到以后（今晚不用）" in html)
check("4d 摄梦人/养鸦人的空过按钮写了人话",
      "今晚不摄（空过）" in html and "今晚不咒人" in html)
check("4e 幸运儿面板列出了三个技能（查验/毒/守护）",
      all(k in html for k in ("① 查验", "② 毒", "③ 守护")))
check("4f 投票面板里有「不用诅咒」和「咒 X 号」两种选择",
      "不用诅咒" in html and "'咒 ' + id + '号'" in html)
check("4g 弃票时诅咒照样生效（payload 里也带 curse_target）",
      "if (curseOn && curseTarget !== null) payload.curse_target = curseTarget;" in html)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
