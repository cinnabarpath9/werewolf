# coding=utf-8
"""「模型参数」页面那套覆盖参数，真的进了座位路由吗（2026-10-31 加）。

为什么要有这个测试
----------------
页面上改了温度/超时/重试，如果只写进了 `settings.local.json` 而没进 `routing.resolve()`，
用户会以为"改好了"、实际打出去的还是 config 里那套 —— 这种"看起来生效"的 bug 最难发现。
所以这里直接调 `resolve()` 看 SeatLLM 的字段，而不是看文件里存了什么。

跑法：python tests/t_api_tuning.py（纯本地，不起服务、不调模型）
"""
from __future__ import annotations

import io
import json
import os
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

import yaml                                            # noqa: E402

from wolf import routing                               # noqa: E402
from wolf.engine import Seat                           # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


def main() -> int:
    cfg = yaml.safe_load(io.open(os.path.join(PROJ, "config.yaml"), encoding="utf-8-sig"))
    seat1 = Seat(id=1, role_key="wolf", is_human=False)      # config：ARK，temp .7 / 32000
    seat2 = Seat(id=2, role_key="witch", is_human=False)     # config：DEEPSEEK，temp 1.0

    for key in ("WOLF_SEAT_PROFILES", "WOLF_API_TUNING"):
        os.environ.pop(key, None)

    print("=== 一、没配参数 = 完全按 config ===")
    plain = routing.resolve(cfg, seat1)
    check("1a 座位 1 用的还是 config 里的值（2026-11-02：统一温度 1.0 / 20000）",
          plain.temperature == 1.0 and plain.max_tokens == 20000, str(plain))
    check("1b 超时 450、重试 4、卡住 25 也没被顺手改掉",
          plain.timeout == 450 and plain.max_retries == 4 and plain.idle_timeout == 25.0,
          f"{plain.timeout}/{plain.max_retries}/{plain.idle_timeout}")

    print("\n=== 二、页面上给这条 API 配了参数 = 覆盖 config ===")
    os.environ["WOLF_API_TUNING"] = json.dumps({
        "MINIMAX": {"temperature": 1.37, "max_tokens": 9000, "timeout": 120,
                "max_retries": 1, "idle_timeout": 12, "thinking": "off"},
    })
    tuned = routing.resolve(cfg, seat1)
    check("2a 温度被页面值覆盖（1.37，不是 config 的 1.0）", tuned.temperature == 1.37, str(tuned.temperature))
    check("2b max_tokens / 超时 / 重试 / 卡住 全部按页面值",
          (tuned.max_tokens, tuned.timeout, tuned.max_retries, tuned.idle_timeout)
          == (9000, 120, 1, 12.0), f"{tuned.max_tokens}/{tuned.timeout}/{tuned.max_retries}/{tuned.idle_timeout}")
    check("2c 思考档覆盖到所有任务组（发言/决策/杂活都是 off）",
          all(v == "off" for group in tuned.thinking.values() for v in group.values()), str(tuned.thinking))

    print("\n=== 三、只有这条 API 受影响 ===")
    other = routing.resolve(cfg, seat2)
    check("3a 座位 2 用的是另一家（千帆 DS），温度仍是 config 的 1.0",
          other.temperature == 1.0 and other.max_tokens == 20000, str(other))
    # 2026-11-02：座位 2 迁千帆 DS，档位按 v2 实测改成 high（见模型参考 §0.5）
    # 2026-11-02：2/8 迁千帆 DS 后是 speech/decision=high、aux=low（非座位活故意低一档）
    check("3b 座位 2 的思考档没被带成 off（发言/决策 high、aux low）",
          other.thinking.get("speech", {}).get(1) == "high"
          and other.thinking.get("decision", {}).get(1) == "high"
          and other.thinking.get("aux", {}).get(1) == "low", str(other.thinking))

    print("\n=== 四、坏数据不能让整局起不来 ===")
    os.environ["WOLF_API_TUNING"] = "{这不是 JSON"
    broken = routing.resolve(cfg, seat1)
    check("4a 坏 JSON → 当没配，回落 config（不抛异常）",
          broken.temperature == 1.0 and broken.max_tokens == 20000, str(broken))
    os.environ["WOLF_API_TUNING"] = json.dumps({"MINIMAX": {"temperature": "很热"}})
    weird = routing.resolve(cfg, seat1)
    check("4b 值类型写错 → 这一项不生效，其它照常",
          weird.temperature == 1.0, str(weird.temperature))
    os.environ["WOLF_API_TUNING"] = json.dumps([1, 2, 3])
    check("4c 不是对象 → 当没配", routing.resolve(cfg, seat1).temperature == 1.0)

    os.environ.pop("WOLF_API_TUNING", None)
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
