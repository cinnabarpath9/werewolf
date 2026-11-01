# coding=utf-8
"""回归：配置写错时的表现（模型层审查的 P2-4 / P3-4）。

以前这几种写法会抛裸 ValueError/TypeError，一路冒到 status 广播 → 手机静默断线、界面无提示。
现在要求：不崩、并且**预检给人话**。
"""
import io
import sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

sys.path.insert(0, ROOT + r"\tools")
from check_api import load_env                           # noqa: E402

from wolf import routing                                 # noqa: E402
from wolf.engine import Seat                             # noqa: E402

# 真实配置那一节要读 .env（否则会把"缺 key"当成配置问题报出来 —— 第一版就白红了一条）
load_env(ROOT + r"\.env")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


seats = [Seat(id=i, role_key="villager") for i in range(1, 12)]

print("=== 一、类型写错不崩、预检报人话 ===")
cases = {
    "default 写成字符串": {"llm": {"default": "QWEN"}},
    "default 写成列表": {"llm": {"default": ["QWEN"]}},
    "seats 块写成数字": {"llm": {"default": {"profile": "GLM"}, "seats": {1: 7}}},
    "extra_body 写成字符串": {"llm": {"default": {"profile": "GLM"}, "seats": {1: {"extra_body": "x"}}}},
}
for label, cfg in cases.items():
    crashed = None
    try:
        routing.resolve(cfg, seats[0])
    except Exception as exc:                              # noqa: BLE001
        crashed = f"{type(exc).__name__}: {exc}"
    check(f"1 {label}：resolve 不抛异常", crashed is None, crashed or "")
    try:
        issues = routing.problems(cfg, seats)
        check(f"1 {label}：预检能跑且有话说", isinstance(issues, list), f"{len(issues)} 条")
    except Exception as exc:                              # noqa: BLE001
        check(f"1 {label}：预检能跑且有话说", False, f"{type(exc).__name__}: {exc}")

print("\n=== 二、拼错的键要报出来（以前静默不生效）===")
bad = {"llm": {"default": {"profile": "GLM"},
               "roles": {"seeer": {"profile": "GLM"}},
               "seats": {12: {"profile": "GLM"}}}}
issues = routing.problems(bad, seats)
check("2a 角色名拼错被报出", any("seeer" in x for x in issues), str(issues)[:100])
# ⚠️ 2026-11-02：**越界的数字座位不再算错误**（11 人阵容跑 6 人板是正常用法，
#    以前在这里挡死，见历史日志 §99）。仍然要报的是**非数字键**。
check("2b 越界的数字座位被忽略（不再挡开局）", not any("12" in x for x in issues),
      str([x for x in issues if "12" in x])[:90])
bad2 = {"llm": {"default": {"profile": "GLM"},
                "seats": {"一号": {"profile": "GLM"}}}}
issues2 = routing.problems(bad2, seats)
check("2c 非数字座位键仍然被报出（真写错了）", any("一号" in x for x in issues2), str(issues2)[:90])

print("\n=== 三、真实配置不误报 ===")
cfg = yaml.safe_load(io.open(ROOT + "/config.yaml", encoding="utf-8").read())
real = routing.problems(cfg, seats)
check("3a 真实 config.yaml 零问题", not real, str(real)[:160])

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
