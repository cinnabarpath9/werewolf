# coding=utf-8
"""座位人设（persona）：一张"角色卡"，只影响**怎么说话**，不影响**怎么判断**。

为什么要有姓名/年龄/职业/经历/关系（2026-10-06 扩容，用户要求）：
只写"说话方式"压不住"同一个模型的几个座位像一个模子" —— 有了具体的人（谁、多大、干什么的、
跟谁熟），口吻才有落点，也能让"场外偏好"（我更信谁、我看谁不顺眼）自然分叉。

三层仍然要分清（这一版把"打法"明确划进合法范围，见 RISKY_WORDS 的注释）：

| 层 | 能不能写 | 例子 |
| --- | --- | --- |
| **表达层** | ✅ 主要写这层 | 口吻、句长、常用词、称呼方式、情绪基调、口头禅、爱不爱用反问 |
| **人物层** | ✅ 可以写（本版新增） | 姓名/年龄/职业/经历/**和其他座位的关系** —— 座位是固定的，身份每局重发 |
| **信息层** | ⛔ 绝对不能写 | 任何关于身份、队友、谁在骗人的内容 —— 那等于给信息隔离开后门 |

两个硬约束（`lint_persona` 会查）：
1. **身份无关**：同一个座位这局可能是狼、下局可能是民，所以人设里不许出现角色名/角色专属知识。
   连"职业"都不许写成角色名（职业写"猎人"就和身份撞车了）。
2. **关系只能当偏好、不能当证据**：关系是"场外"信息，卡片里会明确写清
   "只能在场上判断五五开时用来打破平局，不能推翻场上证据"。

文件格式（`personas/<座位号>.md`）：开头一段 YAML front-matter + 下面自由写表达层正文。
**不带 front-matter 的老文件照样能用**（整篇当正文）。
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

try:                                    # 浏览器内（Pyodide）默认不装 PyYAML：
    import yaml                         # 没有它只影响"人设卡"，规则/对局完全正常
except ImportError:
    yaml = None                         # type: ignore[assignment]

#: 出现这些词就基本可以断定"人设越界了"。
#:
#: ⚠️ 2026-10 调整过一次口径：**打法现在是合法的人设内容**。
#: 原因：只写"说话方式"压不住"同一个模型的几个座位像一个模子"（语气不同、判断还是同一个），
#: 而"打法倾向"（激进/保守/多疑/盯票型）才能真正让他们的**选择**分叉。
#: 所以「投票 / 站边 / 带节奏 / 打法」这类**策略词**从名单里去掉了，
#: 只保留**会泄露身份或角色专属知识**的词（角色名、金水查杀、队友同伙…）——
#: 那些人设写进去就等于把身份提前告诉模型，破坏信息隔离。
RISKY_WORDS = (
    # 角色名 / 角色专属知识：写了就等于泄露身份
    "狼人", "预言家", "女巫", "猎人", "守卫", "愚者", "白痴", "村民",
    "摄梦人", "守墓人", "养鸦人", "幸运儿", "狼王", "神职", "阵营",
    "队友", "同伙", "共边", "查验", "毒药", "解药", "刀人", "凶手",
    "好人", "坏人", "金水", "查杀", "验人", "警长", "自爆",
    "wolf", "seer", "witch", "hunter", "guard", "villager", "idiot",
    "dreamer", "gravekeeper", "crow", "lucky", "wolf_king",
    # 2026-10-06 补：审查里实测能绕过第一版的写法（同义/省略/黑话）
    "神牌", "民牌", "悍跳", "自刀", "夜里动刀", "狼同伴", "狼队友",
    "验了", "去验", "摸牌", "发牌", "身份牌", "我是狼", "我是预言家",
)

#: 角色名（用来查"职业/姓名/经历里是不是混进了身份词"）
#: ⚠️ 从 `ROLES` 派生：以后再加角色/改名，这里自动跟上（以前手写一份，改名就漏）。
from wolf.roles import ROLES as _ROLES_FOR_WORDS          # noqa: E402

ROLE_WORDS = tuple(_ROLES_FOR_WORDS[k].name for k in _ROLES_FOR_WORDS) + ("白痴",)


def _norm_for_match(text: str) -> str:
    """把文本归一成"能挡住绕过写法"的形式再比对。

    第一版是朴素子串匹配，实测这些都能绕过去：
      `Seer` / `Wolf`（大小写）、`ＷＯＬＦ`（全角）、`预\\u200b言家`（零宽字符）、
      `像 狼 人 一样`（中间插空格）。
    归一化做四件事：NFKC（全角→半角）→ 去零宽/方向控制/BOM → 去所有空白 → casefold。
    词表和正文**都**过这一遍，所以"拆字"也躲不掉。
    """
    out = unicodedata.normalize("NFKC", text or "")
    out = re.sub(r"[\u200b-\u200f\u2028-\u202f\ufeff]", "", out)
    out = re.sub(r"\s+", "", out)
    return out.casefold()

#: 卡片里给模型的"场外纪律"（用户定的口径：只能平票时当偏置，不能当证据）
META_RULE = ("关系与好恶属于**场外偏好**：只能在场上判断五五开时用来打破平局，"
             "不能推翻场上证据，也不许把'我跟他熟'当成他不是狼的理由。")

CARD_TITLE = "【你的角色卡】"


@dataclass
class Persona:
    """一张角色卡。除 `style` 外都是可选字段（老文件只有 style）。"""
    seat: int = 0
    name: str = ""
    age: str = ""
    job: str = ""
    traits: str = ""            # 性格（也可以写在正文里）
    background: str = ""        # 经历
    relations: list[str] = field(default_factory=list)   # 和其他座位的关系（自由文本）
    style: str = ""             # 正文：说话方式 / 打法倾向
    source: str = ""
    front_matter_broken: bool = False   # 卡头写了但 YAML 解析失败（要提醒用户，别静默丢字段）

    def is_empty(self) -> bool:
        return not any((self.name, self.age, self.job, self.traits,
                        self.background, self.relations, self.style))


def parse(seat: int, text: str, source: str = "") -> Persona:
    """把一段文本解析成角色卡。带 `---` front-matter 就按字段读，否则整篇当正文。

    ⚠️ 卡头写坏（YAML 语法错）时**正文必须照样能用**：正文取第 3 段，
    不能把坏卡头留在正文里当风格指令（第一版就是这样：try 里才切正文，
    解析失败时整段原样留下，模型会看到 `name: [没闭合` 这种东西）。

    ⚠️ 2026-10-06 再补两种"静默丢字段"的写法（审查实测）：
      · 卡头**只有开头 `---`、没有收尾** → 以前 `split("---", 2)` 只有两段 → 整篇当正文，
        YAML 原文（`---\nname: 沈倩宁\nage: 24`）被当成"说话风格"喂给模型；
      · 文件带 **UTF-8 BOM** → `startswith("---")` 为假 → 同样整篇当正文。
    现在这两种都标 `front_matter_broken=True`、丢掉卡头、并给出明确告警。
    """
    body = text.lstrip("\ufeff").strip()
    meta: dict = {}
    broken = False
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()                  # 先切正文，再管卡头能不能解析
            try:
                loaded = yaml.safe_load(parts[1]) or {}
                if isinstance(loaded, dict):
                    meta = loaded
                else:
                    broken = True
            except yaml.YAMLError:
                broken = True
        else:
            # 只有开头那个 ---：把疑似卡头的部分丢掉，别让它当正文进包
            lines = body.splitlines()
            head_end = 1
            while head_end < len(lines) and re.match(r"^\s*[A-Za-z_\u4e00-\u9fff]+\s*[:：]", lines[head_end]):
                head_end += 1
            body = "\n".join(lines[head_end:]).strip()
            broken = True
    rel = meta.get("relations") or meta.get("关系") or []
    if isinstance(rel, str):
        rel = [x.strip() for x in re.split(r"[;；\n]", rel) if x.strip()]
    return Persona(
        seat=seat,
        name=str(meta.get("name") or meta.get("姓名") or "").strip(),
        age=str(meta.get("age") or meta.get("年龄") or "").strip(),
        job=str(meta.get("job") or meta.get("职业") or "").strip(),
        traits=str(meta.get("traits") or meta.get("性格") or "").strip(),
        background=str(meta.get("background") or meta.get("经历") or "").strip(),
        relations=[str(x).strip() for x in rel if str(x).strip()],
        style=body,
        source=source,
        front_matter_broken=broken,
    )


def _clean(text: str) -> str:
    """去掉正文里的 markdown 噪音（## 标题、** 粗体）—— 模型读纯文本更省心。"""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            out.append("" if not title else f"· {title}：")
            continue
        out.append(line.replace("**", ""))
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def render(persona: Persona) -> str:
    """渲染成进 packet 的那段文本（人类看的面板也是它）。"""
    if persona.is_empty():
        return ""
    head: list[str] = []
    if persona.name:
        who = persona.name
        if persona.age:
            who += f"，{persona.age}"
        if persona.job:
            who += f"，{persona.job}"
        head.append(who)
    elif persona.age or persona.job:
        head.append("，".join(x for x in (persona.age, persona.job) if x))
    lines = [CARD_TITLE + "（这是「谁在说话」，只影响你怎么说、怎么待人，"
                          "**不影响你怎么判断局势，也不给你任何额外信息**）"]
    if head:
        lines.append("你是：" + head[0])
    if persona.traits:
        lines.append("性格：" + persona.traits)
    if persona.background:
        lines.append("经历：" + persona.background)
    if persona.relations:
        lines.append("你和别人的关系：")
        lines.extend("· " + r for r in persona.relations)
        lines.append(META_RULE)
    if persona.style:
        lines.append("")
        lines.append(_clean(persona.style))
    return "\n".join(lines)


def lint_persona(persona: Persona, names: dict[int, str] | None = None) -> list[str]:
    """查越界与自相矛盾，返回人话警告（不拦截，最终由你判断）。"""
    warnings: list[str] = []
    seat = persona.seat
    fields = {"姓名": persona.name, "年龄": persona.age, "职业": persona.job,
              "性格": persona.traits, "经历": persona.background,
              "关系": "；".join(persona.relations), "正文": persona.style}
    if persona.front_matter_broken:
        warnings.append(f"{seat}号 的角色卡开头那段 `---` 里的 YAML 没解析成功"
                        f"（多半是括号/引号/缩进写坏了，或者卡头只有开头没收尾）—— "
                        f"卡头字段被忽略了，只有正文生效。检查一下 name/age/job/relations 那几行。")
    joined = "；".join(f"{k}：{v}" for k, v in fields.items() if v)
    # 词表和正文**都**过 _norm_for_match：这样大小写/全角/零宽/插空格的写法也拦得住
    hay = _norm_for_match(joined)
    hits = sorted({w for w in RISKY_WORDS if _norm_for_match(w) in hay})
    if hits:
        where = [k for k, v in fields.items() if v and any(
            _norm_for_match(w) in _norm_for_match(v) for w in hits)]
        warnings.append(
            f"{seat}号 的角色卡里出现了 {('、'.join(hits))}（在 {('、'.join(where))} 里）—— "
            f"这些词意味着越界：人设不许带身份/队友/角色专属知识。"
        )
    # 最后再拿**真正会进包的文本**查一遍（render 出来的成品）：
    # lint 的字段集合和 render 的字段集合将来可能漂移，这一步是兜底
    rendered = _norm_for_match(render(persona))
    late = sorted({w for w in RISKY_WORDS if _norm_for_match(w) in rendered})
    if late and not hits:
        warnings.append(f"{seat}号 渲染出来的成品文本里出现了 {('、'.join(late))} —— "
                        f"虽然各字段单看没问题，但拼起来越界了，检查一下。")
    if persona.job and any(_norm_for_match(w) in _norm_for_match(persona.job)
                           for w in ROLE_WORDS):
        warnings.append(f"{seat}号 的「职业」写成了角色名（{persona.job}）—— 会和本局身份撞车，"
                        f"换个真实职业（教师/会计/司机…）")
    if persona.name and any(_norm_for_match(w) in _norm_for_match(persona.name)
                            for w in ROLE_WORDS):
        warnings.append(f"{seat}号 的姓名里带了角色名（{persona.name}），换一个")
    for r in persona.relations:
        m = re.findall(r"(\d+)\s*号", r)
        for other in m:
            if int(other) == seat:
                warnings.append(f"{seat}号 的关系里提到了自己（{r}）—— 改成别人的座位号")
        # ⚠️ 2026-10-30：这里原来有个 `if names and any(...): pass` 的空分支 ——
        #    注释说"名字对不上在下面统一查一次"，但函数里并没有那一段。已删。
        #    名字与本座位对不上由 `cross_check()` 统一报（见文件末尾）。
    total = len(joined)
    # ⚠️ 2026-10-11：口径对齐 `tests/check_d_personas.py` 的硬闸门（**渲染后 ≤700 字**）。
    #    以前这里只判 1200 字，告警文案却写"建议压在 400 字以内"—— 实测十张卡渲染后
    #    652~723 字，既超了 400 又触发不了任何告警，等于这句建议没人执行。
    if total > 700:
        warnings.append(f"{seat}号 的角色卡 {total} 字（按渲染前统计），偏长了："
                        f"人设是表达层，越短越不会被模型当成策略指令。"
                        f"`tests/check_d_personas.py` 的硬线是**渲染后 ≤700 字**。")
    return warnings


def cross_check(personas: dict[int, Persona]) -> list[str]:
    """跨座位一致性：A 的关系里提到的名字，要和那个座位自己声明的名字对得上。"""
    warnings: list[str] = []
    names = {sid: p.name for sid, p in personas.items() if p.name}
    for sid, p in personas.items():
        if not p.name:
            warnings.append(f"{sid}号 有角色卡但没写姓名 —— 别人提到他时只能靠座位号，"
                            f"建议补一个")
        for rel in p.relations:
            for other in re.findall(r"(\d+)\s*号", rel):
                oid = int(other)
                if oid == sid:
                    continue
                if oid not in names:
                    warnings.append(f"{sid}号 的关系里提到 {oid}号，但那个座位没有角色卡/姓名")
                elif names[oid] not in rel:
                    warnings.append(f"{sid}号 的关系里称 {oid}号为「{rel[:24]}…」，"
                                    f"而 {oid}号 自己的卡上叫「{names[oid]}」—— 统一一下名字")
    return warnings


@dataclass
class PersonaSet:
    """一次加载的结果：谁用了什么卡、来自哪个文件、有没有越界警告。"""
    texts: dict[int, str] = field(default_factory=dict)        # 渲染好的、直接进 packet 的文本
    personas: dict[int, Persona] = field(default_factory=dict)  # 结构化的卡（面板/校验用）
    sources: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.texts)

    def __contains__(self, seat_id: object) -> bool:
        return seat_id in self.texts

    def get(self, seat_id: int, default: str = "") -> str:
        return self.texts.get(seat_id, default)

    def items(self):
        return self.texts.items()

    def names(self) -> dict[int, str]:
        return {sid: p.name for sid, p in self.personas.items() if p.name}


def lint(seat_id: int, text: str) -> list[str]:
    """兼容老接口：直接对一段文本做越界粗查。"""
    return lint_persona(parse(seat_id, text))


def _register(result: PersonaSet, seat_id: int, text: str, source: str) -> None:
    persona = parse(seat_id, text, source)
    result.personas[seat_id] = persona
    result.texts[seat_id] = render(persona)
    result.sources[seat_id] = source


def load(cfg: dict, project_root: str) -> PersonaSet:
    """读座位角色卡。返回 PersonaSet（渲染文本 + 结构化卡 + 来源 + 越界警告）。

    支持三种写法（可以混用，config.yaml 里的写法优先于 personas_dir）：
      game.personas_dir: "personas"        # 目录里的 1.md / 2.md / ... 按座位号自动加载
      game.personas: {1: "personas/1.md"}  # 指向文件
      game.personas: {1: "你说话很简短。"}  # 直接内联（没有 front-matter 就当纯正文）
    """
    if yaml is None:
        return {}

    game_cfg = (cfg or {}).get("game") or {}
    result = PersonaSet()

    personas_dir = game_cfg.get("personas_dir")
    if personas_dir:
        folder = (str(personas_dir) if os.path.isabs(str(personas_dir))
                  else os.path.join(project_root, str(personas_dir)))
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                stem, ext = os.path.splitext(name)
                if ext.lower() not in (".md", ".txt") or not stem.isdigit():
                    continue
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        text = fh.read().strip()
                except UnicodeDecodeError:
                    # ⚠️ 2026-10-30：混进一个 GBK/ANSI 存的人设卡时，以前会让整局起不来。
                    #    按"这张卡读不到"跳过，并在 warnings 里点名。
                    result.warnings.append(f"人设卡不是 UTF-8，已跳过：{name}")
                    continue
                if text:
                    _register(result, int(stem), text,
                              f"{os.path.basename(str(personas_dir).rstrip('/\\'))}/{name}")
        else:
            result.warnings.append(f"personas_dir 指向的目录不存在：{folder}")

    mapping = game_cfg.get("personas") or {}
    for raw_key, value in mapping.items():
        try:
            seat_id = int(raw_key)
        except (TypeError, ValueError):
            result.warnings.append(f"personas 里的键 {raw_key!r} 不是座位号，已跳过")
            continue
        text = str(value or "").strip()
        if not text:
            result.texts.pop(seat_id, None)
            result.personas.pop(seat_id, None)
            result.sources.pop(seat_id, None)
            continue
        candidate = text if os.path.isabs(text) else os.path.join(project_root, text)
        # 只在"它真的长得像个文件路径"时才当成路径：光看有没有斜杠会把
        # "他/她说话很冲"这种正文误报成"找不到文件"
        looks_like_path = bool(re.fullmatch(r"[A-Za-z0-9_./\\\-]+\.(?:md|txt|ya?ml)", text.strip()))
        if len(text) < 260 and os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as fh:
                    text = fh.read().strip()
            except UnicodeDecodeError:
                result.warnings.append(f"人设文件不是 UTF-8，已跳过：{value}")
                continue
            source = str(value)
        else:
            if looks_like_path:
                # 路径写错时原来会静默把"personas/03.md"当成人设正文，还盖掉目录里能用的人设
                result.warnings.append(
                    f"{seat_id}号 的人设写的是 {text!r}，但找不到这个文件；"
                    f"已把它当成正文了（检查一下路径有没有拼错）")
            source = "config.yaml 里直接写的"
        _register(result, seat_id, text, source)

    names = result.names()
    for seat_id, persona in sorted(result.personas.items()):
        result.warnings.extend(lint_persona(persona, names))
    result.warnings.extend(cross_check(result.personas))
    return result
