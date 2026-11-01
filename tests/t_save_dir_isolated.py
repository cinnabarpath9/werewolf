# coding=utf-8
"""守住一件事：**Room 自己带的 save_dir 必须真被尊重** —— 否则回归会把测试局写进用户的 `games/`。

2026-10-08 的真事故（代价是用户一局真存档）：
  `server.py::_cleanup` 存盘时调的是**不带参数**的 `resolve_save_dir()`，
  而它没拿到配置就回退到**全局 ROOM 的配置** —— 于是"传了临时 save_dir 的 Room"照样写到生产目录。
  后果链：`check_deal_seed` / `t_deal_shuffle` / `t_inproc` 这类用临时目录建 Room 的测试，
  每跑一次回归就往 `games/` 塞 3+ 局 mock（实测 `--fast` 一轮 +8）；
  mock 越积越多，`prune_saves()`（只留 100 局、**从最旧的开始删**）就把真局挤掉删了。

本脚本查四件：
  1. `resolve_save_dir({"game": {"save_dir": tmp}})` 解析出来就是 tmp；
  2. 用这份 cfg 建的 Room，`room.game_cfg['save_dir']` 是 tmp；
  3. **真的存一局**（直接调 `Room._save_game`）→ mock 房的存档与返回路径都必须在
     `tmp/_mock/` 里（2026-10-12 起 mock 局跟真局分开写，见 `server.py::_mock_save_dir`），
     而且 `tmp` 根目录与用户的 `games/` 都不许多出 json；真局仍然落 `tmp` 根目录。
     为什么不"打完整局再看"：mock 局要 40 秒以上（实测），太慢；`_save_game` 就是那条判据路径。
  4. 结构闸 + **变异自证**：`_cleanup` 必须调 `self._save_game(...)`、`_save_game` 必须把
     **本房间的** cfg 传给 `resolve_save_dir` —— 判据本身要能对"旧写法"判红（否则是空过）。

跑法： python t_save_dir_isolated.py
"""
from __future__ import annotations

import ast
import io
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


def games_json_count() -> int:
    d = os.path.join(ROOT, "games")
    return len([f for f in os.listdir(d) if f.endswith(".json")]) if os.path.isdir(d) else 0


def make_cfg(save_dir: str) -> dict:
    with io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("game", {}).update({"save_dir": save_dir, "keep_games": 0, "save_budget_mb": 0})
    return cfg


def _method_calls(src: str, cls: str, method: str) -> list[str]:
    """列出某个类方法体里"调了哪些 self.xxx / 传了什么实参"（给结构闸用）。"""
    out: list[str] = []
    for node in ast.parse(src).body:
        if not (isinstance(node, ast.ClassDef) and node.name == cls):
            continue
        for fn in node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == method:
                for call in ast.walk(fn):
                    if isinstance(call, ast.Call):
                        out.append(ast.unparse(call))
    return out


tmp = tempfile.mkdtemp(prefix="savedir-iso-")
try:
    print("=== 一、解析函数认参数 ===")
    cfg = make_cfg(tmp)
    got = srv.resolve_save_dir(cfg)
    check("1a resolve_save_dir(cfg) 就是 cfg 里那个目录", got == tmp, got)
    check("1b 不传参数时仍然是全局口径（线上行为不变）",
          srv.resolve_save_dir() == srv.resolve_save_dir({}), srv.resolve_save_dir())

    print("\n=== 二、Room 记住了自己的 save_dir ===")
    room = srv.Room(cfg, mock_default=True)
    room.broadcast = lambda *a, **k: None
    check("2a room.game_cfg['save_dir'] == tmp", room.game_cfg.get("save_dir") == tmp,
          str(room.game_cfg.get("save_dir")))

    print("\n=== 三、真存一局：mock 存档必须落 tmp/_mock/，games/ 一个都不许变 ===")
    before = games_json_count()
    game = Game(board=make_board("eleven_dream"), agent_factory=None, seed=1, max_days=1)
    # ⚠️ 2026-10-12：这个 Room 是 **mock 房**（`mock_default=True`），按新口径它的存档要进
    #    `<tmp>/_mock/`（见 `server.py::_mock_save_dir` 的说明：mock 局跟真局分开写，
    #    否则 prune 按时间从最旧删时，最旧的真局会被 mock 挤掉）。
    #    判据跟着改，但**没有放宽**：除了「落在 tmp 底下」，还多断言「tmp 根目录一个 json 都没有」
    #    （旧口径只查 tmp 根目录那一个位置）。
    path = room._save_game(game)
    mock_dir = os.path.join(tmp, srv.MOCK_SAVE_DIRNAME)
    check("3a _save_game 返回了存档路径", bool(path), str(path))
    check("3b 返回的路径在临时目录底下的 _mock/ 子目录里（不是直接扔 tmp 根）",
          bool(path) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(mock_dir),
          str(path))
    check("3c 磁盘上 _mock/ 里确实多了这一个存档、tmp 根目录一个 json 都没有",
          os.path.isdir(mock_dir)
          and len([f for f in os.listdir(mock_dir) if f.endswith(".json")]) == 1
          and not [f for f in os.listdir(tmp) if f.endswith(".json")],
          str(sorted(os.listdir(mock_dir))) if os.path.isdir(mock_dir) else "（没有 _mock/）")
    after = games_json_count()
    check("3d ⚠️ 用户的 games/ 一个 json 都没多（这就是当初把真局挤掉的那条路）",
          after == before, f"跑之前 {before} 个 → 跑之后 {after} 个")
    # 反向：**真局**的存档仍然直接落 save_dir 根目录 —— 分流只针对 mock，别把真局也挪走了
    real_room = srv.Room(cfg, mock_default=False)
    real_room.broadcast = lambda *a, **k: None
    path_real = real_room._save_game(
        Game(board=make_board("eleven_dream"), agent_factory=None, seed=1, max_days=1))
    check("3e 真局的存档仍然落 save_dir 根目录（没被一起挪进 _mock/）",
          bool(path_real)
          and os.path.dirname(os.path.abspath(path_real)) == os.path.abspath(tmp),
          str(path_real))

    print("\n=== 四、结构闸：三个写盘入口都用本房间的配置 ===")
    srv_src = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    cleanup_calls = _method_calls(srv_src, "Room", "_cleanup")
    save_calls = _method_calls(srv_src, "Room", "_save_game")
    write_calls = _method_calls(srv_src, "Room", "_write_save")
    auto_calls = _method_calls(srv_src, "Room", "_autosave")
    check("4a _cleanup 里调的是 self._save_game(...)",
          any("self._save_game(" in c for c in cleanup_calls),
          str([c for c in cleanup_calls if "_save" in c]))
    check("4a2 _save_game 转调 _write_save（in_progress=False）",
          any("self._write_save(" in c and "in_progress=False" in c for c in save_calls),
          str([c for c in save_calls if "_write_save" in c]))
    check("4a3 _autosave 转调 _write_save（in_progress=True）",
          any("self._write_save(" in c and "in_progress=True" in c for c in auto_calls),
          str([c for c in auto_calls if "_write_save" in c]))
    check("4b _write_save 里 resolve_save_dir 收到的是本房间的 room_cfg（不是空参）",
          any(c.startswith("resolve_save_dir(room_cfg)") for c in write_calls),
          str([c for c in write_calls if "resolve_save_dir" in c]))
    check("4c _write_save 里 prune_saves 也吃 room_cfg",
          any(c.startswith("prune_saves(room_cfg)") for c in write_calls),
          str([c for c in write_calls if "prune_saves" in c]))
    check("4e _write_save 把 stem 传给 game.save（自动存盘与终局存盘共用同一个文件）",
          any("stem=self.save_stem" in c for c in write_calls),
          str([c for c in write_calls if "game.save" in c]))

    # 变异自证：把"旧写法"喂给同一套判据，必须判红 —— 否则上面几条是空过
    fake = ('class Room:\n'
            '    async def _cleanup(self, game, clients, abandoned):\n'
            '        save_dir = resolve_save_dir()\n'
            '        path = game.save(save_dir)\n')
    fake_cleanup = _method_calls(fake, "Room", "_cleanup")
    check("4d 变异自证：判据对『_cleanup 里内联旧写法（resolve_save_dir() 空参）』判红",
          not any("self._save_game(" in c for c in fake_cleanup)
          and any(c.startswith("resolve_save_dir()") for c in fake_cleanup),
          str(fake_cleanup))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
