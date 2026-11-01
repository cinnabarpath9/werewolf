# coding=utf-8
"""页面测试：手机端「自检」面板（浏览器支持情况）。

为什么要有：用户问"我手机上用的什么浏览器、要不要换"——服务端只能看到 UA（还会被截断），
所以把判断搬到手机上：面板直接显示 安全上下文 / 麦克风 / 录音 / 权限 / 连接 / PWA / 屏幕。
"""
from __future__ import annotations

import asyncio
import os
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_lite import Page                                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
from live_token import TOKEN
URL = f"http://127.0.0.1:8077/?token={TOKEN}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


async def main() -> int:
    p = Page(port=9746, profile=os.path.join(HERE, "_profiles", "edge-diag-panel"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)
        r = await p.js("""
          (async () => {
            const btn = document.getElementById('btnDiag');
            if (!btn) return {noBtn: true};
            btn.click();
            await new Promise(r => setTimeout(r, 600));
            const box = document.querySelector('[data-panel="diag"]');
            return {exists: !!box, text: box ? box.innerText : '',
                    html: box ? box.innerHTML : ''};
          })()
        """)
        check("D1 头部有「自检」按钮并打得开面板", r.get("noBtn") is not True and r["exists"] is True,
              str(r.get("noBtn")))
        t = r["text"]
        check("D2 显示浏览器标识（UA）", "浏览器标识" in t and "Mozilla/5.0" in t, t[:80])
        check("D3 显示安全上下文（并说明是 https/localhost）",
              "安全上下文" in t and ("是" in t or "否" in t), "")
        check("D4 显示麦克风接口与录音接口",
              "麦克风接口" in t and "MediaRecorder" in t)
        check("D5 显示麦克风权限状态", "麦克风权限" in t)
        check("D6 显示与服务的连接状态", "与服务的连接" in t)
        check("D7 显示是否已装成 PWA", "PWA" in t or "添加到主屏幕" in t)
        check("D8 显示屏幕/视口尺寸（美化时要看）", "屏幕" in t and "视口" in t)
        check("D9 有「常见问题怎么办」的四条说明",
              "常见问题怎么办" in t and "https" in t and "后台回收" in t)
        check("D11 有麦克风自测按钮（不用开局就能验权限）",
              "测一下麦克风" in t and "不发给任何人" in t, "")
        r2 = await p.js("""
          (async () => {
            const btn = document.getElementById('btnMicTest');
            if (!btn) return {noBtn: true};
            btn.click();
            await new Promise(r => setTimeout(r, 2600));
            const out = document.getElementById('micTestOut');
            return {out: out ? out.innerText : ''};
          })()
        """)
        out = str(r2.get("out", ""))
        check("D12 点它之后有明确结论（成功 / 失败原因，不是一直转圈）",
              ("正常" in out) or ("失败" in out) or ("没录到" in out), out[:90])
        check("D13 无头浏览器里没有真麦克风 → 也给人话原因（不抛异常）",
              ("失败" in out) or ("没录到" in out) or ("正常" in out), out[:90])
        check("D10 面板里没有注入 iframe/脚本（转义到位）",
              "<iframe" not in r["html"] and "onerror" not in r["html"])

        bad = [x for x in RESULTS if not x[1]]
        print("\n" + "=" * 74)
        print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
        for n, _, d in bad:
            print(f"  [FAIL] {n}  {d}")
        print("=" * 74)
        return 1 if bad else 0
    finally:
        try:
            p.kill()
        except Exception:
            pass


raise SystemExit(asyncio.run(main()))
