# coding=utf-8
"""回归：exam_live.py 的**命令行真能跑到打局**（不是只跑 --selftest）。

为什么要这条：2026-10-06 那次改动漏注册了 `--max-days`，而 `main()` 里还在用
`args.max_days` → 每次非 selftest 调用都 AttributeError 崩掉（还先花掉一发付费预检）。
旧的 E9 只跑 `--selftest`，在 :320 就 return，永远走不到那行 —— 所以全绿。

═══════════════════════════════════════════════════════════════════════════════
⚠️ 2026-10-08 修的第二个洞 —— **这个测试自己**「自称离线、其实在花钱」
═══════════════════════════════════════════════════════════════════════════════
老版本只桩了 `Model.ask`，可它只管得住**连通性预检**。真打局的每一次决策走的是
另一条路：`play_one` → `routing.build_client(spec_for(model))` → `wolf.llm.LLMClient.chat_json`
→ httpx 真连 `ark.cn-beijing.volces.com:443`。

实测定位（把 DNS/connect 换成「一调用就抛」的探针跑老版本）：
  · 预检**确实**走了桩（`Model.ask` 桩被调到 1 次、`system=你是连通性探针。`、出口零违规）；
  · 但第一手决策立刻 `getaddrinfo(b'ark.cn-beijing.volces.com', 443)` —— 出口违规 2 次。
所以 2026-10-08 那次「日志停在『连通性预检 ✅ 连通正常（0.01s）』、进程 8 分钟不涨
CPU、只有一条到方舟的 ESTABLISHED 连接」——那条连接**不是预检发的**（预检 0.01s 是
桩的返回值），是**第一手打局的模型调用**发的，而且真在花钱。

现在这条测试有三道闸，缺一条就会重新变成「假绿」或「挂死」：
  ① **出口桩**：`socket.getaddrinfo` / `socket.create_connection` / `socket.connect` /
     `httpx.AsyncClient.send` 一律「记录 + 抛异常」；跑完断言违规数 = 0。
     任何路径漏桩都会当场失败，而不是悄悄联网。
  ② **两条路一起打桩**：预检（`Model.ask`）+ 打局（`routing.build_client` → 离线假客户端），
     并断言**两条桩都真的被调用过**（否则「预检失败就 continue」的空跑也会变绿）。
  ③ **硬性内部超时**（`HARD_LIMIT_S`）：整局跑不完就判失败并退出，打印「挂在哪一步 +
     最后一条日志 + 建议查什么」，不靠人杀进程（2026-10-08 现场就是靠人杀的，白等 8 分钟）。

⚠️ 注意「打桩」打的是**网络出口**，不是被测入口：`main()`（非 selftest 那条真入口）、
argparse、`load_models`、`preflight`、`play_one`、`Game.run()` 整局、`print_metrics`、
报告落盘全都真跑 —— 当年漏注册 `--max-days` 就是被这条入口抓到的，不能为了快而跳过它。
"""
from __future__ import annotations

import asyncio
import io
import os
import socket
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

#: 整局（= 整个 `main()`）的硬上限。正常离线一局是**秒级**；180 秒是「机器再忙也够」的天花板，
#: 到点就判失败退出 —— 绝不允许再出现「8 分钟不动、只能靠人杀」。
HARD_LIMIT_S = 180.0
#: 内层（协作取消）比外层早这么多秒收手：这样多数情况能拿到「挂在 exam_live 的哪一行」；
#: 真遇到同步阻塞（事件循环都转不动）时，外层看门狗兜底，拿线程栈。
INNER_MARGIN_S = 15.0

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


# ══════════════════════════════════════════════════════════════════════════════
#  ① 出口桩：任何真实联网 = 当场记录 + 抛异常（不花钱、也不靠「我记得桩了」）
# ══════════════════════════════════════════════════════════════════════════════

class OfflineTrip(BaseException):
    """故意继承 BaseException。

    被测代码里到处是 `except Exception`（`LLMClient.chat_json`、引擎的 `one_call`），
    继承 Exception 会被它们吞掉 → 变成「一次调用失败、重试」→ 看着像上游波动，其实是我们
    自己的桩在报警。继承 BaseException 才能直接冒到测试顶上，让人一眼看到是哪条路漏了。
    """


def _is_loopback(host) -> bool:                              # noqa: ANN001
    """是不是本地回环地址。

    ⚠️ 必须放行回环：Windows 上 `asyncio.new_event_loop()` 造的事件循环会给自己开一对
    self-pipe（`socket.socketpair()` → 退回 127.0.0.1 的 TCP connect）。那是**进程内**
    的唤醒管道，不是联网、不花钱 —— 拦掉它只会让测试自己起不来（第一版就踩了这个坑）。
    真正的「出口」全都不是回环：方舟/任何上游都是域名或公网 IP。
    """
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    h = host.strip().lower()
    return (h in ("localhost", "localhost.localdomain", "::1", "0:0:0:0:0:0:0:1")
            or h.startswith("127."))


def _addr_host(address) -> object:                           # noqa: ANN001
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


class OfflineGuard:
    """把「真实网络出口」换成闸门：调一下就记录调用栈并抛 `OfflineTrip`。

    装哪几处、为什么：
      · `socket.getaddrinfo` —— 一切按域名连接的必经之路（httpx/anyio 也走它）；
      · `socket.create_connection` / `socket.socket.connect` —— 直连 IP、或别的库自己开 socket；
      · `httpx.AsyncClient.send` / `httpx.Client.send` —— 万一 DNS 已经在缓存里/直连 IP，
        这一层还能兜住（`send` 是所有 httpx 请求的唯一出口，且**不放行**回环：
        这条测试就不该有 HTTP 请求）。
    回环地址一律放行（见 `_is_loopback` 的说明），其余一律判违规。
    """

    def __init__(self) -> None:
        self.violations: list[str] = []
        self._saved: list[tuple[object, str, object]] = []

    # -- 闸门本体 ---------------------------------------------------------
    def _trip(self, what: str, detail: str) -> None:
        stack = "".join(traceback.format_stack()[-14:])
        self.violations.append(f"{what}：{detail}\n{stack}")
        print(f"\n  ‼️ 出口桩被绕过（真实联网）：{what} {detail}", flush=True)
        print(stack, flush=True)
        raise OfflineTrip(f"{what} {detail}")

    def _patch(self, obj: object, name: str, new: object) -> None:
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, new)

    def __enter__(self) -> OfflineGuard:
        import httpx

        orig_getaddrinfo = socket.getaddrinfo
        orig_create = socket.create_connection
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex

        def _getaddrinfo(host, *a, **k):                    # noqa: ANN001, ANN202
            if not _is_loopback(host):
                self._trip("socket.getaddrinfo", repr(host))
            return orig_getaddrinfo(host, *a, **k)

        def _create_connection(address, *a, **k):           # noqa: ANN001, ANN202
            if not _is_loopback(_addr_host(address)):
                self._trip("socket.create_connection", repr(address))
            return orig_create(address, *a, **k)

        def _connect(self_, address, *a, **k):              # noqa: ANN001, ANN202
            if not _is_loopback(_addr_host(address)):
                self._trip("socket.socket.connect", repr(address))
            return orig_connect(self_, address, *a, **k)

        def _connect_ex(self_, address, *a, **k):           # noqa: ANN001, ANN202
            if not _is_loopback(_addr_host(address)):
                self._trip("socket.socket.connect_ex", repr(address))
            return orig_connect_ex(self_, address, *a, **k)

        async def _no_send(self_, request, **kwargs):        # noqa: ANN001, ANN202
            self._trip("httpx.AsyncClient.send", f"{request.method} {request.url}")

        def _no_send_sync(self_, request, **kwargs):         # noqa: ANN001, ANN202
            self._trip("httpx.Client.send", f"{request.method} {request.url}")

        self._patch(socket, "getaddrinfo", _getaddrinfo)
        self._patch(socket, "create_connection", _create_connection)
        self._patch(socket.socket, "connect", _connect)
        self._patch(socket.socket, "connect_ex", _connect_ex)
        self._patch(httpx.AsyncClient, "send", _no_send)
        self._patch(httpx.Client, "send", _no_send_sync)
        return self

    def __exit__(self, *exc) -> bool:                       # noqa: ANN002
        for obj, name, old in reversed(self._saved):
            try:
                setattr(obj, name, old)
            except Exception:                               # noqa: BLE001
                pass
        self._saved.clear()
        return False

    def report(self) -> str:
        if not self.violations:
            return "零违规（没有任何真实出口被调用）"
        head = self.violations[0].splitlines()[0]
        return f"❌ {len(self.violations)} 次真实联网，首次：{head}"


# ══════════════════════════════════════════════════════════════════════════════
#  ② 打桩：预检（Model.ask）+ 打局（routing.build_client）
# ══════════════════════════════════════════════════════════════════════════════

STUB = {"preflight": 0, "preflight_prompts": [], "client_calls": 0, "client_hosts": []}


async def _fake_ask(self, prompt, system=None):           # noqa: ANN001
    """冒充模型：回一个合法但空的 JSON（引擎会按规则补默认动作）。

    这就是**连通性预检**走的那条路（`exam_live.preflight` → `Model.ask`）。
    """
    STUB["preflight"] += 1
    STUB["preflight_prompts"].append((str(prompt)[:60], str(system or "")[:40]))
    return {"ok": True, "elapsed": 0.01, "text": "{}", "empty": False,
            "finish": "stop", "reasoning_chars": 0, "error": None}


class _OfflineClient:
    """打局路径的离线假客户端（冒充 `wolf.llm.LLMClient`）。

    为什么要有它：`play_one` 用 `routing.build_client(spec_for(model))` 现造一个真客户端，
    每次决策都 `chat_json` 真发 HTTP —— 老版本漏的就是这一条（预检打桩救不了它）。
    这里逐字实现打局真正用到的那几个接口（`extra_for` / `chat_json` / `last_call` /
    `aclose` / `model`），返回值跟老 `_fake_ask` 同口径：**合法但空的 JSON**。
    """

    def __init__(self, spec, label: str = "") -> None:      # noqa: ANN001
        self.model = str(getattr(spec, "model", "") or "")
        self.base_url = str(getattr(spec, "base_url", "") or "")
        self.host = "offline-stub"                         # 不写真 host：日志里不该看着像在联网
        self.label = label or self.model
        self.key = f"offline/{self.model}"
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "errors": 0, "seconds": 0.0}
        self.last_call: dict = {}
        STUB["client_calls"] += 1
        STUB["client_hosts"].append(self.base_url)

    def extra_for(self, task_kind: str, day: int) -> dict:  # noqa: ANN001
        return {}

    async def chat_json(self, packet: str, extra: dict | None = None) -> dict:
        self.usage["calls"] += 1
        # 每次都给一个**新** dict：`LLMAgent.absorb()` 靠 `is` 判断「这次有没有成功」，
        # 复用同一个对象会被记成「没更新」，账目就少算调用次数。
        self.last_call = {"model": self.model, "raw": "{}", "reasoning": "",
                          "thinking": dict(extra or {}), "prompt_tokens": 0,
                          "completion_tokens": 0, "elapsed": 0.0}
        return {}

    async def aclose(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"<_OfflineClient {self.label}（离线桩，{self.base_url}）>"


def _install_stubs(exam, exam_live) -> tuple[object, object]:   # noqa: ANN001
    """装上两条桩，返回 (原 Model.ask, 原 routing.build_client) 供还原。"""
    old_ask = exam.Model.ask
    old_build = exam_live.routing.build_client
    exam.Model.ask = _fake_ask

    def _offline_build(spec, label: str = "", thinking_style: str | None = None):  # noqa: ANN001, ANN202
        return _OfflineClient(spec, label)

    exam_live.routing.build_client = _offline_build
    return old_ask, old_build


# ══════════════════════════════════════════════════════════════════════════════
#  ③ 硬超时 + 挂住诊断
# ══════════════════════════════════════════════════════════════════════════════

class _Tee:
    """把被测代码的 stdout 同时喂给真终端和内存（诊断要「最后一条日志是什么」）。"""

    def __init__(self, inner) -> None:                     # noqa: ANN001
        self._inner = inner
        self.lines: list[str] = []
        self._buf = ""

    def write(self, text: str) -> int:
        try:
            self._inner.write(text)
        except Exception:                                  # noqa: BLE001
            pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line)
        return len(text)

    def flush(self) -> None:
        try:
            self._inner.flush()
        except Exception:                                  # noqa: BLE001
            pass

    def tail(self, n: int = 8) -> list[str]:
        out = list(self.lines)
        if self._buf.strip():
            out.append(self._buf)
        return [x for x in out if x.strip()][-n:]

    def __getattr__(self, name: str):                       # noqa: ANN401
        return getattr(self._inner, name)


def _task_frames(task) -> str:                              # noqa: ANN001
    """挂住的协程停在哪一行（内层协作取消时拿这个）。"""
    rows: list[str] = []
    try:
        for frame in reversed(task.get_stack()):
            rows.append(f"      {os.path.basename(frame.f_code.co_filename)}:"
                        f"{frame.f_lineno} in {frame.f_code.co_name}()")
    except Exception as exc:                                # noqa: BLE001
        rows.append(f"      （拿不到任务栈：{type(exc).__name__}: {exc}）")
    if not rows:
        rows.append(f"      （没有待执行栈帧；coro={task.get_coro()!r}）")
    return "\n".join(rows)


def _thread_stack(thread: threading.Thread) -> str:
    """外层兜底：事件循环都转不动时（同步阻塞），直接看线程在哪个函数里。"""
    frame = sys._current_frames().get(thread.ident)
    if frame is None:
        return "      （线程已结束，拿不到栈）"
    return "".join("      " + line for line in traceback.format_stack(frame)[-12:])


def _run_guarded(factory, limit: float = HARD_LIMIT_S,
                 inner_margin: float = INNER_MARGIN_S) -> dict:   # noqa: ANN001
    """在独立线程 + 独立事件循环里跑一段协程，带硬超时。

    两层：
      · 内层 `asyncio.wait(timeout=...)`：正常挂住（在 await 上等）时能协作取消，
        并留下「挂在哪个函数哪一行」；
      · 外层 `thread.join(limit)`：内层都被卡住（比如同步阻塞把事件循环焊死）时兜底，
        拿线程栈。线程是 daemon，所以就算它永远不出来，进程也能退出（不让回归被拖住）。
    """
    box: dict = {}
    inner = max(1.0, limit - inner_margin)

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        try:
            task = loop.create_task(factory())
            box["task"] = task
            done, _pending = loop.run_until_complete(asyncio.wait({task}, timeout=inner))
            if task in done:
                try:
                    box["rc"] = task.result()
                except BaseException as exc:                # noqa: BLE001
                    box["exc"] = exc
            else:
                box["hung"] = "inner"
                box["stack"] = _task_frames(task)
                task.cancel()
                try:
                    loop.run_until_complete(asyncio.wait({task}, timeout=5))
                except BaseException:                       # noqa: BLE001
                    pass
        except BaseException as exc:                        # noqa: BLE001
            box["exc"] = exc
        finally:
            box["done"] = True

    thread = threading.Thread(target=worker, name="exam-live-guarded", daemon=True)
    started = time.time()
    thread.start()
    thread.join(limit)
    out = {"elapsed": round(time.time() - started, 1), "alive": thread.is_alive(),
           "box": box, "hung": box.get("hung")}
    if thread.is_alive():
        out["hung"] = "watchdog"
        out["thread_stack"] = _thread_stack(thread)
    return out


def _print_hang_report(result: dict, tee: _Tee, guard: OfflineGuard) -> None:  # noqa: ANN001
    """挂住时必须交代清楚：哪一步、最后一条日志、建议查什么。"""
    box = result["box"]
    print("\n" + "!" * 78)
    print(f"⏱ 硬性内部超时：exam_live.main() 跑了 {result['elapsed']:.1f} 秒还没结束"
          f"（上限 {HARD_LIMIT_S:.0f} 秒）→ 判失败，主动退出（不等别人来杀）")
    print("!" * 78)
    if result["hung"] == "inner":
        print("  挂在哪一步（协程栈，最内层在最上面）：")
        print(box.get("stack") or "      （无栈）")
    else:
        print("  挂在哪一步：**事件循环都转不动了**（内层超时都没机会触发），线程栈：")
        print(result.get("thread_stack") or "      （无栈）")
    print(f"  打局进度：离线模型桩被调用 {STUB['client_calls']} 次"
          f"{'（一次都没调到 → 卡在开局/预检之前的准备阶段）' if not STUB['client_calls'] else ''}"
          f"；预检桩 {STUB['preflight']} 次")
    print(f"  出口桩：{guard.report()}")
    print("  最后一条日志（被测代码的 stdout 尾巴）：")
    for line in tee.tail(8):
        print(f"    | {line}")
    print("  建议查什么：")
    print("    1) 上游是不是「连上了但一个字节都不回」（读超时/对端不应答）——"
          " 看 httpx timeout 与引擎的 call_timeout/decision_timeout"
          "（config.yaml 现在是 300/420 秒，一次决策最坏能安静 7 分钟）；")
    print("    2) 是不是有一条**没被桩覆盖**的调用路径 —— 上面「出口桩」若报了违规，"
          "违规里的调用栈就是漏网的那条；")
    print("    3) 是不是同步阻塞（不在 await 上）—— 看上面的线程栈；")
    print(f"    4) 复现命令：cd {os.path.dirname(os.path.abspath(__file__))} && python"
          f" {os.path.basename(os.path.abspath(__file__))}")
    print("!" * 78)


def _watchdog_selftest() -> bool:
    """看门狗自己也得被测一次：喂一个故意挂住的协程，必须在 2 秒左右报「挂住」。"""
    async def _never() -> int:
        await asyncio.sleep(3600)
        return 0

    res = _run_guarded(_never, limit=3.0, inner_margin=1.0)
    ok = res["hung"] in ("inner", "watchdog") and res["elapsed"] < 6.0
    print(f"       （自检：hung={res['hung']} 用时 {res['elapsed']}s，"
          f"栈={'有' if res['box'].get('stack') else '无'}）")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  报告目录善后：这条测试会真让 exam_live 落一份明细 json（`exam_live.HERE/reports`，
#  也就是 `werewolf/reports/`），可那是**桩打出来的假成绩**，留着会混进真成绩里
#  （实测第一版就落了一个 `exam-live-DS-134023.json`，和 10-07 的真成绩并排放着）。
#  所以只删「本次新产生」的那几个文件，别的一律不碰。
# ══════════════════════════════════════════════════════════════════════════════

REPORTS_DIR = os.path.join(ROOT, "reports")


def _snapshot_reports() -> set[str]:
    try:
        return set(os.listdir(REPORTS_DIR))
    except OSError:
        return set()


def _cleanup_reports(before: set[str]) -> list[str]:
    removed: list[str] = []
    for name in sorted(set(os.listdir(REPORTS_DIR)) - before) if os.path.isdir(REPORTS_DIR) else []:
        path = os.path.join(REPORTS_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(name)
        except OSError:
            pass
    return removed


# ══════════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:                                          # noqa: C901
    started_all = time.time()
    import exam                                            # noqa: E402
    from wolf import routing as wolf_routing                # noqa: E402

    src = io.open(os.path.join(ROOT, "tools/exam_live.py"), encoding="utf-8").read()
    check("L0 看门狗本身有效（喂一个永远挂住的协程，必须报挂住并自己收手）",
          _watchdog_selftest())
    check("L1 parser 注册了 --max-days（这条就是当年崩掉的那个参数）",
          '"--max-days"' in src, "源码里有 --max-days" if '"--max-days"' in src else "❌ 又漏了")

    import exam_live                                         # noqa: E402

    # ⚠️ 预检的桩必须打在**同一个类对象**上：`exam_live` 是 `from exam import Model`，
    #    如果哪天改成别的地方定义的 Model，这里立刻失败（而不是悄悄打空桩去联网）。
    #    打局那条同理：`exam_live.routing` 必须就是 `wolf.routing`，否则下面换掉
    #    `build_client` 也拦不住真客户端。
    check("L1b 两条桩都打在 exam_live 真正用的对象上",
          getattr(exam_live, "Model", None) is exam.Model and exam_live.routing is wolf_routing,
          "exam_live.Model is exam.Model；exam_live.routing is wolf.routing")

    old_ask, old_build = _install_stubs(exam, exam_live)
    old_argv = sys.argv
    real_stdout = sys.stdout
    tee = _Tee(real_stdout)
    guard = OfflineGuard()
    reports_before = _snapshot_reports()
    result: dict = {}
    try:
        sys.argv = ["exam_live.py", "--games", "1", "--mode", "single",
                    "--board", "eleven_dream", "--only", "DS", "--seed", "11"]
        with guard:
            sys.stdout = tee
            print(f"（离线闸门已装；整局硬上限 {HARD_LIMIT_S:.0f} 秒）", flush=True)
            result = _run_guarded(lambda: exam_live.main())
    finally:
        sys.stdout = real_stdout
        sys.argv = old_argv
        exam.Model.ask = old_ask
        exam_live.routing.build_client = old_build

    out = "\n".join(tee.lines)
    rc = result["box"].get("rc")
    exc = result["box"].get("exc")

    # ---- L2：非 selftest 入口真跑完了（AttributeError 就是当年那个 bug）----
    if result["hung"]:
        _print_hang_report(result, tee, guard)
        check(f"L2 真调 main() 跑完（{HARD_LIMIT_S:.0f} 秒硬上限内）", False,
              f"挂住：{result['hung']}，见上面的挂住报告")
    elif exc is not None:
        kind = "AttributeError（就是当年那个 bug）" if isinstance(exc, AttributeError) else type(exc).__name__
        check("L2 真调 main() 不崩", False, f"{kind}: {exc}"[:160])
    else:
        check(f"L2 真调 main() 跑完并返回退出码（{result['elapsed']:.1f}s）",
              rc in (0, 1), f"exit={rc}")

    # ---- L3：座位号按板子人数夹紧：9 人板跑第 9 局不该出现 18 号 ----
    check("L3 座位号不会越界（源码里不再写死 % 11）",
          "seat = 1 + (i % 11)" not in src,
          "已改成按板子人数取模（注释里提到旧写法不算）")

    # ---- L4：离线性证明（出口桩零违规）----
    check("L4 全程零真实联网（出口桩没有任何一次被调用）",
          not guard.violations, guard.report())

    # ---- L5：两条桩都被**真的**调用过（否则「没打局也能绿」）----
    pre_ok = STUB["preflight"] >= 1
    cli_ok = STUB["client_calls"] >= 1
    hosts = "、".join(sorted(set(STUB["client_hosts"]))) or "（没造过客户端）"
    check("L5 预检 + 打局两条路都走了桩（预检桩 ≥1 且打局客户端 ≥1）",
          pre_ok and cli_ok,
          f"预检桩 {STUB['preflight']} 次、打局客户端 {STUB['client_calls']} 次"
          f"（本来要连：{hosts}）")

    # ---- L6：真打到局了（预检失败会 continue，那样 main 也返回 0，不能算过）----
    played = "第 1 局：" in out
    check("L6 真打到局（输出里有「第 1 局：」，不是预检失败就空跑）", played,
          "有打局记录" if played else
          "没看到「第 1 局：」→ 多半是预检/模型清单没过（rc=" + str(rc) + "）")

    removed = _cleanup_reports(reports_before)
    if removed:
        print(f"  （善后：删掉本测试造出来的假成绩明细 {len(removed)} 个：{'、'.join(removed)}）")

    bad = [x for x in RESULTS if not x[1]]
    print(f"\n（本测试总耗时 {time.time() - started_all:.1f}s；打局硬上限 {HARD_LIMIT_S:.0f}s；"
          f"离线 = 出口桩零违规）")
    print("=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    sys.stdout.flush()
    sys.stderr.flush()
    if bad and result.get("alive"):
        # 线程还焊在某个同步调用里：正常 return 会被「等非 daemon 线程」拖住的风险绕开，
        # 直接硬退（摘要已经打完、也 flush 过了），保证「一定自己出来」。
        print("[硬退] 被测线程已经卡死收不回来，直接退出（不让整轮回归被拖住）", flush=True)
        os._exit(1)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
