# coding=utf-8
"""前端新面板复核：警长竞选 / 投警长 / 遗言 / 警徽 四个新面板。

做法：在线上页面（8077，页面每请求现读，所以磁盘上就是最新的）里直接造一条 ask、
把 send() 换成记录器、调 renderFooter()，然后看：
  · 标题对不对（labelMap 有没有漏）
  · 该有的按钮/输入框在不在
  · 点下去发出去的 payload 形状对不对（引擎要能认）
跑法： python t_page_asks.py
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


#: (ask, 期望标题, 期望按钮文字, 依次点哪些按钮, 输入框要不要填字, 期望 payload 里的关键字段)
#: ⚠️ 按**按钮文字**点，不按下标：面板里还有麦克风按钮、座位按钮 + 确认按钮，
#: 按下标点会点到"只选中不发送"的那个（第一版就是这么点歪的）。
CASES = [
    # 上警三选一（2026-10-09 改）：前置位 / 后置位 / 不上警 —— 三个按钮都要真的点得到、
    # 发出去的 payload 要分别是 "front" / "back" / false（旧版是两个按钮 + true/false）。
    ({"kind": "sheriff_run", "text": "第 1 天：警长竞选。你要不要上警？", "legal": [], "extra": {}},
     "要不要竞选警长", ["前置位上警（先发言）", "后置位上警（听完再总结拉票）", "不上警（只投票）"],
     ["前置位上警（先发言）"], None, {"run": "front"}),
    ({"kind": "sheriff_run", "text": "第 1 天：警长竞选。", "legal": [], "extra": {}},
     "要不要竞选警长", ["前置位上警（先发言）", "后置位上警（听完再总结拉票）", "不上警（只投票）"],
     ["后置位上警（听完再总结拉票）"], None, {"run": "back"}),
    ({"kind": "sheriff_run", "text": "第 1 天：警长竞选。", "legal": [], "extra": {}},
     "要不要竞选警长", ["前置位上警（先发言）", "后置位上警（听完再总结拉票）", "不上警（只投票）"],
     ["不上警（只投票）"], None, {"run": False}),
    ({"kind": "sheriff_speech", "text": "你是警长候选人，轮到你发言。", "legal": [2, 3], "extra": {}},
     "轮到你竞选发言", ["发送发言"], ["发送发言"], "我的竞选发言", {"speech": "我的竞选发言"}),
    ({"kind": "sheriff_vote", "text": "投警长。", "legal": [2, 3], "extra": {}},
     "投警长", ["2号", "3号", "弃权", "确认"], ["3号", "确认"], None, {"vote": 3}),
    ({"kind": "last_words", "text": "你刚刚出局了，轮到你发表遗言。", "legal": [], "extra": {}},
     "你的遗言", ["发送遗言", "不说了"], ["发送遗言"], "我的遗言", {"speech": "我的遗言"}),
    ({"kind": "last_words", "text": "你刚刚出局了。", "legal": [], "extra": {}},
     "你的遗言", ["发送遗言", "不说了"], ["不说了"], None, {"abstain": True}),
    ({"kind": "badge", "text": "你是警长，现在你出局了。", "legal": [2, 3], "extra": {}},
     "警徽怎么处理", ["2号", "3号", "撕掉警徽", "确认"], ["2号", "确认"], None, {"to": 2}),
    ({"kind": "badge", "text": "你是警长，现在你出局了。", "legal": [2, 3], "extra": {}},
     "警徽怎么处理", ["2号", "3号", "撕掉警徽"], ["撕掉警徽"], None, {"to": None}),
]


async def main() -> int:
    p = Page(port=9741, profile=os.path.join(HERE, "_profiles", "edge-asks"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)

        import json as _json
        for i, (ask, want_title, want_btns, click_labels, fill, want_payload) in enumerate(CASES, 1):
            r = await p.js("""
              (() => {
                window.__sent = [];
                window.__realSend = window.send;
                window.send = (m) => { window.__sent.push(m); return true; };
                S.status = Object.assign({}, S.status, {running: true, cleaning: false});
                S.ask = %s;
                renderFooter();
                const f = document.getElementById('footer');
                const title = (f.querySelector('.ask-title') || {}).textContent || '';
                const btns = [...f.querySelectorAll('button')];
                const ta = f.querySelector('textarea');
                if (%s !== null && ta) { ta.value = %s; }
                const clicked = [];
                for (const label of %s) {
                  const b = btns.find(x => x.textContent === label);
                  if (b) { b.click(); clicked.push(label); }
                  else { clicked.push('缺:' + label); }
                }
                return {title: title, btns: btns.map(b => b.textContent), clicked: clicked,
                        hasTextarea: !!ta, sent: window.__sent.slice(),
                        askCleared: (S.ask === null)};
              })()
            """ % (_json.dumps(ask, ensure_ascii=False),
                   _json.dumps(fill, ensure_ascii=False),
                   _json.dumps(fill, ensure_ascii=False),
                   _json.dumps(click_labels, ensure_ascii=False)))
            label = f"{ask['kind']}#{i}"
            check(f"{label} 标题正确（{want_title}）", r["title"] == want_title, f"实际={r['title']!r}")
            missing = [b for b in want_btns if b not in r["btns"]]
            check(f"{label} 按钮齐全 {want_btns}", not missing,
                  f"实际={r['btns']} 缺={missing}")
            if fill is not None:
                check(f"{label} 有输入框（发言/遗言要能打字或语音填）", r["hasTextarea"] is True)
            got = r["sent"][0]["payload"] if r["sent"] else {}
            ok_payload = all(got.get(k) == v for k, v in want_payload.items())
            check(f"{label} 依次点 {click_labels} 发出去的 payload 对（{want_payload}）",
                  ok_payload, f"点了={r['clicked']} payload={got}")
            check(f"{label} 发完把面板收掉（S.ask 清空）", r["askCleared"] is True)
            # 还原
            await p.js("window.send = window.__realSend; S.ask = null; renderFooter(); 'ok'")
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
