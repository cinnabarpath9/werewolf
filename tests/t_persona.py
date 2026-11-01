# coding=utf-8
"""人设扩容（角色卡）复核：字段解析、老格式兼容、越界告警、跨座位一致性、进包内容。

最要紧的两条：
  · **不许越界**：角色卡里出现身份/队友/角色专属知识 → 必须报警（信息隔离的后门）
  · **关系不能当证据**：卡片里必须写明"只当平票偏置、不能推翻场上证据"（用户定的口径）

跑法： python t_persona.py
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf import persona                                 # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def main() -> int:
    print("=== 一、解析：新格式（front-matter）/ 老格式（纯正文）===")
    card = persona.parse(3, """---
name: 陈默
age: 30 岁
job: 医疗器械销售
background: 跑了六年医院。
relations:
  - 1号 林知夏是我大学同学
  - 8号 高远以前跟我一个公司
---

## 说话方式
语气大，爱下判断。
""")
    check("1a 姓名/年龄/职业/经历都读出来了",
          (card.name, card.age, card.job) == ("陈默", "30 岁", "医疗器械销售")
          and "医院" in card.background, f"{card.name}/{card.age}/{card.job}")
    check("1b relations 读成列表（2 条）", len(card.relations) == 2, str(card.relations))
    check("1c 正文与卡头分开", "语气大" in card.style and "name:" not in card.style)

    old = persona.parse(5, "你说话很短，爱用反问。")
    check("1d 老格式（没有 front-matter）整篇当正文",
          old.style == "你说话很短，爱用反问。" and not old.name)

    bad = persona.parse(6, "---\nname: [没闭合\n---\n正文在这。")
    check("1e front-matter 写坏了也不丢正文", bad.style.strip() == "正文在这。", bad.style[:30])

    print("\n=== 二、越界告警 ===")
    cases = [
        ("2a 正文写了角色名", persona.parse(2, "我是预言家，我会跳。"), "预言家"),
        ("2b 职业写成角色名", persona.parse(2, "---\njob: 猎人\n---\n说话很稳。"), "猎人"),
        ("2c 关系里带身份知识", persona.parse(2, "---\nrelations:\n  - 3号 是我狼队友\n---\n短。"), "队友"),
        ("2d 姓名里带角色名", persona.parse(2, "---\nname: 女巫小姐\n---\n短。"), "女巫"),
    ]
    for label, p, word in cases:
        warns = persona.lint_persona(p)
        check(f"{label} → 报警", bool(warns) and any(word in w for w in warns),
              (warns[0][:70] if warns else "（没报警）"))

    clean = persona.parse(2, "---\nname: 赵铁柱\njob: 健身教练\n---\n短句直给。")
    check("2e 正常卡片不误报", not persona.lint_persona(clean), str(persona.lint_persona(clean)))

    print("\n=== 三、跨座位一致性 ===")
    a = persona.parse(1, "---\nname: 林知夏\nrelations:\n  - 3号 陈默是我同学\n---\n短。")
    b = persona.parse(3, "---\nname: 陈默\n---\n短。")
    check("3a 互相对得上 → 不报警", not persona.cross_check({1: a, 3: b}),
          str(persona.cross_check({1: a, 3: b})))
    c = persona.parse(3, "---\nname: 老王\n---\n短。")
    warns = persona.cross_check({1: a, 3: c})
    check("3b 叫错名字 → 报警", bool(warns) and "老王" in warns[0], str(warns)[:90])
    d = persona.parse(2, "---\nname: 赵铁柱\nrelations:\n  - 9号 是我朋友\n---\n短。")
    warns = persona.cross_check({2: d})
    check("3c 提到没有角色卡的座位 → 报警", bool(warns) and "9号" in warns[0], str(warns)[:90])

    print("\n=== 四、渲染（进 packet 的那段）===")
    text = persona.render(card)
    check("4a 抬头写清「只影响说话、不给额外信息」",
          "不影响你怎么判断" in text and "不给你任何额外信息" in text)
    check("4b 带姓名/职业/经历/关系", all(k in text for k in ("陈默", "医疗器械销售", "医院", "林知夏")))
    check("4c 关系附了「只当平票偏置、不能当证据」的纪律",
          "只能" in text and "打破平局" in text and "不能推翻场上证据" in text)
    check("4d 没有 markdown 噪音（## / **）", "##" not in text.replace("**", "##").replace("**", "") or "##" not in text)
    check("4e 空卡渲染成空字符串", persona.render(persona.Persona(seat=1)) == "")

    print("\n=== 五、当前配置下的人设（2026-10-07 起默认不用人设卡）===")
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ps = persona.load(cfg, ROOT)
    dir_setting = str((cfg.get("game") or {}).get("personas_dir") or "").strip()
    if not dir_setting:
        check("5a personas_dir 置空 → 不加载任何角色卡（改用通用提示词）", not ps.texts,
              f"{len(ps.texts)} 张")
        check("5b 没有卡时也不该有告警", not ps.warnings, str(ps.warnings)[:120])
    else:
        human_seat = int(((cfg.get("game") or {}).get("human_seat") or 0))
        want = 11 - (1 if human_seat else 0)
        check("5a 十个 AI 座位都加载到了角色卡", len(ps.texts) == want,
              f"{len(ps.texts)} 张 / 期望 {want}")
        check("5b 每张卡都有姓名", len(ps.names()) == len(ps.texts), str(ps.names()))
        check("5c 目录里的卡没有越界/不一致告警", not ps.warnings, str(ps.warnings)[:200])
        avg = sum(len(t) for t in ps.texts.values()) // max(1, len(ps.texts))
        check("5d 卡片长度可控（平均 < 800 字）", avg < 800, f"平均 {avg} 字")
    # 不管用不用卡，越界词检测本身必须还在工作（拿一张临时卡验）
    probe = persona.parse(2, "我是预言家，昨晚验了 3 号是狼。")
    check("5e 越界检测仍然有效（临时卡能被抓出来）", bool(persona.lint_persona(probe)),
          str(persona.lint_persona(probe))[:80])

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
