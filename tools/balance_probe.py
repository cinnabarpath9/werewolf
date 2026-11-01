# coding=utf-8
"""轮次探针：量「狼队的优势里，有多少是**结构性的**、有多少是**好人乱投票**造成的」。

用户 2026-10-09 的问题：`t_mock30` 里狼队胜率 93~97%，这是"随机代理乱投票"的假象，
还是 11 人板本来就偏向狼？光看胜率分不清 —— 所以要**做对照实验**：

  · `baseline`：两边都用 MockAgent（现状口径）。
  · `oracle`：**好人侧开天眼** —— 投票、女巫毒药、猎人开枪一律打**真狼**（其余照旧随机）。
    如果连开天眼的好人都赢不了，说明输在**结构**（轮次/顺序），不是输在投票准度。
  · `oracle-vote`：只有投票开天眼，药和枪照旧随机（区分"信息"与"主动追刀"各值多少）。
  · `wolf-blind`：狼也乱投票（不做绑票）—— 量"狼队协同"值几个点。

再顺带统计（这些数字才是"为什么"）：
  · 平均天数、放逐命中率（票出去的是不是狼）、
  · 死因分布（被刀 / 被票 / 被毒 / 被枪）、
  · 屠边是走神还是走民。

跑法：`python tools/balance_probe.py --games 200`（mock 每局 ~0.05 秒，200 局几秒）。
"""
from __future__ import annotations

import argparse
import collections
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import MockAgent                            # noqa: E402
from wolf.engine import Game                                 # noqa: E402
from wolf.roles import make_board                            # noqa: E402


class ProbeAgent(MockAgent):
    """MockAgent + 可选的"给某一侧开天眼"。

    `oracle_good`：好人侧的**投票/毒/枪**都打真狼（模拟"信息完美、判断从不出错"的好人）。
    `wolf_blind`：狼侧投票也纯随机（模拟"狼队不绑票、不带队"）。
    其余行为完全照 MockAgent（保证与 `t_mock30` 的口径可比）。
    """

    def __init__(self, seat, rng, holder, oracle_good=False, wolf_blind=False,
                 vote_hit: float | None = None, wolf_aim: str | None = None,
                 exile_hit: float | None = None, wolf_smart: float | None = None,
                 guard_save: float | None = None, poison_hit: float | None = None,
                 hunter_hit: float | None = None,
                 dream_save: float | None = None,
                 dream_kill: float | None = None) -> None:
        """`vote_hit` = 好人投票命中真狼的概率（None = 不干预、纯随机）。

        有它才能画一条**命中率曲线**：好人要投多准，狼胜率才回到五五开。
        `oracle_good=True` 更强：连女巫的毒和猎人的枪都开天眼。
        """
        super().__init__(seat, rng)
        self.holder = holder
        self.oracle_good = oracle_good
        self.wolf_blind = wolf_blind
        self.vote_hit = vote_hit
        #: 狼刀质量（回答"探针里狼刀是不是纯随机"）：
        #:   None      = 在好人里纯随机（**基线，比真狼弱**：不抿身份、不盯预言家、不按屠边集中）
        #:   "side"    = 完美屠边记账：往"人少的那一边"集中刀（但仍不知道具体身份）
        #:   "oracle"  = 连身份都知道：按 预言家 > 女巫 > 猎人 > 白痴 的优先级刀
        self.wolf_aim = wolf_aim
        #: —— 用户 2026-10-09 指定的"接近真实对局"那组概率（都是结果概率）——
        #: `exile_hit`：每次放逐**落在狼身上**的概率（所有座位按同一个目标投，保证结果可控）
        #: `wolf_smart`：狼刀有这一比例去刀"**剩余数量最少的那一边**"，其余随机刀好人
        #: `guard_save`：守卫守中当晚刀口的概率（= 平安夜）
        #: `poison_hit`：女巫开毒时**毒中狼**的概率（否则毒到一个好人）
        self.exile_hit = exile_hit
        self.wolf_smart = wolf_smart
        self.guard_save = guard_save
        self.poison_hit = poison_hit
        #: `hunter_hit`：猎人开枪**打中狼**的概率（不给 = 照 mock 的做法不开枪，用来对照"猎人版 vs 守卫版"）
        self.hunter_hit = hunter_hit
        #: `dream_save`：摄梦人**摄中当晚刀口**的概率（= 平安夜）
        #: `dream_kill`：摄梦人**连摄同一头狼**、把他梦死的概率（15% = 每夜掷一次）
        self.dream_save = dream_save
        self.dream_kill = dream_kill
        self._my_exile: dict[int, int] = {}          # 每个座位今天被指派投谁

    def _living_wolves(self) -> list[int]:
        game = self.holder.get("game")
        if game is None:
            return []
        return [s.id for s in game.alive if s.camp == "wolf"]

    def _wolf_pick(self, legal: list[int]) -> int | None:
        """狼刀目标。

        · `wolf_smart`（用户口径）：以这一概率刀"**剩余数量最少的那一边**"（精确刀），
          其余时候在好人里随机；
        · 老参数 `wolf_aim`："side" = 永远刀人少的那边，"oracle" = 连身份都知道。
        """
        game = self.holder.get("game")
        if game is None:
            return None
        if self.wolf_smart is not None:
            good = [s for s in game.alive if s.camp != "wolf" and s.id in legal]
            if not good:
                return None
            if self.rng.random() >= self.wolf_smart:
                return self.rng.choice([s.id for s in good])          # 这刀不精确
            # 精确刀：数**剩余人头**，往少的那一边刀（白痴两边都算 → 两边各减一次）
            gods = [s for s in good if s.role_key != "villager"]
            vills = [s for s in good if s.role_key == "villager"]
            side = gods if len(gods) <= len(vills) else vills
            return self.rng.choice([s.id for s in (side or good)])
        if not self.wolf_aim:
            return None
        good = [s for s in game.alive if s.camp != "wolf" and s.id in legal]
        if not good:
            return None
        if self.wolf_aim == "oracle":
            order = {"seer": 0, "witch": 1, "hunter": 2, "idiot": 3, "villager": 4}
            best = min(order.get(s.role_key, 9) for s in good)
            cand = [s.id for s in good if order.get(s.role_key, 9) == best]
            return self.rng.choice(cand)
        # "side"：数两边人头，往少的那边刀（神边 = 预言家/女巫/猎人/白痴；民边 = 村民 + 白痴）
        gods = [s for s in good if s.role_key != "villager"]
        vills = [s for s in good if s.role_key == "villager"]
        side = gods if len(gods) <= len(vills) else vills
        return self.rng.choice([s.id for s in (side or good)])

    def _oracle_pick(self, legal: list[int]) -> int | None:
        hits = [i for i in self._living_wolves() if i in legal]
        return self.rng.choice(hits) if hits else None

    async def ask(self, packet, task):
        kind = task.kind
        legal = list(task.legal)
        mine = self.seat.camp == "wolf"
        if self.oracle_good and not mine:
            # 投票 / 毒 / 枪：有真狼可选就选真狼
            if kind in ("vote", "sheriff_vote", "vote_intent"):
                hit = self._oracle_pick(legal)
                if hit is not None:
                    if kind == "vote_intent":
                        return {"target": hit, "reason": "（探针）打真狼", "confidence": "high"}
                    return {"vote": hit, "reason": "（探针）打真狼"}
            if kind == "witch":
                hit = self._oracle_pick([i for i in legal if i != self.seat.id])
                if hit is not None and self.rng.random() < 0.9:
                    return {"save": False, "poison": hit, "reason": "（探针）毒真狼"}
            if kind == "hunter_shoot":
                hit = self._oracle_pick(legal)
                if hit is not None:
                    return {"shoot": hit, "reason": "（探针）枪打真狼"}
            if kind == "analyze":                     # 投票前那次权威决策：直接给真狼
                hit = self._oracle_pick(legal)
                reads = [{"seat": i, "camp": ("wolf" if i in self._living_wolves() else "good"),
                          "role": "未知", "why": "（探针）"} for i in legal]
                return {"reads": reads, "summary": "（探针）",
                        "tiers": [{"seat": i, "tier": "中", "good_reason": "-",
                                   "wolf_reason": "-"} for i in legal],
                        "vote": hit if hit is not None else self._pick(legal),
                        "reason": "（探针）打真狼"}
            if kind == "speech_plan":                 # 「决策+发言」合并那条路
                hit = self._oracle_pick(legal)
                reads = [{"seat": i, "camp": ("wolf" if i in self._living_wolves() else "good"),
                          "role": "未知", "why": "（探针）"} for i in legal]
                return {"reads": reads, "summary": "（探针）", "tiers": [],
                        "protect": [], "attack": [hit] if hit is not None else [],
                        "claim": "无", "stance": "-", "role_plan": "-",
                        "vote": hit if hit is not None else self._pick(legal),
                        "speech": "（探针）", "reason": "（探针）打真狼"}
        if self.wolf_blind and mine and kind in ("vote", "vote_intent", "sheriff_vote"):
            # 狼不绑票：从"非狼"的合法目标里随机（等于放弃带队）
            good = [i for i in legal if i not in self._living_wolves()]
            if good:
                who = self.rng.choice(good)
                if kind == "vote_intent":
                    return {"target": who, "reason": "（探针）乱投", "confidence": "low"}
                return {"vote": who, "reason": "（探针）乱投"}
        if mine and kind in ("wolf_kill", "wolf_intent", "wolf_talk"):
            aim = self._wolf_pick(legal)
            if aim is not None:
                reply0 = await super().ask(packet, task)
                if isinstance(reply0, dict):
                    if kind == "wolf_talk":
                        reply0["target"] = aim
                    else:
                        reply0["target"] = aim
                return reply0
        # —— 用户口径：**好人推走狼的概率 = exile_hit** ——
        # 做法：每天先掷一次骰子决定"今天被推出去的是谁"（50% 一头狼 / 50% 一个好人），
        # 然后**所有座位都投他** —— 这样"放逐结果"这个概率是可控的，而不是靠单票准度间接凑。
        if self.exile_hit is not None and kind in ("vote", "vote_intent", "analyze",
                                                   "speech_plan", "sheriff_vote"):
            game = self.holder.get("game")
            if game is not None and kind != "sheriff_vote":
                day = game.day
                # ⚠️ 这里以前写了 `self.seat_id` —— 代理上根本没有这个属性 → AttributeError
                #    被引擎按「调用失败→按规则随机处理」吞掉，表现为「旋钮没生效」。
                #    教训：探针把 on_event 关掉之后，这种异常是完全静音的。
                chosen = self.holder.setdefault("_exile_today", {}).get(day)
                if chosen is None or not game.seat_by_id(chosen).alive:
                    pool = [s for s in game.alive if s.id in legal]
                    wolves = [s.id for s in pool if s.camp == "wolf"]
                    goods = [s.id for s in pool if s.camp != "wolf"]
                    if wolves and self.rng.random() < self.exile_hit:
                        chosen = self.rng.choice(wolves)
                    elif goods:
                        chosen = self.rng.choice(goods)
                    else:
                        chosen = self.rng.choice(pool) if pool else None
                    self.holder.setdefault("_exile_today", {})[day] = chosen
                if chosen is not None:
                    reply0 = await super().ask(packet, task)
                    if isinstance(reply0, dict):
                        if kind == "vote_intent":
                            reply0["target"] = chosen
                        else:
                            reply0["vote"] = chosen
                    return reply0
        # —— 摄梦人：摄中刀口（平安夜）/ 连摄梦死狼人 ——
        if (self.dream_save is not None or self.dream_kill is not None) and kind == "dream":
            game = self.holder.get("game")
            reply0 = await super().ask(packet, task)
            if isinstance(reply0, dict) and game is not None:
                import re as _re
                victim = None
                for e in reversed(game.ledger.entries):
                    if e.kind == "night" and "最终决定击杀" in (e.text or ""):
                        m = _re.search(r"最终决定击杀\s*(\d+)\s*号", e.text)
                        if m:
                            victim = int(m.group(1))
                        break
                pool = [s for s in game.alive if s.id in task.legal]
                wolves = [s.id for s in pool if s.camp == "wolf"]
                goods = [s.id for s in pool if s.camp != "wolf"]
                pick = None
                # ① 守中：摄当晚刀口（这一夜他不死 → 平安夜）
                if (victim is not None and victim in task.legal
                        and self.rng.random() < (self.dream_save or 0)):
                    pick = victim
                # ② 梦死狼：盯住同一头狼连摄两夜（第二夜他死）
                elif wolves and self.rng.random() < (self.dream_kill or 0):
                    prev = self.holder.get("_dream_wolf")
                    if prev in wolves:
                        pick = prev                      # 第二夜：连摄 → 梦死
                        self.holder["_dream_wolf"] = None
                    else:
                        pick = self.rng.choice(wolves)   # 第一夜：先盯上他
                        self.holder["_dream_wolf"] = pick
                # ③ 其余：盲摄一个好人（大概率白摄，用来体现"没有信息时的浪费"）
                elif goods:
                    pick = self.rng.choice(goods)
                if pick is not None:
                    reply0["target"] = pick
            return reply0
        # —— 守卫：守中刀口（平安夜）的概率 = guard_save ——
        if self.guard_save is not None and kind == "guard":
            game = self.holder.get("game")
            # 当晚刀口：狼队出刀汇总在守卫行动**之前**就写进台账了（kind="night"），
            # 探针从那里读 —— 这是"模拟 15% 守中"这个概率所必需的偷看，不是产品行为。
            victim = None
            if game is not None:
                import re as _re
                for e in reversed(game.ledger.entries):
                    if e.kind == "night" and "最终决定击杀" in (e.text or ""):
                        m = _re.search(r"最终决定击杀\s*(\d+)\s*号", e.text)
                        if m:
                            victim = int(m.group(1))
                        break
            reply0 = await super().ask(packet, task)
            if isinstance(reply0, dict):
                if victim is not None and victim in legal and self.rng.random() < self.guard_save:
                    reply0["target"] = victim
                elif victim is not None and victim not in legal:
                    # 规则不允许连守同一个人 → 这一夜守不住（如实体现）
                    reply0["target"] = self._pick(legal)
            return reply0
        # —— 猎人：开枪打中狼的概率 = hunter_hit（不给则照 mock：不开枪）——
        if self.hunter_hit is not None and kind == "hunter_shoot":
            game = self.holder.get("game")
            reply0 = await super().ask(packet, task)
            if isinstance(reply0, dict) and game is not None:
                pool = [s for s in game.alive if s.id in legal]
                wolves = [s.id for s in pool if s.camp == "wolf"]
                goods = [s.id for s in pool if s.camp != "wolf"]
                if wolves and self.rng.random() < self.hunter_hit:
                    reply0["shoot"] = self.rng.choice(wolves)
                elif goods:
                    reply0["shoot"] = self.rng.choice(goods)
            return reply0
        # —— 女巫：开毒时**毒中狼**的概率 = poison_hit（否则毒到一个好人）——
        if self.poison_hit is not None and kind == "witch":
            game = self.holder.get("game")
            reply0 = await super().ask(packet, task)
            if isinstance(reply0, dict) and game is not None:
                pool = [s for s in game.alive if s.id in legal and s.id != self.seat.id]
                wolves = [s.id for s in pool if s.camp == "wolf"]
                goods = [s.id for s in pool if s.camp != "wolf"]
                has_poison = getattr(self.holder.get("game"), "witch_poison", True)
                if has_poison and pool and (game.day or 1) >= 1:
                    if wolves and self.rng.random() < self.poison_hit:
                        reply0["poison"] = self.rng.choice(wolves)
                    elif goods:
                        reply0["poison"] = self.rng.choice(goods)
            return reply0
        reply = await super().ask(packet, task)
        vote_kinds = ("vote", "vote_intent")      # 只放逐票；投警长票不干预
        if (self.vote_hit is not None and not mine and kind in vote_kinds
                and self.rng.random() < self.vote_hit):
            hit = self._oracle_pick(legal)
            if hit is not None and isinstance(reply, dict):
                if kind == "vote_intent":
                    reply["target"] = hit
                else:
                    reply["vote"] = hit
        return reply


def play(seed: int, mode: str, board: str = "eleven_standard",
         decision_first: bool = True, vote_hit: float | None = None,
         wolf_aim: str | None = None, exile_hit: float | None = None,
         wolf_smart: float | None = None, guard_save: float | None = None,
         poison_hit: float | None = None, hunter_hit: float | None = None,
         dream_save: float | None = None, dream_kill: float | None = None
         ) -> tuple[str, int, dict]:
    """跑一局，返回 (赢家, 天数, 统计)。"""
    holder: dict = {}
    stats = collections.Counter()
    board_def = make_board(board)
    game = Game(
        board=board_def, seed=seed, decision_first=decision_first, max_days=8,
        agent_factory=lambda seat: ProbeAgent(
            seat, random.Random(seed * 100 + seat.id), holder,
            oracle_good=mode == "oracle",
            wolf_blind=mode == "wolf-blind",
            vote_hit=vote_hit, wolf_aim=wolf_aim, exile_hit=exile_hit,
            wolf_smart=wolf_smart, guard_save=guard_save, poison_hit=poison_hit,
            hunter_hit=hunter_hit, dream_save=dream_save, dream_kill=dream_kill),
        on_event=lambda *a, **k: None,
    )
    holder["game"] = game
    import asyncio
    asyncio.run(game.run())

    for seat in game.seats:
        if seat.alive:
            continue
        stats[f"死因:{seat.death_cause}"] += 1
        stats[f"死者阵营:{seat.camp}"] += 1
    # ⚠️ 放逐命中率只算**真的被投出去的人**（死因 = vote）。按"每一票"算会把同一个人
    #    的很多张票重复计数 —— 第一版就这么误标了（算出来"20 次放逐/局"）。
    for seat in game.seats:
        if seat.death_cause == "vote":
            stats["放逐:是狼" if seat.camp == "wolf" else "放逐:是好人(票错)"] += 1
    for e in game.ledger.entries:                 # 单票准度另算（跟放逐结果不是一回事）
        if e.kind == "vote" and not (e.data or {}).get("sheriff_vote"):
            target = (e.data or {}).get("target")
            if target:
                who = game.seat_by_id(target)
                stats["票:打狼" if who.camp == "wolf" else "票:打好人"] += 1
    return (game.winner or "draw"), game.day, dict(stats)


def report(mode: str, n: int, board: str = "eleven_standard",
           vote_hit: float | None = None, wolf_aim: str | None = None,
           exile_hit: float | None = None, wolf_smart: float | None = None,
           guard_save: float | None = None, poison_hit: float | None = None,
           hunter_hit: float | None = None, dream_save: float | None = None,
           dream_kill: float | None = None) -> dict:
    wins: collections.Counter = collections.Counter()
    days: list[int] = []
    agg: collections.Counter = collections.Counter()
    for seed in range(1, n + 1):
        w, d, st = play(seed, mode, board, vote_hit=vote_hit, wolf_aim=wolf_aim,
                        exile_hit=exile_hit, wolf_smart=wolf_smart,
                        guard_save=guard_save, poison_hit=poison_hit, hunter_hit=hunter_hit,
                        dream_save=dream_save, dream_kill=dream_kill,
                        # ⚠️ 受控实验：开 curve 时关掉"决策复用"，让每一票都真的走 vote 任务。
                        #    不这么做的话，票是从 analyze/speech_plan 的 decision 里复用的，
                        #    干预点跟实际投票不是同一个地方（第一版得到过一条完全平的曲线）。
                        decision_first=vote_hit is None)
        wins[w] += 1
        days.append(d)
        agg.update(st)
    tot_votes = agg["放逐:是狼"] + agg["放逐:是好人(票错)"]
    hit = (agg["放逐:是狼"] / tot_votes) if tot_votes else 0.0
    print(f"\n=== 模式 {mode}（{n} 局，{board}）===")
    print(f"  狼胜 {wins['wolf']:>3} / 好人胜 {wins['good']:>3} / 平 {wins['draw']:>3}"
          f"　→ 狼胜率 {wins['wolf'] / n:.0%}")
    print(f"  平均天数 {sum(days) / len(days):.2f}（最长 {max(days)}）"
          f"　｜　decision_first={vote_hit is None}")
    votes_all = agg["票:打狼"] + agg["票:打好人"]
    print(f"  放逐命中率（真被投出去的是狼）：{hit:.0%}（{agg['放逐:是狼']} / {tot_votes} 次放逐）"
          f"　｜　单票命中率：{(agg['票:打狼'] / votes_all) if votes_all else 0:.0%}")
    causes = {k.split(":")[1]: v for k, v in agg.items() if k.startswith("死因:")}
    camps = {k.split(":")[1]: v for k, v in agg.items() if k.startswith("死者阵营:")}
    print(f"  死因分布：{causes}")
    print(f"  死者阵营：{camps}")
    return {"mode": mode, "wolf_rate": wins["wolf"] / n, "hit": hit,
            "days": sum(days) / len(days)}


def main() -> int:
    ap = argparse.ArgumentParser(description="轮次/平衡探针（对照实验：随机 vs 开天眼）")
    ap.add_argument("--games", type=int, default=200, help="每种模式跑多少局（默认 200）")
    ap.add_argument("--board", default="eleven_wolfking")
    ap.add_argument("--modes", default="baseline,oracle,wolf-blind",
                    help="逗号分隔：baseline / oracle / wolf-blind")
    ap.add_argument("--exile-hit", type=float, default=None,
                    help="每次放逐落在狼身上的概率（例 0.5；不给则用 mock 的随机投票）")
    ap.add_argument("--wolf-smart", type=float, default=None,
                    help="狼刀有这一比例去刀『剩余数量最少的那一边』（例 0.5）")
    ap.add_argument("--guard-save", type=float, default=None,
                    help="守卫守中当晚刀口的概率=平安夜（例 0.15；只在带守卫的板子有意义）")
    ap.add_argument("--dream-save", type=float, default=None,
                    help="摄梦人摄中当晚刀口（= 平安夜）的概率，例 0.15")
    ap.add_argument("--dream-kill", type=float, default=None,
                    help="摄梦人连摄同一头狼、把他梦死的概率，例 0.15")
    ap.add_argument("--hunter-hit", type=float, default=None,
                    help="猎人开枪打中狼的概率（例 0.3；不给 = 不开枪，用于猎人版对照）")
    ap.add_argument("--poison-hit", type=float, default=None,
                    help="女巫开毒时毒中狼的概率（例 0.3）")
    ap.add_argument("--wolf-aim", default="", choices=["", "side", "oracle"],
                    help="狼刀质量：留空=好人里纯随机（基线）／side=完美屠边记账／oracle=连身份都知道")
    ap.add_argument("--curve", default="",
                    help="命中率曲线：逗号分隔的一串概率，例 0.4,0.5,0.6,0.7,0.8")
    args = ap.parse_args()
    if args.curve:
        print("======== 好人投票命中率 → 狼胜率（其余行为同 mock）========")
        print(f"{'命中率':>8}{'狼胜率':>9}{'好人胜率':>10}{'平均天数':>10}")
        base = report("curve-baseline(df=False)", args.games, args.board, vote_hit=0.0,
                      wolf_aim=(args.wolf_aim or None))
        print(f"→ 命中率 0%（同口径基准：纯随机投；狼刀={args.wolf_aim or '纯随机'}）"
              f"狼胜率 {base['wolf_rate']:.0%}")
        for p in [float(x) for x in args.curve.split(",") if x.strip()]:
            r = report("curve", args.games, args.board, vote_hit=p,
                       wolf_aim=(args.wolf_aim or None))
            print(f"→ 命中率 {p:.0%}：狼胜率 {r['wolf_rate']:.0%}")
    if any(x is not None for x in (args.exile_hit, args.wolf_smart, args.guard_save,
                                   args.poison_hit, args.hunter_hit,
                                   args.dream_save, args.dream_kill)):
        print("======== 用户口径模拟：放逐命中 / 精确刀 / 守中 / 毒中 ========")
        print(f"  参数：放逐命中={args.exile_hit}  精确刀={args.wolf_smart}  "
              f"守中平安夜={args.guard_save}  毒中狼={args.poison_hit}  "
              f"枪中狼={args.hunter_hit}  摄中={args.dream_save}  梦死狼={args.dream_kill}")
        r = report("sim", args.games, args.board, exile_hit=args.exile_hit,
                   wolf_smart=args.wolf_smart, guard_save=args.guard_save,
                   poison_hit=args.poison_hit, hunter_hit=args.hunter_hit,
                   dream_save=args.dream_save, dream_kill=args.dream_kill)
        print(f"\n>>> 结论：狼胜率 {r['wolf_rate']:.1%}（{args.games} 局，{args.board}）")
    # `--modes none` = 只跑 sim/curve，不跑 baseline/oracle/wolf-blind 那三档对照
    rows = [] if args.modes.strip().lower() == "none" else [
        report(m.strip(), args.games, args.board) for m in args.modes.split(",") if m.strip()]
    print("\n================ 汇总 ================")
    print(f"{'模式':<12}{'狼胜率':>8}{'放逐命中率':>12}{'平均天数':>10}")
    for r in rows:
        print(f"{r['mode']:<12}{r['wolf_rate']:>7.0%}{r['hit']:>11.0%}{r['days']:>10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
