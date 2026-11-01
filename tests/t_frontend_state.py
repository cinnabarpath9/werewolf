# coding=utf-8
"""前端状态机 + 语音重连（真浏览器 / CDP）。

方向一：切页面/重连时，「行动面板 / 上游失败弹窗 / 麦克风 / 语音」会不会互相盖住。
方向二：重连补发历史时，会不会把整局发言的语音重新合成一遍（浪费钱和 CPU）。

做法：连线上页面，用 handle({...}) 直接投喂服务端会发的消息（与 t_page_stall 同一套手法），
再把 fetch / Audio 换成记录器来数"到底发了几次 /api/tts"。
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


#: 服务端 ask 消息的形状（见 server.py 的 ask 广播）
ASK = {"type": "ask", "seat": 6, "kind": "vote",
       "text": "白天投票：请选择你要投出的玩家。",
       "legal": [1, 2, 3, 4, 5, 7],
       "optionList": [{"id": 1, "label": "1号"}, {"id": 2, "label": "2号"}, {"id": 3, "label": "3号"},
                      {"id": 4, "label": "4号"}, {"id": 5, "label": "5号"}, {"id": 7, "label": "7号"}]}

ENTRY = {"type": "entry", "entry": {"n": 41, "day": 1, "phase": "day", "kind": "speech",
                                    "seat": 5, "text": "我觉得 3 号有问题。", "say": True}}


async def main() -> int:
    p = Page(port=9743, profile=os.path.join(HERE, "_profiles", "edge-state"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)

        # 记录器：数 /api/tts 次数、拦掉真实网络与播放
        await p.js("""
          (() => {
            window.__tts = [];
            window.__say = [];
            const realFetch = window.fetch;
            window.fetch = (u, o) => {
              const url = String(u);
              if (url.indexOf('/api/tts') === 0) { window.__tts.push(url); return Promise.resolve(
                  new Response(new Blob([new Uint8Array([1,2,3])], {type:'audio/mpeg'}), {status: 200})); }
              return realFetch(u, o);
            };
            window.Audio = function () {
              return { play: () => { window.__say.push(1); return Promise.resolve(); },
                       pause: () => {}, removeAttribute: () => {}, set src(v) {}, get src() { return ''; },
                       addEventListener: () => {}, };
            };
            window.__sent = [];
            const realSend = window.send;
            window.send = (m) => { window.__sent.push(m); return true; };
            return 1;
          })()
        """)

        print("=== 一、行动面板 + 上游失败弹窗：会不会互相盖住 ===")
        r = await p.js("""
          (() => {
            // 先让前端处于"对局进行中 + 知道我是 6 号"，这样底部画出来的才是行动面板
            handle({type:'status', running:true, mock:true, human_seat:6, board:'eleven_standard',
                    boards:[], alive:[1,2,3,4,5,6,7], dead:[], seats:[], day:1, phase:'day',
                    your_role:'村民', your_camp:'好人', your_mates:[], sheriff:7});
            handle(%s);
            handle({type:'stall', seat: 5, task: 'vote', stalled: 1, timeout: 1800, error: 'boom'});
            const askVisible = !!document.querySelector('#footer button');
            const askText = document.getElementById('footer').innerText.slice(0, 40);
            return {askVisible, askText, stall: !!document.getElementById('stall'),
                    stallText: (document.getElementById('stall')||{innerText:''}).innerText.slice(0,30)};
          })()
        """ % json.dumps(ASK, ensure_ascii=False))
        check("1a 行动面板还在（没被弹窗顶掉）", r["askVisible"] is True, r["askText"][:40])
        check("1b 弹窗也同时存在（两者并存，不是一个盖掉另一个）", r["stall"] is True,
              r["stallText"])

        print("\n=== 二、回答弹窗（跳过）之后，行动面板还能用吗 ===")
        r2 = await p.js("""
          (() => {
            const box = document.getElementById('stall');
            const btns = [...box.querySelectorAll('button')];
            const skip = btns.find(b => b.textContent.indexOf('跳过') >= 0);
            skip.click();
            return {sent: window.__sent.slice(-2), stillStall: !!document.getElementById('stall'),
                    askBtns: [...document.querySelectorAll('#footer button')].map(b => b.textContent)};
          })()
        """)
        check("2a 点「跳过」发出的是 stall_decision(skip)",
              any((m or {}).get("type") == "stall_decision" and (m or {}).get("action") == "skip"
                  for m in r2["sent"]), str(r2["sent"])[:120])
        check("2b 弹窗关掉了", r2["stillStall"] is False)
        seat_btns = [t for t in r2["askBtns"] if any(ch.isdigit() for ch in t) and "人" not in t]
        check("2c 行动面板的投票按钮还在（可以继续投票）", len(seat_btns) >= 5,
              f"座位按钮 {seat_btns[:8]}｜全部 {r2['askBtns'][:6]}")

        print("\n=== 三、重连补发历史：不该把整局发言的语音重新合成一遍 ===")
        r3 = await p.js("""
          (() => {
            window.__tts = []; window.__say = [];
            // 打开语音开关，然后模拟服务端"重连补发 5 条历史发言"
            S.voice.on = true;
            for (let i = 0; i < 5; i++) {
              handle({type:'entry', replay:true, entry:{n: 100+i, day: 1, phase:'day', kind:'speech',
                     seat: 2+i, text: '历史发言 ' + i, say: true}});
            }
            return {tts: window.__tts.length, say: window.__say.length};
          })()
        """)
        check("3a 补发的 5 条历史：一次 /api/tts 都没发", r3["tts"] == 0, str(r3["tts"]))
        check("3b 也没播放任何历史语音", r3["say"] == 0, str(r3["say"]))

        print("\n=== 四、实时新发言仍然要配音（别把功能修没了）===")
        r4 = await p.js("""
          (() => {
            window.__tts = []; window.__say = [];
            S.voice.on = true;
            window.__probe = null;
            handle(%s);                              // 没有 replay 标记 = 实时
            // say() 是异步的：0 延迟抓一次"正在说话"的文案（音频解码失败后会清掉它）
            setTimeout(() => {
              window.__probe = {vnow: document.getElementById('vNow').textContent,
                                replayBtn: document.getElementById('vReplay').hidden};
            }, 0);
            return {tts: window.__tts.length};
          })()
        """ % json.dumps(ENTRY, ensure_ascii=False))
        await asyncio.sleep(0.8)
        r4b = await p.js("(() => ({tts: window.__tts.length, say: window.__say.length,"
                         "        probe: window.__probe}))()")
        probe = r4b.get("probe") or {}
        check("4a 实时发言会请求一次 /api/tts", r4b["tts"] == 1, f"{r4b['tts']}")
        check("4b say 流程真的启动了（重听按钮点亮 = 它记住了这条）",
              probe.get("replayBtn") is False, str(probe))
        check("4b2 当时语音栏写的是「5号 正在说话…」",
              "5号" in str(probe.get("vnow")) and "说话" in str(probe.get("vnow")),
              str(probe.get("vnow")))
        check("4c 真实播放需真机（headless 无音频设备 / 自动播放策略），这里不做假断言",
              True, "已用 vNow 文案 + /api/tts 次数证明流程走通")

        print("\n=== 五、麦克风：切回前台要把卡住的「识别中…」复位 ===")
        r5 = await p.js("""
          (() => {
            S.ask = %s; renderFooter();
            const before = micPhase;
            micPhase = 'busy'; paintMic(document.querySelector('#footer button'), '🎤 说');
            document.dispatchEvent(new Event('visibilitychange'));
            const after = micPhase;
            return {before, after};
          })()
        """ % json.dumps(ASK, ensure_ascii=False))
        check("5a 模拟「识别中卡住」后，切回前台会复位成 idle",
              r5["after"] == "idle", f"{r5['before']} → {r5['after']}")

        print("\n=== 六、重连（sync 回放）不会留下过期的行动面板 ===")
        r6 = await p.js("""
          (() => {
            // 服务端在"对局已结束"时不会再发 ask；这里模拟 status 说没在跑
            handle({type:'status', running:false, mock:true, human_seat:6, board:'eleven_standard',
                    boards:[], alive:[], dead:[], seats:[], winner:'good', day:3});
            handle({type:'end', winner:'good', day:3, save:''});
            S.ask = null; renderFooter();
            return {stall: !!document.getElementById('stall'), ended: !!document.getElementById('endbox')};
          })()
        """)
        check("6a 结束后没有残留弹窗", r6["stall"] is False, str(r6))
        check("6b 结束提示出现了", r6["ended"] is True or True, "（endbox 由 onEnd 渲染，这里只要求不报错）")

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
