# coding=utf-8
"""页面行为复核：复盘二级页面（P2）、放弃后还能看复盘（P4）、底部不再卡在「对局进行中」（P3）。

为什么要真浏览器：这三条全是"界面到底画成什么样"的问题 ——
  · P2：首屏必须**快**（轻量口径）、每行要有「思维链 N 字」、点开才拉 `/call/<i>`、
        而且**不许再截断**（用户截图里那条思考链断在「……核」就是 300 字截断）；
  · P3：服务端 `running=false` 时底部**不许**出现「对局进行中」；
  · P4：真的打一局 → 放弃 → 点底部「看上一局的复盘留痕」→ 必须能打开、并列出那局的发言。

做法：自己现挑空闲端口拉一台临时实例（`tests/test-config.yaml` → tests/tmp/games），
在真无头 Edge 里加载它、用 WS 驱动一局 mock（不花模型钱），再在页面里点按钮。
跑法： python t_page_review.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import socket
import shutil
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                      # noqa: BLE001
        pass

import httpx                                               # noqa: E402
import websockets                                          # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_lite import Page                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
#: ⚠️ 本脚本的临时数据放**独立目录**（tests/_tmp_review/），不跟任何人对 `tests/tmp` 抢。
#  为什么（2026-10-08 自测踩到）：`tests/tmp` 是**共享**的 —— run_all.py / 别的测试脚本 /
#  甚至别的 agent 都会往里写、也会**整个清掉**。实测跑着跑着 `tests/tmp/review-games/`
#  连同里面那份合成存档一起消失，于是"服务端 /api/games 回空列表 → 复盘面板打不开"，
#  看起来像服务端/前端的 bug，其实是测试自己的数据被别的进程端走了。
TMP_ROOT = os.path.join(HERE, "_tmp_review")
GAMES = os.path.join(TMP_ROOT, "games")
#: ⚠️ 2026-10-12：mock 局的存档由服务端写进 `<save_dir>/_mock/` 子目录（不写 `games/` 根目录，
#: 见 `server.py::_mock_save_dir`）—— 本测试「放弃之后那一局存没存」就要去这里找。
GAMES_MOCK = os.path.join(GAMES, "_mock")
LOG_DIR = os.path.join(HERE, "_logs")
RESULTS: list[tuple[str, bool, str]] = []
TOKEN = ""
PORT = 0
BASE = ""


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def token_of_instance(base: str, port: int) -> str:
    """从这台实例的**日志**里读它认的配对码（`[token] 本实例认的配对码 = XXXXXX`）。

    ⚠️ 为什么不能读 `配对码.txt`（自测排查了很久）：那个文件是**所有** server.py 共用的，
    每台启动时都会把自己的码写回去 —— 机器上并存好几台实例时（别的 agent 的测试、
    run_all 的 8086…），文件里可能是**别人**的码。症状极难查：
    Python 侧看着一切正常、浏览器打同一台却 401「配对码不对」。
    """
    path = os.path.join(LOG_DIR, f"page_review-{port}.out.log")
    for _ in range(40):
        try:
            text = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        m = re.search(r"\[token\] 本实例认的配对码 = ([A-Z0-9]{6})", text)
        if m:
            return m.group(1)
        time.sleep(0.25)
    return ""


def free_port() -> int:
    """挑一个**现在没人监听**的空闲端口。

    ⚠️ 这里踩过一个很贵的坑（自测排查了两小时）：`bind(0)` 只保证"这一刻没人占"，
    而机器上随时可能冒出别的实例（别的 agent 的测试、上一个没退干净的 server.py）
    抢到同一个号 —— 于是**浏览器打的不是我起的这台**，`/api/games` 回空列表、
    页面上的按钮全都"点了没反应"，看着像前端坏了，其实是测试自己连错了服务器。
    所以：先排除"现在已经在监听"的号，起完再让浏览器**自己报告**它连的是谁（见主流程第一段）。
    """
    for _ in range(30):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("挑不到空闲端口")


def read_token() -> str:
    """读 `配对码.txt`。

    ⚠️ 这个文件是**所有** server.py 共用的（每台启动时都会把自己的码写回去），
    所以"启动实例之前"读到的可能是**别人**那台的码。本测试的用法是
    `start_server()` 之后**立刻**再读一次（见主流程），那时文件里就是这一台的码 ——
    否则会出现「Python 打接口 200、浏览器打同一台 401」这种极难查的假红（自测踩过）。
    """
    try:
        for line in io.open(os.path.join(WW, "配对码.txt"), encoding="utf-8-sig"):
            m = re.search(r"[A-Z0-9]{6}", line)
            if m:
                return m.group()
    except OSError:
        pass
    return ""


def make_config() -> str:
    """把 tests/test-config.yaml 复制一份、只改 `game.save_dir` 指到本脚本自己的目录。

    这样"这一台临时实例"只看得见我合成的存档，别人写多少都与本测试无关。
    """
    import yaml
    with io.open(os.path.join(HERE, "test-config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    os.makedirs(GAMES, exist_ok=True)
    for old in os.listdir(GAMES):                  # 上一轮的残留一律清掉
        path = os.path.join(GAMES, old)
        try:
            if os.path.isdir(path):                # 2026-10-12：`_mock/` 子目录也一起清
                shutil.rmtree(path, ignore_errors=True)   # （否则上一轮的 mock 存档会留在里面）
            else:
                os.remove(path)
        except OSError:
            pass
    cfg.setdefault("game", {})["save_dir"] = GAMES
    cfg["game"]["experience"] = False              # 不写经验笔记（跑得快、也不碰别人的目录）
    path = os.path.join(TMP_ROOT, "review-config.yaml")
    with io.open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    return path


def start_server(port: int, config: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    out_path = os.path.join(LOG_DIR, f"page_review-{port}.out.log")
    log = io.open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py", "--port", str(port),
         "--config", config, "--mock-default"],
        cwd=WW, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, out_path, log
    raise RuntimeError(f"临时实例没起来（看 {out_path}）")


def craft_save(name: str, calls: int = 66, big: int = 60 * 1024) -> str:
    """合成一份「形状像真实那局」的存档：reasoning 很长、台账里有发言 + XSS 载荷。

    目录由 `make_config` 清空过一次（本脚本专用），所以这里不用再防同名残留。
    """
    os.makedirs(GAMES, exist_ok=True)
    ledger = []
    for i in range(20):
        kind = "speech" if i % 3 == 0 else ("vote" if i % 3 == 1 else "notice")
        ledger.append({
            "day": 1, "phase": "day", "kind": kind, "seat": (i % 9) + 1,
            "text": f"第 {i} 条台账：<img src=x onerror=\"window.__XSS__=1\"> 3号有问题。",
            "visible_to": ["public"], "data": {}, "audio": (i % 3 == 0),
        })
    transcript = []
    for i in range(calls):
        human = (i == 0)
        transcript.append({
            "day": 1, "phase": "day", "seat": 6 if human else (i % 11) + 1,
            "role": "villager", "task": "sheriff_vote" if human else "vote",
            "model": "" if human else "glm-5.3-flash", "fallback": "",
            "packet": "P" * 300,
            #: 尾巴上放一个记号，用来证明"显示的是全文、不是前 300 字"
            "reasoning": "R%02d " % i + ("核" * big) + " 【结尾记号%d】" % i,
            "raw": '{"vote": %d}' % ((i % 11) + 1),
            "reply": {"vote": (i % 11) + 1},
            "thinking": "{}", "prompt_tokens": 0 if human else 8980,
            "completion_tokens": 0 if human else 1069,
            "elapsed": 102.15 if human else 44.01, "error": "",
        })
    save = {"board": "eleven_standard", "day": 2, "winner": "aborted", "aborted": True,
            "started_at": "2026-10-08 09:12", "rules": {}, "sheriff_id": 8, "seats": [],
            "ledger": ledger, "transcript": transcript, "decisions": [], "audio_count": 7}
    path = os.path.join(GAMES, name)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(save, fh, ensure_ascii=False)
    return path


#: 上一局（放弃的那局）的存档落在哪儿：`_mock`（2026-10-12 起的新口径）/ `root`（老口径 ——
#: 说明分流失效了）/ 空（压根没存）。给 P4-1b 那条断言用，见 `play_and_abandon()`。
ABANDON_WHERE = ""


async def play_and_abandon() -> tuple[str, int]:
    """在临时实例上开一局 mock、跑到有台账了就放弃，返回（存档名, 台账条数）。

    ⚠️ 2026-10-12：**mock 局的存档现在落 `<save_dir>/_mock/` 子目录**（`server.py::_mock_save_dir`），
    所以这里先去 `_mock/` 找刚写出来的那一份。两处都找（找不到再退回根目录）只是为了让
    「存档了没有」这条断言不脆；**落在哪儿**由 P4-1b 单独钉住，不是静默放宽。
    """
    global ABANDON_WHERE
    before = set(os.listdir(GAMES)) if os.path.isdir(GAMES) else set()
    before_mock = set(os.listdir(GAMES_MOCK)) if os.path.isdir(GAMES_MOCK) else set()
    url = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    async with websockets.connect(url, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 6,
                                  "board": "eleven_standard", "seed": 6}))
        deadline = time.time() + 90
        entries = 0
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed:
                break
            try:
                m = json.loads(raw)
            except Exception:                              # noqa: BLE001
                continue
            if m.get("type") == "entry":
                entries += 1
                if entries >= 6:
                    break
        await ws.send(json.dumps({"type": "abandon"}))
        # 等存档落盘（end 事件带 save 路径；不依赖它，直接看目录）
        for _ in range(60):
            time.sleep(0.5)
            for where, root, seen in (("_mock", GAMES_MOCK, before_mock), ("root", GAMES, before)):
                if not os.path.isdir(root):
                    continue
                new = [f for f in (set(os.listdir(root)) - seen) if f.endswith(".json")]
                if new:
                    ABANDON_WHERE = where
                    newest = max(new, key=lambda f: os.path.getmtime(os.path.join(root, f)))
                    return newest, entries
        ABANDON_WHERE = ""
        return "", entries


async def main() -> int:
    global TOKEN, PORT, BASE
    PORT = free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    name = "game-zzpage-20261008-000000.json"
    cfg_path = make_config()
    saved = craft_save(name)
    proc, log_path, log = start_server(PORT, cfg_path)
    # ⚠️ 配对码要问**这台实例的日志**（不是 `配对码.txt`：那个文件是所有实例共用的，
    #    机器上并存好几台时里面可能是别人的码 —— 见 token_of_instance 的注释）。
    TOKEN = token_of_instance(BASE, PORT)
    if not TOKEN:
        print("读不到这一台实例的配对码（日志里没有 [token] 那行）")
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:                                  # noqa: BLE001
            proc.kill()
        return 1
    #: ⚠️ 起完实例先确认"它认的就是我这份 config"：自测踩过一次
    #  「浏览器拿到 {"games":[]}、服务端日志里却根本没这条请求」—— 现场是**端口被别人占了**
    #  （另一台实例/别的进程在同一个动态端口上应答）。这里直接用 HTTP 对一次口径，
    #  对不上就换端口重来，别让后面的断言去猜。
    try:
        import httpx as _httpx
        _h = {"X-Wolf-Client": "panel"}
        _r = _httpx.get(f"{BASE}/api/games", params={"token": TOKEN}, headers=_h, timeout=10)
        _mine = name in ((_r.json() or {}).get("games") or [])
        if not _mine:
            print(f"     [警告] {PORT} 上那台实例看不到合成存档（{_r.status_code} {_r.text[:80]}）"
                  f" —— 换端口重来一次")
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:                              # noqa: BLE001
                proc.kill()
            log.close()
            PORT = free_port()
            BASE = f"http://127.0.0.1:{PORT}"
            proc, log_path, log = start_server(PORT, cfg_path)
    except Exception as _exc:                              # noqa: BLE001
        print(f"     [警告] 自检 /api/games 失败：{_exc}")
    print(f"     [准备] 合成存档 {os.path.basename(saved)} "
          f"（{os.path.getsize(saved)/1048576:.1f}MB，目录 {os.path.relpath(GAMES, WW)}，"
          f"实例端口 {PORT}）")
    #: ⚠️ 时序探针（自测排查用）：这脚本踩过一次"跑着跑着合成存档不见了"，
    #  所以把"文件在不在 + 目录里有什么"按秒记下来；断言失败时能把现场打出来。
    WATCH: list[str] = []

    async def watch_files(stop: asyncio.Event) -> None:
        t0 = time.time()
        while not stop.is_set():
            try:
                here = os.listdir(GAMES)
            except OSError as exc:
                here = [f"(listdir 失败 {type(exc).__name__})"]
            WATCH.append(f"{time.time() - t0:5.1f}s {'在' if os.path.exists(saved) else '没了'} "
                         f"{here}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    stop_watch = asyncio.Event()
    watcher = asyncio.create_task(watch_files(stop_watch))
    p = None
    last_host = ""
    for attempt in (1, 2):
        try:
            url = f"{BASE}/?token={TOKEN}&_t={int(time.time())}"
            p = Page(port=9751, profile=os.path.join(HERE, "_profiles", "edge-review2"))
            p.start(url)
            await p.connect(url)
            await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                       "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
            await asyncio.sleep(2.0)
            # ⚠️ 第一件事：让**浏览器自己报告**它连的是哪一台（`location.host` 必须是我们的端口）。
            #    现场（自测排查了两小时）：动态端口被别的实例抢了 / cdp_lite 的回退抓到了**别人的**
            #    页面 —— 于是 `/api/games` 回空列表、页面按钮全都"点了没反应"，看着像前端坏了。
            #    现在 cdp_lite 已经收紧（指定 URL 就必须是那个 URL），这里再核一次宿主名。
            for _ in range(8):
                last_host = await p.js("location.host")
                if last_host == f"127.0.0.1:{PORT}":
                    break
                await asyncio.sleep(0.5)
            if last_host == f"127.0.0.1:{PORT}":
                break
            print(f"     [警告] 浏览器连到了 {last_host}，本测试的实例在 127.0.0.1:{PORT} —— 换端口重来")
            try:
                p.kill()
            except Exception:                              # noqa: BLE001
                pass
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:                              # noqa: BLE001
                proc.kill()
            log.close()
            PORT = free_port()
            BASE = f"http://127.0.0.1:{PORT}"
            proc, log_path, log = start_server(PORT, cfg_path)
            p = None
        except Exception as exc:                           # noqa: BLE001
            print(f"     [警告] 第 {attempt} 次起页面失败：{exc}")
    check("0 页面连到的是本测试起的实例（端口没被别人抢）",
          last_host == f"127.0.0.1:{PORT}", f"浏览器连的是 {last_host}")
    if last_host != f"127.0.0.1:{PORT}":
        raise SystemExit(1)
    try:
        # ⚠️ 再确认浏览器拿到的**就是磁盘上这份** index.html：页面是从临时实例现读的，
        #    但无头 Edge 的磁盘缓存偶尔会端出旧的那份 —— 那样后面的断言会指向"已经不存在的 bug"
        #    （自测抓过一次：detailOf 已经修好，页面里还是旧的）。
        ver = await p.js("String(openReview).indexOf(\"closest('[data-detail]')\") >= 0")
        ver2 = await p.js("(String(callLine).indexOf('data-detail') >= 0) "
                          "+ '|' + (String(callLine).indexOf('rvhuman') >= 0) "
                          "+ '|' + (String(isHumanCall).indexOf('completion_tokens') >= 0)")
        check("0 页面确实是磁盘上这一份（新复盘的三个记号都在）",
              ver is True and ver2.startswith("true|true|true"),
              f"openReview 里有 closest 兜底={ver}；callLine 记号(应为 true|true|true)={ver2}")
        # ⚠️ 顺带确认"这台临时实例看到的就是我合成的那一局"：否则后面全是假红
        probe = await p.js("""
          (async () => {
            const url = '/api/games' + tokenQS();
            const r = await fetch(url, {headers: apiHeaders()});
            const txt = await r.text();
            let j = null; try { j = JSON.parse(txt); } catch (e) {}
            // 顺带把这个页面的 Host 上报回来：确认"浏览器打的就是这一台实例"
            const st = await fetch('/api/status' + tokenQS(), {headers: apiHeaders()})
              .then(x => x.json()).catch(() => ({}));
            return {status: r.status, url: url, text: txt.slice(0, 200),
                    games: (j && j.games) || [],
                    host: location.host, board: st.board, seats: (st.boards || []).length,
                    want: window.__saveName || '(未设置)'};
          })()
        """)
        check("0b 临时实例看到我合成的那一局（存档目录隔离生效）",
              name in (probe.get("games") or []),
              f"HTTP {probe.get('status')} host={probe.get('host')} url={probe.get('url')} "
              f"body={probe.get('text')}｜文件时序={WATCH[:6]}")

        print("\n=== 一、P3：服务端 running=false 时，底部**不许**说「对局进行中」 ===")
        r0 = await p.js("""
          (() => {
            window.__XSS__ = 0;
            // 服务端明确说：没在跑（就是用户截图上那个状态）
            handle({type:'status', running:false, cleaning:false, mock:false, human_seat:6,
                    board:'eleven_standard', boards:[], seats:[], day:2, phase:'day',
                    winner:'aborted', your_role:'村民', your_camp:'好人', your_mates:[]});
            const f = document.getElementById('footer');
            return {text: f.innerText, known: S.statusKnown,
                    hasStart: [...f.querySelectorAll('button')].some(b => b.textContent.indexOf('开始新的一局') >= 0),
                    hasReview: [...f.querySelectorAll('button')].some(b => b.textContent.indexOf('复盘') >= 0)};
          })()
        """)
        check("P3-1 running=false → 底部不再出现「对局进行中」",
              "对局进行中" not in r0["text"], r0["text"].replace("\n", " ⏎ ")[:120])
        check("P3-2 running=false → 明确写出「本局已结束 / 没有在跑的对局」+ 能开新的一局",
              ("本局已结束" in r0["text"] or "没有在跑的对局" in r0["text"])
              and r0["hasStart"] is True, r0["text"].replace("\n", " ⏎ ")[:140])
        check("P3-3 status 到了之后 statusKnown 为真（不再靠默认值猜）",
              r0["known"] is True, str(r0["known"]))
        check("P3-4 底部一直有「看上一局的复盘留痕」入口", r0["hasReview"] is True)
        r0b = await p.js("""
          (() => {
            // 反面：服务端说在跑 → 仍然要显示「对局进行中」（别把功能修没了）
            handle({type:'status', running:true, cleaning:false, mock:false, human_seat:6,
                    board:'eleven_standard', boards:[], seats:[], day:2, phase:'day',
                    your_role:'村民', your_camp:'好人', your_mates:[]});
            const t1 = document.getElementById('footer').innerText;
            handle({type:'status', running:false, cleaning:false, mock:false, human_seat:6,
                    board:'eleven_standard', boards:[], seats:[], day:2, phase:'day',
                    your_role:'村民', your_camp:'好人', your_mates:[]});
            return {whileRunning: t1};
          })()
        """)
        check("P3-5 反面：服务端 running=true 时照旧显示「对局进行中」（没有把功能删掉）",
              "对局进行中" in r0b["whileRunning"], r0b["whileRunning"][:80])

        print("\n=== 二、P2：复盘首屏（轻量口径 + 列表骨架 + 点开才拉全文）===")
        await p.js("""
          (() => {
            window.__net = [];
            window.__saveName = %s;
            window.__errs = [];
            window.addEventListener('unhandledrejection',
              e => window.__errs.push('rej:' + (e.reason && e.reason.message || e.reason)));
            window.addEventListener('error', e => window.__errs.push('err:' + e.message));
            if (!window.__fetchWrapped) {
              window.__fetchWrapped = true;
              window.__realFetch = window.fetch;
              window.fetch = (u, o) => {
                const url = String(u);
                window.__net.push(url);
                const p = window.__realFetch(u, o);
                // 轻量口径那一发顺手留一份（断言"真人那一手有没有被打上人工标记"要用）
                if (url.indexOf('/api/game/') === 0 && url.indexOf('lite=1') >= 0) {
                  p.then(r => r.clone().json())
                   .then(j => { window.__lite = j; })
                   .catch(() => {});
                }
                return p;
              };
            }
            return 1;
          })()
        """ % json.dumps(name))
        r1 = await p.js("""
          (async () => {
            window.__XSS__ = 0;
            window.__net = [];
            window.__errs = [];
            document.querySelectorAll('[data-panel="review"]').forEach(e => e.remove());
            const t0 = performance.now();
            // ⚠️ 明确点名要哪一局：临时目录里可能还躺着别的测试写的存档，
            //    不点名就成了"列表是 A 局、取全文打到 B 局"（自测踩过的假红）。
            await openReview(window.__saveName);
            const dt = performance.now() - t0;
            const box = document.querySelector('[data-panel="review"]');
            if (!box) return {open: false, ms: dt, net: window.__net.slice(),
                              errs: window.__errs.slice(0, 4),
                              toasts: [...document.querySelectorAll('.toast')].map(e => e.innerText)};
            window.__box = box;
            const rows = [...box.querySelectorAll('.rvcall')];
            //: 真人那一手（第 0 条，0/0 tokens）在第 1 天最前面，首屏只画**最近 60 条**，
            //  所以正常情况下它不在 rows 里 —— 这条断言要验的是"那条数据被打上了人工标记"，
            //  所以直接拿页面里的 callLine 渲染一次第 0 条来看。
            let humanRow = '';
            let humanKeys = '';
            try {
              // ⚠️ 别依赖"第 0 条在不在首屏"：首屏只画**最近 60 条**，而那 66 条里
              //   真人那一手排在最前面 —— 所以这里直接造一条"seat=6、0/0 tokens"的行，
              //   用页面自己的 callLine 渲染（验的是渲染口径本身）。
              //   "l排序/过滤上区分开"由头顶那句「其中 N 条是你自己的操作」负责，
              //   而那条是真数据算出来的（见 P2-7b）。
              const sample = {i: 0, seat: 6, day: 1, task: 'sheriff_vote', model: '',
                              elapsed: 102.15, prompt_tokens: 0, completion_tokens: 0,
                              error: '', reply_summary: '投 7号', reasoning_len: 0};
              humanKeys = JSON.stringify(sample).slice(0, 160);
              humanRow = callLine(sample, 6, 66);
            } catch (e) { humanRow = 'ERR ' + e; }
            return {open: true, ms: dt, net: window.__net.slice(0, 4),
                    rows: rows.length,
                    first: rows.length ? rows[0].innerText : '',
                    second: rows.length > 1 ? rows[1].innerText : '',
                    humanRow: (humanRow.indexOf('人工操作') >= 0 ? '人工操作' : '（没有标记）')
                              + '|' + (humanRow.indexOf('无模型调用') >= 0 ? '无模型调用' : '（没有标记）'),
                    humanKeys: humanKeys,
                    humanInShown: rows.some(x => x.classList.contains('human')),
                    hasSpeech: box.innerText.indexOf('这一局的发言') >= 0,
                    hasCalls: box.innerText.indexOf('调用留痕') >= 0,
                    callsHeading: box.innerText.split('调用留痕')[1]
                                  ? box.innerText.split('调用留痕')[1].split(String.fromCharCode(10))[0].slice(0, 40) : '',
                    liteMarked: box.innerText.indexOf('轻量口径') >= 0,
                    xss: window.__XSS__,
                    humanSeat: (S.status && S.status.human_seat),
                    fullText: box.innerText.slice(0, 200)};
          })()
        """)
        if not r1.get("open"):
            check("P2-1 复盘面板打得开（首屏就画完了列表骨架）", False,
                  f"没打开：net={r1.get('net')} errs={r1.get('errs')} "
                  f"toasts={[t[:60] for t in (r1.get('toasts') or [])]}")
            raise SystemExit(1)
        check("P2-1 复盘面板打得开（首屏就画完了列表骨架）",
              r1.get("open") is True and r1["rows"] > 0, f"rows={r1.get('rows')}")
        check("P2-2 首屏很快（<1 秒；实测值见右）", r1.get("ms", 9999) < 1000,
              f"{r1.get('ms', -1):.0f}ms")
        check("P2-3 首屏只打了「列存档 + 轻量取那一局」两发，**没有**任何 /call/ 请求",
              len(r1["net"]) == 2 and "lite=1" in r1["net"][1]
              and not any("/call/" in u for u in r1["net"]),
              str(r1["net"]))
        check("P2-4 每个角色每次调用一行（N号 · 第X天 · 任务 · 耗时 · tokens · 思维链 N 字）",
              ("号" in r1["first"] and "第1天" in r1["first"] and "tokens" in r1["first"]
               and "思维链" in r1["first"] and "字" in r1["first"]),
              r1["first"].replace("\n", " ⏎ ")[:150])
        check("P2-5 列表上给出「思维链 N 字」的长度提示（用户要的）",
              "思维链" in r1["second"] and "字" in r1["second"],
              r1["second"].replace("\n", " ⏎ ")[:150])
        check("P2-6 首屏**没有**把长篇思维链渲染出来（一个字都没铺）",
              r1["fullText"].count("核") == 0 and "……核" not in r1["fullText"], "")
        check("P2-7 真人那一手被单独标出来（0/0 tokens 不再像坏数据）",
              r1["humanRow"] == "人工操作|无模型调用" and r1["humanInShown"] is False,
              f"真人那一手渲染成 {r1['humanRow']!r}；它落在首屏 60 行里={r1['humanInShown']}"
              f"（那一手排在最前面，首屏只画最近 60 条，所以看不到它是正常的）")
        r1b = await p.js("document.querySelectorAll('[data-panel=\"review\"] img').length")
        check("P2-8 台账里的 XSS 载荷没被执行（复盘页老坑）",
              r1["xss"] == 0 and r1b == 0, f"xss={r1['xss']} 面板内 {r1b} 个 img")

        print("\n=== 三、点开一条 → 取完整思维链（不截断） ===")
        r2 = await p.js("""
          (async () => {
            const box = window.__box;
            window.__net = [];
            const rows = [...box.querySelectorAll('.rvcall')];
            const row = rows[1];                       // 第 2 条（模型调用）
            // ⚠️ 只读探针：把"这一行的真实 DOM 形状"记下来（自测时这里出过最难查的一次错）
            const parent = row.parentElement;
            const holder0 = row.parentElement.querySelector('[data-detail]');
            const qs = parent.querySelector('[data-detail]');
            window.__dbg = {rows: rows.length, parentClass: parent.className,
                            parentAttrs: [...parent.attributes].map(a => a.name),
                            detailCount: parent.querySelectorAll('[data-detail]').length,
                            holderIsChild: holder0 === qs,
                            idx: row.getAttribute('data-call'),
                            humanSeat: (S.status && S.status.human_seat),
                            boxInDoc: document.contains(box),
                            sameBox: document.querySelector('[data-panel="review"]') === box};
            row.click();
            for (let i = 0; i < 80 && !box.querySelector('.rvdetail[data-loaded="1"]'); i++) {
              await new Promise(r => setTimeout(r, 50));
            }
            const holder = row.parentElement.querySelector('[data-detail]');
            const pre = holder && holder.querySelector('[data-chain]');
            const txt = pre ? pre.textContent : '';
            return {net: window.__net.slice(), loaded: (holder && holder.dataset.loaded) || '',
                    len: txt.length, head: txt.slice(0, 12), tail: txt.slice(-12),
                    preOverflow: pre ? getComputedStyle(pre).overflow : '',
                    preWhite: pre ? getComputedStyle(pre).whiteSpace : '',
                    buttons: holder ? [...holder.querySelectorAll('button')].map(b => b.textContent) : [],
                    detailText: holder ? holder.innerText.slice(0, 120) : '',
                    dbg: window.__dbg, errs: window.__errs.slice(0, 5)};
          })()
        """)
        check("P2-9 点开那一条才去拉 /call/<i>（正好一发，序号和这一行对得上）",
              len(r2["net"]) == 1
              and f"/call/{r2['dbg']['idx']}?" in r2["net"][0],
              f"{r2['net']}｜idx={r2['dbg'].get('idx')}｜errs={r2.get('errs')}")
        marker = f"【结尾记号{r2['dbg']['idx']}】"
        check("P2-10 显示的是**完整**思维链：一个字都没截（结尾记号在）",
              r2["len"] > 60000 and marker in r2["tail"],
              f"{r2['len']} 字；尾巴={r2['tail']!r}（找 {marker}）")
        check("P2-11 思维链用可滚动的 <pre>（保留换行 + overflow auto）",
              r2["preOverflow"] == "auto" and r2["preWhite"] == "pre-wrap",
              f"overflow={r2['preOverflow']} white-space={r2['preWhite']}")
        check("P2-12 有「复制这条思维链」按钮",
              any("复制这条思维链" in b for b in r2["buttons"]), str(r2["buttons"]))

        print("\n=== 四、「只看思维链」开关（默认开）===")
        r3 = await p.js("""
          (async () => {
            const box = window.__box;
            const sw = box.querySelector('[data-chain-only]');
            const before = sw.textContent;
            const beforeHasPacket = box.innerText.indexOf('它当时收到的上下文') >= 0;
            const holder0 = box.querySelector('.rvdetail[data-loaded="1"]');
            const len0 = holder0 && holder0.querySelector('[data-chain]')
              ? holder0.querySelector('[data-chain]').textContent.length : 0;
            const idx = holder0 && holder0.previousElementSibling
              ? holder0.previousElementSibling.getAttribute('data-call') : '';
            const marker = '【结尾记号' + idx + '】';
            sw.click();
            await new Promise(r => setTimeout(r, 100));      // 重画是同步的，等一拍再读
            const after = sw.textContent;
            const afterHasPacket = box.innerText.indexOf('它当时收到的上下文') >= 0;
            const holder = box.querySelector('.rvdetail[data-loaded="1"]');
            const chain = holder && holder.querySelector('[data-chain]');
            const stillFull = !!(chain && chain.textContent.indexOf(marker) >= 0);
            sw.click();                                 // 关回去
            await new Promise(r => setTimeout(r, 100));
            const holder2 = box.querySelector('.rvdetail[data-loaded="1"]');
            const backChain = holder2 && holder2.querySelector('[data-chain]');
            return {before, after, beforeHasPacket, afterHasPacket, stillFull, idx, marker,
                    len0, len1: chain ? chain.textContent.length : 0,
                    len2: backChain ? backChain.textContent.length : 0,
                    back: sw.textContent};
          })()
        """)
        check("P2-13 开关默认是「开」（只看思维链）",
              "开" in r3["before"] and "✓" in r3["before"], r3["before"])
        check("P2-14 关掉之后才多出 packet / raw 那些（开时确实只有思维链）",
              r3["beforeHasPacket"] is False and r3["afterHasPacket"] is True,
              f"开={r3['beforeHasPacket']} 关={r3['afterHasPacket']}")
        check("P2-15 切换开关不会把已经取到的思维链弄丢（内容还在手里）",
              r3["stillFull"] is True and r3["len1"] == r3["len0"]
              and "开" in r3["back"] and r3["len2"] == r3["len0"],
              f"切换前 {r3['len0']} 字 / 关掉后 {r3['len1']} / 切回 {r3['len2']}｜{r3['back']}")

        print("\n=== 五、P4：真的打一局 → 放弃 → 还能看那一局的复盘 ===")
        save_name, entries = await play_and_abandon()
        check("P4-1 放弃之后残局确实存档了（复盘的数据基础）",
              bool(save_name), f"{save_name}（放弃前收到 {entries} 条台账）")
        check("P4-1b ⚠️ 这一局是 mock 局，存档落在 `_mock/` 子目录（没混进存档根目录）",
              ABANDON_WHERE == "_mock",
              f"落点={ABANDON_WHERE or '（哪都没找到）'}　"
              f"（2026-10-12 起 mock 局不写 games/ 根目录，见 server.py::_mock_save_dir）")
        if save_name:
            r4 = await p.js("""
              (async () => {
                // 页面回到"连上服务端、已同步"的真实状态
                handle({type:'status', running:false, cleaning:false, mock:true, human_seat:6,
                        board:'eleven_standard', boards:[], seats:[], day:1, phase:'day',
                        winner:'aborted', your_role:'村民', your_camp:'好人', your_mates:[]});
                document.querySelectorAll('[data-panel="review"]').forEach(e => e.remove());
                const f = document.getElementById('footer');
                const btn = [...f.querySelectorAll('button')]
                  .find(b => b.textContent.indexOf('看上一局的复盘留痕') >= 0);
                const footerText = f.innerText;
                if (!btn) return {noBtn: true, footerText};
                const t0 = performance.now();
                btn.click();
                for (let i = 0; i < 100 && !document.querySelector('[data-panel="review"]'); i++) {
                  await new Promise(r => setTimeout(r, 50));
                }
                const dt = performance.now() - t0;
                const box = document.querySelector('[data-panel="review"]');
                window.__box2 = box;
                return {noBtn: false, footerText, ms: dt, open: !!box,
                        title: box ? box.innerText.slice(0, 90) : '',
                        speechHeading: box ? box.innerText.indexOf('这一局的发言') >= 0 : false,
                        speechRows: box ? box.querySelectorAll('[data-clip]').length : -1,
                        calls: box ? box.querySelectorAll('.rvcall').length : -1,
                        hasSaveName: box ? box.innerText.indexOf('存档：') >= 0 : false};
              })()
            """)
            check("P4-2 放弃后底部有「看上一局的复盘留痕」并可点（不是死的）",
                  r4.get("noBtn") is not True, str(r4.get("footerText", ""))[:80])
            check("P4-3 点了之后复盘面板真的打开了", r4.get("open") is True, r4.get("title", "")[:80])
            check("P4-4 面板里有存档名 + 这一局的发言列表",
                  r4.get("hasSaveName") is True and r4.get("speechHeading") is True,
                  f"发言条数（带 🔊 的）={r4.get('speechRows')}")
            check("P4-5 这一局的调用留痕也列出来了（mock 局有调用）",
                  (r4.get("calls") or 0) > 0, f"{r4.get('calls')} 条")
            check("P4-6 放弃后打开复盘同样很快（<1.5 秒）",
                  r4.get("ms", 99999) < 1500, f"{r4.get('ms', -1):.0f}ms")
            r5 = await p.js("""
              (async () => {
                const box = window.__box2;
                if (!box) return {open: false};
                const sel = box.querySelector('[data-game]');
                const row = box.querySelector('.rvcall');
                if (!row) return {open: true, noRow: true};
                row.click();
                for (let i = 0; i < 80 && !box.querySelector('.rvdetail[data-loaded="1"]'); i++) {
                  await new Promise(r => setTimeout(r, 50));
                }
                const holder = box.querySelector('.rvdetail[data-loaded="1"]');
                return {open: true, hasSelector: !!sel, got: !!holder,
                        len: holder ? holder.innerText.length : 0};
              })()
            """)
            check("P4-7 那一局的条目点得开（能取到全文；mock 局的思维链可能很短，不判字数）",
                  r5.get("open") is True and r5.get("got") is True, str(r5)[:120])
            await p.js("window.__box2 && window.__box2.remove(); 'ok'")

        print("\n=== 六、老服务端兜底：整份存档 + transcript 行**没有** i 字段（审查发现 P3-2）===")
        # 现场：页面是新的、服务进程还是旧的（改完前端没重启服务就是这个状态）——
        # 老服务端忽略 `?lite=1`、回整份存档，而存档的 transcript 行**没有 `i`** →
        # 每行 data-call=""。空序号那条早退**抢先于**兜底生效的话，
        # 点任何一条都只会说「这一条没有序号，取不了全文」，而标签还在承诺「点开看全文」。
        #
        # ⚠️ 这一条**做成结构断言**（自测里试过动态版：要在浏览器里伪造「老服务端」的响应，
        #    会被这一整页的 fetch 包装链、退避 401、跨端口 CORS 搅在一起，折腾很久还是假红）。
        #    它真正要钉住的只是一件**代码顺序**上的事，直接读页面源码钉死更可靠、也不看环境脸色。
        src_html = io.open(os.path.join(WW, "web", "index.html"), encoding="utf-8").read()
        #: ⚠️ 只看**可执行代码**那两处的先后，不看注释 —— 注释里也会出现同样的字眼
        #  （第一版就是这么假红的：命中的是函数上面那段说明文字，不是那一行 if）。
        i_fallback = src_html.find("if (!lite && local && (local.reasoning !== undefined)) {")
        i_empty = src_html.find("if (idx === '' || idx === null) {")
        order_ok = i_fallback > 0 and (i_empty < 0 or i_fallback < i_empty)
        check("P2-16 复盘里「老服务端兜底」排在「空序号」早退**之前**（审查发现 P3-2）",
              order_ok,
              f"兜底在第 {i_fallback} 字符、空序号早退在第 {i_empty} 字符（"
              + ("顺序对" if order_ok else "顺序反了：新页面配旧服务会点不开") + "）")
        # 顺带（非致命）：能不能在浏览器里造出「老服务端形状」的假档 ——
        # 造得出就把行数报出来，造不出（退避 401 / 跨端口）就明说没探到，不假装通过。
        r6 = await p.js("""
          (async () => {
            const real = window.__realFetch;
            const save = await real('/api/game/' + encodeURIComponent(window.__saveName)
                                    + '?lite=1&' + tokenQS().replace(/^\\?/, ''),
                                    {headers: apiHeaders()})
              .then(x => x.ok ? x.json() : null).catch(() => null);
            if (!save) return {skipped: '退避期间拿不到存档'};
            const rows = (save.transcript || []).map(r => {
              const c = Object.assign({}, r);
              delete c.i;                       // 老服务端那批行没有 i
              c.reasoning = 'R' + c.seat + ' ' + '核'.repeat(4000) + ' 【老服务端记号】';
              return c;
            });
            return {n: rows.length, droppedI: !('i' in rows[0])};
          })()
        """)
        print(f"     [探测] 能在浏览器里造出老服务端形状的假档吗：{r6}")

        print("\n=== 七、真人的两处面板：倒计时（P2-2）与私有判断（P3-1）===")
        # 这两条都是审查指出的"用户明确要、但漏做了"的东西：
        #   · 面板上全程没有"还剩多久" → 900 秒后被按弃权，人还以为在等别人
        #   · 发言/遗言/狼队讨论固定发 `suspect: []` + 一句废话 → 他的推理笔记永远是空的
        r7 = await p.js("""
          (async () => {
            window.__sent = [];
            window.__realSend2 = window.send;
            window.send = (m) => { window.__sent.push(m); return true; };
            const seats = [];
            for (let i = 1; i <= 11; i++) seats.push({id:i, alive:true, is_human:(i===6), name:''});
            handle({type:'status', running:true, cleaning:false, mock:true, human_seat:6,
                    human_timeout: 900, board:'eleven_standard',
                    boards:[{name:'eleven_standard', seats:11, tag:'', label:''}],
                    seats: seats,
                    day:1, phase:'day', your_role:'村民', your_camp:'好人', your_mates:[]});
            S.ask = {type:'ask', seat:6, ask_id: 77, kind:'speech', legal:[1,2,3,4,5,7,8,9,10,11],
                     text:'轮到你发言。', extra:{}};
            renderFooter();
            const f = document.getElementById('footer');
            const clock = f.querySelector('[data-ask-clock]');
            const clockText0 = clock ? clock.textContent : '(没有倒计时)';
            const judge = [...f.querySelectorAll('summary')]
              .map(x => x.textContent).find(x => x.indexOf('私有判断') >= 0) || '(没有私有判断入口)';
            // 选两个怀疑对象 + 写一句理由，然后点发送
            const ta = f.querySelector('textarea');
            if (ta) ta.value = '我觉得 7 号和 8 号有问题。';
            const btns = [...f.querySelectorAll('button')];
            const b7 = btns.find(b => b.textContent === '7号');
            const b8 = btns.find(b => b.textContent === '8号');
            if (b7) b7.click();
            if (b8) b8.click();
            const why = [...f.querySelectorAll('input')]
              .find(x => (x.placeholder || '').indexOf('一句话理由') >= 0);
            if (why) why.value = '7号带票太急';
            const send = btns.find(b => b.textContent === '发送发言');
            if (send) send.click();
            await new Promise(r => setTimeout(r, 60));
            const sent = (window.__sent || [])[0] || {};
            window.send = window.__realSend2;
            S.ask = null; renderFooter();
            return {clockText0, judge, sent: JSON.stringify(sent).slice(0, 220),
                    suspect: (sent.payload || {}).suspect,
                    reason: (sent.payload || {}).reason,
                    askId: sent.ask_id};
          })()
        """)
        check("P2-17 行动面板上有倒计时（用服务端 status 里的 human_timeout）",
              "还剩" in str(r7.get("clockText0")) and ":" in str(r7.get("clockText0")),
              str(r7.get("clockText0")))
        check("P3-6 发言面板有「私有判断」入口（怀疑谁 + 一句话理由）",
              "私有判断" in str(r7.get("judge")), str(r7.get("judge")))
        check("P3-7 选了怀疑对象 + 写了理由 → 真的进了 payload 的 suspect/reason",
              r7.get("suspect") == [7, 8] and r7.get("reason") == "7号带票太急"
              and r7.get("askId") == 77,
              f"suspect={r7.get('suspect')} reason={r7.get('reason')!r} ask_id={r7.get('askId')}｜"
              f"整条={r7.get('sent')}")
    finally:
        stop_watch.set()
        try:
            await asyncio.wait_for(watcher, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            watcher.cancel()
        print("     [文件时序] " + " ｜ ".join(WATCH))
        if p is not None:
            try:
                p.kill()
            except Exception:                              # noqa: BLE001
                pass
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:                                  # noqa: BLE001
            try:
                proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        try:
            log.close()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            os.remove(os.path.join(GAMES, name))            # 只删自己合成的那一份
        except OSError:
            pass
        # ⚠️ 也别给自己留垃圾：合成档一律带走（否则下次跑"取最后一局"就指到它头上了）
        try:
            for leftover in os.listdir(GAMES):
                if leftover.startswith("game-zzpage-"):
                    os.remove(os.path.join(GAMES, leftover))
        except OSError:
            pass

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
