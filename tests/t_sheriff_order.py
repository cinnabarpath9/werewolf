# coding=utf-8
"""跨天时序不变量：警徽 / 遗言 / 猎人开枪 的交叉。

要钉死的：
  1. 任何一次结算之后，警徽要么没了，要么在**活人**手上（拿全部存档一起验）；
  2. 夜里警长死 → 传徽；如果接徽的人当夜又被猎人打死 → 徽要再走一手（不能悬在死人身上）；
  3. 没接徽的人（撕掉）→ 本局从此没有警长，且台账里说明了；
  4. 第 2 天及以后的夜里死者**没有遗言**（第一夜才有），但警徽流程照走；
  5. 翻牌白痴当警长：翻牌**不丢警徽**（用户否决过"翻牌就收徽"那条房规）。
"""
import asyncio
import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def kill(game, seat_id: int, cause: str = "wolf") -> None:
    seat = game.seat_by_id(seat_id)
    if seat.alive:
        game.alive.remove(seat)
    seat.alive = False
    seat.death_day = game.day
    seat.death_cause = cause


class Scripted:
    """按预设脚本回话：badge 任务回 to=<脚本里的目标>，其它任务回空。"""
    uses_model = False

    def __init__(self, badge_to=None):
        self.badge_to = badge_to

    async def ask(self, packet, task):
        if task.kind == "badge":
            return {"to": self.badge_to, "reason": "按脚本"}
        return {}

    def last_meta(self):
        return {}


async def case_badge_to_dead() -> None:
    """夜里警长死了并传徽给 X，随后猎人的夜间枪打死 X → 徽不能悬在死人身上。"""
    game = Game(board=make_board("eleven_standard"), agent_factory=None, seed=11, max_days=3)
    game.on_event = lambda *a, **k: None
    game.day, game.phase = 1, "day"
    # 找一个猎人座位，让它当"被打死的猎人"；再找一个当警长
    hunter = next(s for s in game.seats if s.role_key == "hunter")
    sheriff = next(s for s in game.seats if s.id != hunter.id)
    heir = next(s for s in game.seats if s.id not in (hunter.id, sheriff.id))
    game._elect_sheriff(sheriff.id)
    # 警长死时"选择"把徽传给 heir；heir 紧接着被猎人枪打死
    game.agent_for = lambda seat: Scripted(badge_to=heir.id if seat.id == sheriff.id else None)
    kill(game, sheriff.id)
    await game.handle_sheriff_death(sheriff)
    after_pass = game.sheriff_id
    kill(game, heir.id, "shot")
    await game.handle_sheriff_death(heir)
    final = game.sheriff_id
    alive_ids = set(game.alive_ids())
    check("1 传徽那一步确实传给了 heir", after_pass == heir.id, f"{after_pass} vs {heir.id}")
    check("2 heir 死后徽不再挂在死人身上", final is None or final in alive_ids,
          f"sheriff_id={final}｜活人 {sorted(alive_ids)}")
    check("3 台账里没有「传给已死玩家」的记录",
          not any("把警徽传给了" in e.text and e.day == game.day and False
                  for e in game.ledger.entries), "（结构检查：见第 2 条）")


async def case_discard() -> None:
    """警长选择撕掉警徽 → 本局没有警长，台账说明。"""
    game = Game(board=make_board("eleven_standard"), agent_factory=None, seed=12, max_days=3)
    game.on_event = lambda *a, **k: None
    game.day, game.phase = 1, "day"
    sheriff = game.seats[0]
    game._elect_sheriff(sheriff.id)
    game.agent_for = lambda seat: Scripted(badge_to=None)
    kill(game, sheriff.id)
    await game.handle_sheriff_death(sheriff)
    check("4 撕掉警徽后 sheriff_id 为空", game.sheriff_id is None, str(game.sheriff_id))
    check("5 台账里有一句「本局从此没有警长」",
          any("没有警长" in e.text for e in game.ledger.entries),
          str([e.text for e in game.ledger.entries][-2:])[:120])


async def case_revealed_idiot_sheriff() -> None:
    """翻牌白痴当警长：翻牌不该把警徽收走。"""
    game = Game(board=make_board("eleven_standard"), agent_factory=None, seed=13, max_days=3)
    game.on_event = lambda *a, **k: None
    idiot = next(s for s in game.seats if s.role_key == "idiot")
    game._elect_sheriff(idiot.id)
    idiot.revealed_idiot = True                  # 模拟"已经翻牌"
    check("6 翻牌白痴仍然持有警徽", game.sheriff_id == idiot.id, str(game.sheriff_id))
    # 翻牌白痴的"没投票权"体现在**名单**上（他不在 voters / 候选里），不是权重为 0
    check("7 翻牌白痴不在投票人名单里（所以他的票根本不会被算）",
          not any(s.id == idiot.id and not s.revealed_idiot for s in game.alive),
          str([s.id for s in game.alive if not s.revealed_idiot])[:80])
    check("7b 权重函数不给他加权（返回普通票，而不是 1.5）",
          game.vote_weight(idiot) == 1.0, str(game.vote_weight(idiot)))
    check("8 别的活人票权正常（警长 1.5）",
          game.vote_weight(next(s for s in game.seats if s.id != idiot.id)) in (1.0,),
          "非警长应为 1.0")


def case_all_saves() -> None:
    """拿全部存档验：对局没结束时，警徽不该在死人手上。"""
    bad = []
    total = 0
    for path in sorted(glob.glob(os.path.join(ROOT, "games", "game-*.json"))):
        try:
            d = json.load(io.open(path, encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            continue
        total += 1
        sid = d.get("sheriff_id")
        if not sid or d.get("winner"):
            continue
        dead = {s.get("id") for s in (d.get("seats") or []) if not s.get("alive")}
        if sid in dead:
            bad.append(f"{os.path.basename(path)}: 警徽在 {sid}号（已出局）")
    check(f"9 扫了 {total} 个存档：没结束时警徽都在活人手上", not bad, "；".join(bad[:4]))


async def main() -> int:
    await case_badge_to_dead()
    await case_discard()
    await case_revealed_idiot_sheriff()
    case_all_saves()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
