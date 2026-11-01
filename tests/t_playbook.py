# coding=utf-8
"""验打法书的**按角色分发**：每个座位只拿到"通用 + 自己那个角色"的章节。"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context, tabletalk                      # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
book = tabletalk.load_playbook(ROOT)
print("=== 一、按角色过滤 ===")
views = {r: tabletalk.load_playbook(ROOT, "", r) for r in
         ("wolf", "seer", "witch", "hunter", "idiot", "villager")}
check("1a 狼视角有狼的战术（对跳/倒钩/自刀骗药）",
      "一狼上警对跳预言家" in views["wolf"] and "自刀骗药" in views["wolf"])
check("1b 狼视角没有别的角色的章节",
      "验相邻的" not in views["wolf"] and "救不救" not in views["wolf"])
check("1c 预言家视角有验人位置学（相邻锁 2 / 跳着锁 3）",
      "锁的范围**最小（2 个）**" in views["seer"] or "最小（2 个）" in views["seer"],
      "含相邻/跳着验的说明")
check("1d 女巫视角有救不救与什么时候开毒",
      "救不救" in views["witch"] and "毒药什么时候开", "盲毒" in views["witch"])
check("1e 猎人视角有被毒不能开枪", "不能开枪" in views["hunter"])
check("1f 白痴视角有翻牌与两边都算", "翻牌" in views["idiot"] and "两边都算" in views["idiot"])
check("1g 村民视角有算账与别硬凑狼坑", "容错" in views["villager"] and "硬凑狼坑" in views["villager"])
check("1h 每个角色都拿到了通用部分（轮次/容错/边界口径）",
      all("轮次够不够" in v and "屠边优先" in v for v in views.values()))
check("1i 过滤后不留 HTML 标记",
      all("<!-- frag" not in v for v in views.values()))
check("1j 每个角色的章节都比全文短（确实省了）",
      all(len(v) < len(book) for v in views.values()),
      f"全文 {len(book)}；" + "、".join(f"{k} {len(v)}" for k, v in views.items()))

# 2026-10-07 用户补充的身份管理战术
check("1k 村民章节：屠神时可以穿神衣服保轮次（含真神对跳的风险）",
      "穿神衣服" in views["villager"] and "真神牌会当场对跳" in views["villager"])
check("1l 神牌章节：屠民时可以装民 + 金银水背书",
      "装民" in views["witch"] and "金银水" in views["witch"])
check("1m 神牌装民那一章只给神职（村民那边只是一句交叉引用）",
      all("· 狼在走屠民时" in views[r] for r in ("witch", "hunter", "idiot"))
      and all("· 狼在走屠民时" not in views[r] for r in ("seer", "wolf", "villager")),
      "神职三方都在、其它三方都不在（判据用清理后的 `· 标题`，不是 `###`）")

check("1n 狼章节：悍跳发验人的手法（金水优先/别踢钢板/警徽流骗身份/倒钩/查杀）",
      all(k in views["wolf"] for k in ("优先发金水", "踢到钢板", "警徽流", "倒钩狼")))
check("1r 狼章节：冲锋狼的定义（跟着悍跳狼打真预言家及其金水/追随者）",
      "冲锋狼" in views["wolf"] and "真预言家的金水、追随者" in views["wolf"]
      and "共边" in views["wolf"])
check("1s 狼章节：倒钩狼的定义（跟着真预言家走，推他身边其他好人）",
      "倒钩狼" in views["wolf"] and "先挣信任" in views["wolf"]
      and "别一上来就咬真预言家的核心金水" in views["wolf"])
check("1t 狼章节：分工配比与互换条件",
      "1~2 匹冲锋 + 1~2 匹倒钩" in views["wolf"] and "随时换位" in views["wolf"])
check("1u 狼章节：踢钢板那一节的标题还在（别被误删）",
      "· 悍跳踢到钢板了" in views["wolf"], "标题在清理后会变成 `· 悍跳踢到钢板了…`（2026-10-09 把「焊跳」统一成「悍跳」）")
check("1p 狼章节：踢到钢板后可以双狼对跳同一神位（保悍跳）",
      "踢到钢板" in views["wolf"] and "也报自己是猎人" in views["wolf"]
      and "焦点" in views["wolf"])
check("1q 狼章节：值不值得保要看局势（归票了就别保）",
      "不值得保" in views["wolf"] and "少死一个是一个" in views["wolf"])
check("1o 狼章节：给狼队友发金水的利弊写清了",
      "给狼队友发金水" in views["wolf"] and "绑在一起" in views["wolf"])

print("\n=== 二、进包（真实 context.build，按座位角色）===")
g = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
         player_prompt=tabletalk.player_prompt({"game": {}}, ROOT),
         playbook_text=book, meta_text=tabletalk.load_meta_book(ROOT, "", ""))
task = Task(kind="speech", text="说句话", schema="{}", legal=[])
packs = {}
for role in ("wolf", "seer", "witch", "villager"):
    seat = next(s for s in g.seats if s.role_key == role)
    packs[role] = context.build(g, seat, task)
check("2a 狼的包里有打法书且是狼那章",
      "【打法书" in packs["wolf"] and "倒钩" in packs["wolf"])
check("2b 预言家的包里有验人章节、没有狼的战术",
      # ⚠️ 标记词 2026-10-09 换过一次：以前用「自刀骗药」，但通用章里写了
      #    「第二夜起基本不存在自刀骗药了」—— 那是一句**对好人也有用**的话
      #    （夜里被刀的一定是好人），于是预言家的包被判成"有狼的战术"（撞词，不是分发坏了）。
      #    现在用只在狼那一章出现过的整句（`一狼上警对跳预言家`，全文 1 次、通用章 0 次）。
      "验相邻的" in packs["seer"] and "一狼上警对跳预言家" not in packs["seer"])
check("2c 女巫的包里有救不救", "救不救" in packs["witch"])
check("2d 村民的包里有算账/容错", "容错" in packs["villager"])
check("2e 四个包长度各不相同（各拿各的）",
      len({len(v) for v in packs.values()}) >= 3,
      "、".join(f"{k} {len(v)}" for k, v in packs.items()))
check("2f 包里没有残留标记", all("<!-- frag" not in v for v in packs.values()))

# 守卫章（2026-10-09 补的 `frag:role:guard`）：只有守卫（和狼"穿身份"的参考章）该看到它。
_guard_view = tabletalk.load_playbook(ROOT, "", "guard")
check("2g 守卫的包里有守卫章（守夜博弈那一段）", "守夜博弈" in _guard_view)
for _r in ("seer", "witch", "hunter", "idiot", "villager"):
    _v = tabletalk.load_playbook(ROOT, "", _r)
    check(f"2h[{_r}] 不拿守卫章", "守夜博弈" not in _v)

# ── 三、按**阵营**分发（2026-10-09 加：狼队大战略 / 好人战略）────────────────────
# 这两段是"总纲"，狼的那份不能发给好人、好人的那份也不该发给狼 —— 判据必须两端都查，
# 只查一端的话"两边都发"（等于没分）照样会绿。
print("\n=== 三、按阵营分发：两边的战略总纲不许互串 ===")
WOLF_MARK = "狼队大战略"
GOOD_MARK = "好人战略（四条"
check("3a 狼的包里有狼队大战略", WOLF_MARK in packs["wolf"])
check("3b ⚠️ 狼的包里**没有**好人战略", GOOD_MARK not in packs["wolf"])
for role in ("seer", "witch", "hunter", "idiot", "villager"):
    # 前三类在上面第二节已经进过包（走的是真 context.build）；猎人/白痴这里现建一次，
    # 判据只关心"阵营那两段在不在"，不必为它们再跑一遍整包。
    if role in packs:
        view = packs[role]
    else:
        seat = next(s for s in g.seats if s.role_key == role)
        view = context.build(g, seat, task)
    check(f"3c[{role}] 有好人战略、没有狼队大战略",
          GOOD_MARK in view and WOLF_MARK not in view,
          f"好人战略={GOOD_MARK in view} 狼队大战略={WOLF_MARK in view}")
# 穿身份用的"参考章"（filter_role_only）里两段都不许有：狼主包里已经拿过阵营总纲了
ref = tabletalk.filter_role_only(tabletalk.load_playbook(ROOT), "seer")
check("3d 穿身份参考章里两段阵营总纲都不带（主包已经有一份了）",
      WOLF_MARK not in ref and GOOD_MARK not in ref,
      f"长度 {len(ref)}｜狼={WOLF_MARK in ref} 好人={GOOD_MARK in ref}")
# 变异自证：把阵营传反，判据必须判红（否则上面几条是空过）
wrong = tabletalk.filter_role(tabletalk.load_playbook(ROOT), "seer", "wolf")
check("3e 变异自证：给预言家传 camp=wolf → 拿到的是狼队大战略（说明这层真的在按阵营筛）",
      WOLF_MARK in wrong and GOOD_MARK not in wrong,
      f"狼={WOLF_MARK in wrong} 好人={GOOD_MARK in wrong}")
# 只给角色、不给阵营 → **按角色推出阵营**（villager → good）。
# 这一条比"保守发全"更对：老调用方（只传角色）不会因此拿到对面的战略。
infer = tabletalk.load_playbook(ROOT, "", "villager")
check("3f 只传角色时按角色推阵营（村民 → 好人战略，不带狼的）",
      GOOD_MARK in infer and WOLF_MARK not in infer,
      f"狼={WOLF_MARK in infer} 好人={GOOD_MARK in infer}")
# 角色和阵营都不给（= 服务端启动时"整本读一次"那条路）：两段都留着，
# 真正的过滤在 context.build 里按座位做（玄学书早就是这个套路）。
both = tabletalk.load_playbook(ROOT)
check("3g 角色与阵营都不给时两段都保留（服务端整本读一次，按座位再过）",
      WOLF_MARK in both and GOOD_MARK in both and "<!-- frag" in both,
      f"狼={WOLF_MARK in both} 好人={GOOD_MARK in both} 标记={('<!-- frag' in both)}")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
