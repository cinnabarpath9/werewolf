# coding=utf-8
"""狼人杀内核：状态机 + 可见性边界 + 台账。

责任划分（这是"稳定"的来源）：
- **代码**负责：发牌、合法目标、计票、多数据决、技能结算、屠边胜负、超时降级、存档。
- **模型**负责：发言、以及在被给定的合法目标里做选择。
模型不许自己算规则，也不许看到不该看的东西 —— 它拿到的只有 context.build() 产出的那个包。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from . import context, rules, words
from .context import Task
from .ledger import WOLF, Ledger
from .roles import (ROLES, WOLF as WOLF_CAMP, BoardDef, make_board, role_name,
                    seats_text, validate_board)

#: 死因只在私有视角里出现，公开记录里**不写**（标准规则：夜里死亡不公布死因，让场上好人自己猜）。
#: ⚠️ 唯一的例外是**白天**那一枪：按用户定的规则，白天被猎人打死的人死因公示为"被子弹打死"，
#: 这条由 hunter_shoot(daytime=True) 自己写在公告里，不走这张表。
#: 发言上限（2026-11-03）：提示词让模型写 350 字，但**硬上限**放宽到 800，
#: 并且从**句末**截断 —— 以前 `[:600]` 会从半句中间切，模型写超时用户看到的是断句。
#: 2026-11-03（真局复盘）：模型嘴上说"不跳身份"，却在发言里把身份报了出来、claim 还落了账。
#: 命中这些说法时，claim 一律不算（发言原话保留，只不记"声称"）。
NO_CLAIM_MARKS = ("不跳身份", "不报身份", "不亮身份", "先不跳", "暂不跳",
                  "这轮不跳", "不能跳身份", "先藏身份")

SPEECH_LIMIT = 800


def clip_speech(text: str, limit: int = SPEECH_LIMIT) -> str:
    """超长发言按"最后一个句号"截断，补一个省略号；没句号就硬截。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("\n", "。", "！", "？", "；", ".", "!", "?"):
        idx = head.rfind(sep)
        if idx >= limit // 2:                 # 至少保住一半内容，别切得只剩一句
            return head[:idx + len(sep)] + "……"
    return head + "……"


def claim_allowed(speech: str, claim: str) -> bool:
    """言行一致检查：说了"不跳身份"就别记身份声称（返回 False 时只丢 claim，不动发言原话）。"""
    c = str(claim or "").strip()
    if not c or c in ("无", "none", "None", "-", "null"):
        return False
    text = str(speech or "")
    return not any(mark in text for mark in NO_CLAIM_MARKS)


CAUSE_TEXT = {
    "wolf": "夜里被狼人杀害",
    "poison": "夜里被女巫毒死",
    "vote": "白天被投票放逐",
    "hunter": "被猎人开枪带走",
    "lucky": "被幸运儿开枪带走",
    # 2026-10-10 补：下面两条以前没登记，私密经历里会写成英文 key（`dream` / `explode`）
    "dream": "夜里被摄梦人连摄两晚而死",
    "explode": "自爆出局",
    # 狼王（新板子 `eleven_wolfking`）：被他临死前带走
    "king": "被狼王带走",
    "knight": "被骑士审判出局",
}

#: 狼王自爆时要**同时回答**"带走谁" —— 这段提示与 schema 字段**只写这里一份**。
#: 为什么（2026-10-10）：狼有**五个**自爆窗口（上警决策 / 竞选发言 / 警长定方向 /
#: 轮次发言决策 / 狼队团队决策），每个窗口都是一次已有的调用；狼王那一口必须
#: **并进同一次调用**（作者要求：避免额外调用），所以五个窗口都得问同一件事 ——
#: 抄五遍必然漏一处，漏掉的那一处会变成"狼王自爆白爆"（静默少一个技能）。
KING_TAKE_HINT = ("· 你是【狼王】：你**出局时当场带走一名好人**（不能带狼队友）——"
                  "判到这一手自己会出局，就把 `take` 一起填：你带走谁。（11 人板狼王不能自爆）\n")
KING_TAKE_SCHEMA = ('"take": <你是狼王时：这一手出局后带走的编号；不是狼王或不用就填 null>, ')


def king_take_hint(seat) -> str:
    """这个座位是不是狼王？是 → 返回"自爆要一起填 take"那一行；不是 → 空串。"""
    return KING_TAKE_HINT if getattr(seat, "role_key", "") == "wolf_king" else ""


class GameAborted(Exception):
    """玩家放弃本局时，从 act() 里抛出来，让引擎在检查点停下。"""


#: 每一局一个只增不减的代号（`run_token`）：事件带上它，服务端据此丢掉"上一局的任务还在发"。
#: 进程内计数器就够 —— 一局一个对象，"哪一局"不需要跨进程唯一。
_RUN_SEQ = itertools.count(1)


#: 模型/配置把布尔写成各种花样时的归一化表（`bool("false")` 是 True，所以必须显式判）。
#: ⚠️ 2026-10-08 起表本体搬到了 `wolf/words.py`：`context.py` 读同一个开关时也要用它，
#:    而 engine ↔ context 是互相依赖的（`context` 不能 `from .engine import`，那是循环 import）。
#:    这里的两个名字是**导入过来的别名**，老调用方（含 tools/ 与历史测试）照旧能用。
TRUE_WORDS = words.TRUE_WORDS
FALSE_WORDS = words.FALSE_WORDS

#: 哨兵：用来区分"调用方没传 default"和"调用方就是要 None"。
#: 不能写 `default=None` 再判 `if default is not None` —— 那样"压不出目标就什么都别做"
#: 这个意图根本表达不出来，猎人点「不开枪」会变成随机开枪打死一个人（实测过）。
_NO_DEFAULT = object()


def _backoff_list(value) -> list[float]:
    """把 `retry_backoff` 配置规范化成浮点列表。

    用户可能怎么写：`[3, 8]`（正常）、`3`（只写一个数）、`"3,8"`（写成字符串）。
    ⚠️ 直接 `list(value)` 的话：写标量会 **TypeError 让开局直接崩**，
    写字符串会被拆成 ['3', ',', '8']（后面 float(',') 又炸）。这里统一兜住。
    """
    if value is None:
        return [3.0, 8.0]
    if isinstance(value, str):
        items = [x for x in re.split(r"[,，\s]+", value.strip()) if x]
    elif isinstance(value, (int, float)):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            return [3.0, 8.0]
    out: list[float] = []
    for item in items:
        try:
            num = float(str(item).strip())
        except (TypeError, ValueError):
            continue
        if num >= 0:
            out.append(num)
    return out or [3.0, 8.0]


def _secs(value) -> str:
    """把秒数印成人看的样子：150 → "150"、0.5 → "0.5"、30.0 → "30"。

    ⚠️ 别用 `int(value)`：配置写成 0.5 秒时它会印成"超过 0 秒"（测试里已经踩到）。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{num:g}"


def normalize_word(value) -> str:
    """把"是/否"这类回答归一成小写关键词。

    truthy 和"识别不了就写提示"那处**必须共用这一个函数**：
    否则会出现「按'救'执行了」+「提示说你写的不认识，我按不救处理」这种自相矛盾的私有经历。
    """
    return words.normalize_word(value)


#: `game.call_timeout` / `game.decision_timeout` **缺键时的兜底值**（唯一一处，2026-10-08 收敛）。
#: 为什么要专门放一处：这两把秒数以前在四个地方各写了一份 ——
#:   `server.py` 推给手机的 status 写 300、同一份配置拿去建 `Game` 时写 150、
#:   `tools/exam_live.py` 又写 150、引擎自己的参数默认是 0（= 不设上限）。
#: 结果：配置里一旦漏写/写错键名，手机显示「最多等到 300 秒」而引擎 150 秒就砍了
#: —— 正是这个项目最忌讳的「两把尺」。现在 status 与真正建局走同一个函数。
#: ⚠️ 数值口径与 `config.yaml` 的**正式值保持一致**（150 / 540）；这里只是**缺键兜底**，
#:    改 config 时顺手同步它 —— 否则"配置漏写一个键"就悄悄变成另一套时间口径。
#:    （沿革：300/300 → 420/540（2026-10-25）→ **150/540**（2026-11-03 作者把单次收到 150）。）
DEFAULT_CALL_TIMEOUT = 150.0
DEFAULT_DECISION_TIMEOUT = 540.0


def timeouts_from_cfg(game_cfg: dict | None) -> tuple[float, float]:
    """从 `config['game']` 取 (单次上限, 整手预算)，缺键时用上面那套兜底值。

    `server.py`（status 与建局两处）与 `tools/exam_live.py` **都调它** ——
    这样"手机上看到的秒数"和"引擎真正用的上限"永远出自同一个数。
    """
    cfg = game_cfg or {}
    return (float(cfg.get("call_timeout", DEFAULT_CALL_TIMEOUT)),
            float(cfg.get("decision_timeout", DEFAULT_DECISION_TIMEOUT)))



#: 「身份层级」通用规则（用户 2026-10-07 提的认知问题，2026-11-06 改成五档）：
#: 模型以前不做"这个身份合不合理"的校验 —— 被两家预言家都发过金水的人（双金水）在任何
#: 视角下都是铁好、谁打谁就是狼，但模型照样会被打、也照样去打。
#: 这段规则加在**每一次决策**（发言前决策 / 投票前决策 / 夜间决策）的提示词里。
TIER_RULES = (
    "\n\n【身份层级（必须先做，再做决定）】\n"
    "  1) 逐个盘场上活着的人**可能是什么身份**：他可能是好人的理由、可能是狼的理由。\n"
    "     · 用**正逻辑**盘一遍（狼人打的人是好、狼人保的人是坏），再用**反逻辑**盘一遍（假打实保 / 假保实打）；\n"
    "       正逻辑解释得通就先用正逻辑；只有它解释不通（票型/行为对不上）时才翻到反逻辑。\n"
    "  2) 给每个人一个档位（写进 tiers），**只能从这五档里选**；下面只是典型例子，**不是穷举**，\n"
    "     遇到没列出的情况，按证据强度和最接近的档位归类：\n"
    "     · **铁好** = 双金水、翻牌白痴/公开翻牌自证、狼金预（悍跳狼给真预发金水，真预反手拍死悍跳）、\n"
    "       两个独立硬来源都证明的好人；逻辑上不可能有狼面，任何视角下都打不动。\n"
    "     · **身份偏好** = 银水（可能是自刀）、单金水、金银花露水（金水+女巫救）、铜水（被守过又吃过刀）、\n"
    "       带毒女巫、已翻牌愚者等；有信息/被狼攻击/有自证或反打手段，但**不是绝对好**。\n"
    "     · **普通** = 没有硬信息；开局所有人都先算普通，两个预言家对跳时两个预也是 50/50。\n"
    "     · **偏狼** = 单查杀、金杀（一人金水一人查杀）、位置学进坑、发言逻辑乱/票型对不上。\n"
    "     · **铁狼** = 双查杀、翻牌狼等**两个独立硬来源**定死的狼。\n"
    "  3) **分清两种“铁”**：你自己的私有技能明确反馈（验人/守墓/毒/刀口）在你自己的视角可以定铁；\n"
    "     但报给别人只是参考，别人最多按偏狼/偏好处理，除非有公开对账。**公共视角**的铁只来自\n"
    "     公开结算 / 翻牌 / 全员能核对的结果；发言、位置、票型只能放在普通↔偏狼/偏好之间。\n"
    "  4) **铁律**：铁好**不能打**；身份偏好**没有具体爆点也别打**（硬打要说明你掌握了什么新信息）；\n"
    "     铁狼优先归票；普通/偏狼按发言与票型排序。\n"
    "  5) **先数自己的信息再开口**：自己验过几张金水/查杀、谁说的，只能报自己真有的；\n"
    "     不要把别人的金水算成自己的，也不要凭空说“几张金水”。\n"
    "  6) 一边的证据已经足够强时，**不要**为了显得客观去盘反面；只有到残局（活人很少、\n"
    "     实在找不到更可疑的人）才考虑“超深水倒钩狼”这种可能。\n"
    "  7) **先给自己定一个公开档位（self_tier）**：不是你的真实身份，而是“别人现在会怎么看你这张牌”。\n"
    "     · 自评**铁好/身份偏好** → 可以带队找狼；盯身份偏好/普通/偏狼三类，优先从普通/偏狼里找票，\n"
    "       偏好要有具体爆点再打；自评偏好时多攻击普通/偏狼，少自证。\n"
    "     · 自评**普通** → 表水（证明自己是好人）和攻击（找狼）各一半，不要只自证也不敢打；\n"
    "     · 自评**偏狼/铁狼** → **少攻击、多表水**；乱打会树敌、多吃票；好人要先把轮次留住，\n"
    "       狼也要尽量多活一天、把好人扛推出去。\n"
    "     · 你是狼时同样按这个公开档演：别人越信你，你越能带节奏；别人越疑你，越要少树敌。\n"
    "  8) **私有信息可以多次引用，但不要无意义复读**：核心结论报一次；盘正逻辑、对跳、解释视角\n"
    "     需要用到就正常拿出来（二三次引用完全可以）。但它仍是你的单方信息，别人不能当场核对，\n"
    "     对外最多按偏狼参考，不要包装成公共铁证。先正逻辑（撞结果可能是巧合），要盘故意做局\n"
    "     就拿公开票型/发言/位置。悍跳狼该编假的私有信息就编，用来乱视角、混局势。\n"
    "  9) **狼点分两类**：公共狼点（上下邻位双铁好等位置、发言/票型极差，所有人能盘）\n"
    "     和私人狼点（你的技能私有信息确认的狼，再据此推其他牌）。私人狼点在你视角可算铁狼，\n"
    "     报出来别人只能当偏狼参考；公共证据也对上时才升“定狼”。\n"
    "  10) **对跳定狼还是偏狼**：你确知自己牌、开局全活且无穿身份 → 对跳者在你视角是定狼；\n"
    "     旁观者只能判二选一；残局/可能穿衣服/信息混 → 先按偏狼，不要直接定死。\n"
    "  11) **警徽流只属于竞选/拿徽的预言家**：警下预言家只说今晚验谁；非预言家警长不用留，\n"
    "     出局自行决定警徽去向，愿意可在发言结尾轻提“我若倒牌，徽给X”，浅浅站台拉票即可。\n"
    "     拿到警徽、听完一轮发言后，要重新报一次两个警徽流（尤其悍跳狼），别只沿用竞选版；\n"
    "     重报要自洽，并继续用位置学/保人/脏人把水搅浑；悍跳狼的流要围绕阴人/坑票/打抗推位设计，\n"
    "     但仍要符合位置/概率，不能一眼假；真预的流优先摸反对自己的人——金水劝回、查杀打死，\n     站错边的好人会占掉一个假狼坑，推到后面狼坑盘不出去。\n"
    "  12) **归票有优先级，但允许改票**：归票口径（嘴上号召）按自己视野：铁狼/明狼 > 偏狼 >\n"
    "     普通里做狼；不能跳过明狼去号召普通。**实际票默认同序，但可以改**：\n"
    "     · 好人：后置位给出你发言时没听到的新信息（跳愚者/翻牌/公开结算）→ 按全场信息改票，\n"
    "       第二天说清哪条新信息改变了判断；\n"
    "     · 狼：可以口头归 A、实际冲 B 做分票/合票，但必须提前想好第二天怎么解释票型，\n"
    "       并算被女巫毒、幸运儿、摄梦人梦死的风险；\n"
    "     · 改票后票型怪、又解释不了 → 不要改。悍跳狼嘴上把对跳者叫明狼，归票口径仍应归他；\n"
    "       实际票若改，必须按上面的解释/风险预算来。\n"
    "  13) **守墓人局第二天**：若已确认出局的预言家位不是真预，存活真预的查杀就直接按狼打，\n"
    "     不用再盘；出局位若也是悍跳狼则与查杀连狼，若出局位是好人则他不进狼坑、但查杀仍打。\n"
    "     被查杀的狼应该**自己拍守墓人**，不要派外置位狼对跳多漏一个队友；\n"
    "     真守墓人按天报线、拿公开放逐对账。\n  14) **验人位每天都必须报昨晚查验**：真预每天有验，悍跳每天也要编一个新查验；\n     悍跳位第二天不报查验是最大假预破绽，不报/报重复/拿第一天顶第二天都会崩。")
TIER_LEVELS = ("铁好", "身份偏好", "普通", "偏狼", "铁狼")
TIER_ALIASES = {"顶级铁好": "铁好", "铁好": "铁好", "高": "身份偏好",
                "中": "普通", "低": "偏狼", "可疑": "偏狼"}


def normalize_tier(raw, default: str = "普通") -> str:
    """把模型/旧存档的档位名归一成新五档；空值返回 default。"""
    text = str(raw or "").strip()[:6]
    if not text:
        return default
    text = TIER_ALIASES.get(text, text)
    return text if text in TIER_LEVELS else default


def truthy(value, default: bool = False) -> bool:
    """把模型给的"是/否"归一成布尔。判不出来就用 default（并让调用方记一笔）。

    ⚠️ 表在 `wolf/words.py` 里，跟 `context._as_bool`（配置开关）**共用同一张** ——
    以前配置那份只认 `false/no/off/0`，写「关」会被读成 True（看起来关了其实还开着）。
    """
    return words.truthy(value, default)


def parse_sheriff_setting(raw) -> bool | None:
    """把 config 里的 `game.sheriff` 解析成 True / False / None（None = 按人数自动）。

    ⚠️ 字符串必须显式认：YAML 里写 `sheriff: "false"`（带引号）拿到的是字符串，
    而 `bool("false")` 是 **True** —— 会被静默理解成"所有人数都开警长"。
    server.py 和 run_game.py **共用这一个**解析器，免得两处再次跑偏。
    """
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("auto", "none", ""):
            return None
        # ⚠️ 真假值走 `wolf/words.py` 那张**共用**的表（跟 context 读配置开关的是同一张）：
        #    以前这里手抄了一份，于是「关闭」这类写法在这条路上不认、在别处认。
        if text in words.FALSE_WORDS:
            return False
        if text in words.TRUE_WORDS:
            return True
    return bool(raw)


@dataclass
class Seat:
    id: int
    role_key: str
    is_human: bool = False
    alive: bool = True
    mind: str = ""                                  # 自己的推理笔记（私有，跨回合保留）
    experience: str = ""                            # 从往局提炼出来的经验（去身份化，可选）
    persona: str = ""                               # 说话风格（表达层，身份无关）
    card_name: str = ""                             # 角色卡上的姓名（表达层、公开信息）
    private_events: list[str] = field(default_factory=list)
    checked: dict[int, str] = field(default_factory=dict)   # 预言家：验过谁、结果
    last_guard: int | None = None                           # 守卫：昨晚守了谁（不能连守）
    last_dream: int | None = None                           # 摄梦人：昨晚摄了谁（连续两晚会死人）
    last_curse: int | None = None                           # 养鸦人：上一次诅咒了谁（**仅记录**）
    last_curse_day: int = 0                                 # 养鸦人：那一次是哪一天咒的（**仅记录**）
    #: ⚠️ 这两个字段**已经不参与"能不能咒"的判断了**（2026-10-09 作者改口径：
    #:    **可以连着两天甚至更多天咒同一个人** —— 它本质是「两票的民牌」）。
    #:    留着是为了复盘/自查能看出"他这几天一直压着谁"，别再拿它去加限制。
    lucky_used: bool = False                                # 幸运儿：一次性技能是否已用掉
    #: 幸运儿选了「子弹」：还没打出去时是 True；出局时按死因决定能否开枪（毒/梦死不能）。
    lucky_bullet: bool = False
    knight_used: bool = False                               # 骑士：本局审判是否已经发动
    knight_failed: bool = False                             # 骑士：审判到好人后票权降为 0.5（不出局）
    revealed_idiot: bool = False                            # 愚者：已经翻牌免疫过一次（仍可投票，票权 0.5）
    #: 狼王（新板子 `eleven_wolfking`）：他临死那一口**只能带走一次**。
    #: 为什么要有这个标记：他能被四种路径弄死（夜里死 / 被放逐 / 被枪带走 / 自爆），
    #: 每条路径都要在"判胜负之前"给他一次带人机会 —— 没有这个闸，一条路径补了、
    #: 另一条路径忘了补就会**静默吞掉他的技能**（或者反过来重复触发）。
    king_done: bool = False                                 # 狼王：已经用过"带走一人"
    death_day: int | None = None
    death_cause: str | None = None
    agent: object = None

    @property
    def role(self):
        return ROLES[self.role_key]

    @property
    def camp(self) -> str:
        return self.role.camp

    @property
    def kind(self) -> str:
        return self.role.kind


class Game:
    #: 构造时不传板子时的兜底（正常调用都会显式传；配置里的默认板子在 config.yaml）。
    def __init__(self, board: str | BoardDef = "eleven_wolfking", human_seat: int = 0,
                 human_seats: int | list | set | tuple | None = None,
                 agent_factory=None, seed: int | None = None,
                 max_days: int = 20, on_event=None,
                 pk_on_tie: bool = True, wolf_control: bool = True,
                 sheriff: bool | None = None, sheriff_min_seats: int = 11,
                 sheriff_picks_order: bool = True,
                 wolf_explode: bool = True,
                 #: 狼队夜间成本折中（作者 2026-10-09 批准）：
                 #: **四头狼私下定的刀口完全一致时，跳过夜聊**（省 len(wolves) 次调用）。
                 #: 只要有一头狼想刀别人，就照常开讨论。设 False 恢复「每晚都讨论」。
                 wolf_talk_skip_when_unanimous: bool = True,
                 #: 本板子预言家**锁定上警**（作者 2026-10-10）：不给他发 `sheriff_run` 调用，
                 #: 直接算作候选人（前置位）。设 False 恢复「也问他一次」。
                 seer_auto_run: bool = True,
                 on_stall=None, rules_mode: str = "off",
                 rules_detail: str = "brief",
                 call_timeout: float = 0, decision_timeout: float = 0,
                 player_prompt: str = "", meta_text: str = "",
                 edge_order: str = "sides_first", playbook_text: str = "",
                 auto_retries: int = 0, retry_backoff: list | None = None,
                 retry_jitter: float = 0.3,
                 carry_reasoning: bool = True, carry_reasoning_chars: int = 300,
                 decision_first: bool = False,
        context_compress: str = "off") -> None:
        self.board_def: BoardDef = board if isinstance(board, BoardDef) else make_board(str(board))
        # ⚠️ 在这里就把板子拦下来：坏板子（角色名写错 / 屠边没村民 / 神职全无）
        # 一路放行的话，要么第一天就误判狼胜，要么夜里 KeyError: '村民' 直接崩掉整局。
        # run_game.py 和 server.py 自己也会先校验一次，所以正常路径不受影响。
        issues = validate_board(self.board_def)
        if issues:
            raise ValueError("；".join(issues))
        keys = list(self.board_def.roles)        # ⚠️ `seed if seed else None` 会把 seed=0 当成"没传种子"（0 是 falsy），
        # 而网页端的默认种子恰好就是 0 → 想复现同一局永远复现不了。
        # ⚠️ 2026-10-30：seed 传进来可能是数字字符串（手机报文 JSON / 手改配置）。
        #    先归一成 int；认不出来就当"没给种子"，别让一局在建局阶段就崩掉。
        if isinstance(seed, str):
            _s = seed.strip()
            seed = int(_s) if _s.lstrip("+-").isdigit() else None
        self.rng = random.Random(seed if seed is not None else None)
        # ⚠️ 退避抖动要有**自己的**随机源：用 `self.rng` 会偷走游戏随机流的一次抽签，
        #    同一个种子在"发生过重试"和"没发生"时，后面那些"按规则随机兜底"的选择就会不一样
        #    （复盘时看起来像 bug）。这里另起一条流，种子仍由本局种子决定 → 既有抖动、又可复现。
        # ⚠️ 2026-10-30：seed 传进来可能是字符串（手机报文/配置手改）。以前 int() 直接抛
        #    ValueError，建局就崩；现在认不出数字就当"没给种子"（随机发牌），不阻断开局。
        try:
            _jitter_seed = int(seed) if seed is not None else None
        except (TypeError, ValueError):
            _jitter_seed = None
        self._jitter_rng = random.Random(((_jitter_seed * 2654435761) & 0xFFFFFFFF)
                                         if _jitter_seed is not None else None)
        self.rng.shuffle(keys)
        self.board = self.board_def.name
        self.win_rule = self.board_def.win_rule
        #: 2026-10-31 联机：真人可以坐**多个**座位。`human_seat` 保留为旧入口（单人局），
        #: `human_seats` 是联机局里实际被玩家占用的座位集合。
        #: ⚠️ 引擎里所有 `is_human` 分支（跳过决策层、投票单独问、不做自动兜底）都靠它。
        if human_seats is None:
            self.human_seats = {int(human_seat)} if int(human_seat or 0) > 0 else set()
        else:
            try:
                self.human_seats = {int(x) for x in human_seats if int(x or 0) > 0}
            except (TypeError, ValueError):
                self.human_seats = {int(human_seat)} if int(human_seat or 0) > 0 else set()
        self.human_seat = int(human_seat or 0)
        #: **首夜无刀**（板子级开关，作者 2026-10-10 定）：第 1 夜狼队/女巫/摄梦人全部零调用，
        #: 只跑一次预言家验人；女巫与摄梦人从第 2 夜起才行动。见 `night()`。
        self.first_night_quiet = bool(getattr(self.board_def, "first_night_quiet", False))
        #: **出局亮身份**（板子级开关，作者 2026-10-30 定；只有「6 人屠城明牌」开着）：
        #: 任何人出局时公开他的身份牌，死因照旧不公示。见 `kill()` / `dead_list()`。
        self.reveal_on_death = bool(getattr(self.board_def, "reveal_on_death", False))
        self.seats: list[Seat] = [
            Seat(id=i + 1, role_key=keys[i], is_human=((i + 1) in self.human_seats))
            for i in range(len(keys))
        ]
        self.on_event = on_event or (lambda kind, **data: None)
        #: 单次模型调用的硬上限（秒）。客户端自己的读超时管不住"上游挂住不回包"，
        #: 这一层到点就当这一手失败，交给重试 / 暂停弹窗（见 act.one_call）。
        self.call_timeout = float(call_timeout or 0)
        #: 一整手（含重试）的总时间预算（秒）。超过就不再重试、直接把决定权交给用户 ——
        #: 否则"每次 300 秒 × 5 次"会让手机端看起来像死机。
        self.decision_timeout = float(decision_timeout or 0)
        #: 所有座位共用的两段文本（见 wolf/tabletalk.py）：
        #:   通用玩家提示词（"你是一名专业的狼人杀玩家…"）
        #:   位置概率参考（发牌概率 + 使用纪律；已并进《概念库》的进包段落）
        #: 它们取代了按座位的角色卡 —— 用户实测"人设把模型思路固定死了"。
        self.player_prompt = str(player_prompt or "")
        self.meta_text = str(meta_text or "")
        #: 边界口径：同一夜里"屠边完成"与"最后一狼被抓出来"同时成立时算谁赢。
        #:   sides_first（默认，用户定的口径）：屠边优先 → 狼胜
        #:   last_wolf_first：最后一狼先死 → 好人胜
        self.edge_order = str(edge_order or "sides_first").strip().lower()
        #: 打法书（`打法书.md`）：按角色分发 —— 每个座位只拿到"通用 + 自己那个角色"的章节。
        #: 过滤在 context.build 里做（这里存全文，标记保留）。
        self.playbook_text = str(playbook_text or "")
        #: 引擎层自动重试：`auto_retries` 次**额外**尝试（合计 auto_retries + 1 次），
        #: 退避取 `retry_backoff`（默认 3s → 8s，带 ±retry_jitter 抖动），全程受整手预算约束。
        #: 用户口径（2026-10-07）：合计最多 3 次尝试，3 次都失败才弹窗问人。
        self.auto_retries = max(0, int(auto_retries or 0))
        self.retry_backoff = _backoff_list(retry_backoff)
        self.retry_jitter = float(retry_jitter if retry_jitter is not None else 0.3)
        #: 换家之后要不要把替补的**思路摘要**写进该座位私有经历（下一手原模型能看到）。
        #: 不给摘要的话原模型只看到"记录 + 理由"，换手等于断片（用户要的是给）。
        self.carry_reasoning = bool(carry_reasoning)
        self.carry_reasoning_chars = max(0, int(carry_reasoning_chars or 0))
        # 台账每次新增都会变成一个事件 —— 手机端就是靠这个实时刷新的
        self.audit: list[dict] = []
        self.faults: list[dict] = []
        self.ledger = Ledger(on_add=lambda entry: self.emit("ledger", entry=entry))
        self.day = 0
        self.phase = "init"
        self.winner: str | None = None
        self.aborted = False
        #: 这一局的代号（事件带上它，服务端据此丢掉"上一局还在发"的事件）。
        self.run_token = f"g{next(_RUN_SEQ)}"
        #: **代际**（2026-10-30 加）：每 `supersede()`/`abort()` 一次 +1。
        #: `act()` 在发请求前记住当时的代号，回来先核对 —— 不是那一代的结果**一律丢弃**，
        #: 不写台账、不参与流程。防的就是"放弃/重开/硬复位之后，旧任务带着迟到回复继续往下走"。
        self.generation = 0
        self.max_days = max_days
        #: 养鸦人（本项目民牌）：今天被诅咒的人 —— 放逐投票里额外多一票（每天重算）
        self.cursed_today: int | None = None
        #: 守墓人（本项目民牌）：上一个白天**真正被放逐**的人（平票/无人出局/愚者翻牌 → None）
        self.last_exiled: int | None = None
        self.witch_heal = True
        self.witch_poison = True
        #: 平票时让被票的两人再发一轮言、然后在她们之间重投（PK）
        self.pk_on_tie = bool(pk_on_tie)
        #: 白天「决策在前、发言在后」（用户 2026-10-07 定的口径）：
        #:   开 → 每个模型座位先**私下**用高思考定下今天投谁（并发一轮），然后按这个决定发言，
        #:        投票直接复用该决定（不再多花一次高思考调用）。
        #:       好处：① 发言与投票一致（以前模型读到自己刚说的话，决策被自己带跑）；
        #:             ② 决策不被自己的发言影响；③ 发言可以真正降档（说话不再兼任思考）。
        #:       代价：**本轮别人的发言不会改变它的票**（决定在发言之前就定了）。
        #:   关 → 老流程：先全员发言，再各自决策投票（能听完全场再投，但发言与投票可能打架）。
        self.decision_first = bool(decision_first)
        #: 本轮的私密决策：{座位号: {"target": int|None, "reason": str}}
        #: ⚠️ **不是**"每轮开始清空"：这两个 dict 只在对应阶段被**整表覆盖**，
        #:    已出局座位上一轮的条目会留到存档里（复盘读的是快照，别当成"本轮决策"）。
        self.day_decisions: dict[int, dict] = {}
        #: 每个座位的「决策+发言」调用结果（用户要求：每次发言前各调一次，不设次数上限）
        self.speech_decisions: dict[int, dict] = {}
        #: 本轮"票面写了但没算"的座位（死人 / 翻牌愚者 / 不存在的号）——
        #: `vote_summary` 用它把「无效票」和「弃权」分开写（2026-10-31 外部审查 E10）。
        self._invalid_votes: set[int] = set()
        #: 狼队当晚的私密决策（同上，夜里用）。它会被写进存档的 `decisions.wolf`（复盘用）
        self.wolf_decisions: dict[int, dict] = {}
        #: 狼人控场：存活狼数 ≥ 存活好人数，直接判狼胜（否则好人票不出狼，只能被磨死）
        self.wolf_control = bool(wolf_control)
        #: 警长（11 人及以上默认开启；`sheriff=None` = 按人数自动）。
        # 按用户定的规则：第一天**先**决定要不要上警；竞选者各一轮发言、**没有投票权**；
        # 非竞选者可以投票但**不能发言**；全员上警（或无人上警）则警徽流失；
        # 警徽在投票时算 1.5 票；警长出局（被放逐 / 夜里被刀 / 被枪）时可以选择
        # 把警徽传给一名存活玩家，或者撕掉它。
        #: 往日发言压缩：off（默认，全文）/ extract（摘录：身份声明 + 最后一句）
        _cc = str(context_compress or "off").strip().lower()
        self.context_compress = _cc if _cc in ("off", "extract") else "off"
        self._config_warns: list[str] = []
        if _cc not in ("off", "extract"):
            self._config_warns.append(
                f"game.context_compress={context_compress!r} 不认识（只认 off/extract），已按 off 处理")
        for _warn in self._config_warns:          # 以前只写不读：配置拼错静默降级，日志里也没痕迹
            print(f"[config-warn] {_warn}")
        self.sheriff_min_seats = int(sheriff_min_seats)
        self.sheriff_enabled = ((len(self.seats) >= self.sheriff_min_seats)
                                if sheriff is None else bool(sheriff))
        self.sheriff_id: int | None = None
        #: 警长每天可以决定发言方向（顺序 / 逆序）—— 用户 2026-10-08 要求。
        #: `sheriff_picks_order=False` 就退回老口径（永远是"警长下一位先说、警长最后"）。
        self.sheriff_picks_order = bool(sheriff_picks_order)
        self.wolf_talk_skip_when_unanimous = bool(wolf_talk_skip_when_unanimous)
        self.seer_auto_run = bool(seer_auto_run)
        #: 今天的方向（由 `sheriff_pick_order()` 每天重设；没有警徽时它没意义）
        self.speech_reverse = False
        #: 狼人自爆（用户 2026-10-08 加的变版规则）：`False` 就完全不开这个窗口。
        #: `exploded_today` = 今天自爆的狼的座位号（没爆就是 None）——`day_phase` 靠它
        #: 判断"今天到此为止、直接进夜"。
        #: 警长竞选是否正在进行（自爆要据此判断"这次爆会不会吞掉警徽"）
        self._election_open = False
        self.wolf_explode = bool(wolf_explode)
        self.exploded_today: int | None = None
        #: 2026-11-03（11 人板专属新规）：单狼自爆**不吞警徽**，竞选挂起；
        #: 第二天白天先问狼队"要不要再来一次"，双爆才吞警徽。
        #: 结构：{"day": 1, "candidates": [...], "front": [...], "back": [...], "spoken": [...]}
        self._badge_suspend: dict | None = None
        #: 上游连续失败时的"暂停并问用户"回调：`async (seat, task, error, stalled) -> "retry"|"skip"|"abort"`。
        #: 没传（命令行/mock/工具脚本）时保持老行为：记一笔警告，按规则随机处理，继续打。
        self.on_stall = on_stall
        #: 规则书放在哪儿：packet（塞进每个座位的提示词）/ room（开局进公示栏）/ both / off。
        #: 只有 packet / both 会让 context.build 往包里加规则块；room 那条由调用方（server/run_game）
        #: 在开局时写进公开台账 —— 引擎这里只需要知道"包里要不要加"。
        self.rules_mode = str(rules_mode or "off").lower()
        self.rules_detail = str(rules_detail or "brief").lower()
        self.rules_in_packet = self.rules_mode in ("packet", "both")
        #: **按本局板子组装**的规则文本（只含本局真的有的角色 + 有没有警长）。
        #: 用户的要求："这局玩什么板子就发什么板子的规则集" —— 不要把守卫/白狼王这类
        #: 本局根本没有的角色规则也塞进来（费 token，还摊薄注意力）。
        #: 组装失败（规则书缺某个角色的片段）就直接抛错：宁可开局失败，也不要按错的规则打。
        self.rules_text = rules.compose(self.board_def.roles,
                                        sheriff=self.sheriff_enabled,
                                        detail=self.rules_detail,
                                        first_night_quiet=self.first_night_quiet,
                                        # 出局亮身份同样是板子级条件片段：暗牌板子不许发
                                        reveal_on_death=self.reveal_on_death,
                                        # 胜负口径也是板子级（屠边 / 屠城）：发错就是把规则写反
                                        win_rule=self.win_rule,
                                        # 锁定上警是**开局选项**（不是板子属性）：关掉时
                                        # 规则里那句「不问你」必须一起消失，否则模型会以为自己没被问
                                        seer_auto_run=self.seer_auto_run) or ""
        self.agent_factory = agent_factory
        #: 每次模型调用的完整留痕（复盘用）：看到了什么、回了什么、推理链、用量、耗时
        self.transcript: list[dict] = []
        self.started_at = time.time()

    # ------------------------------------------------------------------ #
    # 基础查询
    # ------------------------------------------------------------------ #

    def emit(self, event: str, **data) -> None:
        try:
            # 每条事件都带"这一局的代号"：服务端据此丢掉上一局残留任务发来的事件。
            self.on_event(event, _run=self.run_token, **data)
        except Exception as exc:                 # 日志回调不许影响对局
            # ⚠️ 2026-10-31 外部审查 E12：以前这里裸 `pass` —— 推送回调一抛异常（断线、
            #    data 不可序列化），这条事件就永久丢了，而服务端与前端从此状态不同步，
            #    日志里一个字都没有。现在至少留一行，能看出"是哪条事件没送到"。
            print(f"[engine] 事件 {event!r} 推送失败（对局继续）：{type(exc).__name__}: {exc}")

    def seat_by_id(self, seat_id: int) -> Seat:
        return self.seats[seat_id - 1]

    @property
    def alive(self) -> list[Seat]:
        return [s for s in self.seats if s.alive]

    def alive_ids(self) -> list[int]:
        return [s.id for s in self.alive]

    def dead_list(self) -> list[tuple[int, str]]:
        """公开的死讯：只说谁在第几天出局，不说死因。

        ⚠️ 「出局亮身份」的板子（`reveal_on_death`）要在这里把他的**身份牌**也写上 ——
        这是公开信息（`kill()` 里已经发过公告），而且【已出局】那一行是每个包都会带的状态摘要，
        少了它模型会以为"只知道他死了、不知道他是什么"。
        """
        out: list[tuple[int, str]] = []
        for s in self.seats:
            if s.alive:
                continue
            why = f"第 {s.death_day} 天出局"
            if self.reveal_on_death:
                # 用「·」而不是再套一层括号：ledger 渲染出来是「3号（第 2 天出局·村民）」
                why += f"·{role_name(s.role_key)}"
            out.append((s.id, why))
        return out

    def agent_for(self, seat: Seat):
        if seat.agent is None:
            seat.agent = self.agent_factory(seat)
        return seat.agent

    # ------------------------------------------------------------------ #
    # 一次决策
    # ------------------------------------------------------------------ #

    def coerce_seat(self, value, legal: list[int], default=_NO_DEFAULT) -> int | None:
        """把模型给的任意输入压成合法座位号；压不出来就走降级。

        - 不传 default：压不出来就在 legal 里随机挑一个（狼刀/验人/守人/投票要有人）
        - 传 default=None：压不出来就返回 None，表示"什么都不做"（猎人不开枪、女巫不用毒）
        - 传具体值：压不出来就返回那个值

        ⚠️ NaN / Infinity 必须在这里拦住：json.loads 默认接受它们，
        而 int(nan) 会抛 ValueError 直接崩掉整局（实测过）。
        """
        if isinstance(value, bool):
            value = None
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        if isinstance(value, (int, float)):
            num = int(value)
        else:
            # ⚠️ 2026-10-30：允许负号后再取整，并且 ≤0 一律当"压不出来" ——
            #    以前用 `\d+`，模型写 "-3" 会被抠成 3，等于"投/刀/守 3 号"。
            m = re.search(r"-?\d+", str(value or ""))
            num = int(m.group()) if m else None
        if num is not None and num <= 0:
            num = None
        if num in legal:
            return num
        if default is not _NO_DEFAULT:
            return default
        return self.rng.choice(legal) if legal else None

    def _retry_wait(self, attempt: int, deadline: float) -> float | None:
        """第 `attempt` 次自动重试前等多久（秒）。返回 None = 预算不够，干脆别重试了。

        退避序列来自 config 的 `retry_backoff`（默认 [3, 8]），带 ±`retry_jitter` 抖动
        （避免多座位在同一秒一起重试撞上游）。用**独立的** `self._jitter_rng` 而不是
        `self.rng`：游戏随机流不能被动用，否则同种子对局会因"有没有重试"而分叉。
        """
        seq = list(getattr(self, "retry_backoff", None) or [3.0, 8.0])
        base = float(seq[min(attempt - 1, len(seq) - 1)]) if seq else 3.0
        jitter = float(getattr(self, "retry_jitter", 0.3) or 0.0)
        wait = base
        if jitter > 0:
            # 用**独立的**抖动随机源（见 __init__ 里的 _jitter_rng）：不能吃游戏的随机流
            wait = base * (1.0 + self._jitter_rng.uniform(-jitter, jitter))
        wait = max(0.0, wait)
        if deadline:
            left = deadline - time.time()
            if left <= 1.0:                       # 剩不到 1 秒：重试也白搭
                return None
            wait = min(wait, max(0.0, left - 0.5))
        return wait

    async def act(self, seat: Seat, task: Task) -> dict:
        if self.aborted:                          # 放弃本局：不再往下走
            raise GameAborted("对局已放弃")
        #: 这一手的"代际"。发请求前记下；回来时如果不是这一代（放弃/重开/硬复位），
        #: 结果一律丢弃 —— 见 `supersede()`。这是"脏回复"的唯一入口，所以只在这里判。
        gen = self.generation

        def _stale() -> bool:
            return self.aborted or self.generation != gen

        packet = context.build(self, seat, task)
        self.emit("packet", seat=seat.id, task=task.kind, packet=packet)
        agent = self.agent_for(seat)
        setter = getattr(agent, "set_context", None)
        if setter is not None:                    # 告诉代理"现在第几天"，它据此选思考档
            try:
                setter(self.day, task.kind)
            except Exception:                     # noqa: BLE001
                pass
        deadline = (time.time() + float(self.decision_timeout)
                    if getattr(self, "decision_timeout", 0) else 0.0)
        #: 最近一次尝试的元信息（模型名/思考链/换家信息）。`one_call` 每跑一次就刷新它 ——
        #: 换手记录要用到"最后是哪家答的、它的思考链是什么"（见本函数末尾）。
        last_meta: dict = {}

        async def one_call() -> dict:
            """真正发一次请求，并把这一笔记进 transcript（重试的每一次都留痕）。

            ⚠️ 这一层再套一个**单次硬上限** `call_timeout`：客户端自己的 timeout 是"读超时"，
            但上游挂住不给任何字节时（实测过：进程 CPU 完全不涨、连接一直 ESTABLISHED），
            手机端看到的就只是"X号 正在思考…"一动不动。
            硬上限到了就把这一手当失败交给上面的重试/暂停链路，别让整局无声地卡住。

            ⚠️ **只对"要调模型的座位"生效**（`uses_model`）：真人座位有自己的
            `human_timeout`（默认 900 秒），第一版把上限一律套上，等于把真人思考时间砍到
            `call_timeout` 秒 —— 超了还会被判成"调用失败、按随机处理"，并且往你的座位上写一条
            "系统提示：你这次决策调用失败"。这跟"等真人多久"的配置直接打架。

            ⚠️ 上限的意义是"**最多让它想这么久**"，不是为了早砍：用户口径是别砍在思考中途
            （砍了它得从头重想，更亏），所以上限放到 300 秒，而且**重试只拿剩余预算**。
            """
            started = time.time()
            uses_model = bool(getattr(agent, "uses_model", False))
            # 这一手还能花多久：**重试只拿剩余预算**。否则第一次想满 300 秒失败之后，
            # 重试又能再想 300 秒 —— 用户要的是"最多 300 秒"，不能让重试把这个账翻倍。
            budget = 0.0
            if uses_model and self.call_timeout and self.call_timeout > 0:
                budget = float(self.call_timeout)
                if deadline:
                    budget = min(budget, max(0.0, deadline - time.time()))
            try:
                if budget > 0:
                    got = await asyncio.wait_for(agent.ask(packet, task), timeout=budget)
                elif uses_model and self.call_timeout and self.call_timeout > 0 and deadline:
                    # 预算已经用完：不再发请求，直接算失败（避免"砍了又从头来一遍"）
                    got = {"_error": f"这一手的总预算（{_secs(self.decision_timeout)} 秒）已经用完"}
                else:
                    got = await agent.ask(packet, task)
            except asyncio.TimeoutError:
                if budget >= float(self.call_timeout or 0) - 0.01:
                    got = {"_error": f"单次调用超过 {_secs(self.call_timeout)} 秒"
                                     f"（上游一直没有响应，已按上限判失败）"}
                else:
                    got = {"_error": f"这一手剩下的 {_secs(budget)} 秒也等完了"
                                     f"（上游一直没有响应）"}
            except Exception as exc:              # noqa: BLE001
                got = {"_error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(got, dict):
                got = {}
            meta: dict = {}
            try:
                meta = agent.last_meta() or {}
            except Exception:                     # noqa: BLE001
                meta = {}
            last_meta.clear()
            last_meta.update(meta)
            row = {
                "day": self.day,
                "phase": self.phase,
                "seat": seat.id,
                "role": seat.role_key,
                "task": task.kind,
                "model": meta.get("model", ""),
                # 这一手是不是"换家"打的（复盘/统计用：否则存档里只能看到模型名变过）
                "fallback": str(meta.get("fallback_to") or ""),
                # 托管发言会用 context.build 重拼一份 packet；它通过 meta.packet 交回来，
                # 复盘里应该看到**真正发给模型的那份**（2026-11-03 审查 P2）
                "packet": meta.get("packet") or packet,
                "reply": got,
                "raw": meta.get("raw", ""),
                "reasoning": meta.get("reasoning", ""),      # 思考链（模型有返回才存）
                "thinking": meta.get("thinking", {}),        # 这次用的思考档位
                # 人类座位被"托管"代打时，记一笔 —— 复盘/对局统计里能看出来这不是真人写的
                "butler": bool(meta.get("butler")),
                "prompt_tokens": meta.get("prompt_tokens", 0),
                "completion_tokens": meta.get("completion_tokens", 0),
                # 命中平台侧缓存的输入 token（平台没返回就是 0）—— 用来回答"前缀缓存到底省没省"
                "cached_tokens": meta.get("cached_tokens", 0),
                # 有 meta.elapsed（模型/托管给了真实调用耗时）就用它；没有才用"这一手的总耗时"
                "elapsed": round(float(meta.get("elapsed") or (time.time() - started)), 2),
                "error": got.get("_error", ""),
            }
            if _stale():
                # 这一手确实发出去过（也花了钱）：留一条痕给审计，但**绝不交给引擎**。
                # 不写台账、不选目标、不推进流程 —— 否则就是"上一局的回复打进新局"。
                row["stale"] = True
                row["error"] = "这一手回来时对局已换代（放弃/重开），结果已丢弃"
                self.transcript.append(row)
                raise GameAborted(row["error"])
            self.transcript.append(row)
            return got

        reply = await one_call()
        if _stale():
            raise GameAborted("这一手在途期间对局已作废，结果已丢弃")

        # --- 引擎层的自动重试（2026-10-07 用户定的口径：合计最多 3 次尝试）---
        # 为什么放在引擎层而不是客户端里：
        #   · 客户端只知道"这一次请求"，不知道整手还剩多少预算；
        #   · 客户端内部重试时手机端**看不到任何事件**（只能干等）；
        #   · 客户端那边已经把"挂住（读超时）"排除在重试之外了（挂住再撞同一家没意义）。
        # 这一层的退避是**可见的**（发 `retry` 事件给手机），而且只花剩余预算。
        auto_total = max(0, int(getattr(self, "auto_retries", 0) or 0))
        for attempt in range(1, auto_total + 1):
            if "_error" not in reply or self.aborted:
                break
            if not getattr(agent, "uses_model", False):
                break                             # 真人/mock 的失败不是上游问题
            # ⚠️ 2026-11-03（作者反馈"跑飞"）：第一次烧满单次上限（默认 150s）之后，
            #    整手只剩一点点预算，硬试第二次几乎必失败，纯属让用户多干等几分钟。
            #    剩余预算不够"完整再试一次"（少于单次上限的一半、且不足 60s）时，
            #    直接不再重试，落到下面的"问用户"链路。
            _cap = float(self.call_timeout or 0)
            if deadline and _cap > 0:
                left = deadline - time.time()
                # "够不够再试一次"的判据：至少要能覆盖一次**有意义的**等待 ——
                # 取 min(单次上限, 30 秒)，再用 5 秒做地板（单次上限很小的测试场景也要能重试）。
                if left < max(5.0, min(_cap, 30.0)):
                    self.emit("warn", seat=seat.id, task_kind=task.kind,
                              message=f"只剩 {left:.0f} 秒，不够再完整试一次"
                                      f"（单次上限 {_secs(_cap)} 秒）→ 不再自动重试，直接问你")
                    break
            wait = self._retry_wait(attempt, deadline)
            if wait is None:
                self.emit("warn", seat=seat.id, task_kind=task.kind,
                          message=f"整手预算（{_secs(self.decision_timeout)} 秒）快用完了，"
                                  f"不再自动重试")
                break
            # 换家顶上（用户 2026-10-07 定的）：同一家再撞基本还是挂，换一家才有意义。
            # 代理有没有这个能力由它自己决定（`use_fallback`），引擎只做鸭子调用。
            switch = getattr(agent, "use_fallback", None)
            switched = ""
            if callable(switch):
                try:
                    switched = str(switch() or "")
                except Exception as exc:          # noqa: BLE001
                    self.emit("warn", seat=seat.id, task_kind=task.kind,
                              message=f"换家失败：{type(exc).__name__}: {exc}")
            self.emit("retry", seat=seat.id, task_kind=task.kind, attempt=attempt + 1, total=auto_total + 1,
                      wait=round(wait, 1), why=str(reply.get("_error"))[:160],
                      switched_to=switched, model=str(getattr(agent, "model_now", "") or ""))
            if wait > 0:
                await asyncio.sleep(wait)
                if _stale():
                    raise GameAborted("退避期间对局已作废，不再重试")
            reply = await one_call()

        # --- 自动重试用完还是失败 → **暂停整局**问用户 ---
        # 用户的要求："上游有时波动会请求失败；自动重试（引擎层 3 次尝试）还失败就暂停游戏弹窗提示我"。
        # ⚠️ 别再写死"4 次"：客户端层 5 次 + 引擎层 3 次 = 最坏 15 个请求，口径见交接 §1.3。
        # 只有"真的要调模型"的座位才会走到这里（人类/mock 座位的失败不是上游问题），
        # 所以这里用 `uses_model` 做判断，不用 isinstance（engine 不能反向 import agents）。
        stalled = 0
        while ("_error" in reply and self.on_stall is not None
               and getattr(agent, "uses_model", False) and not self.aborted):
            stalled += 1
            # 整手（含重试）的总时间预算：超过就别再重试了，直接问用户 ——
            # 否则"每次 420 秒 × 多次"会让手机端看起来像死机（实测踩过）。
            if deadline and time.time() > deadline:
                self.emit("warn", seat=seat.id, task_kind=task.kind,
                          message=f"这一手已经花了超过 {_secs(self.decision_timeout)} 秒，不再重试")
                break
            decision = await self.on_stall(seat, task, str(reply["_error"]), stalled)
            if _stale():
                raise GameAborted("等待用户决定期间对局已作废")
            if decision == "abort":
                self.abort("模型连续失败，玩家选择放弃本局")
                raise GameAborted("模型连续失败")
            if decision != "retry":
                break                             # skip：落到下面"按规则随机处理"
            # 用户手动点了重试：同样换家顶上（换家策略与自动重试一致）
            switch = getattr(agent, "use_fallback", None)
            if callable(switch):
                try:
                    switch()
                except Exception as exc:          # noqa: BLE001
                    self.emit("warn", seat=seat.id, task_kind=task.kind,
                              message=f"换家失败：{type(exc).__name__}: {exc}")
            reply = await one_call()

        if _stale():
            raise GameAborted("这一手在途期间对局已作废，结果已丢弃")

        # 换手记录：这一手是替补模型打的 → 把它的**思路摘要**留给这个座位，
        # 下一手原模型就能看到"上一手是谁替我打的、它当时怎么想的"（用户 2026-10-07 要的）。
        # 不给摘要的话，原模型只能看到记录和理由，看不到推演过程 —— 换手会断片。
        if str(last_meta.get("fallback_to") or "") and "_error" not in reply:
            # ⚠️ 只有**替补真的答出来了**才写这条。第一版不看这个：替补也失败时照样写
            #    "由 DEEPSEEK 代打"，于是这个座位下一手会以为"上一手是 DEEPSEEK 打的"，
            #    而实际上那一手根本没打出来（系统按规则随机处理了）—— 假记录比没记录更糟。
            note = f"（换手记录）这一手原本由 {last_meta.get('fallback_from') or '原模型'} 负责，" \
                   f"上游失败后由 **{last_meta['fallback_to']}** 代打"
            if self.carry_reasoning:
                why = str(last_meta.get("reasoning") or "").strip()
                if why:
                    keep = max(0, int(self.carry_reasoning_chars or 0))
                    note += (f"；它当时的思路摘要：{why[:keep]}"
                             + ("…（略）" if keep and len(why) > keep else ""))
                else:
                    note += "；它没有返回思考链"
            if reply.get("_still_missing"):
                miss = "、".join(str(x) for x in reply["_still_missing"])
                note += f"（但它的回复缺了必需字段：{miss}，那一手由系统按规则补了默认动作）"
            seat.private_events.append(note)
            self.emit("warn", seat=seat.id, task_kind=task.kind,
                      message=f"{seat.id}号 已换 {last_meta['fallback_to']} 代打这一手")

        if "_error" in reply:
            seat.private_events.append(f"（系统提示）你这次决策调用失败，已按规则随机处理：{reply['_error']}")
            self._fault(seat, task, str(reply["_error"]))
            self.emit("warn", seat=seat.id, task_kind=task.kind, message=str(reply["_error"]))
        # ⚠️ 缺必需字段（模型回了合法 JSON 但字段名写错，两次都没补上）也要留痕：
        #    以前只有 review 的 transcript 里能看到 `_still_missing`，引擎当正常回复处理 →
        #    这个座位按随机行动出票/验人，**下一回合的上下文里没有任何线索**说那一手不是它选的。
        if reply.get("_still_missing"):
            miss = "、".join(str(x) for x in reply["_still_missing"])
            seat.private_events.append(
                f"（系统提示）你上次回复缺了必需字段（{miss}），系统按规则补了默认动作，"
                f"那一手不是你选的。")
            self._fault(seat, task, f"缺字段：{miss}")
            self.emit("warn", seat=seat.id, task_kind=task.kind, message=f"缺字段：{miss}")
        if _stale():
            raise GameAborted("对局已放弃" if self.aborted else "这一手已作废（对局换代）")
        return reply

    def _fault(self, seat, task, reason: str) -> None:
        """座位级"这一步没做成"：界面挂 ⚠、复盘可查（借 dsh-game 的 fault 角标）。"""
        self.faults.append({
            "day": self.day, "phase": self.phase, "seat": seat.id,
            "task": task.kind, "reason": str(reason)[:300],
        })
        self.emit("fault", seat=seat.id, task_kind=task.kind,
                  reason=str(reason)[:300], night=(self.phase == "night"))

    # ------------------------------------------------------------------ #
    # 夜晚
    # ------------------------------------------------------------------ #

    async def decide_wolf_round(self, day: int, wolves: list[Seat],
                                targets: list[int]) -> dict[int, dict]:
        """狼队的**私密决策轮**（高思考，全员并发）：先各自定下今晚刀谁，再去讨论。

        为什么（用户 2026-10-07 追加：「狼队夜里发言讨论战术也是决策层在发言层之前」）：
        跟白天同一个道理 —— 先发言后决策时，狼会读到自己刚说的话，决策被自己的发言带跑；
        而且讨论阶段其实已经在「想刀谁」，等于把决策拆成两次调用。现在决策在前：
        讨论是「把已定的刀口说清楚 + 争取队友跟票」，出刀直接复用决定。
        """
        decided: dict[int, dict] = {}

        async def one_decide(wolf: Seat) -> tuple[Seat, dict]:
            task = Task(
                kind="wolf_intent",
                text=(f"第 {day} 天夜晚，狼人行动。**还没开始讨论**，先做一次完整分析"
                      f"（只有你自己看得到）：\n"
                      f"1) 逐个点评场上活着的玩家（{ '、'.join(f'{i}号' for i in targets) }）："
                      f"谁像神职、谁像村民、谁最该今晚被刀（刀谁收益最大 / 最能掩盖你们身份）？\n"
                      f"2) 定下你今晚要刀谁。\n"
                      f"⚠️ 目标里**包含你们自己人（狼）**：自刀或刀队友是标准战术 ——"
                      f"做身份、骗女巫的解药、抢轮次都可能用得上；用不用、用在哪，由你判断。"),
                schema=('{"reads": [{"seat": <编号>, "role": "<猜的身份>", "why": "<一句>"}], '
                        '"summary": "<两三句局势判断>", '
                        '"target": <你要刀的玩家编号>, "reason": "<一两句真实理由>"}'),
                legal=targets, required=("target",),
                #: `good` = 只给代理看的"好人名单"：**随机代理（MockAgent）用它避开自刀**
                #: （自刀是刻意战术，不该是随机默认值）。真人与模型不受影响 ——
                #: 它们看的是 legal，里面照样有狼、能点自己人。
                #: ⚠️ 这里必须**自己算**：它是 `decide_wolf_round` 的局部量，
                #:    早先写成 `night()` 里的 `good_alive` → NameError → 被下面那句
                #:    `gather(..., return_exceptions=True)` 静默吞掉，表现为"狼队这轮没决策"。
                extra={"good": [s.id for s in self.alive if s.camp != WOLF_CAMP]},
            )
            return wolf, await self.act(wolf, task)

        gathered = await asyncio.gather(*(one_decide(w) for w in wolves
                                          if not w.is_human), return_exceptions=True)
        for item in gathered:
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                # ⚠️ 别静默（2026-10-08 加）：这里以前对任何异常都 `continue` ——
                #    引擎自己写错个变量名（实测 NameError）会表现成"狼队这轮没决策"，
                #    整局照打、日志里一个字都没有，靠回归才抓出来。打一行给人看。
                print(f"[engine] 狼队决策轮有一头狼失败：{type(item).__name__}: {item}")
                continue
            wolf, reply = item
            target = self.coerce_seat(reply.get("target"), targets, default=None)
            decided[wolf.id] = {"target": target,
                                "reason": str(reply.get("reason") or "").strip()[:200]}
            summary = str(reply.get("summary") or "").strip()
            if summary:
                wolf.mind = self._append_mind(wolf.mind, f"第 {day} 夜狼队判断：{summary[:160]}")
        self.wolf_decisions = decided
        return decided

    async def _seer_check(self, day: int) -> None:
        """预言家的那一夜验人（**每一次夜晚都会跑**，含"首夜无刀"的板子）。

        为什么单独抽出来：`eleven_wolfking`（首夜无刀）的第 1 夜**只跑这一次验人**，
        其余夜间角色全部跳过 —— 两条路径必须共用同一段代码，
        否则"短路那一夜"和"平常的夜"验人行为会悄悄跑偏（改了其中一处另一处不动）。
        """
        seer = next((s for s in self.alive if s.role_key == "seer"), None)
        if not seer:
            return
        unchecked = [i for i in self.alive_ids() if i not in seer.checked and i != seer.id]
        legal = unchecked or [i for i in self.alive_ids() if i != seer.id]
        # ⚠️ 2026-10-30：只剩预言家一个人活着时，上面两条都为空 → coerce 会返回 None，
        #    下一行 `seat_by_id(None)` 直接 TypeError 崩局。空名单 = 今晚不验，写一句私有说明。
        if not legal:
            seer.private_events.append(f"第 {day} 夜：场上没有可查验的人，今晚没有验人。")
            return
        task = Task(kind="seer_check",
                    text=("夜晚，你使用预言家技能查验一名玩家的阵营。"
                          "顺便用一句话总结你现在对场上的判断（谁像狼、你的验人顺序怎么排），"
                          "这部分别人看不到。"),
                    schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", '
                            '"why": "<一句>"}], "summary": "<一句判断>", '
                            '"target": <玩家编号>, "reason": "<一句话>"}'),
                    legal=legal, required=("target",))
        reply = await self.act(seer, task)
        if truthy(reply.get("abstain"), default=False):
            seer.private_events.append(f"第 {day} 夜：你放弃了查验（今晚没有查验任何玩家）。")
            return
        # ⚠️ 2026-10-31 外部审查 P1-06：这里以前没传 `default=None` —— 回复写糊/乱码时
        #    `coerce_seat` 会**随机验一个活人**，还会把那个随机结果固化进验人历史。
        #    守卫/摄梦人/幸运儿三条同类路径 10-30 已改成"压不出来就空过"，预言家漏改了。
        target = self.coerce_seat(reply.get("target"), legal, default=None)
        if target is None:
            seer.private_events.append(
                f"第 {day} 夜：你没给出可查验的目标，今晚按「没有验人」处理。")
            return
        verdict = "狼人" if self.seat_by_id(target).camp == WOLF_CAMP else "好人"
        seer.checked[target] = verdict
        seer.private_events.append(f"第 {day} 夜：你查验了 {target}号，结果是【{verdict}】。")
        # 同时记一条**只有这个座位看得到**的台账：人类玩家坐预言家时，
        # 手机界面上只渲染台账（不渲染上下文包），没有这一条他就永远看不到查验结果。
        # （这也让 ledger 里 `seat:N` 这条可见性分支第一次有了生产者。）
        self.ledger.add(day, "night", "private",
                        f"你查验了 {target}号，结果是【{verdict}】。",
                        seat=seer.id, visible_to=(f"seat:{seer.id}",))
        seer.mind = self._append_mind(seer.mind, f"第 {day} 夜查验 {target}号 = {verdict}")

    async def _quiet_first_night(self, day: int) -> list[tuple[int, str]]:
        """**首夜无刀**板子的第 1 夜：整夜只跑一次预言家验人（作者 2026-10-10 定的口径）。

        为什么要有它（作者观察 + 三局真实思维链证据）：现行板子"首夜女巫手刀"把**银水变成铁好**
        —— 狼自刀骗药即使成功也吞不掉两瓶药、反而白送一狼（EV 为负），于是三局里 77/20/6 处
        提到自刀、**全部主动放弃** ⇒ 狼永远走"外置位中心开刀" ⇒ 银水没人质疑。这不是模型不会玩，
        是机制给的 EV 就是负的，所以改机制。

        具体跳过（不漏、也不偷偷跑一个）：
          · **狼队**：连 `wolf_intent` 都不问，更不结算狼刀（今晚没有刀口）；
          · **女巫**：不给药、**也不告诉她刀口**（她连"今晚死了谁"都不该知道 —— 第 2 夜才问）；
          · **摄梦人**：不上手（她也是第 2 夜起才行动）；
          · **幸运儿**：本来就第 2 夜起才问（这里再挡一道，别依赖另一个分支的条件）；
          · **守卫**：本板子没有守卫，但别的自定义板子开这个开关时也不许动。
        留下的唯一一次调用是 `seer_check`。
        """
        day = int(day or 1)
        self.ledger.add(day, "night", "night",
                        "【首夜无刀】本板子第一夜狼队不行动：今晚没有刀口，女巫/摄梦人也不行动"
                        "（都要听完第一天的发言才动手），整夜只有预言家的一次验人。",
                        visible_to=(WOLF,))
        for s in self.seats:
            if not s.alive:
                continue
            if s.camp == WOLF_CAMP:
                s.private_events.append(
                    f"第 {day} 夜：**首夜无刀** —— 本板子第一夜狼队不刀人（今晚没有刀口，"
                    f"也不用讨论）。你们的第一刀从第 2 夜开始，那时女巫也已经听完第一天全场发言。")
        for frm, why in (("witch", "首夜无刀：今晚没有刀口，你的两瓶药都留到第 2 夜（听完第一天发言再决定）"),
                         ("dreamer", "首夜无刀：你从第 2 夜起才需要摄梦，今晚不用动手"),
                         ("lucky", "首夜无刀：你的幸运技能本来就是第 2 夜起才问，今晚没有你的事"),
                         ("guard", "首夜无刀：今晚没有狼刀，你不用守人")):
            seat = next((s for s in self.alive if s.role_key == frm), None)
            if seat is not None:
                # ⚠️ 人类座位在手机上只渲染台账（不渲染上下文包）：只写私有经历的话，
                #    他会看到"整夜没人问过我"而不知道是规则如此 —— 所以台账也要留一条。
                seat.private_events.append(f"第 {day} 夜：{why}。")
                self.ledger.add(day, "night", "private", f"第 {day} 夜：{why}。",
                                seat=seat.id, visible_to=(f"seat:{seat.id}",))
        await self._seer_check(day)
        self.emit("night_result", day=day, victim=None, save=False, poison=None,
                  guarded=None, deaths=[])
        return []

    async def night(self) -> list[tuple[int, str]]:
        day = self.day
        # ⚠️ 首夜无刀（作者 2026-10-10）：整夜只跑预言家一次验人，其余夜间角色零调用。
        #    必须是**这个位置**（在任何狼队决策之前）—— 见 `_quiet_first_night` 的说明。
        if self.first_night_quiet and day == 1:
            return await self._quiet_first_night(day)
        wolves = [s for s in self.alive if s.camp == WOLF_CAMP]
        #: 出刀的**合法目标 = 全体存活玩家**（含狼、含自己）。用户 2026-10-08 拍板：
        #: 「全体存活（队友也能刀，这才有刀队友骗药/做局的空间）」。
        #: 以前 legal 只有好人，于是"自刀"这条标准战术在引擎层根本不存在 ——
        #: 前端面板也不给选（用户实测报「刀人选择里没有狼人，这导致狼人无法自刀」）。
        #: ⚠️ 刀到狼身上是合法结果：刀口落地后按常规判胜负（狼数变少当然可能直接输），
        #:    女巫的解药/毒药、守卫、猎人都照旧按"任意一张牌"处理，这里没有特例。
        kill_targets = [s.id for s in self.alive]
        #: 兜底随机的池子（只用于"回复写糊了/写了个死人"）：**只在好人里挑**，
        #: 免得模型一个笔误就让狼队刀死自己人（见下面 one_proposal 里的两步走说明）。
        good_alive = [s.id for s in self.alive if s.camp != WOLF_CAMP]

        # 决策在前：先把刀口定下来（并发一轮高思考），讨论与出刀都服从它
        wolf_intents: dict[int, dict] = {}
        if self.decision_first and wolves:
            wolf_intents = await self.decide_wolf_round(day, wolves, kill_targets)

        # --- 狼队讨论：**按座位顺序**一个个说（1号 → 5号 → …）---
        # 只剩一头狼时没什么可讨论的，直接跳过讨论去出刀。
        #
        # ⚠️ 2026-10-09 作者批准的**成本折中**：四头狼私下定的刀口**完全一致**时也跳过夜聊 ——
        #    没东西可讨论就不花这几次调用；只要有一头狼想刀别人，就照常开讨论。
        #    （实测每头狼夜间 2 次调用：定刀口 + 夜聊；一致时这一刀能省掉一半。）
        skip_talk = False
        _tgts: set = set()
        if len(wolves) > 1 and self.wolf_talk_skip_when_unanimous:
            _tgts = {wolf_intents.get(w.id, {}).get("target") for w in wolves if w.alive}
            skip_talk = len(_tgts) == 1 and None not in _tgts
        if skip_talk:
            _tgt = next(iter(_tgts))
            # ⚠️ 只写给狼看：写公开等于告诉好人「狼队今晚意见一致」（那是泄漏）。
            self.ledger.add(day, "night", "night",
                            f"狼队今晚刀口一致（都指向 {_tgt}号）→ 跳过夜聊，直接出刀"
                            f"（省 {len(wolves)} 次调用）。",
                            visible_to=(WOLF,))
            for wolf in wolves:
                if wolf.alive:
                    # 2026-10-31 外部审查 E7：文案以前写死"你们四头狼"，狼数≠4（含中途出局）时是假话。
                    wolf.private_events.append(
                        f"第 {day} 夜：你们 {len([w for w in wolves if w.alive])} 头狼的刀口一致"
                        f"（{_tgt}号），今晚没有夜聊环节。")
        if len(wolves) > 1 and not skip_talk:
            self.ledger.add(day, "night", "night",
                            f"狼队讨论开始（按座位顺序，{len(wolves)} 头狼各说一句想杀谁）。",
                            visible_to=(WOLF,))
            for wolf in sorted(wolves, key=lambda s: s.id):
                if not wolf.alive:
                    continue
                pre = wolf_intents.get(wolf.id) or {}
                pre_txt = ""
                if pre.get("target"):
                    why = pre.get("reason") or ""
                    pre_txt = (f"\n\n【重要】你已经私下决定今晚刀 {pre['target']} 号"
                               + (f"（你自己的理由：{why}）" if why else "")
                               + "。发言要与这个决定一致：可以说理由、争取队友跟票，"
                                 "但不要显得你还没想好，也不要提别人。")
                task = Task(
                    kind="wolf_talk",
                    text=(f"现在是第 {day} 天夜晚，狼队讨论。按座位顺序轮到你说话："
                          f"说明你今晚想杀谁、为什么。（这段话队友看得到，好人看不到）"
                          + pre_txt),
                    schema='{"speech": "<你的发言，150 字以内>", "target": <你想杀的编号>}',
                    legal=kill_targets,
                    extra={"good": good_alive},
                    required=("speech",),
                )
                reply = await self.act(wolf, task)
                say = str(reply.get("speech") or "").strip() or "（我这轮没意见，跟队友走。）"
                want = self.coerce_seat(reply.get("target"), kill_targets, default=None)
                self.ledger.add(day, "night", "night",
                                f"{wolf.id}号：{say}" + (f"　→ 倾向杀 {want}号" if want else ""),
                                seat=wolf.id, visible_to=(WOLF,))

        # --- 各自出刀（同时提交，省时间）---
        async def one_proposal(wolf: Seat):
            # 「决策在前」：今晚的刀口早在讨论之前就定了 → 直接用，不再多花一轮调用
            pre = wolf_intents.get(wolf.id) or {}
            if pre.get("target") in kill_targets and not wolf.is_human:
                # ⚠️⚠️ 这里**只能写来路、绝对不许带那句私有 reason**（P3-2，与 run_vote 的
                #    P0 修法一致）：`decide_wolf_round` 的提示词明写「**只有你自己看得到**」，
                #    而这条出刀条目虽然 `visible_to=(WOLF,)`，也是**台账里的原文** ——
                #    队友的包、存档、前端复盘全都读得到它，等于把"私密"那句话
                #    原文转发给全队友并永久留在台账里。
                #    私有理由仍然留在存档的 `decisions.wolf` 与调用留痕里，复盘看得到。
                return wolf, {"target": pre["target"],
                              "reason": "（沿用今晚早先的决定）",
                              "_prefetched": True}
            task = Task(
                kind="wolf_kill",
                text=f"现在是第 {day} 天夜晚，狼人行动。请提出今晚要击杀的目标，"
                     f"并给一句理由（队友都能看到你的提议）。",
                schema='{"target": <玩家编号>, "reason": "<一句话理由>"}',
                legal=kill_targets,
                extra={"good": good_alive},
                required=("target",),
            )
            return wolf, await self.act(wolf, task)

        gathered = await asyncio.gather(
            *(one_proposal(w) for w in wolves), return_exceptions=True)
        proposals: dict[int, int | None] = {}
        for item in gathered:                     # 按返回顺序记台账，日志才稳定
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                continue
            wolf, reply = item
            if truthy(reply.get("abstain"), default=False):
                # 人类说放弃/超时：真的不提议，不再替他随机点一个人（队友能看到他弃权）
                self.ledger.add(day, "night", "night",
                                f"{wolf.id}号 弃权（今晚不提议击杀目标）。",
                                seat=wolf.id, visible_to=(WOLF,))
                continue
            # ⚠️ 两步走（2026-10-08 加，配合"出刀目标含狼"）：
            #   ① 显式写在答案里的目标**一律尊重** —— 只要它是活着的玩家，哪怕是个狼也行
            #      （自刀/刀队友是用户的明确要求：骗解药、做局、抢轮次都用得上）；
            #   ② 只有"写糊了 / 写了个已经死了的人"才走兜底随机 —— 而兜底**只在好人里随机**。
            #      为什么不让兜底也在全体里随机：那等于"模型写错一个数字，狼队就可能
            #      莫名其妙刀死自己人"，属于静默降智（实测：剧本每晚写"刀 9"，9 号死后
            #      兜底随机到了狼警长头上，整局警徽都没了）。
            target = self.coerce_seat(reply.get("target"), kill_targets, default=None)
            if target is None:
                target = self.coerce_seat(reply.get("target"), good_alive)
            proposals[wolf.id] = target
            self.ledger.add(day, "night", "night",
                            f"{wolf.id}号 提议击杀 {target}号：{reply.get('reason') or '（未给理由）'}",
                            seat=wolf.id, visible_to=(WOLF,),
                            data={"prefetched": bool(reply.get("_prefetched"))})
        victim = self._majority(proposals)
        # 把刀口票数摊开给狼队看（跟白天投票汇总一个风格）：谁投了谁 + 计票
        if proposals:
            who = "，".join(f"{k}号→{v}号" for k, v in sorted(proposals.items()) if v is not None)
            counts = Counter(v for v in proposals.values() if v is not None)
            tally = "、".join(f"{k}号 {v} 票" for k, v in counts.most_common())
            self.ledger.add(day, "night", "night",
                            f"狼队出刀汇总：{who or '（都弃权了）'}。　计票：{tally or '无'}。",
                            visible_to=(WOLF,))
        # ⚠️ 全员弃权时 victim 是 None（人类狼超时也会走到这里）。
        # 以前这里照写"击杀 None号"，把唯一事实来源写脏，还让女巫被告知去救 None号、
        # 白扔一瓶解药（审计实测到）。空刀就是空刀，得如实写。
        if victim is None:
            self.ledger.add(day, "night", "night",
                            "狼队今晚没有出刀（全体弃权/超时），算作空刀。", visible_to=(WOLF,))
        else:
            self.ledger.add(day, "night", "night", f"狼队最终决定击杀 {victim}号。",
                            visible_to=(WOLF,))

        # --- 预言家 ---
        await self._seer_check(day)

        # --- 守卫 ---
        guard_target: int | None = None
        guard = next((s for s in self.alive if s.role_key == "guard"), None)
        if guard:
            legal = [i for i in self.alive_ids() if i != guard.last_guard]
            if not legal:
                # 候选为空 = 只剩守卫自己、而昨晚守的正是自己。
                # 不能退回"谁都能守"——那等于允许连守同一人（规则就被绕过去了），今晚空过。
                guard.private_events.append(
                    f"第 {day} 夜：没有可守护的目标（不能连守同一人），你今晚没有行动。")
            else:
                task = Task(kind="guard",
                            text=("夜晚，你使用守卫技能守护一名玩家（不能连续两晚守同一个人，"
                                  "可以守自己）；**也可以选择今晚不守**（`abstain` 填 true，"
                                  "等于这一夜空过）。顺便写一句你对场上的判断（谁像狼、你在防谁），"
                                  "这部分别人看不到。"),
                            schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", '
                                    '"why": "<一句>"}], "summary": "<一句判断>", '
                                    '"abstain": true/false, "target": <玩家编号>, "reason": "<一句话>"}'),
                            # ⚠️ `target` 不能进 required：选了弃守就没有 target。
                            legal=legal, required=())
                reply = await self.act(guard, task)
                # 2026-10-25（作者口径）：**守卫可以放弃守护** —— 这是一个正式选项，
                # 不是死代码（第一版只有代码分支、schema 里没有 abstain，等于没法表达；
                # 现在 schema / 文案 / required 三处都放开了）。
                if truthy(reply.get("abstain"), default=False):
                    guard_target = None
                    # ⚠️ 2026-10-31 外部审查 P1-07：弃守必须清掉"上一晚守过谁"。
                    #    以前不清 —— 守卫「第 1 夜守 X → 第 2 夜空过 → 第 3 夜想守 X」
                    #    会被当成"连守两晚"直接拒绝（候选因此为空时整夜静默不守）。
                    #    摄梦人那两条同类分支（1136/1150）早就是清 `last_dream` 的写法。
                    guard.last_guard = None
                    guard.private_events.append(f"第 {day} 夜：你放弃了守护（今晚没人被守）。")
                    self.ledger.add(day, "night", "private", "你今晚没有守护任何人（空过）。",
                                    seat=guard.id, visible_to=(f"seat:{guard.id}",))
                else:
                    guard_target = self.coerce_seat(reply.get("target"), legal, default=None)
                    if guard_target is None:
                        # 写了目标但压不出来（乱码 / 写了个不合法的人）：如实说"没守成"，
                        # 不替他随机挑一个 —— 守错人是会影响胜负的一手。
                        guard.last_guard = None      # 空过的一夜不算链条的一环（同上）
                        guard.private_events.append(
                            f"第 {day} 夜：你没给出可守护的目标，今晚按「没有守护」处理。")
                    else:
                        guard.last_guard = guard_target
                        guard.private_events.append(f"第 {day} 夜：你守护了 {guard_target}号。")

        # --- 摄梦人（2026-10-09 新增；本项目默认板子用它顶替守卫）---
        # 本项目：每晚可选梦游者（允许空过）；梦游者免疫夜间伤害；
        #         摄梦人夜里死亡不再带走梦游者（2026-10-10 取消连死）；**连续两晚**成为梦游者也会死。
        # 用户附加：**摄梦对象 + 解药 = 奶穿**（照样死，见下面结算）。
        dreamer_target: int | None = None
        dream_prev: int | None = None
        dreamer = next((s for s in self.alive if s.role_key == "dreamer"), None)
        if dreamer:
            dream_prev = dreamer.last_dream          # 上一夜摄的是谁（判"连续两晚"用）
            legal = list(self.alive_ids())          # 可以摄自己；没有"不能连摄"的限制
            # ⚠️ 文案里**必须**带"第 N 夜"与"你上一夜摄的是谁"：
            #    · 连续两晚摄同一人 = 那个人今晚会死 —— 不知道上一夜摄了谁就没法做这个决定；
            #    · 实测（写这个功能时）我自己的回归脚本就是靠"第 N 夜"解析夜次的，
            #      文案里没有夜次时脚本会把第 2 夜当成第 1 夜（假绿/假红的经典来源）。
            _prev = f"你上一夜摄的是 {dream_prev}号（**今夜再摄他，他就会死**）。" if dream_prev else "这是第一夜，你还没有摄过任何人。"
            task = Task(kind="dream",
                        text=(f"第 {day} 夜，你使用摄梦人技能，**可以**选一名玩家成为梦游者"
                              f"（可以摄自己，**也可以选择今晚空过/不摄**）。{_prev}"
                              f"梦游者免疫夜间伤害，但他不知道自己在梦游；"
                              f"你出局**不会**带走他（2026-10-10 起取消「连死」，跟守卫一样）。"
                              f"顺便写一句你对场上的判断（谁像狼、你在防谁），别人看不到。"),
                        schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", '
                                '"why": "<一句>"}], "summary": "<一句判断>", '
                                '"target": <玩家编号>, "reason": "<一句话>"}'),
                        legal=legal, required=("target",))
            reply = await self.act(dreamer, task)
            # ⚠️ 用户改定：**允许摄梦人空过**（官方是"必须选"，本项目改成可以不发动）。
            #    空过时把 last_dream 清掉 —— 否则"空过的那一夜"会被当成链条的一环，
            #    下一夜再摄同一个人会被误判成"连续两晚"。
            if truthy(reply.get("abstain"), default=False):
                dreamer.last_dream = None
                dreamer.private_events.append(f"第 {day} 夜：你今晚没有摄梦（空过）。")
                self.ledger.add(day, "night", "private", "你今晚没有摄梦（空过）。",
                                seat=dreamer.id, visible_to=(f"seat:{dreamer.id}",))
                dreamer_target = None
            else:
                # ⚠️ 2026-10-30 修：**不替他随机挑人**。以前这里没传 default，
                #    回复乱码/调用失败时会随机摄一个活人；万一随机撞上 `last_dream`，
                #    还会被判成"连摄两晚"直接弄死他 —— 与守卫分支（"不随机，按没守成处理"）
                #    是同一类纪律：摄错人会影响胜负。
                dreamer_target = self.coerce_seat(reply.get("target"), legal, default=None)
                if dreamer_target is None:
                    # 没摄成 = 今夜空过：`last_dream` 必须清掉，否则下一夜会被当成
                    # "连续两晚"链条的一环（同上面 abstain 分支的理由）。
                    dreamer.last_dream = None
                    dreamer.private_events.append(
                        f"第 {day} 夜：你没给出可摄的目标，今晚按「没有摄梦」处理。")
                    self.ledger.add(day, "night", "private",
                                    "你今晚没有摄梦（没有给出可用的目标）。",
                                    seat=dreamer.id, visible_to=(f"seat:{dreamer.id}",))
                else:
                    dreamer.last_dream = dreamer_target
                    # 只让他自己知道结果；梦游者本人**不会**收到任何提示（官方：他不知道自己梦游）
                    dreamer.private_events.append(f"第 {day} 夜：你摄梦了 {dreamer_target}号。")
                    self.ledger.add(day, "night", "private",
                                    f"你摄梦了 {dreamer_target}号。",
                                    seat=dreamer.id, visible_to=(f"seat:{dreamer.id}",))

        # --- 幸运儿（一次性：三选一 —— 查验 / 守护 / 子弹；用完就永远没有）---
        lucky_guard: int | None = None
        lucky = next((s for s in self.alive if s.role_key == "lucky"), None)
        # ⚠️ 用户 2026-10-09 改定：**第 1 夜不问幸运儿**（第 1 夜三个技能都没必要开：
        #    守护可能奶穿、子弹/查验也没必要），**第 2 夜起**每个夜晚
        #    给他一次决策轮；技能用掉之后夜间不再问（一次性的）。
        if lucky and not lucky.lucky_used and day >= 2:
            legal = [i for i in self.alive_ids()]
            task = Task(kind="lucky",
                        text=(f"第 {day} 夜，你使用**幸运儿**的一次性技能（**只有这一次**，用完永远没有）：\n"
                              f"三选一 —— ① `check` 查验一个人是好人还是狼人；"
                              f"② `guard` 让一个人今夜免于狼刀（守护与摄梦撞在同一个人身上会**奶穿**）；"
                              f"③ `bullet` 选择**子弹**：你没有主动毒杀能力，但出局时（被放逐或被狼刀）"
                              f"可以开枪带走一个人自证；**被毒死/被梦死不能开枪**。\n"
                              f"选 `bullet` 时 `target` 留空；选 `check`/`guard` 必须给一个活人。\n"
                              f"也可以选择**今晚不用**（`use: false`），留到后面的夜晚。"
                              f"⚠️ 你看不到女巫/守卫这些技能的其它使用情况。"),
                        schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", "why": "<一句>"}], '
                                '"summary": "<一句判断>", "use": true/false, '
                                '"skill": "check|guard|bullet", "target": <玩家编号或 null>, '
                                '"reason": "<一句话>"}'),
                        legal=legal, required=())
            reply = await self.act(lucky, task)
            if truthy(reply.get("use"), default=False):
                skill = str(reply.get("skill") or "").strip().lower()
                # ⚠️ 官方"守护不能连续两晚守同一人"这条限制在**本项目自动满足**：
                #    幸运儿的技能是**一次性**的（用完永远没有），所以根本不存在
                #    "下一夜再守同一个人"这种场面。（上一轮我在这里加过 lucky_last_guard
                #    过滤 + 换人提示 —— 那是**永远走不到的死逻辑**，已删。）
                # ⚠️ 2026-10-30 修：**不替他随机挑人**。以前 target 写糊 / 写了已死的人时，
                #    coerce_seat 会随机挑一个活人 —— 一次性的毒可能就这样毒死好人，
                #    与守卫（"不随机，按没守成处理"）和女巫的纪律相反。
                tgt = self.coerce_seat(reply.get("target"), legal, default=None)
                if skill not in ("check", "guard", "bullet") or (skill != "bullet" and tgt is None):
                    # 技能名不认识 / 目标不可用：**不消耗**这次机会，并如实告诉他
                    lucky.private_events.append(
                        f"（系统提示）你的 skill 写成了 {reply.get('skill')!r}、"
                        f"target 写成了 {reply.get('target')!r} —— 这一手按「没有使用」处理，"
                        f"这次**没有消耗**你的幸运。请写 check/guard/bullet；"
                        f"check/guard 要给一个活着的人，bullet 不用给目标。")
                else:
                    lucky.lucky_used = True
                    if skill == "check":
                        verdict = "狼人" if self.seat_by_id(tgt).camp == WOLF_CAMP else "好人"
                        lucky.checked[tgt] = verdict
                        lucky.private_events.append(
                            f"第 {day} 夜：你用幸运技能查验了 {tgt}号，结果是【{verdict}】"
                            f"（技能已用完）。")
                        self.ledger.add(day, "night", "private",
                                        f"你查验了 {tgt}号，结果是【{verdict}】。（幸运技能已用完）",
                                        seat=lucky.id, visible_to=(f"seat:{lucky.id}",))
                    elif skill == "guard":
                        lucky_guard = tgt
                        lucky.private_events.append(
                            f"第 {day} 夜：你用幸运技能守护了 {tgt}号（技能已用完）。")
                    else:
                        lucky.lucky_bullet = True
                        lucky.private_events.append(
                            f"第 {day} 夜：你选择了**子弹**（技能已用完）。你出局时若死因是"
                            f"「被放逐」或「被狼刀」，可以开枪带走一个人自证；"
                            f"被毒死/被梦死不能开枪。")
            else:
                lucky.private_events.append(f"第 {day} 夜：你今晚没有使用幸运技能（留着）。")

        # --- 女巫 ---
        witch = next((s for s in self.alive if s.role_key == "witch"), None)
        save = False
        poison: int | None = None
        if witch and (self.witch_heal or self.witch_poison):
            # ⚠️ 空刀时不能说"击杀的是 None号"—— 审计实测这会污染唯一事实来源。
            # 2026-10-25（作者口径）：也不写"狼队空刀" —— 女巫的视角里只能得出
            # "没有人被击杀"，至于是没出刀还是被别的原因挡住，她本来就分不清。
            if victim is None:
                witch.private_events.append(f"第 {day} 夜：今晚没有人被击杀。")
            else:
                witch.private_events.append(f"第 {day} 夜：狼人今晚击杀的是 {victim}号。")
            legal = [i for i in self.alive_ids() if i != witch.id]
            task = Task(
                kind="witch",
                # ⚠️ 2026-10-25（作者口径）：空刀时**只告诉女巫"今晚没有人被击杀"**，
                #    不写"狼队空刀" —— 她从自己的视角分不清「狼队没出刀」还是「守护/别的原因挡住了」，
                #    那是白天看到平安夜之后才谈得上判断的事。给她一个断言的结论＝白送情报。
                text=((f"今晚被狼人击杀的是 {victim}号。"
                       if victim is not None else
                       "今晚没有人被击杀（你也没有得到刀口信息）。")
                      + f"你的解药：{'还在' if self.witch_heal else '已用完'}；"
                        f"毒药：{'还在' if self.witch_poison else '已用完'}。"
                        "请注意：同一夜不能同时使用解药和毒药。请决定你的行动。"),
                schema='{"save": true/false, "poison": <玩家编号或 null>, "reason": "<一句话理由>"}',
                legal=legal,
                extra={"victim": victim, "has_poison": self.witch_poison,
                       # ⚠️ 前端一直在读 `ask.extra.has_heal`（界面据此决定"救"按钮能不能点），
                       #    但引擎从来没发过这个字段 —— 靠的是对文案「你的解药：还在/已用完」
                       #    做正则兜底。一旦文案改了就会退回"按钮可点、引擎静默忽略"。
                       #    这里把字段补上（前端优先读它）。
                       "has_heal": self.witch_heal},
                required=("save",),
            )
            reply = await self.act(witch, task)
            raw_save = reply.get("save")
            wants_save = truthy(raw_save, default=False)     # 不再用 bool()：bool("false") 是 True
            if isinstance(raw_save, str) and normalize_word(raw_save) not in (TRUE_WORDS | FALSE_WORDS):
                witch.private_events.append(
                    f"（系统提示）你的 save 字段写成了 {raw_save!r}，我按「不救」处理。请写 true/false。")
            if victim is not None and self.witch_heal and wants_save:
                save = True
                self.witch_heal = False
                witch.private_events.append(f"第 {day} 夜：你使用了解药，救回了 {victim}号。")
            elif self.witch_poison and reply.get("poison") not in (None, "", 0, "0", "null", False):
                poison = self.coerce_seat(reply.get("poison"), legal, default=None)
                if poison:
                    self.witch_poison = False
                    witch.private_events.append(f"第 {day} 夜：你对 {poison}号 使用了毒药。")
                else:
                    # 想用毒但目标不合法（比如写了个乱码、或可毒的人都不在了）：
                    # 药不扣，但要留一句话，别让模型以为自己毒成了
                    options = "、".join(f"{i}号" for i in legal) or "无"
                    witch.private_events.append(
                        f"（系统提示）你想对 {reply.get('poison')!r} 用毒药，但那个目标不合法"
                        f"（可毒的人：{options}），这一夜没有用毒，毒药还在。")
        elif witch:
            # 两瓶药都用完了 → 女巫这一夜**没有任何可做的行动**：干脆不调用她（省一次调用）。
            # ⚠️ 判据是「解药与毒药**都**空了」：还剩任意一瓶就照旧问上面那条路 ——
            #    别把「还没用」当成「已经用掉」。「用没用」只有一个事实来源：
            #    `self.witch_heal` / `self.witch_poison`（真的用掉那一刻才置 False，见上面结算），
            #    这里不新造第二套状态。
            # ⚠️ 只写一条**中性事实**，而且只给她自己看（`seat:N`）：
            #    · 不写狼刀目标 —— 她已经没有行动，告诉她只是白送好人一条夜间情报；
            #    · 人类女巫在手机上只渲染台账（不渲染上下文包），所以台账也要留一条，
            #      否则「这一夜没被问」看起来像引擎漏了。
            idle = f"第 {day} 夜：你的两瓶药都已经用完了，今晚没有可做的行动。"
            witch.private_events.append(idle)
            self.ledger.add(day, "night", "private", idle,
                            seat=witch.id, visible_to=(f"seat:{witch.id}",))

        # --- 结算 ---
        # 规则细节：同一人既被刀又被毒只死一次，死因记为"毒"（影响猎人能否开枪）
        #          被守卫守住的狼刀不死；但"同守同救"（守卫守 + 女巫救）依然死
        #          **摄梦人**（2026-10-09）：梦游者免疫夜间伤害（狼刀/毒都伤不到），
        #          连续两晚被摄会死；摄梦人夜里死亡不带走梦游者（连死已取消）；
        #          但"摄梦 + 解药"= **奶穿**，照样死（与同守同救同口径）。
        deaths: dict[int, str] = {}
        dream_twice = (dreamer is not None and dreamer_target is not None
                       and dream_prev == dreamer_target)
        # 保护层数：摄梦、守卫、幸运儿的守护都算"一层保护"。
        # 本项目规则：**两层保护撞在同一个人身上 → 奶穿**（照样死）；
        #              **一层保护 + 女巫解药 → 也奶穿**（同守同救 / 摄梦+解药 / 守护+解药）。
        prot_count: dict[int, int] = {}
        for src in (dreamer_target, guard_target, lucky_guard):
            if src is not None:
                prot_count[src] = prot_count.get(src, 0) + 1
        if victim is not None:
            layers = prot_count.get(victim, 0)
            if layers >= 2 or (layers == 1 and save):
                deaths[victim] = "wolf"           # 奶穿
            elif layers == 1 or save:
                pass                              # 被保护住 / 被女巫救下
            else:
                deaths[victim] = "wolf"
        # 毒：**梦游者免疫夜间伤害**，毒不动他（幸运儿已不再有毒药）。
        if poison is not None and poison != dreamer_target:
            deaths[poison] = "poison"
        # 连续两晚被摄 → 第二晚死（含摄梦人自我连摄）
        if dream_twice:
            deaths.setdefault(dreamer_target, "dream")
            if dreamer is not None:
                dreamer.private_events.append(
                    f"第 {day} 夜：{dreamer_target}号 连续两晚被你摄梦 —— 他死了。")
        # ⚠️ 2026-10-10 作者改口径：**取消"连死"** —— 摄梦人死亡**不再**带走梦游者
        #    （跟守卫一样：守卫死了，被守的人不会跟着死）。
        #    原因（作者原话的意思）：摄梦人夜里被刀就顺手带走一张好人 ⇒ 好人直接崩盘，太惩罚性。
        #    仍然保留：连摄两晚梦死 ✓、入梦免疫刀毒 ✓、两层保护 / 一层保护+解药 = 奶穿 ✓。
        result = [(sid, cause) for sid, cause in deaths.items() if self.seat_by_id(sid).alive]
        self.emit("night_result", day=day, victim=victim, save=save, poison=poison,
                  guarded=guard_target, deaths=[s for s, _ in result])
        return result

    @staticmethod
    def _append_mind(mind: str, line: str, limit: int = 2500) -> str:
        """追加一行推理笔记。超过上限就只留最近的 —— 笔记每回合都会被重新注入，
        越滚越长等于每次调用都多花钱。"""
        text = (mind + "\n" if mind else "") + line
        if len(text) > limit:
            text = "（较早的笔记已省略）\n" + text[-limit:]
        return text

    def _majority(self, votes: dict[int, int | None]) -> int | None:
        counts = Counter(v for v in votes.values() if v is not None)
        if not counts:
            return None
        best = max(counts.values())
        leaders = [k for k, v in counts.items() if v == best]
        return self.rng.choice(leaders)

    # ------------------------------------------------------------------ #
    # 警长（11 人及以上）
    # ------------------------------------------------------------------ #

    def sheriff_seat(self) -> Seat | None:
        """当前警长（警徽在谁手上）；没人拿就是 None。"""
        return self.seat_by_id(self.sheriff_id) if self.sheriff_id else None

    def vote_weight(self, seat_id: int) -> float:
        """警长的一票算 1.5 票（多 0.5 票）。

        ⚠️ 权重必须挂在**还活着、且还能投票**的人身上：警徽在死亡路径上会被传承或撕掉
        （放逐/夜里死/被枪打死都会走 handle_sheriff_death），所以正常对局里
        "警徽在尸体手上"是走不到的；但函数层以前没有任何兜底，任何直接调用
        （考试题库就有一处直接置 `sheriff_id`）都能算出"死人的一票算 1.5 票"的公告。
        这里加一道闸：人不在场/已翻牌白痴 → 权重按普通票（他们本来也投不了票）。
        """
        seat = self.seat_by_id(seat_id)
        if seat is None or not seat.alive:
            return 1.0
        # 愚者/无能骑士即使拿了警徽，票权也按 0.5 算（作者口径：保留 0.5，不是清零）。
        if seat.revealed_idiot or getattr(seat, "knight_failed", False):
            return 0.5
        if self.sheriff_id == seat_id:
            return 1.5
        return 1.0

    def weighted_counts(self, votes: dict[int, int | None],
                        candidates: list[int] | None = None) -> dict[int, float]:
        """带警徽权重的计票：{被投的人: 票数}。

        `candidates` 非空（PK 轮）时，只有**在候选名单里**的被咒者才多得那一票。
        不传就按"整场放逐投票"算（首轮）。
        """
        counts: dict[int, float] = {}
        for voter, target in votes.items():
            if target is None:
                continue
            counts[target] = counts.get(target, 0.0) + self.vote_weight(voter)
        # 养鸦人的诅咒：被诅咒者在本轮**放逐投票**里额外多一票
        # （官方 Q&A：对上警投票无效、对放逐投票与平票 PK 重投有效 —— 这条路径只服务放逐）。
        # ⚠️ 必须**无条件**加：被诅咒的人哪怕一票都没有，也多这一票
        #    （原来写成 `if ... in counts` —— 没人投他时这 +1 就凭空消失了，属于真 bug）。
        # ⚠️ 2026-10-30 修：PK 轮只在**候选人**里加。以前无条件加，于是
        #    "首轮平票的人里没有被咒者 + PK 全弃权"时 counts 只剩被咒者这一票，
        #    他被当成 leader 放逐 —— 而他根本不在 PK 名单上。
        if self.cursed_today and (candidates is None or self.cursed_today in candidates):
            counts[self.cursed_today] = counts.get(self.cursed_today, 0.0) + 1.0
        return counts

    @staticmethod
    def fmt_votes(value: float) -> str:
        """3.0 → '3'、3.5 → '3.5'：公告里别出现"3.0 票"这种机器味。"""
        value = float(value)
        return str(int(value)) if value.is_integer() else f"{value:g}"

    def badge_note(self, votes: dict[int, int | None]) -> str:
        """计票后面缀一句"警长的票算 1.5"，让票数对得上。

        警长没投（不在表里）或者弃权（值是 None）就不加这句 —— 否则会出现
        "计票：3号 2 票（3号 是警长，他那一票算 1.5 票）"这种自相矛盾的公告。
        """
        sheriff = self.sheriff_seat()
        if sheriff is None or votes.get(sheriff.id) is None:
            return ""
        return f"（{sheriff.id}号 是警长，他那一票算 1.5 票）"

    # ------------------------------------------------------------------ #
    # 白天
    # ------------------------------------------------------------------ #

    def speech_order(self) -> list[Seat]:
        """白天发言顺序（用户定的规则 + 2026-10-08 加的「警长选方向」）：

        1. **场上没有警徽**：按座位号从小到大（1 → 2 → … → 10）；`speech_reverse` 对它无效
           （没有警长就没有"从警左/警右"这回事）。
        2. **警徽在场**：从警长的**下一个座位**开始顺着数（数到尾就绕回 1 号），警长**最后**发言；
           警长选了**逆序**时改成从警长的**上一个座位**倒着数，警长照样最后发言。
           例（11 人局、警长 7 号）：顺序 → 8、9、10、11、1、2、3、4、5、6、**7**；
                                逆序 → 6、5、4、3、2、1、11、10、9、8、**7**。
        警长死了 / 警徽流失了 → 自动退回第一种。

        （更早以前是"每天换个人先说"＝按天数轮转；想换回那种就把下面 return 改成
        `alive[offset:] + alive[:offset]` 那种写法。）
        """
        alive = sorted(self.alive, key=lambda s: s.id)
        sheriff = self.sheriff_seat()
        if sheriff is None or not sheriff.alive:
            return alive
        ids = [s.id for s in alive]
        if sheriff.id not in ids:                 # 保险：理论上不该发生
            return alive
        if self.speech_reverse:
            # 逆序：从警长的**上一位**开始倒着数 → 警长仍然最后说
            idx = ids.index(sheriff.id)
            return alive[:idx][::-1] + alive[idx + 1:][::-1] + [sheriff]
        start = ids.index(sheriff.id) + 1         # 警长的**下一位**先说
        return alive[start:] + alive[:start]      # 于是警长落在最后

    async def sheriff_pick_order(self, day: int) -> bool:
        """问警长今天从哪个方向开始发言（顺序 / 逆序）。返回 True = 这头狼警长**自爆了**。

        ⚠️ 警长如果是狼（用户 2026-10-09 的口径）：**定方向之前先问它要不要自爆** ——
        它这个回合本身也是"狼发言"的一次机会。爆了 → 今天的发言与投票全部跳过、直接进夜
        （警徽已经在它手上，所以这次爆**不吞警徽**：警徽按规则随它出局而传承/撕掉）。
        """
        self.speech_reverse = False               # 每天重新决定：默认顺序
        sheriff = self.sheriff_seat()
        if sheriff is None or not sheriff.alive:
            return False
        is_wolf = sheriff.camp == WOLF_CAMP
        # ⚠️ 开关关掉 = **这次调用根本不发生**（退回老口径）。狼警长的自爆机会是**搭在
        #    「定方向」这一次决策上**的（用户口径：自爆不额外占调用），所以这里不为它破例 ——
        #    它仍可以在自己那一轮发言的决策里自爆（代价是那时别人已经开说了）。
        if not self.sheriff_picks_order:
            return False
        task = Task(
            kind="sheriff_order",
            text=(f"第 {day} 天，你是警长（{sheriff.id}号）。"
                  + ("**先回答第一件事：这一轮要不要自爆**（`explode`）——\n"
                     "· 自爆 = 你当场出局并亮明狼人身份、今天的发言与投票全部跳过、**没有爆后发言/遗言**、直接进夜，**今晚照常刀人**；\n"
                     "· 警徽已经在你手上，所以这次爆**不会**让警徽流失（它会按规则传承/撕掉）；\n"
                     + king_take_hint(sheriff)
                     + "· 值得爆：白天肯定要被推出去、想抢轮次。不值得爆：你还干净、队友需要你控场。\n"
                     "**不自爆（explode 填 false）再往下决定发言方向**：\n" if is_wolf else "")
                  + f"· 顺序 = 从你的**下一位**开始、按座位号往上数，你最后发言；\n"
                  f"· 逆序 = 从你的**上一位**开始、按座位号往下数，你最后发言。\n"
                  f"两种都是你最后说（听得最全）。"),
            schema=('{"explode": true/false, "reverse": true/false, '
                    + KING_TAKE_SCHEMA +
                    '"reason": "<一句理由>"}'
                    if is_wolf else
                    '{"reverse": true/false, "reason": "<一句理由>"}'),
            legal=[],
            required=("reverse",) if not is_wolf else ("explode", "reverse"),
            extra={"explode_exempt": True} if is_wolf else None,
        )
        reply = await self.act(sheriff, task)
        if is_wolf and truthy(reply.get("explode"), default=False):
            await self._apply_explosion(sheriff.id, day, badge_lost=self._election_open,
                                        take=reply.get("take"))
            return True
        if not self.sheriff_picks_order:
            return False
        self.speech_reverse = truthy(reply.get("reverse"), default=False)
        order = "、".join(f"{s.id}号" for s in self.speech_order())
        self.ledger.add(day, "day", "announce",
                        f"警长 {sheriff.id}号 决定今天**{'逆序' if self.speech_reverse else '顺序'}**发言"
                        f"（{order}）。")
        return False

    def vote_summary(self, day: int, votes: dict[int, int | None], round_no: int = 1,
                     candidates: list[int] | None = None) -> None:
        """把这一轮所有票**按座位顺序**汇总成一条公告。

        逐票明细本来就会一条条进台账（那是"谁在什么时候投的"），
        这里给的是一眼看全的对照表 + 计票结果 —— 也就是"确认后汇总所有人的投票结果"。
        计票**带警徽权重**（警长那票 1.5），不然汇总说 3 票、放逐公告说 3.5 票，自相矛盾。

        `candidates` 非空（PK 轮）时原样转给 `weighted_counts`：被咒者不在候选里就不加那票。
        """
        if not votes:
            return
        # ⚠️ 2026-10-31 外部审查 E10：`votes[seat] = None` 现在有两种含义 ——
        #    「弃权」和「这一票无效（投了死人/翻牌愚者/写了个不存在的人）」。
        #    以前汇总一律写成"弃权"，与本轮逐票台账里那句"这一票无效"自相矛盾。
        invalid = set(getattr(self, "_invalid_votes", ()) or ())
        parts = []
        for seat_id in sorted(votes):
            target = votes[seat_id]
            mark = "（警长）" if seat_id == self.sheriff_id else ""
            if target is not None:
                shown = f"{target}号"
            elif seat_id in invalid:
                shown = "无效票"
            else:
                shown = "弃权"
            parts.append(f"{seat_id}号{mark}→{shown}")
        counts = self.weighted_counts(votes, candidates=candidates)
        tally = "、".join(f"{k}号 {self.fmt_votes(v)} 票"
                          for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "没有有效票"
        tag = "（PK 轮）" if round_no > 1 else ""
        # 🪶 **咒标**（作者 2026-10-09 口径）：被诅咒的人要在结果里**公开显示出来**，
        #    但**不公布是谁咒的**。这一条是"狼跳养鸦人会被抓"的机制基础：
        #    真鸦把票压在他头上 → 票面多一票 + 公开带咒标 → 好人能对着账把他按死。
        # ⚠️ 2026-10-30：只有那一票**真的计进去**了才写咒标，否则公告会自相矛盾
        #    （PK 轮被咒者不在候选里 → 没加那票，却还说"额外多一票"）。
        mark = ""
        curse_counted = bool(self.cursed_today) and (
            candidates is None or self.cursed_today in candidates)
        if curse_counted:
            mark = (f"　【诅咒】{self.cursed_today}号 中了诅咒：这次计票里他**额外多一票**"
                    f"（是谁咒的**不公布**）。")
        self.ledger.add(day, "day", "announce",
                        f"投票汇总{tag}：{'，'.join(parts)}。　计票：{tally}。{self.badge_note(votes)}{mark}")

    async def decide_for_speech(self, seat: Seat, day: int) -> dict:
        """**发言前的决策 + 发言正文，合并成一次高思考调用**（2026-10-07 用户定的口径）。

        为什么合并：以前一天是 11 次 high（决策）+ 11 次 low（发言）+ 11 次 high（投票决策），
        墙钟 10~15 分钟/天。这次调用本来就已经「想清楚了」，再单独叫一次低思考去
        「把想好的东西说成人话」是重复劳动 —— 所以正文（`speech`）就在同一份 JSON 里产出。
        投票前那次 `analyze_round` **保留**（听完全场再独立决策一次，它才是权威）。

        ⚠️ 与旧口径的关键区别（用户特别强调，别再改回去）：
          · 这里的 `vote` 是**此刻的倾向**，不是承诺 —— 投票时允许不同；
          · **没有**「发言必须与决策一致 / 不要说要投别人 / 别显得还在犹豫」这类硬约束：
            狼人心口不一（说投 A 实投 B）是变票型的正经战术，好人听到新消息也该能改；
          · 所以提示词只要求「把此刻的判断说清楚」，不要求「发誓投谁」。

        ⚠️ **真人座位直接跳过**（返回空 dict）：真人不需要先填一份决策表再说话 ——
        那是给模型的一致性机制。其它决策入口（analyze_round / decide_wolf_round /
        投票复用）也都是这么跳过真人的；漏了这一处的话，真人每次发言前都会被问一次
        前端根本不认识的面板（只能点「这一手放弃」或干等 human_timeout）。
        真人那一手由 `day_phase` 退回「单独问一次发言」（前端有 speech 面板）。
        """
        if seat.is_human:
            return {}
        others = [i for i in self.alive_ids() if i != seat.id]
        role = ROLES[seat.role_key]
        # 角色专属的「额外要思考的事」（用户点名要求的：预言家验谁、女巫毒谁、狼的冲锋/倒钩…）
        extra = ""
        if seat.role_key == "seer":
            extra = ("你是预言家：额外想清楚**下一步要验谁**，这一轮发言要不要跳身份、"
                     "要不要把已经验到的结果说出来。")
        elif seat.role_key == "witch":
            extra = ("你是女巫：额外想清楚**要不要用解药/毒药、毒谁**；如果你已经跳出身份，"
                     "把你对场上的判断和自己的打算说出来（但别把底牌细节说得太满）。")
        elif role.camp == "wolf" and not self.can_explode(seat):
            # 11 人板的狼王不能自爆（作者 2026-11-03）：给它一套"普通狼"的打法提示
            extra = ("你是**狼王**。**本板子狼王不能自爆** —— 这条规则对你关闭。"
                     "按普通狼的思路想这一轮怎么发言、怎么带票、哪一轮值得用出局带人。")
        elif role.camp == "wolf":
            # ⚠️ 自爆是**这一次决策里最先要回答的一件事**（用户 2026-10-09 定的口径）：
            #    每个角色白天发言前本来就有一次高思考调用，狼就在这次调用里先输出
            #    "要不要自爆"，不自爆再往下走"怎么打、怎么发言" —— **不额外多花调用**。
            #    （早先我把它做成单独一次调用，等于每头狼每次发言 +1 次，那是错的。）
            extra = ("你是狼人。**先回答第一件事：这一轮要不要自爆**（`explode`）——\n"
                     "· 自爆 = 你当场出局并亮明狼人身份、今天的发言与投票全部跳过、**今晚照常刀人**；\n"
                     "· 值得爆：白天肯定要被推出去、想抢轮次、或者要用自己换掉警徽/预言家的验人；\n"
                     "· 不值得爆：你还干净、或者队友需要你在场上搅局。\n"
                     + king_take_hint(seat)
                     + "**不自爆（explode 填 false）再往下想**：这一轮你的**打法是冲锋还是倒钩**、"
                     "票型怎么走（跟票/分票/带节奏），以及怎么把票导向对狼队有利的人。"
                     "（填 true 的话后面的 reads/vote/speech 可以留空；自爆没有发言阶段，引擎不会读你的 speech，别写爆后发言。JSON 仍然要写完整。）")
        elif seat.role_key == "hunter":
            extra = ("你是猎人：额外想清楚**如果你今天被票出去、枪要打谁**。")
        else:
            extra = ("你是好人：额外想清楚**按目前为止的汇总发言，谁最像狼**。")
        # 狼的「心口不一」是战术、不是禁令：只有算得清收益才用（用户 2026-10-07 的口径）
        lie_note = ""
        if role.camp == "wolf":
            lie_note = ("\n   · 你还可以**心口不一**：嘴上说投 A、实际投 B，用来变票型、"
                        "把某个好人做出去 —— 这是正经战术（发言和投票本来就是两件事）。"
                        "不过要算得清：这么换真能投出你想投的人、且不会把队友架上去，再用；"
                        "没算清就按真实想法说。")
        task = Task(
            kind="speech_plan",
            text=(f"第 {day} 天，马上轮到你发言了。**这一次回复里要把两件事一起做完**："
                  f"先想清楚（决策），再把要说出口的话写出来（发言）。\n"
                  f"1) 回顾你已知的信息 + 目前为止所有人的发言，逐个点评活着的玩家"
                  f"（{ '、'.join(f'{i}号' for i in others) }）：好人还是狼？像什么身份？为什么？\n"
                  f"2) 判断你该**保谁**、该**打谁**（要打就打最像狼的）。\n"
                  f"3) {extra}\n"
                  f"4) 定下你的立场，以及**你此刻的投票倾向**（vote）—— 注意：这只是倾向，"
                  f"不是承诺。等所有人都说完，你还会在投票前**再独立决策一次**，"
                  f"那时候可以改（听到新消息就该改）。\n"
                  f"5) 用 `speech` 写出你这一轮**要说出口的话**（口语化，350 字以内）："
                  f"把你的判断讲出来，可以质疑别人、可以施压。"
                  f"你可以说「目前更倾向 X，等听完再定」——**不要**发誓一定投谁。"
                  + ("（如果你是狼且 explode=true：**不用写 speech**；自爆后直接进夜，没有爆后发言/遗言。）"
                     if self.can_explode(seat) else "")
                  + "\n"
                  f"⚠️ 时间线：要报夜间信息就写「**昨晚**我验的 X 号 / 昨晚被刀的是 X 号」；"
                  f"**不要**拿今天才公开的东西（谁上警、前置/后置位、今天的发言）解释昨晚为什么那样做。"
                  + lie_note + TIER_RULES),
            schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", "role": "<猜的身份或 未知>", '
                    '"why": "<一句>"}], "tiers": [{"seat": <编号>, '
                    '"tier": "铁好|身份偏好|普通|偏狼|铁狼", '
                    '"good_reason": "<像好人的理由>", "wolf_reason": "<像狼的理由>"}], '
                    '"self_tier": "铁好|身份偏好|普通|偏狼|铁狼", '
                    '"summary": "<两三句局势判断>", '
                    '"protect": [<你要保的编号>], "attack": [<你要打的编号>], '
                    '"claim": "<你打算声称的身份，没有就写 无>", '
                    '"stance": "<你的立场一句话>", "role_plan": "<角色额外打算：验谁/毒谁/冲锋还是倒钩/票型>", '
                    '"vote": <你此刻倾向投谁>, '
                    '"speech": "<你这一轮要说出口的话，口语化，350 字以内>"}'
                    # 狼多一个字段、而且排在最前（用户口径：先决定自爆，不自爆再谈怎么打）
                    + ('，另外**第一个字段是** "explode": true/false'
                       '（你是狼：要不要现在就自爆）'
                       if self.can_explode(seat) else '')),
            legal=[i for i in others] or None, required=("reads", "vote", "speech"),
            # 狼选了自爆时，缺 reads/vote/speech 不许再罚一次"整份重写"（见 agents.LLMAgent.ask）
            extra={"explode_exempt": True} if self.can_explode(seat) else None,
        )
        reply = await self.act(seat, task)
        plan = self._digest_plan(reply)
        # ⚠️ 决策要件缺失**不许静默降级**（2026-10-07 审计）：少了字段，`day_phase` 就得为这一轮
        #    **额外补问一次发言**（这次调用白花一半），而且补问那次看不到这份决策
        #    （发言与决策不再绑定）。打完一局谁也看不出来，所以：
        #      ① `required` 里必须有这几项（上面那行）→ 模型漏了会被要求整份重写；
        #      ② 重写完还是缺，就在这个座位的私有经历里留一条**说清后果**的痕迹
        #         （`act()` 只会笼统记一句「缺了必需字段」，看不出这意味着什么）。
        gap = [name for name, got in (("reads", bool(plan["reads"])),
                                      ("vote", plan["vote"] is not None),
                                      ("speech", bool(plan["speech"]))) if not got]
        if gap and not seat.is_human:
            why = "、".join(gap)
            seat.private_events.append(
                f"（系统提示）你这次「决策+发言」的回复缺了必需字段（{why}）："
                f"系统为这一轮**额外补问了一次发言**（多花一次调用），"
                f"而且补问那次看不到你的决策（发言与决策不再绑定）。"
                f"下次请把 {why} 一起写全。")
            self.emit("warn", seat=seat.id,
                      message=f"决策+发言缺字段：{why}（这一轮要额外补问一次发言）")
        self.speech_decisions[seat.id] = plan
        return plan

    def _digest_reads(self, reply: dict) -> list[dict]:
        """把回复里的 reads 归一成 `[{seat, camp, role, why}]`（认不出来的那一条直接丢掉）。

        ⚠️ 「发言前决策」（`_digest_plan`）和「投票前决策」（`analyze_round`）用的是**同一套**
        schema，解析也只留这一份 —— 以前两处各抄一遍，改一处另一处就悄悄跑偏。
        """
        out: list[dict] = []
        raw = reply.get("reads")
        if isinstance(raw, (list, tuple)):
            for r in raw[: len(self.alive_ids()) + 2]:
                if not isinstance(r, dict):
                    continue
                who = self.coerce_seat(r.get("seat"), self.alive_ids(), default=None)
                if who is None:
                    continue
                out.append({"seat": who, "camp": normalize_word(r.get("camp")) or "?",
                            "role": str(r.get("role") or "未知").strip()[:12],
                            "why": str(r.get("why") or "").strip()[:120]})
        return out

    def _digest_tiers(self, reply: dict) -> list[dict]:
        """把回复里的 tiers（身份层级档位）归一成 `[{seat, tier, good_reason, wolf_reason}]`。

        ⚠️ 这个解析器以前只活在 `_digest_plan` 里：`analyze_round` 的 schema 明明要了 tiers，
        组装结果时却把它丢了 —— 每天 11 次白写，投票决策层也拿不到「铁好/身份偏好不能乱打」的档位
        （`_speech_hint` 正好靠它把档位交给发言层）。两处共用这一个，别再抄第二份。
        """
        out: list[dict] = []
        raw = reply.get("tiers")
        if isinstance(raw, (list, tuple)):
            for t in raw[: len(self.alive_ids()) + 2]:
                if not isinstance(t, dict):
                    continue
                who = self.coerce_seat(t.get("seat"), self.alive_ids(), default=None)
                if who is None:
                    continue
                # 兼容旧存档/旧提示词的档位名；新口径只认五档。
                tier = normalize_tier(t.get("tier"), "普通")
                out.append({
                    "seat": who,
                    "tier": tier,
                    "good_reason": str(t.get("good_reason") or "").strip()[:120],
                    "wolf_reason": str(t.get("wolf_reason") or "").strip()[:120],
                })
        return out

    def _digest_plan(self, reply: dict) -> dict:
        """把「决策 + 发言」那一次的回复归一成固定形状（缺字段一律给安全默认值）。"""
        def _ids(key: str) -> list[int]:
            raw = reply.get(key)
            out: list[int] = []
            if isinstance(raw, (list, tuple)):
                for x in raw:
                    who = self.coerce_seat(x, self.alive_ids(), default=None)
                    if who is not None:
                        out.append(who)
            elif raw is not None:
                who = self.coerce_seat(raw, self.alive_ids(), default=None)
                if who is not None:
                    out.append(who)
            return out

        return {
            "reads": self._digest_reads(reply),
            "tiers": self._digest_tiers(reply),
            "self_tier": normalize_tier(reply.get("self_tier"), ""),
            "summary": str(reply.get("summary") or "").strip()[:300],
            "protect": _ids("protect"),
            "attack": _ids("attack"),
            "suspect": _ids("suspect"),
            "claim": str(reply.get("claim") or "").strip()[:20],
            "stance": str(reply.get("stance") or "").strip()[:200],
            "role_plan": str(reply.get("role_plan") or "").strip()[:200],
            # ⚠️ `vote` 的语义是**此刻的倾向**，不是承诺：投票前还有一次权威的 analyze_round
            #    （听完全场再独立决定），所以这里叫倾向、也允许后面变（用户 2026-10-07 定的）。
            "vote": self.coerce_seat(reply.get("vote"), self.alive_ids(), default=None),
            "reason": str(reply.get("reason") or "").strip()[:200],
            # 说出口的正文也在这份回复里（决策与发言合并成一次调用，见 decide_for_speech）
            "speech": clip_speech(str(reply.get("speech") or "").strip()),
            # 狼的"要不要自爆"也在同一次调用里（字段排在最前；好人没有这个字段 → 恒为 False）
            "explode": truthy(reply.get("explode"), default=False),
            # 狼王自爆时**当场带走谁**（同一个字段也在同一次调用里，见 KING_TAKE_HINT 的说明）；
            # 不是狼王时这一栏没人读，留着也无害（它进 speech_decisions 存档，复盘能看到）。
            "take": self.coerce_seat(reply.get("take"), self.alive_ids(), default=None),
        }

    async def analyze_round(self, day: int, candidates: list[int] | None = None,
                            round_no: int = 1) -> dict[int, dict]:
        """白天的**分析/决策层**（最高思考，全员并发）：先想清楚，再说话。

        产出（都在同一个 JSON 里，一次调用拿到）：
          · reads：[{seat, camp, role, why}] —— 逐个点评：好人还是狼、像什么身份、为什么
          · summary：两三句局势判断
          · vote：本轮票谁（投票环节直接复用，不再多花一次高思考调用）
        这份分析会写进座位记忆（mind），后面几天的发言与决策都建立在它之上。
        """
        pool = list(candidates) if candidates else self.alive_ids()
        decided: dict[int, dict] = {}

        async def one(seat: Seat) -> tuple[Seat, dict]:
            legal = [i for i in pool if i != seat.id] or list(pool)
            others = [i for i in self.alive_ids() if i != seat.id]
            task = Task(
                kind="analyze",
                text=(f"第 {day} 天讨论开始前，先做一次完整分析（**只有你自己看得到**）：\n"
                      f"1) 逐个点评场上活着的玩家（{ '、'.join(f'{i}号' for i in others) }）："
                      f"你判断他是好人还是狼？像什么身份（预言家/女巫/猎人/守卫/白痴/村民/狼）？为什么？\n"
                      f"2) 综合局势用两三句总结。\n"
                      f"3) 定下这一轮你把票投给谁 —— **这一轮是权威决定**：你已经听完了全场发言，"
                      f"跟发言时说的不一样完全正常（听到新消息就该改；狼故意说一套投一套也是战术）。"
                      + TIER_RULES),
                schema=('{"reads": [{"seat": <编号>, "camp": "wolf|good", '
                        '"role": "<你猜的身份，写不准就写 未知>", "why": "<一句理由>"}], '
                        '"summary": "<两三句局势判断>", '
                        '"tiers": [{"seat": <编号>, "tier": "铁好|身份偏好|普通|偏狼|铁狼", '
                        '"good_reason": "<好人理由>", "wolf_reason": "<狼人理由>"}], '
                        '"self_tier": "铁好|身份偏好|普通|偏狼|铁狼", '
                        '"vote": <你要投的编号>, "reason": "<投他的核心理由>"}'),
                legal=legal, required=("reads", "vote"),
            )
            # 养鸦人：**在同一次调用里**先决定诅咒、再决定投票（用户 2026-10-09 改定的时机）
            if seat.role_key == "crow" and round_no == 1 and not self.cursed_today:
                task.text += self._crow_curse_prompt(seat, day, round_no)
                task.schema = task.schema[:-1] + ', "curse_target": <编号或 null>}'
                task.extra = {**(task.extra or {}), "curse": True,
                              "curse_legal": self._crow_curse_legal(seat, day)}
            return seat, await self.act(seat, task)

        voters = [s for s in self.alive if not s.is_human]
        gathered = await asyncio.gather(*(one(s) for s in voters), return_exceptions=True)
        for item in gathered:
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                continue
            seat, reply = item
            legal = [i for i in pool if i != seat.id] or list(pool)
            # ⚠️ 2026-11-06 真机复盘 P1：这里以前没传 `default=None`。
            #    模型分析失败（`_error`）时 `coerce_seat(None, legal)` 会**随机挑一个座位**，
            #    再写进 `day_decisions`；投票环节把它当"本轮已定票"复用，
            #    于是上游失败被包装成一张正常票（11-05 那局 9 号就这样投了 11 号）。
            #    分析失败就**不要替模型做决定**：target 留 None，投票前重新问；
            #    再失败就按无效票/弃权处理，让复盘看得见。
            #    注意：模型回了合法 JSON、只是没给可用票（例如 vote 为 None）仍按旧口径
            #    随机处理——这不是"上游失败"，而是它自己没答；测试剧本也依赖这条。
            raw_vote = reply.get("vote") or reply.get("target")
            target = self.coerce_seat(raw_vote, legal, default=None)
            if target is None and "_error" not in reply:
                target = self.coerce_seat(raw_vote, legal)
            # 养鸦人：诅咒要在**计票之前**生效（票数里会多出那一票）
            # ⚠️ 2026-10-30：调用失败（`_error`）时**不写**「这一轮没有使用诅咒」——
            #    那是把上游失败记成玩家主动放弃，会误导它下一轮的判断。
            if seat.role_key == "crow" and round_no == 1 and "_error" not in reply:
                self._apply_crow_curse(seat, reply)
            reads = self._digest_reads(reply)
            summary = str(reply.get("summary") or "").strip()[:300]
            decided[seat.id] = {
                "target": target, "vote": target,
                "reason": str(reply.get("reason") or "").strip()[:200],
                "reads": reads, "summary": summary,
                # ⚠️ tiers（身份高低：铁好/高/中/低/可疑）必须一起收进来：
                #    schema 里要了它、模型也写了，以前却在这里被丢掉 ——
                #    于是 `_speech_hint` 的「你刚才给的身份高低（打之前先看这里）」
                #    在**投票前决策**这条路上永远拿不到档位（只有发言前决策那条路有）。
                "tiers": self._digest_tiers(reply),
                "self_tier": normalize_tier(reply.get("self_tier"), ""),
                "round": round_no,
            }
            # 分析写进记忆：后面几天的发言/决策都建立在它之上（也便于复盘看它怎么想的）
            if summary:
                seat.mind = self._append_mind(seat.mind, f"第 {day} 天判断：{summary[:160]}")
            if reads:
                brief = "；".join(f"{r['seat']}号={r['camp']}/{r['role']}" for r in reads[:6])
                seat.mind = self._append_mind(seat.mind, f"第 {day} 天读牌：{brief}")
        self.day_decisions = decided
        return decided

    def _tell_gravekeeper(self, day: int) -> None:
        """把「昨天被放逐的人是什么阵营」写进守墓人的私有记录（白天发言轮开始时调用一次）。

        零额外调用：这类信息走 `seat.private_events` + 台账私有条目，他当天那次决策
        （发言前的决策 / 投票前的决策）自然就带上了。
        """
        grave = next((x for x in self.alive if x.role_key == "gravekeeper"), None)
        if grave is None:
            return
        last = self.last_exiled                     # 上一个白天真正被放逐的人
        if last is None:
            msg = (f"第 {day} 天：昨天没有人被放逐（平票/无人出局），守墓人没有新信息。")
        else:
            camp = "狼人" if self.seat_by_id(last).camp == WOLF_CAMP else "好人"
            msg = (f"第 {day} 天：昨天被放逐的 {last}号 身份是【{camp}】"
                   f"（守墓人只知道阵营，不知道具体身份）。")
        grave.private_events.append(msg)
        self.ledger.add(day, "day", "private", msg,
                        seat=grave.id, visible_to=(f"seat:{grave.id}",))

    def _crow_curse_legal(self, crow: Seat, day: int) -> list[int]:
        """今天可以诅咒谁：**所有活着的人（含自己）**。

        ⚠️ 2026-10-09 作者改口径：**可以连着两天甚至更多天咒同一个人** ——
        原来那条「不能连续两晚诅咒同一玩家」（官方乌鸦的限制）**本项目不采用**。
        理由（作者）：鸦本质是"**两票的民牌**"，就该能天天把同一张像狼的牌往死里压；
        否则"今天压一票、明天换人"就把这张牌的价值打散了。
        `day` 参数保留是为了兼容调用方（也在记录里留个天号）。
        """
        del day                                   # 现在与天数无关，留着只为签名稳定
        return list(self.alive_ids())

    def _crow_curse_prompt(self, crow: Seat, day: int, round_no: int) -> str:
        """给养鸦人的**投票任务**额外加的一段：先决定诅咒，再决定投票。"""
        legal = self._crow_curse_legal(crow, day)
        if not legal:
            return "\n【养鸦人技能】场上没有可诅咒的目标，这一轮不发动。\n"
        return (f"\n【养鸦人技能】在投出这一票之前，先决定**要不要用你的诅咒**：\n"
                f"  · 用 → 在 schema 里写 `curse_target`（今天的放逐投票里，他会**额外多一票**；"
                f"可以咒自己；候选：{'、'.join(f'{i}号' for i in legal)}）；\n"
                f"  · 不用 → `curse_target` 写 null。\n"
                f"✅ **可以连着几天咒同一个人**（这张牌就是「两票的民牌」：天天把同一张像狼的牌"
                f"往死里压，比每天换人更有效）；"
                f"诅咒对上警投票无效、对放逐投票（含 PK 重投）有效。\n"
                f"⚠️ 结果出来之后，**被咒的人会公开显示「中了诅咒」**（多那一票在票面上看得见）——"
                f"**但别人不知道是谁咒的**。\n"
                f"（先定诅咒、再定投票：这两件事在同一次回复里写完。）")

    def _apply_crow_curse(self, crow: Seat, reply: dict) -> None:
        """从养鸦人的回复里取出诅咒目标并生效（投票阶段调用，投票之前）。

        ⚠️ 这里**不许静默换人**：模型/真人点名了"昨天刚咒过的人"时，旧写法用
        `coerce_seat` 随手换一个别的座位，而给玩家的记录还写着"你诅咒了 X 号" ——
        玩家以为咒的是 A、实际咒的是 B（实测把这当成"已经是别人的票"了）。
        现在的口径：踩禁区就**不发动**，并如实说明原因。
        """
        raw = reply.get("curse_target")
        legal = self._crow_curse_legal(crow, self.day)
        if raw in (None, "", 0, "0", "null", "None") or not legal:
            crow.private_events.append(f"第 {self.day} 天：你这一轮没有使用诅咒。")
            return
        want = None
        try:
            want = int(raw)
        except (TypeError, ValueError):
            want = None
        if want is None or want not in legal:
            crow.private_events.append(
                f"第 {self.day} 天：你给的诅咒目标 {raw!r} 不在可选范围里（只能咒活着的人），"
                f"这一轮没有发动。")
            return
        crow.last_curse = want
        crow.last_curse_day = self.day
        self.cursed_today = want                     # 本轮放逐投票 +1 票（不公布来源）
        crow.private_events.append(
            f"第 {self.day} 天：你诅咒了 {want}号（他今天的放逐投票会多一票，别人看不到是你咒的）。")

    def _speech_hint(self, seat: Seat, day: int) -> str:
        """「单独问一次发言」的那段提示词。

        什么时候还会用到它（合并之后这是**退回路径**，不是主路径）：
          · 真人座位：他不需要先填决策表，直接就问这一句（这也是手机上的发言面板文案）；
          · `decision_first` 关着：老流程（先发言、再决策）；
          · 合并那次调用没能给出 `speech` 正文：补问一次，别拿决策字段拼假话进台账。

        ⚠️ 这里**不再**写「你已经决定投 X 号，发言必须与它一致」（旧口径，2026-10-07 已废）：
        投票前还有一次权威决策，发言只是「此刻的倾向」；狼人说一套投一套是战术，
        好人听到新消息也该能改 —— 所以提示词不要求发誓，也不禁止改口。
        决策层的读牌/身份高低照旧喂进来，让这一手说得有依据（有的话才写）。
        """
        base = (f"第 {day} 天白天，轮到你发言。请说明你的判断：谁是狼、谁像好人、"
                f"你此刻倾向投谁（这只是倾向，投票前你还会再想一次、可以变），并给理由。"
                f"控制在 350 字以内。\n"
                f"⚠️ 时间线：报夜间信息一律用「**昨晚**我验的 X 号」这种过去时；"
                f"**不要**拿今天才公开的信息（谁上警、前置/后置位、今天的发言）当昨晚的理由。")
        # 优先用「发言前的那次决策」（它看得到目前为止所有人的发言），退回到投票决策
        pre = (self.speech_decisions or {}).get(seat.id) or (self.day_decisions or {}).get(seat.id) or {}
        reads = pre.get("reads") or []
        read_txt = ""
        if reads:
            read_txt = "\n\n【你刚才的分析（别人看不到）】\n" + "\n".join(
                f"· {r['seat']}号：{('狼' if r['camp'] == 'wolf' else '好人')}"
                f"/像{r['role']}" + (f"（{r['why']}）" if r.get("why") else "")
                for r in reads[:12])
        if pre.get("summary"):
            read_txt += f"\n局势总结：{pre['summary']}"
        plan_txt = ""
        tiers = pre.get("tiers") or []
        if tiers:
            plan_txt += "\n\n【你刚才给的身份层级（打之前先看这里）】\n" + "\n".join(
                f"· {t['seat']}号 = {t['tier']}"
                + (f"（像好人的理由：{t['good_reason']}）" if t.get("good_reason") else "")
                + (f"（像狼的理由：{t['wolf_reason']}）" if t.get("wolf_reason") else "")
                for t in tiers[:12])
            plan_txt += ("\n· 铁律：**铁好不能打**；**身份偏好没有具体爆点也别打**；"
                         "**铁狼优先归票**；普通/偏狼按发言与票型排序。"
                         "别人如果来打你、而你已经被坐实，那就是他的逻辑漏洞，当场指出来。")
        if pre.get("self_tier"):
            plan_txt += (f"\n· 你给自己的公开档位：**{pre['self_tier']}** —— "
                         "铁好/偏好可带队找狼；普通表水/攻击各半；偏狼少攻击多表水；按这个档说话。")
        if pre.get("protect"):
            plan_txt += "\n· 你要保的人：" + "、".join(f"{i}号" for i in pre["protect"])
        attack = pre.get("attack") or pre.get("suspect")
        if attack:
            plan_txt += "\n· 你要打的人：" + "、".join(f"{i}号" for i in attack)
        if pre.get("stance"):
            plan_txt += f"\n· 你的立场：{pre['stance']}"
        if pre.get("role_plan"):
            plan_txt += f"\n· 你的角色打算（可以说出来，但别把底牌细节说满）：{pre['role_plan']}"
        if pre.get("claim") and pre["claim"] not in ("无", "none", "None", "-", "null"):
            plan_txt += f"\n· 你打算声称的身份：{pre['claim']}"
        lean = pre.get("target") or pre.get("vote")
        tail = ""
        if lean:
            tail = (f"\n\n【此刻的倾向】你眼下更倾向投 {lean} 号 —— 这**不是承诺**："
                    f"等听完后面的发言，投票时你还会再独立决定一次，可以改。"
                    f"发言里把此刻的判断和理由讲清楚就行（可以说「目前更倾向 X，等听完再定」），"
                    f"不要发誓一定投谁。")
        return base + read_txt + plan_txt + tail

    async def day_phase(self, night_deaths: list[tuple[int, str]]) -> None:
        day = self.day

        # --- 1) 先结算死亡（用户定的顺序：第一天白天**先**结算死亡，再竞选警徽）---
        # 愚者第一次受到任何死因都翻牌免疫；必须在“昨晚出局”公告之前把它摘掉，
        # 否则公告会宣布一个实际还活着的人死了。
        night_deaths = list(night_deaths)
        immune_night: list[tuple[int, str]] = []
        for sid, cause in list(night_deaths):
            seat = self.seat_by_id(sid)
            if seat and seat.role_key == "idiot" and not seat.revealed_idiot:
                night_deaths.remove((sid, cause))
                immune_night.append((sid, cause))
        if night_deaths:
            who = "、".join(f"{sid}号" for sid, _ in night_deaths)
            self.ledger.add(day, "day", "announce", f"昨晚出局：{who}。（不公布死因）")
        elif self.first_night_quiet and day == 1:
            # 「首夜无刀」板子的第一天：昨晚必然没人出局，但**原因不是**女巫救人、也不是守中 ——
            # 说成"平安夜"会让模型去盘"谁被守了/谁被救了"（它拿不到任何证据，纯属浪费推理）。
            # 这是**公开规则**（规则书里就写着第一夜狼人不动手），所以照实写不算泄密。
            self.ledger.add(day, "day", "announce",
                            "昨晚是平安夜，没有人出局。（本板子【首夜无刀】：第一夜狼队不行动，"
                            "所以这不是「守中」或「女巫用药」的结果。）")
        else:
            self.ledger.add(day, "day", "announce", "昨晚是平安夜，没有人出局。")
        # 愚者翻牌免疫的公告放在死讯之后；不公布死因，只公开身份。
        for sid, cause in immune_night:
            self._idiot_reveal(self.seat_by_id(sid), cause=cause)

        for seat_id, cause in night_deaths:
            self.kill(seat_id, cause)

        # --- 2) **只有第一夜**的死者有遗言（用户规则：仅第一天夜里的死者获得一次遗言机会）。
        #      第 2 夜起夜里死亡一律没有遗言；白天被放逐 / 白天被枪打死的人另有遗言
        #      （分别在 exile() 与 hunter_shoot() 里）。
        #      ⚠️ 夜里那一枪（夜里被刀的猎人开枪）打死的人**不算**在这里：用户第一次就写明
        #      "夜里被枪杀没有遗言"，这里维持不变。
        if day == 1:
            for seat_id, _cause in night_deaths:
                await self.last_words(self.seat_by_id(seat_id))

        # 夜里出局的警长：公布死讯之后处理警徽（第 1 夜还没有警长，这里是空操作）
        for seat_id, _cause in night_deaths:
            await self.handle_sheriff_death(self.seat_by_id(seat_id))

        # 猎人夜里被刀 → 亮身份开枪。
        # ⚠️ 这一枪算"夜里"的：不公示死因（用户定的规则）。
        # 但**第一夜**被打死的那个人也算"第一夜的死者"，照样有遗言（见 hunter_shoot 里的说明）。
        # 狼王夜里出局（被刀/被毒/被梦死/被奶穿）→ 同样**临死带走一人**（作者口径：任何死因都能带）。
        # ⚠️ 顺序很要紧：这一步必须在下面那句 `check_win()` **之前** ——
        #    否则"狼王夜里死 + 屠边同时成立"会先被判决，他那一口就没了（§56 的结算次序）。
        for seat_id, cause in night_deaths:
            dead = self.seat_by_id(seat_id)
            if dead.role_key == "hunter" and cause == "wolf":
                await self.hunter_shoot(dead, daytime=False)
            elif dead.role_key == "lucky" and cause == "wolf" and dead.lucky_bullet:
                await self.lucky_shoot(dead, daytime=False)
            elif dead.role_key == "wolf_king":
                await self.wolf_king_take(dead, where=f"你夜里出局了（{CAUSE_TEXT.get(cause, cause)}）",
                                          daytime=False)

        if self.check_win():
            return

        # --- 2.9) 昨天单狼自爆挂起的竞选：今天先问双爆、再恢复竞选 ---
        if (self._badge_suspend and self.sheriff_enabled and self.sheriff_id is None
                and int(self._badge_suspend.get("day") or 0) < day):
            if await self.resume_badge_election(day):
                return

        # --- 3) 第一天：死亡结算完了才竞选警长（所以夜里的死者既不参选也不投票）---
        if self.sheriff_enabled and day == 1 and self.sheriff_id is None:
            self._election_open = True
            try:
                await self.sheriff_election()
            finally:
                self._election_open = False
            # 狼在上警名单确定之后自爆了 → 今天到此为止（发言与投票全部跳过，当夜照常刀人）
            if self.exploded_today:
                return
        self.exploded_today = None            # 新的一天：清掉昨天的自爆标记

        # --- 3.5) 警长定今天的发言方向（顺序 / 逆序，用户 2026-10-08 加）---
        # 规则依据：警长有权决定"从警左还是警右"开始发言，两边都是警长**最后**说。
        # 每天问一次（警长可以今天顺序、明天逆序），没有警徽就退回默认（按座位号从小到大）。
        # 代价：有警徽的局每天多 1 次调用（`game.sheriff_picks_order: false` 可关）。
        # ⚠️ 警长是狼时，它这个回合也算"狼发言"：先问要不要自爆；爆了 → 今天到此为止，直接进夜。
        if await self.sheriff_pick_order(day):
            return

        # --- 守墓人：白天发言轮开始时把"昨天被放逐的人是什么阵营"告诉他 ---
        # ⚠️ 用户 2026-10-09 改定：**不占夜间调用**。原先是在夜里推一条私有记录（虽然不花调用，
        #    但时机不对：那时他还没听到今天的发言）。现在挪到白天发言轮开始之前 ——
        #    他当天那次发言决策/投票决策都能带着这条信息一起想，而且**零额外调用**。
        self._tell_gravekeeper(day)

        # --- 依次发言 ---
        # 「决策在前」：每个座位发言前各做一次**高思考**调用，而且那一次调用**同时产出
        # 决策与发言正文**（2026-10-07 合并，见 decide_for_speech）—— 发言层不再单独叫一次。
        # 它能看到「到目前为止所有人的发言」，所以每个人的判断都是最新的。
        # 没拿到正文时（真人座位 / 关着 decision_first / 模型这次没写 speech）才退回
        # "单独问一次发言"：绝不能用决策里的字段拼一句假话写进台账。
        # ⚠️ 2026-11-07 核对：A 的发言落账后，下一轮循环**立即**启动 B 的 `speech_plan`；
        #    实测只差约 0.05ms，A 的 TTS 播放期间 B 的模型请求已经在跑 —— **不要**再另做
        #    “模型预取/提前发 B 的请求”，那会重复调用。真正还能预取的是 B 的 TTS 合成/下载，
        #    已在 `web/index.html` 的 `VOICE.prefetchNext` 里做（A 在播时先取 B 的音频）。
        decisions: dict[int, dict] = {}
        for seat in self.speech_order():
            if not seat.alive:
                continue
            plan: dict = {}
            if self.decision_first:
                plan = await self.decide_for_speech(seat, day)
                # ⚠️ 自爆**不额外占一次调用**（用户 2026-10-09 纠正）：它就是这一次决策里
                #    最先输出的那个字段（狼的 schema 第一个字段是 explode）。填了 true → 当场爆，
                #    后面的发言与投票全部跳过，直接进夜（当夜照常刀人）。
                if self.can_explode(seat) and plan.get("explode"):
                    await self._apply_explosion(seat.id, day, badge_lost=self._election_open,
                                                take=plan.get("take"))
                    return
            speech = clip_speech(str(plan.get("speech") or "").strip())
            claim = str(plan.get("claim") or "").strip()
            got = plan                      # 发言正文与声称身份的来源（决策那次 or 补问那次）
            if not speech:
                reply = await self.act(seat, Task(
                    kind="speech",
                    text=(self._speech_hint(seat, day)),
                    schema=('{"speech": "<你的发言，口语化，350 字以内>", '
                            '"claim": "<你声称的身份，没有就写 无>", '
                            '"suspect": [<你怀疑的玩家编号>], '
                            '"reason": "<你的真实判断，只有你自己看得到>"}'),
                    legal=[i for i in self.alive_ids() if i != seat.id],
                    required=("speech",),
                ))
                got = reply
                speech = clip_speech(str(reply.get("speech") or "").strip())
                claim = str(reply.get("claim") or "").strip()
            speech = speech or "（我过，先听后面的。）"
            # ⚠️ `data={"round": "day"}` 是**白天轮次发言**的标记，必须打：
            #    竞选发言 / PK / 遗言也用 `kind="speech"` 记账（文案前缀【竞选】【PK】【遗言】），
            #    而 `context.py` 的【发言顺序与进度】要靠「谁真的在本轮发过言」算已发言/未发言名单 ——
            #    不区分就会在第 1 天把竞选发言算成轮次发言，给模型一份**错的**名单。
            self.ledger.add(day, "day", "speech", speech, seat=seat.id,
                            data={"round": "day"})

            if claim_allowed(speech, claim):
                self.ledger.add(day, "day", "claim", f"{seat.id}号 声称自己是【{claim}】。",
                                seat=seat.id, data={"claim": claim})

            note = []
            if got.get("summary"):
                note.append(f"第 {day} 天发言前的判断：{str(got['summary'])[:160]}")
            elif got.get("reason"):
                note.append(f"第 {day} 天我的真实判断：{str(got['reason'])[:200]}")
            suspect = got.get("suspect") or got.get("attack")
            if isinstance(suspect, (list, tuple)) and suspect:
                note.append(f"第 {day} 天我怀疑：{suspect}")
            elif isinstance(suspect, (int, str)) and str(suspect).strip():
                note.append(f"第 {day} 天我怀疑：{suspect}")
            if note:
                seat.mind = self._append_mind(seat.mind, "；".join(note))

        if self.check_win():
            return

        # --- 骑士审判：白天全场发言结束后、放逐投票开始前，每局一次 ---
        # ⚠️ 用户 2026-11-07 口径：审判后**当天放逐投票照常**，不因戳死狼/自己与狼王同归于尽而跳票。
        await self.knight_judge(day)
        if self.check_win():
            return

        # --- 投票前的决策（高思考，全员并发）---
        # 放在**全部发言之后**：这时每个人都听完了所有人的发言，正好回答
        # "根据汇总发言，我该投谁"（用户口径：好人想投最像狼的，狼想票型怎么走，
        #  女巫跳了身份还要再想一次投谁）。
        if self.decision_first:
            decisions = await self.analyze_round(day)
            if self.check_win():
                return

        # --- 投票：所有人**同时**投（现实里就是同时举票，也省时间）---
        votes = await self.run_vote(day, prefetched=decisions)
        self.vote_summary(day, votes)             # 先给一份"谁投了谁"的汇总，再看结果
        counts = self.weighted_counts(votes)      # 计票带警徽权重（警长 1.5）
        if not counts:
            self.ledger.add(day, "day", "announce", "无人有效投票，本轮无人出局。")
            self.last_exiled = None           # 守墓人：这天没有可查的放逐
            return

        best = max(counts.values())
        leaders = [k for k, v in counts.items() if v == best]

        if len(leaders) > 1 and self.pk_on_tie:
            tied = "、".join(f"{i}号" for i in leaders)
            self.ledger.add(day, "day", "announce",
                            f"投票平票（{tied} 各 {self.fmt_votes(best)} 票），进入 PK："
                            f"平票者各再发言一轮，然后只在她们之间重投。")
            await self.pk_speeches(day, leaders)
            if self.check_win():
                return
            second = await self.run_vote(day, candidates=leaders, round_no=2)
            self.vote_summary(day, second, round_no=2, candidates=leaders)
            counts2 = self.weighted_counts(second, candidates=leaders)
            if not counts2:
                self.ledger.add(day, "day", "announce", "PK 轮无人有效投票，本轮无人出局。")
                self.last_exiled = None
                return
            best2 = max(counts2.values())
            leaders2 = [k for k, v in counts2.items() if v == best2]
            if len(leaders2) > 1:
                tied2 = "、".join(f"{i}号" for i in leaders2)
                self.ledger.add(day, "day", "announce",
                                f"PK 仍然平票（{tied2} 各 {self.fmt_votes(best2)} 票），本轮无人出局。")
                self.last_exiled = None
                return
            await self.exile(day, leaders2[0], best2)
            return

        if len(leaders) > 1:
            tied = "、".join(f"{i}号" for i in leaders)
            self.ledger.add(day, "day", "announce",
                            f"投票平票（{tied} 各 {self.fmt_votes(best)} 票），本轮无人出局。")
            self.last_exiled = None
            return

        await self.exile(day, leaders[0], best)

    async def run_vote(self, day: int, candidates: list[int] | None = None,
                       round_no: int = 1,
                       prefetched: dict[int, dict] | None = None) -> dict[int, int | None]:
        """让所有存活玩家投一轮票。candidates 非空时只能在这些人里选（PK 轮）。

        白痴翻牌之后没有投票权，也不会再被投票放逐（投他等于浪费一票），
        所以这两种名单里都把他排除掉。

        `prefetched`：decision_first 模式下"本轮早先已经有私密决策"的座位 ——
        直接沿用，不再花一次调用（PK 轮 / 名单变了 / 目标不合法时照旧现问）。
        """
        # ⚠️ 传了 candidates 时也要过滤白痴：否则 PK 轮/以后新增的调用方
        # 会把"已经翻牌的愚者"重新放进候选，等于规则只写在默认分支里（审计实测到）。
        # ⚠️ 2026-10-31 外部审查 E10：本轮"票面写了但没算"的座位（死人/翻牌愚者/不存在的号）。
        #    `vote_summary` 拿它把「无效票」和「弃权」分开写，别让公告自相矛盾。
        self._invalid_votes: set[int] = set()
        pool = list(candidates) if candidates else self.alive_ids()
        voters = list(self.alive)

        async def one_vote(voter: Seat):
            legal = [i for i in pool if i != voter.id] or list(pool)
            # 「决策在前」：这一轮早先已经私下定好了 → 直接用，不再问一次
            pre = (prefetched or {}).get(voter.id) or {}
            if (round_no == 1 and pre and pre.get("target") in legal and not voter.is_human):
                # ⚠️⚠️ 这里**只能写来路、绝对不许带那句私有理由**（2026-10-08 只读复盘抓到的 P0 泄漏）：
                #    投票条目是 **public**（服务端会推给真人前端、也会进其他座位的包），
                #    而 `pre["reason"]` 来自决策层（prompt 里明写「只有你自己看得到」）——
                #    实测泄漏过狼的内部计划原话（「我按倒钩计划…今晚女巫解药已用再刀7号真预」），
                #    并且被 7 号/10 号读进思维链、直接影响了女巫的毒杀选择。
                #    私有理由仍留在存档的 `decisions` 与调用留痕里，复盘看得到、公开文本里不该有。
                # 说的是**来路**，不是「与发言一致」：这一票复用的是投票前那次（听完全场后的）
                # 权威决策；跟发言时嘴上说的倾向可以不一样（台账里另有中性的 lean 可对照）。
                # ⚠️⚠️ P3-4：这句文案**不能再指向"见自己的私有记录"** ——
                #    台账里这条是 public 的：对**别人**说「见你自己的私有记录」是句空话
                #    （他们手里根本没有你的记录），而投票者**本人**读台账也找不到那句理由
                #    （私有理由只进 `mind` 的 summary/reads 与存档）。所以只留中性事实；
                #    它自己的原话由下面 `_append_mind` 记一次，下一轮被追问时自己看得到。
                return voter, legal, {"vote": pre["target"],
                                      "reason": "（本轮已定票）",
                                      "_prefetched": True}
            note = "（PK 轮：只能在平票的座位里选）" if candidates else ""
            task = Task(kind="vote",
                        text=f"第 {day} 天投票{note}（**这一票是放逐票，不是警徽票**）：请选出你要放逐的玩家。",
                        schema='{"vote": <玩家编号>, "reason": "<一句话理由>"}',
                        legal=legal, required=("vote",))
            # 养鸦人：诅咒就发生在**放逐投票**这一刻（用户 2026-10-09 改定的时机）。
            # ⚠️ 只在 round_no == 1 且**今天还没咒过**时加：
            #    决策在前那条路上，诅咒已经在 `analyze_round` 里生效了
            #    （那次回复被 prefetch 到这里复用）—— 再问一次就成了"今天咒两次"。
            if (voter.role_key == "crow" and round_no == 1
                    and not self.cursed_today):
                task.text += self._crow_curse_prompt(voter, day, round_no)
                task.schema = task.schema[:-1] + ', "curse_target": <编号或 null>}'
                task.extra = {**(task.extra or {}), "curse": True,
                              "curse_legal": self._crow_curse_legal(voter, day)}
                reply = await self.act(voter, task)
                self._apply_crow_curse(voter, reply)
                return voter, legal, reply
            return voter, legal, await self.act(voter, task)

        gathered = await asyncio.gather(*(one_vote(s) for s in voters),
                                        return_exceptions=True)
        votes: dict[int, int | None] = {}
        for item in gathered:                      # 按座位顺序记台账，日志才稳定
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                continue
            voter, legal, reply = item
            pk_tag = "（PK 轮）" if round_no > 1 else ""
            # 只记**事实**：这一手投了谁（弃权就是弃权）、是不是复用了早先的决策、以及这个座位
            # **发言时**的倾向（`lean`）。发言时说投 A、投票投 B 是允许的（狼变票型 /
            # 好人听到新消息改主意），所以这里既不做评判、也不要求解释 ——
            # 只为复盘能看出「嘴上说谁、实际投谁」。
            # ⚠️ 警长竞选那一轮**没有** lean：那时还没人发言，压根不存在"发言时的倾向"
            #    （那条在 `sheriff_election` 里单独记，不经过这里）。
            lean = (self.speech_decisions.get(voter.id) or {}).get("vote")
            if truthy(reply.get("abstain"), default=False):
                # 人类玩家弃权 / 超时：真的算弃权，不再偷偷替他随机投一票
                votes[voter.id] = None
                self.ledger.add(day, "day", "vote", f"弃权（这一轮不投票）{pk_tag}",
                                seat=voter.id,
                                data={"abstain": True, "round": round_no, "lean": lean})
                continue
            # ⚠️ 投给**已经翻牌的愚者** = 无效票（用户 2026-10-09 明确："白痴翻牌后直接变成无法被票"）。
            #    他不在 `legal` 里，所以模型的这一票**本来会被 coerce_seat 静默改成随机一个活人** ——
            #    那是典型的"静默降智"：模型以为投了白痴，实际投了别人，台账上还写着"投给 X号"。
            #    规则书写的是"投他等于浪费一票"，所以这里按**无效票**记，并如实写清原因。
            raw = self.coerce_seat(reply.get("vote"), self.alive_ids(), default=None)
            # ⚠️ 2026-10-30 修：**投给已经出局的人**同样是无效票。
            #    以前这一票压不出来（死人不 alive_ids 里）就落到下面 coerce_seat 随机改投，
            #    台账还写着「投给 X号」—— 与白痴那条是同一类「静默降智」。
            dead_vote = self.coerce_seat(reply.get("vote"), [s.id for s in self.seats],
                                         default=None)
            if raw is None and dead_vote is not None and not self.seat_by_id(dead_vote).alive:
                votes[voter.id] = None
                self._invalid_votes.add(voter.id)
                self.ledger.add(day, "day", "vote",
                                f"投给了已经出局的 {dead_vote}号 —— 这一票无效（不计入计票）"
                                f"{pk_tag}",
                                seat=voter.id,
                                data={"invalid": True, "target": dead_vote, "round": round_no,
                                      "lean": lean})
                continue
            if raw is None:
                # ⚠️⚠️ 2026-10-31 外部审查 P1-05：这是第三条"静默降智"的路 ——
                #    模型回了个不存在的座位号 / 自己的座位 / PK 轮投候选外的人时，
                #    `raw`、`dead_vote` 都是 None，于是落到下面那句没有 default 的
                #    `coerce_seat(..., legal)` → **随机投给一个活人**，台账却照写
                #    「投给 X号」。与死人/白痴两条路的纪律（记无效票）对齐。
                votes[voter.id] = None
                self._invalid_votes.add(voter.id)
                self.ledger.add(day, "day", "vote",
                                f"票面写的是 {reply.get('vote')!r}（不是能投的座位）"
                                f" —— 这一票无效（不计入计票）{pk_tag}",
                                seat=voter.id,
                                data={"invalid": True, "target": None, "round": round_no,
                                      "lean": lean})
                continue
            target = self.coerce_seat(reply.get("vote"), legal)
            votes[voter.id] = target
            # ⚠️ 自带括号的文案（例如复用决策那条「（本轮已定票）」）不再套第二层括号 ——
            #    否则公开台账里会写成「投给 2号（（…））」这种双层括号（实测）。
            why = str(reply.get("reason") or "").strip()
            reason = "" if not why else (why if why.startswith("（") else f"（{why}）")
            # 带上轮次：不然台账里两轮投票长得一模一样，模型分不清哪票是哪轮的
            self.ledger.add(day, "day", "vote", f"投给 {target}号{reason}{pk_tag}",
                            seat=voter.id,
                            data={"target": target, "round": round_no,
                                  "lean": lean,
                                  "prefetched": bool(reply.get("_prefetched"))})
            # ⚠️⚠️ P3-4：**复用决策**那一票的公开文案是中性的（「（本轮已定票）」），
            #    理由本身只活在 `day_decisions` 里 —— 而那是引擎内部状态，不进包。
            #    结果就是：它下一轮被追问「你为什么投他」时，**自己**手上没有那句原话。
            #    这里把它的原话记进它自己的推理笔记（`mind` 是私有块，只在它自己的包里出现），
            #    公开台账不受影响。非复用那条理由本来就写在公开条目里，不必重复。
            if reply.get("_prefetched"):
                pre_reason = str(((prefetched or {}).get(voter.id) or {}).get("reason") or "").strip()
                if pre_reason:
                    voter.mind = self._append_mind(
                        voter.mind, f"第 {day} 天我投 {target}号的理由：{pre_reason[:160]}")
        return votes

    async def pk_speeches(self, day: int, tied: list[int]) -> None:
        """平票者各再发一轮言（给自己辩护 / 指出对手问题）。"""
        tie_text = "、".join(f"{i}号" for i in tied)
        for seat_id in sorted(tied):
            seat = self.seat_by_id(seat_id)
            if not seat.alive:
                continue
            task = Task(
                kind="speech",
                extra={"pk": True},          # ⚠️ 托管别把 PK 辩护任务重拼成"白天轮次发言"（2026-11-04 审查 P2）
                text=(f"你被投票平票了（{tie_text}），进入 PK 轮。请再发一次言："
                      f"为自己辩护、或指出对手的问题，说服大家别投你。控制在 300 字以内。"),
                schema=('{"speech": "<你的 PK 发言，300 字以内>", '
                        '"claim": "<你声称的身份，没有就写 无>", '
                        '"suspect": [<建议投谁>], "reason": "<你的真实判断>"}'),
                legal=[i for i in tied if i != seat_id],
                required=("speech",),
            )
            reply = await self.act(seat, task)
            speech = str(reply.get("speech") or "").strip() or "（PK 轮我没什么好说的。）"
            self.ledger.add(day, "day", "speech", f"【PK】{speech}", seat=seat.id)
            # PK 轮的身份声明与推理也要记账（普通发言会记，这里原来漏了）
            claim = str(reply.get("claim") or "").strip()
            if claim_allowed(speech, claim):
                self.ledger.add(day, "day", "claim", f"{seat.id}号 声称自己是【{claim}】。",
                                seat=seat.id, data={"claim": claim})
            note = []
            if reply.get("reason"):
                note.append(f"第 {day} 天 PK 轮我的真实判断：{str(reply['reason'])[:200]}")
            suspect = reply.get("suspect")
            if isinstance(suspect, (list, tuple)) and suspect:
                note.append(f"第 {day} 天 PK 轮我怀疑：{suspect}")
            if note:
                seat.mind = self._append_mind(seat.mind, "；".join(note))

    async def exile(self, day: int, seat_id: int, votes: float) -> None:
        seat = self.seat_by_id(seat_id)
        score = self.fmt_votes(votes)
        self.ledger.add(day, "day", "announce",
                        f"投票结果：{seat_id}号 得 {score} 票。")
        # 守墓人的信息来源：**只有真正被投票放逐**的人才算（愚者翻牌免疫的不算）
        self.last_exiled = seat_id
        died = self.kill(seat_id, "vote")
        if not died:
            # 愚者翻牌免疫：这一轮没有人真的被放逐，不进入遗言/警徽/出局技能链。
            self.last_exiled = None
            self.check_win()
            return
        self.ledger.add(day, "day", "announce",
                        f"投票结果：{seat_id}号 得 {score} 票，被放逐出局。")
        # ⚠️ 2026-10-25 修：**遗言必须排在判胜负之前**。
        #    以前这里是 `if seat.role_key != "wolf_king" and self.check_win(): return` ——
        #    于是"这一票正好结束对局"（投出最后一狼 / 最后一张边牌）时被放逐者**拿不到遗言**，
        #    而那恰恰是最该给遗言的一票。现在顺序 = 放逐 → 遗言 → 警徽 → 猎人/狼王技能 → 判胜负，
        #    与 `content/规则书.md` / README 的承诺一致。
        #    （`self.winner` 已在别处有值时仍然不额外花调用——那是"局早就结束了"的防御分支。）
        if self.winner:
            return
        # 最后一狼被放逐、且已经不可能翻盘 → 不再发遗言调用。
        # 典型：最后一只小狼被推出；或最后一只狼王被推出，但神、民两边都还剩≥2 人，
        # 他带走一个也屠不了边。只有“语言调用”省掉；狼王那一枪仍按规则结算。
        skip_last_words = self._exile_skips_last_words(seat)
        if skip_last_words and seat.role_key != "wolf_king":
            self.check_win()
            return
        if not skip_last_words:
            # 被投票放逐 → 有一轮**单独的遗言**（狼可以拿它骗人，好人可以自白或编身份）
            await self.last_words(seat)
            # 警长被放逐：遗言之后处理警徽（传给某人 / 撕掉）
            await self.handle_sheriff_death(seat)
        # 猎人在白天被放逐 → 开枪打死的人**有遗言**，且死因公示为"被子弹打死"
        # 狼王在白天被放逐 → **临死带走一名好人**（同样公示带走了谁），带完才判胜负
        # ⚠️ **狼王例外**（作者 2026-10-10 的结算次序）：他临死还能带走一人，所以**不许**在
        #    他带人之前就判胜负 —— 否则「好人把最后一狼（狼王）票出去 → 立刻判好人胜」
        #    会把他那一口吃掉，而"狼王反扑带走某一边最后一张好人牌 = 狼队屠边成功"
        #    正是新板子最要紧的一步棋。
        if seat.role_key == "hunter":
            await self.hunter_shoot(seat, daytime=True)
        elif seat.role_key == "lucky" and seat.lucky_bullet:
            await self.lucky_shoot(seat, daytime=True)
        elif seat.role_key == "wolf_king":
            await self.wolf_king_take(seat, where="你被投票放逐了", daytime=True)
        self.check_win()

    async def knight_judge(self, day: int) -> None:
        """骑士在白天放逐投票前的一次性审判；每局只问一次。"""
        knight = next((s for s in self.alive if s.role_key == "knight" and not s.knight_used), None)
        if knight is None:
            return
        legal = [i for i in self.alive_ids() if i != knight.id]
        if not legal:
            return
        task = Task(
            kind="knight_judge",
            text=(f"第 {day} 天，全部发言已经结束，放逐投票开始之前。"
                  f"你是【骑士】，本局还有一次「骑士审判」，可以选择是否发动，以及审判谁。\n"
                  f"· 目标是狼人 → 对方翻牌出局（公示身份）；目标是狼王 → 你和狼王一起出局，狼王不能带人。\n"
                  f"· 目标是好人 → 只公示对方是好人阵营（不公示具体职业），你不出局，但票权降为 0.5。\n"
                  f"· 无论结果如何，今天的放逐投票都会照常进行。\n"
                  f"收益高风险高：没有把握就填 use=false，不要为了用技能而用。"),
            schema=('{"use": true/false, "target": <玩家编号或 null>, '
                    '"reason": "<一句话理由>"}'),
            legal=legal, required=("use",),
        )
        reply = await self.act(knight, task)
        if not truthy(reply.get("use"), default=False):
            return
        target_id = self.coerce_seat(reply.get("target"), legal, default=None)
        if target_id is None:
            self.ledger.add(day, "day", "announce",
                            f"{knight.id}号 想发动骑士审判，但没有给出合法目标；本次不发动。")
            return
        knight.knight_used = True
        target = self.seat_by_id(target_id)
        if target.role_key == "wolf_king":
            # 2026-11-07 作者口径：骑士撞狼王，两人一起出局；狼王不再带走别人。
            self.ledger.add(day, "day", "announce",
                            f"{knight.id}号 翻牌【骑士】，审判 {target_id}号 —— "
                            f"{target_id}号 是【狼王】，骑士与狼王一起出局；狼王不能带人。")
            target.king_done = True
            self.kill(target_id, "knight", reveal=True)
            self.kill(knight.id, "knight", reveal=True)
            await self.handle_sheriff_death(target)
            await self.handle_sheriff_death(knight)
            self.check_win()
            return
        if target.role_key == "wolf":
            self.ledger.add(day, "day", "announce",
                            f"{knight.id}号 翻牌【骑士】，审判 {target_id}号 —— "
                            f"{target_id}号 是【狼人】，翻牌出局。")
            self.kill(target_id, "knight", reveal=True)
            await self.handle_sheriff_death(target)
            self.check_win()
            return
        # 审判到好人：只公示阵营，不公示职业；骑士不出局，但票权降为 0.5。
        knight.knight_failed = True
        self.ledger.add(day, "day", "announce",
                        f"{knight.id}号 翻牌【骑士】，审判 {target_id}号 —— "
                        f"{target_id}号 是好人阵营（不公示具体职业）；"
                        f"{knight.id}号 是【无能的骑士】，不出局，票权从此降为 0.5 票。")
        self.check_win()

    async def hunter_shoot(self, hunter: Seat, daytime: bool = True) -> None:
        """猎人出局时开枪。

        `daytime=True` = 白天那一枪（自己被放逐）：被打死的人**有遗言**、死因公示"被子弹打死"。
        `daytime=False` = 夜里那一枪（自己夜里被刀）：没有遗言、不公示死因（用户定的规则）。
        """
        legal = list(self.alive_ids())
        if not legal:
            return
        where = "你被投票放逐了" if daytime else "你夜里出局了"
        task = Task(kind="hunter_shoot",
                    text=f"{where}，可以亮出猎人身份开枪带走一名玩家；也可以选择不开枪。",
                    schema='{"shoot": <玩家编号或 null>, "reason": "<一句话理由>"}',
                    legal=legal)
        reply = await self.act(hunter, task)
        target = self.coerce_seat(reply.get("shoot"), legal, default=None)
        if not target:
            self.ledger.add(self.day, "day", "announce", f"{hunter.id}号 亮出猎人身份，但没有开枪。")
            return
        if daytime:
            self.ledger.add(self.day, "day", "announce",
                            f"{hunter.id}号 亮出猎人身份，开枪带走 {target}号 —— "
                            f"{target}号 的死因公示：被子弹打死。")
        else:
            # 夜里那一枪：只记"谁被带走了"，不说死因（要场上好人自己去猜）
            self.ledger.add(self.day, "day", "announce",
                            f"{hunter.id}号 亮出猎人身份，开枪带走 {target}号"
                            f"（夜里出局，不公示死因）。")
        if not self.kill(target, "hunter"):
            # 愚者翻牌免疫：这一枪没有造成出局，不进入遗言/狼王/警徽链。
            return
        # 狼王被这一枪带走也算"出局" → 他照样能临死带走一名好人（作者口径：任何原因都能带）。
        # ⚠️ 必须在这句 `check_win()` 之前：被枪打死的可能正是"最后一狼"，
        #    先判胜负就把他那一口吃掉了（§56 的结算次序）。
        victim_seat = self.seat_by_id(target)
        if victim_seat.role_key == "wolf_king":
            await self.wolf_king_take(victim_seat, where="你被猎人开枪带走", daytime=daytime)
        if self.check_win():
            return
        if daytime:
            # 白天被打死的人也有遗言
            await self.last_words(self.seat_by_id(target))
        elif self.day == 1:
            # ⚠️ 第一夜那一枪打死的人**也算第一夜的死者**，照样有遗言（用户明确要求：
            # "猎人第一夜被刀，女巫不救，猎人死后开枪打死另一个人，那么两个人也可以都获得遗言，仅第一夜"）。
            # 第 2 夜起就没有了 —— 那时连猎人自己都没有遗言。
            await self.last_words(self.seat_by_id(target))
        await self.handle_sheriff_death(self.seat_by_id(target))
        self.check_win()

    async def lucky_shoot(self, lucky: Seat, daytime: bool = True) -> None:
        """幸运儿选中「子弹」后，出局时按死因决定能否开枪。

        与猎人同口径：只在**被放逐**或**被狼刀**时触发；被毒死 / 被梦死 /
        被狼王带走 / 被枪带走都不能开枪。子弹只有一次，开了就消耗。
        """
        if lucky is None or not lucky.lucky_bullet:
            return
        lucky.lucky_bullet = False                  # 先落闸：多条死亡路径不许重复触发
        legal = list(self.alive_ids())
        if not legal:
            self.ledger.add(self.day, "day", "announce",
                            f"{lucky.id}号 亮出【幸运儿】身份，但场上没有可带走的人。")
            return
        where = "你被投票放逐了" if daytime else "你被狼刀出局了"
        task = Task(
            kind="lucky_shoot",
            text=(f"{where}。你选的是**子弹**：可以亮出【幸运儿】身份，开枪带走一名玩家自证；"
                  f"也可以选择不开枪。**被毒死/被梦死不能开枪**，本枪只有一次。"),
            schema='{"shoot": <玩家编号或 null>, "reason": "<一句话理由>"}',
            legal=legal,
        )
        reply = await self.act(lucky, task)
        target = self.coerce_seat(reply.get("shoot"), legal, default=None)
        if not target:
            self.ledger.add(self.day, "day", "announce",
                            f"{lucky.id}号 亮出【幸运儿】身份，但没有开枪。")
            return
        if daytime:
            self.ledger.add(self.day, "day", "announce",
                            f"{lucky.id}号 亮出【幸运儿】身份，开枪带走 {target}号 —— "
                            f"{target}号 的死因公示：被子弹打死。")
        else:
            self.ledger.add(self.day, "day", "announce",
                            f"{lucky.id}号 亮出【幸运儿】身份，开枪带走 {target}号"
                            f"（夜里出局，不公示死因）。")
        if not self.kill(target, "lucky"):
            return
        victim_seat = self.seat_by_id(target)
        if victim_seat.role_key == "wolf_king":
            await self.wolf_king_take(victim_seat, where="你被幸运儿开枪带走",
                                      daytime=daytime)
        if self.check_win():
            return
        if daytime:
            # 子弹最早第 2 夜才会被选中；夜里那一枪按“夜里出局无遗言”处理。
            await self.last_words(victim_seat)
        await self.handle_sheriff_death(victim_seat)
        self.check_win()

    # ------------------------------------------------------------------ #
    # 狼王（新板子 `eleven_wolfking`）
    # ------------------------------------------------------------------ #

    def king_targets(self, king: Seat) -> list[int]:
        """狼王这一口能带谁：**场上所有活人里，除了狼队友**（他自己已经出局，不在名单里）。

        作者口径：**不能带走狼队友**。理由（作者原话要点）：带队友做身份的收益远不如带好人，
        而且**禁止能省 AI 的思考额度** —— 名单里没有狼，它就不用再纠结"要不要卖队友"。
        """
        return [s.id for s in self.alive if s.id != king.id and s.camp != WOLF_CAMP]

    async def wolf_king_take(self, king: Seat, *, where: str, daytime: bool,
                             prefetched=None) -> None:
        """狼王出局时**当场带走一名好人**（作者 2026-10-10 定的技能）。

        口径（照作者原话实现）：
          · **任何原因出局都能带**（被刀 / 被毒 / 被梦死 / 被奶穿 / 被放逐 / 被枪带走 / 自爆）——
            作者明确**被毒也能开枪**，推翻了"保留官方例外"的建议；
          · **不能带走狼队友**（见 `king_targets`）；
          · **公开指定**（与猎人开枪一致：谁带走了谁，进公开台账）；
          · 定位：补「首夜无刀」缺的那一刀 —— 只要他不是最后一个走，某种意义上等于**两刀**；
            但**最后一个走时收益很窄**（必须精准带走某一边最后一张才算值），
            所以狼队不能无脑护狼王，要按局势决定他是冲锋还是藏。

        ⚠️⚠️ **调用它的人必须保证这条在 `check_win()` 之前执行**（§56 的结算次序：
        死亡结算 → 狼王带人 → 再判屠边）。否则"好人把狼王票出去 → 立刻判好人胜"
        会把他临死那一口吃掉 —— 而这恰恰是新板子最要紧的一步棋。
        （`edge_order=sides_first` 保证"两边同时成立时算狼胜"，所以只要带人发生在判胜之前，
          "狼王带走某一边最后一张好人牌"就真的能算狼队屠边成功。）

        `prefetched`：自爆那一路**并进同一份决策**的目标（见 `decide_for_speech` /
        `wolf_explode_round`）—— 给了合法目标就不再额外花一次调用（作者要求：避免额外调用）。
        """
        if king is None or king.alive or king.king_done:
            return
        king.king_done = True                       # 先落闸：四条死亡路径都不许重复触发
        targets = self.king_targets(king)
        if not targets:
            self.ledger.add(self.day, "day", "announce",
                            f"{king.id}号 亮出【狼王】身份，但场上已经没有可带走的好人（只剩狼）。")
            return
        target = None
        if prefetched is not None:
            target = self.coerce_seat(prefetched, targets, default=None)
        if target is None:
            task = Task(
                kind="wolf_king_take",
                text=(f"{where}。你是**狼王**：出局时你可以**当场带走一名好人**"
                      f"（**不能带走狼队友**，所以给你的名单里只有好人）。"
                      f"带谁要**公开指定**（全场都会看到你带走了谁）；也可以选择不带"
                      f"（`take` 填 null）。"),
                schema='{"take": <玩家编号或 null>, "reason": "<一句话理由>"}',
                legal=targets)
            reply = await self.act(king, task)
            if truthy(reply.get("abstain"), default=False):
                self.ledger.add(self.day, "day", "announce",
                                f"{king.id}号 亮出【狼王】身份，但没有带走任何人。")
                return
            # 兼容三种写法（模型/剧本/前端各写过一种）：take 是正式字段，另两个是历史写法
            raw = reply.get("take")
            if raw in (None, "", "null", "None"):
                raw = reply.get("shoot")
            if raw in (None, "", "null", "None"):
                raw = reply.get("target")
            target = self.coerce_seat(raw, targets, default=None)
        if target is None:
            # 没给出有效目标：**不替它随机带走一个好人**（和猎人"不开枪"同一个纪律）——
            # 随机的那一口可能直接决定胜负，不是引擎该替它做的决定。
            self.ledger.add(self.day, "day", "announce",
                            f"{king.id}号 亮出【狼王】身份，但没有给出要带走的玩家（这一手作废）。")
            return
        if daytime:
            self.ledger.add(self.day, "day", "announce",
                            f"{king.id}号 亮出【狼王】身份，带走 {target}号 —— "
                            f"{target}号 的死因公示：被狼王带走。")
        else:
            self.ledger.add(self.day, "day", "announce",
                            f"{king.id}号 亮出【狼王】身份，带走 {target}号（夜里出局，不公示死因）。")
        if not self.kill(target, "king"):
            # 愚者翻牌免疫：没有被带走，不进入遗言/警徽链。
            return
        # 被带走的人有没有遗言（与 hunter_shoot 同口径）：
        #   · **白天**被带走 → 有遗言（跟"白天那一枪"一样）；
        #   · **第一夜**被带走 → 也算第一夜的死者，照样有遗言（第 2 夜起就没有了）。
        if daytime or self.day == 1:
            await self.last_words(self.seat_by_id(target))
        await self.handle_sheriff_death(self.seat_by_id(target))

    # ------------------------------------------------------------------ #
    # 遗言（只有"白天出局"的人有）
    # ------------------------------------------------------------------ #

    async def last_words(self, seat: Seat) -> None:
        """一轮单独的遗言：全场都听得到（公开条目）。

        ⚠️ 按用户定的规则，**只有第一夜**的死者 + 白天出局的人有遗言：
        ① **第一夜**死亡的（被刀 / 被毒 / 同守同救 / **被夜里那一枪打死**）—— 全都算，
           而且都在第一天的警长竞选**之前**说完；
        ② 白天被投票放逐的；③ **白天**被猎人开枪打死的；
        ④ 第 2 夜起的一切夜间死亡（含那一枪打死的人）**都没有**遗言。
        """
        task = Task(
            kind="last_words",
            text=("你刚刚出局了，现在轮到你发表**遗言**（全场都能听到，说完就彻底退场）。"
                  "你可以亮身份自白、把掌握的信息交给好人，也可以故意说假话误导对手。"
                  "控制在 350 字以内。"),
            schema=('{"speech": "<你的遗言，350 字以内>", '
                    '"claim": "<你声称的身份，没有就写 无>"}'),
            legal=[],
            required=("speech",),
        )
        reply = await self.act(seat, task)
        if truthy(reply.get("abstain"), default=False):
            self.ledger.add(self.day, "day", "announce", f"{seat.id}号 没有留下遗言。")
            return
        speech = str(reply.get("speech") or "").strip()
        if not speech:
            # 模型没说话（或 mock 没这个词）：如实记一笔，不要替它编遗言
            self.ledger.add(self.day, "day", "announce", f"{seat.id}号 没有留下遗言。")
            return
        self.ledger.add(self.day, "day", "speech", f"【遗言】{clip_speech(speech)}", seat=seat.id)
        claim = str(reply.get("claim") or "").strip()
        if claim_allowed(speech, claim):
            self.ledger.add(self.day, "day", "claim",
                            f"{seat.id}号 在遗言里声称自己是【{claim}】。",
                            seat=seat.id, data={"claim": claim})

    # ------------------------------------------------------------------ #
    # 警长竞选 / 警徽传承（11 人及以上）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run_position(value) -> str | None:
        """把上警决策的回复归一成 `"front"` / `"back"` / `None`（不上警）。

        容忍三种写法，别让旧剧本/旧客户端直接失效：
        · `"front"`/`"back"`（新口径）；· `true`/`false`（旧的是非题：`true` = 前置位）；
        · 中文「前置位/后置位/上警/不上警」。
        """
        if value is True:
            return "front"
        if value is False or value is None:
            return None
        text = str(value).strip().lower()
        if text in ("front", "pre", "qian", "前置", "前置位", "前", "上警", "true", "yes", "y", "1"):
            return "front"
        if text in ("back", "post", "hou", "后置", "后置位", "后", "2"):
            return "back"
        return None

    async def sheriff_election(self) -> None:
        """第一天的警长竞选（**在死亡结算之后**跑）。按用户定的规则：

        · 竞选者各一轮发言、**没有投票权**；非竞选者可以投票、这一轮**不能发言**
        · 无人上警 / 全员上警 → 警徽流失
        · **凭票当选**；平票 → 平票者各再发一轮言（PK）→ 只在他们之间重投 → **再平票才流失**
        · 只有一人上警 → 直接当选（用户没写这条，按官方惯例；记在 HANDOFF §12 的决策表里）
        · 警徽算 1.5 票（第一天还没警长，所以竞选投票本身不带权重）
        """
        day = self.day
        alive_ids = self.alive_ids()
        # 说明里的人数/门槛按实际情况写：config 里能把警长强制开在 9 人局上，
        # 那时还写"11 人及以上有警长"就跟事实不符了。
        why = (f"本局 {len(self.seats)} 人，11 人及以上有警长" if len(self.seats) >= self.sheriff_min_seats
               else f"本局 {len(self.seats)} 人，配置里强制开了警长")
        self.ledger.add(day, "day", "announce",
                        f"第 1 天：警长竞选开始（{why}）。警徽在投票时算 1.5 票。")

        # --- 上警（公开：所有人都能看到谁上警；**前置/后置不公开**，只影响发言顺序）---
        candidates: list[int] = []
        front: list[int] = []     # 前置位上警：先发言
        back: list[int] = []      # 后置位上警：听完前置位再发言（总结、拉票、号召投票）
        run_order = [s.id for s in sorted(self.alive, key=lambda s: s.id)]
        for idx, seat_id in enumerate(run_order):
            seat = self.seat_by_id(seat_id)
            if not seat.alive:
                continue          # 防御：这个循环里每次 await 之前都重看一眼存活
            # ⚠️ 2026-10-10 作者口径：**本板子预言家锁定上警，不问他**（省一次调用）。
            #    理由：不上警只有一个好处「也许不会被秒爆砍死」，但 **100% 丢警徽**；
            #    本板子有**幸运儿的守护**和**摄梦人的梦守**，狼想砍死预言家得连爆几狼
            #    （理论上下限能守出两个平安夜）—— 这笔账明显不划算。
            #    ⚠️ 公开台账**不许写「因为他是预言家」**（那是泄漏），只照常写「上警」。
            if self.seer_auto_run and seat.role_key == "seer":
                candidates.append(seat.id)
                front.append(seat.id)
                self.ledger.add(day, "day", "announce",
                                f"{seat.id}号 上警（竞选警长）。", seat=seat.id)
                # ⚠️ 这段是**只给预言家本人**的私密说明。措辞必须跟板子无关：
                #    2026-10-11 前它写着「本板子有幸运儿的守护 + 摄梦人的梦守」——
                #    打到没有这两张牌的板子上（eleven_standard 等）就是**假情报**，
                #    而且和规则书里的同一句话一起漂（那份也已经改掉，见 §60）。
                self.ledger.add(day, "day", "private",
                                "（你的选择）你是**预言家**：本局**锁定上警**（前置位），"
                                "所以这一轮没有问你 —— 不上警只换来「也许不被首刀」，"
                                "但 100% 丢警徽。公开公告只写「上警」，不会写你是预言家；"
                                "**上警之后的竞选发言照常要你写**。",
                                seat=seat.id, visible_to=(f"seat:{seat.id}",))
                continue
            task = Task(
                kind="sheriff_run",
                text=("第 1 天：警长竞选。你要不要上警（竞选警长）？三选一：\n"
                      "· **前置位上警** = 你先发言（听不到别人的，但先占位、先把话立住）；\n"
                      "· **后置位上警** = 你排在**所有前置位之后**发言 —— 能听完悍跳与真预言家，"
                      "再总结、拉票、号召投票；\n"
                      "· **不上警** = 这一轮不能发言，但**可以投票**选警长。\n"
                      "上警的人这一轮**没有投票权**。\n"
                      "⚠️ 公示时只会写「X号 上警」，**不会写你是前置还是后置** —— "
                      "位置顺序到发言时才看得出来。\n"
                      "（一般口径：预言家、悍跳狼选**前置位**；其他想听完再表态的人选后置位。）"
                      + ("\n**另外：你是狼，这一轮要先回答一件事 —— 要不要现在就自爆？**"
                         "（`explode` 填 true 就直接自爆、不再回答上警的问题；"
                         "填 false 再按上面的三选一答 `run`。）\n"
                         + king_take_hint(seat)
                         if self.can_explode(seat) else "")),
                schema=('{"explode": true/false, "run": "front" | "back" | false, '
                        + KING_TAKE_SCHEMA +
                        '"reason": "<一句话理由>"}' if self.can_explode(seat) else
                        '{"run": "front" | "back" | false, "reason": "<一句话理由>"}'),
                legal=[],
            )
            reply = await self.act(seat, task)
            # ⚠️ 狼在这一轮里**先决定要不要自爆**（跟"要不要上警"同一次调用，不额外花钱）：
            #    「假设 1 号是狼，到是否上警的决策轮，决策两个问题：先决策要不要自爆？
            #     自爆的话就不用后面决策直接自爆，不自爆再决策前置位还是后置位上警。」
            if self.can_explode(seat) and truthy(reply.get("explode"), default=False):
                # 11 人板新规（作者 2026-11-03）：单狼自爆**不吞警徽**，把竞选挂起；
                # 其他板子照旧（自爆吞警徽）。
                suspended = bool(self._king_rule() and self._election_open)
                await self._apply_explosion(seat.id, day,
                                            badge_lost=(self._election_open and not suspended),
                                            take=reply.get("take"))
                if suspended:
                    # 上警轮还没走完：把"没被问过的座位"一起记下来，第二天恢复时补问，
                    # 否则候选名单会残缺（实测：1 号狼在上警决策里自爆 → 剩下的人根本没被问）
                    self._badge_suspend = {
                        "day": day, "candidates": list(candidates),
                        "front": list(front), "back": list(back), "spoken": [],
                        "pending_run": [i for i in run_order[idx + 1:]
                                        if self.seat_by_id(i).alive],
                    }
                return
            where = self._run_position(reply.get("run"))
            if where is not None:
                candidates.append(seat.id)
                (front if where == "front" else back).append(seat.id)
            # ⚠️ 公示**只写"上警/不上警"**（用户 2026-10-09 定的）：前置/后置不进公开台账，
            #    只给这个座位自己留一条私有记录（复盘看得到，别人只能从发言顺序推）。
            self.ledger.add(day, "day", "announce",
                            f"{seat.id}号 " + ("上警（竞选警长）。" if where else "不上警。"),
                            seat=seat.id)
            if where:
                self.ledger.add(day, "day", "private",
                                f"（你的选择）{'前置位' if where == 'front' else '后置位'}上警。",
                                seat=seat.id, visible_to=(f"seat:{seat.id}",))

        if not candidates:
            self._badge_lost("无人上警")
            return

        # --- 自爆窗口（用户 2026-10-08 加的变版规则）---
        # 时机：**上警名单已经确定、还没开始竞选发言**。用户原话：「问完全场 11 张牌是否上警后，
        #   然后在第一张牌上警发言之前，狼看见都有谁上警之后可以决策一次是否要自爆」。
        # 为什么只在这一点开：让狼能在"任何人的发言回合"自爆 = 一头狼多 11 次调用，太慢太贵；
        #   这里只多一轮"四狼各一次"的决策（`game.wolf_explode: false` 可关）。
        if await self.wolf_explode_round(day, candidates, front, back):
            # 自爆吞警徽：竞选直接作废、警徽流失；今天的发言与投票全部跳过，**当夜照常刀人**
            return

        if len(candidates) >= len(alive_ids):
            self._badge_lost("全员上警（没人有投票权）")
            return
        if len(candidates) == 1:
            self._elect_sheriff(candidates[0], note="只有一人上警，直接当选")
            return

        # --- 竞选发言：**前置位先、后置位后**（各自按座位号从小到大）；只有竞选者发言 ---
        exploded = await self._election_speeches(day, front, back, candidates, spoken=[])
        if exploded is not None:
            return

        await self._election_vote_and_elect(day, candidates)


    async def wolf_explode_round(self, day: int, candidates: list[int],
                                 front: list[int] | None = None,
                                 back: list[int] | None = None) -> bool:
        """狼队自爆决策（用户 2026-10-08 加的变版规则）。返回 True = 真的爆了。

        规则口径（照用户原话实现）：
          · **只有狼能爆**，而且只在「上警名单已定、竞选发言未开始」这个窗口问一次；
          · **过半数狼同意**才爆（一头狼说了不算）；
          · 谁爆：优先听狼队自己写的 `who`（多数提名者），没人指名就用**座位号最小的那头同意狼**；
          · 后果：自爆者**当场出局并亮明狼人身份**、**警徽流失**（竞选作废），
            今天的发言与投票全部跳过，**当夜照常刀人**；
          · 代价：一头狼一次决策调用（四狼 = 4 次），且只在有竞选的那一天。
          这条规则的意义（用户举的例子）：只剩一狼一预、自己位置又差时，自爆能**吞掉警徽**
          并当晚直接刀预言家 —— 预言家连一轮查验都发不出来。
        """
        self.exploded_today = None
        if not self.wolf_explode:
            return False
        wolves = [s for s in self.alive if self.can_explode(s)]   # 11 人板：狼王不参与
        if not wolves:
            return False
        wolf_ids = [s.id for s in wolves]
        ballot = "、".join(f"{i}号" for i in candidates) or "（没人上警）"

        async def one(wolf: Seat):
            task = Task(
                kind="wolf_explode",
                text=(f"第 {day} 天，警长竞选的**上警名单已经定了**：{ballot}。\n"
                      f"现在还没开始竞选发言。狼队可以决定**自爆**：\n"
                      f"· 自爆的狼**当场出局并亮明狼人身份**；\n"
                      f"· **警徽直接流失**（竞选作废，今天不再发言、不再投票）；\n"
                      f"· **今晚照常刀人** —— 所以自爆常常是「吞掉警徽 + 当晚刀掉真预言家」的打法。\n"
                      + king_take_hint(wolf)
                      + f"什么时候值得爆：只剩一狼而预言家已经上警、你的位置又容易被进狼坑；"
                      f"或者白天肯定要被推出去、想抢轮次。\n"
                      f"你要不要现在自爆？（`who` 填由谁爆，只能填活着的狼："
                      f"{'、'.join(f'{i}号' for i in wolf_ids)}）"),
                schema=('{"explode": true/false, "who": <由谁自爆的编号或 null>, '
                        + KING_TAKE_SCHEMA +
                        '"reason": "<一句理由>"}'),
                legal=wolf_ids,
                required=("explode",),
            )
            return wolf, await self.act(wolf, task)

        gathered = await asyncio.gather(*(one(w) for w in wolves), return_exceptions=True)
        yes: list[int] = []
        named: dict[int, int] = {}
        takes: dict[int, int | None] = {}
        answered = 0                      # 真的回了话的狼（下面算"过半"的分母）
        for item in gathered:
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                print(f"[engine] 自爆决策有一头狼失败：{type(item).__name__}: {item}")
                continue
            answered += 1
            wolf, reply = item
            if truthy(reply.get("explode"), default=False):
                yes.append(wolf.id)
            who = self.coerce_seat(reply.get("who"), wolf_ids, default=None)
            if who is not None:
                named[who] = named.get(who, 0) + 1
            # 狼王自爆的那一口**并进这一次决策**（作者要求：避免额外调用）——
            # 目标合法性在 wolf_king_take 里再按"不能带队友"过滤一次，这里只做粗筛。
            takes[wolf.id] = self.coerce_seat(reply.get("take"),
                                              [i for i in self.alive_ids() if i != wolf.id],
                                              default=None)

        lines = "，".join(f"{w.id}号→{'爆' if w.id in yes else '不爆'}" for w in sorted(wolves, key=lambda s: s.id))
        # ⚠️ 2026-10-31 外部审查 E11：分母以前固定是 `len(wolves)`。有一头狼的调用失败时
        #    它掉出了分母以外的计数（没进 yes），**却是照样被算进分母的** —— 等于静默把
        #    "过半"的门槛抬高了。现在分母只算"真的回了话的狼"（`answered`）。
        base = max(1, answered)
        # 11 人板新规：自爆只"挂起竞选"，不吞警徽（双爆才吞）
        suspended = bool(self._king_rule())
        if len(yes) * 2 <= base:                   # 没过半：不爆
            self.ledger.add(day, "day", "private",
                            f"狼队自爆决策：{lines}。没过半 → 不自爆。", visible_to=(WOLF,))
            return False

        # ⚠️ 2026-10-30 修：**只从"同意爆"的狼里挑人**。以前只看提名多数，
        #    被提名最多的那只狼就算自己投了「不爆」也会被强制出局 —— 与"每头狼自己
        #    决定要不要爆"的口径冲突。现在取「提名 ∩ 同意」里票最多的；
        #    交集为空就取同意者里编号最小的。
        agreed_named = {k: v for k, v in named.items() if k in yes}
        if agreed_named:
            top = max(agreed_named.values())
            who = min(k for k, v in agreed_named.items() if v == top)
        else:
            who = min(yes)
        self.ledger.add(day, "day", "private",
                        f"狼队自爆决策：{lines}。过半 → 由 {who}号 自爆。", visible_to=(WOLF,))
        await self._apply_explosion(who, day, badge_lost=not suspended, take=takes.get(who))
        if suspended:
            self._badge_suspend = {"day": day, "candidates": list(candidates),
                                   "front": list(front or []), "back": list(back or []),
                                   "spoken": []}
        return True

    def _king_rule(self) -> bool:
        """11 人狼王板专属新规（作者 2026-11-03 定）：狼王不能自爆 + 双爆才吞警徽。

        只对 `eleven_wolfking` 生效；其他板子保持原规则（自爆吞警徽、狼王能爆）。
        """
        return str(getattr(self.board_def, "name", "")) == "eleven_wolfking"

    def can_explode(self, seat) -> bool:
        """这个座位能不能自爆：狼；11 人板上的**狼王不能**。"""
        if seat.camp != WOLF_CAMP:
            return False
        return not (self._king_rule() and seat.role_key == "wolf_king")

    async def double_explode_round(self, day: int) -> bool:
        """第二天白天：剩下的狼决定`要不要再自爆一次`。返回 True = 双爆成立。

        作者 2026-11-03 的口径（11 人板专属）：
          · 第一只狼自爆后**不吞警徽**，先过夜；第二天公布夜里可公开信息（谁出局/平安夜）；
          · 然后剩下的狼**各调用一次**决策要不要再爆（狼王不能爆，不参与）；
          · 过半同意 → **双爆吞警徽**（本局没有警长），当天也到此为止；
          · 不同意 → **恢复竞选**：已发过言的不重复，然后投票决定警徽。
        """
        wolves = [s for s in self.alive if self.can_explode(s)]
        if not wolves or not self.wolf_explode:
            return False
        ids = [s.id for s in wolves]

        async def one(wolf: Seat):
            task = Task(
                kind="wolf_explode",
                text=(f"第 {day} 天：昨天已经有一只狼自爆，警长竞选被挂起、**警徽还没吞**。\n"
                      "现在轮到狼队决定：**要不要再来一次自爆**？\n"
                      "· 双爆（第二只狼也自爆）→ **警徽被吞**，本局没有警长，今天也就此结束；\n"
                      "· 不爆 → 竞选恢复（第一天已发过言的人不再重复），之后照常投票定警徽。\n"
                      "只算一次决策，别为这轮多花调用。"),
                schema='{"explode": true/false, "reason": "<一句理由>"}',
                legal=ids, required=("explode",), extra={"double_explode": True})
            return wolf, await self.act(wolf, task)

        gathered = await asyncio.gather(*(one(w) for w in wolves), return_exceptions=True)
        yes: list[int] = []
        answered = 0
        for item in gathered:
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                continue
            answered += 1
            wolf, reply = item
            if truthy(reply.get("explode"), default=False):
                yes.append(wolf.id)
        lines = "，".join(f"{i}号" for i in ids)
        if answered == 0:
            self.ledger.add(day, "day", "announce",
                            "双爆轮没能拿到狼队的回答（调用失败），按「不继续自爆」处理 → 警长竞选恢复。")
            return False
        if len(yes) * 2 <= max(1, answered):
            self.ledger.add(day, "day", "announce",
                            f"昨天有人自爆后，狼队没有继续自爆（{lines} 都选择不爆）→ 警长竞选恢复。")
            return False
        who = min(yes)
        await self._apply_explosion(who, day, badge_lost=True)
        self.ledger.add(day, "day", "announce",
                        "**双狼自爆**：警徽被吞，本局不再有警长；今天到此为止，天黑后照常行动。")
        self._badge_suspend = None
        return True

    async def resume_badge_election(self, day: int) -> bool:
        """恢复被单狼自爆挂起的警长竞选。返回 True = 当天到此为止（双爆了）。

        恢复要补两件事：
          ① 如果第一只狼是在**上警轮里**爆的，当时还没表态的人要补问（否则候选名单残缺）；
          ② 竞选发言只补没发过言的人，然后投票定警徽。
        """
        state = self._badge_suspend
        if not state:
            return False
        self._badge_suspend = None
        if await self.double_explode_round(day):
            return True

        cands = [i for i in (state.get("candidates") or []) if self.seat_by_id(i).alive]
        front = [i for i in (state.get("front") or []) if i in cands]
        back = [i for i in (state.get("back") or []) if i in cands]
        spoken = [i for i in (state.get("spoken") or []) if i in cands]

        # ① 补完被打断的"上警"半轮
        pending_run = [i for i in (state.get("pending_run") or [])
                       if self.seat_by_id(i).alive]
        if pending_run:
            self.ledger.add(day, "day", "announce",
                            "恢复上警阶段：第 1 天还没表态的人继续决定要不要上警"
                            "（" + "、".join(f"{i}号" for i in pending_run) + "）。")
            for seat_id in pending_run:
                seat = self.seat_by_id(seat_id)
                if not seat.alive:
                    continue
                reply = await self._ask_run_again(seat)
                if self.can_explode(seat) and truthy(reply.get("explode"), default=False):
                    # 补问期间又爆 = 第二只狼 → 双爆吞警徽
                    await self._apply_explosion(seat.id, day, badge_lost=True,
                                                take=reply.get("take"))
                    self.ledger.add(day, "day", "announce",
                                    "**双狼自爆**：警徽被吞，本局不再有警长；今天到此为止。")
                    return True
                where = self._run_position(reply.get("run"))
                if where is not None:
                    cands.append(seat.id)
                    (front if where == "front" else back).append(seat.id)
                self.ledger.add(day, "day", "announce",
                                f"{seat.id}号 " + ("上警（补）。" if where else "不上警（补）。"),
                                seat=seat.id)

        if not cands:
            self._badge_lost("恢复竞选时已没有候选人")
            return False
        self.ledger.add(day, "day", "announce",
                        f"警长竞选恢复（第 1 天被自爆打断）：候选人 "
                        + "、".join(f"{i}号" for i in cands)
                        + ("；已发过言的不再重复。" if spoken else "。"))
        self._election_open = True
        try:
            exploded = await self._election_speeches(day, front, back, cands, spoken=spoken,
                                                     double_explode=True)
            if exploded is not None:
                return True
            if len(cands) >= len(self.alive_ids()):
                self._badge_lost("全员上警（没人有投票权）")
                return False
            if len(cands) == 1:
                self._elect_sheriff(cands[0], note="只有一人上警，直接当选")
                return False
            await self._election_vote_and_elect(day, cands)
        finally:
            self._election_open = False
        return False

    async def _ask_run_again(self, seat) -> dict:
        """恢复竞选时补问一次"要不要上警"（只问没被问过的人）。"""
        can_ex = self.can_explode(seat)
        task = Task(
            kind="sheriff_run",
            text=("第 2 天：昨天警长竞选被自爆打断，现在**补问上警**：你要不要上警？\n"
                  "· **前置位上警** / **后置位上警** / **不上警**（不上警也能投票选警长）。\n"
                  + ("**你是狼：先回答要不要再自爆** —— 再爆 = 双狼自爆，警徽被吞（`explode`）。\n"
                     if can_ex else "")),
            schema=('{"explode": true/false, "run": "front" | "back" | false, '
                    + KING_TAKE_SCHEMA + '"reason": "<一句理由>"}' if can_ex else
                    '{"run": "front" | "back" | false, "reason": "<一句理由>"}'),
            legal=[],
        )
        return await self.act(seat, task)

    async def _apply_explosion(self, who: int, day: int, badge_lost: bool,
                               take: int | None = None) -> None:
        """把"某头狼自爆"这件事**落地**（团队决策与单人决策共用这一段）。

        `badge_lost`：只有"竞选还没结束就爆"才吞警徽（用户口径：自爆吞警徽）。
        白天中途（警长已经选出来了）再爆，警徽不受影响 —— 那不属于"吞警徽"的场合。

        `take`：**狼王自爆 = 白狼王式**（作者 2026-10-10）：亮身份 → **当场带走一人** → 直接进黑夜。
        这个目标通常来自他自爆的**同一次决策**（`decide_for_speech` 的 `take` /
        `wolf_explode_round` 收集的 `take`）—— 给了就不再额外花一次调用。
        ⚠️ 带人必须在下面那句 `check_win()` **之前**：自爆的狼王可能就是最后一狼，
        先判胜负就把他那一口吃掉（§56 的结算次序）。

        注意：这个方法**是 async** 的（自爆可能触发狼王带人 → 可能要问模型）。
        自爆后**没有发言、没有遗言、不投票**，直接进夜；人类局那种爆后唠嗑已删。
        """
        victim = self.seat_by_id(who)
        victim.alive = False
        victim.death_day = day
        victim.death_cause = "explode"
        self.exploded_today = who
        tail = ("警长竞选作废、警徽流失；今天的发言与投票全部跳过，天黑后照常行动。"
                if badge_lost else
                "今天的发言与投票全部跳过，天黑后照常行动（警徽不受影响）。")
        self.ledger.add(day, "day", "announce",
                        f"{who}号 **自爆**（亮出狼人身份，当场出局）。{tail}")
        if victim.mind is not None:
            victim.mind = self._append_mind(victim.mind, f"第 {day} 天我自爆了。")
        self.emit("death", seat=who, cause="explode")   # 让服务端补一条 status（顶部划掉）
        if victim.role_key == "wolf_king":
            await self.wolf_king_take(victim, where="你自爆了", daytime=True, prefetched=take)
        # ⚠️ 2026-10-31 外部审查 P1-08：以前这里完全不管警徽 —— 带警徽的狼自爆后
        #    警长一直停在尸体上，既没有传承也没有「警徽流失」公告，1.5 票权静默消失。
        #    另外三条死亡路径（夜里 / 被票 / 被枪）都调了 handle_sheriff_death，
        #    注释也写着「警徽按规则随它出局而传承/撕掉」，只有自爆漏了。
        if int(self.sheriff_id or 0) == victim.id:
            await self.handle_sheriff_death(victim)
        self.check_win()

    async def _election_speeches(self, day: int, front: list[int], back: list[int],
                                 candidates: list[int],
                                 spoken: list[int] | None = None,
                                 double_explode: bool = False) -> int | None:
        """竞选发言（前置位先、后置位后）。返回自爆的座位号；没爆返回 None。

        2026-11-03：抽出来给"双爆挂起后恢复竞选"复用。`spoken` 是**已经发过言**的人
        （作者口径：第一天已发过的，恢复时不再重复）。狼在这里第一次自爆时，
        11 人板把竞选挂起而不是吞警徽 —— 由调用方（sheriff_election / resume）决定。
        """
        already = set(spoken or [])
        done = list(already)
        for seat_id in sorted(front) + sorted(back):
            if seat_id in already:
                continue
            seat = self.seat_by_id(seat_id)
            if not seat.alive:
                continue
            is_wolf = self.can_explode(seat)
            task = Task(
                kind="sheriff_speech",
                text=(("你是警长候选人（**前置位**：你是最先说的几个人之一），"
                       "现在轮到你发表竞选发言（全场都能听到）。")
                      if seat_id in front else
                      ("你是警长候选人（**后置位**：前面的人已经说完了），"
                       "现在轮到你发言 —— 可以总结前面的发言、拉票、号召大家把票投给谁。"))
                     + "说说你当上警长后会怎么带队、你的判断，或者亮出你的身份。350 字以内。\n"
                       "⚠️ 时间线：报夜间信息一律用「**昨晚**我验的 X 号 / 昨晚被刀的是 X 号」；"
                       "**不要**拿今天才公开的东西（谁上警、前置/后置位、今天的发言）当昨晚的理由。"
                     + ("\n**你是狼：先回答一件事 —— 这一轮要不要自爆？**"
                        "（`explode` 填 true 就直接自爆、不再发这段言、**没有爆后发言/遗言**；填 false 再写 `speech`。）\n"
                        + king_take_hint(seat)
                        if is_wolf else ""),
                schema=('{"explode": true/false, "speech": "<你的竞选发言，350 字以内>", '
                        + KING_TAKE_SCHEMA +
                        '"claim": "<你声称的身份，没有就写 无>", "reason": "<你的真实想法>"}'
                        if is_wolf else
                        '{"speech": "<你的竞选发言，350 字以内>", '
                        '"claim": "<你声称的身份，没有就写 无>", "reason": "<你的真实想法>"}'),
                legal=[i for i in self.alive_ids() if i != seat_id],
                required=("speech",),
                extra={"explode_exempt": True} if is_wolf else None,
            )
            reply = await self.act(seat, task)
            if is_wolf and truthy(reply.get("explode"), default=False):
                # 恢复竞选期间再爆 = **第二只狼** → 双爆吞警徽（2026-11-03 审查 P1）
                if double_explode:
                    await self._apply_explosion(seat.id, day, badge_lost=True,
                                                take=reply.get("take"))
                    self.ledger.add(day, "day", "announce",
                                    "**双狼自爆**：警徽被吞，本局不再有警长；今天到此为止。")
                    return seat.id
                suspended = bool(self._king_rule() and self._election_open)
                await self._apply_explosion(seat.id, day,
                                            badge_lost=(self._election_open and not suspended),
                                            take=reply.get("take"))
                if suspended:
                    self._badge_suspend = {"day": day, "candidates": list(candidates),
                                           "front": list(front), "back": list(back),
                                           "spoken": list(done)}
                return seat.id
            speech = str(reply.get("speech") or "").strip() or "（我没什么要说的，大家随便投。）"
            self.ledger.add(day, "day", "speech", f"【竞选】{clip_speech(speech)}", seat=seat.id)
            claim = str(reply.get("claim") or "").strip()
            if claim_allowed(speech, claim):
                self.ledger.add(day, "day", "claim",
                                f"{seat.id}号 在竞选发言里声称自己是【{claim}】。",
                                seat=seat.id, data={"claim": claim})
            if reply.get("reason"):
                seat.mind = self._append_mind(
                    seat.mind, f"第 {day} 天竞选警长的真实想法：{str(reply['reason'])[:200]}")
            done.append(seat_id)
        return None


    async def _election_vote_and_elect(self, day: int, candidates: list[int]) -> None:
        """竞选投票 + 平票 PK + 选出警长（原 `sheriff_election` 的后半段，抽出来复用）。"""
        voters = [s for s in self.alive if s.id not in candidates]
        votes = await self._election_votes(candidates, voters, round_no=1)
        counts = Counter(t for t in votes.values() if t is not None)
        if not counts:
            self._badge_lost("竞选投票没有有效票")
            return
        best = max(counts.values())
        leaders = [k for k, v in counts.items() if v == best]

        # --- 平票 → 平票者各再发一轮言（PK）→ 只在他们之间重投 → **仍平票才流失** ---
        # （用户规则：竞选凭票，打一轮发言 PK 再平票才流失）
        if len(leaders) > 1:
            tied = "、".join(f"{i}号" for i in leaders)
            self.ledger.add(day, "day", "announce",
                            f"警长竞选平票（{tied} 各 {best} 票），进入 PK："
                            f"平票者各再发言一轮，然后只在他们之间重投；再平票才警徽流失。")
            await self._election_pk_speeches(leaders)
            second = await self._election_votes(leaders, voters, round_no=2)
            counts2 = Counter(t for t in second.values() if t is not None)
            if not counts2:
                self._badge_lost("竞选 PK 轮没有有效票")
                return
            best2 = max(counts2.values())
            leaders2 = [k for k, v in counts2.items() if v == best2]
            if len(leaders2) > 1:
                self._badge_lost("竞选 PK 仍然平票（"
                                 + "、".join(f"{i}号" for i in leaders2) + "）")
                return
            self._elect_sheriff(leaders2[0], note=f"竞选 PK 后得 {best2} 票")
            return
        self._elect_sheriff(leaders[0], note=f"竞选投票得 {best} 票")

    async def _election_votes(self, candidates: list[int], voters: list[Seat],
                              round_no: int = 1) -> dict[int, int | None]:
        """警长竞选的投票（只有非竞选者能投；PK 轮只能在平票的候选人里选）。"""
        pk = round_no > 1

        async def one_vote(voter: Seat):
            legal = [i for i in candidates if i != voter.id] or list(candidates)
            task = Task(kind="sheriff_vote",
                        text=("**这一票是警徽票**（PK 轮：只能在平票的候选人里选）" if pk
                              else "**这一票是警徽票**")
                             + "：请从竞选者里选一个你想让他当警长的人。",
                        schema='{"vote": <玩家编号>, "reason": "<一句话理由>"}',
                        legal=legal, required=("vote",))
            return voter, legal, await self.act(voter, task)

        gathered = await asyncio.gather(*(one_vote(s) for s in voters), return_exceptions=True)
        votes: dict[int, int | None] = {}
        invalid: set[int] = set()        # 票面写了但不算的座位（见下面 E6 那段注释）
        tag = "（PK 轮）" if pk else ""
        for item in gathered:
            if isinstance(item, BaseException):
                if isinstance(item, GameAborted):
                    raise item
                continue
            voter, legal, reply = item
            if truthy(reply.get("abstain"), default=False):
                votes[voter.id] = None
                self.ledger.add(self.day, "day", "vote", f"（警长竞选{tag}）弃权", seat=voter.id,
                                data={"abstain": True, "sheriff_vote": True, "round": round_no})
                continue
            # ⚠️ 2026-10-31 外部审查 E6：竞选票以前是裸 `coerce_seat(..., legal)` ——
            #    写糊/写死人/写候选人以外的人就**静默随机改投**，而放逐投票那边
            #    （run_vote）早就改成"记无效票"了。两份投票逻辑不该有两套纪律。
            raw = self.coerce_seat(reply.get("vote"), legal, default=None)
            if raw is None:
                votes[voter.id] = None
                invalid.add(voter.id)
                self.ledger.add(self.day, "day", "vote",
                                f"（警长竞选{tag}）票面写的是 {reply.get('vote')!r}"
                                f"（不是能投的候选人）—— 这一票无效",
                                seat=voter.id,
                                data={"invalid": True, "sheriff_vote": True, "round": round_no})
                continue
            votes[voter.id] = raw
            self.ledger.add(self.day, "day", "vote", f"（警长竞选{tag}）投给 {raw}号",
                            seat=voter.id,
                            data={"target": raw, "sheriff_vote": True, "round": round_no})
        parts = []
        for sid, t in sorted(votes.items()):
            if t is not None:
                shown = f"{t}号"
            elif sid in invalid:
                shown = "无效票"
            else:
                shown = "弃权"
            parts.append(f"{sid}号→{shown}")
        counts = Counter(t for t in votes.values() if t is not None)
        tally = "、".join(f"{k}号 {v} 票" for k, v in counts.most_common()) or "没有有效票"
        self.ledger.add(self.day, "day", "announce",
                        f"警长竞选投票{tag}（只有非竞选者能投）：{'，'.join(parts) or '没人投'}。　"
                        f"计票：{tally}。")
        return votes

    async def _election_pk_speeches(self, tied: list[int]) -> None:
        """竞选平票后的 PK 发言（只有平票的那几个候选人发言）。"""
        tie_text = "、".join(f"{i}号" for i in tied)
        for seat_id in sorted(tied):
            seat = self.seat_by_id(seat_id)
            if not seat.alive:
                continue          # 防御：跟 pk_speeches 一样，await 之前重看一眼存活
            task = Task(
                kind="sheriff_speech",
                text=(f"警长竞选你和平票了（{tie_text}）。请再发一轮言：说明为什么该选你、"
                      f"或者指出对手的问题，说服大家改投你。300 字以内。"),
                schema=('{"speech": "<你的竞选 PK 发言，300 字以内>", '
                        '"claim": "<你声称的身份，没有就写 无>", "reason": "<你的真实想法>"}'),
                legal=[i for i in tied if i != seat_id],
                required=("speech",),
            )
            reply = await self.act(seat, task)
            speech = str(reply.get("speech") or "").strip() or "（PK 轮我没什么要补的。）"
            self.ledger.add(self.day, "day", "speech", f"【竞选PK】{clip_speech(speech)}", seat=seat.id)
            claim = str(reply.get("claim") or "").strip()
            if claim_allowed(speech, claim):
                self.ledger.add(self.day, "day", "claim",
                                f"{seat.id}号 在竞选 PK 发言里声称自己是【{claim}】。",
                                seat=seat.id, data={"claim": claim})

    def _elect_sheriff(self, seat_id: int, note: str = "") -> None:
        self.sheriff_id = seat_id
        seat = self.seat_by_id(seat_id)
        tail = f"（{note}）" if note else ""
        self.ledger.add(self.day, "day", "announce",
                        f"{seat_id}号 当选警长{tail}。警徽在他手上：投票时他的一票算 1.5 票。")
        seat.private_events.append(
            f"第 {self.day} 天你当选了警长{tail}：投票时你的一票算 1.5 票；"
            f"你出局时可以选择把警徽传给一名存活玩家，或者撕掉它。")
        self.emit("sheriff", seat=seat_id, action="elected", day=self.day)

    def _badge_lost(self, why: str) -> None:
        self.sheriff_id = None
        self.ledger.add(self.day, "day", "announce", f"警徽流失：{why}，本局没有警长。")
        self.emit("sheriff", seat=None, action="lost", day=self.day)

    async def handle_sheriff_death(self, seat: Seat) -> None:
        """警长死了（被放逐 / 被刀 / 被枪）：让他决定把警徽传给谁，或者撕掉。

        用户定的规则："警长死后，不管是被放逐还是被刀死，都可以选择传承警徽或者是丢掉警徽。"
        """
        if self.sheriff_id != seat.id:
            return
        # 警徽可以传给任何存活玩家 —— **包括已经翻牌的愚者**。
        # （曾经按"他没投票权、1.5 票会白扔"的理由把他排除在名单外，用户明确否决了这条房规：
        #   通行规则允许传给任何人，接不接、值不值是玩家自己的判断，引擎不替他做决定。）
        legal = [i for i in self.alive_ids() if i != seat.id]
        if not legal:
            self._badge_lost(f"{seat.id}号 出局时没有可传承的存活玩家")
            return
        task = Task(
            kind="badge",
            text=("你是警长，现在你出局了。你可以把警徽传给一名存活玩家"
                  "（他会成为新警长，票算 1.5），也可以撕掉警徽（本局从此没有警长）。\n"
                  "先按你此前的身份层级判断：\n"
                  "· 好人警长：有铁好/身份偏好就优先飞给他；如果判断剩下的存活者全是偏狼/偏坏，"
                  "可以考虑撕（撕会掉一点好人面），能飞相对最稳的人时优先飞。\n"
                  "· 狼警长：没有铁好时，可以传狼队友、撕掉、或飞一个偏好/偏坏的好人继续乱视角；"
                  "有铁好活着时，撕 = 认狼、暴露队形，真飞铁好能一定程度做高自己/掩护队友，"
                  "但你已经到飞警徽这一步，收益要按局面判断。"),
            schema='{"to": <玩家编号>, "reason": "<一句话理由>"}；撕掉警徽就写 {"to": null}',
            legal=legal,
        )
        reply = await self.act(seat, task)
        target = self.coerce_seat(reply.get("to"), legal, default=None)
        if not target:
            self.sheriff_id = None
            self.ledger.add(self.day, "day", "announce",
                            f"{seat.id}号 撕掉了警徽 —— 本局从此没有警长。")
            self.emit("sheriff", seat=None, action="discarded", day=self.day)
            return
        self.sheriff_id = target
        self.ledger.add(self.day, "day", "announce",
                        f"{seat.id}号 把警徽传给了 {target}号 —— {target}号 成为新警长，"
                        f"他的一票算 1.5 票。")
        self.seat_by_id(target).private_events.append(
            f"第 {self.day} 天 {seat.id}号 把警徽传给了你：你现在是警长，"
            f"投票时你的一票算 1.5 票；你出局时可以选择把警徽传给一名存活玩家，或者撕掉它。")
        self.emit("sheriff", seat=target, action="transferred", day=self.day)

    # ------------------------------------------------------------------ #
    # 出局与胜负
    # ------------------------------------------------------------------ #

    def _idiot_reveal(self, seat: Seat, *, cause: str = "") -> None:
        """愚者第一次受到任何致命结算：翻牌免疫，之后仍可投票（0.5 票）。"""
        seat.revealed_idiot = True
        self.ledger.add(self.day, "day", "announce",
                        f"{seat.id}号 翻开牌亮出【愚者】身份，免疫本次出局；"
                        f"他仍然可以投票，票权为 0.5 票。")
        seat.private_events.append(
            f"第 {self.day} 天你受到致命结算（{CAUSE_TEXT.get(cause, cause)}），"
            f"翻开【愚者】牌免疫了这次出局；你仍然可以投票，票权 0.5。")
        self.emit("idiot_reveal", seat=seat.id, day=self.day)

    def kill(self, seat_id: int, cause: str, reason: str = "", reveal: bool = False) -> bool:
        """让座位出局；返回 True=真的死了，False=愚者翻牌免疫。

        `reveal=True` 只用于骑士审判/特殊公示：立即公开身份牌（暗牌板也公开）。
        """
        seat = self.seat_by_id(seat_id)
        if not seat.alive:
            return False
        if seat.role_key == "idiot" and not seat.revealed_idiot:
            self._idiot_reveal(seat, cause=cause)
            return False
        seat.alive = False
        # ⚠️ 这里**不许**写 `last_exiled`：`kill()` 服务所有死因（夜里被刀/被毒/被枪/被放逐），
        #    而"上一个被放逐的人"只有 `exile()` 知道 —— 见那个函数里的赋值。
        seat.death_day = self.day
        seat.death_cause = cause
        # 审计（借 DND 的 commit(actor, reason)）：只进存档/复盘，**不发事件、不进任何座位包**。
        # 不用 ledger.add：每加一条都会 emit("ledger") 推给前端，engine 档也拦不住。
        self.audit.append({
            "day": self.day, "phase": self.phase, "seat": seat_id,
            "kind": "death", "cause": cause,
            "reason": reason or CAUSE_TEXT.get(cause, cause),
        })
        seat.private_events.append(f"第 {self.day} 天你出局了（{CAUSE_TEXT.get(cause, cause)}）。")
        # ── 出局亮身份（板子级开关 `reveal_on_death`：目前只有「6 人屠城明牌」开着）──
        # 只亮**身份牌**、照旧不公示死因 —— 所以这条公告和"昨晚出局：X 号（不公布死因）"
        # 不冲突：那条说的是怎么死的，这条说的是他是谁。
        # ⚠️ 放在 `emit("death")` 之前：手机端先看到公告、再刷新座位条。
        if self.reveal_on_death or reveal:
            self.ledger.add(self.day, "day", "announce",
                            f"{seat_id}号 出局，亮明身份：【{role_name(seat.role_key)}】。")
        self.emit("death", seat=seat_id, cause=cause, day=self.day)
        return True

    def good_can_still_kill(self) -> bool:
        """好人手里还有没有"能在夜里弄死狼"的牌（控场判定的另一半）。

        作者 2026-10-25 定稿：**"活着但已经没牌"不算反击能力**。具体：
          · **女巫** —— 只有**毒药还在**才算（解药是保命、不杀人）；
          · **猎人** —— 活着就有枪（枪是被动的，活着就说明还没开过）；
          · **摄梦人** —— 连摄两晚能杀人；
          · **幸运儿** —— 一次性技能**还没用掉**才算（用掉后这一局再也杀不了人）。
        不算的：预言家（验人不杀人）、守卫 / 愚者 / 守墓人 / 养鸦人（保护、票、或信息）。

        ⚠️ 2026-10-25 收紧：以前 `seer/witch/hunter/dreamer` 只要**活着**就算，
        女巫两瓶药都用完也算 —— 于是"空药女巫 + 只剩验人的预言家"会被判成"好人还能反击"，
        残局迟迟不结束。现在只认真正还能造成夜间死亡的牌。
        """
        for s in self.alive:
            if s.camp == WOLF_CAMP:
                continue
            if s.role_key == "witch":
                if self.witch_poison:
                    return True
            elif s.role_key in ("hunter", "dreamer"):
                return True
            elif s.role_key == "knight" and not s.knight_used:
                return True
            if s.role_key == "lucky" and (s.lucky_bullet or not s.lucky_used):
                return True
        return False

    def _sides_done(self, good: list) -> str:
        """屠边是否已经完成？返回 ""（没完成）或原因文本。白痴两边都算。"""
        gods = [s for s in good if s.kind == "god" or s.role.also_kind == "god"]
        villagers = [s for s in good if s.kind == "villager" or s.role.also_kind == "villager"]
        if not gods:
            return "神职全部出局"
        if not villagers:
            return "村民全部出局"
        return ""

    def _exile_skips_last_words(self, seat: Seat) -> bool:
        """这次放逐是否已经定死胜负、不需要再发遗言调用。

        ⚠️ 2026-11-06 作者口径：最后一狼被放逐时，如果对局已经不可能翻盘，
        不要再发一轮**遗言**调用白花时间。判据：
          · 放逐后场上已没有任何狼 → 好人胜；
          · 被放逐的是狼王时，还要看他那一枪能不能改变胜负：本局按“屠边”算，
            只有**神、民两边都至少还剩 2 人**时，他带走任意一人都不可能屠边，
            此时跳过遗言；只要有一边只剩 1 人，就仍然给遗言让他交代。
        ⚠️ 狼王那一枪仍按规则结算（不跳过），因为它是“出局技能”，不是“语言调用”；
           如果连技能也跳过，考题/规则口径会从“能带”变成“不能带”。
        """
        if any(s.kind == "wolf" for s in self.alive):
            return False                       # 还有狼，不是最后一狼
        if seat.role_key != "wolf_king":
            return True                        # 最后一狼不是狼王：直接好人胜
        if self.win_rule != "sides":
            return True                        # 屠城板：狼全死就是好人胜
        good = [s for s in self.alive if s.kind != "wolf"]
        gods = [s for s in good if s.kind == "god" or s.role.also_kind == "god"]
        villagers = [s for s in good if s.kind == "villager" or s.role.also_kind == "villager"]
        # 两边都 ≥2，狼王带走任意一张都屠不了边 → 不必再给遗言。
        return len(gods) >= 2 and len(villagers) >= 2

    def check_win(self) -> str | None:
        if self.winner:
            return self.winner
        wolves = [s for s in self.alive if s.kind == "wolf"]
        good = [s for s in self.alive if s.kind != "wolf"]

        # ── 边界口径：**同一夜里"狼屠边完成"与"最后一狼被抓出来"同时成立**时算谁赢 ──
        # 典型两种：① 狼刀掉最后的神（比如最后一个猎人），猎人死前开枪带走最后一狼；
        #           ② 狼刀掉最后的女巫，女巫那一夜毒走最后一狼。
        # 用户定的口径是**屠边优先 → 狼胜**（`edge_order: sides_first`）。想改成
        # "最后一狼先死 → 好人胜"，把 config 里的 `edge_order` 改成 `last_wolf_first` 即可。
        sides_reason = self._sides_done(good) if self.win_rule == "sides" else ""
        if sides_reason and self.edge_order == "sides_first":
            self._win_by_sides(sides_reason)
            return self.winner

        if not wolves:
            self.winner = "good"
            self.ledger.add(self.day, "day", "announce",
                            f"全部狼人出局，好人阵营获胜（第 {self.day} 天）。")
            return self.winner
        if self.wolf_control and len(wolves) >= len(good) and not self.good_can_still_kill():
            # 控场：好人的票再也投不出狼（狼数 ≥ 好人数），**而且**手里已经没有
            # 能在夜里弄死狼的牌了 —— 继续打只是被磨死。
            # 反过来，只要还有预言家/女巫/猎人/**摄梦人**/幸运儿（技能没用）活着，
            # 就给他们最后一夜的机会（2026-10-11 扩表，见 good_can_still_kill）。
            self.winner = "wolf"
            self.ledger.add(self.day, "day", "announce",
                            f"狼人已控场（存活狼 {len(wolves)} 人 ≥ 好人 {len(good)} 人，"
                            f"且好人已无夜间反击能力），狼人阵营获胜（第 {self.day} 天）。")
            return self.winner
        if self.win_rule == "all":
            if not good:
                self.winner = "wolf"
                self.ledger.add(self.day, "day", "announce",
                                f"好人全部出局，狼人阵营获胜（第 {self.day} 天）。")
            return self.winner
        if sides_reason:                      # 走到这里说明口径是"最后一狼先死 → 好人胜"
            self._win_by_sides(sides_reason)
        return self.winner

    def _win_by_sides(self, reason: str) -> None:
        self.winner = "wolf"
        # 有"既算神也算民"的角色（白痴）时，说明一句，免得看战报的人以为规则算错了
        dual = any(ROLES[r].also_kind for r in self.board_def.roles)
        note = "（本局白痴算民也算神，两边都必须连他一起出局）" if dual else ""
        self.ledger.add(self.day, "day", "announce",
                        f"{reason}，狼人阵营获胜（第 {self.day} 天）。{note}")

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def supersede(self, reason: str = "") -> int:
        """把本局推进到下一**代**：在途的回复回来时一律丢弃（不写台账、不参与流程）。

        ⚠️ 只加代号、**不取消任务**：任务回来时会自己发现"我不是那一代了"，然后抛
        `GameAborted` 退出。取消留给调用方（服务端放弃本局时本来就会 cancel 那个 task）。
        为什么要有它（2026-10-30）：放弃 / 重开 / 硬复位之后，旧任务可能还挂在 await 上；
        它回来时如果照常写 transcript、照常返回，就会把上一局的信息带进新局。
        `abort()` 也会调它，所以"放弃"天然带代际校验。

        ⚠️ **只在"整局作废"时调它**。预取那种"这一包基于旧状态、作废重发"属于局内的局部作废，
        不要调这里（会把整局判死）；预取自己带 `state_version` 比对。
        """
        self.generation += 1
        return self.generation

    def abort(self, reason: str = "玩家主动放弃了本局") -> None:
        """放弃当前对局。

        做两件事：把 winner 标成 aborted（各处循环的退出条件都会看到），
        并让下一次 act() 直接抛 GameAborted。已经产生的台账和留痕都保留，
        存档照样能复盘，只是结果记为"放弃"。
        """
        if self.aborted or (self.winner and self.winner != "aborted"):
            return
        self.aborted = True
        self.winner = "aborted"
        # ⚠️ 这里原来硬编码 "day"：夜里放弃本局时，公告会被标成白天，
        # 回放/存档里"第 2 天 白天"就出现一条夜晚该发生的事。用真实阶段。
        self.ledger.add(self.day, self.phase, "announce", f"———— {reason} ————")
        # 放弃 = 推进一代：在途回复回来时直接作废（见 act 的代际核对）
        self.supersede("abort")

    async def run(self) -> str:
        self.emit("start", seats=[(s.id, role_name(s.role_key), s.is_human) for s in self.seats])
        try:
            while not self.winner and not self.aborted and self.day < self.max_days:
                self.day += 1

                self.phase = "night"
                # 养鸦人的诅咒只作用于"接下来的那一个白天"：每轮夜里先清掉上一轮的
                self.cursed_today = None
                self.emit("phase", day=self.day, phase="night")
                deaths = await self.night()
                if self.aborted:
                    break
                # 注意：这里不判胜负 —— night() 从不改 alive（死亡都在白天地落地），
                # 所以原来那句 check_win() 是永远不会生效的死分支。

                self.phase = "day"
                self.emit("phase", day=self.day, phase="day")
                await self.day_phase(deaths)
                if self.aborted:
                    break
        except GameAborted:
            pass

        if not self.winner:
            self.winner = "draw"
            self.ledger.add(self.day, "day", "announce",
                            f"达到最大天数 {self.max_days}，本局判和。")
        self.emit("end", winner=self.winner, day=self.day)
        return self.winner

    # ------------------------------------------------------------------ #
    # 存档 / 回放
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        """当前对局的**快照**（真拷贝）。

        ⚠️ 以前这里是浅拷贝：`private_events` / `transcript` 直接把活 list 交出去，
        `ledger.to_list()` 更是把 `Entry.__dict__` 本体交出去。调用方（网页端、复盘、
        存档路径上的任何代码）拿到"快照"之后，对局每往下走一步，那份"快照"就跟着变 ——
        存下来的档和当时看到的根本不是一局。
        这里用 json 往返做深拷贝：内容全是 JSON 可序列化的，顺带保证
        `save()` 里 json.dump 一定不会再撞上不可序列化的东西。
        """

        def snap(obj):
            return json.loads(json.dumps(obj, ensure_ascii=False))

        return {
            "board": self.board,
            "day": self.day,
            "winner": self.winner,
            "aborted": self.aborted,
            "started_at": self.started_at,
            # 规则状态也存下来：这样存档能自己说明"这一局是按什么规则打的"，
            # 复盘/对比时才不用猜（尤其 pk_on_tie / wolf_control 会影响胜负口径）
            "rules": {
                "win_rule": self.win_rule,
                "max_days": self.max_days,
                "pk_on_tie": self.pk_on_tie,
                "wolf_control": self.wolf_control,
                "witch_heal": self.witch_heal,
                "witch_poison": self.witch_poison,
                "phase": self.phase,
                # 警长：11 人及以上默认开；`sheriff_id` 是"现在警徽在谁手上"
                "sheriff": self.sheriff_enabled,
                "sheriff_min_seats": self.sheriff_min_seats,
                # 首夜无刀（板子级）：存档要能自己说明"这一局的第一夜为什么没有刀口"，
                # 否则复盘时看不出是规则如此、还是引擎漏了一夜
                "first_night_quiet": self.first_night_quiet,
                # 规则书投放方式：存档要能自己说明"这一局到底给没给模型发规则"
                # （不然复盘/对比时无法区分"同一模型在两套规则条件下"的成绩）
                "rules_mode": self.rules_mode,
                "rules_detail": self.rules_detail,
                "rules_chars": len(self.rules_text),
                # 边界口径：同一夜里"屠边完成"与"最后一狼被抓出来"同时成立时算谁赢。
                # 不记下来的话，两局胜负不同却看不出是因为口径改了（复盘/对比会误判）。
                "edge_order": self.edge_order,
                # 提示层的开关与体量：存档要能说明"这一局模型到底看到了哪些指导材料"
                # （通用提示词 / 打法书 / 概念库进包段），不然换了材料之后成绩没法比。
                "player_prompt_chars": len(self.player_prompt),
                "playbook_chars": len(self.playbook_text),
                "meta_chars": len(self.meta_text),
                # 单次上限 / 整手预算：这两个值会直接影响"这一局有多少次调用被判失败"
                "call_timeout": self.call_timeout,
                "decision_timeout": self.decision_timeout,
                "auto_retries": self.auto_retries,
                "retry_backoff": list(self.retry_backoff),
                "carry_reasoning": self.carry_reasoning,
            },
            "sheriff_id": self.sheriff_id,
            "seats": snap([
                {
                    "id": s.id, "role": s.role_key, "is_human": s.is_human, "alive": s.alive,
                    "mind": s.mind, "private_events": s.private_events,
                    "card_name": s.card_name,
                    "checked": {str(k): v for k, v in s.checked.items()},
                    "death_day": s.death_day, "death_cause": s.death_cause,
                    "last_guard": s.last_guard,
                    "knight_used": s.knight_used,
            "knight_failed": s.knight_failed,
            "revealed_idiot": s.revealed_idiot,
                    "lucky_used": s.lucky_used,
                    "lucky_bullet": s.lucky_bullet,
                    # 狼王是否已经用过"带走一人"（复盘要看得出他那一口到底用没用）
                    "king_done": s.king_done,
                }
                for s in self.seats
            ]),
            "ledger": snap(self.ledger.to_list()),
            "audit": snap(self.audit),
            "faults": snap(self.faults),
            "transcript": snap(self.transcript),
            # 三层私密决策（发言前 / 投票前 / 狼队夜里）的最终快照，供复盘：
            #   · 台账里只有「为决策服务的那句发言」，看不到它当时**到底决定投谁、为什么**；
            #   · `wolf_decisions` 以前只写不读，是彻底的死字段 —— 存下来才谈得上复盘
            #     （"当晚狼队各自想刀谁、理由是什么"）。
            # 键统一转成字符串：JSON 的对象键只能是字符串，别让复盘工具两边对不上。
            "decisions": snap({
                "speech": {str(k): v for k, v in self.speech_decisions.items()},
                "day": {str(k): v for k, v in self.day_decisions.items()},
                "wolf": {str(k): v for k, v in self.wolf_decisions.items()},
            }),
        }

    def save(self, save_dir: str = "games", enrich=None, stem: str = "") -> str:
        """把这一局写进存档目录，返回文件路径。

        `enrich(data, stem)`：写盘**之前**的回调，用来给快照补东西（服务端用它把每条发言的
        语音硬链接到 `<存档同名目录>/audio/<序号>.mp3`，并给条目打 `audio: true` 标记）。
        放在写盘前是为了只落一次盘：先归档音频、再写 JSON，存档里就自带"哪几条有语音"。

        `stem`：**指定存档名**（不含 .json）。给服务端的"自动存盘"用 —— 它要在对局中途
        反复覆盖**同一个**文件（崩溃了也能复盘），而不是每次都新建一个（那会变成几十个
        半截存档，还会把 keep_games 的保留窗口冲掉）。留空 = 老行为（按时间戳新建）。
        """
        os.makedirs(save_dir, exist_ok=True)
        if stem:
            # ⚠️ 2026-10-30：stem 只当**文件名**用。以前直接 join，传 "sub/x" 会
            #    FileNotFoundError，传 "../x" 能写到存档目录外面。现在先取 basename、
            #    再按 Windows 非法字符清洗；清空了就直接拒绝（宁可不存，也不写到外面）。
            safe = os.path.basename(str(stem).replace("\\", "/").strip())
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe).strip(" .")
            if not safe:
                raise ValueError(f"存档名不合法：{stem!r}")
            path = os.path.join(save_dir, f"{safe}.json")
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(save_dir, f"game-{stamp}.json")
            suffix = 1
            while os.path.exists(path):       # 同一秒开两局也不能互相覆盖
                suffix += 1
                path = os.path.join(save_dir, f"game-{stamp}-{suffix}.json")
        data = self.to_dict()
        stem = os.path.basename(path)[:-len(".json")]
        if enrich is not None:
            try:
                enrich(data, stem)
            except Exception:                 # noqa: BLE001
                pass                          # 归档失败绝不能影响存档本身
        # 先写临时文件再原子替换：自动存盘是**对局中途**反复覆盖同一个文件，
        # 万一在写一半时进程没了，读到的会是一个残破 JSON（复盘直接打不开）。
        tmp = path + ".partial"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            # ⚠️ 2026-10-30：写失败/序列化失败时把 .partial 收掉，别让残骸越堆越多
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return path
