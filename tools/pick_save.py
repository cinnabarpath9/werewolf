# coding=utf-8
"""挑一个**打完的**存档来出题。

原来的写法是 `sorted(glob(...))[-1]` —— 盲取文件名最大的那个。问题是
`games/` 是共享可变目录：任何一次中断的测试（day=0、台账 0 条）都会留下一个"残局"，
下次出题就挑中它 → 隔离题 24 道、注入题 4 道**全部消失**（题库只剩规则/检索），
而自检还是打印"✅ 题库自检通过"，exit 0 —— 假绿 + 题库缩水，两头都骗人。
（2026-10-06 实测：`game-20261006-235132.json` 就是这样一个 day=0 的残局。）

选法：按修改时间从新到旧，第一个"够格当题源"的：
  · day ≥ 1（真的打过至少一天）
  · 公开台账 ≥ 20 条（有发言/投票可问）
  · transcript 里有真实调用或至少是一局完整 mock（避免只有开局几条）
找不到就明确报错，别硬着头皮出一道空题。
"""
from __future__ import annotations

import glob
import io
import json
import os


def _board_alive(name: str) -> bool:
    """这个存档用的板子现在还"认得"吗？

    十人板删掉之后，`games/` 里还躺着一堆 `ten_idiot` 的存档。拿它们当题源本身没坏处
    （题目只读台账），但题目里一旦提到板子/人数就会跟现状对不上 —— 所以挑题源时优先
    跳过"板子已删除"的存档，只在没有别的可选时才用它。

    2026-10-30：四张 11 人旧板子退役了，但**考核题库本身仍按那几张板子出题**
    （`tools/exam_bank.py::EXAM_BOARD_NAMES`）—— 所以退役板子的存档照样算"可用题源"，
    不能当成已删除（否则用户盘上仅有的几局存档全被判瘦，E1 会红）。
    """
    name = str(name or "").strip()
    if not name:
        return True
    try:
        from wolf.retired_boards import RETIRED_BOARDS
        from wolf.roles import BUILTIN_BOARDS
        return name in BUILTIN_BOARDS or name in RETIRED_BOARDS
    except Exception:                                     # noqa: BLE001
        return True


def _score(data: dict) -> tuple[int, int]:
    ledger = data.get("ledger") or []
    public = [e for e in ledger if "public" in (e.get("visible_to") or ["public"])]
    return int(data.get("day") or 0), len(public)


def pick_game(save_dir: str, need_day: int = 2, need_entries: int = 40) -> tuple[str, str]:
    """挑一个**够丰富**的存档当题源。

    ⚠️ 2026-10-07 调整门槛：只要求 day ≥ 1 时，一局"打了一天就被放弃"的残局也会被选中 ——
    它出的题只有 39 道（隔离/注入/算术都很少），题库自检的"题库缩水"门限会直接红，
    而这不是 bug、是题源太瘦。所以：
      · 优先挑 day ≥ 2、公开台账 ≥ 40 条的；
      · 一个都没有时**退回**最新的可用存档，并在说明里写清"题源偏瘦"，
        让 exam_bank 的缩水门限去决定要不要红（脚本局 demo 会补上大部分类别）。
    """
    files = glob.glob(os.path.join(save_dir, "game-*.json"))
    # ⚠️ 2026-10-14：`games/` 根目录现在**只留真局**（演练局已挪进 `games/_mock/`，
    #    见 server.py 的 _mock_save_dir/_save_is_mock 与 docs/待完善清单.md E57）。
    #    题源只看根目录会挑不到"够丰富"的存档（E1 自检因此变红）⇒ 这里**两处都收**：
    #    演练局的台账同样是引擎生成的，拿它当**题源**没问题（它只是题面素材，不是成绩）。
    files += glob.glob(os.path.join(save_dir, "_mock", "game-*.json"))
    if not files:
        return "", "games/ 里没有存档，先用真模型打一局（或 --game 指定）"
    files.sort(key=os.path.getmtime, reverse=True)
    rich: list[tuple[str, str]] = []
    thin: list[tuple[str, str]] = []
    for path in files:
        try:
            data = json.load(io.open(path, encoding="utf-8"))
        except Exception as exc:                          # noqa: BLE001
            thin.append((path, f"{os.path.basename(path)}(读不了:{type(exc).__name__})"))
            continue
        day, entries = _score(data)
        alive = _board_alive(data.get("board"))
        label = (f"{os.path.basename(path)}（{day} 天，公开台账 {entries} 条"
                 f"{'' if alive else '，板子 ' + str(data.get('board')) + ' 已删除'}）")
        if day >= need_day and entries >= need_entries and alive:
            rich.append((path, label))
        elif day >= 1 and entries >= 10:
            thin.append((path, label))
    if rich:
        return rich[0]
    if thin:
        path, label = thin[0]
        return path, label + "　⚠️ 没有更丰富的存档了，这个题源偏瘦（题库可能缩水）"
    return "", ("没有一局够格当题源（要求 ≥1 天、公开台账 ≥10 条）。"
                "最近的存档：" + "、".join(x[1] for x in thin[:4]))
