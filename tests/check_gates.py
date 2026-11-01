# coding=utf-8
"""闸门自查：**每个登记在案的测试脚本都必须"能变红"**。

为什么需要这个：`run_all.py` 判失败只看两件事 ——
  ① 子进程**非 0 退出**；② 输出里有「共 N 项：通过 X，失败 Y」那一行且 Y > 0。
两样都没有的脚本 = **假回归门**：它哪怕打印满屏 ❌，整轮回归照样全绿。
2026-10-09 项目自查实测抓到 **10 个**这样的脚本（其中两个失败时还真的在打 ❌）——
这不是"某个测试写坏了"，而是一整类结构性漏洞，所以要有东西长期守着。

判据（对每个脚本静态读源码）：
  · 出现 `raise SystemExit(...)` / `sys.exit(...)` / `SystemExit(...)` → 有退出码 ✅
  · 或者出现「共 N 项：通过 X，失败 Y」那种**会计数**的汇总打印 → 会被 run_all 数出来 ✅
  · 两样都没有 → ❌ 判红（并列出脚本名，方便照着补）

排除项：纯工具/夹具（`run_all.py` 自己、`cdp_lite.py`、`extract_js.py`、`fake_*.py`、
以 `_` 开头的临时脚本）。**新增脚本忘了写退出码会被这里挡住。**
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


#: 不是"测试"、因此不要求退出码的文件
EXEMPT = {
    "run_all.py", "cdp_lite.py", "extract_js.py", "index_inline.js",
    "fake_llm.py", "fake_asr.py", "fake_llm_gate.py",
    "check_api.py", "mutation_verify.py",     # 夹具 / 辅助（被别的脚本 import）
}
EXEMPT_PREFIX = ("_", "tmp")

runner = io.open(os.path.join(HERE, "run_all.py"), encoding="utf-8").read()
scripts = sorted(f for f in os.listdir(HERE) if f.endswith(".py"))
registered = [f for f in scripts
              if f not in EXEMPT and not f.startswith(EXEMPT_PREFIX) and f in runner]

print("=== 一、登记在案的脚本（从 run_all.py 里现查，不抄名单）===")
print(f"  共 {len(registered)} 个：{'、'.join(registered[:6])}…")

EXIT_RE = re.compile(r"raise\s+SystemExit\(|sys\.exit\(|SystemExit\(")
COUNT_RE = re.compile(r"共\s*\{?\w*\}?\s*项(?:检查)?：通过.*失败")

print("\n=== 二、每个脚本都必须能变红（有退出码或会计数的汇总行）===")
no_gate: list[str] = []
for name in registered:
    src = io.open(os.path.join(HERE, name), encoding="utf-8", errors="ignore").read()
    has_exit = bool(EXIT_RE.search(src))
    has_count = bool(COUNT_RE.search(src))
    if not (has_exit or has_count):
        no_gate.append(name)
check(f"2a {len(registered)} 个脚本都有『变红』的路", not no_gate,
      ("缺闸门：" + "、".join(no_gate)) if no_gate else "全部合格")

print("\n=== 三、run_all 的判据本身没退化 ===")
# 判失败的两条依据必须还在（有人把 `r["rc"] != 0 or r["bad"]` 改宽了，这里要拦住）
check("3a 仍按『退出码 != 0 或 bad > 0』判失败",
      bool(re.search(r'failed\s*=\s*\[r for r in rows if r\["rc"\]\s*!=\s*0 or r\["bad"\]\]', runner)))
check("3b 仍会把输出里的『共 N 项…失败 Y』数成 bad",
      "def summarize(" in runner and "失败" in runner)
check("3c 仍有『未分类脚本』的点名（新脚本忘登记不会静默漏跑）",
      "未分类" in runner, "见 run_all.py 开头的分档清单")
# ⚠️ 2026-10-11：拆车道那次实测踩到 —— 脚本被分到 8089 却不读 `WW_TEST_PORT`，
#   于是它照样去打 8086，和那条链上的脚本抢同一个房间（表现为某条断言假红，极难排查）。
#   这条守住"run_all 必须保留这个自查 + 自查必须点名到脚本"。
check("3d 仍会自查『车道声明的端口，脚本真的读了吗』",
      "def lane_env_problems(" in runner and "WW_TEST_PORT" in runner,
      "见 run_all.py::lane_env_problems")

print("\n=== 四、测试档**不许花用户的钱**（2026-10-10 账单事故的护栏）===")
# 事故现场：`tests/test-config.yaml` 里写的是生产档案（profile: QWEN/DEEPSEEK/GLM），
#   而 `t_cancel_paths.py` 第四节要验「真模型调用时放弃本局」，发的是 `mock: false`
#   ⇒ 8086 那台测试实例**真的去打用户的付费渠道**（一次全量 3 次真实调用）。
# 这一节把它钉死：① 测试实例的配置只能指向本地假上游；② 发 `mock: false` 的脚本必须在白名单里。
try:
    import yaml                                                 # noqa: PLC0415
    _cfg_path = os.path.join(HERE, "test-config.yaml")
    _cfg = yaml.safe_load(io.open(_cfg_path, encoding="utf-8").read()) or {}
    _llm = (_cfg.get("llm") or {})
    _blocks = {"default": _llm.get("default") or {}}
    for _k, _v in (_llm.get("seats") or {}).items():
        _blocks[f"seats[{_k}]"] = _v or {}
    for _k, _v in (_llm.get("roles") or {}).items():
        _blocks[f"roles[{_k}]"] = _v or {}
    _bad: list[str] = []
    for _name, _blk in _blocks.items():
        if not isinstance(_blk, dict):
            continue
        if _blk.get("profile"):
            _bad.append(f"{_name} 写了 profile: {_blk['profile']}")
        _url = str(_blk.get("base_url") or "")
        if _url and not re.match(r"^http://(127\.0\.0\.1|localhost)", _url):
            _bad.append(f"{_name} base_url 指向外部：{_url}")
    # 允许"不写 base_url"的块（继承 default 的本地地址），但不允许写外部地址。
    _n_blocks = len([n for n in _blocks if n != "default"])
    check("4a tests/test-config.yaml 的 llm 只指向本地假上游（不写 profile、base_url 非外部）",
          not _bad, "；".join(_bad) if _bad else f"default + {_n_blocks} 个座位块全部本地")
    check("4b 测试实例的默认 base_url 就是本地假上游",
          str((_llm.get("default") or {}).get("base_url") or "").startswith("http://127.0.0.1:"),
          str((_llm.get("default") or {}).get("base_url")))
except Exception as exc:                                        # noqa: BLE001
    check("4a tests/test-config.yaml 的 llm 只指向本地假上游", False,
          f"{type(exc).__name__}: {exc}")

# `"mock": False` = 让**服务端**去调真模型。只允许白名单里的脚本这么干，
# 而且它们各自必须把模型指向本地假上游（t_stall_ws 用自己的 stall-config.yaml）
MOCK_FALSE_OK = {"t_stall_ws.py",            # 自拉 8099 假上游 + stall-config.yaml（本地）
                 "t_cancel_paths.py"}        # 第四节要"真模型调用 + 立刻放弃"；8086 现在是假上游
_offenders: list[str] = []
#: 这个文件自己就写着那句话（在下面的报错文案里），当然要跳过。
_SELF = {os.path.basename(__file__)}
for _f in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
    if _f in _SELF:
        continue
    _src = io.open(os.path.join(HERE, _f), encoding="utf-8", errors="ignore").read()
    if re.search(r'"mock"\s*:\s*False', _src) and _f not in MOCK_FALSE_OK:
        _offenders.append(_f)
check(f"4c 发 `mock: false` 的脚本都在白名单里（共 {len(MOCK_FALSE_OK)} 个）",
      not _offenders,
      ("这些脚本会去调真模型，若指向真渠道就是真花钱：" + "、".join(_offenders))
      if _offenders else "没有新增的『真模型』脚本")

print("\n=== 五、内容文件的行尾（CRLF 纪律）===")
# ⚠️ 2026-10-11 加：我自己用 python 脚本批量改中文内容时，**两次**把 `content/*.md`
#    整份从 CRLF 写成了 LF（一次打法书 1453 行、一次概念库 331 行）。
#    根因是 `io.open(p, encoding="utf-8").read()`（文本模式读会把 \r\n 归一成 \n）配
#    `io.open(p, "wb").write(...)`（二进制写不做补回）—— **改内容文件请优先用 edit 工具**。
#    这条闸门就是为了让下一次这种事故当场变红，而不是等谁去看 git diff。
_LF_BAD: list[str] = []
for _d in ("content", "docs"):
    _dir = os.path.join(ROOT, _d)
    if not os.path.isdir(_dir):
        continue
    for _f in sorted(os.listdir(_dir)):
        if not _f.endswith(".md"):
            continue
        _p = os.path.join(_dir, _f)
        _b = io.open(_p, "rb").read()
        _bare = _b.count(b"\n") - _b.count(b"\r\n")
        if _bare:
            _LF_BAD.append(f"{_d}/{_f} 裸 LF {_bare}")
check("5a content/ 与 docs/ 下的 .md 全是 CRLF（没有裸 LF）", not _LF_BAD,
      "；".join(_LF_BAD) if _LF_BAD else "全部 CRLF")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
