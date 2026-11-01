import asyncio, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wolf.agents import MockAgent
from wolf.engine import Game
from wolf.retired_boards import retired_board
#: 2026-10-30：产品四张板子都没有守卫 → 用已退役的 eleven_guard 副本（它才有守卫记录可核）。
#: 以前这里挂的是 eleven_standard（根本没有守卫），循环里 `guard` 恒为 None ⇒ 整条断言
#: 是一路空过的假绿灯；换副本之后才真的在核守卫的私有记录写法。
GUARD_BOARD = retired_board("eleven_guard")
hits = []
for seed in range(80):
    rng = random.Random(seed)
    g = Game(board=GUARD_BOARD, seed=seed, agent_factory=lambda s: MockAgent(s, rng), on_event=lambda *a, **k: None)
    asyncio.run(g.run())
    guard = next((s for s in g.seats if s.role_key == "guard"), None)
    if not guard: continue
    ev = list(guard.private_events)
    bad = [e for e in ev if "守护" in e and "守护了 " not in e]
    if bad:
        hits.append((seed, bad))
print("命中种子数:", len(hits))
for seed, bad in hits[:5]:
    print(f"  seed={seed}: {bad}")
    try:
        [e.split("守护了 ")[1] for e in bad]
        print("    旧写法没崩")
    except Exception as exc:
        print(f"    旧写法崩了 -> {type(exc).__name__}: {exc}")

# ⚠️ 2026-10-09 补：以前只 print 命中数，没有退出码（假回归门）。
# 期望：守卫的私有记录**只**用「守护了 X号」这种新写法（旧写法会让下游解析崩）。
print()
print("判定：", "✅ 守卫记录都是新写法" if not hits else f"❌ {len(hits)} 个种子还在用旧写法")
raise SystemExit(0 if not hits else 1)
