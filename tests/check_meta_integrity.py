# coding=utf-8
"""闸门：**压缩/改写进包材料时不许丢内容**（用户要求"降质量的改动不动"）。

为什么要有它（2026-10-09 实测踩到）：
  我把《概念库》的进包段从 6067 字压到 4139 字时，**动结构动了出问题**：
    · `<!-- frag:good -->` 块被我弄丢了 —— 好人章被圈进了狼块里，
      结果**好人拿不到自己那节、狼反而多拿一节**（阵营分发错位，而且肉眼很难看出来）；
    · 测试盯着的关键词（"空衣服"）被我改写成"衣服空没空" —— 意思没丢，**关键词丢了**。
  这两类错都不会让别的测试变红（它们只查"有没有某些字"），所以必须专门守。

判据（全部对着**当前进包段**做）：
  1. `frag:` 标记**配平**：wolf/good 各自成对，且都在 `frag:meta` 之内；
  2. **内容不漏**：`通用 + 狼块 + 好人块` 拼起来 == 完整进包段（除标记本身）；
  3. **阵营不串**：好人视角里没有狼的专属节、狼视角里没有好人的专属节；
  4. **硬信息（数字）一个都不能少**：一张"必须出现的数字清单"；
  5. **关键概念词一个都不能少**：一张"必须出现的词清单"。
  （4/5 是"压缩不许降质量"的底线：以后要压缩，只能改说法，不能丢结论。）
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

import yaml                                                     # noqa: E402

from wolf import tabletalk                                      # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
path = str((cfg.get("game") or {}).get("meta_book_path") or "")
full = tabletalk.load_meta_book(ROOT, path, "")
wolf = tabletalk.load_meta_book(ROOT, path, "wolf")
good = tabletalk.load_meta_book(ROOT, path, "good")

raw = io.open(os.path.join(ROOT, path), encoding="utf-8").read()
seg = raw.split("<!-- frag:meta:begin -->", 1)[1].split("<!-- frag:meta:end -->", 1)[0]
print(f"=== 进包段 {len(seg)} 字；通用 {len(full)} / 狼 {len(wolf)} / 好人 {len(good)} ===")

# ① 标记配平
opens = re.findall(r"<!-- frag:(wolf|good) -->", seg)
closes = len(re.findall(r"<!-- frag:end -->", seg))
check("1a wolf/good 的 begin 数量与 end 数量配平", len(opens) == closes,
      f"begin={opens} end={closes}")
check("1b 恰好各一个 wolf / good 块", sorted(opens) == ["good", "wolf"], str(opens))

# ② 内容不漏：**逐节**比对（每一节必须整段出现在它该出现的那个视角里）
# ⚠️ 不能把三个视角拼起来做整段比对：段落与 frag 块的交界处会出现 12 字窗口的"假缺口"
#    （标记被去掉、块被抽走造成的），实测会误报 12 个不存在的丢失。
def squash(t: str) -> str:
    return re.sub(r"[\s>*|#`\-]+", "", t)


def sections(text: str) -> list[tuple[str, str]]:
    """把进包段切成 [(该节所属阵营, 该节正文)]；阵营 = common/wolf/good。"""
    out: list[tuple[str, str]] = []
    camp = "common"
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("<!-- frag:wolf"):
            camp = "wolf"
            continue
        if line.startswith("<!-- frag:good"):
            camp = "good"
            continue
        if line.startswith("<!-- frag:end"):
            camp = "common"
            continue
        if line.startswith("## "):
            if buf:
                out.append((camp, "\n".join(buf)))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        out.append((camp, "\n".join(buf)))
    return out


VIEWS = {"common": full, "wolf": wolf, "good": good}
lost: list[str] = []
for camp, body in sections(seg):
    text = squash(body)
    if len(text) < 8:
        continue
    view = squash(VIEWS[camp] if camp != "common" else full)
    # 公共节在"全员视角"里必须完整出现；阵营节在对应阵营视角里必须完整出现
    if text not in squash(full) and text not in view:
        lost.append(f"[{camp}] {body.strip().splitlines()[0][:40]}")
check("2a 每一节都整段出现在它该出现的视角里（没有内容被静默丢掉）",
      not lost, str(lost[:3]))
check("2b 进包段被切成了合理数量的节（结构没被压碎）",
      len(sections(seg)) >= 6, f"{len(sections(seg))} 节")

# ③ 阵营不串
wolf_only = "狼队：先算屠边账"
good_only = "是不是有神在夜里悄悄死了"
check("3a 好人视角拿不到狼的专门节", wolf_only not in good)
check("3b 狼视角拿不到好人的专门节", good_only not in wolf)
check("3c 两边都能拿到通用节（链压逻辑 / 概率速查）",
      "链压逻辑" in wolf and "链压逻辑" in good and "概率速查" in wolf and "概率速查" in good)

# ④ 硬信息（数字）清单 —— 压缩只许改说法，不许丢数字
# ⚠️ 2026-10-11：`19.4%` / `90.9%` 是**首尾不接**的旧口径；作者拍板按"11 张牌成圈"，
#    正数改成 `23.3%` / `96.7%`（表里同时留着旧口径做对照，所以四个都列）。
#    ⚠️ 光钉字面量看不出口径错（这次就是钉了半个月才发现）——真正的守卫是
#    `tests/t_meta_odds.py`：它用发牌枚举把每个数字**重算一遍**再跟表比。
NUMBERS = ["36.4%", "44.4%", "50.0%", "57.1%", "72.2%", "78.6%", "85.7%", "93.3%",
           "30.0%", "22.2%", "12.5%", "77.8%", "2.42%", "0.61%", "21.2%", "10.6%",
           "23.3%", "96.7%", "97.1%", "19.4%", "90.9%"]
miss_num = [n for n in NUMBERS if n not in full]
check(f"4a 概率数字一个都不少（{len(NUMBERS)} 个）", not miss_num, str(miss_num))

# ⑤ 关键概念词清单
WORDS = ["链压逻辑", "藏身份检测", "上置位", "下置位", "端口", "藏身份",
         "屠边账", "混刀", "愚者算民也算神", "空衣服", "穿衣服", "假跳",
         "轮次对不上", "重算", "平安夜", "使用纪律",
         "女巫撒毒", "摄梦人摄谁", "幸运儿怎么用", "狼队开刀", "投票",
         "五五开", "不许当发言的硬证据"]
miss_word = [w for w in WORDS if w not in full]
check(f"5a 关键概念词一个都不少（{len(WORDS)} 个）", not miss_word, str(miss_word))

# ⑥ 成本提醒：进包段不该再膨胀（超过 7000 字要说明理由）
# ⚠️ 2026-10-13 作者拍板：预算 5000 → **7000 字**。它跟通用提示词一样，**每一次调用都要重发**
#    （一局约 127 次调用、单次包 ≈1.5 万 tokens ⇒ ≈2.0M tokens/局）——
#    所以这是**钱 + 延迟 + 指令稀释**的预算，不是窗口限制（模型窗口都是 1M）。
#    +2000 字的账：≈ +1500 tokens/次 → **≈ +19 万 tokens/局 → ≈ +10% 成本 → ≈ ¥0.04/局**。
#    换来的是「以后可能会加一些新的概念，我也不确定」（作者原话）时**不用先压缩腾地方**
#    （旧口径下只剩 19 字余量，往进包段加任何一节都得先等量删）。
#    ⚠️ 但**再想加要有理由**：这条闸门的精神没变 —— 7000 是**上限，不是指标**；
#    它只保证「不炸」，**不鼓励填满**，加内容仍然要说清为什么值这份钱。
check("6a 进包段仍在预算内（≤7000 字）", len(seg) <= 7000, f"{len(seg)} 字")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
