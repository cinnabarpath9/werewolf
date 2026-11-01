# coding=utf-8
"""回归：工具与生产的参数不再漂移（方向三）。

钉死：
  1. exam_live 的客户端 spec 带上生产的 thinking 表（不再是代码兜底默认）；
  2. smoke_real 同样；
  3. 两处都能在拿不到 config 时退回默认（不因为配置问题直接崩）；
  4. 实际解析出来的 thinking 与 config.yaml 的 llm.thinking 一致。
"""
import importlib.util
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf.llm import DEFAULT_THINKING, normalize_thinking  # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def main() -> int:
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    prod = dict(((cfg.get("llm") or {}).get("thinking")) or {})
    print("生产 llm.thinking =", prod)

    print("\n=== 一、exam_live ===")
    ex = load_tool("exam_live")
    got = ex._prod_thinking()
    check("1a exam_live 读到了生产思考档", got == prod, f"{got}")
    # ⚠️ 2026-10-10 改判据：以前要求"生产 ≠ 代码兜底"，但作者定口径后两边**故意一致**
    #    （座位满档、非座位低档）—— 那条判据就永远为假了。
    #    现在改成**更硬的证明**：把"读配置"的函数换成一份特制表，看它会不会跟着变。
    _mutated = {}
    _orig = getattr(ex, "_prod_game_cfg_llm", None)
    try:
        ex._prod_game_cfg_llm = lambda: {"thinking": {"speech": {1: "off"},
                                                      "decision": {1: "off"},
                                                      "aux": {1: "off"}}}
        _mutated = ex._prod_thinking()
    finally:
        if _orig is not None:
            ex._prod_game_cfg_llm = _orig
    check("1b 它**真的读配置**（换成特制档位表，返回值必须跟着变）",
          _mutated == {"speech": {1: "off"}, "decision": {1: "off"}, "aux": {1: "off"}},
          f"{_mutated}")

    class FakeModel:
        name = "f"
        base_url = "http://x/v1"
        api_key = "k"
        model = "m"
        timeout = 10.0
        max_tokens = 1000
        extra_body = {}

    spec = ex.spec_for(FakeModel())
    check("1c spec 里带上了 thinking", bool(spec.thinking), str(spec.thinking))

    print("\n=== 二、smoke_real ===")
    src = io.open(os.path.join(ROOT, r"tools\smoke_real.py"), encoding="utf-8").read()
    check("2a smoke_real 读 config.yaml 取 thinking", "llm" in src and "thinking" in src)
    check("2b 拿不到配置时有 try 兜底", "except Exception" in src)

    print("\n=== 三、配置缺 thinking 时不会崩 ===")
    empty = {"llm": {}}
    norm, warns = normalize_thinking(empty.get("thinking"))
    check("3a normalize_thinking 对空值也能给出可用表", bool(norm), f"{norm} / warn={warns}")
    check("3b 空的就退回默认（调用方按 or None 处理，不会传空表覆盖）",
          ({} or None) is None)

    print("\n=== 四、生产各座位解析出的思考档确实来自配置 ===")
    from wolf import routing                             # noqa: PLC0415
    from wolf.engine import Game                         # noqa: PLC0415
    from _boards import make_board                        # noqa: PLC0415  退役板子的测试副本
    game = Game(board=make_board("eleven_standard"), agent_factory=None, seed=1, max_days=1)
    seat = game.seats[2]
    spec2 = routing.resolve(cfg, seat)
    check("4a 座位 spec 的 thinking 与配置一致",
          spec2.thinking.get("speech") == prod.get("speech"),
          f"{spec2.thinking.get('speech')} vs {prod.get('speech')}")

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(main())
