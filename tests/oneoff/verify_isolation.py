# coding=utf-8
"""隔离自检：证明每个座位只看到自己该看到的东西。

跑法：python verify_isolation.py
它做三件事：
  1. 跑完一局 mock 对局，列出所有「非公开」记录的可见人群。
  2. 逐座位组装上下文包，检查有没有别人的私有信息漏进去。
  3. 打印每个座位在最后一天实际看到的台账行数对比（狼 vs 好人一眼看出差别）。
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wolf import context
from wolf.agents import MockAgent
from wolf.context import Task
from wolf.engine import Game
from wolf.ledger import PUBLIC, WOLF

FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ✅ " if ok else "  ❌ ") + message)
    if not ok:
        FAILURES.append(message)


def audience(game: "Game", entry) -> list[int]:
    return [s.id for s in game.seats if entry.visible(s.id, s.camp)]


def check_public_causes(game: "Game", label: str) -> None:
    """公开记录里**不该**出现死因。

    ⚠️ 唯一的例外是"白天那一枪"：按用户定的规则，白天被猎人打死的人死因公示为
    「被子弹打死」。所以这里放行**这一种**写法，但要求它必须带"死因公示"字样
    （免得以后有人图省事，把死因直接写进普通的死讯里，把"夜里死因要猜"这条规则废掉）。
    """
    public_view = game.ledger.render_public(game.alive_ids(), game.dead_list(), game.day)
    causes = ("被狼人杀害", "被女巫毒死", "被猎人开枪带走", "白天被投票放逐")
    hit = [c for c in causes if c in public_view]
    check(not hit, f"{label}：公开记录里没有泄露死因（查了 {len(causes)} 种：刀/毒/枪/票）")
    for c in hit:
        print(f"      泄漏了：{c}")
    bullet = [e.text for e in game.ledger.entries
              if PUBLIC in e.visible_to and "被子弹打死" in e.text]
    bad_bullet = [t for t in bullet if "的死因公示：被子弹打死" not in t]
    check(not bad_bullet,
          f"{label}：公开提到「被子弹打死」的地方都写着「死因公示」（共 {len(bullet)} 处）")
    for t in bad_bullet:
        print(f"      可疑（缺'死因公示'）：{t[:70]}")


async def main() -> int:
    rng = random.Random(7)
    game = Game(board="eleven_wolfking", seed=7,
                agent_factory=lambda seat: MockAgent(seat, rng),
                on_event=lambda *a, **k: None)
    await game.run()

    print("=" * 78)
    print("一、非公开记录各自被谁看到（这就是隔离）")
    print("=" * 78)
    for entry in game.ledger.entries:
        if PUBLIC in entry.visible_to:
            continue
        who = audience(game, entry)
        tag = "狼队频道" if WOLF in entry.visible_to else "私有"
        print(f"  [{tag}] {entry.text}")
        print(f"          第 {entry.day} 天 · 可见座位：{'、'.join(f'{i}号' for i in who)}")

    print()
    print("=" * 78)
    print("二、逐座位组装上下文，检查有没有串味")
    print("=" * 78)

    probe = Task(kind="inspect", text="（自检）", schema="{}", legal=[])
    packets = {s.id: context.build(game, s, probe) for s in game.seats}

    wolves = [s.id for s in game.seats if s.camp == WOLF]
    good = [s.id for s in game.seats if s.camp != WOLF]

    # 1) 狼队频道只能出现在狼的包里
    wolf_channel_lines = [e.text for e in game.ledger.entries if WOLF in e.visible_to]
    leaked = [sid for sid in good
              if wolf_channel_lines and any(line in packets[sid] for line in wolf_channel_lines)]
    check(not leaked, f"狼队夜间讨论没有出现在好人座位的信息包里（检查了 {len(good)} 个好人座位）")

    # 2) 每个座位的私有经历不能出现在别人的包里
    # ⚠️ 这一项以前是「永久假绿灯」：它遍历 seat.private_events 再看别人的包，
    # 但私有经历根本不进台账（台账里只有 public 和狼队频道两种可见性），
    # 所以 bad 恒为空 —— 把可见性彻底放开它照样通过（实测过）。
    # 现在改成**主动放一个诱饵**：往某个座位的私有状态里塞一句独一无二的话，
    # 然后要求它只出现在这个座位自己的包里。隔离一旦坏掉，它立刻变红。
    canary_seat = game.seats[0]
    canary = f"金丝雀-{canary_seat.id}-只有我自己该看到-{game.day}"
    canary_seat.private_events.append(canary)
    canary_seat.mind = (canary_seat.mind + "\n" + canary).strip()
    canary_seat.checked[1 if canary_seat.id != 1 else 2] = "好人"
    fresh = {s.id: context.build(game, s, probe) for s in game.seats}
    bad = [f"{other_id}号 的包里出现了 {canary_seat.id}号的私有信息"
           for other_id, packet in fresh.items()
           if other_id != canary_seat.id and canary in packet]
    check(canary in fresh[canary_seat.id] and not bad,
          f"私有经历只出现在本人包里（放了一句诱饵「{canary}」做对照）")
    for item in bad:
        print("      " + item)
    canary_seat.private_events.remove(canary)

    # 2b) 台账里 `seat:N` 这一类（引擎里唯一的私有台账通道，例如预言家的验人结果）
    # ⚠️ 这一整类以前**没有任何断言**：审计时做变异测试（把验人结果放开给全场），
    # 自检照样打印"✅ 全部隔离自检通过"并退出 0 —— 典型的"审计工具自己瞎了"。
    # 这里查两遍：先查现实数据里已有的 seat:N 条目，再主动放一条诱饵
    # （现实对局可能一条都没有，那样光查数据等于空跑）。
    seat_entries = [e for e in game.ledger.entries
                    if any(str(v).startswith("seat:") for v in e.visible_to)]

    def _owner(entry) -> int | None:
        return next((int(str(v).split(":")[1]) for v in entry.visible_to
                     if str(v).startswith("seat:") and str(v).split(":")[1].isdigit()), None)

    # ⚠️ 2026-10-25 修自检脚本的假红：引擎会给**每个**上警的人各写一条
    #    「（你的选择）前置位上警。」—— 文本一模一样、只是 owner 不同。
    #    以前这里拿文本做子串匹配，于是 A 的那条会在 B 的包里"命中" B 自己那条同文本条目，
    #    报成泄漏。判据改成：**对方自己也有一条相同文本的私有条目**时无法区分，跳过。
    _own_texts: dict[int, set[str]] = {}
    for _e in seat_entries:
        _own_texts.setdefault(_owner(_e), set()).add(_e.text)
    seat_leak = []
    for e in seat_entries:
        owner = _owner(e)
        for sid, packet in packets.items():
            if sid == owner or not e.text or e.text not in packet:
                continue
            if e.text in _own_texts.get(sid, set()):
                # 这个座位自己也有一条一模一样文本的私有条目（例如"我也上警了"），
                # 文本层面分不出是谁的 → 不算泄漏（真正的隔离由下面那条带唯一标记的诱饵守）。
                continue
            seat_leak.append(f"{e.text[:24]}… 出现在 {sid}号 的包里（本该只有 {owner}号 能看到）")
    check(not seat_leak, f"台账里 {len(seat_entries)} 条 seat:N 私有条目没有外泄（验人结果等）")
    for item in seat_leak:
        print("      " + item)

    visitor = 3 if 3 in packets else min(packets)
    decoy = f"金丝雀私密台账-只给{visitor}号看-{game.day}"
    game.ledger.add(game.day, "day", "private", decoy, visible_to=(f"seat:{visitor}",))
    fresh2 = {s.id: context.build(game, s, probe) for s in game.seats}
    leaked2 = [sid for sid, packet in fresh2.items() if sid != visitor and decoy in packet]
    check(decoy in fresh2[visitor] and not leaked2,
          f"台账里 seat:{visitor} 的条目只进了 {visitor}号 的包（放了诱饵做对照）")
    for sid in leaked2:
        print(f"      {sid}号 的包里出现了本该只有 {visitor}号 能看的台账条目")

    # 3) 每个座位的包里必须有自己的身份
    # 必须查精确前缀「【你的身份】X」：只看角色名的话，别人的发言（"我就是个村民"）
    # 也会让这一项通过 —— 身份注入真坏掉了它照样报绿灯（永不失败的假断言）。
    missing = [s.id for s in game.seats
               if f"【你的身份】{s.role.name}" not in packets[s.id]]
    check(not missing, "每个座位的信息包里都有自己的身份说明")

    # 4) 游戏结束后，公开记录里不该出现死因（四种死因全查）
    # ⚠️ 2026-10-30：板子换成当前的默认板子 eleven_wolfking（11 人）。
    check_public_causes(game, "[eleven_wolfking · 11 人]")

    # 5) 跨局经验必须被标注成"经验"而不是"本局事实"，且身份以本局新发的牌为准
    probe_seat = game.seats[0]
    probe_seat.experience = "我上一局当狼人赢过，第一天冲票很有用。"
    packet_with_memory = context.build(game, probe_seat, probe)
    check("不是本局的事实" in packet_with_memory and "过往经验" in packet_with_memory,
          "跨局经验被明确标注为「不是本局的事实」")
    check(f"【你的身份】{probe_seat.role.name}" in packet_with_memory,
          "注入了往局经验之后，身份仍然以本局重新发的那张牌为准")
    probe_seat.experience = ""

    print()
    print("=" * 78)
    print("三、新规则（遗言 / 警长）的隔离")
    print("=" * 78)
    rng11 = random.Random(11)
    game11 = Game(board="twelve_standard", seed=11,
                  agent_factory=lambda seat: MockAgent(seat, rng11),
                  on_event=lambda *a, **k: None)
    await game11.run()
    probe12 = Task(kind="inspect", text="（自检）", schema="{}", legal=[])
    packets12 = {s.id: context.build(game11, s, probe12) for s in game11.seats}

    # 1) 警长竞选必须真的跑过，而且结果（当选 / 流失）是**公开**公告
    ran = [e for e in game11.ledger.entries if "警长竞选开始" in e.text]
    elected = [e for e in game11.ledger.entries if "当选警长" in e.text]
    lost = [e for e in game11.ledger.entries if "警徽流失" in e.text]
    check(game11.sheriff_enabled and bool(ran), "12 人局（twelve_standard）跑了警长竞选（11 人及以上才有）")
    check(bool(elected) or bool(lost),
          f"竞选有明确结果（当选 {len(elected)} 条 / 流失 {len(lost)} 条）")
    check(all(PUBLIC in e.visible_to for e in elected + lost), "当选/流失/移交警徽的公告都是公开条目")
    # ⚠️ 这里**不能**看 `game11.sheriff_id`：警长可能中途出局并撕掉警徽（那时它是 None），
    # 但"当选"这件事确实发生过 —— 第一版就是这么误判成"竞选没跑"的。

    # 2) 警长的**私有**提示（"你现在是警长…"）不能进别人的包
    owners = [s for s in game11.seats
              if any(("当选了警长" in e or "把警徽传给了你" in e) for e in s.private_events)]
    if owners:
        owner = owners[0]
        note = next(e for e in owner.private_events
                    if "当选了警长" in e or "把警徽传给了你" in e)
        leaked_badge = [s.id for s in game11.seats
                        if s.id != owner.id and note in packets12[s.id]]
        check(not leaked_badge,
              f"警长的私有提示只进他自己的包（{owner.id}号：{note[:20]}…）")
        for sid in leaked_badge:
            print(f"      {sid}号 的包里出现了警长的私有提示")
    else:
        print("      （本局没人当过警长，跳过警长私有提示检查）")

    # 3) 遗言是公开条目（全场都能听到 —— 这正是它可以用来骗人/自白的原因）
    lw = [e for e in game11.ledger.entries if e.text.startswith("【遗言】")]
    # ⚠️ 前置断言：没有遗言时下面两条 `all([])` / `0 == 0` 会**恒真空过**
    #    （2026-10-06 审查指出）。剧本一变就得有人发现，不能静默变成"没测"。
    check(len(lw) > 0, f"这一局的剧本里确实产生了遗言（{len(lw)} 条）")
    check(all(PUBLIC in e.visible_to for e in lw),
          f"遗言是公开条目（本局 {len(lw)} 条遗言）")
    shown = [e.text for e in lw if e.seat and e.text in packets12[e.seat]]
    check(len(shown) == len(lw),
          f"每条遗言都出现在发言者自己的包里（{len(shown)}/{len(lw)}）")

    # 4) 12 人局（twelve_standard）的公开记录同样不泄露死因（白天那一枪除外）
    check_public_causes(game11, "[12 人局 · twelve_standard]")

    print()
    print("=" * 78)
    print("四、同一个时间点，不同座位看到的台账规模")
    print("=" * 78)
    for seat in game.seats:
        visible = game.ledger.view(seat.id, seat.camp)
        private_n = len([e for e in visible if PUBLIC not in e.visible_to])
        print(f"  {seat.id}号 {seat.role.name:<4} 可见台账 {len(visible):>3} 条"
              f"（其中不公开 {private_n} 条）"
              f"｜自己的私有经历 {len(seat.private_events)} 条"
              f"｜自己的推理笔记 {len(seat.mind)} 字")

    print()
    if FAILURES:
        print(f"❌ 自检失败 {len(FAILURES)} 项")
        return 1
    print("✅ 全部隔离自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
