# coding=utf-8
"""独立复核代理 C 在 server.py 里的 6 项修复（进程内直测，不碰用户 .env / 存档 / 8077 服务）。

跑法： python t_inproc.py
证据： stdout（也由外层重定向到日志文件）
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "tmp")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

import server as S  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


def make_room(save_dir: str) -> S.Room:
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8")) or {}
    cfg.setdefault("game", {})
    cfg["game"]["save_dir"] = save_dir
    cfg["game"]["experience"] = False          # 绝不触发真模型调用
    cfg["game"]["human_timeout"] = 2
    room = S.Room(cfg, mock_default=True)
    return room


def spy_broadcast(room: S.Room) -> list[dict]:
    events: list[dict] = []
    orig = room.broadcast

    def spy(payload):
        events.append(dict(payload))
        return orig(payload)

    room.broadcast = spy                                     # type: ignore[method-assign]
    return events


# --------------------------------------------------------------------------- #
# A/B/H: 收尾窗口（cleaning）语义
# --------------------------------------------------------------------------- #

class Hang:
    """aclose() 永远不返回的假客户端 —— 用它把收尾窗口撑开（注意不能用 time.sleep，
    那会把事件循环也一起堵住，测出来的时序是假的）。"""

    async def aclose(self):
        await asyncio.sleep(600)


class Boom:
    async def aclose(self):
        raise RuntimeError("模拟关闭失败")


async def open_window(room: S.Room, clients: list):
    """让这一局立刻'打完'，并让收尾卡在 clients 的 aclose 上 → 得到一个真实的收尾窗口。"""
    await room.start(mock=True, human_seat=1, seed=1)

    async def quick_run():
        await asyncio.sleep(0.05)

    room.game.run = quick_run                                 # type: ignore[assignment]
    room.clients_llm = clients
    await asyncio.sleep(0.6)                                  # 进到 aclose 那一步


async def test_cleanup_window() -> None:
    print("\n=== A/B 收尾窗口：对外报 cleaning 而不是 running ===")
    games = os.path.join(TMP, "games")
    os.makedirs(games, exist_ok=True)
    room = make_room(games)
    events = spy_broadcast(room)
    await open_window(room, [Hang()])

    st = room.status()
    check("A1 收尾窗口内 status.running=False", st.get("running") is False, f"running={st.get('running')!r}")
    check("A2 收尾窗口内 status.cleaning=True", st.get("cleaning") is True, f"cleaning={st.get('cleaning')!r}")
    check("A3 收尾窗口内 room.cleaning 标记已置位", room.cleaning is True)
    check("A4 收尾窗口内 room.running 仍是 True（所以开新局会被拒，不会串局）",
          room.running is True, f"running={room.running}")

    t0 = time.time()
    while time.time() - t0 < 10:
        if not room.cleaning and not room.running:
            break
        await asyncio.sleep(0.1)
    check("A5 收尾结束后 running=False / cleaning=False",
          room.running is False and room.cleaning is False,
          f"running={room.running} cleaning={room.cleaning}")

    end_at = next((i for i, e in enumerate(events) if e.get("type") == "end"), None)
    check("B1 广播里确实出现过 end 事件", end_at is not None)
    if end_at is not None:
        after = events[end_at:]
        bad = [e for e in after if e.get("type") == "status" and e.get("running") is True]
        check("B2 end 之后没有任何 running=True 的 status（原来会闪回'对局进行中'）",
              not bad, f"违例 {len(bad)} 条: {bad[:2]}")
        mid = [e for e in after if e.get("type") == "status"]
        check("B3 end 之后的 status 全是 running=False",
              all(e.get("running") is False for e in mid) and len(mid) >= 1,
              f"{[(e.get('running'), e.get('cleaning')) for e in mid]}")
    first_clean_status = next((e for e in events if e.get("type") == "status" and e.get("cleaning")), None)
    check("B4 收尾一开始就广播过 cleaning=True 的 status", first_clean_status is not None)
    print("     事件序列(type,running,cleaning)："
          + str([(e.get("type"), e.get("running"), e.get("cleaning"))
                 for e in events if e.get("type") == "status"]))


async def test_heal_during_cleanup() -> None:
    print("\n=== H 收尾窗口里来一条 sync（手机重连）会怎样 ===")
    games = os.path.join(TMP, "games")
    room = make_room(games)
    events = spy_broadcast(room)
    await open_window(room, [Hang()])
    check("H1 确实处在收尾窗口里", room.cleaning is True and room.status()["running"] is False,
          f"cleaning={room.cleaning} running={room.running}")

    class FakeWS:
        pass

    await room.handle(FakeWS(), {"type": "sync"})             # type: ignore[arg-type]
    print(f"      sync 之后：running={room.running} cleaning={room.cleaning} "
          f"game={'有' if room.game is not None else 'None'} task={room.task}")

    t0 = time.time()
    while time.time() - t0 < 12:
        if not room.cleaning and not room.running:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    got_end = any(e.get("type") == "end" for e in events)
    check("H2 收尾窗口里收到 sync 之后，结局 end 仍然推给了手机", got_end,
          "❌ 'end' 被吞掉了：手机在收尾窗口重连，就永远看不到「🏁 谁赢了」这一条"
          "（heal_stale_state 只看'任务还在不在'，没排除 cleaning）" if not got_end else "")


# --------------------------------------------------------------------------- #
# C: aclose 超时
# --------------------------------------------------------------------------- #

async def test_aclose_timeout() -> None:
    print("\n=== C aclose 超时：每个客户端最多 5 秒，收尾不再被挂死 ===")
    games = os.path.join(TMP, "games")
    room = make_room(games)
    events = spy_broadcast(room)
    t0 = time.time()
    await open_window(room, [Hang(), Boom(), Hang()])
    while time.time() - t0 < 40:
        if not room.cleaning and not room.running:
            break
        await asyncio.sleep(0.1)
    dt = time.time() - t0
    check("C1 2 个卡死 + 1 个报错的客户端没有把收尾挂死（<25 秒）", dt < 25, f"实测 {dt:.2f}s")
    check("C2 收尾照常推了 end", any(e.get("type") == "end" for e in events))
    check("C3 clients_llm 已清空", room.clients_llm == [])
    print(f"     注意：超时是**每个客户端 5 秒**、串行执行 —— 3 个卡死 ≈15s，"
          f"满座 9 个卡死最坏 ≈45s。本次 2 卡死+1 报错实测 {dt:.1f}s")


# --------------------------------------------------------------------------- #
# D: 队列满不静默丢
# --------------------------------------------------------------------------- #

async def test_queue_drop() -> None:
    print("\n=== D 队列满：记账 + 下次 sync 明确告知 + 丢流水保关键 ===")
    games = os.path.join(TMP, "games")
    room = make_room(games)

    class FakeWS:
        pass

    ws = FakeWS()
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    room.clients[ws] = q                                   # type: ignore[index]
    for i in range(500):
        q.put_nowait({"type": "entry", "n": i})
    room.broadcast({"type": "notice", "text": "1"})
    room.broadcast({"type": "notice", "text": "2"})
    room.send_to(ws, {"type": "notice", "text": "3"})      # type: ignore[arg-type]
    check("D1 三次塞不进去都被记账（dropped=3）", room.dropped.get(ws) == 3,
          f"dropped={room.dropped.get(ws)}")

    # 关键消息必须留着：先清空，再放 entry/thinking/end/ask/notice
    q2: asyncio.Queue = asyncio.Queue(maxsize=50)
    room.clients[ws] = q2                                  # type: ignore[index]
    for payload in ({"type": "entry"}, {"type": "thinking"}, {"type": "end"},
                    {"type": "ask"}, {"type": "notice"}):
        q2.put_nowait(payload)
    dropped_n = room._drain_stale(ws)                       # type: ignore[arg-type]
    kept = []
    while not q2.empty():
        kept.append(q2.get_nowait()["type"])
    check("D2 _drain_stale 丢掉 entry/thinking、保住 end/ask/notice",
          dropped_n == 2 and kept == ["end", "ask", "notice"], f"dropped={dropped_n} kept={kept}")

    # sync：应当收到"你错过了 N 条消息"
    room.dropped[ws] = 3
    await room.handle(ws, {"type": "sync"})                 # type: ignore[arg-type]
    msgs = []
    while not q2.empty():
        msgs.append(q2.get_nowait())
    notice = [m for m in msgs if m.get("type") == "notice" and "错过" in str(m.get("text"))]
    check("D3 sync 时明确告知'你错过了 3 条消息'", bool(notice), f"队列={[m.get('type') for m in msgs]}")
    check("D4 告知之后计数清零", room.dropped.get(ws, 0) == 0, f"dropped={room.dropped.get(ws)}")


# --------------------------------------------------------------------------- #
# F: .env 原子写 + 并发 + 注入 + 回滚
# --------------------------------------------------------------------------- #

def test_env_write() -> None:
    print("\n=== F .env：并发不互相覆盖 / 原子替换 / 注入被挡 / 失败回滚 ===")
    envdir = os.path.join(TMP, "env")
    shutil.rmtree(envdir, ignore_errors=True)
    os.makedirs(envdir, exist_ok=True)
    real = os.path.join(envdir, ".env")
    with io.open(real, "w", encoding="utf-8") as fh:
        fh.write("# 注释要保留\nQWEN_MODEL=old\nQWEN_API_KEY=sk-old-secret\n")

    S.ENV_PATH = real                                       # 只改本进程内的常量，用户的 .env 不动

    # F1 六线程并发各写各的
    def writer(i):
        S.update_env_file({f"QWEN_V{i}_MODEL": f"m{i}"})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = io.open(real, encoding="utf-8").read()
    missing = [f"QWEN_V{i}_MODEL" for i in range(6) if f"QWEN_V{i}_MODEL=m{i}" not in text]
    check("F1 6 线程并发写，6 个变量全都活下来", not missing, f"丢失={missing}")
    check("F2 原来的注释与变量没被吃掉",
          "# 注释要保留" in text and "QWEN_MODEL=old" in text and "QWEN_API_KEY=sk-old-secret" in text)

    # F3 注入：U+2028 / 换行 / 非法变量名
    for bad, label in (("abc\u2028PATH=hacked", "U+2028"),
                       ("abc\nPATH=hacked", "\\n"),
                       ("abc\rPATH=hacked", "\\r"),
                       ("abc\x0bPATH=hacked", "\\x0b"),
                       ("abc\x85PATH=hacked", "NEL \\x85")):
        try:
            S.update_env_file({"QWEN_MODEL": bad})
            check(f"F3 值里的 {label} 被挡住", False, "居然写进去了")
        except ValueError:
            check(f"F3 值里的 {label} 被挡住", True)
    try:
        S.update_env_file({"PATH": "/tmp/x"})
        check("F4 非白名单变量名被挡住", False, "居然写进去了")
    except ValueError as exc:
        check("F4 非白名单变量名被挡住", True, str(exc))

    # F5 原子性：一边高频读、一边狂写长值，读者永远不该看到半截文件
    torn: list[str] = []
    stop = threading.Event()
    long_a, long_b = "A" * 3000, "B" * 3000

    def reader():
        while not stop.is_set():
            try:
                # 用「产品自己的读法」（带毫秒级重试）—— 外部零间隔死循环读者的极端情况
                # 另外在 t_env_race2.py 里单独量，不混进这一项。
                with S._open_text_retry(real) as fh:
                    body = fh.read()
            except OSError as exc:
                torn.append(f"读失败 {type(exc).__name__}: {exc}")
                continue
            vals = [ln.split("=", 1)[1] for ln in body.splitlines()
                    if ln.startswith("QWEN_LONG_MODEL=")]
            if vals and vals[0] not in (long_a, long_b):
                torn.append(f"读到半截值 len={len(vals[0])} 前20={vals[0][:20]!r}")

    rt = threading.Thread(target=reader)
    rt.start()
    errs = []
    for i in range(40):
        try:
            S.update_env_file({"QWEN_LONG_MODEL": long_a if i % 2 else long_b})
        except Exception as exc:                            # noqa: BLE001
            errs.append(f"{type(exc).__name__}: {exc}")
    stop.set()
    rt.join()
    check("F5 并发读写没有读到半截 .env，也没有写失败",
          not torn and not errs, f"torn={torn[:2]} errs={errs[:2]}")

    # F6 写盘失败 → 回滚 + 不残留临时文件
    before = io.open(real, encoding="utf-8").read()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("模拟磁盘满")

    os.replace = boom                                       # type: ignore[assignment]
    raised = None
    try:
        S.update_env_file({"QWEN_MODEL": "should-not-land"})
    except Exception as exc:                                # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    finally:
        os.replace = real_replace                           # type: ignore[assignment]
    after = io.open(real, encoding="utf-8").read()
    leftovers = [f for f in os.listdir(envdir) if ".tmp" in f]
    check("F6 写失败会抛错", raised is not None, str(raised))
    check("F7 写失败后 .env 从 .bak 回滚（内容一位没变）", after == before,
          "内容被改动了" if after != before else "")
    check("F8 不残留 .tmp 文件", not leftovers, str(leftovers))
    check("F9 备份文件存在", os.path.exists(real + ".bak"))


# --------------------------------------------------------------------------- #
# G: 配对码跨重启固定 + 文案是否自相矛盾
# --------------------------------------------------------------------------- #

def test_token_stable() -> None:
    print("\n=== G 配对码跨重启是否真的固定 ===")
    games = os.path.join(TMP, "games")
    r1 = make_room(games)
    r2 = make_room(games)
    check("G1 两次构造 Room（=两次启动）拿到同一个码", r1.env_token == r2.env_token,
          f"{r1.env_token} / {r2.env_token}")
    path = os.path.join(ROOT, "配对码.txt")
    body = io.open(path, encoding="utf-8-sig").read()
    m = S.re.search(r"[A-Z0-9]{6}", body)
    check("G2 文件里的码与服务端一致", bool(m) and m.group() == r1.env_token,
          f"文件={m.group() if m else None} 服务端={r1.env_token}")
    claims_change = ("每次重启服务会换一个新的码" in body)
    check("G3 配对码.txt 的说明文字与实际行为一致（现在是固定的）", not claims_change,
          "❌ 文件里仍写着「每次重启服务会换一个新的码，这个文件会一起更新」——"
          "但这句是 main() 启动时写进去的，和 _load_or_make_token() 里写的正确文案冲突" if claims_change else "")


# --------------------------------------------------------------------------- #

async def main() -> int:
    os.makedirs(TMP, exist_ok=True)
    await test_cleanup_window()
    await test_heal_during_cleanup()
    await test_aclose_timeout()
    await test_queue_drop()
    test_env_write()
    test_token_stable()

    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项检查：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  ❌ {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
