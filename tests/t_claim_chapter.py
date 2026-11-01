# coding=utf-8
"""「穿身份」两件套的验收（2026-10-08 用户反馈：悍跳狼用旁观者口吻谈自己声称的身份）。

钉死三件事：
  1. 打法书狼章里**真的有**「穿身份：跳什么就演什么」这一节，且核心纪律在里面
     （第一人称 + 禁止用「真预言家/后置位预言家」谈自己声称的身份）；
  2. 狼**已经报过某个身份**之后，下一次的包里有**那个身份的章节**（方案 1）；
  3. 好人**不会**因为报身份拿到别人的章节（隔离不许漏）。
"""
import sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context, tabletalk                      # noqa: E402
from wolf.context import Task                            # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import ROLES, make_board                 # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


book = tabletalk.load_playbook(ROOT)
wolf_view = tabletalk.load_playbook(ROOT, "", "wolf")
task = Task(kind="speech", text="说句话", schema="{}", legal=[])

print("=== 一、狼章里真有「穿身份」这一节 ===")
check("1a 新一节在狼章里", "穿身份：跳什么就演什么" in wolf_view)
check("1b 核心纪律：你跳的身份就是你的身份", "你跳的身份就是你的身份" in wolf_view)
check("1c 禁止旁观者口吻（真预言家/后置位预言家）",
      "真预言家怎么怎么样" in wolf_view and "后置位预言家如何如何" in wolf_view)
check("1d 给了正确的说法（那个悍跳的/对跳的假预言家）",
      "那个悍跳的 / 那个对跳的假预言家" in wolf_view)
check("1e 跳预言家的四个具体动作都在",
      all(k in wolf_view for k in ("报验人", "排警徽流", "给归票目标", "把对跳的打死")))
check("1f 演别的身份各有一小段（巫/猎/守/白）",
      all(k in wolf_view for k in ("跳女巫：", "跳猎人：", "跳守卫：", "跳愚者：")))
check("1g 一致性自查三问在里面",
      "我这一轮说自己是" in wolf_view and "只有旁观者或狼才知道" in wolf_view
      and "第三人称" in wolf_view)
check("1j 真局实证①：发过的金水不能反过来打（写成了铁律）",
      "你发过的金水就是好人" in wolf_view and "自我矛盾" in wolf_view
      and "牌根本不干净" in wolf_view and "可能被人带节奏" in wolf_view)
check("1k 真局实证②：声称的身份决定你能知道什么（不许绝对断言）",
      "你声称的身份，决定你能知道什么" in wolf_view and "超出验人能力" in wolf_view
      and "我推测" in wolf_view)
check("1l 自查第三问带上了「打自己发过金水的人」这一条",
      "打自己发过金水的人" in wolf_view)
check("1h 这一节只给狼（好人看不到）",
      all("穿身份：跳什么就演什么" not in tabletalk.load_playbook(ROOT, "", r)
          for r in ("seer", "witch", "hunter", "idiot", "villager")))
check("1i 半角引号仍然是 0", '"' not in book and "'" not in wolf_view,
      f'双引号 {book.count(chr(34))} 个')

print("\n=== 一之二、「附角色章」的取章函数不许把通用部分再塞一遍 ===")
# ⚠️ 这一组是回归钉：第一版直接把 filter_role 的结果当参考章附上去，
#    而 filter_role 含 frag:all（通用部分）—— 结果是同一段 5K 字进包两遍。
only = {r: tabletalk.filter_role_only(book, r) for r in
        ("seer", "witch", "hunter", "idiot", "villager", "guard")}
withall = {r: tabletalk.filter_role(book, r) for r in only}
check("1n 取章结果**不含**通用正文（否则就是从包里再塞一遍）",
      all("好人要算三件事" not in t for t in only.values()))
check("1o 取章结果**比带通用的版本短**（确实剥掉了）",
      all(len(only[r]) < len(withall[r]) for r in only),
      "、".join(f"{r} {len(only[r])}/{len(withall[r])}" for r in only))
check("1p 取章结果**带着本角色那一章**（不是剥空了）",
      "验相邻的" in only["seer"] and "救不救" in only["witch"]
      and "不能开枪" in only["hunter"] and "翻牌" in only["idiot"])
check("1q 取章结果**不含别的角色的章节**",
      all("跳女巫" not in t and "救不救" not in t for r, t in only.items() if r != "witch"))
check("1r 取章结果不含残留 frag 标记",
      all("<!-- frag" not in t for t in only.values()))
check("1s 神职的 frag:god 段照旧跟着走（女巫/猎人/愚者有、预言家没有）",
      "被排进「查验顺序」时" in only["witch"] and "被排进「查验顺序」时" in only["hunter"]
      and "被排进「查验顺序」时" in only["idiot"]
      and "被排进「查验顺序」时" not in only["seer"])

print("\n=== 二、还没报过身份 → 默认附「预言家」章（2026-10-08 按用户要求补的兜底）===")
g = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
         playbook_text=book)
wolf_seat = next(s for s in g.seats if s.role_key == "wolf")
before = context.build(g, wolf_seat, task)
# 兜底的理由：第 1 天悍跳预言家是狼最主流的打法，而第 1 天不竞选警长的狼**第一次开口时
# 台账里必然还没有 claim**（claim 就是这次发言的产物）—— 不兜这个默认，它最需要演的那一次
# 手上只有狼章、没东西可演。
check("2z 一次都没报过 → 默认附预言家章，而且抬头写明「还没报、只是备着」",
      "【你还没报身份 —— 这一章备着演预言家用】" in before
      and "验相邻的" in before and "你**还没有**向任何人报过身份" in before,
      f"位置 {before.find('【你还没报身份')}")
check("2y 兜底的默认章不会被说成「你已经报名了」",
      "你**已经报名**了这个身份" not in before)
check("2x 兜底之后狼自己的章照旧在（没有被顶掉）",
      "狼队纪律" in before and "自爆：什么时候值得爆" in before)
def ref_block(role_key: str, assumed: bool = False) -> str:
    """某个身份「被附上来」时那一整段应该长什么样（跟 context.build 的拼法一致）。

    ⚠️ 2026-10-09：打法书按**局面阶段**发（`frag:part:*`）之后，取章也必须带上
    同一个阶段 —— 否则逐字比对会全断（那次实测红了 14 条）。
    """
    return tabletalk.playbook_block(
        tabletalk.filter_role_only(book, role_key, tabletalk.phase_of(g)),
        title=tabletalk.claimed_role_title(ROLES[role_key].name, assumed=assumed))


check("2w 兜底的是**预言家**章、别的身份章一章都没混进来",
      ref_block("seer", assumed=True) in before
      and all(ref_block(r) not in before and ref_block(r, True) not in before
              for r in ("witch", "hunter", "idiot", "guard", "villager")),
      "判据是逐字对齐那一段（狼章本身也提到过「救不救」「不能开枪」这些词，"
      "拿关键词当判据会误伤）")
check("2v 好人没报身份时**不**附任何参考章（隔离没漏）",
      all("【你还没报身份" not in context.build(g, s, task)
          for s in g.seats if s.role_key != "wolf"),
      "所有非狼座位都检查过")

print("\n=== 二之二、报过身份之后，附的是**它报的那个**身份 ===")
# 模拟引擎在它报过身份之后写下的台账条目（文案与 engine.py 完全一致）
for claim, expect, probe in (("预言家", "seer", "验相邻的"),
                             ("女巫", "witch", "救不救"),
                             ("猎人", "hunter", "不能开枪"),
                             ("愚者", "idiot", "翻牌")):
    g.ledger.add(1, "day", "claim", f"{wolf_seat.id}号 声称自己是【{claim}】。",
                 seat=wolf_seat.id, data={"claim": claim})
    got = context.build(g, wolf_seat, task)
    check(f"2b 报【{claim}】之后包里附上了 {expect} 那一章",
          f"【你声称的身份：{claim}" in got and probe in got,
          f"位置 {got.find('【你声称的身份')}")
    check(f"2c 附上的章节里带了纪律（手法按狼章来 + 编好的说法）",
          "出手手法仍按你自己的狼章来" in got and "自己编好的那套说法" in got)
    check(f"2d 报【{claim}】不影响它自己的狼章",
          "狼队纪律" in got and "自爆：什么时候值得爆" in got)
    # ⚠️ 回归钉：附上来的参考章**不许**把通用部分再带一遍（同一段 5K 字进包两遍）。
    #    判据是**内容**：包里那一段必须跟"这一章 + 抬头"逐字一致 —— 直接对字符串，
    #    不用长度增量（增量会被"默认预言家章换成了它报的那一章"这层变化干扰），
    #    真出了重复（多 6000+ 字）对齐立刻就断。
    want = tabletalk.playbook_block(
        tabletalk.filter_role_only(book, expect, tabletalk.phase_of(g)),
        title=tabletalk.claimed_role_title(ROLES[expect].name))
    check(f"2f 包里 {claim} 那一章是逐字给过来的（没有把通用部分再塞一遍）",
          want in got, f"该段 {len(want)} 字，包长 {len(got)}")
    # 归一化：认不出/否定句不算报名
    check(f"2e 认不出来的身份文本不会硬套章节（{claim} 之后）",
          context._norm_role_key("不是预言家") == "" and context._norm_role_key("未知") == "")

print("\n=== 三、好人报身份**不会**拿到别人的章节（隔离）===")
for role_key in ("seer", "witch", "villager"):
    seat = next(s for s in g.seats if s.role_key == role_key)
    if seat.id == wolf_seat.id:
        continue
    g.ledger.add(1, "day", "claim", f"{seat.id}号 声称自己是【猎人】。",
                 seat=seat.id, data={"claim": "猎人"})
    pack = context.build(g, seat, task)
    check(f"3a {role_key} 报猎人之后**没有**拿到猎人章",
          "【你声称的身份" not in pack, f"包长 {len(pack)}")

print("\n=== 四、读的是**最近一次**声称（改口换衣服）===")
g.ledger.add(1, "day", "claim", f"{wolf_seat.id}号 声称自己是【守卫】。",
             seat=wolf_seat.id, data={"claim": "守卫"})
got = context.build(g, wolf_seat, task)
check("4a 改口之后附的是新衣服（守卫），不是旧衣服（预言家）",
      "【你声称的身份：守卫" in got and "【你声称的身份：预言家" not in got)
check("4b 台账顺序就是时间顺序（最后一条 claim 生效）",
      context.claimed_role_key(g, wolf_seat.id) == "guard")
check("4c 队友的声称不会被算到自己头上",
      all(context.claimed_role_key(g, s.id) in ("", "guard", "hunter")
          for s in g.seats if s.id != wolf_seat.id))

print("\n=== 四之二、报了但**认不出**的身份：抬头不许说「你还没报过」（P3-1）===")
# 真局里最常见的几种写法（都不在别名表里）：我是好人 / 好人 / 猎魔人 / 骑士 / 丘比特 / 暂无
# ⚠️ 2026-10-09：样例里的「守墓人」已经从「不存在的角色」变成**本项目的真角色**（民牌），
#    所以换成仍然不存在的「猎魔人」—— 否则这个用例测的就不是「认不出的身份」了。
# ⚠️ 这时候引擎**已经**写了公开的 claim 条目 —— 同一份包里既有
#    「N号 声称自己是【我是好人】。」又有「你**还没有**向任何人报过身份」= 自相矛盾。
# ⚠️ 用**干净的一局**：上面第三/四节已经往 `g` 的台账里塞过 claim 了，
#    拿它来验"认不出的报名"会被历史条目干扰（实测：这个座位的最近一条成「守卫」了）。
g_u = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
           playbook_text=book)
u_wolf = next(s for s in g_u.seats if s.role_key == "wolf")
for weird in ("我是好人", "好人", "猎魔人", "骑士", "丘比特", "暂无"):
    g_u.ledger.add(1, "day", "claim", f"{u_wolf.id}号 声称自己是【{weird}】。",
                   seat=u_wolf.id, data={"claim": weird})
    pack = context.build(g_u, u_wolf, task)
    check(f"4d 报【{weird}】之后，包里不许说「你还没有向任何人报过身份」",
          "还没有**向任何人报过身份" not in pack
          and "【你还没报身份" not in pack,
          f"位置 {pack.find('【你报的身份')}")
    check(f"4e 报【{weird}】之后，抬头如实转述它报的原文",
          f"【你报的身份：{weird} —— 这一章备着参考】" in pack)
    check(f"4f 报【{weird}】之后仍然给了可演的预言家章正文（不是空手）",
          "拿到警徽之后：报「警徽流」" in pack)
    check(f"4g 报【{weird}】之后不许说它「已经报名了这个身份」（坐实口径）",
          "你**已经报名**了这个身份" not in pack)
# 最近一次报的是「暂无」→ 原文就是它（不是最早那个「我是好人」）
check("4h 认不出的报名算「报过」（_has_claim 按台账条目判，不按归一化结果）",
      context._has_claim(g_u, u_wolf.id) is True
      and context._claim_of(g_u, u_wolf.id) == ""
      and context._raw_claim_of(g_u, u_wolf.id) == "暂无",
      f"raw={context._raw_claim_of(g_u, u_wolf.id)!r}")
# 改口：又报了一个**认得出**的身份 → 立刻回到"已报名"版抬头（不能粘在"报的身份"版上）
g_u.ledger.add(1, "day", "claim", f"{u_wolf.id}号 声称自己是【女巫】。",
               seat=u_wolf.id, data={"claim": "女巫"})
rec = context.build(g_u, u_wolf, task)
check("4h2 从「认不出」改口成「女巫」→ 抬头切回「你声称的身份：女巫」",
      "【你声称的身份：女巫" in rec and "【你报的身份" not in rec)
# ⚠️ 但**一次都没报过**时，兜底抬头照旧（这条是既有契约，别被上面的改动弄坏）
g_fresh = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
               playbook_text=book)
fresh_wolf = next(s for s in g_fresh.seats if s.role_key == "wolf")
fresh_pack = context.build(g_fresh, fresh_wolf, task)
check("4i 一次都没报过 → 仍然是「你还没报身份」版兜底抬头（老契约没被改坏）",
      "【你还没报身份 —— 这一章备着演预言家用】" in fresh_pack
      and "你**还没有**向任何人报过身份" in fresh_pack
      and "【你报的身份" not in fresh_pack)

print("\n=== 四之三、照旧报**预言家**（认得出的）行为一字不变（P3-1 不许误伤）===")
g_ok = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
            playbook_text=book)
ok_wolf = next(s for s in g_ok.seats if s.role_key == "wolf")
g_ok.ledger.add(1, "day", "claim", f"{ok_wolf.id}号 声称自己是【预言家】。",
                seat=ok_wolf.id, data={"claim": "预言家"})
ok_pack = context.build(g_ok, ok_wolf, task)
check("4j 报【预言家】→ 照旧是「你声称的身份：预言家」版抬头",
      "【你声称的身份：预言家" in ok_pack and "【你报的身份" not in ok_pack
      and "你**已经报名**了这个身份" in ok_pack)
check("4k 报【预言家】→ 逐字等于「已报名」版那一段（一字未改）",
      tabletalk.playbook_block(
          tabletalk.filter_role_only(book, "seer", tabletalk.phase_of(g)),
          title=tabletalk.claimed_role_title("预言家")) in ok_pack)

print("\n=== 四之四、狼声称「狼人 / 狼」：不附自己那一章（P3-3）===")
# 真缺陷：`claimed == seat.role_key` 以前没判，于是狼章被当成"要演的衣服"又附了一遍，
# 同一段 6294 字进包两遍（既白烧 token，又让模型读成两份互相冲突的书）。
g_w = Game(board=make_board("eleven_standard"), agent_factory=None, seed=4, max_days=1,
           playbook_text=book)
wolf2 = next(s for s in g_w.seats if s.role_key == "wolf")
base_pack = context.build(g_w, wolf2, task)           # 没报过 → 附兜底预言家章
check("4l0 基准包是「还没报过 → 兜底预言家章」那一版",
      "【你还没报身份 —— 这一章备着演预言家用】" in base_pack,
      f"基准包长 {len(base_pack)}")
for self_claim in ("狼人", "狼", "wolf"):
    g_w.ledger.add(1, "day", "claim", f"{wolf2.id}号 声称自己是【{self_claim}】。",
                   seat=wolf2.id, data={"claim": self_claim})
    pack = context.build(g_w, wolf2, task)
    # 报了自己真正的角色 = 不用再演别人 → 既不附兜底预言家章、也不附狼章第二遍
    check(f"4l 狼报【{self_claim}】→ 包里比基准**更短**（不把自己那一章再附一遍）",
          len(pack) < len(base_pack),
          f"{len(base_pack)} → {len(pack)} 字（差 {len(pack) - len(base_pack)}）")
    check(f"4m 狼报【{self_claim}】→ 没有多出任何参考章抬头",
          "【你声称的身份" not in pack and "【你还没报身份" not in pack
          and "【你报的身份" not in pack)
    # 判据用 **core** 小节（狼队纪律 / 自爆）：`穿身份`、`倒钩狼` 现在是 mid 内容，
    # 第 1 天本来就不发 —— 拿它们当"狼章还在不在"的标记会误伤（2026-10-09 阶段切分时实测）。
    check(f"4n 狼报【{self_claim}】→ 它自己那一章照旧在（不是把狼章删了）",
          "狼队纪律" in pack and "自爆：什么时候值得爆" in pack)
# 对照：认得出的**别的**身份照旧附（别把"不附"做成一刀切）。
# 从"最近一条是狼人"的状态改口成村民 → 包里应当**比同状态的狼人版更长**。
pack_wolfish = context.build(g_w, wolf2, task)
g_w.ledger.add(1, "day", "claim", f"{wolf2.id}号 声称自己是【村民】。",
               seat=wolf2.id, data={"claim": "村民"})
vill_pack = context.build(g_w, wolf2, task)
check("4o 对照：狼报【村民】照旧附村民章（不是把所有附章都关了）",
      "【你声称的身份：村民" in vill_pack and len(vill_pack) > len(pack_wolfish),
      f"{len(pack_wolfish)} → {len(vill_pack)} 字")

print("\n=== 五、包结构没被破坏 ===")
check("5a 包里没有残留 frag 标记", "<!-- frag" not in got)
check("5b 附上的那一章在【你声称的身份】块里、并不重复塞【打法书】标题",
      got.count("【打法书") == 1, f"【打法书 出现 {got.count('【打法书')} 次")
check("5c 【输出格式】块还在，参考章接在它后面（这一块是包的尾巴）",
      "【输出格式】" in got and got.find("【输出格式】") < got.find("【你声称的身份")
      and "输出格式块仍然在最后" not in got,
      f"末 40 字：{got[-40:].replace(chr(10), ' ')}")
check("5d 没有第一人称冒充（块头写的是「你声称的身份」而不是「你是猎人」）",
      "【你的身份】" in got and got.count("【你的身份】") == 1)

print("\n=== 五之二、配置开关 game.attach_claimed_chapter（默认开）===")
import os                                                # noqa: E402
import tempfile                                          # noqa: E402

cfg_text = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8").read()
check("5e config.yaml 里有这个开关、默认 true",
      "attach_claimed_chapter: true" in cfg_text)
check("5f 开关走默认路径读到的是 True", context.attach_claimed_chapter() is True)
check("5f2 真从 config.yaml 里读出来也是 True",
      context._config_flag(os.path.join(ROOT, "config.yaml"),
                           "attach_claimed_chapter", False) is True)
# ⚠️ 真写几个临时配置验读取逻辑（不是自己验自己）：字符串 "false" 的坑必须堵住
with tempfile.TemporaryDirectory() as tmpd:
    def _cfg_with(value: str) -> str:
        p = os.path.join(tmpd, "c.yaml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"game:\n  attach_claimed_chapter: {value}\n")
        return p
    check("5f3 配置写 false / no / off / 0 都读成关",
          all(context._config_flag(_cfg_with(v), "attach_claimed_chapter", True) is False
              for v in ("false", "no", "off", "0", '"false"', "'no'")))
    check("5f4 配置写 true / yes / on / 1 都读成开",
          all(context._config_flag(_cfg_with(v), "attach_claimed_chapter", False) is True
              for v in ("true", "yes", "on", "1", '"true"')))
    p = os.path.join(tmpd, "none.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("game:\n  board: eleven_standard\n")
    check("5f5 配置里根本没写这个键 → 用默认值（缺省 = 开）",
          context._config_flag(p, "attach_claimed_chapter", True) is True)
    badp = os.path.join(tmpd, "bad.yaml")
    with open(badp, "w", encoding="utf-8") as fh:
        fh.write("game: [这不是一个映射\n")
    check("5f6 配置文件不存在 / YAML 坏掉 → 用默认值，不抛异常",
          context._config_flag(os.path.join(tmpd, "nope.yaml"),
                               "attach_claimed_chapter", True) is True
          and context._config_flag(badp, "attach_claimed_chapter", True) is True)
# 关掉：清缓存 → 让读取逻辑返回 False → 整块不许出现
_saved_cfg = list(context._CFG_CACHE)
context._CFG_CACHE.clear()
context._CFG_CACHE.append(False)
off = context.build(g, wolf_seat, task)
check("5g 关掉开关 → 狼的包里**什么参考章都不附**（狼章照旧在）",
      "【你声称的身份" not in off and "【你还没报身份" not in off
      and "【打法书" in off and "狼队纪律" in off,
      f"包长 {len(off)}（开着时 {len(got)}）")
check("5h 关掉之后狼只剩「穿身份」那一节的要点可参考（别人的章节一点都没有）",
      "狼队纪律" in off
      and "拿到警徽之后：报「警徽流」" not in off      # 预言家章独有
      and "毒药什么时候开" not in off                  # 女巫章独有
      and "枪错会导致好人直接减员" not in off,          # 猎人章独有
      "判据用各章独有的句子：狼章本身也提到过「救不救」「不能开枪」这些词")
check("5h2 关掉之后好人也照旧不附（开关不影响隔离）",
      all("【你声称的身份" not in context.build(g, s, task)
          for s in g.seats if s.role_key != "wolf"))
context._CFG_CACHE[:] = _saved_cfg          # 还原，别把状态漏给后面的用例
check("5i 还原之后又恢复附章", "【你声称的身份" in context.build(g, wolf_seat, task))

print("\n=== 五之三、中英文「是/否」表跟引擎共用一张（P3-5）===")
# 真缺陷：`context._as_bool` 的假值只有 false/no/off/0/空 —— 写「关」是**非空字符串** → True，
# 于是 `game.attach_claimed_chapter: 关` = "看起来关了其实还开着"。
# 修法：跟 engine 的 `truthy` 共用 `wolf/words.py` 那张表。
for word in ("关", "关闭", "否", "false", "no", "off", "0", "n", "False", "关闭。"):
    check(f"5j2 配置写 {word!r} → 读成关",
          context._as_bool(word, True) is False,
          f"_as_bool={context._as_bool(word, True)}")
for word in ("开", "开启", "是", "true", "yes", "on", "1", "y", "True"):
    check(f"5j3 配置写 {word!r} → 读成开",
          context._as_bool(word, False) is True,
          f"_as_bool={context._as_bool(word, False)}")
check("5j4 空串 / 没写 → 用默认值（不是「关」）",
      context._as_bool("", True) is True and context._as_bool(None, True) is True
      and context._as_bool("", False) is False)
check("5j5 真布尔值照旧", context._as_bool(True, False) is True
      and context._as_bool(False, True) is False)
# ⚠️ 关键：两张表**必须是同一张**，不然哪天只改一边又会跑偏
from wolf import engine as engine_mod                     # noqa: E402
check("5j6 context 与 engine 共用同一张「是/否」表（不是各抄一份）",
      context.words.TRUE_WORDS is engine_mod.TRUE_WORDS
      and context.words.FALSE_WORDS is engine_mod.FALSE_WORDS
      and context.words.TRUE_WORDS is context.words.truthy.__globals__["TRUE_WORDS"])
check("5j7 引擎的 truthy 也认中文（两边口径一致）",
      engine_mod.truthy("关", True) is False and engine_mod.truthy("开启", False) is True
      and engine_mod.truthy("否", True) is False and engine_mod.truthy("是", False) is True)
# 真跑一遍：配置里写「关」，走**完整读取链路**（_config_flag，不是直接调 _as_bool）
with tempfile.TemporaryDirectory() as tmpd2:
    def _cfg_cn(value: str) -> str:
        p = os.path.join(tmpd2, "cn.yaml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"game:\n  attach_claimed_chapter: {value}\n")
        return p
    check("5j8 配置文件里写「关 / 关闭」→ _config_flag 真读成关",
          all(context._config_flag(_cfg_cn(v), "attach_claimed_chapter", True) is False
              for v in ("关", "关闭", '"关"', "'关闭'")),
          str([context._config_flag(_cfg_cn(v), "attach_claimed_chapter", True)
               for v in ("关", "关闭")]))
    check("5j9 配置文件里写「开 / 开启」→ _config_flag 真读成开",
          all(context._config_flag(_cfg_cn(v), "attach_claimed_chapter", False) is True
              for v in ("开", "开启", '"开"')))

print("\n=== 五之四、开关必须跟着 --config 走（P3-6）===")
# 真缺陷：`attach_claimed_chapter()` 只探「wolf/ 旁边」+「当前工作目录」两份 config.yaml，
# 而 `run_game.py --config /tmp/x.yaml` 指的要是别处，这个开关就**绕过 --config 了**。
with tempfile.TemporaryDirectory() as tmpd3:
    other = os.path.join(tmpd3, "other.yaml")
    with open(other, "w", encoding="utf-8") as fh:
        fh.write("game:\n  attach_claimed_chapter: false\n")
    # ⚠️ 这时 `attach_claimed_chapter()` 早就被上面几个用例读过、缓存里有值（True）。
    #    所以下面这条同时钉住"set_config_path 必须清缓存"。
    context.set_config_path(other)
    try:
        check("5k1 --config 指向的那份里写了 false → 开关真的读成关（没有被默认路径顶掉）",
              context.attach_claimed_chapter() is False,
              f"attach_claimed_chapter()={context.attach_claimed_chapter()}")
        pack_off = context.build(g, wolf_seat, task)
        check("5k2 于是狼的包里真的**不附**参考章",
              "【你声称的身份" not in pack_off and "【你还没报身份" not in pack_off
              and "【你报的身份" not in pack_off and "狼队纪律" in pack_off,
              f"包长 {len(pack_off)}")
        # 反向：把 --config 指回真 config.yaml（开关是 true）→ 又要附上。
        # ⚠️ 判据用 `g_fresh`（**一次都没报过**的狼），不用 `g`：
        #    上面几个用例把 `g` 那个狼座位的最近一条 claim 改成了「守卫」，
        #    而它自己的角色就是守卫 → P3-3 生效，本来就不该附章（拿它验开关会误判）。
        context.set_config_path(os.path.join(ROOT, "config.yaml"))
        check("5k3 --config 指回真配置（true）→ 又恢复附章",
              context.attach_claimed_chapter() is True
              and "【你还没报身份 —— 这一章备着演预言家用】" in context.build(
                  g_fresh, fresh_wolf, task))
        # 换路径必须清缓存：直接验"缓存不被旧值粘住"
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("game:\n  attach_claimed_chapter: true\n")
        context.set_config_path(other)
        check("5k4 改过文件内容 + 重新 set_config_path → 读到新值（缓存真的清了）",
              context.attach_claimed_chapter() is True)
    finally:
        context.set_config_path("")           # 恢复默认查找，别把状态漏给后面的用例
        context.reset_config_cache()
    check("5k5 传空串 = 恢复默认查找（读到真 config.yaml 的 true）",
          context.attach_claimed_chapter() is True)
    check("5k6 run_game.py 读了 --config 之后真的调了 set_config_path",
          "context.set_config_path(args.config)" in
          open(os.path.join(ROOT, "run_game.py"), encoding="utf-8").read())


# ── 六、整局跑一遍（走真实引擎链路：发言 → 写 claim 台账 → 下一轮进包）───────
# 上面的二/四节是**手工往台账里塞条目**验的过滤逻辑；这一节验的是"引擎自己写的那条
# claim 真的能被读到" —— 两件事分开验，否则哪天引擎改了台账文案，过滤逻辑还是绿的。
print("\n=== 六、整局真跑：引擎写的 claim 被读到 ===")
import asyncio                                          # noqa: E402
import random                                           # noqa: E402

from wolf.agents import MockAgent                       # noqa: E402


async def e2e() -> tuple[dict, list, object]:
    g2 = Game(board=make_board("eleven_standard"), human_seat=0, seed=7, max_days=2,
              decision_first=True, sheriff=True, playbook_text=book)
    captured: dict[int, list[str]] = {}
    claims: list[tuple[int, str]] = []

    class Claiming(MockAgent):
        """所有狼都在发言里报【预言家】—— 模拟"悍跳狼"这条真实链路。"""

        async def ask(self, packet, task):
            reply = await super().ask(packet, task)
            if task.kind in ("speech_plan", "speech") and self.seat.role_key == "wolf":
                captured.setdefault(self.seat.id, []).append(packet)
                claims.append((self.seat.id, "预言家"))
                reply = dict(reply)
                reply["claim"] = "预言家"
            return reply

    rng = random.Random(7)
    g2.agent_factory = lambda seat: Claiming(seat, rng)
    await g2.run()
    return captured, claims, g2


captured, claims, g2 = asyncio.run(e2e())
check("6a 确实有狼在发言里报了预言家（台账外的旁证）", bool(claims),
      f"{len(claims)} 次")
wolf_claims = []
for e in g2.ledger.entries:
    if e.kind == "claim" and (e.data or {}).get("claim"):
        seat_role = g2.seat_by_id(e.seat).role_key if e.seat else ""
        wolf_claims.append((e.seat, e.data["claim"], seat_role))
check("6b 引擎把狼的声称写进了 claim 台账（文案与 context 读的一致）",
      any(r == "wolf" and c == "预言家" for _, c, r in wolf_claims),
      str(wolf_claims[:4]))
first_ever = True
seen_attached = False
for seat_id, packets in captured.items():
    for pk in packets:
        # 报过之后 → 【你声称的身份：预言家…】；还没报过（第一次开口）→ 兜底的
        # 【你还没报身份 —— 这一章备着演预言家用】。两种都得给到预言家章正文。
        claimed_hdr = "【你声称的身份：预言家" in pk
        assumed_hdr = "【你还没报身份 —— 这一章备着演预言家用】" in pk
        if first_ever:
            # ⚠️ 2026-10-08 用户要求补的兜底：第一次开口时它**还没报过**身份
            #    （claim 是这次发言之后才写台账的），但**必须**已经有预言家章可演，
            #    否则"第 1 天悍跳预言家"这个最主流的打法恰好是空手的。
            check("6c 第一次开口（还没报过身份）也拿到了预言家章，且抬头写明是兜底",
                  assumed_hdr and not claimed_hdr
                  and "拿到警徽之后：报「警徽流」" in pk,
                  f"兜底抬头={assumed_hdr} 已报抬头={claimed_hdr}")
            first_ever = False
        if claimed_hdr:
            seen_attached = True
            # 用预言家章里的**嵌套小标题**做判据：这一行只可能来自预言家那一章，
            # 而且它证明附上来的是**正文**、不只是加了个抬头。
            check("6d 报过之后附的确实是预言家那一章（不是只加了个抬头）",
                  "拿到警徽之后：报「警徽流」" in pk)
            break
check("6e 报过身份之后狼拿到的是「已报名」版抬头", seen_attached,
      f"狼 {sorted(captured)} 各 {[len(v) for v in captured.values()]} 次发言")
check("6f 整局里好人的包一次都没被附过别人的章节",
      all("【你声称的身份" not in str(c.get("packet") or "")
          and "【你还没报身份" not in str(c.get("packet") or "")
          for c in g2.transcript
          if (c.get("seat") is not None
              and g2.seat_by_id(c["seat"]).role_key != "wolf")
          and c.get("packet")),
      f"transcript {len(g2.transcript)} 条")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n, _, d in bad:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
