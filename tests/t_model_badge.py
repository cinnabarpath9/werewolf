# coding=utf-8
"""右上角「当前发言模型」徽章的闸门（2026-10-31 加）。

两件事必须钉住：
  ① 白天公开行动 → 报文里带 `model`（前端据此画「3号 · xxx」）；
  ② **夜里 / 狼专属任务 → 绝不带座位号，也不带 model** ——
     「几号在思考 + 用的哪个模型」组合起来就能点名谁有夜间身份，这是硬泄露。

跑法：python tests/t_model_badge.py（纯本地，0.2s）
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server                                        # noqa: E402
from wolf import routing                             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


class Stub:
    """只带 `_thinking_msg` / `_seat_model` 需要的那几个属性。"""
    def __init__(self, seats, night=False, cfg=None) -> None:
        self.game = SimpleNamespace(seats=seats)
        self.cfg = cfg or {}
        self._night = night

    SECRET_ASK_KINDS = server.Room.SECRET_ASK_KINDS
    _seat_model = server.Room._seat_model

    def _is_night(self) -> bool:
        return self._night


def main() -> int:
    real_resolve = routing.resolve
    routing.resolve = lambda cfg, seat: SimpleNamespace(model=f"model-of-{seat.id}")
    try:
        seats = [SimpleNamespace(id=3, is_human=False, role_key="wolf"),
                 SimpleNamespace(id=5, is_human=True, role_key="seer")]

        print("=== 一、白天公开行动：带座位号 + 模型名 ===")
        day = Stub(seats, night=False)
        msg = server.Room._thinking_msg(day, 3, "speech")
        check("1a 报文里有 seat=3", msg.get("seat") == 3, str(msg))
        check("1b 报文里有 model 且是这个座位的", msg.get("model") == "model-of-3", str(msg.get("model")))
        check("1c 不带 key/host 之类敏感字段",
              not any(k in msg for k in ("key", "api_key", "host", "base_url")), str(msg.keys()))

        print("\n=== 二、夜里：座位号与模型都不给 ===")
        night = Stub(seats, night=True)
        m2 = server.Room._thinking_msg(night, 3, "seer_check")
        check("2a 夜里 seat 为 None", m2.get("seat") is None, str(m2))
        check("2b 夜里没有 model 字段", "model" not in m2, str(m2))
        check("2c 夜里标了 night=True", m2.get("night") is True, str(m2))

        print("\n=== 三、白天但狼专属任务：同样脱敏 ===")
        for kind in server.Room.SECRET_ASK_KINDS:
            m3 = server.Room._thinking_msg(day, 3, kind)
            ok = m3.get("seat") is None and "model" not in m3
            check(f"3  {kind} 不带座位/模型", ok, str(m3))

        print("\n=== 四、人类座位没有模型名（前端据此隐藏）===")
        m4 = server.Room._thinking_msg(day, 5, "speech")
        check("4a 人类座位 model 为空串", m4.get("model") == "", str(m4))

        print("\n=== 五、模型名缓存：同一座位只解析一次 ===")
        calls = {"n": 0}
        def counting(cfg, seat):
            calls["n"] += 1
            return SimpleNamespace(model="cached-model")
        routing.resolve = counting
        room = Stub([SimpleNamespace(id=7, is_human=False, role_key="wolf")], night=False)
        for _ in range(3):
            server.Room._seat_model(room, 7)
        check("5a 三次查询只调用一次 routing.resolve", calls["n"] == 1, f"calls={calls['n']}")
    finally:
        routing.resolve = real_resolve

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())