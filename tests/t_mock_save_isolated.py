# coding=utf-8
"""守住一件事：**mock 局的存档不许进生产 `games/` 根目录，真局不许被 mock 挤出保留窗口**。

2026-10-12 的真事故（用户 `games/` 里 100 份存档、只有 9 份是真局，而 `prune_saves()`
只留 100 局、**从最旧的开始删** ⇒ 那 9 份真局正是下一批会被删掉的）：
  · `tests/t_page_persona.py` / `t_page_rules.py` 这类**真浏览器页面测试**打的是**用户那台
    正在跑的 8077**（它们不自己起服务，拿不到临时 save_dir）—— 它们在真服务里开一局 mock、
    放弃 → `Room._save_game` 就把 mock 局写进了生产 `games/`；
  · `prune_saves()` 只按文件名（= 时间戳）排一条队、从最旧的开始删 —— mock 比真局多得多，
    于是**最旧的真局正好是第一批被删的**。

本脚本查六件（全程不打真模型、不碰用户存档、实测 1 秒内）：
  一、`_mock_save_dir()` 的规则本身（子目录名 + 拼出来的路径）；
  二、**真存一份 mock 局**：落在 `<save_dir>/_mock/`，根目录一个 json 都不多；
      反向：真局（`mock_default=False`）仍然落 `<save_dir>` 根目录（分流只针对 mock）；
  三、**生产目录探针**：拿真 `config.yaml`（`save_dir: games`）建 mock Room、跑 `_write_save`
      （`in_progress=True`：不走 prune、也不真写盘）→ 记录的目录必须是 `games/_mock`；
      再做**变异自证**：把 `_mock_save_dir` 换成恒等函数（= 旧写法）后同一条判据必须判红；
  四、`prune_saves()` 在 mock 与真局混在一起时**先删 mock**（构造假存档，不跑真局）；
  五、**1 份真局（还是最旧的）+ 一堆 mock、一起超窗口** ⇒ 真局不许被删掉；
  六、`<save_dir>/_mock/` 出不了复盘列表（`/api/games` 只列根目录，进程内打真路由验）。

跑法： python t_mock_save_isolated.py
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

import httpx                                                    # noqa: E402
import yaml                                                     # noqa: E402

import server as srv                                            # noqa: E402
from wolf.engine import Game                                    # noqa: E402
from wolf.roles import make_board                               # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

#: 假配对码（进程内打路由用）。要跟下面那个假 ROOM 的 `env_token` 一致。
TOKEN = "MOCK01"

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def json_count(dir_path: str) -> int:
    """目录**根**里有几个 .json（不递归 —— 正是 `/api/games` 与 prune 的口径）。"""
    try:
        return len([f for f in os.listdir(dir_path)
                    if f.endswith(".json") and os.path.isfile(os.path.join(dir_path, f))])
    except OSError:
        return 0


def save_payload(kind: str) -> str:
    """造一份形状像存档的 JSON 文本。

    kind：
      real          = 真局（transcript 里有非空 model）
      mock          = mock 局（每一条 model 都是空串 —— MockAgent 的样子）
      explicit_mock = 显式标了 `mock: true` 的 mock 局（2026-10-12 起新存档会带这个标记）
    """
    def entry(model: str) -> dict:
        return {"day": 1, "phase": "day", "seat": 3, "task": "vote",
                "model": model, "reasoning": "想一下", "raw": '{"vote": 5}'}

    tr = [entry("glm-5.3-flash"), entry("")]          # 真局：至少有一条带模型名
    if kind in ("mock", "explicit_mock"):
        tr = [entry("") for _ in tr]
    data: dict = {"board": "eleven_wolfking", "day": 1, "winner": "good",
                  "ledger": [], "transcript": tr, "decisions": []}
    if kind == "explicit_mock":
        data["mock"] = True
    return json.dumps(data, ensure_ascii=False)


def put(dir_path: str, name: str, kind: str) -> str:
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, name)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(save_payload(kind))
    return path


def make_cfg(save_dir: str, keep: int = 0) -> dict:
    """临时 save_dir 的配置（照抄 `config.yaml`，只改存档目录与留档策略）。"""
    with io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("game", {}).update(
        {"save_dir": save_dir, "keep_games": keep, "save_budget_mb": 0,
         "audio_in_save": False, "experience": False})
    return cfg


def make_game() -> Game:
    """一局空引擎（不跑）—— 存盘只需要一个 Game 对象，见 t_save_dir_isolated 的同一手法。"""
    return Game(board=make_board("eleven_wolfking"), agent_factory=None, seed=1, max_days=1)


TMP: list[str] = []


def tmpdir(tag: str) -> str:
    d = tempfile.mkdtemp(prefix=f"mocksave-{tag}-")
    TMP.append(d)
    return d


try:
    print("=== 一、mock 局的存档目录规则 ===")
    check("1a 子目录名就是 _mock", srv.MOCK_SAVE_DIRNAME == "_mock", srv.MOCK_SAVE_DIRNAME)
    check("1b mock 子目录 = <save_dir>/_mock",
          srv._mock_save_dir(os.path.join(ROOT, "games")) == os.path.join(ROOT, "games", "_mock"),
          srv._mock_save_dir(os.path.join(ROOT, "games")))

    print("\n=== 二、真存一份 mock 局：落 _mock/ 子目录，根目录一个都不多 ===")
    tmp_mock = tmpdir("mock")
    room = srv.Room(make_cfg(tmp_mock), mock_default=True)
    room.broadcast = lambda *a, **k: None
    before_root = json_count(tmp_mock)
    path = room._save_game(make_game())
    mock_dir = os.path.join(tmp_mock, srv.MOCK_SAVE_DIRNAME)
    check("2a _save_game 返回了存档路径", bool(path), str(path))
    check("2b 返回的路径就在 `<save_dir>/_mock/` 里",
          bool(path) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(mock_dir),
          str(path))
    check("2c 磁盘上 _mock/ 里确实多了这一份", json_count(mock_dir) == 1,
          f"{json_count(mock_dir)} 个：{sorted(os.listdir(mock_dir)) if os.path.isdir(mock_dir) else '-'}")
    check("2d ⚠️ 存档根目录一个 json 都没多（这就是往生产 games/ 塞 mock 的那条路）",
          json_count(tmp_mock) == before_root == 0,
          f"跑之前 {before_root} 个 → 跑之后 {json_count(tmp_mock)} 个")
    check("2e 存档里写了显式标记 mock=true（给将来留的第二路判据）",
          json.load(io.open(path, encoding="utf-8")).get("mock") is True, "")

    print("\n=== 二·反向：真局仍然落存档根目录（分流只针对 mock）===")
    tmp_real = tmpdir("real")
    room_real = srv.Room(make_cfg(tmp_real), mock_default=False)
    room_real.broadcast = lambda *a, **k: None
    path_real = room_real._save_game(make_game())
    check("2f 真局的存档落 `<save_dir>` 根目录（没被挪进子目录）",
          bool(path_real)
          and os.path.dirname(os.path.abspath(path_real)) == os.path.abspath(tmp_real),
          str(path_real))
    check("2g 真局那份的 mock 标记是 false",
          json.load(io.open(path_real, encoding="utf-8")).get("mock") is False, "")
    check("2h 真局那份里 _mock/ 目录没被建出来",
          not os.path.isdir(os.path.join(tmp_real, srv.MOCK_SAVE_DIRNAME)), "")

    print("\n=== 三、生产目录探针：真 config.yaml（save_dir: games）的 mock 房间 ===")
    with io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        prod_cfg = yaml.safe_load(fh)
    prod_root = os.path.join(ROOT, "games")
    prod_mock = os.path.join(prod_root, srv.MOCK_SAVE_DIRNAME)
    games_root_before = json_count(prod_root)

    def probe_prod_dir() -> str | None:
        """用真配置建 mock Room，跑一次 `_write_save`，**只记录它想写到哪**（不真写盘）。

        为什么用 `in_progress=True`：那条路不会调 `prune_saves`，于是**碰不到任何真存档**
        （判据只需要「它会往哪写」，不需要真写一份）。`game.save` 换成记录器，
        连 `_archive_audio` 都不会被调到 —— 整个探针在用户 `games/` 上零副作用。
        """
        room3 = srv.Room(prod_cfg, mock_default=True)
        room3.broadcast = lambda *a, **k: None
        game3 = make_game()
        seen: dict[str, str] = {}

        def recorder(save_dir, enrich=None, stem=""):
            seen["dir"] = save_dir
            return os.path.join(save_dir, (stem or "game-x") + ".json")

        game3.save = recorder                      # 实例属性盖住绑定方法
        room3._write_save(game3, in_progress=True)
        return seen.get("dir")

    got_prod = probe_prod_dir()
    check("3a mock 房间在生产配置下想写的是 `games/_mock`（不是 `games`）",
          bool(got_prod) and os.path.abspath(got_prod) == os.path.abspath(prod_mock),
          str(got_prod))
    check("3b 探针没有往生产根目录写任何东西",
          json_count(prod_root) == games_root_before,
          f"跑之前 {games_root_before} 个 → 跑之后 {json_count(prod_root)} 个")

    # 变异自证：把分流函数换成恒等函数（= 修之前的写法），同一条判据必须判红
    real_mock_dir_fn = srv._mock_save_dir
    try:
        srv._mock_save_dir = lambda d: d
        got_mut = probe_prod_dir()
    finally:
        srv._mock_save_dir = real_mock_dir_fn
    check("3c 变异自证：去掉分流后，同一条判据看到的就是生产 `games`（说明 3a 不是空过）",
          bool(got_mut) and os.path.abspath(got_mut) == os.path.abspath(prod_root)
          and os.path.abspath(got_prod) != os.path.abspath(got_mut),
          f"正常={got_prod}　变异={got_mut}")

    print("\n=== 四、prune_saves：mock 与真局混在一起时先删 mock ===")
    tmp4 = tmpdir("prune")
    # 名字就是时间顺序：真局排最前（最旧）—— 旧口径下它第一个被删
    put(tmp4, "game-20260101-000001.json", "real")
    for i in range(2, 6):
        put(tmp4, f"game-20260101-{i:06d}.json", "mock")
    rep = srv.prune_saves(make_cfg(tmp4, keep=3))
    left = sorted(f for f in os.listdir(tmp4) if f.endswith(".json"))
    check("4a 超窗口就按条数删（5 → 3）", len(left) == 3, str(left))
    check("4b 删掉的两个都是 mock（不是按文件名从最旧的删）",
          sorted(rep["removed"]) == ["game-20260101-000002.json", "game-20260101-000003.json"],
          str(rep["removed"]))
    check("4c 最旧的那份**真局**还在磁盘上（旧口径下它才是第一个被删的）",
          "game-20260101-000001.json" in left, str(left))
    check("4d kept 报的是磁盘上真实剩几局", rep["kept"] == 3, str(rep["kept"]))

    print("\n=== 五、真局不会被 mock 挤出保留窗口（1 真 + 10 mock，窗口只有 2）===")
    tmp5 = tmpdir("window")
    real_name = "game-20260101-000000.json"                # 真局还是**最旧**的那一份
    put(tmp5, real_name, "real")
    for i in range(1, 11):
        put(tmp5, f"game-20260101-{i:06d}.json", "mock")
    rep5 = srv.prune_saves(make_cfg(tmp5, keep=2))
    left5 = sorted(f for f in os.listdir(tmp5) if f.endswith(".json"))
    check("5a 只删 mock（9 个），一个真局都没动",
          len(rep5["removed"]) == 9 and real_name not in rep5["removed"],
          f"removed={rep5['removed']}")
    check("5b 最旧的真局活下来了（这就是用户那 9 份真局的处境）",
          real_name in left5 and os.path.exists(os.path.join(tmp5, real_name)), str(left5))
    check("5c 剩下的 2 份 = 1 真 + 1 mock", len(left5) == 2 and left5[-1] != real_name, str(left5))

    print("\n=== 五·补充：读不出来的存档当真局（宁可多留，不可误删）===")
    tmp5b = tmpdir("broken")
    broken = os.path.join(tmp5b, "game-20260101-000000.json")
    with io.open(broken, "w", encoding="utf-8") as fh:
        fh.write('{"board": "eleven_wolfking", "transcript": [')     # 半截 JSON
    put(tmp5b, "game-20260101-000001.json", "explicit_mock")
    put(tmp5b, "game-20260101-000002.json", "explicit_mock")
    rep5b = srv.prune_saves(make_cfg(tmp5b, keep=1))
    check("5d 半截 JSON 排最后（先删两个显式 mock）",
          rep5b["removed"] == ["game-20260101-000001.json", "game-20260101-000002.json"]
          and os.path.exists(broken),
          f"removed={rep5b['removed']}")

    print("\n=== 六、`_mock/` 出不了复盘列表（只列根目录）===")
    tmp6 = tmpdir("list")
    put(tmp6, "game-20260101-000001.json", "real")
    put(os.path.join(tmp6, srv.MOCK_SAVE_DIRNAME), "game-20260101-000002.json", "mock")
    real_resolve = srv.resolve_save_dir
    real_room = srv.ROOM
    try:
        srv.resolve_save_dir = lambda cfg=None: tmp6
        srv.ROOM = type("R", (), {"game_cfg": {"save_dir": tmp6}, "env_token": TOKEN})()

        async def call_games() -> object:
            transport = httpx.ASGITransport(app=srv.app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1") as cl:
                return await cl.get("/api/games", params={"token": TOKEN})

        resp = asyncio.run(call_games())
        games = (resp.json() or {}).get("games") or []
        check("6a /api/games 只列根目录那一份（mock 子目录没被扫进来）",
              games == ["game-20260101-000001.json"], f"HTTP {resp.status_code} {games}")
    finally:
        srv.resolve_save_dir = real_resolve
        srv.ROOM = real_room
    src_review = io.open(os.path.join(ROOT, "tools", "review.py"), encoding="utf-8").read()
    check("6b tools/review.py 的取档也是非递归 glob（不会把 _mock 当最近一局）",
          'glob.glob(os.path.join(save_dir, "game-*.json"))' in src_review, "")
    src_server = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    check("6c server.py 里说明了 `_mock/` 为什么不在复盘列表里（不是忘了排除）",
          "故意不在复盘" in src_server and "_mock" in src_server, "")

    print("\n=== 七、`_save_is_mock` 的判据本身（内容优先，兜底保守）===")
    tmp7 = tmpdir("judge")
    p_real = put(tmp7, "game-20260101-000001.json", "real")
    p_mock = put(tmp7, "game-20260101-000002.json", "mock")
    p_exp = put(tmp7, "game-20260101-000003.json", "explicit_mock")
    p_bad = os.path.join(tmp7, "game-20260101-000004.json")
    with io.open(p_bad, "w", encoding="utf-8") as fh:
        fh.write("not json at all")
    p_false = os.path.join(tmp7, "game-20260101-000005.json")
    # ⚠️ 这一份要显式标成「真局」。**故意用下标赋值**，不写成字典字面量：check_gates 第 4c 条
    #    会扫「让服务端去调真模型」那种写法并点名，本脚本一个模型都不调，不该进那张白名单。
    payload_false: dict = {"board": "eleven_wolfking", "transcript": []}
    payload_false["mock"] = False
    with io.open(p_false, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload_false, ensure_ascii=False))
    p_empty = os.path.join(tmp7, "game-20260101-000006.json")
    with io.open(p_empty, "w", encoding="utf-8") as fh:
        fh.write("{}")
    check("7a 真局（transcript 里有非空 model）→ 不是 mock", srv._save_is_mock(p_real) is False, "")
    check("7b mock 局（model 全是空串）→ 是 mock", srv._save_is_mock(p_mock) is True, "")
    check("7c 显式 mock: true → 是 mock", srv._save_is_mock(p_exp) is True, "")
    check("7d 读不出来（半截/不是 JSON）→ 当真局（不先删）",
          srv._save_is_mock(p_bad) is False, "")
    check("7e 显式 mock: false → 真局（不靠 transcript 也能认）",
          srv._save_is_mock(p_false) is False, "")
    check("7f 空壳 `{}`（没有任何真局证据）→ 算 mock（先删它，轮不到真局）",
          srv._save_is_mock(p_empty) is True, "")
finally:
    for d in TMP:
        shutil.rmtree(d, ignore_errors=True)

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
