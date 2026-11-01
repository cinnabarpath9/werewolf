# coding=utf-8
"""高难度笔试题库（笔试 2.0）：W / X / Y / Z / S / V 六类，答案**全部由引擎现算**。

为什么要有它（规格书 `docs/考题设计.md`）：老题库 60 道题分不出模型 ——
规则题**每题都把规则原文当上下文附上**（只有「读完就能答」的二选一）、检索/算术题从一份
演示存档里数数、隔离题一律可以答「无法确定」。要分档，题目必须
①**多步**（局面 → 账 → 结论）、②**答案唯一且由引擎现算**、③**错误选项对模型有吸引力**。

## 开卷 / 闭卷（2026-10-11 作者补的关键口径，规格书 §1.5）
**本项目特设的规则**（愚者双算、狼王那一口、首夜无刀、三张技能民牌、锁定上警、连死取消、
可连咒、翻牌愚者无效票、13 人场构成）**一律开卷** —— 游戏里本来就不是闭卷：
`game.rules_mode: room`（默认）把**按本局板子组装好的规则全文**当公开公告写进台账，
每个座位每次调用都看得到。逼模型背这些既没意义、又**必然答错**（本项目把官方是神牌的
守墓人/养鸦人/幸运儿**降成了民牌**，联网搜到的官方答案反而是错的）。
所以本库**统一**把 `context` 写成 `rules:<本题那一局的板子>`：模型能查，也必须查本局那一份。
⚠️ 统一用 `rules:` 还有第二个好处：`context="public"` 需要一份 `games/` 存档才解析得出，
而 `rules:` 不需要 —— 换题库不存在「题被静默丢掉」的风险（`exam.py` 的 `group_questions`
会把上下文取不到正文的题直接跳过）。
闭卷只允许用在**纯官方机制**上（票数、平票 PK、遗言时机、警徽 1.5、概率数学）。
`Q.book` 标的就是这件事：`open` = 答案依赖特设机制（必须开卷）；`closed` = 只用官方机制。
`Q.why` 里**逐题**写明「开卷理由：…」或「闭卷理由：…」。

## 六类题（规格书 §1 / §1.5）
· **W 轮次与胜负账**：控场判定、屠边还差几张、必须放逐谁才能赢、狼队一刀定胜负 ——
  全部**枚举**候选动作 + 引擎判胜，**并列最优的直接弃掉**（不出有歧义的题）。
· **X 新口径边界**：首夜无刀的调用面、狼王五种死因下那一口、结算次序、锁定上警、
  连死取消、可连咒、翻牌愚者无效票 —— 干扰项一律来自**官方/旧口径**。
· **Y 票型与时间线对账**：真跑两段半局（剧本代理、零成本），答案**只从公开条目里推**
  （谁是言行矛盾的、谁不可能是预言家、票算到谁头上）。
· **Z 抗崩决策**：枚举 + 明确收益函数 → 最优解集合，`grader="legal"`，
  `why` 里给出完整排序（含「次优解是什么、为什么次优」）。
· **S 13 人场专属**：构成/屠边人头/狼坑/警徽 1.5 + **位置学基线按 13 座现算**
  （枚举全部类型排列；**11 座的 36.4% 在这里不许出现**）。
· **V 规则检索与应用**：开卷，**必须翻对「本局那一份」规则公告**（同一句话在不同板子上
  答案不同），专门测「会不会查" + "查到的是不是本局那一份」。

⚠️ 纪律：这个文件只**出题 + 跑引擎**（离线、零成本、不联网、不花钱）。

跑法（只出题、看答案怎么来的）：
    python tools/exam_hard.py          # 自检：打印每类题量、每题期望答案与 why，退出码 0/1
    python tests/check_exam_hard.py     # 闸门（≥8 项）
"""
from __future__ import annotations

import itertools
import math
import os
import random
import re
import sys
from math import comb

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

#: 复用老题库的 `Q` 数据结构与两个现成工具（`_run_blocking` 解决「已经在事件循环里」的坑）。
from exam_bank import Q, _run_blocking, default_board, rules_text_for   # noqa: E402
from wolf.engine import Game                                          # noqa: E402
from wolf.retired_boards import RETIRED_BOARDS, retired_board          # noqa: E402
from wolf.roles import BoardDef, role_name, validate_board            # noqa: E402
from wolf.roles import make_board as _product_make_board              # noqa: E402


def make_board(name, custom: dict | None = None):
    """题库用的板子解析：退役的 11 人板走测试副本（卷面按它们写死，不随产品换板而失效）。"""
    if isinstance(name, str) and not (custom and name in custom) and name in RETIRED_BOARDS:
        return retired_board(name)
    return _product_make_board(name, custom)

# --------------------------------------------------------------------------- #
# 0) 板子与座位表（**显式钉死座位↔身份**，不靠发牌顺序）
# --------------------------------------------------------------------------- #

#: 本题库 W/X/Y/Z 用的默认板子（= `config.yaml` 的 `game.board`）。
HARD_BOARD = "eleven_wolfking"

#: 狼王版的座位表（3 狼 + 狼王 + 预/女/摄梦/愚者 + 守墓人/养鸦人/幸运儿）。
KING_SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf_king",
              5: "seer", 6: "witch", 7: "dreamer", 8: "idiot",
              9: "gravekeeper", 10: "crow", 11: "lucky"}
#: 预女梦愚版（4 平狼，没有狼王）—— Y 类用它：**这张板子没有「首夜无刀」**，
#: 所以「第 1 夜有人出局」与板子规则自洽（狼王版第一夜不可能死人）。
DREAM_BOARD = "eleven_dream"
DREAM_SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf",
               5: "seer", 6: "witch", 7: "dreamer", 8: "idiot",
               9: "gravekeeper", 10: "crow", 11: "lucky"}
#: 守卫版（4 狼 + 预女猎 + 守卫 + 3 民）—— V 类对照用。
ELEVEN_BOARD = "eleven"
ELEVEN_SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf",
                5: "seer", 6: "witch", 7: "hunter", 8: "guard",
                9: "villager", 10: "villager", 11: "villager"}
#: 标准版（4 狼 + 预女猎 + 愚者 + 3 民）—— V 类对照用。
STANDARD_BOARD = "eleven_standard"
STANDARD_SEATS = {1: "wolf", 2: "wolf", 3: "wolf", 4: "wolf",
                  5: "seer", 6: "witch", 7: "hunter", 8: "idiot",
                  9: "villager", 10: "villager", 11: "villager"}

#: 13 人场（S 类）：4 狼 + 狼王 + 预言家/女巫/摄梦人/愚者/猎人 + 守墓人/养鸦人/幸运儿。
#: ⚠️ 这张板子目前**没有进 `wolf/roles.py` 的 BUILTIN_BOARDS**（本会话不许改那个文件），
#:    所以它只在题库里以 `BoardDef` 形式存在，并且由 `validate_board` 校验通过。
THIRTEEN_ROLES = ["wolf", "wolf", "wolf", "wolf", "wolf_king",
                  "seer", "witch", "dreamer", "idiot", "hunter",
                  "gravekeeper", "crow", "lucky"]
THIRTEEN_SEATS = {i + 1: key for i, key in enumerate(THIRTEEN_ROLES)}


def thirteen_board() -> BoardDef:
    """13 人场的板子定义（S 类专用）。坏板子这里就会抛出来，不许静默开局。"""
    board = BoardDef(name="thirteen_hard", roles=list(THIRTEEN_ROLES),
                     win_rule="sides",
                     desc="13 人：4 狼 + 狼王 + 预/女/摄梦/愚者/猎人 + 守墓人/养鸦人/幸运儿")
    issues = validate_board(board)
    if issues:
        raise ValueError("13 人板子没通过 validate_board：" + "；".join(issues))
    return board


#: 闸门用的「引擎现算登记表」：只有**真的跑过引擎校验**、且校验通过的题号才会被登记。
VERIFIED: set[str] = set()
#: 出题过程中发现的问题（引擎行为与预期不一致 / 某道题因为并列最优被弃掉…）。
NOTES: list[str] = []


def _note(text: str) -> None:
    NOTES.append(text)


# --------------------------------------------------------------------------- #
# 1) 引擎小工具
# --------------------------------------------------------------------------- #

class _Stub:
    """最小剧本代理：记下「引擎问了哪个座位哪种任务」，并按剧本回一个 dict。

    为什么必须有它：W 之外的每一类都要求「引擎真的跑过这条路」（夜间调用面、狼王带人、
    自爆、投票无效票、守墓人信息…），而跑引擎就得有代理。这里**坚决不接真模型**
    （不联网、不花钱），只回一份写死的剧本。
    """

    def __init__(self, seat_id: int, script: dict, log: list) -> None:
        self.seat_id = seat_id
        self.script = script or {}
        self.log = log
        self.day = 1

    def set_context(self, day, kind) -> None:              # 引擎每次调用前会告诉它第几天
        self.day = int(day or 1)

    async def ask(self, packet: str, task) -> dict:
        self.log.append((self.seat_id, task.kind, self.day))
        table = self.script.get(task.kind)
        reply: dict = {}
        if callable(table):
            reply = table(self.seat_id, task, self.day) or {}
        elif isinstance(table, dict):
            # ⚠️ 剧本表的键既有 int 座位号也有 "*"：两种都要认（老剧本全是这种写法）
            reply = table.get(self.seat_id, table.get("*", {})) or {}
        elif table is not None:
            reply = table
        return dict(reply) if isinstance(reply, dict) else {}

    def last_meta(self) -> dict:
        return {"model": "hardbank-stub"}

    async def aclose(self) -> None:
        return None


def _game(board=HARD_BOARD, roles=None, alive=None, seed=1, script=None,
          log=None, **kw) -> Game:
    """构造一个**局面完全可控**的引擎局：座位→角色显式指定、存活集合显式指定。

    ⚠️ 为什么钉死座位↔身份：`Game` 每局都重新洗牌（同一种子会洗出固定的映射，
    但那**不是我们写的那张表**）—— 老题库踩过「题面说 4 号是狼、引擎里其实是狼王」的过期。
    所以这里一律 `seat_by_id(i).role_key = ...`。

    `script` 给了就挂一个 `_Stub` 代理（跑异步流程用）；不给就 `agent_factory=None`
    （只调 `check_win` / `kill` / `king_targets` 这类纯逻辑）。
    """
    g = Game(board=make_board(board), agent_factory=None, seed=seed, max_days=1,
             on_event=lambda *a, **k: None, **kw)
    for sid, key in (roles or {}).items():
        g.seat_by_id(sid).role_key = key
    if alive is not None:
        for s in g.seats:
            s.alive = s.id in alive
    if script is not None:
        box = log if log is not None else []
        g.agent_factory = lambda seat: _Stub(seat.id, script, box)   # noqa: E731
    return g


def _sides(g: Game) -> tuple[list[int], list[int]]:
    """（神边、民边）的**存活**座位号 —— 双算的牌（愚者）两边都收。

    口径直接取引擎自己的屠边判定（`kind` 或 `also_kind` 命中），不另写一份规则。
    """
    good = [s for s in g.alive if s.kind != "wolf"]
    gods = sorted(s.id for s in good if s.kind == "god" or s.role.also_kind == "god")
    vils = sorted(s.id for s in good if s.kind == "villager" or s.role.also_kind == "villager")
    return gods, vils


def _sides_gap(alive, board=HARD_BOARD, roles=None) -> int | None:
    """「狼队还要再让几个好人出局，屠边才算成功」——**枚举**：逐个 k 试所有组合，
    交给引擎的 `_sides_done` 判。返回 None 表示「枚举不出结论」（那种局面直接弃题）。"""
    ids = sorted(set(alive))
    good = [i for i in ids if _game(board, roles, alive).seat_by_id(i).kind != "wolf"]
    for k in range(0, len(good) + 1):
        for combo in itertools.combinations(good, k):
            left = set(ids) - set(combo)
            g = _game(board, roles, alive=left)
            if g._sides_done([s for s in g.alive if s.kind != "wolf"]):
                return k
    return None


def _wolf_control(alive, board=HARD_BOARD, roles=None, lucky_used=None) -> bool:
    """扩表**之后**的控场判据（预言家/女巫/猎人/摄梦人 + 技能没用掉的幸运儿；
    女巫药用完仍算）。屠边已经成立的局面不算「控场」（那是屠边题，不是控场题）。

    `lucky_used`：幸运儿的一次性技能用没用掉 —— **这条直接决定他算不算夜间反击**
    （2026-10-11 扩表），所以必须能显式传进来。
    """
    g = _game(board, roles, alive=alive)
    if lucky_used is not None:
        g.seat_by_id(11).lucky_used = bool(lucky_used)
    if g._sides_done([s for s in g.alive if s.kind != "wolf"]):
        return False
    wolves = [s for s in g.alive if s.kind == "wolf"]
    good = [s for s in g.alive if s.kind != "wolf"]
    return bool(g.wolf_control and len(wolves) >= len(good) and not g.good_can_still_kill())


def _outcome(alive, target, cause, board=HARD_BOARD, roles=None) -> str | None:
    """「把 target 以 cause 弄出局」之后引擎判谁赢 —— 只用来枚举，所以不跑狼王那一口。"""
    g = _game(board, roles, alive=alive)
    g.day = 3
    g.kill(target, cause)
    return g.check_win()


def _unique_winner(alive, cause, want, board=HARD_BOARD, roles=None,
                   exclude=()) -> int | None:
    """枚举每个候选 → 谁能让 `want` 方**立刻**获胜。

    ⚠️ **并列最优或一个都没有就返回 None**：那种局面有歧义，规格书要求直接弃掉 ——
    宁可少一道题，也不要出一道「两个答案都对」的题去误判模型。
    """
    hits = [i for i in sorted(set(alive))
            if i not in exclude and _outcome(alive, i, cause, board, roles) == want]
    return hits[0] if len(hits) == 1 else None


def _alive_text(roles: dict, alive) -> str:
    """把「谁活着、谁出局、各自是什么牌」写成人话（W/X/S 的局面描述都用它）。"""
    live = "、".join(f"{i}号（{role_name(roles[i])}）" for i in sorted(roles) if i in alive)
    dead = "、".join(f"{i}号（{role_name(roles[i])}）" for i in sorted(roles) if i not in alive)
    out = f"现在活着的是 {live}。"
    if dead:
        out += f"已经出局的是 {dead}。"
    return out


def _q(qid, category, scenario, ask, expect, why, *, grader="exact", legal=None,
       forbid=None, board=None, book="open", mechanic="特设", tags=(),
       giveaways=(), recheck=None) -> Q:
    """造一道高难度题，并把闸门要用的元信息**挂在 Q 上**。

    ⚠️ 为什么挂属性而不是给 `exam_bank.Q` 加字段：本会话不许改 `exam_bank.py` / `exam.py`，
    而 dataclass 允许调用方临时挂属性。闸门读的就是这几个：
      · `scenario`  = 局面 / 公开记录那一段（**反饱和检查只查它**，不查问句 ——
        问句不点出「在问什么动作」就没法问）；
      · `book`      = 答案是否依赖**本项目特设**机制（open 必须带 `rules:` 上下文）；
      · `mechanic`  = 这道题考的是特设还是官方机制（写报告用）；
      · `giveaways` = 明确不许出现在 scenario 里的「答案字样」。
    """
    q = Q(qid=qid, category=category, question=scenario + "\n" + ask,
          expect=str(expect), why=why, grader=grader, context=f"rules:{board or HARD_BOARD}",
          legal=list(legal or []), forbid=list(forbid or []), tags=list(tags),
          board=board or HARD_BOARD, engine_checked=True)
    q.scenario = scenario
    q.ask = ask
    q.book = book
    q.mechanic = mechanic
    q.giveaways = list(giveaways)
    q.recheck = recheck
    VERIFIED.add(qid)
    return q


# --------------------------------------------------------------------------- #
# 2) W 类：轮次与胜负账（唯一解，引擎枚举得出）
# --------------------------------------------------------------------------- #

#: W 类的局面。`kind` 决定问什么，全部由下面的 `_w_answer()` 现算 —— 不写死答案。
W_CASES: tuple[dict, ...] = (
    {"qid": "W01", "kind": "control", "alive": (1, 2, 3, 4, 7, 8, 9)},
    {"qid": "W02", "kind": "control", "alive": (1, 2, 3, 4, 8, 9, 11), "lucky_used": True},
    {"qid": "W03", "kind": "control", "alive": (1, 2, 3, 4, 8, 9, 11), "lucky_used": False},
    {"qid": "W04", "kind": "gap", "alive": (1, 2, 3, 4, 5, 8, 9, 10, 11)},
    {"qid": "W05", "kind": "god_count", "alive": (1, 2, 3, 8, 9, 10)},
    {"qid": "W06", "kind": "villager_count", "alive": (1, 2, 3, 8, 9, 11), "lucky_used": True},
    {"qid": "W07", "kind": "exile_win", "alive": (1, 5, 6, 9)},
    {"qid": "W08", "kind": "kill_win", "alive": (1, 8, 9, 11), "lucky_used": True},
    {"qid": "W09", "kind": "gap", "alive": (1, 2, 8, 9, 10, 11)},
)

W_BY_QID = {c["qid"]: c for c in W_CASES}


def _w_answer(case: dict) -> tuple[str, str] | None:
    """算一道 W 题的（期望答案, 推理链）。算不出来（有歧义/局面不合法）就返回 None。"""
    alive = set(case["alive"])
    kind = case["kind"]
    roles = KING_SEATS
    g = _game(HARD_BOARD, roles, alive=alive)
    if case.get("lucky_used") is not None:
        # 幸运儿的一次性技能用没用的，直接决定「他还算不算夜间反击能力」（2026-10-11 扩表）
        g.seat_by_id(11).lucky_used = bool(case["lucky_used"])
    if kind == "control":
        gods, vils = _sides(g)
        if g._sides_done([s for s in g.alive if s.kind != "wolf"]):
            return None                       # 屠边已成立 ⇒ 这不是「控场」题
        control = _wolf_control(alive, HARD_BOARD, roles, case.get("lucky_used"))
        # 与引擎的判胜**互相印证**：控场成立 ⟺ check_win 判狼胜（在没有屠边的前提下）
        verdict = g.check_win()
        if control != (verdict == "wolf"):
            _note(f"{case['qid']}：控场自算（{control}）与 check_win（{verdict}）不一致")
            return None
        wolves = [s.id for s in g.alive if s.kind == "wolf"]
        answer = "控场" if control else "没控场"
        chain = (f"引擎实测：存活狼 {len(wolves)} 人、好人 {len(g.alive) - len(wolves)} 人；"
                 f"神边 {gods}、民边 {vils}；good_can_still_kill()="
                 f"{g.good_can_still_kill()} ⇒ check_win()={verdict or '没结束'}。"
                 f"扩表后的控场判据（预/女/猎/摄梦 + 技能没用掉的幸运儿）与引擎一次对上。")
        return answer, chain
    if kind in ("gap", "god_count", "villager_count"):
        gods, vils = _sides(g)
        gap = _sides_gap(alive, HARD_BOARD, roles)
        if gap is None:
            _note(f"{case['qid']}：枚举不出屠边还差几张，弃题")
            return None
        if kind == "gap":
            answer = str(gap)
            chain = (f"枚举实测：最少再让 {gap} 个好人出局就能屠边（神边 {gods}、民边 {vils}，"
                     f"取更近的一边）")
        elif kind == "god_count":
            answer = str(len(gods))
            chain = f"引擎的屠边口径算出神边活人 = {gods}（愚者 kind=god，本来就进神边）"
        else:
            answer = str(len(vils))
            chain = (f"引擎的屠边口径算出民边活人 = {vils} —— ⚠️ 愚者 also_kind=villager，"
                     f"所以**民边也把他算进去**（漏掉他会少数一张）")
        return answer, chain
    if kind == "exile_win":
        seat = _unique_winner(alive, "vote", "good", HARD_BOARD, roles)
        if seat is None:
            _note(f"{case['qid']}：放逐候选人里没有唯一能让好人立刻获胜的，弃题")
            return None
        chain = ("枚举实测：逐个候选模拟放逐（kill(t,'vote') 之后 check_win()=='good'），"
                 "**只有 {0}号 唯一**".format(seat))
        return str(seat), chain
    if kind == "kill_win":
        seat = _unique_winner(alive, "wolf", "wolf", HARD_BOARD, roles)
        if seat is None:
            _note(f"{case['qid']}：狼刀候选人里没有唯一能一刀定胜负的，弃题")
            return None
        chain = ("枚举实测：逐个候选模拟狼刀（kill(t,'wolf') 之后 check_win()=='wolf'），"
                 "**只有 {0}号 唯一**".format(seat))
        return str(seat), chain
    _note(f"{case['qid']}：未知的 kind={kind}")
    return None


def _w_ask(case: dict, answer: str) -> tuple[str, str, str]:
    """（问句, 期望答案的另一种说法, 不许出现在局面里的答案字样）。"""
    kind = case["kind"]
    if kind == "control":
        return ("现在狼人控场了吗？请只回答「控场」或「没控场」。",
                "没控场" if answer == "没控场" else "控场",
                ["控场了", "已经控场", "不会控场", "不判控场"])
    if kind == "gap":
        return ("按屠边账，狼队还要再让**几个**好人出局，才能屠边成功？请只回答数字。",
                answer, ["还差", "答案是"])
    if kind == "god_count":
        return ("现在**神边**（屠神要杀的那一边，含两边都算的牌）还有几个活人？请只回答数字。",
                answer, ["神边还有", "答案是"])
    if kind == "villager_count":
        return ("现在**民边**（屠民要杀的那一边，含两边都算的牌）还有几个活人？请只回答数字。",
                answer, ["民边还有", "答案是"])
    if kind == "exile_win":
        return ("今天白天好人手里的票要怎么用：**放逐几号**才能立刻获胜"
                "（所有狼人出局）？请只回答数字。", answer,
                ["只有", "必须", "答案是", "号才能"])
    if kind == "kill_win":
        return ("狼队今晚只有一刀，而且这一刀必定落地（本局没有女巫用药、没有摄梦人、"
                "没有幸运儿的守护）。**刀几号**能让狼人立刻获胜？请只回答数字。", answer,
                ["只有", "必须", "答案是", "号才能"])
    return ("请只回答数字。", answer, [])


def build_w(seed: int = 0) -> list[Q]:
    """W 类：轮次与胜负账。每道题的答案都走 `_w_answer()`（内部全是引擎调用）。"""
    out: list[Q] = []
    order = list(W_CASES)
    random.Random(seed).shuffle(order)                 # 顺序由 seed 决定，但题量不变
    for case in order:
        got = _w_answer(case)
        if got is None:
            continue                                   # 弃掉有歧义的题（并列最优 / 局面无效）
        answer, chain = got
        alive = set(case["alive"])
        scenario = ("【本题自成一体：下面的局面是题目给的，与别处的记录无关】\n"
                    f"11 人局（狼王版板子：3 狼 + 狼王 + 预言家、女巫、摄梦人、愚者 + "
                    f"守墓人、养鸦人、幸运儿）。{_alive_text(KING_SEATS, alive)}")
        if case.get("lucky_used") is not None:
            scenario += ("幸运儿 11号 的一次性技能**已经用掉**（本局他再也杀不了人）。"
                         if case["lucky_used"] else
                         "幸运儿 11号 的一次性技能**还没用掉**（他还能选『毒』）。")
        ask, _alt, giveaways = _w_ask(case, answer)
        trap = {
            "control": "最容易漏的是**幸运儿技能没用掉也算夜间反击**（2026-10-11 扩表）、"
                       "以及**女巫即使两瓶药都用完仍算**；只按官方印象答会算错。",
            "gap": "最容易漏的是**愚者两边都算**（漏了他，屠边账就少数一张）。",
            "god_count": "最容易漏的是**愚者 kind=god，本来就进神边**。",
            "villager_count": "最容易漏的是**愚者 also_kind=villager ⇒ 民边也要把他算进去**。",
            "exile_win": "枚举候选时要先把**两边都算的牌**算清（愚者既在神边也在民边），"
                         "否则会误判「放逐他算不算屠边」。",
            "kill_win": "枚举候选时要先把**两边都算的牌**算清（愚者既在神边也在民边），"
                        "否则会误判「刀他算不算一刀定胜负」。",
        }.get(case["kind"], "")
        why = (f"真值来源：{chain}。陷阱：{trap}"
               f"开卷理由：本题的判据（愚者双算 / 扩表后的反击清单）是本项目特设口径，"
               f"模型该去查本板规则公告，不该靠记忆。")
        out.append(_q(case["qid"], "轮次", scenario, ask, answer, why,
                      board=HARD_BOARD, book="open", mechanic="特设",
                      tags=[case["kind"]], giveaways=giveaways,
                      recheck=(lambda cid=case["qid"]: _w_answer(W_BY_QID[cid]))))
    return out


def recheck_w(qid: str) -> str | None:
    """闸门用：**重新用引擎推导**一道 W 题的期望答案（引擎一改，答案就会变 → 闸门判红）。"""
    case = W_BY_QID.get(qid)
    if case is None:
        return None
    got = _w_answer(case)
    return None if got is None else got[0]


# --------------------------------------------------------------------------- #
# 3) X 类：新口径边界（开卷；干扰项来自官方/旧口径）
# --------------------------------------------------------------------------- #

#: 狼王能「带走一人」的五种死因（规格书 §1 X 类点名的那五种）。
KING_CAUSES = (("wolf", "被狼刀"), ("poison", "被女巫毒"), ("dream", "被摄梦人连摄两晚梦死"),
               ("vote", "被投票放逐"), ("explode", "自爆"))


def _x_first_night(log: list):
    """首夜无刀那张板子的第 1 夜：引擎到底问了谁、问了几次。"""
    g = _game(HARD_BOARD, KING_SEATS, script={}, log=log)
    g.day = 1
    g.phase = "night"
    _run_blocking(g.night())
    return g, [(sid, kind) for sid, kind, _d in log]


def _king_take_paths() -> dict[str, bool]:
    """狼王（4号）在五种死因下出局时，引擎**真的有**让他带走 5号 吗（走真路径）。

    为什么必须跑真路径而不是读 `role.brief`：狼王那一口是**结算次序**里的一步
    （死亡结算 → 狼王带人 → 再判屠边），写在文档里的口径和引擎里真跑出来的可能不一致 ——
    这道题的全部价值就在「引擎到底做没做」。
    """
    out: dict[str, bool] = {}
    for cause, _name in KING_CAUSES:
        script = {"wolf_king_take": {"*": {"take": 5, "reason": "剧本：带走 5号"}},
                  "hunter_shoot": {"*": {"shoot": None, "reason": "剧本"}},
                  "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
                  "badge": {"*": {"to": None, "reason": "剧本"}}}
        g = _game(HARD_BOARD, KING_SEATS, alive={4, 5, 6, 7, 8, 9, 10, 11},
                  script=script, log=[], wolf_control=False)
        g.day = 2
        g.phase = "day"
        if cause in ("wolf", "poison", "dream"):
            _run_blocking(g.day_phase([(4, cause)]))
        elif cause == "vote":
            _run_blocking(g.exile(2, 4, 5.0))
        else:
            _run_blocking(g._apply_explosion(4, 2, False, 5))
        victim = g.seat_by_id(5)
        out[cause] = (not victim.alive) and victim.death_cause == "king"
    return out


def _x_king_order_winner() -> str:
    """结算次序题的真值：最后一狼是狼王、被票出去，他带走神边最后一张好人 → 谁赢。"""
    script = {"wolf_king_take": {"*": {"take": 8, "reason": "剧本：带走愚者"}},
              "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
              "badge": {"*": {"to": None, "reason": "剧本"}}}
    g = _game(HARD_BOARD, KING_SEATS, alive={4, 8, 9, 10, 11}, script=script, log=[],
              wolf_control=False)
    g.day = 3
    g.phase = "day"
    _run_blocking(g.exile(3, 4, 5.0))
    return "狼人阵营获胜" if g.winner == "wolf" else "好人阵营获胜"


def _x_seer_asked_run(log: list) -> bool:
    """`seer_auto_run` 开着时，预言家会不会被问「要不要上警」。"""
    g = _game(HARD_BOARD, KING_SEATS, script={}, log=log, seer_auto_run=True)
    g.day = 1
    g.phase = "day"
    _run_blocking(g.sheriff_election())
    asked = [(sid, kind) for sid, kind, _d in log]
    return any(kind == "sheriff_run" and sid == 5 for sid, kind in asked)


def _x_dreamer_death_kills_dreamed() -> bool:
    """摄梦人夜里被狼刀死时，今晚的梦游者会不会跟着死（连死已取消）。"""
    script = {"wolf_intent": {"*": {"target": 7, "reason": "剧本"}},
              "wolf_talk": {"*": {"speech": "（剧本）", "target": 7}},
              "wolf_kill": {"*": {"target": 7, "reason": "剧本"}},
              "seer_check": {"*": {"target": 1, "reason": "剧本"}},
              "dream": {"*": {"target": 8, "reason": "剧本"}},
              "witch": {"*": {"save": False, "poison": None, "reason": "剧本"}},
              "lucky": {"*": {"use": False, "reason": "剧本"}}}
    g = _game(DREAM_BOARD, DREAM_SEATS, script=script, log=[])
    g.day = 2
    g.phase = "night"
    deaths = _run_blocking(g.night())
    dead = [sid for sid, _cause in deaths]
    if 7 not in dead:
        _note(f"X 连死题：摄梦人 7号 第 2 夜没被狼刀死（deaths={deaths}），这道题跳过")
        return False
    return 8 in dead


def _x_idiot_vote_counts() -> bool:
    """已经翻牌的愚者再被投票时，那一票算不算有效票（引擎按无效票记）。"""
    script = {"vote": {"*": {"vote": 8, "reason": "剧本：投给翻牌的愚者"}},
              "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}}}
    g = _game(HARD_BOARD, KING_SEATS, script=script, log=[])
    g.day = 2
    g.phase = "day"
    g.seat_by_id(8).revealed_idiot = True
    votes = _run_blocking(g.run_vote(2))
    return all(v is None for v in votes.values())


def build_x() -> list[Q]:
    """X 类：新口径边界。**全部开卷**（附本板规则公告），干扰项来自官方/旧口径。"""
    out: list[Q] = []

    # --- 一、首夜无刀那张板子：第 1 夜的调用面 ---
    log: list = []
    _g, asked = _x_first_night(log)
    kinds = [k for _sid, k in asked]
    witch_asked = any(k == "witch" for k in kinds)
    dream_calls = kinds.count("dream")
    if len(kinds) != 1 or kinds[0] != "seer_check":
        _note(f"X 首夜无刀题：第 1 夜的调用面是 {kinds}（期望只有一次 seer_check）")
    else:
        base = ("这是一局 11 人场，板子是狼王版（3 狼 + 狼王 + 预言家、女巫、摄梦人、愚者 + "
                "守墓人、养鸦人、幸运儿）。现在问的是**第 1 夜**引擎会让谁行动。"
                "本局规则公告见上（按本局板子组装，不是别的板子）。")
        out.append(_q(
            "X01", "边界", base,
            "第 1 夜引擎一共会问**几个**座位做夜间行动（狼队、预言家、女巫、摄梦人、幸运儿"
            "都算）？请只回答数字。", len(kinds),
            f"真值来源：真跑 `night()`（第 1 天夜），剧本代理记下被问到的任务 = {kinds}。"
            f"陷阱：官方印象是「每夜狼刀 + 女巫 + 预言家一起动」，但本板【首夜无刀】——"
            f"第 1 夜狼队不出刀、女巫不给药、摄梦人不动手，只跑预言家一次验人。"
            f"开卷理由：首夜无刀是本项目特设的板子级规则（规则公告里有那一段）。",
            board=HARD_BOARD, book="open", mechanic="特设", tags=["首夜无刀"],
            giveaways=["只有一次", "答案是"]))
        out.append(_q(
            "X02", "边界", base,
            "第 1 夜引擎会不会问女巫「今晚救不救、毒不毒」？请回答「会」或「不会」。",
            "会" if witch_asked else "不会",
            f"真值来源：同一次 `night()` 实测里女巫的任务是否出现（witch={witch_asked}）。"
            f"陷阱：官方与旧板子的口径都是「第一夜女巫手刀」（她第一夜就用药）——"
            f"本板第一夜她**既不行动、也不被告知刀口**。"
            f"开卷理由：首夜无刀是特设规则，模型该读本局公告那一段。",
            board=HARD_BOARD, book="open", mechanic="特设", tags=["首夜无刀"],
            giveaways=["不会问", "会问", "答案是"]))
        out.append(_q(
            "X03", "边界", base,
            "第 1 夜摄梦人需要摄梦几次？请只回答数字。", dream_calls,
            f"真值来源：真跑 `night()` 得到的 dream 任务数 = {dream_calls}。"
            f"陷阱：官方摄梦人「每晚必须选一名梦游者」——本板第 1 夜她不行动（第 2 夜起才摄）。"
            f"开卷理由：特设规则，见本局公告。",
            board=HARD_BOARD, book="open", mechanic="特设", tags=["首夜无刀"],
            giveaways=["不用", "答案是"]))

    # --- 二、狼王那一口：五种死因 + 名单 ---
    paths = _king_take_paths()
    n_cannot = sum(1 for v in paths.values() if not v)
    king_seat = "4号（狼王）"
    king_base = ("这是一局 11 人场（狼王版）。局面：3 头平狼都已经出局，场上只剩狼王 4号 一个狼；"
                 "好人这边 5号（预言家）、6号（女巫）、7号（摄梦人）、8号（愚者）、9号（守墓人）、"
                 "10号（养鸦人）、11号（幸运儿）都还活着。本局规则公告见上。")
    out.append(_q(
        "X04", "边界", king_base,
        f"如果 {king_seat} 是被**女巫的毒药**毒死的，他还能不能当场带走一名好人？"
        f"请回答「能」或「不能」。", "能" if paths.get("poison") else "不能",
        f"真值来源：真跑 `day_phase([(4,'poison')])`，剧本让他 `take=5`，"
        f"5号 真的以「被狼王带走」出局 = {paths.get('poison')}"
        f"（这一步在判胜负之前）。"
        f"陷阱：**官方口径是「被毒不能开枪」**（规则书里猎人那条就是这个），"
        f"本项目的狼王**明确推翻了这个例外**：被毒也能带。"
        f"开卷理由：这是本项目特设口径，公告的【狼王】那一段写着「被毒也能带」。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["狼王", "被毒也能带"],
        giveaways=["能带人", "不能带人", "照样能带", "答案是"]))
    out.append(_q(
        "X05", "边界", king_base,
        f"如果 {king_seat} 是**自爆**出局的（当场亮出狼王身份），他还能不能当场带走一名好人？"
        f"请回答「能」或「不能」。", "能" if paths.get("explode") else "不能",
        f"真值来源：真跑 `_apply_explosion(4, 2, False, take=5)` → 5号 是否以 king 死因出局 "
        f"= {paths.get('explode')}。"
        f"陷阱：官方「白狼王自爆带人」与「狼王出局带人」是两张牌，很多模型只记得其中一张；"
        f"本项目的狼王**自爆 = 白狼王式**（亮身份 → 带人 → 直接进黑夜）。"
        f"开卷理由：特设口径，公告里写着。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["狼王", "自爆"],
        giveaways=["能带人", "不能带人", "答案是"]))
    g_targets = _game(HARD_BOARD, KING_SEATS, alive=set(KING_SEATS))
    targets = g_targets.king_targets(g_targets.seat_by_id(4))
    has_wolf_target = any(g_targets.seat_by_id(i).camp == "wolf" for i in targets)
    out.append(_q(
        "X06", "边界", king_base,
        "狼王 4号 出局时，引擎给他的「可以带走谁」名单里**有没有狼队友**？"
        "请回答「有」或「没有」。", "有" if has_wolf_target else "没有",
        f"真值来源：`king_targets(4号)` 实测 = {targets}（全是好人）。"
        f"陷阱：官方狼王只写「带走一名玩家」，不排除队友 —— 本项目是**硬规则：不能带狼队友**。"
        f"开卷理由：特设口径，公告的【狼王·不能带队友】那一段。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["狼王"],
        giveaways=["只有好人", "没有狼", "答案是"]))
    out.append(_q(
        "X07", "边界", king_base,
        "狼王因为下面这五种原因出局时 —— ① 被狼刀 ② 被女巫毒 ③ 被摄梦人连摄两晚梦死 "
        "④ 被投票放逐 ⑤ 自爆 —— 其中有**几种**会让他**不能**带走一名好人？请只回答数字。",
        n_cannot,
        f"真值来源：五种死因各跑一次真路径（`day_phase` / `exile` / `_apply_explosion`），"
        f"能不能带走 5号 的结果 = {paths} ⇒ 不能带的有 {n_cannot} 种。"
        f"陷阱：官方口径只有「被毒不能开枪」这一条例外，模型很容易答 1；"
        f"本项目**任何原因出局都能带**（包括被毒、包括自爆），所以正解是 0。"
        f"开卷理由：特设口径，公告【狼王·带走一人】那一段把这五种都列出来了。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["狼王"],
        giveaways=["答案是", "共有"]))
    order_winner = _x_king_order_winner()
    out.append(_q(
        "X08", "边界",
        ("这是一局 11 人场（狼王版）。局面：3 头平狼都出局了，只剩狼王 4号 一个狼；"
         "好人还活着 8号（愚者）、9号（守墓人）、10号（养鸦人）、11号（幸运儿）——"
         "也就是说预言家、女巫、摄梦人都已经出局，**神边只剩愚者这一张**。"
         "今天白天好人把狼王 4号 投票放逐了，他临死带走了 8号。本局规则公告见上。"),
        "这一局谁赢了？请回答「狼人阵营获胜」或「好人阵营获胜」。", order_winner,
        f"真值来源：真跑 `exile(3, 4号)`（剧本 take=8）→ `winner={order_winner}`。"
        f"陷阱：官方直觉是「最后一狼被票出去 = 好人赢」；本项目的结算次序是"
        f"**死亡结算 → 狼王带人 → 再判屠边**，而且边界口径**屠边优先**，"
        f"所以狼王带走神边最后一张 = 狼队屠边成功。"
        f"开卷理由：特设口径（公告【狼王·结算次序】）。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["狼王", "结算次序"],
        giveaways=["狼队赢", "好人赢", "答案是"]))

    # --- 三、锁定上警 / 连死 / 连咒 / 无效票 ---
    log2: list = []
    seer_asked = _x_seer_asked_run(log2)
    out.append(_q(
        "X09", "边界",
        ("这是一局 11 人场（狼王版），第 1 天要竞选警长。本局规则公告见上。"),
        "按本局规则，预言家这一轮会不会被引擎问「你要不要上警」？请回答「会」或「不会」。",
        "会" if seer_asked else "不会",
        f"真值来源：真跑 `sheriff_election()`，剧本代理记下的任务里预言家（5号）有没有 "
        f"`sheriff_run` = {seer_asked}（同一局公告里有「5号 上警」）。"
        f"陷阱：官方口径是「预言家自己决定上不上警」；本局 `seer_auto_run` 默认开 ⇒ "
        f"**锁定上警、不问他**（省一次调用）。"
        f"开卷理由：特设开局选项，公告【预言家·锁定上警】那一段写着「本局不问你」。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["锁定上警"],
        giveaways=["不问他", "会问", "答案是"]))
    dream_kills = _x_dreamer_death_kills_dreamed()
    out.append(_q(
        "X10", "边界",
        ("这是一局 11 人场（预女梦愚版：4 狼 + 预言家、女巫、摄梦人、愚者 + 守墓人、养鸦人、"
         "幸运儿）。第 2 夜：摄梦人 7号 摄了 8号 当梦游者，同时狼队一刀砍在摄梦人 7号 身上，"
         "7号 当场死了。本局规则公告见上。"),
        "他今晚摄的梦游者 8号 会不会跟着一起死？请回答「会」或「不会」。",
        "会" if dream_kills else "不会",
        f"真值来源：真跑 `night()`（第 2 夜）→ 死者里有没有 8号 = {dream_kills}。"
        f"陷阱：**官方口径是「摄梦人夜里死亡则梦游者一并死亡」（连死）**，"
        f"本项目 2026-10-10 已取消这条（跟守卫一致）。"
        f"开卷理由：特设口径改动，公告【摄梦人】那一段写着「你出局不会带走梦游者」。",
        board=DREAM_BOARD, book="open", mechanic="特设", tags=["摄梦人", "连死取消"],
        giveaways=["不会死", "会死", "答案是"]))
    g_crow = _game(HARD_BOARD, KING_SEATS, alive=set(KING_SEATS))
    crow = g_crow.seat_by_id(10)
    crow.last_curse = 5
    crow.last_curse_day = 1
    legal_curse = g_crow._crow_curse_legal(crow, 2)
    out.append(_q(
        "X11", "边界",
        ("这是一局 11 人场（狼王版）。养鸦人 10号 昨天（第 1 天）已经把诅咒开在 5号 头上了。"
         "本局规则公告见上。"),
        "今天（第 2 天）他想再咒 5号 一次，规则上允许吗？请回答「可以」或「不可以」。",
        "可以" if 5 in legal_curse else "不可以",
        f"真值来源：`_crow_curse_legal(10号, 第2天)` 实测 = {legal_curse}，"
        f"里面**含上一次咒过的 5号**。"
        f"陷阱：官方乌鸦「不能连续两晚诅咒同一名玩家」；本项目的养鸦人是"
        f"「两票的民牌」，**可以连着几天咒同一个人**（旧的 last_curse 字段只记录、不设限）。"
        f"开卷理由：特设口径，公告【养鸦人】那一段。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["养鸦人", "可连咒"],
        giveaways=["可以连", "不可以", "答案是"]))
    idiot_invalid = _x_idiot_vote_counts()
    out.append(_q(
        "X12", "边界",
        ("这是一局 11 人场（狼王版）。8号 是愚者，他昨天（第 1 天）已经被投票过一次、"
         "翻牌活了下来。今天白天投票时，有别的玩家把票投给了 8号。本局规则公告见上。"),
        "这一票算不算有效票（计入计票）？请回答「算」或「不算」。",
        "不算" if idiot_invalid else "算",
        f"真值来源：真跑 `run_vote()`（8号 revealed_idiot=True，剧本让所有人投 8号）→ "
        f"引擎把每一票都记成无效票 = {idiot_invalid}。"
        f"陷阱：官方口径只说「白痴翻牌后失去投票权」，**没说别人投他不计票**；"
        f"本项目明确按无效票处理（公告【愚者】那一段写着「翻牌之后别人投他 = 无效票」）。"
        f"开卷理由：特设口径，公告里写着。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["愚者", "无效票"],
        giveaways=["无效票", "算有效", "答案是"]))
    return out


# --------------------------------------------------------------------------- #
# 4) Y 类：票型与时间线对账（真跑半局，答案只从公开条目里推）
# --------------------------------------------------------------------------- #

#: 两个半局里的「剧本」：台词、声称身份、票。**答案由构造决定**，
#: 但题干里给的只有**公开记录**，所以「能不能从公开信息推出来」这件事本身也是被校验的。
Y_A_SPEECH = {
    1: "我是好人。2号 报的 9号 金水我认，5号 那个悍跳的我看着像狼。",
    2: "我是预言家。第 1 夜我验了 9号，是好人（金水）。",
    3: "我是好人，我信 2号，先出 5号。",
    4: "我是好人，2号 报的金水我信，5号 是悍跳。",
    5: "我是预言家。第 1 夜我验了 2号，是狼人（查杀）。今天必须出 2号。",
    6: "我信 5号：2号 是查杀，今天出 2号。",
    7: "我跟 5号，出 2号。",
    8: "5号 的报验比 2号 干净，我出 2号。",
    9: "我出 2号。",
    10: "我信 2号，5号 才是悍跳的那个人。",
    11: "我出 2号。",
}
Y_A_CLAIM = {2: "预言家", 5: "预言家"}
Y_A_VOTE = {1: 5, 2: 9, 3: 5, 4: 5, 5: 2, 6: 2, 7: 2, 8: 2, 9: 2, 10: 5, 11: 2}

Y_B_SPEECH = {
    1: "我是好人，3号 那个悍跳的我看着像狼。",
    2: "我是好人，先听预言家报验。",
    3: "我是预言家。第 1 夜我验了 6号，是好人；第 2 夜我验了 9号，是好人。",
    4: "我是好人，我信 3号 的报验。",
    5: "我是预言家。第 1 夜我验了 1号，是狼人（查杀）；第 2 夜我验了 4号，是好人。",
    6: "我是好人，我信 5号 的查杀。",
    7: "我跟 5号 走，出 1号。",
    8: "我出 1号。",
    10: "我出 1号。",
    11: "我出 1号。",
}
Y_B_CLAIM = {3: "预言家", 5: "预言家"}
#: ⚠️ 这一组票是**故意设计**成「人头打平、加权分胜负」的：
#: 1号 的票来自 {2, 4, 5（警长）, 10}、6号 的票来自 {1, 3, 7, 8} —— **人头各 4 票**；
#: 而 5号 是警长（他那一票算 1.5）⇒ 加权之后 1号 4.5 > 6号 4。只数人头会判成平票。
Y_B_VOTE = {1: 6, 2: 1, 3: 6, 4: 1, 5: 1, 6: 3, 7: 6, 8: 6, 10: 1, 11: 3}


def _y_game_a(seed: int) -> Game:
    """半局 A：平安夜 + 两个悍跳预言家 + 一场放逐投票（不设警长）。"""
    def speech(sid, task, day):
        if day == 1:
            return {"speech": "（第 1 天）先听大家的。", "claim": "无"}
        return {"speech": Y_A_SPEECH.get(sid, "（我不多说。）"), "claim": Y_A_CLAIM.get(sid, "无")}

    def vote(sid, task, day):
        if day == 1:
            return {"abstain": True}
        return {"vote": Y_A_VOTE.get(sid), "reason": "剧本"}

    script = {"speech": speech, "vote": vote,
              "last_words": {"*": {"speech": "（剧本遗言）我是好人。", "claim": "无"}}}
    g = _game(DREAM_BOARD, DREAM_SEATS, script=script, log=[], seed=seed,
              decision_first=False, sheriff=False, sheriff_picks_order=False)
    for day in (1, 2):
        g.day = day
        g.phase = "day"
        _run_blocking(g.day_phase([]))
    return g


def _y_game_b(seed: int) -> Game:
    """半局 B：第 1 夜有人出局 + 选警长 + 加权计票（警徽 1.5 决定胜负）。"""
    def speech(sid, task, day):
        if day == 1:
            return {"speech": "（第 1 天）先听预言家报验。", "claim": "无"}
        return {"speech": Y_B_SPEECH.get(sid, "（我不多说。）"), "claim": Y_B_CLAIM.get(sid, "无")}

    def vote(sid, task, day):
        if day == 1:
            return {"abstain": True}
        return {"vote": Y_B_VOTE.get(sid), "reason": "剧本"}

    script = {"speech": speech, "vote": vote,
              # 上警：1号（狼）后置位上警；预言家 5号 由 seer_auto_run 自动当前置位候选人
              "sheriff_run": {1: {"run": "back"}, "*": {"run": False}},
              "sheriff_speech": {"*": {"speech": "（剧本竞选发言）我要当警长。", "claim": "无"}},
              "sheriff_vote": {"*": {"vote": 5, "reason": "剧本"}},
              "sheriff_order": {"*": {"explode": False, "reverse": False, "reason": "剧本"}},
              "last_words": {"*": {"speech": "（剧本遗言）我是好人。", "claim": "无"}},
              "badge": {"*": {"to": None, "reason": "剧本"}}}
    g = _game(DREAM_BOARD, DREAM_SEATS, script=script, log=[], seed=seed,
              decision_first=False, sheriff=True, sheriff_min_seats=11,
              sheriff_picks_order=False)
    # 第 1 夜：9号 被狼刀（**预女梦愚版没有「首夜无刀」**，所以这条记录与板子规则自洽）
    g.day = 1
    g.phase = "day"
    _run_blocking(g.day_phase([(9, "wolf")]))
    g.day = 2
    g.phase = "day"
    _run_blocking(g.day_phase([]))
    return g


def _y_rows(g: Game) -> list[dict]:
    """公开条目（模型能看到的就是这些）。"""
    return [e for e in g.ledger.to_list() if "public" in (e.get("visible_to") or ())]


def _y_render(rows: list[dict], day_kinds: dict | None = None,
              day_require: dict | None = None, days=None) -> str:
    """把公开条目渲染成模型看到的那份记录（格式与 `exam.py` 的 `public_record` 一致）。

    `days`：只渲染这几天（None = 全部）。
    `day_kinds`：可选，形如 `{1: {"announce"}}` = 第 1 天只给公告。
    `day_require`：可选，形如 `{1: ("昨晚出局",)}` = 第 1 天只留含这些字样的条目。
    为什么要后两个参数：半局 B 的第一天有一地弃权票与汇总公告，跟考点无关 ——
    把它们原样倒进题面只会摊薄注意力（**绝不改内容，只是挑跟考点有关的那些行**）。
    """
    lines: list[str] = []
    for e in rows:
        day = int(e.get("day") or 0)
        if day <= 0:
            continue
        if days is not None and day not in days:
            continue
        allow = (day_kinds or {}).get(day)
        if allow is not None and e.get("kind") not in allow:
            continue
        need = (day_require or {}).get(day)
        if need is not None and not any(part in str(e.get("text") or "") for part in need):
            continue
        who = f"{e['seat']}号：" if e.get("seat") else "公告："
        lines.append(f"[第{day}天·{'夜' if e.get('phase') == 'night' else '白'}] {who}{e['text']}")
    return "\n".join(lines)


_CLAIM_SEER_RE = re.compile(r"^\d+号 声称自己是【预言家】")
_CHECK_RE = re.compile(r"第\s*(\d+)\s*夜[^。；\n]*?验了\s*(\d+)\s*号[^。；\n]*?是\s*(好人|狼人)")


def _y_speech(rows: list[dict], day: int, seat: int) -> str:
    for e in rows:
        if (e.get("kind") == "speech" and int(e.get("day") or 0) == day
                and e.get("seat") == seat and not str(e.get("text") or "").startswith("【")):
            return str(e.get("text") or "")
    return ""


def _y_vote(rows: list[dict], day: int, seat: int):
    for e in rows:
        if (e.get("kind") == "vote" and int(e.get("day") or 0) == day and e.get("seat") == seat):
            d = e.get("data") or {}
            if d.get("sheriff_vote"):
                continue
            if d.get("abstain") or d.get("invalid"):
                return None
            return d.get("target")
    return None


def _y_seer_chains(rows: list[dict], day: int) -> dict[int, list[tuple[int, int, str]]]:
    """从**公开条目**里解析「谁跳了预言家、报了哪几夜验谁是什么」。

    只用公开发言 + 公开的身份声明 —— 这正是模型手上有的那份材料。
    """
    claimants = {int(e["seat"]) for e in rows
                 if e.get("kind") == "claim" and e.get("seat")
                 and _CLAIM_SEER_RE.match(str(e.get("text") or ""))}
    out: dict[int, list[tuple[int, int, str]]] = {}
    for sid in sorted(claimants):
        found = _CHECK_RE.findall(_y_speech(rows, day, sid))
        if found:
            out[sid] = [(int(n), int(t), v) for n, t, v in found]
    return out


def _y_death_days(rows: list[dict]) -> dict[int, int]:
    """「谁在第几天的公告里被公布出局」→ {座位: 公告的天}（死因不公示，只有天号）。"""
    out: dict[int, int] = {}
    for e in rows:
        if e.get("kind") != "announce":
            continue
        m = re.match(r"昨晚出局：(.+?)。", str(e.get("text") or ""))
        if not m:
            continue
        for sid in re.findall(r"(\d+)号", m.group(1)):
            out.setdefault(int(sid), int(e.get("day") or 0))
    return out


def _y_contradictory(rows: list[dict], day: int) -> set[int]:
    """谁把自己报过的**金水**投了出去 —— 说法与票自己打自己（公开信息就能看出来）。"""
    bad: set[int] = set()
    for sid, chain in _y_seer_chains(rows, day).items():
        vote = _y_vote(rows, day, sid)
        if vote is None:
            continue
        for _night, target, verdict in chain:
            if verdict == "好人" and vote == target:
                bad.add(sid)
    return bad


def _y_impossible(rows: list[dict], day: int) -> set[int]:
    """谁的报验与**出局时间线**矛盾：说某一夜验了一个那时已经出局的座位。

    推理链（写进 why 供人工复核）：预言家只能验**当时活着**的人；
    「昨晚出局：9号」这条公告（第 1 天）说明 9号 在第 1 夜已经出局 ⇒
    任何「第 2 夜验了 9号」的说法都是硬伤。
    """
    dead = _y_death_days(rows)
    bad: set[int] = set()
    for sid, chain in _y_seer_chains(rows, day).items():
        for night, target, _v in chain:
            announced = dead.get(target)
            if announced is not None and night > announced:
                bad.add(sid)
    return bad


def _y_final_round(rows: list[dict], day: int) -> int:
    rounds = [int((e.get("data") or {}).get("round") or 1) for e in rows
              if e.get("kind") == "vote" and int(e.get("day") or 0) == day
              and not (e.get("data") or {}).get("sheriff_vote")]
    return max(rounds) if rounds else 1


def _y_head_tally(rows: list[dict], day: int) -> dict[int, int]:
    """这一天最后一轮的**人头票数**（不算警徽加权）—— 从公开的 vote 条目直接数。"""
    final = _y_final_round(rows, day)
    tally: dict[int, int] = {}
    for e in rows:
        if e.get("kind") != "vote" or int(e.get("day") or 0) != day:
            continue
        d = e.get("data") or {}
        if (d.get("sheriff_vote") or d.get("abstain") or d.get("invalid")
                or int(d.get("round") or 1) != final or not d.get("target")):
            continue
        tally[int(d["target"])] = tally.get(int(d["target"]), 0) + 1
    return tally


def _y_head_top(rows: list[dict], day: int) -> int | None:
    """这一天人头最多的是谁。并列 → None（闸门/出题会据此区分「平票」和「唯一」）。"""
    tally = _y_head_tally(rows, day)
    if not tally:
        return None
    best = max(tally.values())
    top = [k for k, v in tally.items() if v == best]
    return top[0] if len(top) == 1 else None


def _y_weighted_top(g: Game, rows: list[dict], day: int) -> int | None:
    """按**本局计票规则**（警长那一票算 1.5）算这一天最后一轮的最高票 ——
    直接把解析出来的票喂给引擎的 `weighted_counts()`，不自己写一份计票。"""
    final = _y_final_round(rows, day)
    votes: dict[int, object] = {}
    for e in rows:
        if e.get("kind") != "vote" or int(e.get("day") or 0) != day or not e.get("seat"):
            continue
        d = e.get("data") or {}
        if d.get("sheriff_vote") or int(d.get("round") or 1) != final:
            continue
        votes[int(e["seat"])] = None if (d.get("abstain") or d.get("invalid")) else d.get("target")
    counts = g.weighted_counts(votes)
    if not counts:
        return None
    best = max(counts.values())
    top = [k for k, v in counts.items() if abs(v - best) < 1e-9]
    return top[0] if len(top) == 1 else None


def build_y(seed: int = 0) -> list[Q]:
    """Y 类：票型与时间线对账。真跑两段半局，答案**只从公开条目里推**。"""
    out: list[Q] = []

    # ---------------- 半局 A ----------------
    ga = _y_game_a(seed)
    rows_a = _y_rows(ga)
    day_a = 2
    rec_a = _y_render(rows_a, days={day_a})
    chains_a = _y_seer_chains(rows_a, day_a)
    bad_a = _y_contradictory(rows_a, day_a)
    if len(chains_a) != 2 or len(bad_a) != 1:
        _note(f"Y 半局A：跳预言家的有 {sorted(chains_a)}，自相矛盾的有 {sorted(bad_a)}"
              f"（期望各 2 / 1）—— 相关题全部弃掉")
    else:
        guilty = sorted(bad_a)[0]
        good_seer = [s for s in chains_a if s not in bad_a]
        base = ("【本题自成一体：下面是这一局的公开记录，题目只在它上面推】\n"
                f"{rec_a}\n"
                "（本局是 11 人预女梦愚板子：4 狼 + 预言家、女巫、摄梦人、愚者 + "
                "守墓人、养鸦人、幸运儿；本局规则公告见上。）")
        out.append(_q(
            "Y01", "对账", base,
            "第 2 天里，谁的**公开发言与他自己的票对不上**（他说自己验出的好人，却把票投给了"
            "这个人）？请只回答数字。", guilty,
            f"真值来源：构造 + 公开条目复核。引擎真跑的台账里，"
            f"{guilty}号 声称自己是【预言家】并在发言里报了"
            f"{chains_a[guilty]}，而他这一天的票投给了 "
            f"{_y_vote(rows_a, day_a, guilty)}号 —— 金水投出去，自己打自己。"
            f"推理链（人工可复核）：{_y_seer_chains(rows_a, day_a)}。"
            f"闭卷理由：本题只用官方机制（跳身份、票型、金水/查杀的含义），"
            f"模型记忆里的答案与本项目一致；公告照附只为与游戏内一致。",
            board=DREAM_BOARD, book="closed", mechanic="官方", tags=["言行矛盾"],
            giveaways=[f"{guilty}号 矛盾", "答案是", "矛盾的是"]))
        if len(good_seer) == 1:
            out.append(_q(
                "Y02", "对账", base,
                "按这份公开记录，跳预言家的两个人里，**谁的报验和票是对得上的**？"
                "请只回答数字。", good_seer[0],
                f"真值来源：同一份台账的两步对账 —— {sorted(chains_a)} 两个人都跳预言家，"
                f"{guilty}号 把金水投了出去；{good_seer[0]}号 报的查杀和票一致"
                f"（验 {chains_a[good_seer[0]]}，票投 {_y_vote(rows_a, day_a, good_seer[0])}号）。"
                f"闭卷理由：只用官方机制（跳身份 + 票型自洽），不需要本板特设规则。",
                board=DREAM_BOARD, book="closed", mechanic="官方", tags=["报验自洽"],
                giveaways=[f"{good_seer[0]}号 自洽", "答案是"]))
        top = _y_head_top(rows_a, day_a)
        if top is None:
            _note("Y 半局A：人头最高票不唯一，得票题弃掉")
        else:
            out.append(_q(
                "Y03", "对账", base,
                "第 2 天**最后一轮**放逐投票里，得票最多的是几号（只数人头，不算警徽加权）？"
                "请只回答数字。", top,
                f"真值来源：从公开的 vote 条目里数人头（本局没有警长，所以不需要加权），"
                f"最高票 = {top}号。"
                f"闭卷理由：纯官方机制（计票），模型不需要查规则公告。",
                board=DREAM_BOARD, book="closed", mechanic="官方", tags=["计票"],
                giveaways=["答案是", "最高票是"]))

    # ---------------- 半局 B ----------------
    gb = _y_game_b(seed)
    rows_b = _y_rows(gb)
    day_b = 2
    # 半局 B 只要第一天的**出局公告**（弃权票与汇总公告跟考点无关，不倒进题面）
    rec_b = _y_render(rows_b, days={1, day_b}, day_kinds={1: {"announce"}},
                      day_require={1: ("昨晚出局",)})
    chains_b = _y_seer_chains(rows_b, day_b)
    impossible_b = _y_impossible(rows_b, day_b)
    if len(chains_b) != 2 or len(impossible_b) != 1:
        _note(f"Y 半局B：跳预言家的有 {sorted(chains_b)}，有时间线硬伤的有 {sorted(impossible_b)}"
              f"（期望各 2 / 1）—— 相关题全部弃掉")
    else:
        liar = sorted(impossible_b)[0]
        consistent = [s for s in chains_b if s not in impossible_b]
        sheriff = gb.sheriff_id
        base = ("【本题自成一体：下面是这一局的公开记录，题目只在它上面推】\n"
                f"{rec_b}\n"
                "（本局是 11 人预女梦愚板子：4 狼 + 预言家、女巫、摄梦人、愚者 + "
                "守墓人、养鸦人、幸运儿；本局规则公告见上。）")
        out.append(_q(
            "Y04", "对账", base,
            "按这份公开记录（含**出局公告**），跳预言家的两个人里，**哪一号不可能是预言家**？"
            "请只回答数字。", liar,
            f"真值来源：构造 + 公开条目复核。{liar}号 公开声称「第 2 夜验了 "
            f"{[t for n, t, _v in chains_b[liar] if n > _y_death_days(rows_b).get(t, 99)]}」"
            f"里的那个座位，而那份座位**第 1 天就已经被公告出局**"
            f"（出局公告见记录，时间线 = {_y_death_days(rows_b)}）；"
            f"预言家只能验**当时活着**的人 ⇒ 这个报验不可能是真的。"
            f"另一位的验人链与票自洽。推理链：{chains_b}。"
            f"闭卷理由：本题的硬伤靠官方机制（预言家只能验活人 + 出局公告的时间线）判，"
            f"不需要本板特设规则。",
            board=DREAM_BOARD, book="closed", mechanic="官方", tags=["时间线对账"],
            giveaways=[f"{liar}号 不可能", "答案是", "不可能是"]))
        if len(consistent) == 1:
            out.append(_q(
                "Y05", "对账", base,
                "同一份记录里，跳预言家的人中**谁的报验链没有硬伤**（可以是真预言家）？"
                "请只回答数字。", consistent[0],
                f"真值来源：同一份台账 —— {liar}号 的报验撞上出局时间线，"
                f"{consistent[0]}号 报的两夜验人对象在各自那一夜都还活着，且查杀与票一致。"
                f"闭卷理由：同上（官方机制）。",
                board=DREAM_BOARD, book="closed", mechanic="官方", tags=["时间线对账"],
                giveaways=[f"{consistent[0]}号 没有硬伤", "答案是"]))
        wtop = _y_weighted_top(gb, rows_b, day_b)
        if wtop is None:
            _note("Y 半局B：加权最高票不唯一，加权计票题弃掉")
        else:
            head = _y_head_top(rows_b, day_b)
            head_tally = _y_head_tally(rows_b, day_b)
            out.append(_q(
                "Y06", "对账", base,
                f"第 2 天最后一轮放逐投票里，按**本局的计票规则**（警长是 {sheriff}号，"
                f"他那一票算 1.5）算，得票最高的是几号？请只回答数字。", wtop,
                f"真值来源：把台账里的票喂给引擎的 `weighted_counts()`（警长权重 1.5），"
                f"最高票 = {wtop}号。"
                f"⚠️ 陷阱：**只数人头会打成平票**（人头票数 = {head_tally}）——"
                f"警徽那 0.5 票才是分水岭，这就是「账要算对」的地方。"
                f"闭卷理由：警徽 1.5 票是官方机制（公告里也写着），不需要特设规则。",
                board=DREAM_BOARD, book="closed", mechanic="官方", tags=["警徽加权"],
                giveaways=["答案是", "最高票是"]))
    return out


# --------------------------------------------------------------------------- #
# 5) Z 类：抗崩决策（枚举 + 明确收益函数 → legal 判分）
# --------------------------------------------------------------------------- #

def _z_scenario(base: str, pain: str) -> str:
    return base + pain


def build_z() -> list[Q]:
    """Z 类：给定两难局面问「你会怎么做」，答案是**枚举 + 收益函数**算出的最优解集合。

    ⚠️ `grader="legal"` 只认座位号（`exam.py` 的 legal 分支会对答案取整数），
    所以每道 Z 题的问法都收在「选几号」上；收益函数的完整排序写在 `why` 里 ——
    判分器目前**分不出「它选的是第几档」**（那是规格书 §1 Z 想要的能力，
    需要 exam.py 扩展记档，见报告）。
    """
    out: list[Q] = []

    # --- Z1 女巫：手里有毒，两个候选里毒谁 ---
    g1 = _game(HARD_BOARD, KING_SEATS, alive=set(KING_SEATS))
    paths = _king_take_paths()
    poison_candidates = (2, 4)
    scores = {}
    for c in poison_candidates:
        s = 2 if g1.seat_by_id(c).camp == "wolf" else 0
        if g1.seat_by_id(c).role_key == "wolf_king":
            s -= 1                     # 他那一口照样带走一名好人（引擎里真会发生）
        scores[c] = s
    best = max(scores.values())
    legal1 = [c for c in poison_candidates if scores[c] == best]
    worst1 = [c for c in poison_candidates if scores[c] != best]
    if legal1 and not paths.get("poison"):
        _note("Z1：引擎实测狼王被毒后**没有**带走人，收益函数的前提失效")
    elif legal1:
        out.append(_q(
            "Z01", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人场（狼王版）。第 2 天白天的公开信息：2号 跳预言家、说「第 1 夜我验了 "
             "9号，是好人」，可他投票时把票投给了 9号；5号 也跳预言家、说「第 1 夜我验了 2号，"
             "是狼人」，并且把票投给了 2号。\n"
             "现在你是 6号 女巫，手里还有一瓶毒药，今晚你**只能在 2号 和 4号 里选一个毒**"
             "（别人都不能选）。本局规则公告见上。"),
            "你毒几号？请只回答数字。", legal1[0],
            f"真值来源：枚举两个候选 + 明确收益函数（引擎给出的真实阵营/身份）。"
            f"收益函数：① 毒死一张**狼牌** = 2 分；② 毒死好人 = 0 分；"
            f"③ **毒到狼王要减 1 分** —— 因为本项目口径「狼王被毒**照样能带走一名好人**」，"
            f"那一口不是「毒掉就废了」（真跑 `day_phase([(4,'poison')])` 实测他确实带走了人 "
            f"= {paths.get('poison')}）。"
            f"打分：{scores} ⇒ 最优集合 = {legal1}，次优 = {worst1}。"
            f"陷阱：① 官方口径「被毒不能开枪」会让模型以为毒狼王最赚（正解恰恰相反）；"
            f"② 不看公开信息、随手毒一个也会踩到好人。"
            f"开卷理由：收益函数里「毒狼王不减他的那一口」是本项目特设口径，必须查公告。",
            grader="legal", legal=legal1, forbid=worst1, board=HARD_BOARD,
            book="open", mechanic="特设", tags=["女巫", "毒药", "狼王被毒也能带"],
            giveaways=["答案是", "应该毒"]))

    # --- Z2 狼王：今晚确定被票出去，带谁 ---
    alive2 = {4, 8, 9, 10, 11}
    takes = [8, 9, 10, 11]
    king_scores = {}
    for c in takes:
        script = {"wolf_king_take": {"*": {"take": c, "reason": "剧本"}},
                  "last_words": {"*": {"speech": "（剧本遗言）", "claim": "无"}},
                  "badge": {"*": {"to": None, "reason": "剧本"}}}
        gk = _game(HARD_BOARD, KING_SEATS, alive=alive2, script=script, log=[],
                   wolf_control=False)
        gk.day = 3
        gk.phase = "day"
        _run_blocking(gk.exile(3, 4, 5.0))
        king_scores[c] = 3 if gk.winner == "wolf" else 0
    best2 = max(king_scores.values())
    legal2 = [c for c in takes if king_scores[c] == best2]
    worst2 = [c for c in takes if king_scores[c] != best2]
    if best2 > 0 and len(legal2) == 1:
        out.append(_q(
            "Z02", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人场（狼王版）。场上只剩狼王 4号 一个狼；好人这边活着的是 "
             "8号（愚者）、9号（守墓人）、10号（养鸦人）、11号（幸运儿）——"
             "预言家、女巫、摄梦人都已经出局。今天白天好人已经铁了心把 4号 投出去。"
             "本局规则公告见上。"),
            "你是狼王 4号，临死那一口带几号？请只回答数字。", legal2[0],
            f"真值来源：**逐个候选真跑一遍 `exile()`**（引擎真的结算他的带人 + 再判胜负）："
            f"打分 = {king_scores}（一刀直接赢下这一局 = 3 分，否则 0 分）⇒ "
            f"最优集合 = {legal2}，次优 = {worst2}。"
            f"推理链：神边只剩愚者 8号 一张（愚者 also_kind=villager，**民边也把他算进去**），"
            f"所以带走 8号 = 神边清空 → 按「死亡结算 → 狼王带人 → 再判屠边 + 屠边优先」"
            f"照样算狼队屠边成功；带走 9/10/11 只掉民边一张，屠边不成立。"
            f"陷阱：官方直觉「最后一狼死了就完了」会让他放弃那一口，或者随手带一个。"
            f"开卷理由：双算 + 结算次序都是本项目特设口径。",
            grader="legal", legal=legal2, forbid=worst2, board=HARD_BOARD,
            book="open", mechanic="特设", tags=["狼王", "带人", "屠边双算"],
            giveaways=["答案是", "应该带"]))
    else:
        _note(f"Z2：狼王带人的最优解不唯一（{king_scores}），弃题")

    # --- Z3 猎人：白天被票出去，开谁 ---
    g3 = _game(STANDARD_BOARD, STANDARD_SEATS, alive=set(STANDARD_SEATS))
    hunt_candidates = (2, 11)
    hunt_scores = {c: (2 if g3.seat_by_id(c).camp == "wolf" else 0) for c in hunt_candidates}
    best3 = max(hunt_scores.values())
    legal3 = [c for c in hunt_candidates if hunt_scores[c] == best3]
    worst3 = [c for c in hunt_candidates if hunt_scores[c] != best3]
    if len(legal3) == 1:
        out.append(_q(
            "Z03", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人标准场（4 狼 + 预言家、女巫、猎人、愚者 + 3 名村民）。"
             "第 2 天：7号 是猎人，他被大家投票放逐了，临死可以开枪。"
             "场上活着的人里，12 个人的位置都被人报过身份，只剩 2号 和 11号 两个人你拿不准："
             "2号 昨天带头把票投给了跳预言家的 5号（5号 后来被证实是真预言家），"
             "11号 全程没说过一句有用的话。本局规则公告见上。"),
            "你这一枪打几号？请只回答数字。", legal3[0],
            f"真值来源：枚举两个候选，按身份打分（引擎给出真实阵营）：{hunt_scores} ⇒ "
            f"最优集合 = {legal3}，次优 = {worst3}。"
            f"收益函数：打死一张**狼牌** = 2 分（少一个夜间刀口 + 少一票）；"
            f"打死好人 = 0 分（好人一共只有几个人头，白送一张）。"
            f"陷阱：官方机制（猎人被放逐可以开枪）大家都记得，难的是**别把枪打成好人的头**。"
            f"闭卷理由：本题只用官方机制（猎人开枪 + 票型读牌），不需要本板特设规则；"
            f"公告照附只为与游戏内一致。",
            grader="legal", legal=legal3, forbid=worst3, board=STANDARD_BOARD,
            book="closed", mechanic="官方", tags=["猎人", "开枪"],
            giveaways=["答案是", "应该打"]))

    # --- Z4 养鸦人：今天的诅咒开在谁头上 ---
    g4 = _game(HARD_BOARD, KING_SEATS, alive=set(KING_SEATS))
    crow_candidates = (3, 9)
    crow_scores = {c: (2 if g4.seat_by_id(c).camp == "wolf" else 0) for c in crow_candidates}
    best4 = max(crow_scores.values())
    legal4 = [c for c in crow_candidates if crow_scores[c] == best4]
    worst4 = [c for c in crow_candidates if crow_scores[c] != best4]
    if len(legal4) == 1:
        out.append(_q(
            "Z04", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人场（狼王版）。第 2 天：3号 跳预言家、说「我验了 9号 是好人」，"
             "然后他把票投给了 9号；5号 也跳预言家、说「我验了 3号 是狼人」，票投给了 3号。"
             "你是 10号 养鸦人，今天这一票之前要先决定**诅咒开在谁头上**"
             "（中咒的人这一次放逐投票**额外多一票**）。你只在 3号 和 9号 里选。本局规则公告见上。"),
            "你的诅咒开给几号？请只回答数字。", legal4[0],
            f"真值来源：枚举两个候选，按真实阵营打分：{crow_scores} ⇒ 最优集合 = {legal4}，"
            f"次优 = {worst4}。"
            f"收益函数：把多出来的那一票压在**像是狼的人**头上 = 2 分（这一票能真的把他推出去）；"
            f"压在好人头上 = 0 分（多一票可能把好人推出去，等于替狼干活）。"
            f"推理链：3号 把自己报的金水投了出去 ⇒ 他才是悍跳的那一个。"
            f"陷阱：很多人把养鸦人那张票当「随便加一票」，不看公开信息就乱开。"
            f"开卷理由：养鸦人是本项目**降为民牌**的特设牌，技能与时机都得查本板公告。",
            grader="legal", legal=legal4, forbid=worst4, board=HARD_BOARD,
            book="open", mechanic="特设", tags=["养鸦人", "诅咒"],
            giveaways=["答案是", "应该咒"]))

    # --- Z5 狼队：今晚刀谁一刀定胜负 ---
    # ⚠️ 局面必须挑成「一票就能屠边」**且其他候选都不会顺手触发控场**：
    #    狼 3 对好人 3 时，随便刀一个好人都会让 `check_win()` 走「控场」而不是「屠边」，
    #    于是会出现两个最优解（实测踩到过）—— 所以这里用 狼 2 对好人 4。
    alive5 = {1, 2, 8, 9, 10, 11}
    kill_scores = {}
    for c in sorted(alive5):
        if c in (1, 2):
            kill_scores[c] = 0                      # 刀自己人：白送轮次
            continue
        g5 = _game(HARD_BOARD, KING_SEATS, alive=alive5)
        g5.day = 3
        g5.kill(c, "wolf")
        kill_scores[c] = 3 if g5.check_win() == "wolf" else 1
    best5 = max(kill_scores.values())
    legal5 = [c for c in sorted(kill_scores) if kill_scores[c] == best5]
    worst5 = [c for c in sorted(kill_scores) if kill_scores[c] != best5]
    if best5 >= 3 and len(legal5) == 1:
        out.append(_q(
            "Z05", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人场（狼王版）。场上活着的是 1号、2号（两头狼）和 "
             "8号（愚者）、9号（守墓人）、10号（养鸦人）、11号（幸运儿）——"
             "预言家、女巫、摄梦人都已经出局。今晚狼队只有一刀，而且这一刀必定落地"
             "（对面已经没有任何保护手段）。本局规则公告见上。"),
            "你们刀几号？请只回答数字。", legal5[0],
            f"真值来源：**逐个候选模拟一次夜间结算**（`kill(t,'wolf')` 之后引擎判胜）："
            f"{kill_scores} ⇒ 最优集合 = {legal5}；次优（只掉一张牌、不能立刻结束）= {worst5}。"
            f"收益函数：① 这一刀直接屠边成功 = 3 分；② 只掉一边一张牌 = 1 分；"
            f"③ 刀到狼队友 / 自己人 = 0 分。"
            f"推理链：8号（愚者）**既算神也算民**，而神边只剩他一张 ⇒ 刀他直接屠边成功；"
            f"9/10/11 是民边，民边还有三张（愚者也在这条线上），一刀清不掉。"
            f"陷阱：愚者双算；只看官方「白痴算神」会算错。"
            f"开卷理由：双算与屠边账都是本项目特设口径。",
            grader="legal", legal=legal5, forbid=worst5, board=HARD_BOARD,
            book="open", mechanic="特设", tags=["狼队", "屠边双算"],
            giveaways=["答案是", "应该刀"]))
    else:
        _note(f"Z5：狼队一刀定胜负的最优解不唯一（{kill_scores}），弃题")

    # --- Z6 预言家：今晚验谁 ---
    g6 = _game(HARD_BOARD, KING_SEATS, alive=set(KING_SEATS))
    seer_candidates = (1, 11)
    seer_scores = {c: (2 if g6.seat_by_id(c).camp == "wolf" else 1) for c in seer_candidates}
    best6 = max(seer_scores.values())
    legal6 = [c for c in seer_candidates if seer_scores[c] == best6]
    worst6 = [c for c in seer_candidates if seer_scores[c] != best6]
    if len(legal6) == 1:
        out.append(_q(
            "Z06", "决策",
            ("【本题自成一体：下面的局面是题目给的】\n"
             "这是一局 11 人场（狼王版）。第 2 夜，你是 5号 预言家。白天 1号 是全场最急着把票"
             "带到你头上的人（他带着两个人喊「5号 是悍跳」），11号 从头到尾没说过一句有用的话。"
             "今晚你只能验 1号 或者 11号 其中一个。本局规则公告见上。"),
            "你今晚验几号？请只回答数字。", legal6[0],
            f"真值来源：枚举两个候选，按真实阵营打分：{seer_scores} ⇒ 最优集合 = {legal6}，"
            f"次优 = {worst6}。"
            f"收益函数：① 验到**狼**（能立刻带队推掉一张狼）= 2 分；"
            f"② 验到**好人**（只是一个金水，缩小狼坑）= 1 分。"
            f"陷阱：验人是「信息位」的活，**往最像狼的位置上验**才有价值；"
            f"随手验一个没人怀疑的位置等于浪费一整夜。"
            f"闭卷理由：验人规则是官方机制（每夜验一人、报阵营），不需要特设规则。",
            grader="legal", legal=legal6, forbid=worst6, board=HARD_BOARD,
            book="closed", mechanic="官方", tags=["预言家", "验人"],
            giveaways=["答案是", "应该验"]))
    return out


# --------------------------------------------------------------------------- #
# 6) S 类：13 人场专属（基线**现算**，不许沿用 11 座的 36.4%）
# --------------------------------------------------------------------------- #

N13, WOLVES13, GODS13, VILLAGERS13 = 13, 5, 5, 3


def enum_13() -> dict[str, float]:
    """13 座的位置学基线：把**所有等概率的类型排列**枚举一遍。

    与 `tools/meta_odds.py` 同一套枚举法（那里是 N=11），这里换 N=13、狼 5 / 神 5 / 纯民 3。
    ⚠️ **愚者双算不进类型排列**：它只影响屠边账（神边/民边两边都算），
       不影响「某个座位是不是狼」，所以位置学基线按 狼 5 / 神 5 / 纯民 3 算。
    ⚠️ 11 座那套数字（P(某位是狼)=36.4%、P(神)=36.4%、P(民)=27.3%）在这里**一律不许出现**。
    """
    deals: list[tuple[str, ...]] = []
    for wolves in itertools.combinations(range(N13), WOLVES13):
        rest = [i for i in range(N13) if i not in wolves]
        for gods in itertools.combinations(rest, GODS13):
            row = ["好"] * N13
            for i in wolves:
                row[i] = "狼"
            for i in gods:
                row[i] = "神"
            for i in rest:
                if i not in gods:
                    row[i] = "民"
            deals.append(tuple(row))
    total = len(deals)
    p_wolf = sum(1 for o in deals if o[0] == "狼") / total
    p_three_good = sum(1 for o in deals if all(o[i] != "狼" for i in range(3))) / total
    given = [o for o in deals if o[0] == "狼"]
    p_second = (sum(1 for o in given if o[1] == "狼") / len(given)) if given else 0.0
    # 成圈 13 个窗口里至少一处「3 连神」—— 复用 meta_odds 的判定函数（同一套枚举口径）
    windows = [tuple((s + j) % N13 for j in range(3)) for s in range(N13)]
    try:
        import meta_odds                                          # noqa: PLC0415
        has_run = meta_odds.has_run
    except Exception:                                             # noqa: BLE001
        def has_run(o, kind, starts):
            return any(all(o[i] == kind for i in w) for w in starts)
    p_three_gods = sum(1 for o in deals if has_run(o, "神", windows)) / total
    return {"deals": total, "P(1号是狼)": p_wolf,
            "P(1、2、3号全是好人)": p_three_good,
            "P(1号是狼→2号也是狼)": p_second,
            "全场至少一处3连神(成圈)": p_three_gods}


def build_s() -> list[Q]:
    """S 类：13 人场专属。构成/人头/狼坑/警徽 + 位置学基线（现算）。"""
    out: list[Q] = []
    board = thirteen_board()                    # validate_board 在这里把关
    g = _game(board, THIRTEEN_SEATS, alive=set(THIRTEEN_SEATS))
    gods, vils = _sides(g)
    wolves = sorted(s.id for s in g.alive if s.kind == "wolf")

    def kill_all(ids) -> str:
        g2 = _game(board, THIRTEEN_SEATS, alive=set(THIRTEEN_SEATS) - set(ids))
        return g2._sides_done([s for s in g2.alive if s.kind != "wolf"])

    reason_gods = kill_all(gods)
    reason_vils = kill_all(vils)
    base = (f"【本题自成一体：下面的局面是题目给的】\n"
            f"这一局是 **13 人场**，构成是：4 张普通狼 + 狼王（共 5 个狼人座位）"
            f"+ 预言家、女巫、摄梦人、愚者、猎人 + 守墓人、养鸦人、幸运儿。"
            f"（13 人场 ≥11 人，所以有警长竞选。）"
            f"⚠️ 这张 13 人板子还没有注册进内置板子表，所以上面附的规则公告是"
            f"同一套特设机制的那张 11 人板（愚者双算、狼王那一口都写在同一份规则书里）；"
            f"本局的 13 座构成以这道题的题面为准。")

    # ⚠️ 13 人板子没进 BUILTIN_BOARDS ⇒ `rules:` 只能挂同款特设片段的 11 人板（HARD_BOARD）。
    out.append(_q(
        "S01", "13人", base,
        "按本局的构成，**神边**（屠神要杀的那一边，含两边都算的牌）一共有几个人？"
        "请只回答数字。", len(gods),
        f"真值来源：按引擎的屠边口径（kind==god 或 also_kind==god）数 13 座 ⇒ {gods}。"
        f"陷阱：愚者 kind=god，本来就进神边；漏掉猎人/摄梦人也会少数。"
        f"开卷理由：13 人场构成是本项目特设的新板子（公告里没有这一张），"
        f"但屠边判定的口径（含双算）与公告同源。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["13人", "屠边"],
        giveaways=["答案是", "神边有"]))
    out.append(_q(
        "S02", "13人", base,
        "按本局的构成，**民边**（屠民要杀的那一边，含两边都算的牌）一共有几个人？"
        "请只回答数字。", len(vils),
        f"真值来源：按引擎口径（kind==villager 或 also_kind==villager）数 13 座 ⇒ {vils}。"
        f"陷阱：愚者 also_kind=villager ⇒ **民边也要把他算进去**（只数守墓人/养鸦人/幸运儿"
        f"会答 3，正解是 4）。"
        f"开卷理由：双算是本项目特设口径（公告【本板特别规则】那一段）。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["13人", "屠边", "双算"],
        giveaways=["答案是", "民边有"]))
    out.append(_q(
        "S03", "13人", base,
        "本局**狼坑**（狼人阵营）一共有几个座位？请只回答数字。", len(wolves),
        f"真值来源：按阵营数 13 座 ⇒ {wolves}（4 普通狼 + 狼王；狼王 camp=wolf）。"
        f"陷阱：官方 13 人场常见配置是 4 狼，本板多了一张狼王（狼队阵营）。"
        f"开卷理由：狼王属于狼队是本项目特设（公告【狼王】写着「狼人阵营」）。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["13人", "狼坑"],
        giveaways=["答案是"]))
    nearer = "民边" if len(vils) < len(gods) else ("神边" if len(gods) < len(vils) else "两边一样远")
    out.append(_q(
        "S04", "13人", base,
        "按屠边账，狼队离成功**更近**的是哪一边？请回答「神边」或「民边」。", nearer,
        f"真值来源：神边 {len(gods)} 人、民边 {len(vils)} 人 ⇒ 更近的是{nearer}"
        f"（引擎实测：杀光神边 {gods} 时 `_sides_done` = {reason_gods!r}；"
        f"杀光民边 {vils} 时 = {reason_vils!r}）。"
        f"开卷理由：双算口径特设（公告里写着愚者两边都算）。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["13人", "屠边"],
        giveaways=["更近的是", "答案是"]))
    out.append(_q(
        "S05", "13人", base,
        "本局有警长竞选。警长投出的那一票算几票？请只回答数字（例如 1 或 1.5）。",
        "1.5",
        "真值来源：公告【警徽】那一段与 `vote_weight()` 都是 1.5（多 0.5 票）。"
        "陷阱：有人记成「警长两票」。"
        "闭卷理由：警徽 1.5 票是官方机制，模型记忆里的答案与本项目一致。",
        board=HARD_BOARD, book="closed", mechanic="官方", tags=["13人", "警徽"],
        giveaways=["答案是", "算 1.5"]))

    # --- 位置学基线：全部**现算**（枚举全部类型排列），基线数字由 `enum_13()` 给出 ---
    stats = enum_13()
    out.append(_q(
        "S06", "13人", base,
        "发牌是均匀随机的。随机发牌后，**1号 是狼**的概率是多少？"
        "请只回答百分数（保留一位小数，例如 12.3%）。",
        f"{stats['P(1号是狼)'] * 100:.1f}%",
        f"真值来源：**现算** —— 枚举 13 座的全部类型排列（C(13,5)×C(8,5) = "
        f"{stats['deals']} 种，等概率），数到 {stats['P(1号是狼)'] * 100:.2f}%。"
        f"⚠️ 11 座的 36.4% 在这里**不许沿用**（狼 5/13 ≠ 4/11）。"
        f"闭卷理由：这是纯数学（给定构成算位置概率），不需要本板特设规则。",
        board=HARD_BOARD, book="closed", mechanic="官方", tags=["13人", "基线"],
        giveaways=["答案是", "概率是"]))
    out.append(_q(
        "S07", "13人", base,
        "随机发牌后，**1号、2号、3号 三个人全是好人**（一个狼都没有）的概率是多少？"
        "请只回答百分数（保留一位小数）。",
        f"{stats['P(1、2、3号全是好人)'] * 100:.1f}%",
        f"真值来源：**现算** —— 同一份枚举里数「前三个位置都不是狼」的排列，"
        f"得 {stats['P(1、2、3号全是好人)'] * 100:.2f}%（解析值 = C(10,5)/C(13,5)）。"
        f"⚠️ 11 座的同类数字不能拿来用（狼数与座位数都变了）。"
        f"闭卷理由：纯数学。",
        board=HARD_BOARD, book="closed", mechanic="官方", tags=["13人", "基线"],
        giveaways=["答案是", "概率是"]))
    out.append(_q(
        "S08", "13人", base,
        "随机发牌后，已知 **1号 是狼**，那么 **2号 也是狼**的概率是多少？"
        "请只回答百分数（保留一位小数）。",
        f"{stats['P(1号是狼→2号也是狼)'] * 100:.1f}%",
        f"真值来源：**现算** —— 在「1号 是狼」的那些排列里数「2号 也是狼」的比例 "
        f"= {stats['P(1号是狼→2号也是狼)'] * 100:.2f}%（解析值 = 4/12）。"
        f"陷阱：把 5/13 当答案（忘了「已经用掉一个狼位」）。"
        f"闭卷理由：纯数学。",
        board=HARD_BOARD, book="closed", mechanic="官方", tags=["13人", "基线"],
        giveaways=["答案是", "概率是"]))
    out.append(_q(
        "S09", "13人", base,
        "按屠边账，狼队想屠边成功，**至少**要杀几张好人牌？请只回答数字。",
        min(len(gods), len(vils)),
        f"真值来源：min(神边 {len(gods)}、民边 {len(vils)}) = {min(len(gods), len(vils))}"
        f"（枚举实测：{_sides_gap(set(THIRTEEN_SEATS), board, THIRTEEN_SEATS)}）。"
        f"陷阱：报「神边 5 张」（忘了民边更近），或者漏算愚者。"
        f"开卷理由：双算口径特设。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["13人", "屠边"],
        giveaways=["答案是"]))
    out.append(_q(
        "S10", "13人", base,
        "随机发牌后，「全场**至少有一处三连神**」的概率是多少？"
        "（按 13 座**成圈**算：13→1→2 也算一处三连；一处的三个人都是神职。）"
        "请只回答百分数（保留一位小数）。",
        f"{stats['全场至少一处3连神(成圈)'] * 100:.1f}%",
        f"真值来源：**现算** —— 与 `tools/meta_odds.py` 同一套枚举（换成 N=13、神 5），"
        f"13 个成圈窗口里至少一处三连神 = "
        f"{stats['全场至少一处3连神(成圈)'] * 100:.2f}%。"
        f"⚠️ 11 座的 23.3%（成圈三连神）不能沿用。"
        f"闭卷理由：纯数学。",
        board=HARD_BOARD, book="closed", mechanic="官方", tags=["13人", "基线"],
        giveaways=["答案是", "概率是"]))
    return out


# --------------------------------------------------------------------------- #
# 7) V 类：规则检索与应用（开卷，必须翻对「本局那一份」）
# --------------------------------------------------------------------------- #

def _v(text_role: str, board: str) -> bool:
    """本局那份规则公告里有没有这个角色条目（`【猎人】` 这种）。"""
    return f"【{text_role}】" in rules_text_for(board)


def build_v() -> list[Q]:
    """V 类：开卷的规则检索与应用。

    ⚠️ 规格书 §1.5 想要的是「题面附**五张板子**的规则片段，或走两步问答协议」。
    `exam.py` 的上下文机制**一组只挂一份规则文本**（`context` 只能是 `rules:<板子>`），
    所以这里用的是**等价但弱一点**的做法：每题附**本局那一份**规则公告，
    靠「同一句话在不同板子上答案不同」来考「查到的是不是本局那一份」。
    想要真·多板对照，需要给 `exam.py` 的 `context_for` 加一种「多板规则」上下文（见报告）。
    """
    out: list[Q] = []

    # V01：本局有没有猎人（狼王版 vs 官方印象里的「11 人标准场有猎人」）
    # ⚠️ 这里**不能**只查 `【猎人】` 那个角色条目：狼王版公告里还有两处提到「猎人」，
    #    但都是**条件句/类比**（「被猎人开枪带走」是狼王死因表里的一项、
    #    「如果本局有猎人，他照样能开枪」是摄梦人条目的条件句）——
    #    真正的判据是本局**角色表里有没有猎人**（`make_board().roles`），引擎也只在
    #    `role_key == "hunter"` 时才走开枪那条路。所以要问「会不会有人开那一枪」。
    board_roles = list(make_board(HARD_BOARD).roles)
    has_hunter = "hunter" in board_roles
    out.append(_q(
        "V01", "查规则",
        ("【本题自成一体】这一局是 11 人场，好人这边有三张**带技能的民牌**："
         "守墓人、养鸦人、幸运儿；神职是预言家、女巫、摄梦人、愚者。"
         "上面的规则公告就是**本局**那一份（按本局板子组装），不是别的板子。"),
        "按本局的规则公告，这一局**会不会有人开猎人那一枪**？请回答「会」或「不会」。",
        "会" if has_hunter else "不会",
        f"真值来源：本局角色表 = {board_roles} —— 里面{'有' if has_hunter else '没有'}猎人；"
        f"引擎只在 `role_key == 'hunter'` 时才走开枪那条路（对照：`{STANDARD_BOARD}` 有猎人）。"
        f"陷阱：**公告里确实出现了「猎人」这两个字**——狼王死因表里有「被猎人开枪带走」、"
        f"摄梦人条目里有「如果本局有猎人，他照样能开枪」。两句都是条件句/死因表，"
        f"**不是「本局有猎人」的声明**；只抓关键词就会答错，必须翻到本局的角色条目去确认。"
        f"开卷理由：V 类就是考「会不会查、查的是不是本局那一份」。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["V", "板子识别"],
        giveaways=["答案是", "没有猎人"]))
    # V02：标准板上会不会出现「摄梦 + 解药 = 奶穿」
    has_dreamer = _v("摄梦人", STANDARD_BOARD)
    out.append(_q(
        "V02", "查规则",
        ("【本题自成一体】这一局是 11 人场，好人这边有猎人、愚者和三名普通村民。"
         "上面的规则公告就是**本局**那一份。"
         "（顺便说一句：公告的【术语速查】里确实收着「奶穿」这个词。）"),
        "按本局的规则公告，本局会不会出现「摄梦 + 解药 = 奶穿」这种死法？"
        "请回答「会」或「不会」。", "会" if has_dreamer else "不会",
        f"真值来源：本局规则公告里有没有【摄梦人】= {has_dreamer}"
        f"（没有摄梦人 ⇒ 这条死法在本局不可能出现）。"
        f"陷阱：公告里出现「奶穿」这个词（术语速查），只抓到关键词就答「会」是错的 —— "
        f"必须**翻到本局的角色条目**确认没有摄梦人。"
        f"开卷理由：这就是 V 类要测的「查到了关键词 ≠ 查对了本局那一份」。",
        board=STANDARD_BOARD, book="open", mechanic="特设", tags=["V", "板子识别"],
        giveaways=["答案是", "会出现"]))
    # V03：狼王版会不会出现「同守同救」
    # ⚠️ 同样**不能**只查 `【守卫】` 条目：狼王版公告的摄梦人条目里有一句
    #    「你出局不会带走梦游者（跟守卫一样：守卫死了，被守的人照样活着）」，
    #    那是**类比**，不是「本局有守卫」。判据同样取本局角色表。
    has_guard = "guard" in list(make_board(HARD_BOARD).roles)
    out.append(_q(
        "V03", "查规则",
        ("【本题自成一体】这一局是 11 人场，神职是预言家、女巫、摄梦人、愚者。"
         "上面的规则公告就是**本局**那一份。"),
        "按本局的规则公告，本局会不会出现「同守同救」（守卫守 + 女巫解药救同一个人 → "
        "照样出局）这种死法？请回答「会」或「不会」。", "会" if has_guard else "不会",
        f"真值来源：本局角色表 = {list(make_board(HARD_BOARD).roles)} —— "
        f"{'有' if has_guard else '没有'}守卫 ⇒ 没有守卫就不存在「守 + 救」这种组合。"
        f"陷阱：公告的【术语速查】里写着「奶穿（同守同救、摄梦+解药都算）」，"
        f"摄梦人条目里还有一句「跟守卫一样」—— 这两处只是**术语与类比**，"
        f"不代表本局有守卫；只抓关键词就会答错。"
        f"开卷理由：V 类考检索本局那一份。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["V", "板子识别"],
        giveaways=["答案是", "会出现"]))
    # V04：首夜无刀那张板子的平安夜是什么原因
    quiet_board = "首夜无刀" in rules_text_for(HARD_BOARD)
    out.append(_q(
        "V04", "查规则",
        ("【本题自成一体】这一局是 11 人场（狼王版）。第 1 天早上公告写："
         "「昨晚是平安夜，没有人出局。」上面的规则公告就是**本局**那一份。"),
        "按本局的规则公告，这个平安夜最可能的原因是什么？请回答"
        "「本板第一夜本来没有狼刀」或「女巫用解药救中了」。",
        "本板第一夜本来没有狼刀" if quiet_board else "女巫用解药救中了",
        f"真值来源：本局公告里有没有【首夜无刀】那一段 = {quiet_board}。"
        f"陷阱：官方印象是「平安夜 = 女巫救人 / 守卫守中」，"
        f"本板第一夜狼队根本不行动（也没有银水）。"
        f"开卷理由：首夜无刀是特设板子级规则，公告里明写「不是守中、也不是女巫用药的结果」。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["V", "首夜无刀"],
        giveaways=["首夜无刀", "答案是"]))
    # V05 / V06：同一句话在两张板子上答案不同（民边有没有双算的牌）
    g_eleven = _game(ELEVEN_BOARD, ELEVEN_SEATS, alive=set(ELEVEN_SEATS) - {9, 10, 11})
    reason_eleven = g_eleven._sides_done([s for s in g_eleven.alive if s.kind != "wolf"])
    g_std = _game(STANDARD_BOARD, STANDARD_SEATS, alive=set(STANDARD_SEATS) - {9, 10, 11})
    reason_std = g_std._sides_done([s for s in g_std.alive if s.kind != "wolf"])
    same_q = ("本局里 9号、10号、11号 就是那三张**村民牌**。狼队把这三位全部杀光之后，"
              "狼队的**屠民**成功了吗？请回答「成功」或「没成功」。")
    out.append(_q(
        "V05", "查规则",
        ("【本题自成一体】这一局是 11 人场，构成是 4 狼 + 预言家、女巫、猎人、守卫 + "
         "3 名普通村民。上面的规则公告就是**本局**那一份。"),
        same_q, "成功" if reason_eleven else "没成功",
        f"真值来源：真跑引擎的屠边判定（把 {[9, 10, 11]} 标成出局）→ `_sides_done` = "
        f"{reason_eleven!r}。本板没有「两边都算」的牌，所以三张村民死光 = 屠民成功。"
        f"陷阱：官方/别的板子的双算规则会被记混到这里来。"
        f"开卷理由：本局有没有双算的牌，必须翻本局那一份公告（本板没有愚者）。",
        board=ELEVEN_BOARD, book="open", mechanic="特设", tags=["V", "屠边", "板子识别"],
        giveaways=["答案是", "屠民成功"]))
    out.append(_q(
        "V06", "查规则",
        ("【本题自成一体】这一局是 11 人场，构成是 4 狼 + 预言家、女巫、猎人、愚者 + "
         "3 名普通村民。上面的规则公告就是**本局**那一份。"),
        same_q, "成功" if reason_std else "没成功",
        f"真值来源：真跑引擎的屠边判定（把 {[9, 10, 11]} 标成出局）→ `_sides_done` = "
        f"{reason_std!r}。⚠️ 本板有愚者，他 also_kind=villager ⇒ 民边没清空。"
        f"陷阱：**同一句话在 V05 那张板子上答案是「成功」** —— 只看关键词、"
        f"不翻本局那一份就会答错。"
        f"开卷理由：双算口径特设，公告【本板特别规则】那一段。",
        board=STANDARD_BOARD, book="open", mechanic="特设", tags=["V", "屠边", "双算"],
        giveaways=["答案是", "屠民成功"]))
    # V07：狼王结算次序（开卷），与 X08 同一个机制、不同问法（看谁真读了公告）
    winner_v7 = _x_king_order_winner()
    out.append(_q(
        "V07", "查规则",
        ("【本题自成一体】这一局是 11 人场（狼王版）。局面：只剩狼王 4号 一个狼，"
         "好人还活着 8号（愚者）、9号（守墓人）、10号（养鸦人）、11号（幸运儿）——"
         "预言家、女巫、摄梦人都已出局，**神边只剩愚者这一张**。"
         "今天好人把狼王投票放逐，他临死带走了 8号。上面的规则公告就是**本局**那一份。"),
        "按本局的规则公告，这一局谁赢了？请回答「狼人阵营获胜」或「好人阵营获胜」。",
        winner_v7,
        f"真值来源：真跑 `exile()`（剧本 take=8）→ winner={winner_v7}。"
        f"陷阱：公告的【结算次序】那一段就是答案所在（死亡结算 → 狼王带人 → 再判屠边；"
        f"边界口径屠边优先）；不读公告、按官方「狼队清空就输」的直觉会答反。"
        f"开卷理由：特设口径，必须查公告。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["V", "狼王", "结算次序"],
        giveaways=["狼队赢", "好人赢", "答案是"]))
    out.append(_q(
        "V08", "查规则",
        ("【本题自成一体】这一局是 11 人场（狼王版），9号 是守墓人。"
         "第 2 天白天，大家投票把 3号（狼人）放逐了出去。第 2 天的发言轮开始时，"
         "上面那份规则公告就是**本局**那一份。"),
        "9号 会**自动**拿到什么信息？请回答「3号 是狼人」或「他不会拿到任何信息」。",
        "3号 是狼人",
        "真值来源：真跑 `_tell_gravekeeper(第2天)`（先把 last_exiled 设成 3号）→ "
        "守墓人的私有信息里写着「昨天被放逐的 3号 身份是【狼人】」（只报阵营）。"
        "陷阱：官方**没有守墓人这张牌**（本项目把官方的神牌降成了民牌并改了技能时机）——"
        "只靠官方记忆答「不会拿到任何信息」是错的。"
        "开卷理由：这是本项目特设的民牌技能，必须查公告【守墓人】那一段。",
        board=HARD_BOARD, book="open", mechanic="特设", tags=["V", "守墓人"],
        giveaways=["9号 会", "答案是"]))
    return out


# --------------------------------------------------------------------------- #
# 8) 组装与自检
# --------------------------------------------------------------------------- #

#: 六类的题量下限（闸门按这个判红）。
MIN_PER_CLASS = 6


def build_hard_bank(board=None, seed: int = 0, games: int = 1) -> list[Q]:
    """出整套高难度题。

    `board`：W/X/Z 用哪张 11 人板子（默认 = `config.yaml` 的默认板子）。
        ⚠️ 只有**构成与狼王版一致**的板子才用它 —— 否则退回狼王版并记一条 note
        （题目里写的角色表必须和引擎里的座位表一致，不然就是老题库那种「过期题」）。
    `seed`：只影响出题顺序与 Y 类半局的随机源（答案本身由引擎现算，不靠 seed）。
    `games`：Y 类最多构造几局半局。**默认 1 也至少构造 2 局** ——
        一局的公开信息凑不出 6 道互不重复的对账题（规格书要求每类 ≥6）。
    """
    VERIFIED.clear()
    NOTES.clear()
    picked = board or default_board()
    try:
        picked_roles = sorted(make_board(picked).roles)
    except KeyError:
        picked_roles = []
    if picked_roles != sorted(KING_SEATS.values()):
        if board is not None:
            _note(f"传入的板子 {picked!r} 构成与狼王版不一致，W/X/Z 改回 {HARD_BOARD}")
        picked = HARD_BOARD
    bank: list[Q] = []
    bank += build_w(seed)
    bank += build_x()
    bank += build_y(seed)
    bank += build_z()
    bank += build_s()
    bank += build_v()
    _ = games                                     # 见 docstring：至少 2 局，参数只留接口
    # 题号唯一性（撞号会让模型分不清在问哪道）
    seen: dict[str, int] = {}
    for q in bank:
        seen[q.qid] = seen.get(q.qid, 0) + 1
    for qid, n in seen.items():
        if n > 1:
            _note(f"题号 {qid} 出现了 {n} 次")
    return bank


def notes() -> list[str]:
    """出题过程中记下的问题（闸门与自检都会打印）。"""
    return list(NOTES)


def verified_qids() -> set[str]:
    """最近一次出题里**真的跑过引擎校验**的题号。"""
    return set(VERIFIED)


_CLASSES = ("轮次", "边界", "对账", "决策", "13人", "查规则")


def main() -> int:
    bank = build_hard_bank()
    by_cat: dict[str, int] = {}
    for q in bank:
        by_cat[q.category] = by_cat.get(q.category, 0) + 1
    print("=" * 78)
    print(f"高难度题库：{len(bank)} 道题")
    print("   " + "　".join(f"{c} {by_cat.get(c, 0)} 道" for c in _CLASSES))
    thin = {c: by_cat.get(c, 0) for c in _CLASSES if by_cat.get(c, 0) < MIN_PER_CLASS}
    open_n = sum(1 for q in bank if getattr(q, "book", "") == "open")
    print(f"   开卷（依赖本项目特设机制）{open_n} 道 / 闭卷（只用官方机制）"
          f"{len(bank) - open_n} 道；全部题的上下文都是 rules:<本局板子>")
    print("=" * 78)
    for q in bank:
        print(f"\n[{q.qid}·{q.category}·{q.grader}·{getattr(q, 'book', '?')}] "
              f"{getattr(q, 'scenario', '')[:60]}…")
        print(f"    问：{getattr(q, 'ask', q.question)[:90]}")
        print(f"    期望答案：{q.expect}　（{q.why[:110]}…）")
    print("\n" + "=" * 78)
    for n in NOTES:
        print("   ⚠️ " + n)
    if thin:
        print("\n❌ 题量不够（每类至少要 %d 道）：" % MIN_PER_CLASS)
        for c, n in thin.items():
            print(f"   {c}：{n} 道")
        return 1
    if NOTES:
        print("\n❌ 出题自检有问题（引擎行为与题库答案不一致，必须先修）")
        return 1
    print(f"\n✅ 自检通过：{len(bank)} 道题，六类都 ≥{MIN_PER_CLASS} 道，"
          f"答案全部由引擎现算（{len(verified_qids())} 道登记为 engine_checked）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
