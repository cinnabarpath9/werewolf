# coding=utf-8
"""我自己补跑前端第 5~9 项里**不需要复杂打桩**的那几条（对 8077 上的最终版页面）。

覆盖：7 设置面板不重复打开 / 8 重连退避数值 / 9 收尾窗口文案 / 6a 复盘 401 提示。
（5 ASR 超时、6b XSS 需要打桩/伪造存档，这次不做，报告里标 UNVERIFIED。）

⚠️ 对线上服务只做只读 + 一次"错误配对码"探测（失败计数 1 次，离退避阈值 8 次还远）。
"""
from __future__ import annotations

import asyncio
import io
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


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


async def main() -> int:
    p = Page(port=9711, profile=os.path.join(HERE, "_profiles", "edge-checks"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)                            # 等 WS 连上、status 到手
        st = await p.js("JSON.stringify({run: S.status.running, cln: S.status.cleaning, "
                        "boards: (S.status.boards||[]).length, mock: S.status.mock})")
        print("  页面初始状态：", st)

        # ---- 8 重连退避数值 ----
        seq = await p.js("""(() => {
            const out = [];
            const oldGaveUp = gaveUp;
            for (let i = 0; i < 6; i++) { scheduleReconnect(); out.push(reconnectDelay); }
            clearTimeout(reconnectTimer);
            reconnectDelay = 0; gaveUp = oldGaveUp; renderFooter();
            return out;
        })()""")
        want = [1500, 3000, 6000, 12000, 15000, 15000]
        check("8 重连退避是 1.5s→3s→6s→12s→15s 封顶（不是每 1.5 秒硬撞）",
              seq == want, f"实测序列={seq}")

        # ---- 7 设置面板不重复打开 ----
        n = await p.js("""(async () => {
            openKeySettings(); openKeySettings(); openKeySettings();
            await new Promise(r => setTimeout(r, 900));
            const n = document.querySelectorAll('[data-panel="key"]').length;
            const saveBtns = [...document.querySelectorAll('[data-panel="key"] #kSave')].length;
            const alive = [...document.querySelectorAll('[data-panel="key"] #kSave')]
                            .filter(b => typeof b.onclick === 'function').length;
            document.querySelectorAll('[data-panel="key"]').forEach(el => el.remove());
            return {panels: n, saveBtns: saveBtns, aliveSaveBtns: alive};
        })()""")
        check("7 连开三次「填 API Key」只剩一个面板，且它的保存按钮是活的",
              n["panels"] == 1 and n["saveBtns"] == 1 and n["aliveSaveBtns"] == 1, str(n))

        # ---- 9 收尾窗口文案 ----
        txt = await p.js("""(() => {
            S.ask = {kind: 'vote', text: '轮到你投票', legal: [2, 3]};
            S.status = Object.assign({}, S.status, {cleaning: true, running: false});
            renderFooter();
            const cleaning = document.getElementById('footer').innerText;
            const askCleared = (S.ask === null);
            S.status = Object.assign({}, S.status, {cleaning: false, running: true});
            renderFooter();
            const running = document.getElementById('footer').innerText;
            S.status = Object.assign({}, S.status, {cleaning: false, running: false});
            renderFooter();
            const idle = document.getElementById('footer').innerText;
            return {cleaning, askCleared, running, idle};
        })()""")
        c = txt["cleaning"].replace("\n", " / ")
        r = txt["running"].replace("\n", " / ")
        check("9a 收尾中：底部说「本局正在收尾…」而不是「对局进行中」",
              "正在收尾" in c and "对局进行中" not in c, c[:90])
        check("9b 收尾中：旧的行动面板被清掉（不会留一张点不动的面板）", txt["askCleared"] is True)
        check("9c 收尾中：不露出开局面板（没有「开始新的一局」）",
              "开始新的一局" not in c, c[:90])
        check("9d 对局进行中：说「对局进行中，等别人发言…」", "对局进行中" in r, r[:90])
        check("9e 空闲时：露出开局面板「开始新的一局」", "开始新的一局" in txt["idle"],
              txt["idle"].replace("\n", " / ")[:90])

        # ---- 6a 复盘 401（故意用错码，只打一发） ----
        t = await p.js("""(async () => {
            const good = localStorage.getItem('wolf_token');
            localStorage.setItem('wolf_token', 'WRONG9');
            const before = document.querySelectorAll('.toast').length;
            await openReview();
            await new Promise(r => setTimeout(r, 800));
            const msgs = [...document.querySelectorAll('.toast')].slice(before).map(e => e.innerText);
            document.querySelectorAll('.toast').forEach(e => e.remove());
            localStorage.setItem('wolf_token', good);
            return {msgs};
        })()""")
        joined = " | ".join(t["msgs"])
        check("6a 复盘遇到 401：提示「看不了存档（HTTP 401）」+ 指向配对码，而不是「还没有存档」",
              ("401" in joined and "配对码" in joined and "还没有存档" not in joined), joined[:160])

        # ---- 顺带：确认页面用的还是正确的码、没被上面那发探测带偏 ----
        ok = await p.js("""(async () => {
            const r = await fetch('/api/games' + tokenQS(), {headers: apiHeaders()});
            return r.status;
        })()""")
        check("6b 探测之后正确配对码仍然是 200（只错了一次，离退避阈值 8 次很远）", ok == 200, f"HTTP {ok}")

        # ⚠️ 页面里根本没有 window.__errs（全文件 grep 零命中），所以这条以前恒过 ——
        # 现在如实报：收集器没部署，等于"无法验证"，不再伪装成 PASS。
        errs = await p.js("window.__errs || null")
        if errs is None:
            # 收集器没部署 = 无法验证。**不能**再走 check()（那会把"未验证"计成 PASS）。
            print("  SKIP  P9 控制台没有未捕获错误   | 页面没有部署错误收集器"
                  "（window.__errs），这条无法验证 —— 要真验就得在页面里加监听")
        else:
            check("控制台没有未捕获错误（页面自己的 window.onerror 计数）",
                  errs == 0, str(errs))
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
