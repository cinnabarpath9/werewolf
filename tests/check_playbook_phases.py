# coding=utf-8
"""闸门：打法书**按阶段切**之后，内容一个字都不许丢（用户："降质量的改动不动"）。

判据：
  1. `frag:part:*` 标记**配平**（open/mid/late 的 begin 与 endpart 数量一致）；
  2. **所有阶段合起来 == 原文**（把 open/mid/late 三份拼起来必须覆盖整章，
     不许有"哪个阶段都不发"的孤儿正文 —— 那才是真的丢内容）；
  3. `late` 必须是 `open` 的**超集**（残局仍然要看 mid 的内容：穿身份、屠边账）；
  4. 狼章开局要**真的省下来**（≤ 全章的 75%）—— 不然这套机制白做；
  5. 每个 `frag:part` 块里的正文都不为空（防止空标签骗过第 2 条）。
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import tabletalk                                      # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _content import playbook_raw  # noqa: E402
raw = playbook_raw()
print(f"=== 打法书 {len(raw)} 字 ===")

# ① 标记配平
opens = re.findall(r"<!-- frag:part:(open|mid|late) -->", raw)
closes = len(re.findall(r"<!-- frag:endpart -->", raw))
check("1a part 标记配平", len(opens) == closes, f"begin={len(opens)} end={closes}")
check("1b 三个阶段都真的用上了", set(opens) == {"open", "mid", "late"}, str(sorted(set(opens))))

# ② 每个 part 块非空
empty = []
for m in re.finditer(r"<!-- frag:part:(open|mid|late) -->(.*?)<!-- frag:endpart -->", raw, re.S):
    if len(m.group(2).strip()) < 40:
        empty.append(f"{m.group(1)}：{m.group(2).strip()[:20]}")
check("2a 每个 part 块里都有正文（防空标签）", not empty, str(empty))

# ③ 逐节归属检查（比"整体覆盖率"更准：滑动窗口跨节会假报）
ROLES = ("wolf", "wolf_king", "seer", "witch", "dreamer", "idiot", "gravekeeper", "crow", "lucky")
PHASES = ("open", "mid", "late")
CAMP = {"wolf": "wolf", "wolf_king": "wolf", "seer": "good", "witch": "good", "dreamer": "good",
        "idiot": "good", "gravekeeper": "good", "crow": "good", "lucky": "good"}


def squash(t: str) -> str:
    # ⚠️ 先把 frag 标记行整体删掉再比：全文里有标记、阶段文本里没有，
    #    不删的话跨标记的窗口会假报"内容丢失"。
    t = re.sub(r"<!--\s*frag:[^>]*-->", "", t)
    return re.sub(r"[\s>*|#`\-]+", "", t)


def segs_of_chapter(text: str) -> list[tuple[str, str]]:
    """把一章切成 [(归属, 正文)]：归属 ∈ core/open/mid/late。"""
    out: list[tuple[str, str]] = []
    cur = "core"
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*<!--\s*frag:part:(open|mid|late)\s*-->\s*$", line)
        if m:
            if buf:
                out.append((cur, "\n".join(buf)))
            cur, buf = m.group(1), []
            continue
        if re.match(r"\s*<!--\s*frag:endpart\s*-->\s*$", line):
            if buf:
                out.append((cur, "\n".join(buf)))
            cur, buf = "core", []
            continue
        if re.match(r"\s*<!--\s*frag:", line):        # role/god/camp/end 等结构标记
            if buf:
                out.append((cur, "\n".join(buf)))
                buf = []
            continue
        buf.append(line)
    if buf:
        out.append((cur, "\n".join(buf)))
    return out


bad: list[str] = []
from _content import playbook_raw as _pb_raw  # noqa: E402
raw_book = _pb_raw()
for role in ROLES:
    m = re.search(rf"<!-- frag:role:{role} -->(.*?)<!-- frag:end -->", raw_book, re.S)
    if not m:
        bad.append(f"{role}: 找不到章")
        continue
    chapter = m.group(1)
    views = {ph: squash(tabletalk.filter_role(raw_book, role, CAMP[role], ph))
             for ph in PHASES}
    for where, body in segs_of_chapter(chapter):
        text = squash(body)
        if len(text) < 30:            # 标题行这种太短的跳过
            continue
        if where == "core":
            for ph in PHASES:         # core 必须三个阶段都在
                if text not in views[ph]:
                    bad.append(f"{role}: core 在 {ph} 里少了「{body.strip()[:18]}」")
                    break
        else:
            want = {"open": ["open"], "mid": ["mid", "late"], "late": ["late"]}[where]
            for ph in want:
                if text not in views[ph]:
                    bad.append(f"{role}: {where} 节在 {ph} 里少了「{body.strip()[:18]}」")
                    break
            # 反向：不该出现的阶段里不许出现
            for ph in [x for x in PHASES if x not in want]:
                if text in views[ph]:
                    bad.append(f"{role}: {where} 节不该出现在 {ph}：{body.strip()[:18]}")
                    break
check("3a 逐节检查：core 三阶段都在、open/mid/late 只在该出现的阶段（无内容丢失、无越界）",
      not bad, str(bad[:3]))

# ⑤ 狼章的开局要真的省下来
wolf_full = len(tabletalk.filter_role(raw, "wolf", "wolf", ""))
wolf_open = len(tabletalk.filter_role(raw, "wolf", "wolf", "open"))
ratio = wolf_open / max(1, wolf_full)
check("5a 狼章开局 ≤ 全章的 75%（真的省下来了）", ratio <= 0.75,
      f"{wolf_open}/{wolf_full} = {round(ratio * 100)}%")
check("5b 狼章开局拿不到残局那一节", "残局（人少了" not in
      tabletalk.filter_role(raw, "wolf", "wolf", "open"))
check("5c 狼章残局拿得到那一节", "残局（人少了" in
      tabletalk.filter_role(raw, "wolf", "wolf", "late"))
check("5d 狼章开局拿不到「穿身份」大节（留到中后期）",
      "穿身份：跳什么就演什么" not in tabletalk.filter_role(raw, "wolf", "wolf", "open"))
check("5e 狼章中后期拿得到「穿身份」",
      "穿身份：跳什么就演什么" in tabletalk.filter_role(raw, "wolf", "wolf", "mid"))

# ⑥ 阶段计算（按局面，不只看天数）
class _S:
    def __init__(self, day, alive):
        self.day = day
        self.seats = [type("x", (), {"alive": i < alive})() for i in range(11)]


check("6a 第 1 天 → open", tabletalk.phase_of(_S(1, 11)) == "open")
check("6b 第 2 天（人还多）→ mid", tabletalk.phase_of(_S(2, 10)) == "mid")
check("6c 第 3 天 → mid", tabletalk.phase_of(_S(3, 9)) == "mid")
check("6d 第 4 天 → late", tabletalk.phase_of(_S(4, 9)) == "late")
check("6e 第 2 天但只剩 5 个人 → late（残局不只看天数）",
      tabletalk.phase_of(_S(2, 5)) == "late")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
