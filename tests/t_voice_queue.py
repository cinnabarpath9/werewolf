# coding=utf-8
"""回归：语音队列（按顺序补播）+ 每次发言前的决策。

语音队列是纯前端逻辑，用无头浏览器直接验行为：
  · 连推 3 条发言 → 播放顺序必须是 1→2→3（不是"只播最后一条"）；
  · 队列超上限时丢最旧的（并给出提示），不会无限积压。
"""
import asyncio
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")

from cdp_lite import Page                                   # noqa: E402

from live_token import TOKEN
URL = f"http://127.0.0.1:8077/?token={TOKEN}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


async def main() -> int:
    html = io.open(os.path.join(os.path.dirname(HERE), "web", "index.html"),
                   encoding="utf-8").read()
    check("1a 前端有语音队列（按顺序补播，不再是「掐掉上一条」）",
          "VOICE_QUEUE_MAX" in html and "voiceQueuePush" in html and "voiceQueuePump" in html)
    check("1b 队列有上限，超了丢最旧的",
          "VOICE_Q.list.shift()" in html and "跳过了" in html)
    check("1c 发言入队（不再直接 VOICE.say）",
          "if (e.say) voiceQueuePush(e);" in html and "if (e.say) VOICE.say(e);" not in html)
    check("1d 关语音会清空队列", "voiceQueueClear" in html)

    p = Page(port=9747, profile=os.path.join(HERE, "_profiles", "edge-vq"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(1.5)
        # 把真实播放换成"记账"：只验顺序与去重，不真的放声音
        r = await p.js("""
          (async () => {
            const played = [];
            S.voice.on = true;                               // ⚠️ 语音没开时队列泵会立刻退出
            VOICE.speak = async (e) => { played.push(e.n); await new Promise(r => setTimeout(r, 60)); };
            VOICE_Q.list.length = 0; VOICE_Q.busy = false;
            voiceQueuePush({n: 1, seat: 1, say: true});
            voiceQueuePush({n: 2, seat: 2, say: true});
            voiceQueuePush({n: 3, seat: 3, say: true});
            voiceQueuePush({n: 3, seat: 3, say: true});      // 重复的应被忽略
            await new Promise(r => setTimeout(r, 500));
            const order = played.slice();
            // 上限：塞 20 条，只应留下 VOICE_QUEUE_MAX 条（且在播的过程中会被消费掉）
            VOICE_Q.list.length = 0;
            for (let i = 100; i < 120; i++) voiceQueuePush({n: i, seat: 1, say: true});
            const left = VOICE_Q.list.length;
            await new Promise(r => setTimeout(r, 200));
            VOICE_Q.list.length = 0;
            return {order, left, max: VOICE_QUEUE_MAX};
          })()
        """)
        check("2a 三条发言按 1→2→3 顺序播完（不是只播最后一条）",
              r["order"][:3] == [1, 2, 3], str(r["order"]))
        check("2b 重复的条目不会重复排", r["order"].count(3) == 1, str(r["order"]))
        check("2c 队列有上限（塞 20 条不会全留着）", r["left"] <= r["max"], f"剩 {r['left']}/{r['max']}")
    finally:
        try:
            p.kill()
        except Exception:                                # noqa: BLE001
            pass

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
