# coding=utf-8
"""规则书：**按板子组装**规则片段，只发本局用得到的。

为什么要按板子组装（用户提的）：11 人预女猎白场没有守卫、没有白狼王、没有狼王 ——
把这些角色的能力全摞成一本"总规则"发给模型，既费 token（一局几百次调用），
又把注意力摊薄在"这局根本不存在的角色"上。
所以：`content/01_规则书.md` 里按片段写（通用 / 每个角色一段 / 警长一段 / 白痴双算一段），
开局按 `BoardDef.roles` + 是否开警长**只挑需要的**拼出来。

三种放置方式（都实现了，实测对比见 `tools/rules_ab.py`）：
  · **room**（默认）：开局当**公开公告**进台账 —— 和公开记录一起渲染进每个 packet
  · **packet**：塞进每个座位的提示词顶部（位置最显眼、明确是"给你的规则"）
  · **off**：都不放（手机上还能看全文）

⚠️ 片段与"给人看的完整说明"在同一个文件里：模型看到的是片段拼出来的，
人看到的是文件下半部分。加新角色时**必须**补一段 `frag:role:<key>`，
否则 `compose()` 会直接报错（宁可开局失败，也不要悄悄少发一条角色规则）。
"""

from __future__ import annotations

import io
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(HERE, "content/01_规则书.md")

#: 通用片段（每个板子都要）
CORE = "core"
#: **胜负口径**（2026-10-30 从 core 拆出来）：屠边 / 屠城两张板子判法不同，
#: 写在 core 里会让 9 / 6 人板读到「屠边」这条错的胜负条件。`compose(win_rule=...)` 选一段发。
WIN_SIDES = "win_sides"
WIN_ALL = "win_all"
#: 条件片段
SHERIFF = "sheriff"
DUALKIND = "dualkind"
#: **首夜无刀**（板子级规则，作者 2026-10-10 定；只有 `BoardDef.first_night_quiet` 的板子发）。
#: 为什么不写进 `core`：core 是**所有板子共用**的 —— 把"本板子第一夜没有狼刀"写进 core，
#: 玩 `eleven_dream` 的模型会以为第一夜没人被刀，于是第一天的推理全歪（这条正是新板子的
#: 核心差异，绝不能发错对象）。
FIRSTNIGHT = "firstnight"
#: **出局亮身份**（板子级规则，作者 2026-10-30 定；只有 `BoardDef.reveal_on_death` 的板子发）。
#: 跟 FIRSTNIGHT 一样是**条件片段**：暗牌板子发这段，模型会以为"死了就亮身份"，
#: 于是敢放心去验一个死人 —— 判错就是直接输。
REVEAL = "reveal"
#: **预言家锁定上警**（开局选项 `game.seer_auto_run`，默认开；只有开着才发）。
#: 跟 FIRSTNIGHT 一样是**条件片段**：它不是板子属性 —— 关掉开关后引擎会照常问预言家
#: 「要不要上警」，那时再发"本局不问你"就是错的（2026-10-11 前它写在 core 里，人人都有）。
SEERLOCK = "seerlock"

_cache: dict[str, tuple[float, dict, str]] = {}


def _parse(text: str) -> dict:
    """把 `<!-- frag:名字:begin --> … <!-- frag:名字:end -->` 抽成字典。

    ⚠️ 名字里带冒号（`role:wolf`），所以字符集必须包含 `:` ——
    第一版写成 `[a-z_]+`，结果只认出 core/sheriff/dualkind，**七个角色片段一个都没认出来**
    （compose 立刻报"规则书里没有角色 [wolf] 的片段"，算是被这条硬检查救了）。
    """
    out: dict[str, str] = {}
    for name, body in re.findall(r"<!--\s*frag:([a-z_:]+):begin\s*-->(.*?)"
                                 r"<!--\s*frag:\1:end\s*-->", text, flags=re.S):
        out[name] = body.strip()
    return out


def _human_part(text: str) -> str:
    """给人看的那半部分（片段区之后的内容）。"""
    marker = "以下是给人看的完整说明"
    i = text.find(marker)
    if i < 0:
        return ""
    tail = text[i:]
    tail = tail.split("-->", 1)[-1] if "-->" in tail else tail
    return _strip_markdown(tail)


def load(path: str = DEFAULT_PATH) -> tuple[dict, str]:
    """返回（片段字典, 给人看的全文）。文件没改就走缓存。"""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}, ""
    hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError:
        return {}, ""
    except UnicodeDecodeError as exc:
        # ⚠️ 2026-10-30：规则书被另存成 GBK/ANSI 时，UnicodeDecodeError 不是 OSError，
        #    以前会直接崩掉整局。这里按"读不到"处理（返回空片段），并打一行能查的日志。
        print(f"[rules] {path} 不是 UTF-8，忽略：{exc}")
        return {}, ""
    frags = _parse(text)
    human = _human_part(text)
    _cache[path] = (stamp, frags, human)
    return frags, human


def _strip_markdown(text: str) -> str:
    """去掉 markdown 噪音（# 标题、表格线、** 粗体），留成好读的纯文本。"""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith(("<!--", ">")):
            continue
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            out.append("" if not title else f"—— {title} ——")
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            out.append("　".join(c for c in cells if c))
            continue
        if line.strip().startswith("- "):
            out.append("· " + line.strip()[2:])
            continue
        out.append(line.replace("**", ""))
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def compose(board_roles, sheriff: bool = False, detail: str = "brief",
            path: str = DEFAULT_PATH, first_night_quiet: bool = False,
            seer_auto_run: bool = False, reveal_on_death: bool = False,
            win_rule: str = "sides", skills=None) -> str:
    """按本局板子拼出给模型的规则文本。

    `board_roles` = `BoardDef.roles`（角色 key 列表，可重复）。重复的角色只发一次。
    `first_night_quiet` = 本板子是不是"首夜无刀"（见 `roles.BoardDef.first_night_quiet`）：
    是就**额外**发一段 [firstnight]（女巫/摄梦人第 2 夜才动、第一夜没有刀口）。
    `seer_auto_run` = 本局是不是"预言家锁定上警"（见 `engine.Game.seer_auto_run` /
    `config.yaml` 的 `game.seer_auto_run`，默认开）：是就**额外**发一段 [seerlock]。
    `reveal_on_death` = 本板子是不是"出局亮身份"（见 `roles.BoardDef.reveal_on_death`）：
    是就**额外**发一段 [reveal]（暗牌板子一个字都不发）。
    ⚠️ 这是一个**开局选项**、不是板子属性：关掉之后引擎会照常问预言家「要不要上警」，
    这时再发那段"不问你"就是错的 —— 所以它跟 firstnight 一样走参数，不写死在 core 里
    （2026-10-11：原来那段写在 core 里，于是**每一张板子、每一个开关状态**都会发）。
    `detail="full"` 时在片段后面再附上规则书下半部分那份**完整说明**
    （给人看的那份也能给模型看，只是更长 ≈+1400 字）。
    """
    frags, human = load(path)
    keys: list[str] = []
    # ⚠️ 通用片段（胜负/流程/投票与 PK/发言顺序/遗言/死因）缺失必须**硬报错**：
    #    以前是 `if CORE in frags` 静默跳过，规则书被编辑坏掉时组装结果只剩角色条目 ——
    #    模型在不知道胜负条件、不知道遗言怎么给的情况下打完整局，而且没有任何告警。
    if CORE not in frags:
        raise KeyError(f"规则书里没有通用片段 [{CORE}]（{path}）："
                       f"胜负/流程/投票/遗言/死因都在这一段里，缺了它不能开局")
    keys.append(CORE)
    # 胜负口径：屠边 / 屠城必须发**对应的那一段**（发错等于把胜负条件写反）。
    _win_key = WIN_ALL if str(win_rule).strip().lower() == "all" else WIN_SIDES
    if _win_key not in frags:
        raise KeyError(f"规则书里没有胜负片段 [{_win_key}]（{path}）："
                       f"没有它模型不知道这局怎么算赢")
    keys.append(_win_key)
    seen: set[str] = set()
    for role_key in board_roles:
        key = f"role:{role_key}"
        if role_key in seen:
            continue
        seen.add(role_key)
        if key not in frags:
            # 宁可开局就报错，也不要悄悄少发一条角色规则（模型会按错的规则打）
            raise KeyError(f"规则书里没有角色 [{role_key}] 的片段（{path}）；"
                           f"请补一段 <!-- frag:role:{role_key}:begin --> … end -->")
        keys.append(key)
    # 技能片段（Phase 2）：只发本局 active_skills 里、声明了 skill: 规则的技能。
    # 角色片段只留身份/定位，具体技能结算集中在这里，避免新技能改两处。
    if skills is None:
        from .skills import skills_for_roles
        skills = skills_for_roles(list(board_roles))
    from .skills import SKILLS
    for skill_key in sorted(skills, key=lambda k: (SKILLS[k].order, k) if k in SKILLS else (999, k)):
        sd = SKILLS.get(skill_key)
        if not sd or not sd.rules_frag.startswith("skill:"):
            continue
        frag_key = sd.rules_frag
        if frag_key not in frags:
            raise KeyError(f"规则书里没有技能 [{skill_key}] 的片段（{path}）；"
                           f"请补一段 <!-- frag:{frag_key}:begin --> … end -->")
        keys.append(frag_key)
    if sheriff:
        if SHERIFF not in frags:
            raise KeyError(f"规则书里没有警长片段（{path}）")
        keys.append(SHERIFF)
    # 首夜无刀：只有「本板子第一夜不结算狼刀」的板子才发 ——
    # ⚠️ 发错对象的代价极高（玩普通板子的模型会以为第一夜没人被刀），所以这里也要硬报错。
    if first_night_quiet:
        if FIRSTNIGHT not in frags:
            raise KeyError(f"本板子是「首夜无刀」，但规则书里没有 [{FIRSTNIGHT}] 片段（{path}）")
        keys.append(FIRSTNIGHT)
    # 预言家锁定上警：**开局选项**（不是板子属性）—— 开着才发，关掉时引擎会照常问他上不上警。
    # ⚠️ 还必须**本局真有警长竞选**才发：9 / 6 人板没有警长这一环，发这段等于告诉预言家
    #    「你自动上警了」，而他根本没被问过、也没有竞选 —— 是纯假信息（2026-10-30 修）。
    if seer_auto_run and sheriff:
        if SEERLOCK not in frags:
            raise KeyError(f"本局开了「预言家锁定上警」，但规则书里没有 [{SEERLOCK}] 片段（{path}）")
        keys.append(SEERLOCK)
    # 出局亮身份：只有「本板子出局就公开身份」的板子才发 —— 发错对象同样代价极高
    # （暗牌板子的模型会以为死人身份是公开的，拿"他死了所以他是狼"当事实去推）。
    if reveal_on_death:
        if REVEAL not in frags:
            raise KeyError(f"本板子是「出局亮身份」，但规则书里没有 [{REVEAL}] 片段（{path}）")
        keys.append(REVEAL)
    # 双算规则：本板有"两边都算"的角色（目前是白痴）就必须发 ——
    # 缺了它，模型会在"三民已死、白痴还活着"时误判狼人屠边成功（胜负规则静默消失）。
    from .roles import ROLES
    if any(getattr(ROLES.get(r), "also_kind", None) for r in seen):
        if DUALKIND not in frags:
            raise KeyError(f"本局有「既算神也算民」的角色，但规则书里没有 "
                           f"[{DUALKIND}] 片段（{path}）")
        keys.append(DUALKIND)
    blocks = [frags[k] for k in keys]
    text = "\n".join(blocks).strip()
    if str(detail).lower() == "full" and human:
        # `rules_detail: full` 才附完整说明；默认 brief 只发按板子挑出来的片段
        text = text + "\n\n—— 补充说明 ——\n" + human
    return text.replace("**", "")          # 片段里写了 `**强调**`，发给模型时去掉星号噪音


def for_game(board_def, sheriff: bool = False, path: str = DEFAULT_PATH,
             detail: str = "brief", seer_auto_run: bool = False) -> tuple[str, str]:
    """返回（给模型的规则文本, 给人看的全文）。"""
    _frags, human = load(path)
    return (compose(getattr(board_def, "roles", []), sheriff=sheriff, path=path,
                    detail=detail,
                    first_night_quiet=bool(getattr(board_def, "first_night_quiet", False)),
                    seer_auto_run=bool(seer_auto_run),
                    reveal_on_death=bool(getattr(board_def, "reveal_on_death", False)),
                    win_rule=str(getattr(board_def, "win_rule", "sides") or "sides")), human)


def packet_block(rules_text: str) -> str:
    """放到座位提示词里的规则块。"""
    if not rules_text:
        return ""
    return ("【本局规则（按本局板子发的，判错会直接输）】\n" + rules_text +
            "\n（以上是规则，不是本局发生的事；发言时不要复述规则。）")


def room_notice(rules_text: str) -> str:
    """开局进"房间公示栏"的那条公开公告。"""
    if not rules_text:
        return ""
    return "【本局规则公示】（所有人都看得到，判错会直接输）\n" + rules_text
