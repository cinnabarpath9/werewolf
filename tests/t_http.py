# coding=utf-8
"""HTTP 层复核：/api/asr 体积上限是否在读 body 之前生效、只读接口退避、token 闸门。

对 8086 上的**临时测试实例**打（临时 save_dir、只跑 mock、不花模型钱）。
注意执行顺序：退避那一节会把自己的 IP 锁 30 秒，所以放在最后。

跑法： python t_http.py
"""
from __future__ import annotations

import io
import os
import re
import socket
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

import httpx

#: 跑在哪台测试实例上。默认 8086（老行为）；`run_all.py` 用 `WW_TEST_PORT` 覆盖
#: （2026-10-11 提速：服务端单房间，拆几台实例才能并行，见 run_all 的 TEST_INSTANCES）。
PORT = int(os.environ.get("WW_TEST_PORT") or 8086)
BASE = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = ""
for line in io.open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig"):
    m = re.search(r"[A-Z0-9]{6}", line)
    if m:
        TOKEN = m.group()
        break
print(f"配对码（来自 配对码.txt）：{TOKEN}")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


def read_full(s: socket.socket, got: bytes, seconds: float) -> bytes:
    """把整个响应读完（含 body）：只看到 \\r\\n\\r\\n 就返回会把正文漏掉。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        head, _, rest = got.partition(b"\r\n\r\n")
        if head:
            m = re.search(rb"content-length:\s*(\d+)", head, re.I)
            if m and len(rest) >= int(m.group(1)):
                return got
            if b"transfer-encoding: chunked" in head.lower() and rest.endswith(b"0\r\n\r\n"):
                return got
        s.settimeout(max(0.05, deadline - time.time()))
        try:
            piece = s.recv(65536)
        except (socket.timeout, ConnectionResetError, OSError):
            return got
        if not piece:
            return got
        got += piece
    return got


def status_of(raw: bytes) -> str:
    return raw.split(b"\r\n", 1)[0].decode("utf-8", "replace") if raw else "(无回应)"


def body_of(raw: bytes) -> str:
    return raw.partition(b"\r\n\r\n")[2].decode("utf-8", "replace")[:200]


def asr_head_only(declared: int) -> tuple[bytes, float]:
    """只发请求头（含 Content-Length: declared），一个字节 body 都不发。"""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=15)
    head = (f"POST /api/asr?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
            f"Content-Length: {declared}\r\nContent-Type: audio/webm\r\n"
            "X-Wolf-Client: panel\r\n\r\n")
    t0 = time.time()
    s.sendall(head.encode())
    raw = read_full(s, b"", 5.0)
    s.close()
    return raw, time.time() - t0


def main() -> int:
    print("\n=== 1. /api/asr：25MB 上限是不是在“读 body 之前”生效 ===")
    raw, dt = asr_head_only(30 * 1024 * 1024)
    check("1a 声明 30MB、body 一个字节都不发 → 服务端立刻回 413（说明没去等 body）",
          b"413" in raw and dt < 3.0, f"{status_of(raw)}  用了 {dt:.2f}s")
    check("1b 413 是**我们自己的** JSON 提示（不是中间件顺手挡的）",
          "录音太大" in body_of(raw) and '"error"' in body_of(raw), body_of(raw))

    # 1c 不带 content-length（分块发），看它在第几 MB 回话
    s = socket.create_connection(("127.0.0.1", PORT), timeout=15)
    s.sendall((f"POST /api/asr?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
               "Transfer-Encoding: chunked\r\nContent-Type: audio/webm\r\n"
               "X-Wolf-Client: panel\r\n\r\n").encode())
    chunk = b"\x00" * (1024 * 1024)
    sent, got, stopped = 0, b"", None
    for _ in range(40):
        s.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        sent += len(chunk)
        s.settimeout(0.08)
        try:
            got += s.recv(65536)
        except socket.timeout:
            pass
        if got:
            stopped = sent
            break
    got = read_full(s, got, 2.0)
    s.close()
    check(f"1c 分块猛灌：服务端在第 {stopped/1048576:.0f}MB 就回 413（上限 25MiB=26.2MB），"
          "没有把整个 body 吞完",
          b"413" in got and stopped is not None and stopped <= 26 * 1024 * 1024,
          f"{status_of(got)}  已发 {sent/1048576:.1f}MB，回话时已发 {(stopped or 0)/1048576:.1f}MB")

    r = httpx.post(f"{BASE}/api/asr?token={TOKEN}", content=b"",
                   headers={"X-Wolf-Client": "panel", "Content-Type": "audio/webm"}, timeout=20)
    check("1d 空 body → 400「没收到音频」，且这一路不会去打真实 ASR（不花钱）",
          r.status_code == 400 and "没收到音频" in r.text, f"{r.status_code} {r.text[:120]}")

    print("\n=== 2. 别的闸门（回归，先做，免得被退避影响） ===")
    r = httpx.get(f"{BASE}/api/status", timeout=20)
    check("2a /api/status 无码 → 401，正文里没有 your_role / your_mates",
          r.status_code == 401 and "your_role" not in r.text and "your_mates" not in r.text,
          f"{r.status_code} {r.text[:100]}")
    r = httpx.get(f"{BASE}/api/status", params={"token": TOKEN}, timeout=20)
    check("2b 带码的 /api/status → 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    r = httpx.get(f"{BASE}/api/games", params={"token": TOKEN}, headers={"Host": "evil.example.com"},
                  timeout=20)
    check("2c 陌生 Host → 403（挡 DNS rebinding）", r.status_code == 403, f"{r.status_code} {r.text[:80]}")
    r = httpx.get(f"{BASE}/api/tts", params={"token": TOKEN, "n": "abc"}, timeout=20)
    check("2d /api/tts?n=abc → 400 + {\"error\"}（不是 FastAPI 的 422 detail）",
          r.status_code == 400 and "error" in r.text and "detail" not in r.text,
          f"{r.status_code} {r.text[:120]}")
    r = httpx.get(f"{BASE}/api/game/x%5Cy.json", params={"token": TOKEN}, timeout=20)
    check("2e 存档名里带反斜杠 → 400 bad name", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    r = httpx.get(f"{BASE}/api/games", params={"token": TOKEN}, headers={"X-Wolf-Client": "panel"},
                  timeout=20)
    check("2f 正确的码 → 200 且拿到存档列表", r.status_code == 200 and "games" in r.text,
          f"{r.status_code} {r.text[:120]}")

    print("\n=== 3. 只读接口退避（以前只有写接口有） ===")
    codes = []
    for _ in range(8):
        rr = httpx.get(f"{BASE}/api/games", params={"token": "WRONG1"},
                       headers={"X-Wolf-Client": "panel"}, timeout=20)
        codes.append((rr.status_code, rr.json().get("error", "")[:20]))
    print("      前 8 次错码：", codes[0], "…（共 8 次）")
    check("3a 错码一律 401 + 人话提示", all(c == 401 for c, _ in codes))
    rr9 = httpx.get(f"{BASE}/api/games", params={"token": "WRONG1"},
                    headers={"X-Wolf-Client": "panel"}, timeout=20)
    check("3b 第 9 次开始进入退避（换成'等 30 秒'的提示）",
          rr9.status_code == 401 and "太多次" in rr9.text, f"{rr9.status_code} {rr9.text[:120]}")
    rr_ok = httpx.get(f"{BASE}/api/games", params={"token": TOKEN},
                      headers={"X-Wolf-Client": "panel"}, timeout=20)
    check("3c 退避期间连**正确**的码也一起挡（同一张表、同一套窗口）",
          rr_ok.status_code == 401 and "太多次" in rr_ok.text,
          f"{rr_ok.status_code} {rr_ok.text[:120]}")

    print("\n=== 4. 读写接口共用同一张失败表 ===")
    rw = httpx.post(f"{BASE}/api/settings/env", json={"token": "WRONG2", "updates": {"QWEN_MODEL": "x"}},
                    headers={"X-Wolf-Client": "panel"}, timeout=20)
    check("4a 写接口（错码）回的是同一句退避文案 → 共用一张表",
          "太多次" in rw.text, f"{rw.status_code} {rw.text[:120]}")

    print("\n=== 5. 退避 30 秒后能不能自己恢复 ===")
    time.sleep(31)
    rr = httpx.get(f"{BASE}/api/games", params={"token": TOKEN},
                   headers={"X-Wolf-Client": "panel"}, timeout=20)
    check("5a 等 31 秒后用正确的码 → 200（不会永久锁死）",
          rr.status_code == 200 and "games" in rr.text, f"{rr.status_code} {rr.text[:100]}")
    rr = httpx.get(f"{BASE}/api/games", params={"token": "WRONG1"},
                   headers={"X-Wolf-Client": "panel"}, timeout=20)
    check("5b 退避过去后错码回到普通提示（计数已重置）",
          rr.status_code == 401 and "太多次" not in rr.text, f"{rr.status_code} {rr.text[:100]}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
