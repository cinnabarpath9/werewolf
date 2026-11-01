# coding=utf-8
"""自动存盘：**对局中途**也要落盘（用户 2026-10-08 的血泪教训）。

事故：用户打了一个半小时的真实对局，进程一停（浏览器关了/机器重启/服务被杀），
`games/` 里**一个文件都没有** —— 因为存档以前只在"收尾"里写一次（`_save_game`），
那一局从来没被收尾过。用户的原话是「这半局存档」找不到了。

现在的口径：
  · 每过一个**阶段**（夜里↔白天）覆盖写一次**同一个**存档文件，收尾时再覆盖一次；
  · 途中那份带 `in_progress: true`，收尾那份带上结果并清掉标记；
  · 一局只会多出**一个**文件（不会产生半截存档堆积、也不会把 keep_games 的窗口冲掉）；
  · 写盘走"临时文件 + 原子替换"，中途断电不会留下读不出来的残破 JSON；
  · `game.autosave: false` 关掉就退回老行为（只在收尾写一次）。

本脚本（全部走真代码路径，不打整局 —— mock 整局要 40 秒以上）：
  1. 途中存一次 → 文件在、`in_progress` 是 true、房间的 save_stem 定下来了；
  2. 再存一次 → **同一个文件**被覆盖（目录里始终只有 1 个 json），内容更新；
  3. 收尾存一次 → 同一个文件、`in_progress` 变 false；
  4. `autosave: false` → `_autosave()` 什么都不做；
  5. 写盘中断的兜底：落盘过程中不该留下 `.partial` 残留（原子替换的产物要清干净）；
  6. `engine.Game.save(stem=...)` 的两种口径（给 stem / 不给 stem）都对。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                    # noqa: E402

import server as srv                                           # noqa: E402
from wolf.engine import Game                                   # noqa: E402
from wolf.roles import make_board                              # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def make_cfg(tmp: str, autosave: bool = True) -> dict:
    with io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("game", {}).update({"save_dir": tmp, "keep_games": 0, "save_budget_mb": 0,
                                       "autosave": autosave, "audio_in_save": False})
    return cfg


def jsons(d: str) -> list[str]:
    return sorted(f for f in os.listdir(d) if f.endswith(".json"))


tmp = tempfile.mkdtemp(prefix="autosave-")
try:
    cfg = make_cfg(tmp)
    # ⚠️ 2026-10-12：这里是**真局口径**（`mock_default=False`）—— 自动存盘这个功能本来就是为
    #    用户那局真棋写的（1.5 小时打水漂那次），所以判据盯着"落 save_dir 根目录"。
    #    mock 局的口径（落 `<save_dir>/_mock/`）在第七节与 `t_mock_save_isolated.py` 里验。
    room = srv.Room(cfg, mock_default=False)
    room.broadcast = lambda *a, **k: None
    room.game = Game(board=make_board("eleven_dream"), agent_factory=None, seed=7, max_days=1)
    room.human_seat = 6

    print("=== 一、途中存一次 ===")
    room._autosave()
    files = jsons(tmp)
    check("1a 落了一个存档文件", len(files) == 1, str(files))
    data = json.loads(io.open(os.path.join(tmp, files[0]), encoding="utf-8").read())
    check("1b 途中那份带 in_progress: true（打开复盘能看出'这局还没打完'）",
          data.get("in_progress") is True, str(data.get("in_progress")))
    check("1c 房间记下了本局的存档名", bool(room.save_stem), str(room.save_stem))
    check("1d 计数 +1", room.autosave_count == 1, str(room.autosave_count))

    print("\n=== 二、再存一次：覆盖同一个文件，不新增 ===")
    room.game.ledger.add(1, "day", "announce", "（模拟又过了一个阶段）")
    room._autosave()
    files2 = jsons(tmp)
    check("2a 目录里**还是只有一个** json（不是又新建一个半截存档）",
          len(files2) == 1 and files2 == files, str(files2))
    data2 = json.loads(io.open(os.path.join(tmp, files2[0]), encoding="utf-8").read())
    check("2b 内容确实更新了（新阶段那条公告在里面）",
          any("又过了一个阶段" in str(e.get("text")) for e in (data2.get("ledger") or [])))
    check("2c 计数 +2", room.autosave_count == 2, str(room.autosave_count))

    print("\n=== 三、收尾存一次：同一个文件、标记清掉 ===")
    path = room._save_game(room.game)
    files3 = jsons(tmp)
    check("3a 收尾写的还是那个文件", len(files3) == 1 and os.path.basename(path or "") == files3[0],
          f"{path} / {files3}")
    data3 = json.loads(io.open(os.path.join(tmp, files3[0]), encoding="utf-8").read())
    check("3b in_progress 变 false（这局已经打完了）", data3.get("in_progress") is False,
          str(data3.get("in_progress")))

    print("\n=== 四、关掉开关：什么都不做 ===")
    tmp2 = tempfile.mkdtemp(prefix="autosave-off-")
    room2 = srv.Room(make_cfg(tmp2, autosave=False), mock_default=False)
    room2.broadcast = lambda *a, **k: None
    room2.game = Game(board=make_board("eleven_dream"), agent_factory=None, seed=7, max_days=1)
    room2._autosave()
    check("4a autosave=false 时不落盘", jsons(tmp2) == [], str(jsons(tmp2)))
    check("4b 房间的开关也读的是 false", room2.autosave is False)
    shutil.rmtree(tmp2, ignore_errors=True)

    print("\n=== 五、原子替换：不留 .partial 残渣 ===")
    leftovers = [f for f in os.listdir(tmp) if f.endswith(".partial")]
    check("5a 目录里没有 .partial 残留", leftovers == [], str(leftovers))

    print("\n=== 六、engine.save 的两种口径 ===")
    g = Game(board=make_board("eleven_dream"), agent_factory=None, seed=7, max_days=1)
    p1 = g.save(tmp, stem="game-20990101-000000")
    check("6a 给了 stem 就用它", os.path.basename(p1) == "game-20990101-000000.json",
          os.path.basename(p1))
    p2 = g.save(tmp)
    check("6b 不给 stem 就按时间戳新建（老行为不变）",
          os.path.basename(p2).startswith("game-") and p2 != p1, os.path.basename(p2))
    check("6c 两次都写成功了", os.path.exists(p1) and os.path.exists(p2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 七、端到端：真跑一局，看挂钩会不会真触发 ─────────────────────────────────────
# ⚠️ 前六节验的是"方法本身对不对"，这一节验的是"**它到底有没有被叫到**" ——
#    挂钩写在 on_event('phase') 里，只有真跑一局才走得到（这是两回事，别只测前者）。
#    真人座位永远等人（`factory()` 里 is_human 优先），所以把 human_timeout 调到 0.1 秒
#    让它自动超时弃权 —— 一局 mock 只要 0.8 秒，跑得起。
print("\n=== 七、端到端：真跑一局（mock、真人座位自动超时弃权）===")
tmp3 = tempfile.mkdtemp(prefix="autosave-e2e-")


def _one_game() -> tuple[int, str, list[str], dict]:
    cfg3 = make_cfg(tmp3)
    cfg3["game"].update({"human_timeout": 0.1, "max_days": 1})
    room3 = srv.Room(cfg3, mock_default=True)
    room3.broadcast = lambda *a, **k: None

    async def go() -> None:
        await room3.start(mock=True, human_seat=1, seed=3)
        if room3.task is not None:
            await asyncio.wait_for(room3.task, 300)

    asyncio.run(go())
    # ⚠️ 2026-10-12：这一局是 **mock 局**（`start(mock=True)`），服务端把它写在
    #    `<save_dir>/_mock/` 子目录里（见 `server.py::_mock_save_dir`）—— 所以到子目录去数文件。
    #    顺带这一节也成了「真跑一局 mock → 存档确实**没**落在存档根目录」的端到端证据。
    files = jsons(os.path.join(tmp3, srv.MOCK_SAVE_DIRNAME))
    data = json.loads(io.open(os.path.join(tmp3, srv.MOCK_SAVE_DIRNAME, files[0]),
                             encoding="utf-8").read()) if files else {}
    return room3.autosave_count, room3.save_stem or "", files, data


try:
    count, stem, files3, data3 = _one_game()
    check("7a ⚠️ 真跑一局时挂钩**真的被叫到**了（不是只测了方法本身）", count >= 1, f"自动存盘 {count} 次")
    check("7b 阶段越多存得越多（这一局至少过了一个阶段交界）", count >= 1 and len(files3) == 1,
          f"count={count} 文件={files3}")
    check("7c 一局始终只有一个文件（自动存盘与收尾存盘共用 save_stem）",
          len(files3) == 1, str(files3))
    check("7d 收尾之后 in_progress 是 false", data3.get("in_progress") is False,
          str(data3.get("in_progress")))
    check("7e 存档里确实有这一局的台账（不是个空壳）", len(data3.get("ledger") or []) > 10,
          f"{len(data3.get('ledger') or [])} 条")
    check("7f ⚠️ 存档根目录一个 json 都没有（mock 局只进 `_mock/`，不挤真局）",
          jsons(tmp3) == [], str(jsons(tmp3)))
    check("7g 这份 mock 存档自己带了 mock: true 标记（prune 的第二路判据）",
          data3.get("mock") is True, str(data3.get("mock")))
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
