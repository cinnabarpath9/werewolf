# coding=utf-8
"""技能库（Phase 0 元数据层）。

这一层先只回答“**本局有哪些技能、这些技能要接哪些线**”，不改现有引擎结算。
真正的 handler 仍暂时在 `wolf/engine.py`，等行为对照 tests 齐了再逐个迁。

设计口径（见 `werewolf-dev/docs/36-技能库Phase0.md`）：
  · RoleDef.skills 是技能的唯一归属；技能不在角色表里就不会被编译；
  · BoardDef 只引用角色；active_skills(board) 由角色集合编译，技能不会跨板泄漏；
  · SkillDef 统一登记 AI task、真人面板、规则/打法片段、handler、是否可被封；
  · `skill_order` 是**板子自己的**顺序；没有声明的板子按 SkillDef.order 排序。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SkillDef:
    key: str
    name: str
    owner_roles: tuple[str, ...]
    phase: str                       # night / day / death / passive
    task_kind: str | None = None     # engine task.kind；纯被动可为 None
    handler: str = ""                # 现有结算入口（Phase 0 只登记，不调度）
    blockable: bool = True           # 是否受狼巫诅咒/未来沉默影响
    rules_frag: str = ""             # 规则书片段（当前指向 role:xxx）
    playbook_frag: str = ""          # 打法书片段（当前指向 role:xxx）
    human_panel: str | None = None   # web/index.html 里的面板标记
    ai_schema: str | None = None     # engine 里的 schema 标记
    order: int = 500                 # 未声明板子顺序时的缺省排序
    #: 显式事件槽：night / night_wolf / day_start / day_before_vote / day_vote /
    #: on_death。Phase 1e 起，白天/死亡入口按这个槽位找技能。
    event: str = ""
    note: str = ""


SKILLS: dict[str, SkillDef] = {
    # ── 狼队共用 ──────────────────────────────────────────────
    "wolf_kill": SkillDef(
        "wolf_kill", "狼队出刀", ("wolf", "wolf_king", "wolf_witch"), "night",
        task_kind="wolf_intent", handler="Game.night.wolf_kill",
        blockable=False, rules_frag="role:wolf", playbook_frag="role:wolf",
        human_panel="wolf_kill", ai_schema='"target": <你要刀的玩家编号>', order=10,
        note="狼队共同出刀；不是个人技能，不能被狼巫诅咒封。",
    ),
    # ── 狼王 ──────────────────────────────────────────────────
    "wolf_king_take": SkillDef(
        "wolf_king_take", "狼王带人", ("wolf_king",), "death",
        task_kind="wolf_king_take", handler="Game.wolf_king_take",
        blockable=True, rules_frag="role:wolf_king", playbook_frag="role:wolf_king",
        human_panel="wolf_king_take", ai_schema="wolf_king_take", order=910,
        note="出局时带走一名好人；被诅咒时失效。",
    ),
    # ── 狼巫 ──────────────────────────────────────────────────
    "wolf_curse": SkillDef(
        "wolf_curse", "狼巫诅咒", ("wolf_witch",), "night",
        task_kind="wolf_intent", handler="Game._apply_curse_target",
        blockable=True, rules_frag="skill:wolf_curse", playbook_frag="skill:wolf_curse",
        human_panel="curse_legal", ai_schema='"curse": <你要诅咒的玩家编号或 null>',
        order=11, note="和狼刀合并在同一次 wolf_intent 里；先 curse 后 target。",
    ),
    # ── 好人夜间主动技能 ─────────────────────────────────────
    "seer_check": SkillDef(
        "seer_check", "预言家查验", ("seer",), "night",
        task_kind="seer_check", handler="Game._seer_check",
        blockable=True, rules_frag="role:seer", playbook_frag="role:seer",
        human_panel="seer_check", ai_schema='"target": <玩家编号>', order=20,
    ),
    "witch_potions": SkillDef(
        "witch_potions", "女巫用药", ("witch",), "night",
        task_kind="witch", handler="Game.night.witch",
        blockable=True, rules_frag="role:witch", playbook_frag="role:witch",
        human_panel="ask.kind === 'witch'", ai_schema='"save": true/false', order=50,
        note="解药/毒药共用一次决策；技能库先按一个技能登记，资源在 Seat/Game。",
    ),
    "dream": SkillDef(
        "dream", "摄梦人摄梦", ("dreamer",), "night",
        task_kind="dream", handler="Game.night.dreamer",
        blockable=True, rules_frag="role:dreamer", playbook_frag="role:dreamer",
        human_panel="'dream'", ai_schema='"abstain": true/false', order=30,
    ),
    "guard": SkillDef(
        "guard", "守卫守护", ("guard",), "night",
        task_kind="guard", handler="Game.night.guard",
        blockable=True, rules_frag="role:guard", playbook_frag="role:guard",
        human_panel="guard", ai_schema='"abstain": true/false', order=25,
    ),
    "lucky_skill": SkillDef(
        "lucky_skill", "幸运儿三选一", ("lucky",), "night",
        task_kind="lucky", handler="Game.night.lucky",
        blockable=True, rules_frag="skill:lucky_skill", playbook_frag="skill:lucky_skill",
        human_panel="skillDefs", ai_schema='"skill": "check|guard|bullet"', order=40,
        note="一次性技能；子弹是另一个 death 阶段技能。",
    ),
    "lucky_bullet": SkillDef(
        "lucky_bullet", "幸运儿子弹", ("lucky",), "death",
        task_kind="lucky_shoot", handler="Game.lucky_shoot",
        blockable=True, rules_frag="role:lucky", playbook_frag="role:lucky",
        human_panel="lucky_shoot", ai_schema="lucky_shoot", order=930,
    ),
    # ── 好人白天主动技能 ─────────────────────────────────────
    "knight_judge": SkillDef(
        "knight_judge", "骑士审判", ("knight",), "day",
        task_kind="knight_judge", handler="Game.knight_judge",
        blockable=True, rules_frag="skill:knight_judge", playbook_frag="skill:knight_judge",
        human_panel="knight_judge", ai_schema='"use": true/false', order=120,
    ),
    "crow_curse": SkillDef(
        "crow_curse", "养鸦人诅咒", ("crow",), "day",
        task_kind="vote", handler="Game._apply_crow_curse",
        blockable=True, rules_frag="skill:crow_curse", playbook_frag="skill:crow_curse",
        human_panel="curse_target", ai_schema="curse_target", order=110,
        note="和放逐投票同一次决策；不是独立 ask。",
    ),
    # ── 被动/信息技能 ────────────────────────────────────────
    "gravekeeper_result": SkillDef(
        "gravekeeper_result", "守墓人验尸", ("gravekeeper",), "day",
        task_kind=None, handler="Game._tell_gravekeeper",
        blockable=True, rules_frag="skill:gravekeeper_result", playbook_frag="skill:gravekeeper_result",
        order=100, note="白天发言轮开始前自动发；被诅咒时当日不补。",
    ),
    "idiot_immune": SkillDef(
        "idiot_immune", "愚者免死", ("idiot",), "passive",
        task_kind=None, handler="Game.kill.idiot_immune",
        blockable=True, rules_frag="role:idiot", playbook_frag="role:idiot",
        order=900, note="第一次致命结算免疫；被诅咒时直接出局且不翻牌。",
    ),
    "hunter_shoot": SkillDef(
        "hunter_shoot", "猎人开枪", ("hunter",), "death",
        task_kind="hunter_shoot", handler="Game.hunter_shoot",
        blockable=True, rules_frag="role:hunter", playbook_frag="role:hunter",
        human_panel="hunter_shoot", ai_schema="hunter_shoot", order=920,
    ),
}


#: Phase 1b 试点：技能 key → Game 上的真实方法名。
#: 只登记已经迁移到 run_skill_async 的技能；其余仍走旧硬编码分支。
SKILL_METHODS: dict[str, str] = {
    "wolf_kill": "_night_wolf_kill",
    "wolf_curse": "_night_wolf_curse",
    "seer_check": "_seer_check",
    "guard": "_night_guard",
    "dream": "_night_dream",
    "lucky_skill": "_night_lucky",
    "witch_potions": "_night_witch",
    "knight_judge": "knight_judge",
    "gravekeeper_result": "_tell_gravekeeper",
    "crow_curse": "_apply_crow_curse",
    "hunter_shoot": "hunter_shoot",
    "lucky_bullet": "lucky_shoot",
    "wolf_king_take": "wolf_king_take",
}

#: Phase 1e：事件槽。day_start / day_before_vote 已经由 Game 消费。
_EVENTS: dict[str, str] = {
    "wolf_kill": "night_wolf",
    "wolf_curse": "night_wolf",
    "seer_check": "night",
    "witch_potions": "night",
    "dream": "night",
    "guard": "night",
    "lucky_skill": "night",
    "lucky_bullet": "on_death",
    "knight_judge": "day_before_vote",
    "crow_curse": "day_vote",
    "gravekeeper_result": "day_start",
    "idiot_immune": "on_death",
    "hunter_shoot": "on_death",
    "wolf_king_take": "on_death",
}
for _key, _event in _EVENTS.items():
    SKILLS[_key] = replace(SKILLS[_key], event=_event)


#: Phase 1f：死亡事件里，角色 → 该触发的技能（只登记死亡时真的会问的）。
ROLE_DEATH_SKILL: dict[str, str] = {
    "hunter": "hunter_shoot",
    "lucky": "lucky_bullet",
    "wolf_king": "wolf_king_take",
}

#: Phase 1g：显式事件顺序表。event 槽内按这里的先后执行；未列出的技能按
#: SkillDef.order 追加到末尾。夜间拆成 night_wolf（狼队）和 night（好人）。
EVENT_ORDER: dict[str, tuple[str, ...]] = {
    "night_wolf": ("wolf_kill", "wolf_curse"),
    "night": ("seer_check", "guard", "dream", "lucky_skill", "witch_potions"),
    "day_start": ("gravekeeper_result",),
    "day_before_vote": ("knight_judge",),
    "day_vote": ("crow_curse",),
    "on_death": ("hunter_shoot", "lucky_bullet", "wolf_king_take", "idiot_immune"),
}

class SkillConfigError(ValueError):
    """板子/角色引用了技能库里不存在的技能。"""


def skills_for_roles(roles: list[str]) -> set[str]:
    """按角色表编译本局技能集合；不认识的技能直接报错，不静默跳过。"""
    from .roles import ROLES

    out: set[str] = set()
    for role in roles:
        rd = ROLES.get(role)
        if rd is None:
            raise SkillConfigError(f"板子里有未定义角色：{role}")
        for key in tuple(getattr(rd, "skills", ()) or ()):
            sd = SKILLS.get(key)
            if sd is None:
                raise SkillConfigError(f"角色 {role} 引用了未登记技能：{key}")
            if role not in sd.owner_roles:
                raise SkillConfigError(
                    f"技能 {key} 登记在 {sd.owner_roles}，但被角色 {role} 引用")
            out.add(key)
    return out


def active_skills(board) -> tuple[str, ...]:
    """本局真正激活的技能（按 SkillDef.order 排；板子可覆盖顺序）。"""
    keys = skills_for_roles(list(board.roles))
    declared = [str(k) for k in (getattr(board, "skill_order", ()) or ())]
    if declared:
        seen: set[str] = set()
        ordered: list[str] = []
        for key in declared:
            if key in seen:
                continue
            seen.add(key)
            if key in keys:
                ordered.append(key)
        rest = sorted(keys - set(ordered), key=lambda k: (SKILLS[k].order, k))
        return tuple(ordered + rest)
    return tuple(sorted(keys, key=lambda k: (SKILLS[k].order, k)))


def validate_board_skills(board) -> list[str]:
    """板子级技能校验：缺技能、重复顺序、顺序里混入未激活技能。"""
    issues: list[str] = []
    try:
        keys = skills_for_roles(list(board.roles))
    except SkillConfigError as exc:
        return [str(exc)]
    declared = [str(k) for k in (getattr(board, "skill_order", ()) or ())]
    if len(declared) != len(set(declared)):
        issues.append(f"{board.name}：skill_order 有重复项")
    inactive = [k for k in declared if k not in keys]
    if inactive:
        issues.append(f"{board.name}：skill_order 含本板未激活技能 {inactive}")
    # 每个激活技能至少要有规则/打法/handler 元数据，防止新增技能只写一半。
    for key in sorted(keys):
        sd = SKILLS[key]
        if not sd.rules_frag:
            issues.append(f"{board.name}：技能 {key} 缺 rules_frag")
        if not sd.playbook_frag:
            issues.append(f"{board.name}：技能 {key} 缺 playbook_frag")
        if not sd.handler:
            issues.append(f"{board.name}：技能 {key} 缺 handler")
    return issues


__all__ = ["SkillDef", "SKILLS", "SKILL_METHODS", "ROLE_DEATH_SKILL", "EVENT_ORDER", "SkillConfigError",
           "skills_for_roles", "active_skills", "validate_board_skills"]
