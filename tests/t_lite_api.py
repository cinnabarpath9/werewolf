# coding=utf-8
"""服务端两处修复的接口级复核（2026-10-08）。

P1：`/api/asr` 在客户端**上传中途断开**时不再把异常冒到 ASGI 顶层（stderr 里不再出现
    `Exception in ASGI application` / `ClientDisconnect` traceback），并给一个明确的 499 JSON。
P2：`/api/game/<name>?lite=1` 轻量口径（不带 reasoning/raw/packet 全文）+ 按需取单条
    `/api/game/<name>/call/<i>`（越界 400、档名非法 400、缺码 401、Host 不认 403）。

做法：自己现挑空闲端口拉一台临时实例（`tests/test-config.yaml` → save_dir 是 tests/tmp/games，
绝不碰用户的 games/），跑完关掉；存档用**合成**的一份（transcript 里故意放 120KB 的 reasoning）。
跑法： python t_lite_api.py
"""
from __future__ import annotations

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
    except Exception:                                      # noqa: BLE001
        pass

import httpx                                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
GAMES = os.path.join(HERE, "tmp", "games")
LOG_DIR = os.path.join(HERE, "_logs")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
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
    os.makedirs(LOG_DIR, exist_ok=True)
    out_path = os.path.join(LOG_DIR, f"lite_api-{port}.out.log")
    log = io.open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py", "--port", str(port),
         "--config", os.path.join(HERE, "test-config.yaml"), "--mock-default"],
        cwd=WW, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, out_path, log
    raise RuntimeError(f"临时实例没起来（看 {out_path}）")


#: 合成的存档：一份"大档"（reasoning 很长，模拟真实那局 2.9MB 的形状）
def craft_save(name: str, calls: int = 66, big: int = 60 * 1024) -> str:
    os.makedirs(GAMES, exist_ok=True)
    ledger = []
    for i in range(20):
        kind = "speech" if i % 3 == 0 else ("vote" if i % 3 == 1 else "notice")
        ledger.append({
            "day": 1, "phase": "day", "kind": kind, "seat": (i % 9) + 1,
            "text": f"第 {i} 条台账：<img src=x onerror=\"window.__XSS__=1\"> 3号有问题。",
            "visible_to": ["public"], "data": {}, "audio": (i % 3 == 0),
        })
    transcript = []
    for i in range(calls):
        # 第一条故意是"真人自己那一手"（0/0 tokens），用来验前端标记
        human = (i == 0)
        transcript.append({
            "day": 1, "phase": "day", "seat": 6 if human else (i % 11) + 1,
            "role": "villager", "task": "sheriff_vote" if human else "vote",
            "model": "" if human else "glm-5.3-flash", "fallback": "",
            "packet": "P" * 300,
            "reasoning": ("R%02d " % i) + ("核" * big),
            "raw": '{"vote": %d}' % ((i % 11) + 1),
            "reply": ({"vote": (i % 11) + 1} if not human else {"vote": 7}),
            "thinking": "{}", "prompt_tokens": 0 if human else 8980,
            "completion_tokens": 0 if human else 1069,
            "elapsed": 102.15 if human else 44.01, "error": "",
        })
    save = {"board": "eleven_standard", "day": 2, "winner": "aborted", "aborted": True,
            "started_at": "2026-10-08 09:12", "rules": {}, "sheriff_id": 8, "seats": [],
            "ledger": ledger, "transcript": transcript, "decisions": [], "audio_count": 7}
    path = os.path.join(GAMES, name)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(save, fh, ensure_ascii=False)
    return path


def raw_post_disconnect(port: int, token: str, declared: int = 2 * 1024 * 1024,
                        send_bytes: int = 64 * 1024) -> None:
    """发一半 body 就**掐断**（手机端上传超时后 abort 的现场）。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = (f"POST /api/asr?token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Length: {declared}\r\nContent-Type: audio/webm\r\n"
            "X-Wolf-Client: panel\r\n\r\n")
    s.sendall(head.encode())
    s.sendall(b"\x1a\x45\xdf\xa3" + b"A" * (send_bytes - 4))
    time.sleep(0.4)
    # RST（SO_LINGER 0）：服务端读流时立刻拿到"对端没了"，而不是干等 content-length
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.close()


def main() -> int:
    token = read_token()
    if not token:
        print("读不到配对码，先跑一次 server.py 生成 配对码.txt")
        return 1
    name = "game-zzlite-20261008-000000.json"
    craft_save(name)

    port = free_port()
    proc, log_path, log = start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        print(f"\n=== 一、/api/game/<name>?lite=1 轻量口径（临时实例 {port}）===")
        full = httpx.get(f"{base}/api/game/{name}", params={"token": token},
                         headers={"X-Wolf-Client": "panel"}, timeout=60)
        lite = httpx.get(f"{base}/api/game/{name}", params={"token": token, "lite": 1},
                         headers={"X-Wolf-Client": "panel"}, timeout=60)
        check("1a 默认（不带 lite）行为不变：200 + 完整 JSON",
              full.status_code == 200 and "reasoning" in full.text, f"{full.status_code}")
        check("1b lite=1 → 200 且标了 lite: true",
              lite.status_code == 200 and lite.json().get("lite") is True, f"{lite.status_code}")
        ratio = len(lite.content) / max(1, len(full.content))
        check("1c 响应体显著变小（目标 < 500KB）",
              len(lite.content) < 500 * 1024,
              f"全文 {len(full.content)/1024:.0f}KB → lite {len(lite.content)/1024:.0f}KB"
              f"（{ratio*100:.2f}%）")
        lj = lite.json()
        rows = lj.get("transcript") or []
        check("1d lite 里每条只留小字段 + 一行回复摘要 + reasoning_len",
              rows and all(("reasoning" not in r and "raw" not in r and "packet" not in r
                            and "reply_summary" in r and "reasoning_len" in r and "i" in r)
                           for r in rows),
              f"{len(rows)} 条；第一条键={sorted(rows[0].keys()) if rows else None}")
        check("1e reasoning_len 报的是**长度**（不是内容）",
              rows and isinstance(rows[0]["reasoning_len"], int) and rows[0]["reasoning_len"] > 1000,
              str(rows[0]["reasoning_len"]) if rows else "")
        check("1f 台账只留发言行（复盘要列的那几条）",
              lj.get("ledger") and all(e.get("kind") == "speech" for e in lj["ledger"]),
              f"{len(lj.get('ledger') or [])} 条发言")
        check("1g lite 里没有全文残留（'核' 那种长文本不该出现）",
              "核核核" not in lite.text and "P" * 300 not in lite.text, "")

        print("\n=== 二、/api/game/<name>/call/<i> 按需取单条 ===")
        one = httpx.get(f"{base}/api/game/{name}/call/1", params={"token": token},
                        headers={"X-Wolf-Client": "panel"}, timeout=30)
        oj = one.json() if one.status_code == 200 else {}
        check("2a 第 1 条 → 200，带完整 reasoning / raw / reply",
              one.status_code == 200 and len(str(oj.get("reasoning") or "")) > 1000
              and oj.get("raw") and oj.get("reply") is not None,
              f"{one.status_code} reasoning={len(str(oj.get('reasoning') or ''))} 字")
        check("2b 单条的大小量级是对的（几十 KB，不是整档 2.9MB）",
              20 * 1024 < len(one.content) < 400 * 1024, f"{len(one.content)/1024:.0f}KB")
        check("2c 它和 lite 列表里的 reasoning_len 对得上",
              oj.get("reasoning") and len(str(oj["reasoning"])) == rows[1]["reasoning_len"],
              f"{len(str(oj.get('reasoning') or ''))} vs {rows[1]['reasoning_len']}")
        r400 = httpx.get(f"{base}/api/game/{name}/call/99999", params={"token": token},
                         headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2d 越界序号 → 400 + 人话（不是 404「没有这条」）",
              r400.status_code == 400 and "超出" in r400.text, f"{r400.status_code} {r400.text[:80]}")
        rneg = httpx.get(f"{base}/api/game/{name}/call/-1", params={"token": token},
                         headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2e 负数序号 → 400", rneg.status_code == 400, f"{rneg.status_code}")
        rbad = httpx.get(f"{base}/api/game/x%5Cy.json/call/0", params={"token": token},
                         headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2f 档名里带反斜杠（目录穿越的另一种写法）→ 400",
              rbad.status_code == 400 and "不合法" in rbad.text,
              f"{rbad.status_code} {rbad.text[:60]}")
        rbad2 = httpx.get(f"{base}/api/game/..%5Cconfig.yaml", params={"token": token, "lite": 1},
                          headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2f2 同样的名字打到 lite 口径上也是 400（三处共用一个判据）",
              rbad2.status_code == 400, f"{rbad2.status_code} {rbad2.text[:60]}")
        r401 = httpx.get(f"{base}/api/game/{name}/call/0", timeout=30)
        check("2g 缺配对码 → 401", r401.status_code == 401, f"{r401.status_code}")
        r403 = httpx.get(f"{base}/api/game/{name}/call/0", params={"token": token},
                         headers={"Host": "evil.example.com", "X-Wolf-Client": "panel"}, timeout=30)
        check("2h 不认识的 Host → 403", r403.status_code == 403, f"{r403.status_code}")
        rmiss = httpx.get(f"{base}/api/game/game-nope.json/call/0", params={"token": token},
                          headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2i 存档不存在 → 404", rmiss.status_code == 404, f"{rmiss.status_code}")
        rnan = httpx.get(f"{base}/api/game/{name}/call/abc", params={"token": token},
                         headers={"X-Wolf-Client": "panel"}, timeout=30)
        check("2j 序号不是数字 → 400（不是 FastAPI 的 422 detail）",
              rnan.status_code == 400 and "error" in rnan.text and "detail" not in rnan.text,
              f"{rnan.status_code} {rnan.text[:80]}")

        print("\n=== 三、/api/asr 客户端中途断开（P1）===")
        before = io.open(log_path, encoding="utf-8", errors="replace").read()
        raw_post_disconnect(port, token)
        time.sleep(1.5)
        after = io.open(log_path, encoding="utf-8", errors="replace").read()
        new = after[len(before):]
        check("3a 服务端记了一句「上传中断」（认出来了，而不是无声无息）",
              "上传中断" in new, new.strip().splitlines()[-1][:120] if new.strip() else "(没有新日志)")
        check("3b ⚠️ stderr/stdout 里**没有** ClientDisconnect 的 traceback",
              "ClientDisconnect" not in new and "Exception in ASGI application" not in new,
              new[-300:].replace("\n", " ⏎ ") if new else "")
        check("3c 断开之后服务端还活着（下一条请求照常 200）",
              httpx.get(f"{base}/api/status", params={"token": token},
                        headers={"X-Wolf-Client": "panel"}, timeout=20).status_code == 200)

        print("\n=== 四、/api/asr 原有闸门没被弄丢（回归）===")
        # ⚠️ 声明 30MB 这一条必须走**裸 socket**：httpx 不允许 content-length 和 body 不一致
        #    （h11 会直接 LocalProtocolError），而这条测试要验的正是"body 一个字节都没发"。
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall((f"POST /api/asr?token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Length: {30 * 1024 * 1024}\r\nContent-Type: audio/webm\r\n"
                   "X-Wolf-Client: panel\r\n\r\n").encode())
        s.settimeout(5)
        raw413 = b""
        want = "录音太大".encode("utf-8")
        try:
            # ⚠️ 要一直读到 body：413 的正文可能是第二个 TCP 段（只 recv 一次会读成空 body，
            #    于是"是不是我们自己的 JSON 提示"这一条就假红 —— 第一版就是这么红的）。
            while want not in raw413 and len(raw413) < 65536:
                piece = s.recv(65536)
                if not piece:
                    break
                raw413 += piece
        except OSError:
            pass
        s.close()
        check("4a 声明 30MB、body 一个字节都不发 → 立刻 413「录音太大」",
              b"413" in raw413 and "录音太大".encode("utf-8") in raw413,
              raw413.split(b"\r\n", 1)[0].decode("utf-8", "replace") if raw413 else "(无回应)")
        r400 = httpx.post(f"{base}/api/asr", params={"token": token}, content=b"",
                          headers={"X-Wolf-Client": "panel", "Content-Type": "audio/webm"}, timeout=20)
        check("4b 空 body → 400「没收到音频」",
              r400.status_code == 400 and "没收到音频" in r400.text, f"{r400.status_code}")
        r401b = httpx.post(f"{base}/api/asr", content=b"x",
                           headers={"X-Wolf-Client": "panel", "Content-Type": "audio/webm"}, timeout=20)
        check("4c 缺配对码 → 401", r401b.status_code == 401, f"{r401b.status_code}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:                                  # noqa: BLE001
            try:
                proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        try:
            log.close()
        except Exception:                                  # noqa: BLE001
            pass
        # 只删自己合成的那一份存档（绝不碰别的）
        try:
            os.remove(os.path.join(GAMES, name))
        except OSError:
            pass

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
