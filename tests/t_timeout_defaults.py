# coding=utf-8
"""守住一件事：**手机看到的秒数 = 引擎真正用的上限**（两把尺是这项目最忌讳的毛病）。

背景（2026-10-08 收敛前）：
  `game.call_timeout` / `game.decision_timeout` 这两个键的**兜底默认值**在四个地方各写了一份 ——
  `server.py` 推给手机的 status 写 300、同一份配置建 `Game` 时写 150、
  `tools/exam_live.py` 又写 150、引擎参数默认 0（= 不设上限）。
  配置里一旦漏写或写错键名，手机就会显示「最多等到 300 秒」而引擎 150 秒就把它砍了。
  现在唯一取值口是 `wolf/engine.py::timeouts_from_cfg`。

本脚本查四件事：
  1. 取值函数本身：缺键 → 兜底；写了 → 原样；`None` → 兜底；
  2. `Room` 用**故意删掉这两个键**的配置建出来时，`room.call_timeout` 与
     `room.status()['call_timeout']` 是同一个数（这就是当年那条不一致）；
  3. 真配置（config.yaml）下，status 报的数 == 配置里写的数（手机看到的就是你配的）；
  4. 结构闸：`server.py::Room.start` 里 `Game(...)` 的这两个关键字实参必须是 `self.*`、
     `tools/exam_live.py` 里必须是 `timeouts_from_cfg` 那一对 —— 不许再写死字面量。
     ⚠️ 第 4 条是**结构类**判据（规范 §9：只查"机关在不在"），所以它自带一个**变异自证**：
     拿一段"故意写回字面量"的假源码喂给同一个判据，必须判红；否则说明这条判据是空过的。

跑法： python t_timeout_defaults.py
"""
from __future__ import annotations

import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                   # noqa: E402

import server as srv                                          # noqa: E402
from wolf.engine import (DEFAULT_CALL_TIMEOUT,                 # noqa: E402
                         DEFAULT_DECISION_TIMEOUT, timeouts_from_cfg)

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def load_cfg() -> dict:
    with io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── 一、取值函数本身 ────────────────────────────────────────────────────────────
print("=== 一、timeouts_from_cfg：缺键兜底 / 显式值 / None ===")
c0, d0 = timeouts_from_cfg({})
check("1a 空配置 → 兜底值", (c0, d0) == (DEFAULT_CALL_TIMEOUT, DEFAULT_DECISION_TIMEOUT),
      f"{c0} / {d0}")
c1, d1 = timeouts_from_cfg(None)
check("1b None → 同一个兜底（不能因为传 None 就炸或换一套数）", (c1, d1) == (c0, d0))
c2, d2 = timeouts_from_cfg({"call_timeout": 123, "decision_timeout": 456})
check("1c 写了就用写的", (c2, d2) == (123.0, 456.0), f"{c2} / {d2}")
c3, _ = timeouts_from_cfg({"call_timeout": "0.5"})
check("1d 字符串数字也吃得下（YAML 里写成带引号是常见事）", c3 == 0.5, str(c3))
c4, _ = timeouts_from_cfg({"call_timeout": 0})
check("1e 显式 0 原样返回（0 = 不设上限，是引擎自己的口径）", c4 == 0.0, str(c4))

# ── 二、Room：建局与 status 必须是同一个数（本次修的就是这条）────────────────────
print("\n=== 二、配置里**没有**这两个键时：status 报的数与建局用的是不是同一个 ===")
cfg = load_cfg()
cfg.setdefault("game", {})
for k in ("call_timeout", "decision_timeout"):
    cfg["game"].pop(k, None)                      # 故意删掉，逼出兜底路径
room = srv.Room(cfg, mock_default=True)
st = room.status()
check(f"2a room.call_timeout == 兜底值 {DEFAULT_CALL_TIMEOUT:g}",
      room.call_timeout == DEFAULT_CALL_TIMEOUT, str(room.call_timeout))
check("2b status 报的 call_timeout 与建局用的是同一个数",
      float(st["call_timeout"]) == room.call_timeout, f"{st['call_timeout']} vs {room.call_timeout}")
check("2c decision_timeout 同理",
      float(st["decision_timeout"]) == room.decision_timeout,
      f"{st['decision_timeout']} vs {room.decision_timeout}")
# ⚠️ 两个兜底值**可以不同**（现在是 420 / 540，与 config.yaml 一致）；
#    这条守的是"缺键时不互相串"，不是"两者必须相等"。
check(f"2d 两个键缺失时不会互相串（各自落到自己的兜底："
      f"{DEFAULT_CALL_TIMEOUT:g} / {DEFAULT_DECISION_TIMEOUT:g}）",
      (room.call_timeout, room.decision_timeout)
      == (DEFAULT_CALL_TIMEOUT, DEFAULT_DECISION_TIMEOUT),
      f"{room.call_timeout} / {room.decision_timeout}")

# ── 三、真配置：手机上看到的秒数就是 config.yaml 里写的 ─────────────────────────
print("\n=== 三、真配置（config.yaml）下 status 与配置一致 ===")
real = load_cfg()
room2 = srv.Room(real, mock_default=True)
st2 = room2.status()
want_c, want_d = timeouts_from_cfg(real.get("game") or {})
check("3a status.call_timeout == config.yaml 的 game.call_timeout",
      float(st2["call_timeout"]) == want_c,
      f"status={st2['call_timeout']} config={want_c}")
check("3b status.decision_timeout == config.yaml 的 game.decision_timeout",
      float(st2["decision_timeout"]) == want_d,
      f"status={st2['decision_timeout']} config={want_d}")

# ── 四、结构闸（自带变异自证）───────────────────────────────────────────────────
print("\n=== 四、结构闸：建局处不许再写死字面量 ===")


def kwarg_sources(src: str, func: str, method_of: str | None = None) -> dict[str, str]:
    """抠出某个函数里 `Game(...)` 调用的关键字实参源码。method_of 给了就只在那个类里找。"""
    tree = ast.parse(src)
    scope: list = [tree]
    if method_of:
        cls = next((n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == method_of), None)
        scope = [cls] if cls else []
    for node in scope:
        for fn in [n for n in ast.walk(node)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]:
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Game":
                    return {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
    return {}


srv_src = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
live_src = io.open(os.path.join(ROOT, "tools", "exam_live.py"), encoding="utf-8").read()
kw_srv = kwarg_sources(srv_src, "start", method_of="Room")
kw_live = kwarg_sources(live_src, "run_one_game")
if not kw_live:                                   # 函数名变了就退一步：整个文件里第一个 Game(...)
    tree = ast.parse(live_src)
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Game":
            kw_live = {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
            break

check("4a server.py::Room.start 里 Game(call_timeout=...) 传的是 self.call_timeout",
      kw_srv.get("call_timeout") == "self.call_timeout", str(kw_srv.get("call_timeout")))
check("4b 同一处 decision_timeout 也是 self.*",
      kw_srv.get("decision_timeout") == "self.decision_timeout",
      str(kw_srv.get("decision_timeout")))
check("4c exam_live.py 的 Game(...) 用的是 timeouts_from_cfg 那一对，不是字面量",
      "timeouts_from_cfg" in live_src
      and kw_live.get("call_timeout", "").startswith("_call_timeout")
      and kw_live.get("decision_timeout", "").startswith("_decision_timeout"),
      f"{kw_live.get('call_timeout')} / {kw_live.get('decision_timeout')}")

# 变异自证：把判据喂给"写回字面量的旧写法"，它必须判红 —— 否则上面三条是空过
fake = ('class Room:\n'
        '    async def start(self):\n'
        '        g = Game(call_timeout=float(self.game_cfg.get("call_timeout", 150)),\n'
        '                 decision_timeout=float(self.game_cfg.get("decision_timeout", 300)))\n')
fake_kw = kwarg_sources(fake, "start", method_of="Room")
check("4d 变异自证：判据对『旧写法（150 字面量）』会判红",
      fake_kw.get("call_timeout") != "self.call_timeout",
      f"判据看到的实参：{fake_kw.get('call_timeout')}")
good = ('class Room:\n'
        '    async def start(self):\n'
        '        g = Game(call_timeout=self.call_timeout, decision_timeout=self.decision_timeout)\n')
check("4e 反向对照：判据对『正确写法』判绿",
      kwarg_sources(good, "start", method_of="Room").get("call_timeout") == "self.call_timeout")

# ── 汇总 ───────────────────────────────────────────────────────────────────────
print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
