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
        """把玩家文本里的标记括号剥掉，句子其余部分原样保留。"""
        if not text:
            return text
        out = text
        for ch in cls.MARK_GLYPHS:
            out = out.replace(ch, "")
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

    def render(self, seat_id: int, camp: str, alive: list[int],
               dead: list[tuple[int, str]], day: int) -> str:
        """渲染成该座位可见的结构化文本。"""
        return self._render(self.view(seat_id, camp), alive, dead, day)

    def render_public(self, alive: list[int], dead: list[tuple[int, str]], day: int) -> str:
        """只渲染公开信息（用于对局结束后复盘、以及给观众的记录）。"""
        public = [e for e in self.entries if PUBLIC in e.visible_to]
        return self._render(public, alive, dead, day)

    # ---------- 内部 ----------

    def _render(self, entries: list[Entry], alive: list[int],
                dead: list[tuple[int, str]], day: int) -> str:
        from .roles import seats_text

        lines: list[str] = []
        # 公告编号：按**全局出现顺序**编号（#1、#2…），渲染多少次都稳定；
        # 这样模型引用公告必须带编号，编不出编号的就是假的（第二道锁）。
        numbers = {id(e): i + 1 for i, e in enumerate(
            [x for x in self.entries if x.kind == "announce"])}
        lines.append(f"【存活玩家】{seats_text(alive)}")
        if dead:
            lines.append("【已出局】" + "、".join(f"{i}号（{why}）" for i, why in dead))
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
                    who = f"{e.seat}号：" if e.seat is not None else ""
                    lines.append(f"  {who}{e.text}")
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
            led.entries.append(Entry(
                day=row["day"], phase=row["phase"], kind=row["kind"], text=row["text"],
                seat=row.get("seat"), data=row.get("data") or {},
                visible_to=tuple(row.get("visible_to") or (PUBLIC,)),
            ))
        return led
