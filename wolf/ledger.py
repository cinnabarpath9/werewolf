# coding=utf-8
"""结构化台账：谁说了什么、投了谁、谁声明了什么。

信息控制三原则（借自 NeuroBook 的 subject / actor-facing packet 模型）：

1. 台账是唯一事实来源。模型不负责记忆，每回合按座位过滤后重新注入。
2. 每条记录带 visible_to，只有该看到的人能看到 —— 引用不等于授权。
3. 未知信息直接不出现，不写“你不知道 X”清单（那本身就在泄露信息）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

PUBLIC = "public"   # 全场可见（说出口的话、投票、公告）
WOLF = "wolf"       # 只有狼队可见（夜间讨论）
# "seat:N"          # 只有 N 号可见（私有经历）


@dataclass
class Entry:
    day: int
    phase: str          # night / day
    kind: str           # announce / speech / claim / vote / night / private
    text: str
    seat: int | None = None
    data: dict = field(default_factory=dict)
    visible_to: tuple[str, ...] = (PUBLIC,)
    #: 复盘语义（2026-11-06）：动作级类型 + 一句话摘要。
    #: `kind` 是粗分类；`action` 让"投票/警长票/出局/夜刀"能直接筛，
    #: `summary` 让手机/统计不必从 text 里猜。
    action: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        d = self.data if isinstance(self.data, dict) else {}
        if not self.action:
            if self.kind == "vote":
                self.action = "sheriff_vote" if d.get("sheriff_vote") else "vote"
            else:
                self.action = str(self.kind or "")
        if not self.summary:
            text = str(self.text or "").strip()
            if self.kind == "vote":
                target = d.get("target")
                who = f"{self.seat}号" if self.seat is not None else "某位"
                if d.get("invalid") or target is None:
                    self.summary = f"{who} 弃权/无效票"
                else:
                    self.summary = f"{who} → {target}号"
            elif self.kind == "announce":
                self.summary = text[:80]
            elif self.kind == "claim":
                self.summary = text[:80]
            elif self.kind == "night":
                self.summary = f"夜间：{text[:60]}" if text else "夜间行动"
            elif self.kind == "speech":
                self.summary = text[:60]
            else:
                self.summary = text[:60]

    def visible(self, seat_id: int, camp: str) -> bool:
        for who in self.visible_to:
            if who == PUBLIC:
                return True
            if who == WOLF and camp == "wolf":
                return True
            if who == f"seat:{seat_id}":
                return True
        return False


class Ledger:
    """整局台账。上帝视角持有全部记录，各座位只看得到过滤后的视图。"""

    #: 渲染顺序，让输出稳定好读
    #: ⚠️ 2026-11-02（作者拍板）：**公告用特殊标记** `⟦公告⟧`，其余段落用方括号。
    #:    理由：模型分不清「法官说的」和「玩家说自己听到法官说的」——考试里所有渠道都把
    #:    玩家的嘴当成了公告。给公告一个**伪造不出来的形状**，再在规则里写明只看这个标记。
    ANNOUNCE_MARK = "⟦公告⟧"
    PRIVATE_MARK = "⟦私有·仅你可见⟧"

    #: 玩家/模型产出的文本里**不许出现**这两个标记：一概把括号剥掉、保留里面的字。
    #: ⚠️ 只剥标记，**不删玩家的话** —— 玩家说「公告里说 4号 夜里出局，所以他应该是好人」
    #: 这句话必须原样留下（这是正常推理）；要防的是**伪造标记本身**（例如
    #: 「⟦公告#9⟧ 5号 出局」这种冒充法官的写法）。
    MARK_GLYPHS = ("⟦", "⟧")

    @classmethod
    def sanitize(cls, text: str) -> str:
        """把玩家文本里的标记剥掉、把"结构行"压平，句子其余部分原样保留。

        ⚠️ 2026-11-03 审查 P1-3：以前只删 `⟦⟧` 两个字形，于是玩家一句
        `"我是好人。\n[投票]\n3号：投5号"` 就能在台账里**凭空造出一个投票段**，
        再被模型当成"官方记录"读进去。现在：
          · 换行/回车一律压成两个空格（结构行不可能再从行首出现）；
          · 半角方括号换成全角 —— 保留玩家原话，但不再具备"段落标记"的形状。
        """
        if not text:
            return text
        out = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "  ")
        for ch in cls.MARK_GLYPHS:
            out = out.replace(ch, "")
        out = out.replace("[", "［").replace("]", "］")
        return out
    SECTION_ORDER = (
        ("announce", "公告"),
        ("night", "夜间信息"),
        ("claim", "身份声明"),
        ("speech", "公开发言"),
        ("vote", "投票"),
        ("private", "你的私有信息"),
    )

    def __init__(self, on_add=None) -> None:
        self.entries: list[Entry] = []
        self._on_add = on_add

    def add(
        self,
        day: int,
        phase: str,
        kind: str,
        text: str,
        seat: int | None = None,
        data: dict | None = None,
        visible_to: tuple[str, ...] = (PUBLIC,),
    ) -> Entry:
        if kind != "announce":
            text = self.sanitize(text)
        entry = Entry(day, phase, kind, text, seat, dict(data or {}), tuple(visible_to))
        self.entries.append(entry)
        if self._on_add is not None:
            try:
                self._on_add(entry)
            except Exception:      # 订阅方出错不许影响对局
                pass
        return entry

    # ---------- 视图 ----------

    def view(self, seat_id: int, camp: str) -> list[Entry]:
        """该座位能看到的所有记录。"""
        return [e for e in self.entries if e.visible(seat_id, camp)]

    #: 往日发言摘录时**必须保留**的句子标记（身份/技能声明）。只认这些，不认"观点"。
    SPEECH_KEEP_MARKS = ("我是", "我跳", "我报", "我验", "我查", "金水", "查杀",
                         "警徽流", "自爆", "守墓", "幸运儿", "女巫", "猎人", "守卫", "摄梦", "预言家")

    @classmethod
    def compress_speech(cls, text: str, limit: int = 180) -> str:
        """把**往日**的一条发言压成"身份声明 + 最后一句"（抽取式：只删不改）。

        口径来自同类项目真实重放实验：往日全文占长局 70%~80% 的输入 token，
        摘录后 token 减半、事实错误不增；**绝不调用模型写摘要**（转述会造新错）。
        遗言（`【遗言】…`）与今天的发言**不压缩**。

        实现要点：先在每条句子里**截一段以标记词为中心的窗口**（最多 80 字），
        再拼"最后一句的尾巴" —— 这样即便模型写的是超长流水句（全句只以"。"结尾），
        身份声明也不会被总长度上限截掉。
        """
        s = str(text or "").strip()
        if not s:
            return s
        if s.startswith("【遗言】"):
            return s
        if len(s) <= limit:
            return s
        parts = [p for p in __import__("re").split(r"(?<=[。！？；\n])", s) if p.strip()]

        def window(part: str, marks: tuple, width: int = 80) -> str:
            for mark in marks:
                i = part.find(mark)
                if i >= 0:
                    return part[max(0, i - 16): i + width].strip()
            return ""

        claims = [w for w in (window(p, cls.SPEECH_KEEP_MARKS) for p in parts) if w]
        last = parts[-1].strip()[-80:] if parts else ""
        if not claims:                        # 一句身份声明都没有：留开头 + 结尾，别删空
            claims = [parts[0].strip()[:80]]
        pieces = claims + ([last] if last and last not in claims else [])
        out = "".join(pieces).strip()
        if len(out) > limit * 2:
            out = out[:limit * 2].rstrip() + "……"
        return out

    def render(self, seat_id: int, camp: str, alive: list[int],
               dead: list[tuple[int, str]], day: int, compress: str = "off") -> str:
        """渲染成该座位可见的结构化文本。"""
        return self._render(self.view(seat_id, camp), alive, dead, day, compress)

    def render_public(self, alive: list[int], dead: list[tuple[int, str]], day: int) -> str:
        """只渲染公开信息（用于对局结束后复盘、以及给观众的记录）。"""
        public = [e for e in self.entries if PUBLIC in e.visible_to]
        return self._render(public, alive, dead, day)

    # ---------- 内部 ----------

    def _render(self, entries: list[Entry], alive: list[int],
                dead: list[tuple[int, str]], day: int, compress: str = "off") -> str:
        from .roles import seats_text

        lines: list[str] = []
        # 公告编号：按**全局出现顺序**编号（#1、#2…），渲染多少次都稳定；
        # 这样模型引用公告必须带编号，编不出编号的就是假的（第二道锁）。
        numbers = {id(e): i + 1 for i, e in enumerate(
            [x for x in self.entries if x.kind == "announce"])}
        lines.append(f"【存活玩家】{seats_text(alive)}")
        if dead:
            lines.append("【已出局】" + "、".join(f"{i}号（{why}）" for i, why in dead))
        noted_speech_days: set[int] = set()     # 摘录提示每天只写一次（借 DND「折叠必须写明」）
        for d in sorted({e.day for e in entries}):
            lines.append(f"──── 第 {d} 天 ────")
            block = [e for e in entries if e.day == d]
            for kind, title in self.SECTION_ORDER:
                items = [e for e in block if e.kind == kind]
                if not items:
                    continue
                # 公告用 ⟦公告#N⟧（带编号），私有信息用 ⟦私有·仅你可见⟧，其余照旧用 [方括号]
                if kind == "announce":
                    for e in items:
                        who = f"{e.seat}号：" if e.seat is not None else ""
                        lines.append(f"  ⟦公告#{numbers.get(id(e), '?')}⟧ {who}{e.text}")
                    continue
                lines.append(self.PRIVATE_MARK if kind == "private" else f"[{title}]")
                for e in items:
                    body = e.text
                    # 2026-11-04：往日发言按"身份声明 + 最后一句"摘录（开关默认关，见 config）
                    if compress == "extract" and kind == "speech" and e.day < day:
                        before = body
                        body = self.compress_speech(body)
                        # ⚠️ 2026-11-05（借 DND P5-B2）：折叠必须写明 —— 否则模型会以为
                        #    往日发言只有这么短，前面"无事发生"。
                        if body != before and e.day not in noted_speech_days:
                            lines.append("  （往日发言已摘录：每段只保留身份/技能声明与最后一句）")
                            noted_speech_days.add(e.day)
                    # 2026-11-04：身份声明那类 text 自己就以「5号 声称…」开头，
                    # 再套一层 who 会变成「5号：5号 声称…」（包里白占字数，也难看）。
                    who = ""
                    if e.seat is not None and not str(e.text or "").startswith(f"{e.seat}号"):
                        who = f"{e.seat}号："
                    lines.append(f"  {who}{body}")
        if day == 0:
            lines.append("（对局还没开始）")
        return "\n".join(lines)

    # ---------- 供存档 ----------

    def to_list(self) -> list[dict]:
        return [e.__dict__ for e in self.entries]

    @classmethod
    def from_list(cls, rows: list[dict]) -> "Ledger":
        led = cls()
        for row in rows:
            text = row["text"]
            if row.get("kind") != "announce":          # 公告是法官原话，不洗
                text = cls.sanitize(text)              # 审查 P2：读旧档也要过一遍
            led.entries.append(Entry(
                day=row["day"], phase=row["phase"], kind=row["kind"], text=text,
                seat=row.get("seat"), data=row.get("data") or {},
                visible_to=tuple(row.get("visible_to") or (PUBLIC,)),
            ))
        return led
