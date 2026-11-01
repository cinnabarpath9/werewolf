# coding=utf-8
"""术语库闸门：模型读的「术语速查」和人读的 `content/术语库.md` 必须对得上。

为什么要有这个：2026-10-09 项目自查发现，`打法书.md`/`规则书.md` 里
金水 43 次、悍跳 48 次、刀口 39 次、上警 85 次，**一次定义都没有** ——
模型只能猜这些行话的意思，猜错就是"发言听着有理、判断全歪"。
补了两份（模型版速查 + 人看的术语库）之后，用这个脚本盯住它们别走散：

  1. 两份文件都在、模型版真的进了**所有板子**的规则（它是 `frag:core` 的一部分）；
  2. 两份都必须覆盖一组**关键术语**（MUST 表）—— 谁删了一条立刻红；
  3. 人看的术语库要有一条**免责说明**（各家规则细节不同、以本项目为准）；
  4. 模型版要够短（它进每一次调用的包，撑大了就是真金白银）。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


#: 必须被解释的关键术语（模型会在发言里直接用，听不懂就误判）
MUST_TERMS = ["金水", "查杀", "银水", "警徽流", "悍跳", "对跳", "倒钩", "冲锋", "抗推",
              "归票", "退水", "站边", "票型", "前置位", "后置位", "警上", "警下",
              "平安夜", "奶死", "同守同救", "自刀", "空刀", "刀口", "盲毒",
              "屠边", "屠城", "PK", "翻牌"]

from exam_bank import rules_text_for                            # noqa: E402

print("=== 一、两份文件都在 ===")
from _content import TERMS as glossary_path  # noqa: E402
check("1a 人看的术语库存在", os.path.isfile(glossary_path))
glossary = open(glossary_path, encoding="utf-8").read() if os.path.isfile(glossary_path) else ""

print("\n=== 二、模型版速查进了每个板子的规则 ===")
#: 2026-10-30：产品只剩四张板子（11 人狼王版 / 12 人预女猎白 / 9 人屠城 / 6 人明牌屠城）
boards = ["eleven_wolfking", "twelve_standard", "nine_all", "six_all_reveal"]
per: dict[str, str] = {}
for b in boards:
    try:
        per[b] = rules_text_for(b)
    except Exception as exc:                                  # noqa: BLE001
        per[b] = ""
        check(f"2[{b}] 规则能组装", False, f"{type(exc).__name__}: {exc}")
for b, text in per.items():
    check(f"2[{b}] 含【术语速查】", "【术语速查】" in text)

print("\n=== 三、关键术语两边都要有 ===")
body = per.get("eleven_wolfking", "")
missing_model = [t for t in MUST_TERMS if t not in body]
missing_gloss = [t for t in MUST_TERMS if t not in glossary]
check(f"3a 模型版覆盖全部 {len(MUST_TERMS)} 个关键术语", not missing_model,
      f"缺：{missing_model}" if missing_model else "")
check(f"3b 人看的术语库也覆盖全部 {len(MUST_TERMS)} 个", not missing_gloss,
      f"缺：{missing_gloss}" if missing_gloss else "")

print("\n=== 四、格式与成本 ===")
check("4a 人看的术语库有「免责说明」（各家规则细节不同、以本项目为准）",
      "免责说明" in glossary and "各家" in glossary)
# ⚠️ 2026-10-09 修判据：组装后的规则文本里 **frag 标记已经被剥掉**，所以原来的
#    `(?=<!-- frag:core:end -->|$)` 永远匹配不到 → 一路吃到文末，量出 1568 字（假红）。
#    正确的边界是"下一段【…】"（速查后面紧跟角色片段）。
seg = re.search(r"【术语速查】(.*?)(?=\n【|$)", body, re.S)
seg_len = len(seg.group(1).strip()) if seg else 0
check("4b 模型版速查够短（≤700 字，它进每一次调用的包）", 0 < seg_len <= 700,
      f"实际 {seg_len} 字")
rows = len(re.findall(r"^\|", glossary, re.M))
check("4c 人看的术语库条目够全（≥40 行表格）", rows >= 40, f"{rows} 行")

# ── 五、打法书与本项目口径（2026-10-31 加）──
#   起因：一局真机对局里好人「不算票」被狼带票，且官方攻略里有本项目做不了的东西
#   （退水）和过时的站位建议（悍跳只认"后置位警上金水"、不看位置学）。
print("\n=== 五、打法书口径（退水 / 算票 / 位置学）===")
from _content import playbook_raw
playbook = playbook_raw()
from _content import read as _read_content, RULES as _RULES
rules_book = _read_content(_RULES)

check("5a 规则书的「退水」明确标注本项目不支持",
      "退水" in rules_book and "本项目不支持退水" in rules_book, "")
check("5b 打法书里不许再把退水当成可用选项（负向断言）",
      "立刻退水" not in playbook and "怂打、立刻退水" not in playbook, "")
check("5c 打法书写明「本项目没有退水」",
      "本项目没有「退水」" in playbook, "")
check("5d 好人算票小节在（三个数：几狼几好 / 票够不够 / 投错代价）",
      "好人必须算的三个数" in playbook and "今天够不够票推出一个人" in playbook
      and "投错的代价是几轮" in playbook, "")
check("5e 悍跳假验人必须过位置学（不许飞半个场子）",
      "假验人必须按「位置学」编" in playbook and "别飞半个场子" in playbook, "")
check("5f 有一节「官方攻略怎么用」，冲突时以本文件为准",
      "官方攻略怎么用" in playbook and "本项目没有退水" in playbook, "")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")

sys.exit(1 if BAD else 0)
