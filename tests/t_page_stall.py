# coding=utf-8
"""前端"上游失败 → 暂停弹窗"复核：弹窗内容、三个按钮发出去的指令、以及错误文本的转义。

做法：在线上页面（8077）里直接调 handle({type:'stall', ...})，把 send() 换成记录器，
再看 DOM 和发出去的 payload —— 不需要真的把上游打挂。
跑法： python t_page_stall.py
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


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


#: 故意塞一段 HTML 进错误文本：弹窗里必须**转义**显示，不能真的插进 DOM（复盘页踩过 XSS）
EVIL = '<img src=x onerror="window.__xss=1"> boom 500'


async def main() -> int:
    p = Page(port=9742, profile=os.path.join(HERE, "_profiles", "edge-stall"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)

        r = await p.js("""
          (() => {
            window.__sent = [];
            window.__realSend = window.send;
            window.send = (m) => { window.__sent.push(m); return true; };
            window.__xss = 0;
            handle({type: 'stall', seat: 5, task: 'vote', stalled: 1, timeout: 1800,
                    error: %s});
            const box = document.getElementById('stall');
            const btns = box ? [...box.querySelectorAll('button')].map(b => b.textContent) : [];
            return {exists: !!box,
                    text: box ? box.innerText : '',
                    html: box ? box.innerHTML : '',
                    btns: btns,
                    xss: window.__xss,
                    imgInjected: box ? !!box.querySelector('img') : false};
          })()
        """ % json.dumps(EVIL, ensure_ascii=False))
        check("P1 弹窗真的出现了（#stall）", r["exists"] is True)
        check("P2 标题写明'对局已暂停'", "对局已暂停" in r["text"], r["text"][:40])
        check("P3 写清是哪个座位、哪一步卡了", "5号" in r["text"] and "vote" in r["text"],
              r["text"].replace("\n", " ")[:80])
        check("P4 把上游错误原文显示出来了", "boom 500" in r["text"])
        # ⚠️ 2026-10-31：文案从写死的「自动重试 4 次」改成按服务端 `stalled` 报
        #    「连续失败 N 次」—— 重试次数会随配置/客户端重试变化，写死 4 会跟真实行为对不上。
        check("P5 说明已经自动重试过（按 stalled 报「连续失败 1 次」）",
              "连续失败 1 次" in r["text"], r["text"].replace("\n", " ")[:100])
        check("P6 告诉用户多久没人管会跳过", "跳过" in r["text"] and "分钟" in r["text"])
        check("P7 三个按钮齐全（再试一次 / 跳过 / 放弃本局）",
              r["btns"] == ["🔁 再试一次", "⏭ 跳过这一手（随机处理）", "🛑 放弃本局"], str(r["btns"]))
        check("P8 错误文本被转义了（没有真的插进 DOM、没触发 onerror）",
              r["xss"] == 0 and r["imgInjected"] is False and "&lt;img" in r["html"])

        click = await p.js("""
          (() => {
            const box = document.getElementById('stall');
            const b = [...box.querySelectorAll('button')].find(x => x.textContent.includes(%s));
            b.click();
            return {sent: window.__sent.slice(), gone: !document.getElementById('stall')};
          })()
        """ % json.dumps("再试一次", ensure_ascii=False))
        check("P9 点「再试一次」发出 stall_decision=retry",
              click["sent"] == [{"type": "stall_decision", "action": "retry"}], str(click["sent"]))
        check("P10 点完弹窗自己关掉", click["gone"] is True)

        for label, action in (("跳过这一手", "skip"), ("放弃本局", "abort")):
            r2 = await p.js("""
              (() => {
                window.__sent = [];
                handle({type: 'stall', seat: 7, task: 'wolf_kill', error: 'x', timeout: 1800});
                const box = document.getElementById('stall');
                [...box.querySelectorAll('button')].find(x => x.textContent.includes(%s)).click();
                return {sent: window.__sent.slice(), gone: !document.getElementById('stall')};
              })()
            """ % json.dumps(label, ensure_ascii=False))
            check(f"P11 点「{label}」发出 stall_decision={action}",
                  r2["sent"] == [{"type": "stall_decision", "action": action}]
                  and r2["gone"] is True, str(r2["sent"]))

        r3 = await p.js("""
          (() => {
            handle({type: 'stall', seat: 5, task: 'vote', error: 'x'});
            const before = !!document.getElementById('stall');
            handle({type: 'stall_cleared', action: 'retry'});
            return {before: before, after: !!document.getElementById('stall')};
          })()
        """)
        check("P12 收到 stall_cleared 时弹窗会收掉", r3["before"] and not r3["after"], str(r3))

        keep = await p.js("""
          (() => {
            handle({type: 'stall', seat: 5, task: 'vote', error: 'x'});
            // 别的消息（台账/状态）不该把弹窗顶掉 —— 否则用户还没点就消失了
            handle({type: 'notice', text: '普通提示'});
            handle({type: 'entry', entry: {day: 1, phase: 'day', kind: 'speech', text: 'x'}});
            return !!document.getElementById('stall');
          })()
        """)
        check("P13 其它消息不会把弹窗顶掉（要点按钮才关）", keep is True)
        await p.js("window.send = window.__realSend; handle({type:'stall_cleared'}); 'ok'")
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
