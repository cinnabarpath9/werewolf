# coding=utf-8
"""前端"过期回调"复核（2026-10-07 的 P2 修复）：

  ① ask_done 的编号守卫：断线时在**旧**面板上点了确认（那一手排队），重连后服务端先补发
     **新**的 pending_ask（新面板出现），紧接着旧的那一手被收下、广播回一条**旧编号**的
     ask_done —— 以前不问编号就清面板，玩家看着面板消失、以为投出去了。现在只清编号相同的那条；
     ask_id 不存在时保持老行为（照旧清面板），服务端还没上线也不会坏。
  ② CLIP 的代际守卫：点 🔊 之后、fetch 回来之前关掉面板（或换存档）→ 那次请求回来
     不许再 audio.src / play()（面板已经关了，声音不该响），但按钮也不能卡在 ⏳。
  ③ 复盘关面板 / 换存档时都会作废在飞的那一次（stop → gen++）。
  ④ 对局里那条 🔊 先打 `cached=1`（只读缓存）：200 直接播、404 才问一次要不要现合成。

做法：在线上页面（8077）里直接喂服务端会发的消息（handle）、把 fetch 换成桩、
给 `Audio.prototype.play` 挂记录器（⚠️ **不能**换 `window.Audio`：CLIP 里那个 audio 是
页面加载时就 new 出来的，换了 window.Audio 它根本看不见 —— 自测抓到的）。
**只读页面行为，不往服务端写东西。**
跑法： python t_page_stale.py
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

#: 装"音频记录器"：给 Audio.prototype.play / pause 挂 spy（页面里那个 CLIP 也走原型）
SPY_JS = """
  (() => {
    window.__play = []; window.__pause = 0;
    if (!window.__audioSpied) {
      window.__audioSpied = true;
      window.__realPlay = Audio.prototype.play;
      window.__realPause = Audio.prototype.pause;
      Audio.prototype.play = function () {
        window.__play.push(String(this.src || ''));
        const pr = Promise.resolve();          // 别真的去解码 blob（headless 里会 reject）
        return pr;
      };
      Audio.prototype.pause = function () { window.__pause++; try { return window.__realPause.call(this); } catch (e) {} };
    }
    return 1;
  })()
"""


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


#: 服务端 ask 的形状（ask_id 是这轮新加的编号；老服务端没有这个字段）
def ask_msg(ask_id, kind="vote"):
    m = {"type": "ask", "seat": 6, "kind": kind,
         "text": "白天投票：请选择你要投出的玩家。", "legal": [1, 2, 3, 4, 5, 7], "extra": {}}
    if ask_id is not None:
        m["ask_id"] = ask_id
    return m


async def main() -> int:
    p = Page(port=9748, profile=os.path.join(HERE, "_profiles", "edge-stale"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)
        await p.js(SPY_JS)

        print("=== 一、ask_done 的编号守卫（断线补发不会误清新面板）===")
        r1 = await p.js("""
          (() => {
            window.__sent = [];
            const realSend = window.send;
            window.send = (m) => { window.__sent.push(m); return true; };
            S.status = Object.assign({}, S.status, {running: true, cleaning: false});
            const out = {};
            // 断线前那一条 ask（编号 7）→ 玩家点了确认 → 本地先把面板收掉
            handle(%s);
            const f = document.getElementById('footer');
            const seat4 = [...f.querySelectorAll('button')].find(b => b.textContent === '4号');
            const confirm7 = [...f.querySelectorAll('button')].find(b => b.textContent === '确认');
            out.sawButtons = !!seat4 && !!confirm7;
            seat4.click(); confirm7.click();
            out.sent = window.__sent.slice();
            out.clearedAfterAnswer = (S.ask === null);
            // 重连：服务端先补发**新**的 pending_ask（编号 8），紧接着旧的 ask_done（编号 7）到了
            handle(%s);
            out.newPanelVisible = !!document.querySelector('#footer .ask-title');
            handle({type: 'ask_done', seat: 6, ask_id: 7});
            out.staleDoneKeptPanel = (S.ask !== null)
              && !!document.querySelector('#footer .ask-title');
            out.askIdAfterStale = S.ask && S.ask.ask_id;
            // 服务端把**这一条**（编号 8）收下了 → 面板该收掉
            handle({type: 'ask_done', seat: 6, ask_id: 8});
            out.freshDoneClearedPanel = (S.ask === null)
              && !document.querySelector('#footer .ask-title');
            // 兼容老服务端：没有 ask_id 的 ask_done 照旧清面板
            handle(%s);
            handle({type: 'ask_done', seat: 6});
            out.legacyDoneClearedPanel = (S.ask === null);
            S.ask = null; renderFooter();
            window.send = realSend;
            return out;
          })()
        """ % (json.dumps(ask_msg(7), ensure_ascii=False),
               json.dumps(ask_msg(8), ensure_ascii=False),
               json.dumps(ask_msg(9), ensure_ascii=False)))
        sent7 = (r1["sent"] or [{}])[0]
        check("1a 投票面板画出来了（1号…7号 + 确认）", r1["sawButtons"] is True)
        check("1b 回答那一手把 ask_id 原样回传（服务端才认得出是哪一条）",
              sent7.get("ask_id") == 7 and sent7.get("type") == "action"
              and (sent7.get("payload") or {}).get("vote") == 4, str(sent7)[:160])
        check("1c 点了确认之后本地先把面板收掉（不等服务端）", r1["clearedAfterAnswer"] is True)
        check("1d 重连补发的新 ask 把面板补回来了（编号 8）",
              r1["newPanelVisible"] is True and r1["askIdAfterStale"] == 8, str(r1["askIdAfterStale"]))
        check("1e ⚠️ 旧编号（7）的 ask_done **不许**清新面板（编号 8）",
              r1["staleDoneKeptPanel"] is True, f"S.ask={r1['askIdAfterStale']}")
        check("1f 编号相同（8）的 ask_done 正常清面板", r1["freshDoneClearedPanel"] is True)
        check("1g 兼容老服务端：不带 ask_id 的 ask_done 照旧清面板",
              r1["legacyDoneClearedPanel"] is True)

        print("\n=== 二、CLIP 代际守卫（在飞的请求回来时，面板已经关了）===")
        # 把 /api/tts 与 /api/saved_audio 换成"挂住"的桩：由测试决定什么时候让它回来
        await p.js("""
          (() => {
            window.__tts = [];
            window.__resolveTts = null;
            const realFetch = window.fetch;
            window.fetch = (u, o) => {
              const url = String(u);
              if (url.indexOf('/api/tts') !== 0 && url.indexOf('/api/saved_audio') !== 0) {
                return realFetch(u, o);
              }
              window.__tts.push(url);
              return new Promise((res) => { window.__resolveTts = () => res(
                  new Response(new Blob([new Uint8Array([1,2,3])], {type:'audio/mpeg'}), {status: 200})); });
            };
            return 1;
          })()
        """)
        r2 = await p.js("""
          (async () => {
            %s
            const p = document.createElement('div');
            p.className = 'toast'; p.dataset.panel = 'clipbox';
            const b = document.createElement('button'); b.textContent = '🔊';
            p.appendChild(b); document.body.appendChild(p);
            CLIP.play('/api/tts?n=41', b);       // 请求发出，但还没回来
            CLIP.stop();                         // 关面板 → 作废这一次
            p.remove();
            const urls = window.__tts.slice();
            if (window.__resolveTts) window.__resolveTts();   // 请求"回来了"
            await new Promise(r => setTimeout(r, 100));
            return {urls, played: window.__play.slice(), btn: b.textContent, disabled: b.disabled};
          })()
        """ % SPY_JS)
        check("2a 点 🔊 确实发了 /api/tts（不是被守卫吃掉了）",
              any(u.startswith('/api/tts') for u in r2["urls"]), str(r2["urls"]))
        check("2b ⚠️ 请求回来时面板已经关了 → 一次都没播（audio.play 没被调用）",
              r2["played"] == [], str(r2["played"]))
        check("2c 作废之后按钮不能卡在 ⏳/禁用（否则用户回来再点没反应）",
              r2["btn"] == '🔊' and r2["disabled"] is False, f"{r2['btn']}/{r2['disabled']}")

        r3 = await p.js("""
          (async () => {
            %s
            window.__tts = [];
            const p = document.createElement('div');
            p.className = 'toast'; p.dataset.panel = 'clipbox2';
            const b1 = document.createElement('button'); b1.textContent = '🔊';
            const b2 = document.createElement('button'); b2.textContent = '🔊';
            p.append(b1, b2); document.body.appendChild(p);
            const pA = CLIP.play('/api/saved_audio?game=A&n=1', b1);
            const rA = window.__resolveTts; window.__resolveTts = null;
            const pB = CLIP.play('/api/saved_audio?game=B&n=2', b2);      // 换存档
            const rB = window.__resolveTts; window.__resolveTts = null;
            const trace = ['A=' + (typeof rA) + ' B=' + (typeof rB)];
            const pAw = pA.then(() => { trace.push('pA-done@' + b1.textContent); });
            const pBw = pB.then(() => { trace.push('pB-done@' + b2.textContent); });
            rA();                        // 旧存档那一次先回来（应当被丢弃）
            trace.push('afterRA=' + b1.textContent);
            rB();                        // 新存档那一次后回来（应当播）
            trace.push('afterRB=' + b2.textContent);
            // 等到两个按钮都被还原（或超时），别用"猜一个 150 毫秒"
            const waitBtn = async (el) => {
              for (let i = 0; i < 60 && el.textContent !== '🔊'; i++) {
                await new Promise(r => setTimeout(r, 20));
              }
              return el.textContent;
            };
            const s1 = await waitBtn(b1), s2 = await waitBtn(b2);
            await Promise.all([pAw, pBw]);
            trace.push('afterAll=' + b1.textContent + '/' + b2.textContent);
            trace.push('again=' + b1.textContent + '/' + b1.disabled);
            CLIP.stop();                     // 再摸一次：看 b1 会不会被还原
            await new Promise(r => setTimeout(r, 60));
            trace.push('afterStop=' + b1.textContent);
            p.remove();
            return {urls: window.__tts.slice(), played: window.__play.slice(), b1: s1, b2: s2,
                    trace};
          })()
        """ % SPY_JS)
        check("2d 换存档：旧的那次回来了也不播，只有新的那次播（正好播一次）",
              len(r3["played"]) == 1 and len(r3["urls"]) == 2,
              f"played={len(r3['played'])} urls={len(r3['urls'])}")
        check("2e 旧按钮被还回「🔊」、新按钮也是「🔊」（两个都不能卡住）",
              r3["b1"] == '🔊' and r3["b2"] == '🔊', f"{r3['b1']}/{r3['b2']} trace={r3['trace']}")

        print("\n=== 三、复盘面板：关掉 / 换存档要作废在飞的那一次 ===")
        r4 = await p.js("""
          (() => {
            const src = openReview.toString();
            return {stopOnClose: /data-close[^\\n]*CLIP\\.stop/.test(src),
                    stopOnSwitch: /data-game[\\s\\S]{0,160}CLIP\\.stop/.test(src)};
          })()
        """)
        check("3a 点「关闭」会 CLIP.stop()（作废在飞的那一次）",
              r4["stopOnClose"] is True, str(r4))
        check("3b 切换存档会 CLIP.stop()（不让上一局的语音窜到这一局）",
              r4["stopOnSwitch"] is True, str(r4))

        print("\n=== 四、对局里那条 🔊：先只读缓存，没缓存才问（不偷偷合成）===")
        # n=41 是"当时没合成过"（cached=1 → 404）；n=42 是"缓存里有"（cached=1 → 200）。
        # 不带 cached 一律 200（= 允许现合成）。
        await p.js("""
          (() => {
            window.__tts = [];
            window.fetch = (u, o) => {
              const url = String(u);
              if (url.indexOf('/api/tts') !== 0) return Promise.resolve(new Response('{}', {status: 200}));
              window.__tts.push(url);
              if (url.indexOf('cached=1') >= 0 && url.indexOf('n=41') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({error: '这条当时没合成过语音'}),
                                                   {status: 404}));
              }
              return Promise.resolve(new Response(new Blob([new Uint8Array([1,2,3])], {type:'audio/mpeg'}),
                                                  {status: 200}));
            };
            return 1;
          })()
        """)
        r5 = await p.js("""
          (async () => {
            %s
            const p = document.createElement('div');
            p.className = 'toast'; p.dataset.panel = 'clipbox3';
            const b = document.createElement('button'); b.textContent = '🔊';
            p.appendChild(b); document.body.appendChild(p);
            onLiveClip(41, b);                       // 没有缓存的那条
            await new Promise(r => setTimeout(r, 150));
            const dlg = document.querySelector('[data-synth-ask]');
            const out = {urls: window.__tts.slice(), played: window.__play.length,
                         dialog: !!dlg,
                         dialogText: dlg ? dlg.innerText : '',
                         buttons: dlg ? [...dlg.querySelectorAll('button')].map(x => x.textContent) : [],
                         btn: b.textContent};
            if (!dlg) { p.remove(); return out; }
            [...dlg.querySelectorAll('button')].find(x => x.textContent === '不用了').click();
            await new Promise(r => setTimeout(r, 80));
            out.afterCancel = {dialog: !!document.querySelector('[data-synth-ask]'),
                               calls: window.__tts.length, played: window.__play.length};
            out.btnAfterCancel = b.textContent;
            onLiveClip(41, b);                       // 再点一次，这次点「合成一次再说」
            await new Promise(r => setTimeout(r, 150));
            const dlg2 = document.querySelector('[data-synth-ask]');
            out.dialogAgain = !!dlg2;
            if (dlg2) {
              [...dlg2.querySelectorAll('button')].find(x => x.textContent.indexOf('合成一次') >= 0).click();
            }
            await new Promise(r => setTimeout(r, 250));
            out.urls2 = window.__tts.slice();
            out.played2 = window.__play.length;
            out.btnFinal = b.textContent;
            p.remove();
            return out;
          })()
        """ % SPY_JS)
        check("4a 点 🔊 先打的是**只读缓存**那一发（cached=1），而且只打了这一发",
              len(r5["urls"]) == 1 and 'cached=1' in r5["urls"][0] and 'n=41' in r5["urls"][0],
              str(r5["urls"])[:160])
        check("4b 404（这条当时没合成过）→ 弹一次确认，弹之前**没有**播任何东西",
              r5["dialog"] is True and r5["played"] == 0, f"dialog={r5['dialog']} played={r5['played']}")
        check("4c 确认框说的是「当时没合成过」+ 两个按钮（不用了 / 合成一次再说）",
              "没合成过" in r5["dialogText"] and r5["buttons"] == ['不用了', '合成一次再说'],
              f"{r5['buttons']} / {r5['dialogText'][:50]}")
        check("4d 点「不用了」→ 关掉确认框，而且**不再发任何请求**（一个字都没合成）",
              r5["afterCancel"]["dialog"] is False
              and r5["afterCancel"]["calls"] == 1
              and r5["afterCancel"]["played"] == 0, str(r5["afterCancel"]))
        check("4e 取消之后那个 🔊 还能再点（不是卡在 ⏳）", r5["btnAfterCancel"] == '🔊',
              r5["btnAfterCancel"])
        check("4f 点「合成一次再说」→ 才发那一发**允许现合成**的请求（不带 cached）并播出来",
              r5["dialogAgain"] is True
              and any('cached=1' not in u and 'n=41' in u for u in r5["urls2"])
              and r5["played2"] == 1 and r5["btnFinal"] == '🔊',
              f"again={r5['dialogAgain']} urls={r5['urls2']} played={r5['played2']} btn={r5['btnFinal']}")

        r6 = await p.js("""
          (async () => {
            %s
            window.__tts = [];                       // 只数这一段的请求
            const p = document.createElement('div');
            p.className = 'toast'; p.dataset.panel = 'clipbox4';
            const b = document.createElement('button'); b.textContent = '🔊';
            p.appendChild(b); document.body.appendChild(p);
            onLiveClip(42, b);                        // 42 在缓存里（桩对 cached=1 回 200）
            await new Promise(r => setTimeout(r, 200));
            const dlg = document.querySelector('[data-synth-ask]');
            const out = {urls: window.__tts.slice(), played: window.__play.length,
                         dialog: !!dlg, btn: b.textContent};
            p.remove();
            return out;
          })()
        """ % SPY_JS)
        check("4g 命中缓存（cached=1 回 200）→ 直接播、**零弹窗**（语音开着打的那一局不会白问）",
              r6["dialog"] is False and r6["played"] == 1 and len(r6["urls"]) == 1,
              f"dialog={r6['dialog']} played={r6['played']} urls={len(r6['urls'])}")

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
        except Exception:                                   # noqa: BLE001
            pass


raise SystemExit(asyncio.run(main()))
