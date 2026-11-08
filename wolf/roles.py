# coding=utf-8
"""角色与板子定义。

板子是**数据**，不是硬编码规则：内置 4 套（6 / 9 / 11 / 12 人），你也可以在 config.yaml 里
自己写一套（任意人数）。校验函数会拦下明显不平衡的板子。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

WOLF = "wolf"
GOOD = "good"


@dataclass(frozen=True)
class RoleDef:
    key: str
    name: str
    camp: str          # wolf / good
    kind: str          # wolf / god / villager  —— 用于屠边判定
    night: str | None  # kill / check / witch / guard / shoot_on_death / None
    brief: str         # 一句话身份说明，会进模型上下文
    #: 这张牌拥有的技能 key（`wolf/skills.py` 的唯一归属）。
    #: 换板子/加角色时只看这张表；不在板子里的角色不会编译出它的技能。
    skills: tuple[str, ...] = ()
    #: 屠边判定时**同时**算成另一种（"白痴算民也算神"就靠它）。
    #: ⚠️ 这条规则很要紧：狼人不管想屠神还是屠民，都必须把白痴也杀死 ——
    #: 否则会在白痴还活着的时候被误判成"那一边已经死光"，狼人白捡一个胜场。
    also_kind: str | None = None


ROLES: dict[str, RoleDef] = {
    "wolf": RoleDef(
        "wolf", "狼人", WOLF, "wolf", "kill",
        "你和狼队友每晚共同击杀一名玩家。白天你要伪装成好人，误导好人投票。",
    ),
    "seer": RoleDef(
        "seer", "预言家", GOOD, "god", "check",
        "你每晚可以查验一名玩家的阵营（狼人 / 好人）。你要把查到的信息用发言传出去，"
        "同时别被狼人发现你是预言家。",
    ),
    "witch": RoleDef(
        "witch", "女巫", GOOD, "god", "witch",
        "你有一瓶解药（救回当晚被狼人杀害的玩家）和一瓶毒药（毒死任意一名玩家），各只能用一次，"
        "且同一夜不能同时使用；**被狼刀的就是你自己那一晚，你也可以用解药自救**。",
    ),
    "hunter": RoleDef(
        "hunter", "猎人", GOOD, "god", "shoot_on_death",
        "你被投票出局或被狼人杀死时可以开枪带走一名玩家；但被女巫毒死时不能开枪。",
    ),
    "guard": RoleDef(
        "guard", "守卫", GOOD, "god", "guard",
        "你每晚可以守护一名玩家（可以守自己，但不能连续两晚守同一个人）。"
        "被守护的人当晚被狼刀不会死；但如果同时被女巫用解药救，依然会死（同守同救）。",
    ),
    "villager": RoleDef(
        "villager", "村民", GOOD, "villager", None,
        "你没有技能，只能靠发言和投票找出狼人。",
    ),
    #: 2026-10-09 用户改定：本项目默认板子把**守卫换成摄梦人**（11 人预女白梦）。
    #: 机制来源＝官方手册「角色→摄梦人」技能原文：
    #:   · 每晚可选一名玩家成为梦游者（本项目允许空过，官方“必须”已改）；梦游者不知道自己梦游，且**免疫夜间伤害**；
    #:   · 摄梦人夜晚死亡不再带走梦游者（2026-10-10 取消连死）；
    #:   · **连续两晚**成为梦游者也会死亡。
    #: 用户附加的口径（以用户为准）：**摄梦对象 + 女巫解药 = 奶穿**（和同守同救一样，照样死）。
    "dreamer": RoleDef(
        "dreamer", "摄梦人", GOOD, "villager", "dream",
        "你每晚**可以**选一名玩家成为梦游者，也可以选择今晚空过：他不知道自己正在梦游，而且**免疫夜间伤害**"
        "（狼刀、毒药都伤不到他）。**连续两晚**摄同一个人，那个人会死；"
        "你**在夜里出局不会带走**当晚的梦游者。"
        "⚠️ 但如果你摄的人同时又挨了女巫的解药 —— **照样死**（奶穿，和同守同救一个道理）。",
    ),
    "idiot": RoleDef(
        #: 2026-10-09 用户改名：白痴 → **愚者**（key 保持 `idiot` 不变，避免全项目大改；
        #: 显示名与文案一律用「愚者」）。仍然**神民双料**。
        "idiot", "愚者", GOOD, "god", None,
        "你没有主动技能。你可以**免疫一次任何原因的出局**（被放逐、被刀、被毒、被奶穿、"
        "同刀同毒、两连梦、被枪/被子弹/被狼王带走都算）：第一次致命结算时翻开牌亮出【愚者】身份，"
        "取消这次出局；之后**仍然可以投票，票权为 0.5 票**（不是失去投票权）。"
        "免疫只有一次，之后再死就是真死。"
        "⚠️ 屠边判定里你**既算神也算民**：狼人想屠神或屠民，都必须连你一起杀死。",
        also_kind="villager",
    ),
    #: —— 下面三张：官方都是神牌，**本项目一律当民牌**（用户 2026-10-09 改定），
    #:    用来替换原来的三个村民。用户原话：这三张"成神太拉了，还是叫民吧"。
    "gravekeeper": RoleDef(
        "gravekeeper", "守墓人", GOOD, "villager", "grave",
        "每天**白天轮到你发言时自动**（强制发动、不用你决定）得知**上一个白天被放逐的玩家**是好人还是狼人 —— "
        "只知道阵营，不知道具体身份。白天没人被放逐（平票/无人出局）时不会给你新信息。"
        "你的价值是把「白天推出去的那个人推对没有」变成一条**确定信息**，"
        "帮好人校准之后的票。",
    ),
    "crow": RoleDef(
        "crow", "养鸦人", GOOD, "villager", "curse",
        "**白天放逐投票那一刻**（投出自己那一票之前）临时决定：**要不要诅咒、开在谁头上**"
        "（可以咒自己、也可以不用）—— 被咒的人**这一次放逐投票额外多一票**。"
        "✅ **可以连着两天甚至更多天咒同一个人**（这张牌本质是「两票的民牌」，"
        "就该能天天把同一张像狼的牌往死里压）。"
        "诅咒**对警长竞选投票无效**，但在**放逐投票**（含平票后的 PK 重投）里都有效；"
        "结果出来时**被咒的人会公开带「咒标」**（多那一票看得见），但**不公布是谁咒的**。",
    ),
    #: —— 狼王（作者 2026-10-10 拍板的新板子 `eleven_wolfking` 的核心角色）。
    #: 官方有两个容易混的狼：**狼王**（出局后能带人，被毒**不能**带）与
    #: **白狼王**（自爆时带人）。本项目按作者口径把两者**合成一张牌**：
    #:   · **任何原因出局都能带走一人**（作者明确：**被毒也能带**，推翻"保留官方例外"的建议）；
    #:   · **狼王不能自爆**（2026-11-03 新规，11 人板）：自爆这条路由 `Game.can_explode()` 关闭；
    #:   · **不能带走狼队友**（作者定：带队友做身份的收益远不如带好人，而且禁止能省 AI 的思考额度）；
    #:   · 定位：补"首夜无刀"缺的那一刀 —— 只要他不是最后一个走，某种意义上等于**两刀**。
    "wolf_king": RoleDef(
        "wolf_king", "狼王", WOLF, "wolf", "take_on_death",
        "你属于狼队，夜里和队友共同出刀（你没有额外的夜间技能）。"
        "你的特权是**出局时**：**除被骑士审判同归于尽外**，只要你出局"
        "（被刀 / 被毒 / 被梦死 / 被奶穿 / 被放逐 / 被枪带走），"
        "你都可以**当场带走场上一名好人**（**不能带走狼队友**，带谁要公开指定）。"
        "⚠️ 本板子狼王**不能自爆**：自爆这条路对你关闭。",
    ),
    "wolf_witch": RoleDef(
        "wolf_witch", "狼巫", WOLF, "wolf", "kill",
        "你属于狼队，夜里和队友共同出刀。**从第 2 夜起**，你每晚可以额外诅咒一张活着的牌："
        "被诅咒者**本夜和次日**失去技能（狼刀照常另算）。你的行动和狼刀合在一次决策里："
        "先输出 `curse` 要诅咒谁，再输出 `target` 要刀谁；`curse` 可以填 null 不用。"
        "诅咒不会公开，目标会收到私有提示；它只封技能，不封发言和投票。",
    ),
    "knight": RoleDef(
        "knight", "骑士", GOOD, "god", None,
        "你每局有**一次**「骑士审判」：在白天放逐投票之前，可以公开翻牌审判一名其他玩家。"
        "对方是狼人 → 对方翻牌出局（公示身份）；对方是狼王 → 你与狼王一起出局，狼王不能带人；"
        "对方是好人 → 只公示对方是好人阵营（不公示职业），你不出局，但票权降为 0.5。"
        "无论审判结果如何，当天的放逐投票都照常进行。",
    ),
    "lucky": RoleDef(
        "lucky", "幸运儿", GOOD, "villager", "lucky",
        "你有一份**一次性**的幸运（本项目不含奇迹商人，技能直接在你身上）："
        "每个夜晚你都可以想想要不要用 —— 用的话**三选一**："
        "① **预言家的查验**（验一个人是好人还是狼人）；② **守卫的守护**（让一个人今夜免于狼刀）；"
        "③ **子弹**（不主动杀人；你出局时若死因是**被放逐**或**被狼刀**，可以开枪带走一个人自证，"
        "被毒死/被梦死不能开枪）。**只能使用一次**，用完就永远没有了"
        "（之后夜里也不会再问你）。⚠️ 守护与摄梦撞在同一个人身上一样会**奶穿**。",
    ),
}

#: 角色 → 技能归属。**唯一事实源**：SkillDef.owner_roles 必须与此一致。
#: 新增角色/技能先改这里，再在 `skills.py` 登记元数据；引擎的现有分支暂时不动。
_SKILLS_BY_ROLE = {
    "wolf": ("wolf_kill",),
    "seer": ("seer_check",),
    "witch": ("witch_potions",),
    "hunter": ("hunter_shoot",),
    "guard": ("guard",),
    "villager": (),
    "dreamer": ("dream",),
    "idiot": ("idiot_immune",),
    "gravekeeper": ("gravekeeper_result",),
    "crow": ("crow_curse",),
    "wolf_king": ("wolf_kill", "wolf_king_take"),
    "wolf_witch": ("wolf_kill", "wolf_curse"),
    "knight": ("knight_judge",),
    "lucky": ("lucky_skill", "lucky_bullet"),
}
for _role, _skill_keys in _SKILLS_BY_ROLE.items():
    ROLES[_role] = replace(ROLES[_role], skills=tuple(_skill_keys))


@dataclass
class BoardDef:
    name: str
    roles: list[str]
    win_rule: str = "sides"      # sides = 屠边（杀光神或杀光民）；all = 屠城（杀光所有好人）
    desc: str = ""
    #: 首夜无刀（作者 2026-10-10 定，`eleven_wolfking` 专用）：
    #: **第 1 夜狼队不行动、女巫不给药也不被告知刀口、摄梦人不上手**，整夜只跑预言家一次验人。
    #: 为什么要它（作者观察，已被三局真实思维链证实）：现行板子"首夜女巫手刀"让**银水变成铁好**
    #: ——狼"自刀骗药"即使成功也吞不掉两瓶药、反而白送一狼，EV 为负 ⇒ 三局里 77/20/6 处提到自刀
    #: 全部主动放弃 ⇒ 银水没人质疑，好人白拿一条确定信息。改机制，不是改打法书。
    first_night_quiet: bool = False
    #: **出局亮身份**（作者 2026-10-30 定的「6 人屠城明牌」板子）：
    #: 任何人出局时**当场公开他的身份牌**（夜里被刀/被毒/被梦死/被奶穿、白天被放逐，全都亮），
    #: **死因仍然不公示**（只亮身份、不解释怎么死的）。默认 False = 暗牌（只亮白痴翻牌）。
    reveal_on_death: bool = False
    #: 是否出现在产品板子列表里（旧的默认板降为隐藏的回归/兼容板时置 False）
    visible: bool = True
    #: **板子自己的技能顺序**；不写就按 SkillDef.order。Phase 0 只用于元数据/
    #: 校验/未来调度，现有 engine.night 仍按旧硬编码顺序跑。
    skill_order: tuple[str, ...] = ()
    #: 板子级技能变体（例如同一角色不同板子参数不同）；Phase 0 先留槽位。
    skill_variants: dict[str, str] = field(default_factory=dict)


def _b(name: str, roles: list[str], desc: str, win_rule: str = "sides",
       first_night_quiet: bool = False, reveal_on_death: bool = False,
       visible: bool = True, skill_order: tuple[str, ...] = (),
       skill_variants: dict[str, str] | None = None) -> BoardDef:
    return BoardDef(name=name, roles=roles, win_rule=win_rule, desc=desc,
                    first_night_quiet=first_night_quiet, reveal_on_death=reveal_on_death,
                    visible=visible, skill_order=tuple(skill_order),
                    skill_variants=dict(skill_variants or {}))


#: 常见配置，**不是官方标准**，你可以照着自己那套规则改，或者在 config.yaml 里自定义。
#: 2026-10-30 作者拍板：**只保留四张板子** —— 默认的狼王版 + 12 人预女猎白 +
#: 常规 9 人屠城（暗牌）+ 6 人屠城（明牌）。其余内置板子（eleven / eleven_standard /
#: eleven_dream / eleven_guard）**全部退役**，定义留在 `wolf/retired_boards.py`
#: （只给回归与考核题库复现历史机制，产品面板不列、`make_board` 也不认这些名字）。
#: 退役板子的完整备份与恢复办法见 `docs/板子备份_已删除.md`。
BUILTIN_BOARDS: dict[str, BoardDef] = {
    #: 2026-11-07 新默认板：幸运儿退场，摄梦人降为民（机制不变），新增骑士；
    #: 愚者改成「任何死因免疫一次 + 保留 0.5 票」。
    "eleven_knight": _b("eleven_knight",
                        ["wolf", "wolf", "wolf_king", "wolf_witch",
                         "seer", "witch", "knight", "idiot",
                         "dreamer", "gravekeeper", "crow"],
                        "11 人（狼巫版·**当前默认**）：2 小狼 + 1 狼王 + **1 狼巫** + 预言家、女巫、"
                        "**骑士**、**愚者** + **摄梦人（民）/守墓人/养鸦人**。"
                        "首夜无刀；女巫与摄梦人从第 2 夜起行动。"
                        "狼巫从第 2 夜起每晚先诅咒一张牌（本夜+次日失去技能），再和狼队出刀；"
                        "骑士每局一次白天审判（狼死/好人只公示阵营且骑士掉到 0.5 票/狼王与骑士同死且不能带人），"
                        "审判后仍正常放逐投票。愚者免疫一次任何出局、保留 0.5 票；算神也算民。",
                        first_night_quiet=True),
    #: 旧默认板：保留给回归/旧存档/考题，不在产品板子列表里显示。
    "eleven_wolfking": _b("eleven_wolfking",
                          ["wolf", "wolf", "wolf", "wolf_king",
                           "seer", "witch", "dreamer", "idiot",
                           "gravekeeper", "crow", "lucky"],
                          "11 人（旧狼王版，已隐藏）：3 狼 + 1 狼王 + 预言家、女巫、摄梦人、"
                          "愚者 + 守墓人/养鸦人/幸运儿（2026-11-07 被骑士版取代）。",
                          first_night_quiet=True, visible=False),
    "twelve_standard": _b("twelve_standard",
                          ["wolf", "wolf", "wolf", "wolf",
                           "seer", "witch", "hunter", "idiot",
                           "villager", "villager", "villager", "villager"],
                          "12 人（预女猎白）：4 狼 + 预言家、女巫、猎人、**愚者** + 4 民。"
                          "**屠边**：屠神 / 屠民二选一，愚者**既算神也算民**（两边都要连他一起杀）。"
                          "12 人 ≥ 11 ⇒ **有警长竞选**（警徽 1.5 票）。暗牌：出局不亮身份。"),
    "nine_all": _b("nine_all",
                   ["wolf", "wolf", "wolf", "seer", "witch", "hunter",
                    "villager", "villager", "villager"],
                   "9 人（常规·屠城·暗牌）：3 狼 + 预言家、女巫、猎人 + 3 民。"
                   "**屠城**：狼必须**杀光所有好人**才算赢，杀光神或杀光民都**不算**；"
                   "9 人 < 11 ⇒ 没有警长竞选。暗牌：出局不亮身份。",
                   win_rule="all"),
    "six_all_reveal": _b("six_all_reveal",
                         ["wolf", "wolf", "seer", "witch", "villager", "villager"],
                         "6 人（屠城·明牌）：2 狼 + 预言家、女巫 + 2 民。"
                         "**屠城**（杀光所有好人）+ **出局亮身份**：任何人出局时当场公开他的身份牌"
                         "（死因仍然不公示）；6 人 < 11 ⇒ 没有警长竞选。",
                         win_rule="all", reveal_on_death=True),
}


def role_name(key: str) -> str:
    return ROLES[key].name if key in ROLES else key


def camp_name(camp: str) -> str:
    return "狼人阵营" if camp == WOLF else "好人阵营"


def camp_label(camp: str) -> str:
    return "狼人" if camp == WOLF else "好人"


def is_wolf(key: str) -> bool:
    return key in ROLES and ROLES[key].camp == WOLF


def seats_text(ids) -> str:
    return "、".join(f"{i}号" for i in ids) if ids else "（无）"


def make_board(name: "str | BoardDef", custom: dict | None = None) -> BoardDef:
    """按名字取板子：优先用 config.yaml 里的自定义板子，其次内置。

    也接受**直接传一个 `BoardDef`**（原样返回一份拷贝）——
    `Game(board=...)` 本来就接受 BoardDef，让 `make_board` 也认它，
    调用方就不用为"板子对象还是板子名"分两条路（测试本地定义的板子常这么用）。
    """
    if isinstance(name, BoardDef):
        return BoardDef(name=name.name, roles=list(name.roles),
                        win_rule=name.win_rule, desc=name.desc,
                        first_night_quiet=bool(getattr(name, "first_night_quiet", False)),
                        reveal_on_death=bool(getattr(name, "reveal_on_death", False)),
                        visible=bool(getattr(name, "visible", True)),
                        skill_order=tuple(getattr(name, "skill_order", ()) or ()),
                        skill_variants=dict(getattr(name, "skill_variants", {}) or {}))
    if custom and name in custom:
        raw = custom[name] or {}
        roles = raw.get("roles") if isinstance(raw, dict) else raw
        return BoardDef(name=name, roles=list(roles or []),
                        win_rule=str((raw or {}).get("win_rule") or "sides") if isinstance(raw, dict) else "sides",
                        desc=str((raw or {}).get("desc") or "自定义板子") if isinstance(raw, dict) else "自定义板子",
                        # 自定义板子也能开「首夜无刀」（config.yaml 里写 first_night_quiet: true）
                        first_night_quiet=bool((raw or {}).get("first_night_quiet")) if isinstance(raw, dict) else False,
                        # 自定义板子也能开「出局亮身份」（config.yaml 里写 reveal_on_death: true）
                        reveal_on_death=bool((raw or {}).get("reveal_on_death")) if isinstance(raw, dict) else False,
                        visible=bool((raw or {}).get("visible", True)) if isinstance(raw, dict) else True,
                        skill_order=tuple((raw or {}).get("skill_order") or ()) if isinstance(raw, dict) else (),
                        skill_variants=dict((raw or {}).get("skill_variants") or {}) if isinstance(raw, dict) else {})
    if name in BUILTIN_BOARDS:
        # ⚠️ 必须**拷贝**再返回：直接返回模块级对象的话，谁写一句
        # `board.roles.append(...)` 就把 BUILTIN_BOARDS 本身改了，
        # 从此这台机器上所有用同一个板子的对局都跟着变（实测过：多一个座位）。
        base = BUILTIN_BOARDS[name]
        return BoardDef(name=base.name, roles=list(base.roles),
                        win_rule=base.win_rule, desc=base.desc,
                        first_night_quiet=bool(getattr(base, "first_night_quiet", False)),
                        reveal_on_death=bool(getattr(base, "reveal_on_death", False)),
                        visible=bool(getattr(base, "visible", True)),
                        skill_order=tuple(getattr(base, "skill_order", ()) or ()),
                        skill_variants=dict(getattr(base, "skill_variants", {}) or {}))
    raise KeyError(f"没有这个板子：{name}（内置：{'、'.join(BUILTIN_BOARDS)}）")


def validate_board(board: BoardDef) -> list[str]:
    """拦下明显有问题的板子，返回人话问题清单。"""
    issues: list[str] = []
    roles = board.roles
    if not roles:
        return [f"{board.name}：板子是空的"]
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        issues.append(f"{board.name}：有没定义的角色 {sorted(set(unknown))}")
    # 唯一角色不能出现两次（审查 2026-10-07）：规则上预言家/女巫/猎人/守卫/白痴
    # 都只有一个；重复会让 rules.compose 去重后只发一次片段（模型不知道场上有俩），
    # 屠神边也要多杀一个，而"对跳预言家"整类判断失去基准。
    # ⚠️ 2026-10-25 修：这段原来被缩进在上面 `if unknown:` 里面 —— 于是**只有拼错角色名时才会查重复**，
    #    正常的板子写两个预言家能一路通过校验。（已复现：`[wolf,wolf,seer,seer,...]` 当时 issues 为空。）
    for _k in ("seer", "witch", "hunter", "guard", "idiot", "knight"):
        if list(roles).count(_k) > 1:
            issues.append(f"唯一角色 {_k} 出现了 {list(roles).count(_k)} 次")
    if len(roles) < 5:
        issues.append(f"{board.name}：只有 {len(roles)} 个座位，太少了（至少 5 人）")
    if len(roles) > 18:
        issues.append(f"{board.name}：{len(roles)} 个座位太多了（最多 18）")
    wolves = [r for r in roles if r in ROLES and ROLES[r].camp == WOLF]
    gods = [r for r in roles if r in ROLES and ROLES[r].kind == "god"]
    villagers = [r for r in roles if r in ROLES and ROLES[r].kind == "villager"]
    if not wolves:
        issues.append(f"{board.name}：没有狼人")
    if not gods and not villagers:
        issues.append(f"{board.name}：除了狼人没有别的角色")
    if wolves and len(wolves) >= len(roles) - len(wolves):
        issues.append(f"{board.name}：狼人 {len(wolves)} 个，好人才 {len(roles) - len(wolves)} 个，"
                      f"开局狼就赢了")
    if board.win_rule == "sides":
        if not gods:
            issues.append(f"{board.name}：屠边规则要求至少有一个神职")
        if not villagers:
            issues.append(f"{board.name}：屠边规则要求至少有一个村民")
    if board.win_rule not in ("sides", "all"):
        issues.append(f"{board.name}：win_rule 只能是 sides（屠边）或 all（屠城），现在写的是 {board.win_rule}")
    # 技能库 Phase 0：角色引用的每个技能都必须在 SKILLS 登记，且归属一致。
    try:
        from .skills import validate_board_skills
        issues.extend(validate_board_skills(board))
    except Exception as exc:  # noqa: BLE001  —— 技能库自身坏掉也要变成人话，而不是建局崩
        issues.append(f"{board.name}：技能库校验失败（{type(exc).__name__}: {exc}）")
    return issues


def _role_counts(keys: list[str]) -> str:
    """按「角色×N」写一段，重复角色合并计数、顺序按首次出现。

    空列表给"无神职"而不是空字符串 —— 否则 summary 里会拼出
    `2 狼 +  + 3 民` 这种两个加号挨着的空段。
    """
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "无神职"
    return "、".join(name if n == 1 else f"{name}×{n}"
                     for name, n in ((role_name(k), n) for k, n in counts.items()))


def board_summary(board: BoardDef) -> str:
    roles = list(board.roles or [])
    wolves = sum(1 for r in roles if ROLES.get(r) and ROLES[r].camp == WOLF)
    gods = [r for r in roles if ROLES.get(r) and ROLES[r].kind == "god"]
    villagers = sum(1 for r in roles if ROLES.get(r) and ROLES[r].kind == "villager")
    # 没定义的角色名（写错的板子）以前直接从摘要里消失，看起来像"5 人：2 狼 + 3 民"
    # 这种算不平的账；单独列出来，让摘要自己暴露问题。
    unknown = [r for r in roles if r not in ROLES]
    rule = "屠边" if board.win_rule == "sides" else "屠城"
    # 人数用 len(roles) 而不是把各段相加：这样坏板子也照样显示"板子上真有 N 个座位"
    out = (f"{len(roles)} 人：{wolves} 狼 + {_role_counts(gods)} + {villagers} 民（{rule}）")
    # 首夜无刀是**板子级**的规则差异（同一批角色、完全不同的开局），摘要里必须写出来 ——
    # 否则面板上「狼王版」和「预女梦愚版」看起来只差一个角色，实际第一天流程不一样。
    if getattr(board, "first_night_quiet", False):
        out += "　首夜无刀"
    # 出局亮身份同样是**板子级**的规则差异：摘要里写出来，手机挑板子时一眼能看出
    # 「6 人屠城明牌」和「6 人屠城暗牌」不是同一件事（目前只有明牌那一张开着）。
    if getattr(board, "reveal_on_death", False):
        out += "　出局亮身份"
    if unknown:
        out += f"  ⚠️ 有没定义的角色：{'、'.join(unknown)}"
    if len(roles) < 5:
        out += f"  ⚠️ 座位只有 {len(roles)} 个（至少 5 人）"
    return out
