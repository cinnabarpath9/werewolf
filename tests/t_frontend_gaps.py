# coding=utf-8
"""补上之前"只做了静态核对"的两条前端验证：

① 第 5 项：ASR 20 秒超时 → 按钮必须回到可点、并提示"识别超时"（对线上 8077 的最终版页面）
② 第 6b 项：复盘 XSS → 存档里塞 <img onerror>，必须只当文字显示、绝不执行
   （用一个**临时实例 8086** 的临时存档目录，绝不往用户 games/ 里写东西）

跑法：先起 8086（临时 save_dir），再 python t_frontend_gaps.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_lite import Page                                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))          # tests/
#: 项目根（读 web/index.html、config.yaml、server.py 时用）
PROJ = os.path.dirname(HERE)
GAMES = os.path.join(HERE, "tmp", "games")                  # 8086 用的临时存档目录
from live_token import TOKEN
LIVE = f"http://127.0.0.1:8077/?token={TOKEN}"
TEST = f"http://127.0.0.1:8086/?token={TOKEN}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


def craft_xss_save() -> str:
    """在临时存档目录里写一份带 XSS 载荷的存档（文件名排最后，复盘会挑中它）。"""
    os.makedirs(GAMES, exist_ok=True)
    payload = '<img src=x onerror="window.__XSS__=1">'
    save = {
        "board": "six", "day": 1, "winner": "good", "aborted": False,
        "rules": {}, "seats": [], "ledger": [],
        "transcript": [{
            "day": 1, "seat": 1, "task": "speech" + payload, "elapsed": 1.0,
            "prompt_tokens": 1, "completion_tokens": 1,
            "reasoning": payload,
            "reply": {"speech": payload},
        }],
    }
    path = os.path.join(GAMES, "game-zz-xss.json")
    io.open(path, "w", encoding="utf-8").write(json.dumps(save, ensure_ascii=False))
    return path


async def part1_asr_timeout() -> None:
    print("\n=== ① ASR 20 秒超时（线上 8077）===")
    p = Page(port=9721, profile=os.path.join(HERE, "_profiles", "edge-gap1"))
    p.start(LIVE)
    try:
        await p.connect(LIVE)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(1.5)
        await p.js("""
          (() => {
          window.__asrCalls = 0;
          window.fetch = (u, init) => new Promise((res, rej) => {
            window.__asrCalls++;
            const sig = init && init.signal;
            if (sig) sig.addEventListener('abort', () => {
              const e = new Error('aborted'); e.name = 'AbortError'; rej(e);
            });
          });
          navigator.mediaDevices = navigator.mediaDevices || {};
          navigator.mediaDevices.getUserMedia = async () => ({
            getTracks: () => [{ stop() { this.stopped = true; }, stopped: false }],
          });
          window.MediaRecorder = class {
            constructor(stream, opts) { this.stream = stream; this.state = 'inactive';
              this.mimeType = (opts && opts.mimeType) || 'audio/webm'; }
            start() { this.state = 'recording'; }
            stop() { this.state = 'inactive';
              this.ondataavailable && this.ondataavailable({ data: new Blob([new Uint8Array(4000)]) });
              this.onstop && this.onstop(); }
            static isTypeSupported() { return true; }
          };
          const b = document.createElement('button');
          b.id = 'probeMic';
          document.body.appendChild(b);
          window.__probeBtn = b;
          window.__results = [];
          MIC.start(b, { idle: '🎤 说', legal: [2, 3],
                         onResult: (t, s) => window.__results.push([t, s]) });
          return 'started';
          })()
        """)
        await asyncio.sleep(0.4)
        await p.js("MIC.stop(); 'stopped'")                 # 相当于用户点「说完了」
        st1 = await p.js("({phase: micPhase, text: document.getElementById('probeMic').textContent, "
                        "disabled: document.getElementById('probeMic').disabled})")
        print(f"     停录之前：{st1}")
        await asyncio.sleep(1.0)                            # 让 onstop / 识别中 那一段跑起来
        st2 = await p.js("({phase: micPhase, text: document.getElementById('probeMic').textContent, "
                        "disabled: document.getElementById('probeMic').disabled, "
                        "calls: window.__asrCalls})")
        print(f"     发了 ASR 之后（识别中）：{st2}")
        check("①-2 识别期间按钮变「识别中…」且禁用（不许重复点）",
              st2["phase"] == "busy" and "识别中" in st2["text"] and st2["disabled"] is True
              and st2["calls"] >= 1, str(st2))

        # ⚠️ 超时值别写死：2026-10-07 把 ASR 超时从 20 秒放宽到 45 秒（长录音会被误杀），
        #    写死就会假红。这里从 index.html 里读常量，按它等。
        import re as _re
        _html = io.open(os.path.join(PROJ, "web", "index.html"),
                        encoding="utf-8").read()
        _m = _re.search(r"ASR_TIMEOUT_MS\s*=\s*(\d+)", _html)
        asr_ms = int(_m.group(1)) if _m else 20000
        wait_s = asr_ms / 1000 + 2
        print(f"     等 {wait_s:.0f} 秒看超时（页面里的上限是 {asr_ms} 毫秒）……")
        await asyncio.sleep(wait_s)
        st3 = await p.js("""({
            phase: micPhase,
            text: document.getElementById('probeMic').textContent,
            disabled: document.getElementById('probeMic').disabled,
            toasts: [...document.querySelectorAll('.toast')].map(e => e.innerText),
            results: window.__results,
        })""")
        print(f"     {wait_s:.0f} 秒后：{st3}")
        joined = " | ".join(st3["toasts"])
        check(f"①-3 ASR 上限（{asr_ms // 1000} 秒）之后超时：提示「识别超时」，按钮回到可点、不再卡在「识别中…」",
              "识别超时" in joined and st3["phase"] == "idle" and st3["disabled"] is False
              and "识别中" not in st3["text"], f"{joined[:80]} / phase={st3['phase']}")
        check("①-4 超时不会假装识别成功（没往输入框填东西）", st3["results"] == [], str(st3["results"]))
    finally:
        p.kill()


async def part2_review_xss() -> None:
    print("\n=== ② 复盘 XSS（临时实例 8086 + 临时存档）===")
    path = craft_xss_save()
    print(f"     伪造存档：{os.path.basename(path)}")
    p = Page(port=9722, profile=os.path.join(HERE, "_profiles", "edge-gap2"))
    p.start(TEST)
    try:
        await p.connect(TEST)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(1.5)
        r = await p.js("""(async () => {
            window.__XSS__ = 0;
            await openReview();
            await new Promise(r => setTimeout(r, 1200));
            const boxes = [...document.querySelectorAll('body > .toast')];
            const box = boxes[boxes.length - 1];
            const out = {
                xss: window.__XSS__,
                imgTags: document.querySelectorAll('img').length,
                imgsInBox: box ? box.querySelectorAll('img').length : -1,
                showsLiteral: box ? box.innerText.indexOf('<img src=x onerror=') >= 0 : false,
                head: box ? box.innerText.slice(0, 120) : '(没有复盘面板)',
                full: box ? box.innerText : '',
            };
            boxes.forEach(b => b.remove());
            return out;
        })()""")
        print(f"     结果：{ {k: v for k, v in r.items() if k != 'head'} }")
        print(f"     面板开头：{r['head']}")
        # ⚠️ 面板结构 2026-10-07 变过：现在是「存档：<名>」+「这一局的发言」+「调用留痕」，
        #    所以别只认"调用留痕"这一句在开头 —— 断言它确实打开了（读到存档 + 有这两个版块）。
        # 面板内容变长了（多了"这一局的发言"和存档选单），所以断**全文**而不是前 120 字
        check("②-1 复盘面板正常打开（说明临时存档被读到了）",
              "存档" in r["full"] and "调用留痕" in r["full"] and "这一局的发言" in r["full"],
              r["head"][:80])
        check("②-2 载荷没有被当成 HTML 执行（window.__XSS__ 仍是 0）", r["xss"] == 0, str(r["xss"]))
        check("②-3 面板里没有注入进来的 <img> 元素", r["imgsInBox"] == 0 and r["imgTags"] == 0,
              f"面板内 {r['imgsInBox']} 个 / 全页 {r['imgTags']} 个")
        check("②-4 载荷是以**文字**形式显示的（转义生效）", r["showsLiteral"] is True)
    finally:
        p.kill()


async def part3_https_hint() -> None:
    """http 页面里浏览器**不给麦克风 API、也不允许装 PWA** —— 前端必须把该用的地址写出来。

    用户 2026-10-10 报的"语音识别无法使用 + 浏览器还是安装不了"就是这个根因：
    以前只在点麦克风时才弹一句提示，而手机一直开着 http 页面，根本看不到。
    """
    print("\n=== 三、http 页面要给「请用 https 打开」的提示条 ===")
    html = io.open(os.path.join(PROJ, "web", "index.html"), encoding="utf-8").read()
    check("3a 前端在不安全上下文里会显示提示条（读的是 isSecureContext）",
          "isSecureContext !== false" in html and "httpsBar" in html, "")
    # ⚠️ 2026-10-10 踩过：提示条第一版在页面加载时读 S.status（那时 S 还没建好）→ 抛异常
    #    → 同一个 <script> 里后面用 const 定义的函数（openReview…）再也不会被定义 → 整页失灵。
    #    所以它必须包 try/catch，并且延时到拿到 status 之后再插。
    check("3a2 提示条不会把整页脚本带崩（try/catch + 延时插入）",
          "maybeShowHttpsBar" in html and "catch (err)" in html
          and "setTimeout(maybeShowHttpsBar" in html, "")
    check("3b 提示条里写的是**可直接点的完整地址**（https 基址 + 当前 token）",
          "public_https_base" in html and "new URLSearchParams(location.search)" in html
          and "get('token')" in html, "")
    cfg = io.open(os.path.join(PROJ, "config.yaml"), encoding="utf-8").read()
    check("3c 服务端配置里有 public_https_base（Tailscale 的 https 地址）",
          "public_https_base" in cfg, "")
    srv = io.open(os.path.join(os.path.dirname(HERE), "server.py"), encoding="utf-8").read()
    # 2026-10-31 起这个值可以来自 config，也可以来自 .env 的 WOLF_PUBLIC_HTTPS_BASE
    # （公开仓库里的 config 是空的，作者本机的真实地址在 .env）
    check("3d /api/status 会把 public_https_base 发给前端（config 或 .env）",
          '"public_https_base": str(self.game_cfg.get("public_https_base")' in srv
          and "WOLF_PUBLIC_HTTPS_BASE" in srv, "")


async def part4_support_page() -> None:
    """「赞助与反馈」二级页（2026-10-31 新增）：入口在首页左下角，页里三块内容。

    为什么要实测：这页全是**外链**，写错一个字用户就点进 404，而静态核对看不出来；
    另外它走的是 showScreen() 那套显隐，漏写一行就会"点了没反应"。
    """
    print("\n=== 四、赞助与反馈页（入口 + 三个链接）===")
    html = io.open(os.path.join(PROJ, "web", "index.html"), encoding="utf-8").read()
    check("4a 首页有入口按钮、二级页有返回键",
          'id="btnSupport"' in html and 'id="btnSupportBack"' in html and 'id="support"' in html, "")

    p = Page(port=9722, profile=os.path.join(HERE, "_profiles", "edge-gap4"))
    p.start(TEST)
    try:
        await p.connect(TEST)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(1.2)
        st = await p.js("""({
          homeHidden: document.getElementById('home').hidden,
          supportHidden: document.getElementById('support').hidden
        })""")
        check("4b 一开始二级页是藏着的", st["supportHidden"] is True and st["homeHidden"] is False, str(st))

        await p.js("document.getElementById('btnSupport').click(); 1")
        await asyncio.sleep(0.3)
        st2 = await p.js("""(() => {
          const g = id => document.getElementById(id);
          const pans = [...g('supportPans').querySelectorAll('a')].map(a => [a.textContent.trim(), a.getAttribute('href')]);
          return {
            supportHidden: g('support').hidden, homeHidden: g('home').hidden,
            afdian: g('supportAfdian').getAttribute('href'),
            qq: g('supportQq').getAttribute('href'),
            pans: pans,
            title: g('support').querySelector('h2').textContent
          };
        })()""")
        check("4c 点入口进得去、首页同时藏起来",
              st2["supportHidden"] is False and st2["homeHidden"] is True and "赞助" in st2["title"], str(st2["title"]))
        check("4d 爱发电链接指向 langrensha 主页",
              st2["afdian"] == "https://afdian.com/a/langrensha", str(st2["afdian"]))
        check("4e Q 群链接是 qm.qq.com 的加群页",
              st2["qq"].startswith("https://qm.qq.com/q/"), str(st2["qq"]))
        names = [x[0] for x in st2["pans"]]
        hrefs = [x[1] for x in st2["pans"]]
        check("4f 两个网盘都在，且夸克/百度地址正确",
              len(st2["pans"]) == 2
              and any("pan.quark.cn" in h for h in hrefs)
              and any("pan.baidu.com" in h for h in hrefs), str(names))
        check("4g 百度网盘那一条带出提取码（vv9n）",
              any("vv9n" in n for n in names), str(names))

        await p.js("document.getElementById('btnSupportBack').click(); 1")
        await asyncio.sleep(0.3)
        st3 = await p.js("({homeHidden: document.getElementById('home').hidden, "
                         "supportHidden: document.getElementById('support').hidden})")
        check("4h 返回键能回到首页", st3["homeHidden"] is False and st3["supportHidden"] is True, str(st3))
    finally:
        p.kill()

    srv = io.open(os.path.join(PROJ, "server.py"), encoding="utf-8").read()
    check("4i /api/site 会把 pans 带给前端（只放行 http(s) 地址）",
          '"pans": []' in srv and 'startswith(("http://", "https://"))' in srv, "")


async def part5_api_params_page() -> None:
    """「模型参数」二级页（2026-10-31 加）：入口在 API 行上，页里六项参数。

    为什么只做 DOM 级、不真的保存：8086 那台实例的 `settings.local.json` 落在项目根，
    真保存会写用户的真实设置文件。这里只验证"页面能打开、字段能回填、读出来的对象对"。
    """
    print("\n=== 五、模型参数二级页（入口 + 六项 + 回填/读取）===")
    html = io.open(os.path.join(PROJ, "web", "index.html"), encoding="utf-8").read()
    check("5a 列表行有「参数」按钮、页面与六个字段都在",
          'data-params=' in html and 'id="apiParams"' in html
          and all(f'id="{x}"' in html for x in
                  ("pTemp", "pMaxTokens", "pTimeout", "pRetries", "pIdle", "pThinking")), "")
    check("5b 蓄着的入口函数与保存函数都定义了",
          "function openApiParams(" in html and "async function saveApiParams(" in html
          and "function readTuningForm(" in html, "")

    p = Page(port=9724, profile=os.path.join(HERE, "_profiles", "edge-gap5"))
    p.start(TEST)
    try:
        await p.connect(TEST)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(1.2)
        r = await p.js("""(() => {
          const g = id => document.getElementById(id);
          const out = {hiddenBefore: g('apiParams').hidden};
          // 造一条假 API（只活在页面内存里，不落盘）
          SETTINGS.apis = [{id: 'ZZPARAM', name: 'ZZPARAM', base_url: 'https://x.example.com/v1',
                            model: 'm', style: '', has_key: false,
                            tuning: {temperature: 1.4, max_tokens: 9000, timeout: 120,
                                     max_retries: 1, idle_timeout: 12, thinking: 'off'}}];
          openApiParams('ZZPARAM');
          out.hiddenAfter = g('apiParams').hidden;
          out.title = g('paramsTitle').textContent;
          out.filled = [g('pTemp').value, g('pMaxTokens').value, g('pTimeout').value,
                        g('pRetries').value, g('pIdle').value, g('pThinking').value];
          // 清空两个 → 读出来的对象里就应该只剩另外那几个
          g('pTemp').value = '';
          g('pThinking').value = '';
          out.read = readTuningForm();
          return out;
        })()""")
        check("5c 一开始藏着，点入口能打开、标题带上这条 API",
              r["hiddenBefore"] is True and r["hiddenAfter"] is False and "ZZPARAM" in r["title"],
              str(r["title"]))
        check("5d 六项按已保存的值回填",
              r["filled"] == ["1.4", "9000", "120", "1", "12", "off"], str(r["filled"]))
        check("5e 留空的项不会写进提交对象（= 跟随默认）",
              "temperature" not in r["read"] and "thinking" not in r["read"]
              and r["read"].get("max_tokens") == 9000, str(r["read"]))
    finally:
        p.kill()


async def main() -> int:
    await part1_asr_timeout()
    await part2_review_xss()
    await part3_https_hint()
    await part4_support_page()
    await part5_api_params_page()
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
