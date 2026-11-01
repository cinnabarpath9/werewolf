# coding=utf-8
"""正常语音路径回归：小段音频 → /api/asr → 识别文字 + 号码猜测。

对 8087 上那个把 VOICE_BASE_URL 指向本机假端点的临时实例打，所以**不花语音钱**。
跑法： python t_asr_path.py
"""
from __future__ import annotations

def _skip_if_no_8087() -> bool:
    """这个脚本要打在 8087 上的"假语音"临时实例（VOICE_BASE_URL 指向本机假端点）。

    那台实例不归 runner 管（要单独起、还要设环境变量），所以这里**端口没起就明确跳过** ——
    以前是直接 ConnectionRefused 报红，看起来像"语音坏了"，其实只是没起实例。

    真跑法：
        set VOICE_BASE_URL=http://127.0.0.1:8095/v1 & set VOICE_API_KEY=fake
        python server.py --port 8087 --config werewolf/tests/test-config.yaml --mock-default
    """
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", 8087)) != 0:
            print("  SKIP：需要 8087 上的假语音临时实例（见文件头注释），当前没起 → 跳过")
            return True
    return False


import hashlib
import io
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

import httpx

PORT = 8087
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = ""
for line in io.open(os.path.join(ROOT, "配对码.txt"), encoding="utf-8-sig"):
    m = re.search(r"[A-Z0-9]{6}", line)
    if m:
        TOKEN = m.group()
        break

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


def main() -> int:
    if _skip_if_no_8087():
        return 0
    # 8KB 的假 webm（EBML 魔数开头，让 guess_format 认出 webm）
    blob = b"\x1a\x45\xdf\xa3" + os.urandom(8 * 1024 - 4)
    r = httpx.post(f"{BASE}/api/asr", params={"token": TOKEN, "legal": "1,2,3,4,5"},
                   content=blob, headers={"X-Wolf-Client": "panel", "Content-Type": "audio/webm"},
                   timeout=30)
    check("A1 小段音频正常走通（200）", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    try:
        j = r.json()
    except Exception:                                   # noqa: BLE001
        j = {}
    check("A2 返回识别文字", j.get("text") == "四号", json.dumps(j, ensure_ascii=False)[:160])
    check("A3 顺带猜出号码 4（中文数字也能认）", j.get("seat") == 4, f"seat={j.get('seat')}")

    # 不带 legal 时不该乱猜号码
    r2 = httpx.post(f"{BASE}/api/asr", params={"token": TOKEN}, content=blob,
                    headers={"X-Wolf-Client": "panel", "Content-Type": "audio/webm"}, timeout=30)
    j2 = r2.json()
    check("A4 没传 legal 时 seat 为 null（不乱猜）", j2.get("seat") is None, json.dumps(j2, ensure_ascii=False)[:120])

    # 音频格式嗅探：content-type 撒谎报 mp3、但字节是 webm 魔数 → 应当按 webm 发出去
    r3 = httpx.post(f"{BASE}/api/asr", params={"token": TOKEN}, content=blob,
                    headers={"X-Wolf-Client": "panel", "Content-Type": "audio/mpeg"}, timeout=30)
    check("A5 谎报 mime 也按魔数走（200 + 照样识别）", r3.status_code == 200, f"{r3.status_code}")

    log = os.path.join(HERE, "fake_asr_hits.jsonl")
    hits = [json.loads(x) for x in io.open(log, encoding="utf-8")] if os.path.exists(log) else []
    print("\n  假端点收到的请求：")
    for h in hits:
        print("   ", json.dumps(h, ensure_ascii=False))
    check("A6 服务端真的把音频发出去了（假端点收到 3 次）", len(hits) == 3, f"{len(hits)} 次")
    if hits:
        h = hits[0]
        check("A7 用的是 data: URI（裸 base64 会被当成 URL 去下载，实测报 FILE_DOWNLOAD_FAILED）",
              h["uri_is_data_scheme"] and h["data_uri_prefix"].startswith("data:audio/"),
              h["data_uri_prefix"])
        check("A8 带上了 X-DashScope-SSE: disable（少了它拿不到一次性结果）",
              (h["sse_header"] or "").lower() == "disable", str(h["sse_header"]))
        check("A9 format=webm、sample_rate=16000 照旧",
              h["format"] == "webm" and h["sample_rate"] == "16000",
              f"format={h['format']} rate={h['sample_rate']}")
        check("A10 音频字节**原样**送达（SHA1 一致，分块读没有截断/多拼字节）",
              all(h["decoded_len"] == len(blob) and h["decoded_sha1"] == hashlib.sha1(blob).hexdigest()
                  for h in hits),
              f"收到 {hits[0]['decoded_len']} 字节 / 发出 {len(blob)} 字节；"
              f"sha1 收到 {hits[0]['decoded_sha1'][:12]} / 发出 {hashlib.sha1(blob).hexdigest()[:12]}")
        check("A11 三次请求里 mime 撒谎那次也被识别成 webm（魔数优先）",
              hits[-1]["format"] == "webm", f"第三次 format={hits[-1]['format']}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
