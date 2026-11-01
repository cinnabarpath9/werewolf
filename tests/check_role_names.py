# coding=utf-8
"""角色名接线闸门：**改了角色名或新增角色，所有"靠名字认识它"的表都必须跟上**。

为什么要有它（2026-10-09 复查实测，一次抓到 6 处漏接线）：
  白痴改名叫**愚者**、又新增了摄梦人/守墓人/养鸦人/幸运儿之后，下面这些**手写的名字表**
  统统没跟上 —— 而且**没有任何东西会报错**：
    · `wolf/context.py::_CLAIM_ALIASES` —— 模型说「我是愚者」，引擎认不出来（当成没报身份）；
    · `wolf/persona.py::RISKY_WORDS` —— 人设卡里写「守墓人」不再被拦（等于提前泄露身份）；
    · `server.py::board_options.tag_of` —— 板子标签失灵、且名字写死旧称；
    · `tools/review.py::ROLE_NAME` —— 复盘页把新角色打成英文 key；
    · `wolf/tabletalk.py` 的「我是X」识别串、`tools/bench_thinking.py` 的角色识别元组。

判据：**以 `wolf.roles.ROLES` 为唯一事实来源**，逐个角色检查上面这些表认不认得它的**当前名字**；
另外把"表里写没写死旧名字"也顺手查一遍（写死的那几处已改成派生）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import context as ctx                                  # noqa: E402
from wolf import persona as persona_mod                          # noqa: E402
from wolf.roles import BUILTIN_BOARDS, ROLES                     # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


names = {k: v.name for k, v in ROLES.items()}
# ⚠️ 2026-10-10：以前写死 `BUILTIN_BOARDS["eleven_dream"]` —— 默认板子换成狼王版之后，
#    这张"默认板子上的角色都要认得出来"的闸门就管错对象了（狼王根本不在检查范围里）。
import io as _io                                               # noqa: E402
import yaml as _yaml                                            # noqa: E402

try:
    _cfg = _yaml.safe_load(_io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    _default_board = (_cfg.get("game") or {}).get("board") or "eleven_wolfking"
except Exception:                                               # noqa: BLE001
    _default_board = "eleven_wolfking"
default_roles = list(dict.fromkeys(BUILTIN_BOARDS[_default_board].roles))
print(f"（默认板子 = {_default_board}）")

print(f"=== 一、自称识别：每个角色的**当前名字**都要认得出来（{len(ROLES)} 个角色）===")
unrecognized = [f"{k}({names[k]})" for k in ROLES if ctx._norm_role_key(names[k]) != k]
check("1a 规则书里写的角色名 → 都能归一成角色 key", not unrecognized, str(unrecognized))
check("1b 默认板子上的角色**全部**认得出来",
      all(ctx._norm_role_key(names[k]) == k for k in default_roles),
      str([names[k] for k in default_roles if ctx._norm_role_key(names[k]) != k]))
check("1c 旧称仍然认（老存档/模型偶尔还用「白痴」）", ctx._norm_role_key("白痴") == "idiot")

print("\n=== 二、人设卡敏感词：角色名不许写进人设（否则等于泄露身份）===")
missing_risky = [names[k] for k in ROLES if names[k] not in persona_mod.RISKY_WORDS]
check("2a 每个角色名都在 RISKY_WORDS 里", not missing_risky, str(missing_risky))
missing_words = [names[k] for k in ROLES if names[k] not in persona_mod.ROLE_WORDS]
check("2b 每个角色名都在 ROLE_WORDS 里", not missing_words, str(missing_words))

print("\n=== 三、复盘页 / 板子标签：名字表要跟 ROLES 一致 ===")
try:
    import review                                                # noqa: E402
    bad_review = [f"{k}: {review.ROLE_NAME.get(k)!r}≠{names[k]!r}"
                  for k in ROLES if review.ROLE_NAME.get(k) != names[k]]
    check("3a 复盘页 ROLE_NAME 与 ROLES 一致", not bad_review, str(bad_review))
except Exception as exc:                                         # noqa: BLE001
    check("3a 复盘页 ROLE_NAME 与 ROLES 一致", False, f"{type(exc).__name__}: {exc}")

srv = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("3b server.py 里不再写死旧角色名做标签",
      'return "白痴"' not in srv and 'return "守卫"' not in srv)

print("\n=== 四、识别串（发言里说「我是X」）覆盖默认板子 ===")
book = open(os.path.join(ROOT, "wolf", "tabletalk.py"), encoding="utf-8").read()
missing_claim = [names[k] for k in default_roles if f"我是{names[k]}" not in book and names[k] not in book]
check("4a tabletalk 的识别串覆盖默认板子上的角色",
      not missing_claim, str(missing_claim))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
