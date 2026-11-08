# coding=utf-8
"""两个"给所有座位共用"的提示层（2026-10-07 用户要求，取代按座位的人设卡）：

  1. **通用玩家提示词**（`game.player_prompt`）：每个 AI 座位都收到同一段话 ——
     "你是一名专业的狼人杀玩家…按本局身份牌为自己的阵营争取胜利"。
     为什么换掉人设卡：用户实测觉得"加了姓名/性格/说话方式/打法策略之后，
     反而把模型的思路固定死了"，而且每个座位的语气差别并不值得那么多 token。
  2. **场外玄学书**（`game.meta_book`，内容在 `场外玄学.md`）：一份**基于发牌概率**的参考
     （连好链之后下一位是狼的概率、三连神有多罕见…），并写明纪律：
     只能当参考、只在五五开时用、不许推翻发言与票型、不许当发言的硬证据。
     它替代了原来的"人际关系式场外"（谁跟谁熟），因为那个在实战里显得很随意。

两个都是"整篇文本进 packet"的简单注入，不再有 per-seat 渲染。
"""
from __future__ import annotations

import os
import re

from . import persona as persona_mod

#: ⚠️ 2026-10-09：《场外玄学书》已并进《概念库》——默认路径改成它，
#:    并且**只取** `<!-- frag:meta:begin/end -->` 圈出的那一段进包
#:    （不能因为合并就把整本都塞进每次调用：成本敏感）。
DEFAULT_META_PATH = "content/06_概念库.md"
DEFAULT_PLAYBOOK_PATH = "content/05_战术书.md"
#: 角色章单独一份（2026-10-31 拆书）：默认路径 = 战术书 + 角色书，拼起来才是完整的"打法书"
DEFAULT_PLAYBOOK_EXTRA = "content/04_角色书.md"
#: 技能打法片段（Phase 2）：只发本局 active_skills 的 `frag:skill:<key>`。
DEFAULT_SKILL_BOOK = "content/08_技能书.md"
DEFAULT_PLAYER_PROMPT = (
    "你是一名专业的狼人杀玩家。请根据本局发给你的身份牌进行发言和决策，"
    "你的发言和决策要尽可能让你所在阵营获得优势，并最终取得胜利。"
)
META_TITLE = "【概念与概率（位置概率参考）】"
PLAYBOOK_TITLE = "【打法书（按你的角色给的）】"


def load_playbook(project_root: str, path: str = "", role_key: str = "",
                  camp: str = "") -> str:
    """读打法书（默认 `打法书.md`）。按**角色 + 阵营**分发：

      `<!-- frag:all -->…<!-- frag:end -->`          → 所有角色都看（轮次/容错/边界口径）
      `<!-- frag:camp:wolf -->…<!-- frag:end -->`    → 只有这个**阵营**看（狼队大战略 / 好人战略）
      `<!-- frag:role:wolf -->…<!-- frag:end -->`    → 只有这个角色看

    ⚠️ 2026-10-09 加的阵营分片：**狼队的大战略不能发给好人、好人战略也不能发给狼**
    （玄学书早就是按阵营分的，打法书以前只有 role/all/god 三级）。
    `camp` 留空时跟角色一样**不过滤该层**（老调用方与测试的调用方式保持可用）。
    """
    name = str(path or DEFAULT_PLAYBOOK_PATH)
    full = name if os.path.isabs(name) else os.path.join(project_root, name)
    text = _read(full)
    if not path:
        # 2026-10-31 拆书：默认路径 = 战术书 + 角色书，拼起来才是原来那一份完整打法书。
        # 显式传了 path 的调用方（测试 / 自定义书）只读那一份，行为不变。
        extra = _read(os.path.join(project_root, DEFAULT_PLAYBOOK_EXTRA))
        if extra:
            text = text + "\r\n" + extra
        skill_book = _read(os.path.join(project_root, DEFAULT_SKILL_BOOK))
        if skill_book:
            text = text + "\r\n" + skill_book
    if not text:
        return ""
    if role_key or camp:
        return _clean(_filter_role(text, role_key, camp))
    return _clean(text)


#: 打法书里的**阶段标记**：`<!-- frag:part:open -->` … `<!-- frag:endpart -->`。
#: 不打标签的正文 = core（每次都发）。阶段名：open（首夜/第一天）/ mid / late（残局）。
_PART_MARK = re.compile(r"\s*<!--\s*frag:part:(open|mid|late)\s*-->\s*$")
_PART_END = re.compile(r"\s*<!--\s*frag:endpart\s*-->\s*$")

#: **事件触发块**：`<!-- frag:when:<name> -->` … `<!-- frag:endwhen -->`。
#: 只在当前局面真的命中该事件时才进包；`triggers=None`（页面预览 / 老调用方）
#: 时整块保留，避免编辑器预览里看不到内容。
_WHEN_MARK = re.compile(r"\s*<!--\s*frag:when:([a-z_]+)\s*-->\s*$")
_WHEN_END = re.compile(r"\s*<!--\s*frag:endwhen\s*-->\s*$")

#: 阶段顺序（`late` 也拿 mid 的内容：残局仍然需要"怎么穿身份/倒钩/煽动"，
#: 只是不需要"第一天怎么上警" —— 所以过滤规则是"**去掉比当前阶段更早/更晚的专门节**"）
PHASE_ORDER = {"open": 0, "mid": 1, "late": 2}

#: **角色章继承**：某些角色要**连带**看另一个角色的章（2026-10-10 加，为狼王）。
#: 为什么必须这么做：狼王是**狼队**角色，但他不是 `wolf` —— 只按 `frag:role:wolf_king`
#: 过滤的话，他拿到的只有"狼队大战略 + 狼王那一小章"，**整本狼队打法（悍跳/倒钩/刀口/穿身份…）
#: 一个字都看不到**，等于拿着狼牌却只会一件事。所以狼王要连带拿 `role:wolf` 那章。
#: ⚠️ 这份继承**只影响主包**（`_filter_role`）：狼"穿身份"附参考章走 `filter_role_only`，
#: 那里把"不是你这个角色的章"整段丢掉 —— 否则小狼报一句「我是狼王」就会被附上整整一章狼人，
#: 而那一章他主包里早就有（实测过：同一章进两遍既烧 token、又会被读成两份互相冲突的书）。
ROLE_EXTRA_FRAGS: dict[str, tuple[str, ...]] = {"wolf_king": ("wolf",),
                                               "wolf_witch": ("wolf",)}


def _role_frag_keys(role_key: str) -> set[str]:
    """这个角色**能收**哪些 `frag:role:` 章（自己的 + 继承来的）。"""
    key = (role_key or "").strip().lower()
    keys = {key} | set(ROLE_EXTRA_FRAGS.get(key, ()))
    return {f"role:{k}" for k in keys if k}


def phase_of(state) -> str:
    """按**局面**算当前阶段（不只看天数）。

    · 人少（≤5 个活人）→ 残局，哪怕才第 2 天（少人局第 2 天就已经是残局了）；
    · 第 4 天及以后 → 残局；
    · 第 1 天 → 首夜/第一天；
    · 其它 → 前两轮。
    """
    day = int(getattr(state, "day", 1) or 1)
    seats = list(getattr(state, "seats", []) or [])
    alive = sum(1 for x in seats if getattr(x, "alive", False))
    if alive and alive <= 5:
        return "late"
    if day >= 4:
        return "late"
    if day <= 1:
        return "open"
    return "mid"


#: 夜间动作任务：只发“夜里怎么动”的段落。
NIGHT_TASKS = frozenset(("witch", "dream", "guard", "lucky", "seer_check",
                         "wolf_intent", "wolf_talk", "wolf_kill"))
#: 白天说话/投票任务：只发“白天怎么打”的段落。
DAY_TASKS = frozenset(("speech", "pk_speech", "sheriff_speech", "last_words",
                       "vote", "sheriff_vote", "sheriff_run", "claim", "badge",
                       "wolf_explode", "knight_judge"))
#: 竞选相关任务：才发“上警报不报身份 / 上警代价”。
SHERIFF_TASKS = frozenset(("sheriff_run", "sheriff_speech", "sheriff_vote", "badge"))
#: 出局时的技能任务：猎枪、幸运儿子弹、狼王带人。
DEATH_TASKS = frozenset(("hunter_shoot", "lucky_shoot", "wolf_king_take"))


def task_triggers_for(task=None) -> set[str]:
    """只算**任务**触发的块（不含台账事件）。

    这些块全部在 `context.build` 的**包尾补章**里发；主角色书不带任务块，
    这样同一个座位在夜里/白天之间切换时，前面的稳定前缀不会因为任务变化而断缓存。
    """
    kind = str(getattr(task, "kind", "") or "")
    out: set[str] = set()
    if kind in NIGHT_TASKS:
        out.add("night_action")
    if kind in DAY_TASKS:
        out.add("day_speech")
    if kind in SHERIFF_TASKS:
        out.add("sheriff_election")
    if kind in DEATH_TASKS:
        out.add("death_action")
    return out


def triggers_for(state, seat, task=None) -> set[str]:
    """按**当前局面 + 当前任务**算命中哪些触发块。

    事件只从「已经发生、当前座位看得到」的台账里判断；不看未来。
    · `front_seer_claim`：今天前面已经有人公开声称预言家；
    · `night_action` / `day_speech` / `sheriff_election` / `death_action`：按 task.kind 决定。
    """
    out: set[str] = task_triggers_for(task)
    day = int(getattr(state, "day", 1) or 1)
    who = getattr(seat, "id", None)
    # 女巫的“救不救”只在**手里还有解药**时是有效判断题；不能用“第一夜”当条件。
    if str(getattr(task, "kind", "") or "") == "witch" \
            and str(getattr(seat, "role_key", "") or "") == "witch" \
            and bool(getattr(state, "witch_heal", False)):
        out.add("witch_save")
    for e in getattr(getattr(state, "ledger", None), "entries", []) or []:
        if int(getattr(e, "day", 0) or 0) != day:
            continue
        if getattr(e, "kind", "") != "claim":
            continue
        if getattr(e, "seat", None) == who:
            continue
        claim = str((getattr(e, "data", None) or {}).get("claim", "") or "")
        if "预言家" in claim or "预言家" in str(getattr(e, "text", "") or ""):
            out.add("front_seer_claim")
    return out


def _filter_part(text: str, phase: str) -> str:
    """只保留"当前阶段该看的"小节。

    ⚠️ 规则（2026-10-09 用户口径："开局根本用不到残局怎么打"）：
      · `open`  → 去掉 mid/late 的专门节；
      · `mid`   → 去掉 open 与 late 的专门节；
      · `late`  → 去掉 open 的专门节（**mid 的照样发**：残局仍然要穿身份、算屠边账）；
      · phase 为空 → 原样返回（老调用方 / 页面预览看到的还是全文）。
    """
    if not phase or phase not in PHASE_ORDER:
        return text
    cur = PHASE_ORDER[phase]
    out: list[str] = []
    keep = True
    skipping = False
    for line in text.splitlines():
        # ⚠️ 阶段过滤只删正文；但结构标记**不能跟着被删**，否则后面的
        # `_filter_role` 会丢掉角色状态或条件块的开/关。
        # part 标记仍要先参与 keep/skipping 计算，再把标记本身留着。
        m = _PART_MARK.match(line)
        if m:
            tag = m.group(1)
            # late 阶段要 mid 的内容；mid 阶段不要 late 的；其余按"等于当前阶段"处理
            if phase == "late":
                keep = tag in ("mid", "late")
            else:
                keep = (tag == phase)
            skipping = True
            out.append(line)
            continue
        if _PART_END.match(line):
            keep = True
            skipping = False
            out.append(line)
            continue
        if re.match(r"\s*<!--\s*frag:", line):
            out.append(line)
            continue
        if keep or not skipping:
            out.append(line)
    return "\n".join(out)


def _filter_role(text: str, role_key: str, camp: str = "", phase: str = "",
                 triggers: set[str] | None = None, keep_when: bool = False,
                 skills: set[str] | None = None) -> str:
    """按角色 + 阵营 + 阶段 + 事件过滤。

    角色/阵营标记：`<!-- frag:all -->`、`frag:camp:xx`、`frag:role:xx`、`frag:god`。
    事件标记：`<!-- frag:when:name -->` … `<!-- frag:endwhen -->`；`triggers=None`
    时保留整块（页面预览/老调用方），传空集合时全部收起。
    """
    key = (role_key or "").strip().lower()
    camp_key = (camp or "").strip().lower()
    try:
        from .roles import ROLES
        # ⚠️ 这里把**预言家排除**在"神牌通用"之外：那一段讲的是"被排进查验顺序时怎么办"，
        #    预言家自己是查验方，拿到它只是噪音（他是主动报身份的那一方）。
        is_god = (getattr(ROLES.get(key), "kind", "") == "god" and key != "seer")
        if not camp_key:
            # 没传阵营就按座位角色推一个（`ROLES[x].camp` 是 "wolf"/"good"）
            camp_key = str(getattr(ROLES.get(key), "camp", "") or "").strip().lower()
    except Exception:                                     # noqa: BLE001
        is_god = key in ("witch", "hunter", "idiot")
    # 先按"阶段"把不该看的小节整块去掉（没传 phase 就什么都不动）
    if phase:
        text = _filter_part(text, phase)
    skill_set = set(skills or ())
    skill_filter = skills is not None
    out: list[str] = []
    keep = True
    #: 条件块可能嵌套（例如 `day_speech` 里再套 `front_seer_claim`）。
    #: 必须用栈：只记一个布尔时，内层结束后外层后续正文会漏出来（实测主包重复进一段）。
    when_stack: list[bool] = []
    for line in text.splitlines():
        m_when = _WHEN_MARK.match(line)
        if m_when:
            when_stack.append(triggers is None or m_when.group(1) in triggers)
            if keep_when:
                out.append(line)
            continue
        if _WHEN_END.match(line):
            if when_stack:
                when_stack.pop()
            if keep_when:
                out.append(line)
            continue
        if any(not on for on in when_stack):
            continue
        # ⚠️ `part:*` / `endpart` 也要认：不带 phase 时 `_filter_part` 原样返回，
        #    这些标记行必须在这里被消费掉，否则会漏成可见文字进包（t_playbook 1i 就查这个）。
        marker = re.match(r"\s*<!--\s*frag:(all|god|role:[a-z_]+|skill:[a-z_]+|"
                          r"camp:(?:wolf|good)|wolf|good|end"
                          r"|part:(?:open|mid|late)|endpart)\s*-->\s*$", line)
        if marker:
            what = marker.group(1)
            if what.startswith("part:") or what == "endpart":
                continue                      # 阶段标记：什么都不改（该发的正文照发）
            if what in ("end", "all"):
                keep = True
            elif what.startswith("skill:"):
                keep = (not skill_filter) or (what.split(":", 1)[1] in skill_set)
            elif what == "god":
                keep = bool(is_god)
            elif what.startswith("camp:") or what in ("wolf", "good"):
                side = what.split(":")[-1]
                # 没给阵营信息时**保守起见发出去**（否则老调用方会静默少一整段内容）
                keep = True if not camp_key else (side == camp_key)
            else:
                # 角色章：自己那一章 + `ROLE_EXTRA_FRAGS` 里继承来的那一章（狼王 → 狼人章）
                keep = (what in _role_frag_keys(key))
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def when_blocks(text: str, role_key: str, camp: str = "", phase: str = "",
                triggers: set[str] | None = None) -> str:
    """只取出**命中的 `frag:when` 块**，用于主包之外的“任务补章”。

    为什么单独做：主角色书不再按任务删段（那样会改前缀、断缓存），而是先把
    **所有** when 块从主包去掉，再把这一次命中的块贴到包尾。正文和原来一样，
    只是搬了位置；`triggers=None` 按空集处理（不附任何块）。
    """
    if not text:
        return ""
    wanted = set() if triggers is None else set(triggers)
    body = _filter_role(text, role_key, camp, phase, triggers=None, keep_when=True)
    out: list[str] = []
    #: 栈里每个条件块的 `(名字, 本块自己是否命中)`。嵌套块的正文要求**每一层都命中**：
    #: 只命中内层 `front_seer_claim` 而外层 `day_speech` 没命中时，内层不能单独漏出来。
    stack: list[tuple[str, bool]] = []
    for line in body.splitlines():
        m = _WHEN_MARK.match(line)
        if m:
            stack.append((m.group(1), m.group(1) in wanted))
            continue
        if _WHEN_END.match(line):
            if stack:
                stack.pop()
            continue
        if stack and all(on for _, on in stack):
            out.append(line)
    return _clean("\n".join(out)).strip()


def playbook_block(text: str, title: str = "") -> str:
    """把打法书包成 packet 里的一块（并清掉可能残留的标记行）。

    `title` 是**附加参数**（2026-10-08 加，默认空 = 老行为一字不改）：
    给狼附「它声称的那个身份」的章节时要用自己的抬头（见 `context.build`）——
    包两个【打法书】抬头会让模型以为是两份书冲突，而不是"这一章现在也是你的"。
    """
    if not text:
        return ""
    text = "\n".join(ln for ln in text.splitlines()
                     if not re.match(r"\s*<!--\s*frag:", ln))
    head = title or (PLAYBOOK_TITLE + "（只讲怎么打；规则见规则书，位置概率见概念库）")
    return head + "\n" + text

PLAYER_TITLE = "【你的定位】"


def claimed_role_title(role_name: str, assumed: bool = False, raw: str = "") -> str:
    """狼「穿身份」参考章的抬头（2026-10-08）。放在这里是为了两处引用同一个说法，
    不会一处改了另一处还是老抬头。

    `assumed=True`：狼**还没报过**身份 → 这一章是系统按"第一天多半要悍跳预言家"兜的
    默认，抬头必须说清「**还没报**、这只是给你备着的」，否则模型会以为自己已经报过了、
    把这一步跳过去（那就白兜了）。

    `raw="我是好人"`（P3-1）：狼**报过**了、但报的说法认不出来（不在别名表里）。
    这时**不能**说「你还没有向任何人报过身份」—— 引擎已经写了公开的 claim 条目，
    同一份包里那两句会直接打架。抬头改成如实转述 + 中性口径：
    这一章是备着参考的，不是"你现在穿的衣服"。
    """
    if assumed:
        return (f"【你还没报身份 —— 这一章备着演{role_name}用】\n"
                f"你**还没有**向任何人报过身份。这里是{role_name}这一章的原文，"
                "给你**备着**：第一天要抢的硬身份基本都是预言家，等你决定穿这件衣服，"
                "就照它的**口吻和纪律**说话（全程第一人称、报验人、把对跳的打死），"
                "出手手法仍按你自己的狼章来。\n"
                "⚠️ 别把这一章当成本局的事实：你没有它的技能，"
                "「验人结果」这类信息只能来自你**自己编好的那套说法**。")
    if raw:
        # ⚠️ 别把 raw 原样拼进"你说自己是X"这种句式：它是模型写的自由文本，
        #    可能很长、也可能自己带括号。用「」框起来、只做引用。
        return (f"【你报的身份：{raw} —— 这一章备着参考】\n"
                f"你**已经**向场上报过身份（原话如上）。你报的这个说法不在"
                f"系统认识的名单里，所以这里按真局经验给你一份{role_name}章的原文"
                "**备着参考**：如果你本来就是想演这个身份，"
                "就照它的**口吻和纪律**说话（全程第一人称、报验人、把对跳的打死）；"
                "出手手法仍按你自己的狼章来。\n"
                "⚠️ 别把它当成本局的事实，也别把「报过」当成「坐实」：你没有它的技能，"
                "「验人结果」这类信息只能来自你**自己编好的那套说法**，"
                "而且必须跟你已经说过的话对得上。")
    return (f"【你声称的身份：{role_name}（穿身份的参考章）】\n"
            "⚠️ 这是参考资料，不是当前任务；当前任务和输出格式在包尾。\n"
            "你**已经报名**了这个身份，所以下面这一章也是你的："
            "照着它的**口吻和纪律**说话，出手手法仍按你自己的狼章来。\n"
            "⚠️ 这是**真玩家手里那张牌**的思路，不是给你换打法的："
            "你没有它的技能，任何只有真神才知道的信息（验人结果、药、刀口、守护对象）"
            "都必须来自你**自己编好的那套说法**，而且要和你前面几轮说过的话对得上。")



def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""
    except UnicodeDecodeError as exc:
        # ⚠️ 2026-10-30：content/*.md 被另存成 GBK/ANSI 时不能直接崩开局
        #    （UnicodeDecodeError 不是 OSError）。按"这一本读不到"处理并留一行日志。
        print(f"[tabletalk] {path} 不是 UTF-8，按空内容处理：{exc}")
        return ""


#: 模型包里的作者注/日期（`（2026-11-07 真局补）`、`（用户 2026-10-08 复盘）`）。
#: 这些是给维护者看的变更痕迹，不是给模型判断的规则；留在指令里只会增加噪音。
_AUTHOR_NOTE = re.compile(r"（[^（）]*20\d{2}-\d{1,2}-\d{1,2}[^（）]*）")


def _clean(text: str) -> str:
    """去掉 markdown 噪音（标题井号、粗体星号）和作者注/日期，模型读纯文本更省心。"""
    text = _AUTHOR_NOTE.sub("", text)
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith(">"):
            line = line.lstrip("> ").strip()          # 引言块去掉 >
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            out.append("" if not title else f"· {title}")
            continue
        if re.fullmatch(r"[-|: ]+", line or "-"):      # 表格分隔行
            continue
        out.append(line.replace("**", ""))
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def filter_camp(text: str, camp: str) -> str:
    """公开别名：给 context.build 用（按阵营过滤 `<!-- frag:wolf|good -->` 段落）。"""
    return _filter_camp(text, camp)


def filter_role(text: str, role_key: str, camp: str = "", phase: str = "",
                triggers: set[str] | None = None, skills: set[str] | None = None) -> str:
    """公开别名：给 context.build 用（按角色 + 阵营 + 阶段 + 事件 + 技能过滤）。"""
    return _filter_role(text, role_key, camp, phase, triggers, skills=skills)


def filter_role_only(text: str, role_key: str, phase: str = "") -> str:
    """只取**这个角色自己那一章**（`frag:role:xx` + 女巫/猎人/白痴的 `frag:god`），
    **不含** `frag:all` 的通用部分。

    给谁用：狼「穿身份」时附的参考章（见 `context.build`）。这里必须把通用部分剥掉 ——
    通用章狼在自己的包里**已经有一份**，再附一次就是同一段 5K 字进包两遍：
    既白烧 token，又因为"通用"和"角色"挨着出现两回，模型容易读成两份互相冲突的书。
    （2026-10-08 实测踩过：附上来的"预言家章"里开头又是整段通用部分。）

    ⚠️ 判定条件跟 `_filter_role` **逐条对齐**（哪些标记收、`is_god` 怎么算）——
    这是一份让 `_filter_role` 再跑一遍、同时把"通用片段"里的正文行丢掉的外壳。
    为什么不给 `_filter_role` 加参数：那是**生产路径**（`context.build` 主包）在用的函数，
    这里多一次过滤只影响狼的参考章，不去动已经在跑的那条路。
    """
    key = (role_key or "").strip().lower()
    try:
        from .roles import ROLES
        # ⚠️ 这里把**预言家排除**在"神牌通用"之外：那一段讲的是"被排进查验顺序时怎么办"，
        #    预言家自己是查验方，拿到它只是噪音（他是主动报身份的那一方）。
        is_god = (getattr(ROLES.get(key), "kind", "") == "god" and key != "seer")
    except Exception:                                     # noqa: BLE001
        is_god = key in ("witch", "hunter", "idiot")

    lines = text.splitlines()
    # 标记所在的行号：`_filter_role` 只认这些行、把它们消费掉，所以这里必须自己认一遍。
    # ⚠️ 认的标记集合必须跟 `_filter_role` **完全一致**（含 2026-10-09 加的阵营标记）——
    #    少认一种，那个标记就不会被消费掉，`_filter_role` 会因为"标记序列断了"而收错段。
    marks: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*<!--\s*frag:(all|god|role:[a-z_]+|skill:[a-z_]+|"
                     r"camp:(?:wolf|good)|wolf|good|end"
                     r"|part:(?:open|mid|late)|endpart)\s*-->\s*$", line)
        if m:
            marks.append((i, m.group(1)))

    drop: set[int] = set()
    #: ⚠️ 判据是**精确的那一章**（不是 `_role_frag_keys` 的继承集合）：参考章是"补给你这一手用的"，
    #:    继承来的那一章（狼王 → 狼人章）**他自己主包里已经有**，附进来就是同一章进两遍。
    own_role_frag = f"role:{key}"
    for pos, (i, what) in enumerate(marks):
        # 通用片段（frag:all）与**阵营片段**（frag:camp:xx）里的正文都丢掉：
        # 狼在自己主包里已经拿到过这两段，附参考章时再来一遍就是同一段文字进包两遍。
        is_camp = what in ("wolf", "good") or what.startswith("camp:")
        # ⚠️ 别的**角色章**也一起丢（2026-10-10）：狼王章继承着 `role:wolf`，而"我是狼王"
        #    这句自称会让引擎给一头小狼附参考章 —— 不丢的话，附上来的东西里带着整整一章狼人，
        #    而那一章他主包里早就有（P3-3 的同一类事故：同一章进两遍）。
        is_other_role = what.startswith("role:") and what != own_role_frag
        is_skill = what.startswith("skill:")
        if what != "all" and not is_camp and not is_other_role and not is_skill:
            continue
        # ⚠️ 段的结束位置必须是**它自己的 `frag:end`**，不能是"下一个标记"：
        #    角色章内部有 `frag:part:open/mid/late` 这些标记，按"下一个标记"算的话
        #    只会丢掉开头那一小段，**整章的正文照样留在参考章里**
        #    （2026-10-10 实测：狼报「我是狼王」时，附上来的参考章里带着整整一章狼人，
        #      22K 字的狼章因此进了第二次包 —— 正是引擎侧 P3-3 要避免的"同一章进两遍"）。
        end = len(lines)
        for pos2 in range(pos + 1, len(marks)):
            if marks[pos2][1] == "end":
                end = marks[pos2][0]
                break
        for j in range(i + 1, end):
            s = lines[j].strip()
            # ⚠️ 通用片段**保留标题行**（结构还在，一眼能看出"这里原本有一段通用的"）；
            #    但**阵营片段连标题一起丢** —— 参考章是给狼"穿身份"用的，
            #    里面挂着一行「好人战略」纯属噪音（2026-10-09：3b 就是被这个标题判红的）。
            if is_camp or is_other_role or is_skill:
                drop.add(j)
                continue
            # ⚠️ 判的是**原始行**：`_clean()` 会先把 `### x` 变成 `· x`，
            #    所以 `#` 开头的标题也要放行，不能只认 `·`。
            if s and not s.startswith(("#", "·")):
                drop.add(j)
    out = [ln for i, ln in enumerate(lines) if i not in drop]
    # ⚠️ 标记行必须**原样留着**交给 `_filter_role` —— 它靠这些标记判断收哪一段；
    #    在这里就把标记删掉的话，`_filter_role` 会以为全文都没有标记 → **整本原样返回**
    #    （2026-10-08 实测踩过：附上来的"预言家章"里带着狼章和村民章，比主包还长）。
    #
    # ⚠️⚠️ 但**阵营片段**是例外：它的正文已经被上面丢掉了，而标记还必须**一起删** ——
    #    否则下面这次 `_filter_role(...)`（不带 camp，按"保守发全"处理）会把它当成
    #    "阵营未知 → 发出去"，等于把**好人战略**塞进狼的穿身份参考章里
    #    （2026-10-09 实测：3b「狼的包里没有好人战略」就是这么红的）。
    #    删掉它不会触发上面那个"整本原样返回"的坑：别的标记（role/all）都还在。
    out = [ln for ln in out
           if not re.match(r"\s*<!--\s*frag:(camp:(?:wolf|good)|wolf|good)\s*-->\s*$", ln)]
    # 按阶段过滤：这是"狼穿身份时附的参考章"，同样不该把残局/开局专门节全塞进去
    if phase:
        out = _filter_part("\n".join(out), phase).splitlines()
    # 参考章只要角色本身：技能书片段一律不附（主包已按 active_skills 发过）。
    return _clean(_filter_role("\n".join(out), key, "", phase, skills=set()))


def load_meta_book(project_root: str, path: str = "", camp: str = "") -> str:
    """读概念库的进包段（默认 `content/概念库.md`）。读不到就返回空串（不阻断开局）。

    ⚠️ 2026-10-30：旧注释还写着「默认 场外玄学.md」——那个文件早就并入概念库、不存在了。

    ⚠️ 按**阵营**分发：文件里用 `<!-- frag:wolf -->` / `<!-- frag:good -->` 标出"只有这一边看"
    的章节（狼的屠边账与穿衣服、好人的轮次校准）。这样每个座位只拿到跟自己有关的打法，
    既省 token 又更聚焦；阵营为空（比如 mock/测试）时两段都给。
    """
    name = str(path or DEFAULT_META_PATH)
    full = name if os.path.isabs(name) else os.path.join(project_root, name)
    text = _read(full)
    if not text:
        return ""
    # ⚠️ 合并之后这个概念库文件里既有"给人看"的机制章节、也有"进包"的概率段落：
    #    只把 `<!-- frag:meta:begin/end -->` 圈出来的那一段送进包。
    #    标记缺失说明文件被人改坏了 —— 这时**宁可返回空**（不阻断开局、也不偷偷塞整本进去）。
    _m = re.search(r"<!--\s*frag:meta:begin\s*-->(.*?)<!--\s*frag:meta:end\s*-->",
                   text, re.S)
    if not _m:
        return ""
    text = _m.group(1).strip()
    if camp:
        # 指定了阵营：这里就过滤好
        return _clean(_filter_camp(text, camp))
    # ⚠️ 没指定阵营时**保留 `<!-- frag:xx -->` 标记**：服务端只加载一次（阵营未知），
    #    真正按座位过滤是在 context.build 里做的。第一版在这里就过滤了一遍（camp=""),
    #    标记被消费掉 → 后面再想按阵营分就已经没有标记可用了（实测两个阵营拿到同一份）。
    return _clean(text)


def _filter_camp(text: str, camp: str) -> str:
    """按 `<!-- frag:wolf -->…<!-- frag:end -->` 过滤阵营专属段落。"""
    camp = (camp or "").strip().lower()
    out: list[str] = []
    keep = True
    for line in text.splitlines():
        marker = re.match(r"\s*<!--\s*frag:(wolf|good|end)\s*-->\s*$", line)
        if marker:
            what = marker.group(1)
            if what == "end":
                keep = True
            else:
                keep = (not camp) or (camp == what) or (
                    # 狼阵营的"狼"、好人阵营的"好人/神/民"都要能对上
                    what == "wolf" and camp in ("wolf", "狼")
                ) or (what == "good" and camp in ("good", "好人"))
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def player_prompt(cfg: dict, project_root: str) -> str:
    """通用玩家提示词：配置里可以直接写一段话，也可以写一个 .md 文件名。"""
    game_cfg = ((cfg or {}).get("game") or {})
    raw = str(game_cfg.get("player_prompt") or "").strip()
    if not raw:
        return ""
    candidate = raw if os.path.isabs(raw) else os.path.join(project_root, raw)
    if len(raw) < 200 and os.path.isfile(candidate):
        return _clean(_read(candidate))
    return _clean(raw)


def player_block(text: str) -> str:
    if not text:
        return ""
    return (PLAYER_TITLE + "（每个座位都一样：只讲怎么打，不给你任何额外信息）\n" + text)


def meta_block(text: str) -> str:
    """把玄学书包成 packet 里的一块，并再强调一遍纪律（怕模型把它当硬证据）。

    顺手把可能残留的 `<!-- frag:xx -->` 标记行去掉 —— 万一哪条路径忘了过滤，
    也绝不让这种内部标记进到模型眼前。
    """
    if not text:
        return ""
    text = "\n".join(ln for ln in text.splitlines()
                     if not re.match(r"\s*<!--\s*frag:", ln))
    return (META_TITLE + "\n" + text +
            "\n（提醒：这是**位置概率**，投票/毒/枪/刀/验人都能当参考，"
            "但只在两个选择五五开时用来打破僵局；**不能推翻发言和票型**，"
            "也不许在发言里说「数学上他就是狼」。）")


def lint_meta_book(text: str) -> list[str]:
    """玄学书允许出现身份名（它讲的本来就是"预言家报金水之后…"这类概率），
    但不允许出现**第一人称的身份声明**（那等于给人设塞身份，见 wolf/persona.py）。
    """
    if not text:
        return []
    hay = persona_mod._norm_for_match(text)
    bad = [w for w in ("我是狼", "我是预言家", "我是女巫", "我是猎人", "我的身份是",
                   "我是愚者", "我是白痴", "我是摄梦人", "我是守墓人", "我是养鸦人", "我是幸运儿", "我是骑士",
                   "我是狼王", "我是狼巫")
           if persona_mod._norm_for_match(w) in hay]
    return [f"概念库（进包段落）里出现了第一人称身份声明：{('、'.join(bad))}"] if bad else []
