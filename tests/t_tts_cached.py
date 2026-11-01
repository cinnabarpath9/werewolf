# coding=utf-8
"""`/api/tts?cached=1`（只读缓存）的服务端口径复核 —— 前端那个「🔊 重放」靠它做到"不偷偷花钱"。

怎么做到**一个真实合成都不发**（省钱是这条修复的全部意义，测试本身当然也不能乱花钱）：
  · 先用 `wolf/voice.py` 自己的规则算出两条发言的缓存路径，往里各放一份**假的** mp3；
  · 于是这两条在服务端眼里都是"缓存里有"（`/api/tts` 会直接回文件，不会去合成）；
  · 再用一个**必然没有缓存**的序号（台账里没有的号）验"没命中 → 404"。
    —— 服务端对"没有这条"和"没有缓存"回的都是 404，但 body 不同，用 body 区分；
       而且这台实例真的配了语音 key，所以"没命中时回 404 而不是去合成"这件事
       只能靠"body 是『这条没有缓存』"加上"缓存目录没有多出新文件"来证明。

查这些：
  ① 命中缓存：cached=1 → 200 + 那份字节；不带 cached 也命中同一份（老路径没坏）
  ② 没命中缓存：cached=1 → 404 + "这条没有缓存"，**不合成、不写缓存**
  ③ 认不出来的 cached 值 → 当没开（不 422）
  ④ n 不合法仍然 400、缺配对码仍然 401、不认识的 Host 仍然 403（加参数没把闸门弄丢）

跑法： python t_tts_cached.py
（自己拉起一个临时实例、跑完关掉；端口现挑空闲的，不占 8086。）
⚠️ 它会往**语音缓存目录**里临时写两份假音频，跑完删掉（只碰它自己算出来的那两个文件名）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass

import httpx                                              # noqa: E402
import websockets                                         # noqa: E402
import yaml                                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
GAMES = os.path.join(HERE, "tmp", "games")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def read_token() -> str:
    for line in io.open(os.path.join(WW, "配对码.txt"), encoding="utf-8-sig"):
        m = re.search(r"[A-Z0-9]{6}", line)
        if m:
            return m.group()
    return ""


def start_server(port: int):
    log = io.open(os.path.join(HERE, "_logs", f"tts_cached-{port}.out.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py", "--port", str(port),
         "--config", os.path.join(HERE, "test-config.yaml"), "--mock-default"],
        cwd=WW, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        time.sleep(0.5)
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, base, log
    raise RuntimeError("临时实例没起来（看 _logs/tts_cached-*.out.log）")


async def collect_speeches(port: int, token: str, want: int = 2) -> list[tuple[int, int, str]]:
    """开一局 mock，收几条"别人的发言"，返回 [(台账序号, 座位, 正文)]。"""
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}",
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_dream", "seed": 3}))
        t0 = time.time()
        got: list[tuple[int, int, str]] = []
        while time.time() - t0 < 90 and len(got) < want:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "entry":
                e = m["entry"]
                if e.get("kind") == "speech" and e.get("seat") not in (None, 6) \
                        and e.get("n") is not None:
                    got.append((int(e["n"]), int(e["seat"]), str(e.get("text") or "")))
            elif m.get("type") == "ask":
                await ws.send(json.dumps({"type": "action",
                                          "payload": {"abstain": True, "speech": "（测试）我过。",
                                                      "reason": "只读缓存口径测试"}}))
        if not got:
            raise RuntimeError("mock 局里没等到别人的发言")
        return got


def cache_path(seat: int, text: str) -> str:
    """按服务端的规则算缓存文件路径（`server._voice_cache_path` + `voice.SeatVoice.key`）。

    ⚠️ 2026-10-08 改（审查发现的 P3-5）：缓存目录**直接问 server.py 要**，
    不再自己按项目根 config.yaml 拼一份。
    为什么：这个脚本会把假音频写进缓存目录再删掉 —— 以前它算的是**线上共用**的
    `var/voice_cache`（`server.py` 那时只认项目根 config.yaml），
    今天只是恰好没撞上线上条目，哪天有人把线上音色抄进 tests/test-config.yaml，
    测试就会覆盖线上真实缓存。现在 server.py 认 `--config`：
    跑 `--config tests/test-config.yaml` 的实例，缓存在 tests/var/voice_cache。
    """
    sys.path.insert(0, WW)
    os.chdir(WW)
    from wolf import voice as wolf_voice                      # noqa: E402
    with io.open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    vcfg = wolf_voice.VoiceConfig.from_env(cfg)
    sv = vcfg.seat_voice(seat)
    key = hashlib.sha1(f"{seat}|{vcfg.tts_model}|{sv.key()}|{text}".encode("utf-8")).hexdigest()[:20]
    #: 和测试实例（`start_server` 用 `--config tests/test-config.yaml`）算出来的**同一份**
    import importlib                                        # noqa: E402
    sys.argv = [a for a in sys.argv if not a.startswith("--config")]
    sys.argv += ["--config", os.path.join(HERE, "test-config.yaml")]
    server = importlib.import_module("server")
    d = server._voice_cache_dir()
    return os.path.join(d, f"{key}.mp3")


def main() -> int:
    token = read_token()
    print(f"配对码：{token}")
    os.makedirs(GAMES, exist_ok=True)
    port = free_port()
    proc, base, log = start_server(port)
    print(f"临时实例：{base}（现挑的空闲端口，不占 8086）")
    hdr = {"X-Wolf-Client": "panel"}
    made: list[str] = []
    try:
        speeches = asyncio.run(collect_speeches(port, token, want=2))
        hit = speeches[0]
        print(f"打算当'有缓存'的那条：台账 n={hit[0]}，{hit[1]}号：「{hit[2][:26]}」")
        if len(speeches) > 1:
            print(f"        另一条（不会被写缓存）：n={speeches[1][0]}，{speeches[1][1]}号")

        # ① 命中缓存：手写两份假音频（用服务端的规则算路径）
        payload = b"ID3\x03\x00\x00\x00" + bytes(range(64))
        for (n, seat, text) in speeches:
            p = cache_path(seat, text)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "wb") as fh:
                fh.write(payload)
            made.append(p)
        print(f"     写好 {len(made)} 份假音频（跑完删）")

        r_hit = httpx.get(f"{base}/api/tts", params={"n": hit[0], "cached": "1", "token": token},
                          headers=hdr, timeout=15)
        check("①-1 命中缓存：cached=1 → 200 + 那份字节",
              r_hit.status_code == 200 and r_hit.content == payload,
              f"HTTP {r_hit.status_code} {len(r_hit.content)} 字节 {r_hit.text[:50]}")
        check("①-2 返回的是音频（Content-Type: audio/mpeg）",
              "audio/mpeg" in (r_hit.headers.get("content-type") or ""),
              r_hit.headers.get("content-type"))
        r_old = httpx.get(f"{base}/api/tts", params={"n": hit[0], "token": token},
                          headers=hdr, timeout=15)
        check("①-3 不带 cached 也命中同一份（老路径没被改坏）",
              r_old.status_code == 200 and r_old.content == payload,
              f"HTTP {r_old.status_code}")

        r_weird = httpx.get(f"{base}/api/tts", params={"n": hit[0], "cached": "abc", "token": token},
                            headers=hdr, timeout=15)
        check("①-4 认不出来的 cached 值当没开（不 422；这条有缓存所以照样 200）",
              r_weird.status_code == 200, f"HTTP {r_weird.status_code}")

        # ② 没命中缓存：先把它那份缓存**删掉**（这条发言还在台账里、别的检查都不受影响），
        #    再打 cached=1 —— 必须 404，而且绝不能现合成 + 写缓存。
        miss = speeches[1] if len(speeches) > 1 else None
        if miss is None:
            check("②-0 需要第二条发言来验'没命中'（这局只收到一条）", False, str(speeches))
        else:
            miss_path = cache_path(miss[1], miss[2])
            try:
                os.remove(miss_path)
                made = [p for p in made if p != miss_path]
            except OSError as exc:                            # noqa: BLE001
                check("②-0 能删掉第二条的假缓存（构造'没命中'）", False, str(exc))
            before = set(os.listdir(os.path.dirname(miss_path)))
            r_miss = httpx.get(f"{base}/api/tts", params={"n": miss[0], "cached": "1", "token": token},
                               headers=hdr, timeout=15)
            body = {}
            try:
                body = r_miss.json()
            except Exception:                                 # noqa: BLE001
                pass
            check(f"②-1 没命中（台账 n={miss[0]} 这条没有缓存）：cached=1 → 404",
                  r_miss.status_code == 404, f"HTTP {r_miss.status_code} {r_miss.text[:60]}")
            check("②-2 404 的 body 是前端看得懂的 {\"error\": ...}（不是 FastAPI 的 detail）",
                  isinstance(body, dict) and bool(body.get("error")) and "detail" not in body,
                  str(body)[:80])
            check("②-3 404 说的是'没有缓存'（而不是'没有这条'）",
                  "缓存" in str(body.get("error") or ""), str(body.get("error"))[:60])
            after = set(os.listdir(os.path.dirname(miss_path)))
            check("②-4 ⚠️ 缓存目录没有多出新文件（只读口径真的没合成、没写缓存）",
                  after == before, f"多了：{sorted(after - before)[:4]}")

        # ③ 闸门照旧
        r_bad_n = httpx.get(f"{base}/api/tts", params={"n": "abc", "cached": "1", "token": token},
                            headers=hdr, timeout=15)
        check("③-1 n 不合法仍然 400", r_bad_n.status_code == 400, f"HTTP {r_bad_n.status_code}")
        r_notoken = httpx.get(f"{base}/api/tts", params={"n": hit[0], "cached": "1"}, headers=hdr,
                              timeout=15)
        check("③-2 缺配对码仍然 401（只读口径也绕不开闸门）",
              r_notoken.status_code == 401, f"HTTP {r_notoken.status_code}")
        r_badhost = httpx.get(f"{base}/api/tts", params={"n": hit[0], "cached": "1", "token": token},
                              headers={"X-Wolf-Client": "panel", "Host": "evil.example"},
                              timeout=15)
        check("③-3 不认识的 Host 仍然 403", r_badhost.status_code == 403,
              f"HTTP {r_badhost.status_code}")
    finally:
        for p in made:
            try:
                os.remove(p)
            except OSError:
                pass
        if made:
            print(f"     已清掉 {len(made)} 份假音频")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                                     # noqa: BLE001
            proc.kill()
        log.close()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(main())
