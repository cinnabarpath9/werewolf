# coding=utf-8
"""回归：换手（替补）那一手的思考档位 —— 现状事实 + `llm.fallback_thinking`（E58）。

真局现场（**只读**看过存档 `games/game-20261012-105959.json`，测试**不依赖**那份存档，
用户 prune 掉它也不影响本脚本）：
  · 5 号 = `profile: MIMO` + `thinking: low`；第 1 天 `speech_plan` 那一手 **420.01 秒超时**
    （`error` 非空、tokens 0），随后换 DEEPSEEK 顶班 —— 那一手 **36.09 秒 / 5,384 tokens**；
  · 同局 DS 的两个**原生**座位（2/8，**当时**是 `thinking: "off"`；2026-10-15 已改成 `low`）
    只要 **1.5~2.0 秒 / 36~824 tokens**。
    ⚠️ 所以本脚本**不拿生产配置当行为锚点**：座位 2 的 `off` 由 `CFG_S2_OFF` 在测试里显式造出来
    （见下面 §一 的说明）。行为测试跟着生产配置漂，就会"每次调档位红一次"。

查到的真实路径（带行号）—— **B：按「目标档案的风格 × 本座位档位」重新解析**，
**不是** A（继承原档案算出来的参数），也**不是** C（座位层配置对替补失效）：
  · `wolf/routing.py:429` `thinking_style=thinking_style_for(cfg, profile)` —— 风格跟**目标档案**；
  · `wolf/routing.py:421-427` `thinking=_merge_thinking([全局表, default, 角色, 座位, 换手覆盖])`
    —— 档位跟**本座位**（座位层 `thinking: low`）。档案本身**不带**档位：`.env` 里一个档案
    只有 `BASE_URL / API_KEY / MODEL` 三个键，`_expand` 也只认它们（没有 `<档案>_THINKING`）；
  · `wolf/agents.py:269` `extra = self.client.extra_for(task.kind, self._day)`
    —— **每次调用**按"当前客户端"现算，所以换家之后自然按新家的参数名发；
  · `wolf/llm.py:638-643` 档位 → 该风格的参数表（`THINKING_LEVELS_BY_STYLE`）。
  ⇒ DEEPSEEK 那次发的 `{thinking: {type: enabled}, reasoning_effort: low}` 是**ark 的 low**
  （`config.yaml` 的 `thinking_style` 把 DEEPSEEK 登记成 `ark`，因为它 2026-10-07 起走火山方舟），
  只是它与 **mimo 的 low 恰好同形**（`glm` 的 low 也同形）—— **光看参数分不出 A / B**。
  能分辨的是换到 **QWEN**（`enable_thinking` / `thinking_budget`）或 **HAIKU**
  （`thinking_style` 表里没有它 ⇒ 兜底 openai，只有 `reasoning_effort`）：
  那两个目标下**绝不能**出现 `thinking.type`（1d / 1e / 1g 就是钉这个的）。

本脚本钉死两件事：
  1. **现状口径不许变**：不配 `llm.fallback_thinking` 时，参数与 E58 记的那一组**逐字节相同**
     （含"座位层 off 必须原样跟过去"）；
  2. **配了之后按目标档案覆盖档位**：验收要求的那条是 `{DEEPSEEK: off}`
     ⇒ 换到 DEEPSEEK 那一手必须**正好**是 `{"thinking": {"type": "disabled"}}`。
末节（五）是**进程内变异自证**：摘掉覆盖层 / 让默认也覆盖 ⇒ 前面的判据立刻不成立。
"""
import asyncio
import copy
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

import run_game                                          # noqa: E402
from wolf import routing                                 # noqa: E402
from wolf.engine import Seat                             # noqa: E402
from wolf.llm import levels_for                          # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


#: 探测用的客户端：`build_client` 每建一个就是一个 httpx 连接池（虽然一次请求都不发），
#: 收尾统一关掉，别留一堆没人关的池子。
_CLIENTS: list = []


def probe(cfg: dict, sid: int, target: str, kind: str = "speech", day: int = 1) -> dict:
    """真发出去的那组思考参数：座位 `sid` 换到 `target` 这家、这个任务、第几天。"""
    spec = routing.resolve_profile(cfg, Seat(id=sid, role_key="villager"), target)
    cli = routing.build_client(spec, label="probe")
    _CLIENTS.append(cli)
    return cli.extra_for(kind, day)


def probe_primary(cfg: dict, sid: int, kind: str = "speech") -> dict:
    """座位**自己的主档**（不换家）发出去的那组参数。"""
    spec = routing.resolve(cfg, Seat(id=sid, role_key="villager"))
    cli = routing.build_client(spec, label="probe")
    _CLIENTS.append(cli)
    return cli.extra_for(kind, 1)


def with_fb(fb, base=None) -> dict:
    """复制一份配置、塞进 `llm.fallback_thinking`（真配置里这一段是注释）。

    `base` 不给就用真实 `config.yaml`；要验"某个座位自己的档位"时传合成配置。
    """
    cfg = copy.deepcopy(CFG if base is None else base)
    cfg["llm"]["fallback_thinking"] = fb
    return cfg


# ⚠️ 先读 .env：真实档案（DEEPSEEK / QWEN / MIMO / HAIKU …）的 BASE_URL / KEY / MODEL 都在里面，
#    不读的话 `resolve_profile` 解析出来是空的、`build_client` 会直接抛"没有 API Key"。
run_game.load_env(os.path.join(ROOT, ".env"))
CFG = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))

#: E58 / 存档里那一组（5 号换到 DEEPSEEK 顶班时真发出去的参数）—— 逐字节锚点。
SAVE_DS_LOW = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
#: **行为锚点**：座位 2 显式设成 `off` 的合成配置。
#: ⚠️ 2026-10-25：以前 1f / 2a / 2c / 2d 直接用生产 `CFG`，而生产 2/8 已在 10-15 改成 `low`
#:    ⇒ 这四条断言必红，且以后每次调档位都会假红。现在把它们锚在**显式造出来的 off** 上：
#:    验的是"座位层 off 会不会原样跟到替补"，与生产当前用什么档位无关。
CFG_S2_OFF = copy.deepcopy(CFG)
CFG_S2_OFF.setdefault("llm", {}).setdefault("seats", {}).setdefault(2, {})["thinking"] = "off"
#: 验收要求的那一组：`{DEEPSEEK: off}` 之下换到 DEEPSEEK 必须正好是它（ark 的 off）。
DS_OFF = {"thinking": {"type": "disabled"}}

print("=== 一、现状事实：换手的参数名跟**目标档案**，档位跟**本座位** ===")
check("1a 生产 config 里 DEEPSEEK 登记为 ark 风格；且 ark / mimo / glm 的 low **同形**"
      "—— 所以「5 号换到 DS 发的参数和 mimo 一样」**不能**当成「继承了 MiMo 参数」的判据",
      routing.thinking_style_for(CFG, "DEEPSEEK") == "ark"
      and levels_for("ark")["low"] == levels_for("mimo")["low"] == levels_for("glm")["low"]
      == SAVE_DS_LOW,
      f"styles: DEEPSEEK={routing.thinking_style_for(CFG, 'DEEPSEEK')}；"
      f"ark.low={levels_for('ark')['low']} mimo.low={levels_for('mimo')['low']}")

_mine5 = probe_primary(CFG, 5)
check("1b 座位 5（MIMO + low）主档发的是 mimo 的 low",
      _mine5 == SAVE_DS_LOW, str(_mine5))
check("1c 座位 5 → 替补 DEEPSEEK 发的与存档里那一手逐字节相同（= ark 的 low）",
      probe(CFG, 5, "DEEPSEEK") == SAVE_DS_LOW, str(probe(CFG, 5, "DEEPSEEK")))
#: ⚠️ 1d / 1e 是本节的**判别闸门**：只要哪天改成"把原档案风格算出来的 dict 带过去"（= A），
#:    这两条立刻红（那时 QWEN 会收到 mimo 形态、HAIKU 会收到 thinking.type）。
_qwen = probe(CFG, 5, "QWEN")
check("1d 座位 5 → 替补 QWEN 发的是 qwen 那套（enable_thinking + thinking_budget），"
      "**不许**出现 thinking.type / reasoning_effort（这就证明没有「继承原档案的参数」）",
      _qwen == {"enable_thinking": True, "thinking_budget": 512}
      and "thinking" not in _qwen and "reasoning_effort" not in _qwen,
      str(_qwen))
_haiku = probe(CFG, 5, "HAIKU")
check("1e 座位 5 → 替补 HAIKU（表里没有它 ⇒ 兜底 openai）只有 reasoning_effort，"
      "同样不许出现 thinking.type / enable_thinking",
      _haiku == {"reasoning_effort": "low"} and "thinking" not in _haiku
      and "enable_thinking" not in _haiku,
      str(_haiku))
check("1f 座位 2（合成配置：`off`）→ 替补 MIMO 必须是 mimo 的 off 形态（换家**不换档位**）",
      probe(CFG_S2_OFF, 2, "MIMO") == DS_OFF, str(probe(CFG_S2_OFF, 2, "MIMO")))

#: 1g 合成档案：不依赖用户的 .env（四个风格各造一份，只看"目标风格 × 座位档位"）。
#: 生产档的 DEEPSEEK(ark) 与 MIMO(mimo) 同形，光靠它证明不了"跟着目标走"，
#: 这里用形态两两不同的四家把这件事钉死。
for _tag, _style in (("FAKE_MIMO", "mimo"), ("FAKE_QWEN", "qwen"),
                     ("FAKE_GLM", "glm"), ("FAKE_OPENAI", "openai")):
    os.environ[f"{_tag}_BASE_URL"] = "http://127.0.0.1:9/v1"
    os.environ[f"{_tag}_API_KEY"] = "sk-fake-not-a-real-key"
    os.environ[f"{_tag}_MODEL"] = f"{_tag.lower()}-model"
CFG_SYN = {"llm": {"default": {"profile": "FAKE_MIMO"},
                   "seats": {5: {"thinking": "low"}},
                   "thinking_style": {"FAKE_MIMO": "mimo", "FAKE_QWEN": "qwen",
                                      "FAKE_GLM": "glm", "FAKE_OPENAI": "openai"},
                   "thinking": {"speech": {1: "high"}, "decision": {1: "high"},
                                "aux": {1: "low"}}}}
_syn_mine = probe_primary(CFG_SYN, 5)
_syn = {tag: probe(CFG_SYN, 5, tag) for tag in
        ("FAKE_MIMO", "FAKE_QWEN", "FAKE_GLM", "FAKE_OPENAI")}
check("1g 同一个座位（low）换到四个风格不同的档案：每个都等于**该风格** low 的精确翻译，"
      "且四者互不相同（若改成继承原档案的形态，后三条立刻红）",
      _syn_mine == SAVE_DS_LOW
      and _syn["FAKE_MIMO"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
      and _syn["FAKE_QWEN"] == {"enable_thinking": True, "thinking_budget": 512}
      and _syn["FAKE_GLM"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
      and _syn["FAKE_OPENAI"] == {"reasoning_effort": "low"}
      and len({str(v) for v in _syn.values()}) == 3,
      str(_syn))
_names = [(n, s.thinking_style) for n, s in
          routing.fallback_specs(CFG, Seat(id=5, role_key="villager"),
                                 (CFG.get("game") or {}).get("fallback"))]
check("1h 生产替补名单（server.py::_build_fallbacks 走的就是它）里 5 号第一个替补是 DEEPSEEK，"
      "且它算出来的参数与直接 resolve_profile 一致",
      _names and _names[0][0] == "DEEPSEEK" and _names[0][1] == "ark",
      str(_names))

print("\n=== 二、不配 `llm.fallback_thinking` = 现状（默认值纪律）===")
_base = probe(CFG_S2_OFF, 2, "DEEPSEEK")
check("2a 三种写法（完全没有这个键 / 空映射 {} / 只点了别的档案）产出的参数逐字节相同",
      probe(CFG_S2_OFF, 2, "DEEPSEEK") == probe(with_fb({}, CFG_S2_OFF), 2, "DEEPSEEK")
      == probe(with_fb({"GLM": "off"}, CFG_S2_OFF), 2, "DEEPSEEK") == DS_OFF,
      f"没有键={_base} 空映射={probe(with_fb({}, CFG_S2_OFF), 2, 'DEEPSEEK')} "
      f"点别的={probe(with_fb({'GLM': 'off'}, CFG_S2_OFF), 2, 'DEEPSEEK')}")
check("2b 绝对锚点①：座位 5 → DEEPSEEK 仍是存档那一组（36.09 秒 / 5,384 tokens 那次发的）",
      probe(CFG, 5, "DEEPSEEK") == SAVE_DS_LOW, str(probe(CFG, 5, "DEEPSEEK")))
check("2c 绝对锚点②：座位 2（合成配置 `off`）→ DEEPSEEK 仍是 {\"thinking\": {\"type\": \"disabled\"}}"
      "（座位层 off 原样跟过去 —— 这条被摘掉就说明默认被偷偷改了）",
      _base == DS_OFF, str(_base))
check("2d 只影响点名的那一家：{GLM: off} 之下换到 DEEPSEEK 的参数与不配时逐字节相同",
      probe(with_fb({"GLM": "off"}, CFG_S2_OFF), 2, "DEEPSEEK") == _base,
      f"{probe(with_fb({'GLM': 'off'}, CFG_S2_OFF), 2, 'DEEPSEEK')} vs {_base}")
check("2e 主档（resolve）根本不读它：{MIMO: off, DEEPSEEK: off} 之下座位 5 主档仍是 mimo 的 low",
      probe_primary(with_fb({"MIMO": "off", "DEEPSEEK": "off"}), 5) == SAVE_DS_LOW,
      str(probe_primary(with_fb({"MIMO": "off", "DEEPSEEK": "off"}), 5)))
_src = io.open(os.path.join(ROOT, "wolf", "routing.py"), encoding="utf-8").read()
_between = lambda a, b: _src[_src.index(a):_src.index(b)]        # noqa: E731
check("2f 源码级：resolve() 的函数体里一个字都没提 fallback_thinking，它只出现在 resolve_profile()",
      "fallback_thinking" not in _between("def resolve(cfg: dict, seat)", "def thinking_style_for(")
      and "fallback_thinking_for(cfg, profile)" in _between("def resolve_profile(", "def fallback_specs("),
      "见 wolf/routing.py::resolve / resolve_profile")

print("\n=== 三、配了 = 按目标档案覆盖档位 ===")
check("3a 【验收那条】{DEEPSEEK: off} ⇒ 5 号换到 DEEPSEEK 那一手**正好** "
      "{\"thinking\": {\"type\": \"disabled\"}}（一个多余的键都不许有）",
      probe(with_fb({"DEEPSEEK": "off"}), 5, "DEEPSEEK") == DS_OFF,
      str(probe(with_fb({"DEEPSEEK": "off"}), 5, "DEEPSEEK")))
check("3b 同一份配置下，换到 GLM 完全不受影响（还是 glm 的 low —— 只按目标档案点名生效）",
      probe(with_fb({"DEEPSEEK": "off"}), 5, "GLM") == SAVE_DS_LOW,
      str(probe(with_fb({"DEEPSEEK": "off"}), 5, "GLM")))
check("3c 形态由**目标风格**决定：{QWEN: off} → {enable_thinking: false}；"
      "{MIMO: high} → 只开思考；{GLM: low} → glm 的 low",
      probe(with_fb({"QWEN": "off"}), 5, "QWEN") == {"enable_thinking": False}
      and probe(with_fb({"MIMO": "high"}), 2, "MIMO") == {"thinking": {"type": "enabled"}}
      and probe(with_fb({"GLM": "low"}), 5, "GLM") == SAVE_DS_LOW,
      f"{probe(with_fb({'QWEN': 'off'}), 5, 'QWEN')} / "
      f"{probe(with_fb({'MIMO': 'high'}), 2, 'MIMO')} / "
      f"{probe(with_fb({'GLM': 'low'}), 5, 'GLM')}")
#: ⚠️ 与 E56 那个坑同类：YAML 1.1 会把**裸 `off`** 解析成布尔 `False`。这里真拿 yaml 解析一遍
#:    （不是手写 False），证明 `DEEPSEEK: off` 不加引号也能表达"关思考"。
_fb_yaml = yaml.safe_load("fallback_thinking: {DEEPSEEK: off, MIMO: low}\n")["fallback_thinking"]
check("3d YAML 裸 off（不加引号 ⇒ 真解析出来是布尔 False）也认；同一段里 low 照常（还是开思考）",
      _fb_yaml["DEEPSEEK"] is False
      and probe(with_fb(_fb_yaml), 5, "DEEPSEEK") == DS_OFF
      and probe(with_fb(_fb_yaml), 5, "MIMO") == SAVE_DS_LOW,
      f"解析={_fb_yaml} 换到DS={probe(with_fb(_fb_yaml), 5, 'DEEPSEEK')} "
      f"换到MIMO={probe(with_fb(_fb_yaml), 5, 'MIMO')}")
check("3e 分组写法逐组覆盖：{DEEPSEEK: {decision: high}} ⇒ 决策=high、发言仍沿用座位层 low",
      probe(with_fb({"DEEPSEEK": {"decision": "high"}}), 5, "DEEPSEEK", "vote")
      == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
      and probe(with_fb({"DEEPSEEK": {"decision": "high"}}), 5, "DEEPSEEK", "speech")
      == SAVE_DS_LOW,
      f"vote={probe(with_fb({'DEEPSEEK': {'decision': 'high'}}), 5, 'DEEPSEEK', 'vote')} "
      f"speech={probe(with_fb({'DEEPSEEK': {'decision': 'high'}}), 5, 'DEEPSEEK', 'speech')}")
check("3f 逐天覆盖：{DEEPSEEK: {decision: {3: high}}} ⇒ 第 1 天 low、第 3 天起 high",
      probe(with_fb({"DEEPSEEK": {"decision": {3: "high"}}}), 5, "DEEPSEEK", "vote", 1)
      == SAVE_DS_LOW
      and probe(with_fb({"DEEPSEEK": {"decision": {3: "high"}}}), 5, "DEEPSEEK", "vote", 3)
      == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
      f"d1={probe(with_fb({'DEEPSEEK': {'decision': {3: 'high'}}}), 5, 'DEEPSEEK', 'vote', 1)} "
      f"d3={probe(with_fb({'DEEPSEEK': {'decision': {3: 'high'}}}), 5, 'DEEPSEEK', 'vote', 3)}")
check("3g 档案名大小写不敏感（config 里两种写法都有）：{deepseek: off} 同样生效",
      probe(with_fb({"deepseek": "off"}), 2, "DEEPSEEK") == DS_OFF,
      str(probe(with_fb({"deepseek": "off"}), 2, "DEEPSEEK")))

print("\n=== 四、写错要报人话（不静默「配了却不生效」）===")
_SEATS = [Seat(id=i, role_key="villager") for i in range(1, 12)]


def issues_for(fb) -> list:
    return [x for x in routing.problems(with_fb(fb), _SEATS)
            if "fallback_thinking" in x or "换手档位" in x]


_bad_level = issues_for({"DEEPSEEK": "medium"})
check("4a 档位名写错（medium）被预检报出，并点明是 llm.fallback_thinking.DEEPSEEK",
      len(_bad_level) == 1 and "llm.fallback_thinking.DEEPSEEK" in _bad_level[0]
      and "medium" in _bad_level[0],
      str(_bad_level))
_bad_name = issues_for({"DEEPSEEKX": "off"})
check("4b 档案名拼错（thinking_style / game.fallback / .env 里都没有）被报「没见过」",
      len(_bad_name) == 1 and "DEEPSEEKX" in _bad_name[0], str(_bad_name))
check("4c 写成字符串 / 列表：不崩（resolve、resolve_profile 都不抛）且预检报人话",
      (lambda: (probe(with_fb("off"), 2, "DEEPSEEK") is not None,
                probe(with_fb(["DEEPSEEK"]), 2, "DEEPSEEK") is not None,
                len(issues_for("off")) == 1, len(issues_for(["DEEPSEEK"])) == 1))() == (True,) * 4,
      f"{issues_for('off')} / {issues_for(['DEEPSEEK'])}")
check("4d 留空（DEEPSEEK: ）被报出（留空 = 不覆盖，会静默沿用座位层档位，不能不说）",
      len(issues_for({"DEEPSEEK": None})) == 1 and "off" in issues_for({"DEEPSEEK": None})[0],
      str(issues_for({"DEEPSEEK": None})))
check("4e 只在 .env 里有、不在 thinking_style 表里的档案（HAIKU）**不许**误报",
      issues_for({"HAIKU": "low"}) == [], str(issues_for({"HAIKU": "low"})))
check("4f 真实 config.yaml（这一段是注释、没启用）仍然零问题 —— 校验不许对生产配置误报",
      routing.problems(CFG, _SEATS) == [], str(routing.problems(CFG, _SEATS))[:120])

print("\n=== 五、进程内变异自证（把新代码改回旧写法 ⇒ 上面的判据立刻不成立）===")
#: `resolve_profile` 是按模块全局名调用 `fallback_thinking_for` 的，所以在进程里把它换掉，
#: 就等于"把这次的新代码改回去"——不需要动磁盘上的文件（文件级的变异自证另有一份，
#: 见 `tests/mutation_verify.py` 的 F1 / F2）。
_orig_helper = routing.fallback_thinking_for
_CFG_OFF = with_fb({"DEEPSEEK": "off"})
try:
    routing.fallback_thinking_for = lambda cfg, profile: None      # = 覆盖层被摘掉（旧写法）
    _killed = probe(_CFG_OFF, 5, "DEEPSEEK")
finally:
    routing.fallback_thinking_for = _orig_helper
check("5a 摘掉覆盖层后 3a 的判据立刻不成立（换到 DS 又变成 low）—— 证明 3a 真钉住了新代码",
      _killed != DS_OFF and _killed == SAVE_DS_LOW, f"摘掉后={_killed}（期望 != {DS_OFF}）")
try:
    routing.fallback_thinking_for = lambda cfg, profile: "low"     # = 默认也覆盖（偷偷改现状）
    _changed = probe(CFG_S2_OFF, 2, "DEEPSEEK")
finally:
    routing.fallback_thinking_for = _orig_helper
check("5b 让「不配时」也覆盖（返回 low）后 2c 立刻不成立 —— 证明「不写=现状」那条真的在守",
      _changed != DS_OFF and _changed == SAVE_DS_LOW, f"默认被覆盖后={_changed}（期望 != {DS_OFF}）")
check("5c 两次打桩都还原干净（helper 还是原来那个函数对象）",
      routing.fallback_thinking_for is _orig_helper)


async def _close_probe_clients() -> None:
    for cli in _CLIENTS:
        try:
            await cli.aclose()
        except Exception:                                 # noqa: BLE001
            pass


asyncio.run(_close_probe_clients())

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n_, _, d in bad:
    print(f"  [FAIL] {n_}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
