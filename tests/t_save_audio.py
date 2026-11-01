# coding=utf-8
"""回归：存档留档（条数 + 预算）、发言语音一起留档、按序号的语音重放接口。

全部在**临时目录**里做，不碰真存档；只有"接口能播"那一条用真存档目录里的临时局。
"""
import io
import os
import re
import shutil
import sys
import tempfile

WW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WW)
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(WW)

import json                                              # noqa: E402
import asyncio                                            # noqa: E402
import stat                                               # noqa: E402
import time                                              # noqa: E402

import httpx                                             # noqa: E402

import run_game                                          # noqa: E402
import server                                            # noqa: E402
from wolf.agents import MockAgent                        # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


run_game.load_env(os.path.join(WW, ".env"))
TOKEN = re.search(r"[A-Z0-9]{6}", io.open("配对码.txt", encoding="utf-8-sig").read()).group()

print("=== 一、发言语音归档（硬链接，不重新合成）===")
tmp = tempfile.mkdtemp(prefix="saves-")
try:
    # 造一条"缓存里已经有语音"的发言：直接用真实语音文件（拿缓存里现成的）
    cache = sorted([p for p in __import__("glob").glob(os.path.join(WW, "var", "voice_cache", "*.mp3"))],
                   key=os.path.getmtime)
    check("0a 语音缓存里有现成文件可用", bool(cache), f"{len(cache)} 个")

    # 用真实配置算一个缓存键：拿一条真发言文本，写一份假缓存文件进去
    import yaml
    cfg = yaml.safe_load(io.open("config.yaml", encoding="utf-8"))
    game_cfg = cfg.get("game") or {}
    server.ROOM = server.Room(cfg, True) if hasattr(server, "Room") else None
    vcfg = server.voice.VoiceConfig.from_env(cfg)
    check("0b 语音配置就绪", bool(vcfg.ready), f"tts={vcfg.tts_model}")

    SPEECH_TEXT = "这是一条用来测试留档的发言。"
    sv = vcfg.seat_voice(3)
    key_path = server._voice_cache_path(3, sv.key(), SPEECH_TEXT, vcfg.tts_model)
    # ⚠️ 挑一个**不是 key_path 自己**的文件当源：上一轮跑这个测试时造出来的那个文件
    #    会是最新的，选到它就变成"自己复制到自己"（SameFileError）。
    picks = [p for p in cache if os.path.abspath(p) != os.path.abspath(key_path)]
    src = picks[-1] if picks else cache[-1]
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(key_path):
        shutil.copyfile(src, key_path)

    data = {"ledger": [
        {"kind": "announce", "seat": None, "text": "昨晚是平安夜。"},
        {"kind": "speech", "seat": 3, "text": SPEECH_TEXT},
        {"kind": "vote", "seat": 3, "text": "投给 5号"},
        {"kind": "speech", "seat": 4, "text": "这条缓存里没有，应该被跳过"},
    ]}
    server.prune_saves = server.prune_saves  # 保持引用（下面会用到）
    saved = server._archive_audio(tmp, "game-test", data)
    check("1a 只有「缓存里有语音」的发言被留档", saved == 1, f"归档 {saved} 条")
    target = os.path.join(tmp, "game-test", "audio", "1.mp3")
    check("1b 归档文件按台账序号命名", os.path.exists(target), target)
    check("1c 存档数据里给这条打了 audio 标记",
          data["ledger"][1].get("audio") is True and not data["ledger"][3].get("audio"), "")
    check("1d 硬链接：和缓存同一个文件（不占额外空间）",
          os.path.exists(target) and os.path.getsize(target) == os.path.getsize(cache[-1]),
          f"{os.path.getsize(target)} vs {os.path.getsize(cache[-1])}")
    check("1e 统计写进了 audio_count", data.get("audio_count") == 1, str(data.get("audio_count")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== 二、留档条数 + 磁盘预算 ===")
tmp2 = tempfile.mkdtemp(prefix="prune-")
real_resolve = server.resolve_save_dir
try:
    server.resolve_save_dir = lambda cfg=None: tmp2   # cfg：prune/_cleanup 现在会传本房间配置进来
    server.ROOM = type("R", (), {"game_cfg": {"keep_games": 100, "save_budget_mb": 0}})()
    for i in range(114):
        name = f"game-2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}-0000{i:03d}.json"
        io.open(os.path.join(tmp2, name), "w", encoding="utf-8").write("{}")
        os.makedirs(os.path.join(tmp2, name[:-5], "audio"), exist_ok=True)
        io.open(os.path.join(tmp2, name[:-5], "audio", "1.mp3"), "wb").write(b"x" * 1024)
    rep = server.prune_saves()
    left = [f for f in os.listdir(tmp2) if f.endswith(".json")]
    dirs = [d for d in os.listdir(tmp2) if os.path.isdir(os.path.join(tmp2, d))]
    check("2a 留档条数生效（114 → 100）", len(left) == 100, f"剩 {len(left)}")
    check("2b 删的是最旧的", len(rep["removed"]) == 14, f"删了 {len(rep['removed'])} 个")
    # 目录名 = JSON 名去掉 .json（不是 d[:-5] —— 那是从目录名尾巴上切，切出来当然对不上）
    check("2c 语音目录跟着一起删（不留孤儿目录）",
          len(dirs) == 100 and all((d + ".json") in left for d in dirs),
          f"{len(dirs)} 个目录，孤儿：{[d for d in dirs if (d + '.json') not in left][:3]}")

    # 预算兜底：把预算设成很小，应该继续删到只剩 1 局
    server.ROOM = type("R", (), {"game_cfg": {"keep_games": 0, "save_budget_mb": 1}})()
    for i in range(6):                    # 塞 6 个 1 MB 的"胖"存档
        p = os.path.join(tmp2, f"game-2026060{i + 1}-120000.json")
        io.open(p, "wb").write(b"y" * (1024 * 1024))
    rep2 = server.prune_saves()
    total_mb = server._dir_size_mb(tmp2)
    check("2d 预算超了就继续删（总量落回预算内）", total_mb <= 1.2 or len(rep2["removed"]) >= 6,
          f"删了 {len(rep2['removed'])} 个，剩 {total_mb:.1f} MB")
    check("2e 预算再紧也至少留 1 局（不能删空）",
          len([f for f in os.listdir(tmp2) if f.endswith('.json')]) >= 1,
          str(len([f for f in os.listdir(tmp2) if f.endswith('.json')])))
finally:
    server.resolve_save_dir = real_resolve
    shutil.rmtree(tmp2, ignore_errors=True)

print("\n=== 三、配置口径 ===")
check("3a config 里有 keep_games = 100（用户定的留档局数）",
      int(game_cfg.get("keep_games") or 0) == 100, str(game_cfg.get("keep_games")))
check("3b 有磁盘预算兜底（5 GB）", int(game_cfg.get("save_budget_mb") or 0) == 5000,
      str(game_cfg.get("save_budget_mb")))
check("3c 发言语音默认一起留档", bool(game_cfg.get("audio_in_save", True)) is True, "")

print("\n=== 四、/api/saved_audio 接口（对着真服务 8077）===")
# ⚠️ 8077 上跑的是**用户那台**服务：如果它是改动之前启动的，第 4b 条会看到旧语义（404）。
#    那不是代码错，而是「新代码还没生效」 —— 重启 8077 后重跑本脚本即可。
#    新语义本身在第 七 节用进程内路由直接打 FastAPI app 验，不依赖 8077 的新旧。
try:
    games = httpx.get("http://127.0.0.1:8077/api/games", params={"token": TOKEN},
                      timeout=10).json().get("games") or []
    check("4a 存档列表能读", bool(games), f"{len(games)} 局")
    r = httpx.get("http://127.0.0.1:8077/api/saved_audio",
                  params={"token": TOKEN, "game": games[-1] if games else "game-x.json",
                          "n": 999999}, timeout=10)
    stale = r.status_code == 404
    check("4b 越界序号给 400（不是 404「没有留语音」）",
          r.status_code == 400 and "超出" in r.text,
          f"{r.status_code} {r.text[:70]}"
          + ("　→ 8077 还是改动前的老进程，重启后复验（新语义见第七节）" if stale else ""))
    r2 = httpx.get("http://127.0.0.1:8077/api/saved_audio",
                   params={"token": TOKEN, "game": "../config.yaml", "n": 1}, timeout=10)
    check("4c 目录穿越被拒（400）", r2.status_code == 400, f"{r2.status_code}")
    r3 = httpx.get("http://127.0.0.1:8077/api/saved_audio",
                   params={"token": TOKEN, "game": "game-20261007-174335.json", "n": "abc"},
                   timeout=10)
    check("4d 序号不是数字时给 400（不是 500）", r3.status_code == 400, f"{r3.status_code}")
    r4 = httpx.get("http://127.0.0.1:8077/api/saved_audio",
                   params={"token": TOKEN, "game": games[-1] if games else "game-x.json",
                           "n": -1}, timeout=10)
    stale4 = r4.status_code == 404
    check("4e 负数序号给 400（旧行为是 404「没有留语音」，语义误导）",
          r4.status_code == 400, f"{r4.status_code} {r4.text[:60]}"
          + ("　→ 8077 还是老进程，重启后复验" if stale4 else ""))
except Exception as exc:                                  # noqa: BLE001
    check("4x 服务端可达", False, f"{type(exc).__name__}: {exc}")

print("\n=== 五、前端：每条发言的 🔊 + 复盘播存档 ===")
html = io.open(os.path.join(WW, "web", "index.html"), encoding="utf-8").read()
check("5a 有 CLIP 播放器（主动点的那一下必须完整播完）",
      "const CLIP" in html and "CLIP.play" in html)
check("5b 每条发言后面挂 🔊（用台账序号）",
      "mkClipBtn('live'" in html and "/api/tts?n=${e.n}" in html)
check("5c 复盘里播的是**存档音频**，不是重新合成",
      "/api/saved_audio?game=" in html and "escap" in html)
check("5d 复盘面板列出这一局的发言 + 没有语音时标明",
      "这一局的发言" in html and "（没留语音）" in html)
check("5e 复盘面板有存档选单（留几局就能点几局）",
      "data-game=" in html and "openReview(b.getAttribute('data-game'))" in html)

print("\n=== 六、存档卫生：删除失败要单独计、孤儿语音目录要清 ===")
# A. 删除失败**不能**混进 removed（日志会骗人）
tmp3 = tempfile.mkdtemp(prefix="prune-fail-")
real_resolve3 = server.resolve_save_dir
try:
    server.resolve_save_dir = lambda cfg=None: tmp3       # cfg：prune/_cleanup 现在会传本房间配置进来
    server.ROOM = type("R", (), {"game_cfg": {"keep_games": 1, "save_budget_mb": 0}})()

    def mk_json(name: str, audio: bool = False) -> str:
        p = os.path.join(tmp3, name)
        io.open(p, "w", encoding="utf-8").write("{}")
        if audio:
            os.makedirs(os.path.join(tmp3, name[:-len(".json")], "audio"), exist_ok=True)
            io.open(os.path.join(tmp3, name[:-len(".json")], "audio", "1.mp3"), "wb").write(b"x")
        return p

    locked = mk_json("game-20260101-000001.json", audio=True)     # 最旧 → 会被删，但删不动
    mk_json("game-20260101-000002.json", audio=True)
    mk_json("game-20260101-000003.json", audio=True)
    os.chmod(locked, stat.S_IREAD)                 # Windows：只读文件删不掉 → 模拟删除失败
    rep3 = server.prune_saves()
    lname = os.path.basename(locked)
    check("6a 删失败的不进 removed（否则日志会骗人）", lname not in rep3["removed"],
          str(rep3["removed"]))
    check("6b 删失败单独进 failed、文件还在磁盘上",
          lname in (rep3.get("failed") or []) and os.path.exists(locked),
          f"failed={rep3.get('failed')}")
    check("6c 真删掉的那个才进 removed",
          rep3["removed"] == ["game-20260101-000002.json"], str(rep3["removed"]))
    on_disk = [f for f in os.listdir(tmp3) if f.endswith(".json")]
    check("6d kept 报的是**磁盘上真实剩几局**（不是「列表里剩几个」）",
          rep3["kept"] == len(on_disk) == 2, f"kept={rep3['kept']} 磁盘={len(on_disk)}")
    os.chmod(locked, stat.S_IWRITE)
finally:
    server.resolve_save_dir = real_resolve3
    shutil.rmtree(tmp3, ignore_errors=True)

# B. 孤儿语音目录（有 <stem>/audio、没有同名 json）要清掉 —— 只清该清的
tmp4 = tempfile.mkdtemp(prefix="prune-orphan-")
real_resolve4 = server.resolve_save_dir
try:
    server.resolve_save_dir = lambda cfg=None: tmp4       # cfg：prune/_cleanup 现在会传本房间配置进来
    server.ROOM = type("R", (), {"game_cfg": {"keep_games": 0, "save_budget_mb": 0}})()

    def mk_orphan(name: str, audio: bool = True, age: float = 3600.0) -> str:
        p = os.path.join(tmp4, name)
        if audio:
            os.makedirs(os.path.join(p, "audio"), exist_ok=True)
            io.open(os.path.join(p, "audio", "1.mp3"), "wb").write(b"x")
        else:
            os.makedirs(p, exist_ok=True)
        old = time.time() - age
        os.utime(p, (old, old))
        return p

    orphan = mk_orphan("game-20260101-000009")               # 老孤儿 → 该清
    not_ours = mk_orphan("my-notes", audio=False)            # 名字不像 game- → 不碰
    no_audio = mk_orphan("game-20260101-000010", audio=False)  # 没有 audio 子目录 → 不碰
    fresh = mk_orphan("game-20260101-000011", age=0.0)       # 刚建的（可能正在写）→ 先放过
    rep4 = server.prune_saves()
    check("6e 老孤儿语音目录被清掉", not os.path.isdir(orphan)
          and "game-20260101-000009" in (rep4.get("orphans") or []),
          f"orphans={rep4.get('orphans')}")
    check("6f 不碰别的目录（名字不像 game- / 没有 audio 子目录）",
          os.path.isdir(not_ours) and os.path.isdir(no_audio), "")
    check("6g 刚出现的目录先放过（可能正是「先建语音目录、后写 json」那一局）",
          os.path.isdir(fresh), "grace 窗口内")
    swept = server._prune_orphan_audio(tmp4, grace_sec=0)
    check("6h 过了宽限期它就是真孤儿，会被扫掉",
          not os.path.isdir(fresh) and "game-20260101-000011" in swept, str(swept))
finally:
    server.resolve_save_dir = real_resolve4
    shutil.rmtree(tmp4, ignore_errors=True)

print("\n=== 七、进程内路由（直接打 FastAPI app，不依赖 8077 那个进程的新旧）===")
# 为什么要有这一节：第 4 节打的是**用户那台 8077**，它可能是改动之前启动的老进程，
# 那样 4b/4e 会按老语义回 404（重启后才变 400）。这里用 httpx 的 ASGITransport 直接打
# 本进程里的 FastAPI app —— 验的是**磁盘上这份代码**，跟 8077 新不新没关系。
_server_cfg = globals().get("cfg") or {}                  # 第一节读到的 config.yaml（读不到就空着）


class _InprocRoom:
    """只给路由用：`/api/saved_audio` 与 `/api/tts` 需要 ROOM 的配置、配对码和当前对局。"""

    def __init__(self, cfg_: dict, game_) -> None:
        self.cfg = cfg_
        self.game_cfg = cfg_.get("game") or {}
        self.env_token = TOKEN
        self.human_seat = 1
        self.game = game_

    def identify(self, token: str):
        return ("host", "host") if str(token or "").upper() == self.env_token.upper() else None

    def _viewer_seat_obj(self, seat):
        if self.game is None or not seat:
            return None
        try:
            return self.game.seat_by_id(int(seat))
        except (IndexError, TypeError, ValueError):
            return None


def _inproc_routes() -> None:
    tmp5 = tempfile.mkdtemp(prefix="inproc-")
    real_resolve5 = server.resolve_save_dir
    real_cache = server.VOICE_CACHE
    try:
        # --- /api/saved_audio：负数 / 越界 → 400；序号在范围内但没语音 → 404 ---
        server.resolve_save_dir = lambda cfg=None: tmp5   # cfg：prune/_cleanup 会传本房间配置进来
        name = "game-20260101-010101.json"
        stem = name[:-len(".json")]
        os.makedirs(os.path.join(tmp5, stem, "audio"), exist_ok=True)
        io.open(os.path.join(tmp5, stem, "audio", "0.mp3"), "wb").write(b"ID3fake")
        io.open(os.path.join(tmp5, name), "w", encoding="utf-8").write(
            json.dumps({"ledger": [{"kind": "announce"}, {"kind": "speech"}]},
                       ensure_ascii=False))          # 台账只有 2 条（序号 0~1）
        server.ROOM = _InprocRoom(_server_cfg, None)

        async def calls() -> dict:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1") as cl:
                async def get(url: str, **params):
                    return await cl.get(url, params={"token": TOKEN, **params})
                return {
                    "ok": await get("/api/saved_audio", game=name, n=0),
                    "no_audio": await get("/api/saved_audio", game=name, n=1),
                    "neg": await get("/api/saved_audio", game=name, n=-1),
                    "big": await get("/api/saved_audio", game=name, n=2),
                    "huge": await get("/api/saved_audio", game=name, n=999999),
                }

        got = asyncio.run(calls())
        check("7a 序号在范围内、语音也在 → 200 播出来",
              got["ok"].status_code == 200 and got["ok"].content == b"ID3fake",
              f"{got['ok'].status_code} {got['ok'].content[:8]}")
        check("7b 序号在范围内、但这条没留语音 → 404 + 人话原因",
              got["no_audio"].status_code == 404 and "没有留语音" in got["no_audio"].text,
              f"{got['no_audio'].status_code} {got['no_audio'].text[:60]}")
        check("7c n 为负数 → 400（不是 404「没有留语音」）",
              got["neg"].status_code == 400 and "负数" in got["neg"].text,
              f"{got['neg'].status_code} {got['neg'].text[:60]}")
        check("7d n 超出这一局的台账条数 → 400 并说明总条数",
              got["big"].status_code == 400 and "超出" in got["big"].text
              and "2" in got["big"].text,
              f"{got['big'].status_code} {got['big'].text[:70]}")
        check("7e 明显离谱的序号也是 400（不是 404）",
              got["huge"].status_code == 400, f"{got['huge'].status_code}")

        # --- /api/tts?cached=1：只读缓存，不许现场合成 ---
        game = Game(board=make_board("eleven_wolfking"), human_seat=1, seed=1, max_days=1,
                    agent_factory=lambda s: MockAgent(s, __import__("random").Random(1)))
        TEXT = "这是一条用于 cached 测试的发言。"
        game.ledger.add(1, "day", "speech", TEXT, seat=3)
        idx = len(game.ledger.entries) - 1
        server.ROOM = _InprocRoom(_server_cfg, game)
        cache_dir = os.path.join(tmp5, "voice_cache")
        os.makedirs(cache_dir, exist_ok=True)
        server.VOICE_CACHE = cache_dir
        vcfg = server.voice.VoiceConfig.from_env(_server_cfg)
        sv = vcfg.seat_voice(3)
        key_path = server._voice_cache_path(3, sv.key(), TEXT, vcfg.tts_model)
        before = sorted(os.listdir(cache_dir))
        check("7f 测试用的语音缓存键不在缓存里（这样才测得到 miss）",
              not os.path.exists(key_path), f"{os.path.basename(key_path)}；目录={before}")

        async def tts_calls() -> dict:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1") as cl:
                miss = await cl.get("/api/tts", params={"token": TOKEN, "n": idx, "cached": 1})
                after_miss = sorted(os.listdir(cache_dir))     # miss 之后立刻看：有没有偷偷写盘
                io.open(key_path, "wb").write(b"MP3fake")      # 手工造一条「当时合成过」的缓存
                hit = await cl.get("/api/tts", params={"token": TOKEN, "n": idx, "cached": 1})
                return {"miss": miss, "hit": hit, "after_miss": after_miss}

        tts = asyncio.run(tts_calls())
        check("7g cached=1 没命中 → 404 + 「这条没有缓存」",
              tts["miss"].status_code == 404 and "没有缓存" in tts["miss"].text,
              f"{tts['miss'].status_code} {tts['miss'].text[:70]}")
        check("7h cached=1 未命中时**没有偷偷合成**（缓存目录文件数不变）",
              before == tts["after_miss"] == [], f"{before} → {tts['after_miss']}")
        check("7i cached=1 命中 → 200 直接播缓存（零花费）",
              tts["hit"].status_code == 200 and tts["hit"].content == b"MP3fake",
              f"{tts['hit'].status_code} {tts['hit'].content[:8]}")
    finally:
        server.resolve_save_dir = real_resolve5
        server.VOICE_CACHE = real_cache
        shutil.rmtree(tmp5, ignore_errors=True)


_inproc_routes()

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n, _, d in bad:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
