# coding=utf-8
"""复核子代理报的反例#1：「audio.play() 被拒时 blob 地址永久泄漏」——到底是不是"永久"。

在 8077 的最终版页面上做：打桩 createObjectURL/revokeObjectURL 计数 + 让 play() 抛 NotAllowedError，
然后看下一次 say() / stop() 会不会把上一条地址收回去。
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

STUB = """
window.__created = []; window.__revoked = [];
const _c = URL.createObjectURL.bind(URL), _r = URL.revokeObjectURL.bind(URL);
URL.createObjectURL = (b) => { const u = _c(b); window.__created.push(u); return u; };
URL.revokeObjectURL = (u) => { window.__revoked.push(u); return _r(u); };
const _fetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = String(input && input.url ? input.url : input);
  if (url.indexOf('/api/tts') === 0) {
    return { ok: true, status: 200, blob: async () => new Blob([new Uint8Array(4000)], {type: 'audio/mpeg'}) };
  }
  return _fetch(input, init);
};
window.__blockPlay = true;
const _play = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () {
  if (window.__blockPlay) return Promise.reject(new DOMException('blocked', 'NotAllowedError'));
  return _play.apply(this, arguments);
};
'ok'
"""


async def main() -> int:
    p = Page(port=9712, profile=os.path.join(HERE, "_profiles", "edge-blob"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)
        await p.js(STUB)

        r1 = await p.js("""(async () => {
            await VOICE.say({seat: 4, n: 1, say: true, kind: 'speech'});
            return {created: __created.length, revoked: __revoked.length,
                    live: __created.filter(u => !__revoked.includes(u)).length,
                    vNow: document.getElementById('vNow').textContent};
        })()""")
        print("  ① 第一次 say() 被 play 拒 →", r1)

        await asyncio.sleep(3.0)          # 等"只提示一次"的 toast 走完
        r2 = await p.js("""(async () => {
            await VOICE.say({seat: 5, n: 2, say: true, kind: 'speech'});
            return {created: __created.length, revoked: __revoked.length,
                    live: __created.filter(u => !__revoked.includes(u)).length};
        })()""")
        print("  ② 紧接着第二次 say() →", r2)

        r3 = await p.js("""(() => {
            VOICE.stop();
            return {created: __created.length, revoked: __revoked.length,
                    live: __created.filter(u => !__revoked.includes(u)).length};
        })()""")
        print("  ③ 再 VOICE.stop() →", r3)

        # 对照：play 正常时
        r4 = await p.js("""(async () => {
            window.__blockPlay = false;
            window.__created = []; window.__revoked = [];
            await VOICE.say({seat: 6, n: 3, say: true, kind: 'speech'});
            return {created: __created.length, revoked: __revoked.length,
                    live: __created.filter(u => !__revoked.includes(u)).length};
        })()""")
        print("  ④ 对照：play 正常 →", r4)

        live_after_all = await p.js(
            "({created: __created.length, revoked: __revoked.length, "
            "live: __created.filter(u => !__revoked.includes(u)).length})")
        print("  ⑤ 全部结束后 →", live_after_all)

        # ⚠️ 契约已升级（2026-10-07）：play 被拒时**当场回收**地址（不再"滞留到下一次 say"）。
        #    老脚本还在等 revoked==0，于是假红。现在验的是"任何时刻都不留活地址"。
        ok = (r1["created"] >= 1 and r1["live"] == 0          # 被拒后立刻回收
              and r2["live"] == 0                             # 第二次 say 也不留
              and r3["live"] == 0                             # stop 之后更不留
              and live_after_all["live"] == 0)                # 整轮下来一个都不留
        print("\n" + ("  PASS  结论：play 被拒时那条地址**当场回收**，全程不留活地址"
                      if ok else
                      "  FAIL  结论：确实留下了收不回去的地址（与代码阅读不符，需要重看）"))
        return 0 if ok else 1
    finally:
        p.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
