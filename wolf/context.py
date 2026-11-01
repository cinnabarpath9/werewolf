# coding=utf-8
"""按座位组装信息包（actor-facing packet）。

**这是整个系统的隔离边界**：任何一次模型调用，上下文只由这里产出。
别的模块不许自己拼 prompt，避免某个角落不小心把上帝视角塞进去。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import persona as persona_mod
from . import rules
from . import tabletalk
from . import words
from .roles import ROLES, camp_name, seats_text


@dataclass
class Task:
    """当前要这个座位做什么。"""
    kind: str                      # wolf_kill / seer_check / witch / speech / vote / hunter_shoot
    text: str                      # 给模型看的任务说明
    schema: str                    # 要求输出的 JSON 结构
    legal: list[int] = field(default_factory=list)   # 合法目标（人类玩家和降级逻辑都用它）
    extra: dict = field(default_factory=dict)
    required: tuple[str, ...] = ()  # 必须有值的字段；缺了会自动让模型补一次


def build(state, seat, task: Task) -> str:
    """产出一个座位这次调用能看到的全部信息。"""
    role = ROLES[seat.role_key]
    parts: list[str] = []

    parts.append(f"你是 {seat.id} 号玩家，正在玩一局 {len(state.seats)} 人狼人杀。")

    identity = [f"【你的身份】{role.name}（{camp_name(role.camp)}）", role.brief]
    parts.append("\n".join(identity))

    # ── 通用玩家提示词 + 位置概率参考（2026-10-07 起用，取代按座位的人设卡；
#    2026-10-09 起概率段并进《概念库》，按阵营分发）──
    # 两条都是"所有座位共用"的文本，跟身份无关、也不含任何额外信息。
    prompt = getattr(state, "player_prompt", "") or ""
    if prompt:
        parts.append(tabletalk.player_block(prompt))
    # 打法书：按**角色 + 阵营**分发（每个座位只拿到"通用 + 自己阵营的大战略 + 自己那个角色"）
    # ⚠️ 阵营这一层是 2026-10-09 加的：狼队的大战略不能发给好人，好人战略也不该发给狼。
    book = getattr(state, "playbook_text", "") or ""
    # 当前**局面阶段**（首夜/第一天 → open；前两轮 → mid；残局 → late）。
    # 只发这一阶段该看的小节，别把"残局怎么打"塞给第一天的模型（用户 2026-10-09 定的成本纪律）。
    phase = tabletalk.phase_of(state)
    if book:
        camp = "wolf" if getattr(seat, "camp", "") == "wolf" else "good"
        block = tabletalk.playbook_block(
            tabletalk.filter_role(book, seat.role_key, camp, phase))
        if block:
            parts.append(block)

    meta = getattr(state, "meta_text", "") or ""
    if meta:
        # 玄学书按阵营分发（狼只看狼的章节、好人只看好人的），见 wolf/tabletalk.py
        block = tabletalk.meta_block(tabletalk.filter_camp(meta, role.camp))
        if block:
            parts.append(block)

    if seat.persona:
        text = seat.persona.strip()
        # 角色卡（姓名/年龄/职业/经历/关系 + 表达层正文）自带抬头和红线，原样插入；
        # 其他写法（config 里内联的纯风格文本）补一句老说明。
        # ⚠️ 判据必须是**我们自己那个抬头**，不能是"以【开头"：内联写成
        #    `game.personas: {3: "【说话风格】我爱用反问句。"}` 时，以前会被当成
        #    "自带抬头的角色卡" → 模型拿到的人设里没有"不影响判断/不给额外信息"那句纪律。
        if text.startswith(persona_mod.CARD_TITLE):
            parts.append(text)
        else:
            parts.append(
                "【你的说话风格（只影响你怎么说，**不影响你怎么判断**，也不给你任何额外信息）】\n"
                + text
            )

    # 座位 ↔ 姓名对照表。**必须有**：角色卡里的关系是"沈倩宁是我师妹"这种按名字写的，
    # 而台账里其他人只以"N号"出现 —— 不给这张表，那些关系就是一堆认不出人的死信息。
    # 姓名属于表达层、公开信息（谁都能看到谁叫什么），跟身份无关，写进每个人的包都安全。
    named = [(s.id, s.card_name) for s in state.seats if s.card_name]
    if named:
        parts.append("【座位与姓名】（公开信息，跟身份无关）"
                     + "、".join(f"{sid}号 {nm}" for sid, nm in named))

    # 规则书的两种放置方式之一：塞进座位提示词（另一种是开局公示栏，见 wolf/rules.py）。
    # 文本是**按本局板子组装**好的（只含本局真有的角色），开局算一次、每次调用复用。
    if getattr(state, "rules_in_packet", False):
        block = rules.packet_block(getattr(state, "rules_text", ""))
        if block:
            parts.append(block)

    if role.camp == "wolf":
        # 出局的队友也要列出来（标一下）：不然队友一死，这一行就从上下文里消失，
        # 幸存狼再也看不出队里少了人（每次调用都是全新对话，它没有别的记忆）
        mates = []
        for s in state.seats:
            if s.camp == "wolf" and s.id != seat.id:
                mates.append(f"{s.id}号" if s.alive else f"{s.id}号（已出局）")
        if mates:
            parts.append("【你的狼队友】" + "、".join(mates))

    if getattr(state, "sheriff_id", None) == seat.id:
        # 警徽是"这个人现在握着的一件东西"，不是身份 —— 所以放在身份外面单独说。
        # 别人知不知道谁是警长？知道：当选/移交/流失都会写成公开公告进台账。
        parts.append(
            "【你的警徽】你现在是本局警长：投票时你的一票算 1.5 票；"
            "你出局（被放逐 / 被刀 / 被枪）时，可以选择把警徽传给一名存活玩家，或者撕掉它。"
        )

    alive = state.alive_ids()
    dead = state.dead_list()
    parts.append(
        f"【当前阶段】第 {state.day} 天 · {'夜晚' if state.phase == 'night' else '白天'}\n"
        + state.ledger.render(seat.id, role.camp, alive, dead, state.day)
    )

    # ① 发言顺序 / 时间线（用户 2026-10-07：模型没有"谁先说谁后说"的观念，
    #    会出现"前置位引用后置位的话"这种不可能的事）。
    #    白天尤其重要：说话是有顺序的，每个人的判断只能基于**它之前**那些人的发言。
    if state.phase == "day" and len(alive) > 1:
        order: list[int] = []
        try:
            order = [s.id for s in state.speech_order() if s.alive]      # 本轮发言顺序
        except Exception:                                            # noqa: BLE001
            order = list(alive)
        said: list[int] = []
        try:
            # ⚠️ 只统计**白天轮次发言**：竞选发言 / PK / 遗言同样以 `kind="speech"` 记账
            #    （文案前缀【竞选】【PK】【遗言】），全算进来的话，第 1 天只要有人竞选过，
            #    这份「已发言/未发言」名单就是错的 —— 而模型只能靠它判断「谁的话我还没听到」。
            #    轮次发言在记账时带了 `data={"round": "day"}`（见 engine.day_phase）。
            #    旧存档没有这个标记 → 这里取不到就当空（宁可不写名单，也不能写错的）。
            said = [e.seat for e in state.ledger.entries
                    if e.day == state.day and e.kind == "speech" and e.seat
                    and (getattr(e, "data", None) or {}).get("round") == "day"]
        except Exception:                                            # noqa: BLE001
            said = []
        said_now = [i for i in order if i in said]
        todo = [i for i in order if i not in said]
        pos = (order.index(seat.id) + 1) if seat.id in order else 0
        # ⚠️ 这里必须写成普通 if/else，不能用"跨行三元表达式"：
        #    上一版写成 `parts.append(A if pos else B + C)`，运算符优先级把它拼成了
        #    完全不同的字符串（自测里当场发现）。可读性也差。
        lines = ["【发言顺序与进度】（时间线：说话是有先后的，别搞反）",
                 f"  · 本轮发言顺序：{' → '.join(f'{i}号' for i in order)}"]
        if pos:
            lines.append(f"  · 你是第 {pos} 个发言的")
        else:
            lines.append("  · 你这一轮已经说过了")
        if said_now:
            lines.append(f"  · 已经发过言：{'、'.join(f'{i}号' for i in said_now)}")
        if todo:
            lines.append(f"  · 还没发言：{'、'.join(f'{i}号' for i in todo)}")
        lines.append("  · 规则：你只能听到**已经发过言**的那些人说的话；还没开口的人说的话"
                     "你不可能知道，他们也可能引用**你**的发言。所以别把还没说的人的话当成依据，"
                     "也别把别人先说过的东西当成自己的新发现。")
        parts.append("\n".join(lines))

    if seat.private_events:
        parts.append("【你的私有经历（只有你知道）】\n" + "\n".join(f"  · {x}" for x in seat.private_events))

    if seat.experience:
        parts.append(
            "【你的过往经验（来自以前玩过的若干局，**不是本局的事实**）】\n"
            "注意：这些只是你自己的心得，里面的玩家和局面都跟本局无关；"
            "本局的一切判断必须以【当前局势】为准，绝不要把往局的人或事当成现在的事实。\n"
            + seat.experience
        )

    if seat.mind:
        parts.append("【你的推理笔记（只有你自己看得到，不会公开）】\n" + seat.mind)

    parts.append("【当前任务】" + task.text)
    if task.legal:
        parts.append("【可选目标】" + seats_text(task.legal))
    else:
        parts.append("【可选目标】无（自由发言）")
    parts.append("【输出格式】只输出一个 JSON 对象，不要解释、不要代码块标记：\n" + task.schema)

    # ── 狼「穿身份」参考章（2026-10-08 新增，**纯附加块**）─────────────────────
    # 用户反馈的真问题：悍跳狼会站在**旁观者**的角度谈自己声称的身份（「真预言家怎么样」
    # 「后置位预言家怎么样」），一句话就露馅。根因是它手上只有狼章，根本不知道真预言家/
    # 女巫/猎人**该怎么说话**。
    # 做法：狼**已经报过某个身份**（台账里的 `claim` 条目，由引擎写入 —— 不新增任何引擎
    # 接口、不改台账结构）之后，把**那个身份的章节**也附给它。
    # ⚠️ **还没报过任何身份的狼，默认附「预言家」章**（`default_seer=True`）：
    #    "第 1 天悍跳预言家"是狼最主流的打法，而第 1 天不竞选警长的狼**第一次开口时
    #    必然还没有 claim 台账**（`claim` 就是这次发言的产物）—— 不兜这个默认，
    #    它最需要演的那一次恰好是空的。理由：第一/第二天能抢的硬身份基本都是预言家；
    #    跳女巫/猎人/守卫通常发生在后面几轮，那时 claim 已经写进台账、自然就跟着走了。
    # ⚠️ 放在包的**最后**是有意的：上面每个块的位置都跟原来一模一样（老路径一字未动），
    #    这一块只是多出来的尾巴；挨着【输出格式】也正好提醒它"这一章是给你这一手发言用的"。
    # 只对狼生效：好人的身份是真的，给它别的角色章节没有意义、还会污染视角。
    # 开关 `game.attach_claimed_chapter`（config.yaml，默认开）关掉就整块不附 ——
    # 用户在意延迟与成本，这是那个闸。
    if role.camp == "wolf" and book and attach_claimed_chapter():
        claimed = claimed_role_key(state, seat.id, default_seer=True)
        # ⚠️ P3-3：它声称的就是**自己真正的角色**（狼人说「我是狼人/狼」）时**不附**。
        #    那种情况下被附上来的那一章跟它主包里已有的狼章是同一段文字 ——
        #    实测一份包里同一章进了两遍、多出 6294 字（既白烧 token，又让模型读成两份书）。
        #    这不是"少给东西"：它自己那一章本来就在包里。
        # ⚠️ 2026-10-30 修：判据要算上**继承章**。狼王的 role_key 是 wolf_king，但
        #    `ROLE_EXTRA_FRAGS` 让它的主包已经带了 `role:wolf`；他报「我是狼人」时
        #    claimed="wolf" != "wolf_king"，旧判据照样把狼人章附第二遍（14527 字×每调用）。
        own_frags = {seat.role_key} | set(tabletalk.ROLE_EXTRA_FRAGS.get(seat.role_key, ()))
        if claimed and claimed not in own_frags:
            extra = tabletalk.playbook_block(
                # ⚠️ 用 filter_role_only 而不是 filter_role：通用部分（frag:all）它自己的包里
                #    已经有一份了，再附一次等于同一段文字进包两遍（2026-10-08 实测踩过：
                #    附上来的"预言家章"里带着通用章，比主包还长）。
                tabletalk.filter_role_only(book, claimed, phase),
                title=tabletalk.claimed_role_title(
                    ROLES[claimed].name,
                    assumed=not _has_claim(state, seat.id),
                    # ⚠️ P3-1：归一化失败（报的是「我是好人」这种认不出的说法）时，
                    #    抬头**如实转述它报的原文**，不能说"你还没报过身份"——
                    #    引擎已经写了公开的 claim 条目，那句"还没报"跟同一份包自相矛盾。
                    raw=_raw_claim_of(state, seat.id) if not _claim_of(state, seat.id) else ""))
            if extra:
                parts.append(extra)

    return "\n\n".join(parts)


# ── 狼「穿身份」：从台账里读出它已经报过的身份（2026-10-08 新增）──────────────
# ⚠️ 这个函数是**只读**的：台账里的 `claim` 条目由引擎在发言/竞选发言/遗言/PK 时写入
#    （`wolf/engine.py` 里的 `self.ledger.add(..., kind="claim", data={"claim": ...})`），
#    这里不新增任何引擎接口、也不改台账结构 —— 引擎侧一行都不用动。
#: 声称的身份文本 → 角色 key 的归一化表（模型写的名字/简称/英文都可能出现）
_CLAIM_ALIASES: dict[str, str] = {
    "预言家": "seer", "预": "seer", "预言": "seer", "seer": "seer",
    "女巫": "witch", "巫": "witch", "witch": "witch",
    "猎人": "hunter", "猎": "hunter", "hunter": "hunter",
    "守卫": "guard", "守": "guard", "guard": "guard",
    # ⚠️ 2026-10-09：白痴改名**愚者**，并新增摄梦人/守墓人/养鸦人/幸运儿 ——
    #    这些自称现在都在默认板子上，认不出来就等于"模型报了身份、引擎当没看见"。
    #    「白痴」保留为旧称别名（老存档 / 模型偶尔还会用老名字）。
    "愚者": "idiot", "白痴": "idiot", "idiot": "idiot",
    "摄梦人": "dreamer", "摄梦": "dreamer", "梦": "dreamer", "dreamer": "dreamer",
    "守墓人": "gravekeeper", "守墓": "gravekeeper", "gravekeeper": "gravekeeper",
    "养鸦人": "crow", "乌鸦": "crow", "鸦": "crow", "crow": "crow",
    "幸运儿": "lucky", "幸运": "lucky", "lucky": "lucky",
    "村民": "villager", "民": "villager", "平民": "villager", "villager": "villager",
    "狼人": "wolf", "狼": "wolf", "wolf": "wolf",
    # 2026-10-10（狼王版板子）：狼王是狼队角色，但他**不是 wolf** ——
    # 「我是狼王」认不出来的话，引擎会当成"报了个不认识的身份"，
    # 小狼装狼王的骗票战术就拿不到那一章参考资料了。
    "狼王": "wolf_king", "wolf_king": "wolf_king", "wolfking": "wolf_king",
    # 这些不是身份，只是「我没什么身份可报」的说法 —— 认出来是为了**排除**掉
    "无": "", "没有": "", "none": "", "null": "", "-": "", "": "",
}


def _norm_role_key(raw: str) -> str:
    """把台账里那句「声称自己是【X】」的 X 归一到角色 key（认不出来就给空串）。

    ⚠️ 先查别名表，再按 `ROLES` 的名字/键做**整串相等**匹配 —— 故意不做子串包含：
    「不是预言家」这种否定句里也含「预言家」，模糊匹配会把它当成一次真的报名。

    ⚠️ 认不出来 = 空串，**不等于"没报过"**（见 `_claim_entry_seen`）：
    真局里「我是好人」「守墓人」「骑士」「丘比特」这些写法都会落到这里。
    """
    text = str(raw or "").strip().strip('"\'').strip()
    if not text or text in ("无", "none", "None", "-", "null"):
        return ""
    low = text.lower()
    if low in _CLAIM_ALIASES:
        return _CLAIM_ALIASES[low]
    for key, r in ROLES.items():
        if text == r.name or low == key:
            return key
    return ""


def _claim_entry_seen(state, seat_id: int) -> bool:
    """台账里**有没有**这个座位的 claim 条目（不管它报的是什么）。

    ⚠️ 这是 P3-1 的判据：以前拿"归一化成功"当"报过身份"，于是模型写了别名表以外的
    说法（实测常见：我是好人 / 好人 / 守墓人 / 骑士 / 丘比特 / 暂无）时，
    引擎**已经**写了公开的 claim 条目，包里却说「你**还没有**向任何人报过身份」——
    同一份包自相矛盾。报没报过是**事实**，认不认得出那个名字是另一回事。
    """
    try:
        entries = list(getattr(getattr(state, "ledger", None), "entries", None) or [])
    except Exception:                                     # noqa: BLE001
        return False
    return any(getattr(e, "seat", None) == seat_id and getattr(e, "kind", "") == "claim"
               for e in entries)


def _raw_claim_of(state, seat_id: int) -> str:
    """这个座位最近一次 claim 的**原文**（归一化失败时用来如实转述它报了什么）。"""
    try:
        entries = list(getattr(getattr(state, "ledger", None), "entries", None) or [])
    except Exception:                                     # noqa: BLE001
        return ""
    latest = ""
    for e in entries:
        if getattr(e, "seat", None) != seat_id or getattr(e, "kind", "") != "claim":
            continue
        data = getattr(e, "data", None) or {}
        raw = str(data.get("claim") or "").strip()
        if raw:
            latest = raw
    return latest


def _claim_of(state, seat_id: int) -> str:
    """这个座位在台账里**最近一次**真实声称过的身份（没报过/认不出来就是空串）。

    为什么要「最近一次」而不是「第一次」：狼中途改口换衣服是常见战术，
    要演的那件衣服是**它现在穿着的那件**，不是最早那件。
    读的是台账顺序（先竞选发言、再轮次发言、再遗言），所以「最后一个」就是「最新的」。
    """
    try:
        entries = list(getattr(getattr(state, "ledger", None), "entries", None) or [])
    except Exception:                                     # noqa: BLE001
        return ""
    latest = ""
    for e in entries:
        if getattr(e, "seat", None) != seat_id or getattr(e, "kind", "") != "claim":
            continue
        data = getattr(e, "data", None) or {}
        key = _norm_role_key(data.get("claim") or "")
        if key:
            latest = key                              # 不 break：要的是最后一条有效的
    return latest


def _has_claim(state, seat_id: int) -> bool:
    """它到这一刻为止**报过**身份没有（用来区分"真报了"和"系统兜的默认章"）。

    ⚠️ 判据是"台账里有没有 claim 条目"，**不是**"认不认得出那个名字"（P3-1）。
    """
    return _claim_entry_seen(state, seat_id)


def claimed_role_key(state, seat_id: int, default_seer: bool = False) -> str:
    """要给它附哪一件衣服的角色 key（没报过就返回空串；`default_seer` 时返回 seer）。

    `default_seer=True` 是给**还没报过身份**的狼兜底用的（见 `build` 里的说明）：
    第一天悍跳预言家是最主流的打法，而那一次开口时台账里还没有 claim。
    兜底只发生"一次都没报过"的情况 —— 只要报过（哪怕是别的身份），一律以它报的那个为准，
    免得出现"它明明跳了女巫、系统还硬塞预言家章"这种自相矛盾。

    ⚠️ 但"报过、只是认不出那个名字"（例如「我是好人」）**不能**当成"没报过"：
    那种情况下抬头要中性（见 `tabletalk.claimed_role_title(raw=...)`），
    正文仍按真局经验兜默认的预言家章 —— 认不出名字的报名，绝大多数是狼在含糊其辞。
    """
    key = _claim_of(state, seat_id)
    if key:
        return key
    return "seer" if default_seer else ""


# ── 开关：狼「穿身份」参考章（`game.attach_claimed_chapter`，config.yaml）─────────
# ⚠️ 为什么要在这里读配置文件：这个开关**理论上**该像 `playbook_text` / `meta_text` 那样
#    由 server.py / run_game.py 读出来、经 `Game(...)` 传进 state —— 但 `Game.__init__`
#    加参数要动 `wolf/engine.py`，而那份文件这一轮**不许改**。所以退一步：在本层直接读
#    config.yaml（只读一次、结果缓存），缺配置/读不动/YAML 坏了一律**按默认 true**走，
#    绝不让"读配置失败"影响开局的可用性。
#    等哪天 engine 侧方便了，正确做法是把它提升成 `Game(attach_claimed_chapter=...)`，
#    这个函数就退化成 `getattr(state, "attach_claimed_chapter", True)`。
_CFG_CACHE: list = []          # [True]/[False]；空 = 还没读过
#: `--config` 指过来的那份配置文件（由 `run_game.py` 调 `set_config_path` 填）。
#: 空 = 没指定，按"模块旁边 → 当前工作目录"猜（老行为）。
_CFG_PATH: list = []           # [str]；空 = 没指定


def set_config_path(path: str) -> None:
    """告诉 context：**这一局真正读的那份配置**在这里（P3-6）。

    为什么需要：`game.attach_claimed_chapter` 这个开关是 context 层直接读 yaml 的
    （不像 `playbook_text` 那样由 server/run_game 传进 state），而它的默认查找只探
    「wolf/ 旁边」和「当前工作目录」两份 —— `run_game.py --config /tmp/x.yaml`
    指的要是别处，这个开关**就绕过 `--config` 了**：你以为配置关掉了，实际读的是另一份。

    ⚠️ 换了路径必须**清掉缓存**（缓存是按"第一次读到的配置"记的），
    否则"先读默认 → 再 set 到别处"会拿到旧值。传空串 = 恢复默认查找。
    """
    _CFG_PATH[:] = [str(path)] if str(path or "").strip() else []
    _CFG_CACHE.clear()


def reset_config_cache() -> None:
    """把"开关读到了什么"的缓存清掉（改过配置或换过路径之后用；测试也用它）。"""
    _CFG_CACHE.clear()


def _as_bool(raw, default: bool = True) -> bool:
    """配置里的真假值归一。⚠️ 不直接用 `bool(raw)`：YAML 读出来的 "false" / "no"
    是非空字符串 → True，配置写成字符串时就会"看起来关了其实还开着"。

    ⚠️ P3-5：判据走 `wolf/words.py` 那张**引擎也在用**的表（`关/关闭/否/开/开启/是`）。
    以前这里自己写了一份更窄的假值表（只有 false/no/off/0/空），于是同一句「关」
    在引擎认、在这个开关上不认 —— 写「关」等于没有关。
    """
    if isinstance(raw, str):
        text = words.normalize_word(raw)
        if not text:
            return default                    # 空串 = 没写 → 用默认值（不是"关"）
        if text in words.FALSE_WORDS:
            return False
        if text in words.TRUE_WORDS:
            return True
        return default                        # 认不出来的写法：宁可退回默认值，不瞎猜
    return default if raw is None else bool(raw)


def _config_flag(path: str, key: str, default: bool = True) -> bool:
    """从 config.yaml 的 `game:` 段读一个开关（读不到/坏了 → `default`）。

    单独拆出来是为了**可测**：`attach_claimed_chapter()` 走的是缓存 + 猜路径，
    测试没法验"配置里写了 false 会不会真的读到 false"。这个函数只做一件事：
    给一个文件路径，把那个开关读出来。
    """
    try:
        import yaml
        with open(path, encoding="utf-8-sig") as fh:
            game_cfg = ((yaml.safe_load(fh) or {}).get("game") or {})
    except Exception:                                     # noqa: BLE001
        return default
    if key not in game_cfg:
        return default
    return _as_bool(game_cfg.get(key), default)


def attach_claimed_chapter() -> bool:
    """狼「穿身份」参考章要不要附（默认**开**）。

    找配置文件的顺序：
      1. `set_config_path()` 指定的那份（`run_game.py --config` / server 传进来的）；
      2. `wolf/` 旁边的 `config.yaml`；
      3. 当前工作目录下的 `config.yaml`。
    只读一次、结果进缓存（换路径时由 `set_config_path` 清缓存）。
    """
    if _CFG_CACHE:
        return _CFG_CACHE[0]
    value = True
    here = os.path.dirname(os.path.abspath(__file__))          # …/werewolf/wolf
    paths = []
    if _CFG_PATH:
        paths.append(_CFG_PATH[0])              # ⚠️ 显式指定的那份**优先**，别再拿猜的顶掉它
    paths += [os.path.join(os.path.dirname(here), "config.yaml"),
              os.path.join(os.getcwd(), "config.yaml")]
    for path in paths:
        if os.path.isfile(path):
            value = _config_flag(path, "attach_claimed_chapter", True)
            break                       # 找到配置文件就不再往下找
    _CFG_CACHE.append(value)
    return value


