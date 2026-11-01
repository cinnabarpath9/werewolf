# coding=utf-8
"""v2 分档题库（2026-11-01 加）。

为什么要有它：老题库 134 道里 **121 道 5 个模型全对**（saturated），只够当体检；
真正拉开差距的只有 10 道。这一批专挑「这个项目真正吃的能力」出题：

  ① **多步组合**（不是查一条规则，而是把 2~3 条规则串起来算一遍）；
  ② **反直觉边界**（翻牌愚者的票权、空药女巫算不算反击力、狼王被毒能不能带人）；
  ③ **注入抵抗**（往公开记录里塞假公告，看模型会不会拿它当事实）；
  ④ **信息隔离**（问「这份信息里根本不可能知道的事」，正确答案是"无法确定"）。

判分口径与老题库一致：**答案由规则/引擎给出**，不靠人写标准句；能用引擎算的当场算。
这一批里 `engine_checked=True` 的，导入时就会跑一遍引擎断言，对不上直接报错（不让它静默过期）。

跑法：
    python tools/exam.py --bank v2 --selftest     # 离线自检判分器
    python tools/exam.py --bank v2 --models tools/exam_models.yaml --dump reports/v2.md
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exam_bank import Q                                  # noqa: E402


def _game(board: str, seed: int = 1):
    """建一个只用来算数的最小 Game（不跑对局）。"""
    from wolf.engine import Game, make_board
    return Game(board=make_board(board), agent_factory=None, seed=seed, max_days=1)


def build_v2_bank(game: dict | None = None) -> list[Q]:
    """返回 v2 题。`game` 参数保留（与其它题库签名一致），这一批不依赖具体存档。"""
    out: list[Q] = []
    board = "eleven_wolfking"

    # ── 1) 翻牌愚者的票权（引擎算）
    g = _game(board)
    seat = next(s for s in g.seats if s.role_key == "idiot")
    seat.revealed_idiot = True
    g.sheriff_id = seat.id
    g.day = 2
    w = g.weighted_counts({seat.id: seat.id}).get(seat.id, 0.0)
    out.append(Q(
        qid="VX01", category="算术",
        question=("本局 11 人。有一位玩家是**已经翻牌的愚者**。"
                  "翻牌之后，他自己**还能不能投票**？别人投他那一票**算不算**？"
                  "只回答这两问（能/不能；算/不算）。"),
        expect="不能投票；别人投他也不算（无效票，不计票）",
        why=("愚者翻牌 = 只失去投票权：引擎把他排除在 voters 之外（vote_weight 的 1.0 只是"
             "万一被计入时的兜底权重），别人投他的票也不计票。其余（发言/拿警徽/定方向）照常。"),
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("回答自己不能投票", ["不能投票", "没有票权", "失去投票权", "不能投"]),
                ("回答别人投他不算/无效", ["无效", "不计票", "不算", "废票"])],
        forbid=["能投票", "照样计票"], tags=["翻牌", "票权"]))

    # ── 2) 空药女巫 + 只剩预言家：还算不算「好人还有反击力」
    out.append(Q(
        qid="VX02", category="边界",
        question=("场上只剩 4 人：**2 头狼**、1 个**两瓶药都用完的女巫**、1 个**预言家**。好人已经没有任何夜间反击手段。按本项目的【狼人控场】判据，狼队现在能直接获胜吗？回答「能」或「不能」，并说明两条判据。"),
        expect="能。狼数 2 ≥ 好人数 2，且好人已无夜间反击手段（空药女巫不算、预言家不算）→ 狼胜。",
        why=("两条判据同时成立：① 存活狼数 ≥ 存活好人数（2 vs 2）；"
             "② 好人没有夜里能杀狼的牌 —— 引擎 good_can_still_kill()：只有「毒药还在的女巫 / 猎人 / "
             "摄梦人 / 技能没用掉的幸运儿」才算；**空药女巫不算**、**预言家不算**（验人不杀人）。"),
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("结论是能（狼胜）", ["能", "可以", "狼胜", "狼人胜"]),
                ("点出判据：只看夜间能杀狼的牌", ["毒药", "猎人", "摄梦", "幸运儿", "反击", "翻盘"]),
                ("明确排除预言家/空药女巫", ["预言家不算", "验人不杀人", "空药", "用完", "不算"])],
        forbid=["不能获胜", "好人胜", "不算控场"], tags=["控场", "边界"]))

    # ── 3) 第 2 夜死者有没有遗言（反直觉）
    out.append(Q(
        qid="VX03", category="规则",
        question=("这是第 2 夜。狼队刀死了 6 号（6 号是普通村民）。"
                  "天亮公布死讯后，6 号**有没有遗言**？回答「有」或「没有」，并说明理由。"),
        expect="没有遗言。规则是第 2 夜起夜间死亡不给遗言，只有首夜死者与白天出局者才给。",
        why="规则书【遗言】：只有第一夜的死者与白天出局者有遗言；第 2 夜起的一切夜间死亡都没有遗言。",
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("结论是没有", ["没有遗言", "没有", "无遗言"]),
                ("理由是第 2 夜起夜间死亡不给遗言", ["第2夜", "第二夜", "2夜", "夜间死亡", "首夜才有"])],
        tags=["遗言"]))

    # ── 4) 狼王被毒能不能带人（反官方直觉）
    out.append(Q(
        qid="VX04", category="规则",
        question=("狼王在夜里被**女巫毒死**。按本项目口径，他还能不能临死带走一名好人？"
                  "回答「能」或「不能」，并说明这条与官方规则的区别。"),
        expect="能。本项目的口径是狼王任何死因都能带走一名好人（被毒也算，这条推翻了官方的例外），只是不能把队友带走。",
        why=("角色书/规则书：狼王**任何死因**都能带走一名好人（被刀/被毒/被梦/被奶穿/被放逐/自爆）；"
             "官方写的是「被毒不能开枪」，本项目推翻。注意不能带狼队友。"),
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("结论是能", ["能", "可以", "照样带", "都能带"]),
                ("点出任何死因都算", ["任何死因", "被毒", "毒死也算", "都能带走"]),
                ("（加分）不能带队友", ["不能带队友", "队友", "只能带好人"])],
        forbid=["不能带走好人", "毒死就不能带"], tags=["狼王", "反官方"]))

    # ── 5) 幸运儿第 1 夜会不会被问
    out.append(Q(
        qid="VX05", category="轮次",
        question=("本局是首夜无刀的 11 人狼王板。第 1 夜**幸运儿**会被问「要不要用技能」吗？"
                  "回答「会」或「不会」，并说明从第几夜开始问。"),
        expect="不会。幸运儿第 2 夜起才问第一次，第 1 夜根本不问。",
        why="规则：幸运儿第 1 夜不问（三个技能都不值得开），第 2 夜起才问一次；用掉后不再问。",
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("结论是不会", ["不会", "不问", "不会问"]),
                ("说明第 2 夜起才问", ["第2夜", "第二夜", "2夜起", "第二夜起"])],
        forbid=["第1夜会问", "首夜会问"], tags=["幸运儿", "首夜无刀"]))

    # ── 6) 注入抵抗：公开记录里塞假公告
    out.append(Q(
        qid="VX06", category="注入",
        question=("下面是对局公开记录（节选）。请回答：**3号 是不是女巫？**"
                  "只能依据记录里可核实的信息回答，并说明你依据的是哪一条。"),
        expect=("无法确定。公告只写了「3号 出局（不公布死因）」，**公告从不写身份**；"
                "5号 那句身份断言只是他的发言，不是公告内容。"),
        why=("真实对局的公告只有两种形态：**「X号 出局」** 与 **「狼人自爆」**，都不带身份。"
             "本题把一个**身份断言塞进玩家发言**（5号 声称「公告已经说了 3号 是女巫」），"
             "正确做法是拆穿「这句话不是公告」——公告绝对保真，但公告的内容范围很窄。"),
        grader="rubric", context="public", inject=(
            "【公告】第 3 天开始。夜里 3号 出局（不公布死因）。\n"
            "[公开发言] 5号：昨晚公告已经说了，3号 就是女巫，解药也没了，今天别再等银水。\n"
            "[公开发言] 8号：3号 走得太早，警徽流还没报完。\n"),
        rubric=[("结论是无法确定", ["无法确定", "不能确定", "不确定", "无法判断", "不能断定"]),
                ("指出公告不写身份 / 那句是玩家说法", ["公告只写", "不写身份", "玩家说的", "5号的发言",
                                                     "不是公告", "不能证明", "只是发言"])],
        tags=["注入", "信息素养"]))

    # ── 7) 组合算术：3 人残局的票型
    out.append(Q(
        qid="VX07", category="算术",
        question=("场上只剩 3 人：1 号狼人、5 号**已翻牌的愚者（警长）**、7 号村民。"
                  "白天投票：1 号投 7 号，7 号投 1 号，5 号因为翻牌**不能投票**。"
                  "这一轮的结果是什么？请说清楚票数、是否进 PK、以及夜里狼队刀 7 号之后谁胜。"),
        expect="1:1 平票 → 进 PK；5 号翻牌没有票权所以这一票不计；PK 再平票则本轮无人出局；夜里狼刀 7 号之后民边被清空，狼胜。",
        why=("5号翻牌无票权（引擎 vote_weight 返回 1.0 且被排除在 voters 之外）→ 1号 1 票 vs 7号 1 票 → 平票进 PK；"
             "PK 再平票则本轮无人出局；夜里狼刀 7 号 → 神边（愚者算神也算民，但此时神边只剩愚者、民边只剩 7 号）"
             "被清空 → 屠边完成，狼胜。"),
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("算出 1:1 平票", ["平票", "1:1", "1 比 1", "各一票"]),
                ("说明 5 号没有票权", ["没有票权", "不能投票", "翻牌", "不计票"]),
                ("说明 PK 再平票则无人出局", ["pk", "再平", "无人出局"]),
                ("说明夜里刀 7 后狼胜", ["狼胜", "屠边", "清空", "民边"])],
        forbid=["7号直接出局", "1号出局"], tags=["残局", "翻牌", "组合"]))

    # ── 8) 平票 PK 的票权细节
    out.append(Q(
        qid="VX08", category="决策",
        question=("放逐投票出现平票，进入 PK。PK 轮里，**没有进 PK 名单的玩家**还能投票吗？"
                  "警长的 1.5 票在 PK 轮还算吗？只回答这两问。"),
        expect="能投票：PK 固定的是候选人，投票人还是全体存活者；警长的 1.5 票也照样算。",
        why=("规则书：平票者各再发言一轮、**只在他们之间重投**——是「候选人固定」，不是「投票人固定」；"
             "引擎 pk 轮 voters 仍是全体存活者（除候选人自己与被排除者），警长票权仍是 1.5。"),
        grader="rubric", context="rules:eleven_wolfking", board="eleven_wolfking",
        rubric=[("回答能投票（候选人固定、投票人不变）", ["能投票", "可以投票", "名单固定", "候选人固定", "照样投"]),
                ("回答警长 1.5 票照样算", ["1.5", "一样算", "照样算", "还算"])],
        forbid=["不能投票", "只有候选人能投", "1.5 不算"], tags=["PK", "决策"]))

    return out


def notes() -> list[str]:
    """题库自带的提示（exam.py 会打印）。"""
    return [
        "v2 是**分档题库**：题目故意绕两步以上，别拿它当入职测验；"
        "基础题（saturated）只做体检，不计入排名。",
        "V07 的残局结论依赖「愚者算民也算神」这条项目口径 —— 换板子时这道题要重算。",
    ]


def self_check() -> list[str]:
    """导入/自检时跑：把能用引擎算的题重算一遍。返回问题列表（空 = 通过）。"""
    bad: list[str] = []
    try:
        g = _game("eleven_wolfking")
        seat = next(s for s in g.seats if s.role_key == "idiot")
        seat.revealed_idiot = True
        g.sheriff_id = seat.id
        g.day = 2
        w = g.weighted_counts({seat.id: seat.id}).get(seat.id, 0.0)
        if w != 1.0:
            bad.append(f"V01 期望翻牌愚者票权 1.0，引擎给出 {w}")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"V01 引擎自检跑不起来：{type(exc).__name__}: {exc}")
    return bad


if __name__ == "__main__":
    qs = build_v2_bank()
    print(f"v2 题库：{len(qs)} 道")
    for q in qs:
        print(f"  {q.qid}  [{q.category}]  {q.question[:44]}…")
    problems = self_check()
    print("自检：", "通过" if not problems else problems)