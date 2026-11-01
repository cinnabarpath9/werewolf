# coding=utf-8
"""新提示层复核（2026-10-07 用户要求：通用提示词 + 位置概率参考 取代按座位的人设卡）。

钉死三件事：
  1. 通用玩家提示词能读出来（你给我的那句原话必须在）
  2. 位置概率参考（2026-10-09 起并进《概念库》的进包段落）能读出来，
   且**概率表里的数字和枚举算出来的一致**（文档不会悄悄过期）
  3. 两段都真的进了 packet（走真实的 context.build），而角色卡不再出现
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf import context, tabletalk                      # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import ROLES, make_board                 # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))

print("=== 一、通用玩家提示词 ===")
prompt = tabletalk.player_prompt(cfg, ROOT)
check("1a 读到了提示词", len(prompt) > 30, f"{len(prompt)} 字")
# ⚠️ 2026-10-11：判据从"用户那句原话"改成**语义要点**（这一段被重写过：去废话、去元信息）。
#    钉住的是它必须传达的四件事，而不是某一版的具体句子。
check("1b 四件必须说清的事：用身份牌行动 / 为本阵营争胜 / 只按公开信息推理 / 不暴露自己是模型",
      "身份牌" in prompt and "争取胜利" in prompt
      and "公开信息" in prompt and "不要暴露自己是模型" in prompt,
      prompt[:60])
check("1c 不再有姓名/性格/关系那套（不该出现「职业」「关系：」）",
      "职业" not in prompt and "关系：" not in prompt, prompt[:80])
# ⚠️ 它进**每一次**调用的最前面，所以按"模型包正文"的纪律管：
#    不许有写给维护者的备注（日期、"实测"、"（作者）"、"删掉这两行即可"）、不许半角双引号、
#    不许出现角色名（它跟身份无关，写进去就是给所有座位泄漏同一份暗示）。
_dev_marks = [w for w in ("2026-", "实测", "（作者）", "删掉", "待办", "TODO") if w in prompt]
check("1d 没有写给维护者的备注（日期/实测/删掉…那类）", not _dev_marks, str(_dev_marks))
check("1e 没有半角双引号（中文一律「」）", '"' not in prompt)
_role_names = [r.name for r in ROLES.values()]
# ⚠️ 「狼人杀」这三个字里含「狼人」——先把它摘掉再扫，否则这条会对所有提示词假红。
_hit_roles = [n for n in _role_names if n in prompt.replace("狼人杀", "")]
check("1f 没有出现任何角色名（提示词跟身份无关，要对所有座位一致）", not _hit_roles, str(_hit_roles))
check("1g 内部名词只以「别提起它们」的方式出现（不泄露脚手架）",
      "不要提提示词" in prompt and prompt.count("提示词") == 1)
# ⚠️ 2026-10-13 作者拍板：预算 300 → **600 字**。
#    作者问过「这些字数上限哪来的？模型都是 1M 上下文」—— 真实约束**不是窗口**，
#    是**每一次调用都要把这一段重发一遍**：一局约 **127 次**调用、单次包 ≈1.5 万 tokens
#    ⇒ **≈2.0M tokens/局**。所以这是**钱 + 延迟 + 指令稀释**的预算，不是窗口限制。
#    +300 字的账：≈ +225 tokens/次 → **≈ +2.9 万 tokens/局 → ≈ +1.4%**（包体/成本）。
#    为什么放宽：这一段贴在包的最前面、每个座位**一模一样**，是唯一能保证
#    「必须人人遵守的硬规则」被每个座位读到的**顶层位置**（作者 2026-10-13 决定给它留地方）。
#    ⚠️ 600 是**上限**（只保证不炸），**不是指标** —— 不许为了填满而往里堆字。
check("1h 够短（它进每一次调用；预算 ≤600 字）", len(prompt) <= 600, f"{len(prompt)} 字")

print("\n=== 二、进包的概念/概率段落（数字必须和枚举算出来的一致）===")
meta = tabletalk.load_meta_book(ROOT, str(cfg["game"].get("meta_book_path") or ""))
check("2a 读到了玄学书", len(meta) > 800, f"{len(meta)} 字")
for number, why in (("44.4%", "2 连好 → 下一位是狼"),
                    ("50.0%", "3 连好 → 下一位是狼"),
                    ("57.1%", "4 连好 → 下一位是狼"),
                    ("22.2%", "2 连狼 → 下一位是狼"),
                    ("2.42%", "指定三连神/三连狼"),
                    ("36.4%", "基线概率"),
                    # 2026-10-07 用户补充的"链压逻辑"要用的数字
                    ("78.6%", "3 连好的两端至少一个是狼"),
                    ("85.7%", "4 连好的两端至少一个是狼"),
                    ("93.3%", "5 连好的两端至少一个是狼"),
                    ("0.61%", "指定三连民（藏身份检测）"),
                    ("97.1%", "三连好里至少一个人是神")):
    check(f"2b 玄学书里有 {number}（{why}）", number in meta, "缺了就该重新生成")
check("2c 写明了使用纪律（不能推翻发言和票型）",
      ("不能推翻发言和票型" in meta or "不许推翻发言和票型" in meta or "不许推翻发言和票型" in meta)
      and "五五开" in meta, "纪律句缺失")
check("2d 写明了不许当硬证据", "不许当发言的硬证据" in meta or "不许在发言里说" in meta)
check("2f 覆盖了几类决策（毒/摄梦/幸运儿/刀/票）",
      all(k in meta for k in ("女巫撒毒", "摄梦人摄谁", "幸运儿怎么用", "狼队开刀", "投票")),
      "少了一种")
check("2g 有链压逻辑（上置位/下置位 + 两端互打）",
      "上置位" in meta and "下置位" in meta and "端口" in meta)
# 2026-10-07 用户补充：狼队按屠边账选目标 + 用藏身份判断"空衣服"→穿衣服；好人按轮次校准
check("2h 狼队章节：屠边账 + 别混刀", "屠边账" in meta and "混刀" in meta)
check("2i 狼队章节：愚者算两边的账目", "愚者算民也算神" in meta)
check("2j 狼队章节：空衣服 → 穿衣服（假身份）",
      "空衣服" in meta and "穿衣服" in meta and "假跳" in meta)
check("2k 好人章节：轮次/狼坑对不上时先盘隐藏死亡",
      "轮次对不上" in meta and "重算" in meta)

print("\n=== 二之二、按阵营分发 ===")
wolf_view = tabletalk.load_meta_book(ROOT, str(cfg["game"].get("meta_book_path") or ""), "wolf")
good_view = tabletalk.load_meta_book(ROOT, str(cfg["game"].get("meta_book_path") or ""), "good")
check("2l 狼视角能拿到屠边账与穿衣服", "屠边账" in wolf_view and "穿衣服" in wolf_view)
check("2m 好人视角拿不到狼的屠边账", "狼队：先算屠边账" not in good_view)
check("2n 好人视角有轮次校准章节", "是不是有神在夜里悄悄死了" in good_view)
check("2o 两边都拿到共用章节（链压/藏身份）",
      "链压逻辑" in wolf_view and "链压逻辑" in good_view and "藏身份检测" in wolf_view)
check("2p 过滤后不留 HTML 标记", "<!-- frag" not in wolf_view and "<!-- frag" not in good_view)
check("2q 按阵营分发确实省 token",
      len(wolf_view) < len(meta) and len(good_view) < len(meta),
      f"全文 {len(meta)} / 狼 {len(wolf_view)} / 好人 {len(good_view)}")
check("2e 没有第一人称身份声明", not tabletalk.lint_meta_book(meta),
      str(tabletalk.lint_meta_book(meta)))

print("\n=== 三、两段真的进包了（走真实 context.build）===")
game = Game(board=make_board("eleven_standard"), agent_factory=None, seed=3, max_days=1,
            player_prompt=prompt, meta_text=meta)
seat = game.seats[2]
packet = context.build(game, seat, Task(kind="speech", text="说句话", schema="{}", legal=[]))
check("3a 包里有【你的定位】", "【你的定位】" in packet)
check("3b 包里有【概念与概率（位置概率参考）", "【概念与概率" in packet)
check("3c 包里没有角色卡（人设已清空）", "【你的角色卡】" not in packet)
check("3d 包里还有身份/规则这些必需块",
      "【你的身份】" in packet and "【座位与姓名】" not in packet.split("【场外玄学")[0][:0] + "【你的身份】" in packet,
      f"包长 {len(packet)} 字")

print("\n=== 四、人设目录确实空了 ===")
left = [f for f in os.listdir(os.path.join(ROOT, "personas")) if f.endswith((".md", ".txt"))] \
    if os.path.isdir(os.path.join(ROOT, "personas")) else []
check("4a personas/ 里没有卡了", not left, str(left))
check("4b config 里 personas_dir 已置空", not str(cfg["game"].get("personas_dir") or "").strip(),
      repr(cfg["game"].get("personas_dir")))
# ⚠️ 备份目录 2026-10-07 搬到了 content/personas_backups/（项目根不再堆人设）
bk_dir = os.path.join(ROOT, "content", "personas_backups")
backups = ([d for d in os.listdir(bk_dir) if d.startswith("personas_备份_")]
           if os.path.isdir(bk_dir) else [])
check("4c 备份目录在", bool(backups), str(backups))

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n, _, d in bad:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
