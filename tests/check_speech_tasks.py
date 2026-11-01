# coding=utf-8
"""确认每个任务种类落到哪个思考档（新加的警长/遗言类任务有没有漏在"发言档"外面）。"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.llm import AUX_TASKS, SPEECH_TASKS             # noqa: E402

print("SPEECH_TASKS =", SPEECH_TASKS)
kinds = ["speech", "pk_speech", "sheriff_speech", "last_words", "vote", "sheriff_vote",
         "sheriff_run", "badge", "wolf_talk", "wolf_kill", "seer_check", "witch",
         "guard", "hunter_shoot"]
for k in kinds:
    if k in AUX_TASKS:
        tag = "辅助档 low（非座位）"
    elif k in SPEECH_TASKS:
        tag = "发言档 high（座位）"
    else:
        tag = "决策档 high（座位）"
    print(f"  {k:<16} -> {tag}")

# ⚠️ 2026-10-09 补：以前只打印每个任务落到哪个档，没有退出码（假回归门）。
# ⚠️ 2026-10-10 再修：分组从两组变**三组** ——
#    · 发言组 = ("speech",) 座位上的单问发言；
#    · **辅助组 = ("reflect",) 非座位的活**（复盘笔记、连通性自检）→ 低档；
#    · 其余全在决策组（座位上的行动，满档）。
#    以前这份清单里还列着 pk_speech / sheriff_speech / last_words —— 那**根本不是 task.kind**
#    （PK 发言、竞选发言、遗言全都用 kind="speech" 记账，靠文案前缀区分），
#    所以它们"不在 SPEECH_TASKS 里"是**正常的**，旧判据是假的。
#    真正要守的是这三条：
#      · **发言组**集合必须正好是 ("speech",)；
#      · **辅助组**集合必须正好是 ("reflect",)（这两组过去被写成一组，注释已按代码改正）；
#      · `wolf_talk` **不许**被悄悄降回发言档 —— 2026-10-08 用户拍板升回决策档
#        （夜聊是协商分工，不是照着念），代码注释里写明"要退回低档只需加回元组"，
#        这里就是防那一改没人发现。
_bad = []
if tuple(SPEECH_TASKS) != ("speech",):
    _bad.append(f"发言组应为 ('speech',)，实际 {SPEECH_TASKS}")
if tuple(AUX_TASKS) != ("reflect",):
    _bad.append(f"辅助组应为 ('reflect',)，实际 {AUX_TASKS}")
if "wolf_talk" in SPEECH_TASKS or "wolf_talk" in AUX_TASKS:
    _bad.append("wolf_talk 不许降出发言/辅助档 —— 它是决策档（2026-10-08 用户拍板）")

print()
if _bad:
    print("  [FAIL] " + "；".join(_bad))
else:
    print("  [OK] 三组各就各位：发言 ('speech',) / 辅助 ('reflect',) / 其余决策档")
print(f"\n共 3 项：通过 {3 - len(_bad)}，失败 {len(_bad)}")
sys.exit(1 if _bad else 0)
