# coding=utf-8
"""把最后一条动态缺口补上：**切后台时，在飞的 TTS 回来不许播**（第 3 项）。

做法（避开子代理踩的"两个 VOICE 实例"坑）：用 CDP 的
`Page.addScriptToEvaluateOnNewDocument` 在**页面脚本执行之前**装好桩
（Audio.play / createObjectURL / fetch 全部可控），然后刷新页面 ——
页面里只有它自己的那一份 VOICE，没有第二份。
"""
from __future__ import annotations

import asyncio
import json
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

STUB = r"""
(() => {
  window.__created = []; window.__revoked = []; window.__plays = 0; window.__pending = [];
  const _c = URL.createObjectURL.bind(URL), _r = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (b) => { const u = _c(b); window.__created.push(u); return u; };
  URL.revokeObjectURL = (u) => { window.__revoked.push(u); return _r(u); };
  HTMLMediaElement.prototype.play = function () { window.__plays++; return Promise.resolve(); };
  const _f = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = String(input && input.url ? input.url : input);
    if (url.indexOf('/api/tts') === 0) {
      return new Promise((res, rej) => { window.__pending.push({res, rej}); });
    }
    return _f(input, init);
  };
})();
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


SNAP = ("({created: __created.length, revoked: __revoked.length, plays: __plays, "
        "pending: __pending.length, vNow: document.getElementById('vNow').textContent})")


async def say_and_snapshot(p: Page, label: str) -> dict:
    await p.js("VOICE.say({seat: 4, n: 1, say: true, kind: 'speech'}); 'sent'")
    await asyncio.sleep(0.3)
    s = await p.js(SNAP)
    print(f"     {label}：{s}")
    return s


async def main() -> int:
    p = Page(port=9731, profile=os.path.join(HERE, "_profiles", "edge-t3"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.send("Page.addScriptToEvaluateOnNewDocument", {"source": STUB})
        await p.send("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(3.0)                            # 等页面重新加载完
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        armed = await p.js("({stub: typeof __created, audioCount: (window.__audios||[]).length})")
        print(f"     桩已装好：{armed}")
        check("0 桩在页面脚本之前就装好了（单实例，无第二份 VOICE）",
              armed["stub"] == "object", str(armed))

        # ---- 对照组：不管它，等到请求回来 → 应该照常播 ----
        base = await p.js(SNAP)
        s1 = await say_and_snapshot(p, "对照组：say() 之后（请求挂在桩上）")
        await p.js("__pending.shift().res({ok: true, status: 200, "
                   "blob: async () => new Blob([new Uint8Array(4000)], {type: 'audio/mpeg'})}); 'resolved'")
        await asyncio.sleep(0.5)
        s2 = await p.js(SNAP)
        print(f"     对照组：请求回来之后：{s2}")
        check("1 对照组：没人拦它 → 真的建了地址并 play（说明这个测法不是假绿）",
              s2["created"] - base["created"] == 1 and s2["plays"] - base["plays"] == 1,
              f"+created={s2['created'] - base['created']} +plays={s2['plays'] - base['plays']}")

        # ---- 实验组：请求在飞的时候切后台 ----
        await p.js("VOICE.stop(); window.__mark = {created: __created.length, revoked: __revoked.length, "
                   "plays: __plays, pending: __pending.length}; 'reset'")
        mark = await p.js("window.__mark")
        s3 = await say_and_snapshot(p, "实验组：say() 之后（请求挂在桩上）")
        vis = await p.js("""(() => {
            try { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true}); }
            catch (e) { return 'defineProperty 失败: ' + e; }
            document.dispatchEvent(new Event('visibilitychange'));
            return 'visibilitychange 已派发：hidden=' + document.hidden;
        })()""")
        print(f"     切后台：{vis}")
        s4 = await p.js(SNAP)
        print(f"     切后台之后（请求还在飞）：{s4}")
        await p.js("__pending.shift().res({ok: true, status: 200, "
                   "blob: async () => new Blob([new Uint8Array(4000)], {type: 'audio/mpeg'})}); 'resolved'")
        await asyncio.sleep(0.6)
        s5 = await p.js(SNAP)
        print(f"     迟到的请求回来之后：{s5}")

        check("2 切后台时那条在飞的语音被作废（请求回来后没有 play）",
              s5["plays"] - mark["plays"] == 0, f"+plays={s5['plays'] - mark['plays']}")
        check("3 也没有为它建出新的 blob 地址（卡在 createObjectURL 之前就返回了）",
              s5["created"] - mark["created"] == 0, f"+created={s5['created'] - mark['created']}")
        check("4 语音条上的字被清掉了（不会停在「正在说话…」）",
              s4["vNow"] == "" and s5["vNow"] == "", f"after_vis={s4['vNow']!r} after_resolve={s5['vNow']!r}")
        check("5 切后台走的是页面自己的监听器（真实 visibilitychange 事件）",
              "visibilitychange 已派发" in str(vis) and "hidden=true" in str(vis), str(vis))
    except Exception as exc:                                # noqa: BLE001
        check("CDP 会话", False, f"{type(exc).__name__}: {exc}")
    finally:
        p.kill()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
