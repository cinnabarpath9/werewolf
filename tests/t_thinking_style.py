# coding=utf-8
"""回归：本轮三个修复
  1) 思考档位按**平台风格**发（Qwen 用 thinking_budget、豆包/智谱用 thinking.type）；
  2) 整手预算 > 单次上限（否则挂住一次就换不了家）；
  3) 服务端收到行动后广播 ask_done（修"票投了、面板还赖着"）。
"""
import asyncio
import io
import json
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import re
import sys

WW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WW)
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(WW)

import yaml                                              # noqa: E402

import run_game                                          # noqa: E402
from wolf import routing                                 # noqa: E402
from wolf.engine import Game                             # noqa: E402
from wolf.llm import levels_for                          # noqa: E402
from wolf.roles import make_board                        # noqa: E402
from _boards import make_board                    # noqa: E402,F401  退役板子的测试副本（产品已下架）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


run_game.load_env(os.path.join(WW, ".env"))
cfg = yaml.safe_load(io.open("config.yaml", encoding="utf-8"))
game_cfg = cfg.get("game") or {}
g = Game(board=make_board(game_cfg.get("board") or "eleven_wolfking"), agent_factory=None,
         seed=1, max_days=1)

print("=== 一、思考参数：逐座位（发言第1天 / 决策）===")
#: ── 作者 2026-10-11 深夜定的**最终阵容**（`config.yaml` 的 `llm.seats`，别再改）──
#:   座位 ↔ 档案/模型 ↔ 档位 ↔ 实测依据（26 道复杂题，报告在 `reports/*-20261011.md`）：
#:     1、7  ARK　`Doubao-Seed-2.1-lite`（豆包 2.1）   **low**（中档）
#:           豆包中档 81% / 53s，高档 73% / 313s（B 组：中档比高档高 8 分、快 6 倍）；低档 65% 不能关。
#:     2、8  DEEPSEEK（`DeepSeek-V4.1-Flash`）        **off**（有意关思考）
#:           固定卷三档：关思考 88% / 2,926 tok / 3s ＞ 中档 81% / 66,397 ＞ 高档 77% / 80,656。
#:           ⚠️ 座位层要写 **`thinking: "off"`（带引号）** —— 裸 `off` 会被 YAML 当布尔 false、
#:              写 `disabled` 会被当错名，**两者都会被静默丢掉、回落满档**（见下面那段与 E56）。
#:     3、9  ARK + `model: glm-5.3-flash`             **high**（跟随全局 `llm.thinking`）
#:           火山上 glm **关不掉**（发 disabled 会 400）；空档（什么都不发）会落到平台默认：
#:           62% / 201,294 tok / 192s（全场最差也最贵）⇒ 必须显式满档。
#:     4、10 HAIKU（`claude-haiku-5-5`）              **low**（中档）
#:           D 组：中/低档都 77%、高档 73% 且慢一倍 ⇒ 作者拍板中档。
#:     5、11 MIMO（`mimo-v2.6-flash`）                **low**（中档）
#:           中档 81%、决策题 100%；高档 85% 但要 5.5 倍时间（154s）且大包有交白卷风险。
#:     6     真人座位（生产 `game.human_seat: 6`）；本测试没传 human_seat ⇒ 它按 `llm.default`
#:           的 GLM 档案跑，档位 = 全局 `high`。
#:   ⚠️ 这与 2026-10-06 的旧口径（「五个模型都不关思考、全程满档」）**正好相反** ——
#:      那是**已被作者推翻**的旧口径，别再照它把某个座位改回 high。
#:      要改任何一座的档位，请连 `docs/待完善清单.md` 的 E56 与本文件一起改。
EXPECT = {1: "low", 2: "low", 3: "high", 4: "low", 5: "low", 6: "high",
          7: "low", 8: "low", 9: "high", 10: "low", 11: "low"}

#: ⚠️ 2026-10-11 深夜的一课（已登记在 `docs/待完善清单.md` E56）：**关思考很容易"假关"**。
#:   两个坑都实测踩到过，而且**预检都不报**（`t_routing_config` 当时 11/11 全绿）：
#:   ① 座位层写 `thinking: disabled` —— `disabled` 是 ARK 风格**发出去的参数**里的值、不是档位名；
#:      `wolf/llm.py::level_name()`（第 204~214 行）只把值小写化、**不把 `disabled` 当档位**；
#:      合法档位名只有 `THINKING_LEVELS` 的三个键（第 26~31 行）。于是 `normalize_thinking()`
#:      （第 186~189 行）把这一行当「不认识的档位」**丢掉** ⇒ 座位**静默回落到全局默认 high**。
#:   ② 座位层写**裸 `off`**（不加引号）—— YAML 把 on/off/yes/no 解析成布尔，拿到的是 `False`；
#:      而 `routing._merge_thinking()` 与 `routing.problems()` 当时都用 falsy 判断（`if not spec`），
#:      于是这一行被当空值**整段跳过**，照样静默回落 high（比 ① 更隐蔽：一个字都不报）。
#:   ⇒ 作者 2026-10-11 深夜的处理：config 改成 **`thinking: "off"`（带引号 = 字符串）**，
#:      代码侧同时修掉那两处 falsy 判断 + 让 `normalize_thinking` 认布尔 `False`＝off。
#:      现在 **1m** 正面钉住「2/8 发出的参数必须正好是 `{"thinking": {"type": "disabled"}}`」。


def _tier_of(params: dict, style: str) -> str:
    """把**真发出去的参数**反查回档位名（off / low / high）；认不出就返回 '?' 或 '空档'。

    ⚠️ 「空档」（一个思考参数都不发）**永远不是合法结果** —— 那等于落到平台默认，
      对火山 glm 这种「关不掉」的模型实测是 201,294 tok / 192 秒（见 1g）。
    """
    if not params:
        return "空档"
    for name, argv in levels_for(style).items():
        if params == argv:
            return name
    return "?"


probes: dict[int, dict] = {}
for seat in g.seats:
    if seat.is_human:
        continue
    prof = routing._profile_of(cfg, seat)
    spec = routing.resolve(cfg, seat)
    cli = routing.build_client(spec, label="probe")
    probes[seat.id] = {"prof": prof, "model": spec.model, "style": cli.thinking_style,
                       "sp": cli.extra_for("speech", 1), "de": cli.extra_for("vote", 5),
                       "aux": cli.extra_for("reflect", 1)}
    _p = probes[seat.id]
    print(f"  {seat.id:>2}号 {prof:<9} {_p['model']:<22} 风格={_p['style']:<7} "
          f"档位={_tier_of(_p['sp'], _p['style']):<8} 发言={_p['sp']}  决策={_p['de']}")

#: ⚠️ 2026-10-11 深夜：**千问已经不在座位上了**（1/7 换豆包 2.1-lite、4/10 换 Haiku），
#:    所以它不会出现在上面的逐座位表里。但 `game.fallback` 里还有 QWEN（换家真会用到），
#:    它的参数契约（1a/1b）**单独直连探一次**，别让它变成「永远跳过」的死断言。
_qwen_high = routing.build_client(
    routing.SeatLLM(base_url="http://probe.invalid/v1", api_key="probe", model="qwen3.8-flash",
                    thinking={"speech": {1: "high"}, "decision": {1: "high"}}),
    label="qwen-high", thinking_style="qwen")
_qwen_sp, _qwen_de = _qwen_high.extra_for("speech", 1), _qwen_high.extra_for("vote", 5)
check("1a 千问走 Qwen 那套参数名（enable_thinking，绝不是 reasoning_effort）"
      "—— 它已下桌、但仍是 game.fallback 里的一家",
      "enable_thinking" in _qwen_sp and _qwen_sp.get("enable_thinking") is True
      and "reasoning_effort" not in _qwen_sp, str(_qwen_sp))
#: ⚠️ 2026-10-10 晚定稿：上限**必须有，但给足** —— 完全不给上限时快米兔千问第一次调用就顶穿 300 秒
#:    （实测 300.0s 失败 → 换家）；旧值 4096 时 avg 92s / max 256s（说明 4096 一直在保命，只是偏小）。
#:    现在 16384：比旧值大 4 倍，同时留了边界。改小或删掉都要连着改这段说明。
check("1b 千问的思考上限＝16384（给足但要有边界）",
      int(_qwen_de.get("thinking_budget") or 0) == 16384, f"决策={_qwen_de}")

#: 1c 参数名按**档案**判（档位由 1d 逐座位判）—— 这两件事分开：风格错 = 换家那套也会错，
#:    档位错 = 某座位被偷偷降档/抬档。
_STYLE_WANT = {"ARK": "ark", "DEEPSEEK": "ark", "MIMO": "mimo", "HAIKU": "openai", "GLM": "glm"}
for _sid, _p in sorted(probes.items()):
    _want = _STYLE_WANT.get(_p["prof"])
    if _want:
        check(f"1c {_sid}号（{_p['prof']} / {_p['model']}）走 {_want} 那套思考参数名",
              _p["style"] == _want, f"实际 {_p['style']}；发言={_p['sp']}")

#: ⚠️ 1d 是**新口径的核心闸门**：不是「必须 high」，而是「必须与作者那张表一字不差」。
#:   谁把某座位的档位改了 —— 把「有意关思考」的座位偷偷开回来、或把中档抬成满档 → 红。
for _sid, _p in sorted(probes.items()):
    _got_sp, _got_de = _tier_of(_p["sp"], _p["style"]), _tier_of(_p["de"], _p["style"])
    _want = EXPECT.get(_sid)
    check(f"1d {_sid}号（{_p['prof']} / {_p['model']}）发出的档位＝{_want}"
          f"（作者 2026-10-11 深夜定的阵容；依据见本文件头那张表 / docs/待完善清单.md E56）",
          _got_sp == _want and _got_de == _want,
          f"实际 发言={_got_sp} 决策={_got_de}；参数 发言={_p['sp']} 决策={_p['de']}")

#: 1e MIMO：先钉**档位表**，再钉**座位 5/11 真按 low 发**。
#:   ⚠️ 2026-10-11 深夜重测（同一份 28,914 字的最大包，四档都正常出正文）：
#:     `disabled` → 思考 0 字 / 13.4s；`budget_tokens: 8192` → 7,912 字 / 69.7s；
#:     `reasoning_effort: low` → 12,204 字 / 82.8s；只开思考（= high）→ 26,562 字 / 166.9s。
#:   ⚠️ 旧断言写的是「5/11 没在座位层写 thinking ⇒ 两处都不该带 reasoning_effort」——
#:     那是**已作废的前提**：作者 2026-10-11 深夜把 5/11 定成**中档**（low 档本来就带
#:     `reasoning_effort: low`，见 wolf/llm.py:74），所以「带 low」是**对的**，不是漂移。
_mimo_lv = levels_for("mimo")
check("1e MIMO 四档表：low = thinking.type=enabled **且带** reasoning_effort=low；"
      "high = 只发 thinking.type；off = thinking.type=disabled"
      "（2026-10-11 重测：0 / 7,912 / 12,204 / 26,562 字）",
      _mimo_lv["low"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
      and _mimo_lv["high"] == {"thinking": {"type": "enabled"}}
      and (_mimo_lv["off"].get("thinking") or {}).get("type") == "disabled",
      f"low={_mimo_lv['low']} high={_mimo_lv['high']} off={_mimo_lv['off']}")
for _sid, _p in sorted(probes.items()):
    if _p["prof"] != "MIMO":
        continue
    check(f"1e {_sid}号（MiMo）真按**中档**发：thinking.type=enabled + reasoning_effort=low"
          f"（作者 2026-10-11 深夜拍板中档：26 题 81%、决策题 100%；高档 85% 但要 5.5 倍时间、"
          f"且大包有交白卷风险）",
          (_p["sp"].get("thinking") or {}).get("type") == "enabled"
          and _p["sp"].get("reasoning_effort") == "low"
          and (_p["de"].get("thinking") or {}).get("type") == "enabled"
          and _p["de"].get("reasoning_effort") == "low",
          f"发言 {_p['sp']} → 决策 {_p['de']}")

#: 1f DEEPSEEK（2/8）：风格是 **ark**（走火山方舟、认 thinking.type），不是 openai 的裸
#:    `reasoning_effort` —— config.yaml:77 写着 `DEEPSEEK: ark`，别哪天被「想当然」改回去。
#:    ⚠️ 它的档位是 **`off`**（有意关思考）⇒ 真发出去的是 `{thinking: {type: disabled}}`：见 1m。
for _sid, _p in sorted(probes.items()):
    if _p["prof"] != "DEEPSEEK":
        continue
    check(f"1f {_sid}号（DEEPSEEK / {_p['model']}）按 ark 风格发 thinking.type"
          f"（不是裸 reasoning_effort；它是 off 档 ⇒ 真发的是 thinking.type=disabled）",
          _tier_of(_p["sp"], _p["style"]) in ("off", "low", "high")
          and "thinking" in _p["sp"]
          and "enable_thinking" not in _p["sp"] and "thinking_budget" not in _p["sp"],
          f"风格={_p['style']}；发言={_p['sp']}")

#: 1g **单独钉住**座位 3/9（`profile: ARK` + `model: glm-5.3-flash` —— 真的是火山端点上的 glm）：
#:   不许 disabled、不许空档、必须显式满档。
#:   依据：火山上 glm 关不掉思考（发 `thinking.type: disabled` 会 400 InvalidParameter），
#:   而「什么都不发」= 落到平台默认 ⇒ 实测 62% / 201,294 tok / 192s（全场最差也最贵）。
#:   ⚠️ 判据要带 `prof == "ARK"`：6 号走 `llm.default` 的 GLM 档案（那个**不是**火山端点，
#:      风格是 glm、off 档是「什么都不发」），别把 6 号也算进这条。
_glm_seats = sorted(sid for sid, p in probes.items()
                    if "glm" in str(p["model"]).lower() and p["prof"] == "ARK")
check("1g 3/9 就是那两张走火山端点的 glm 牌（座位表没漂，config.yaml:125-126）",
      _glm_seats == [3, 9], str(_glm_seats))
for _sid in _glm_seats:
    _p = probes[_sid]
    check(f"1g {_sid}号（火山 glm-5.3-flash）**不许出现 disabled、也不许空档**，"
          f"必须显式满档 high（作者 2026-10-11 深夜：火山上 glm 只能开不能关）",
          bool(_p["sp"]) and bool(_p["de"])
          and (_p["sp"].get("thinking") or {}).get("type") == "enabled"
          and (_p["de"].get("thinking") or {}).get("type") == "enabled"
          and "disabled" not in str(_p["sp"]) and "disabled" not in str(_p["de"]),
          f"发言={_p['sp']} 决策={_p['de']}")

#: 1h 总闸：**每个座位的发言档与决策档必须一致**（配置里是按座位写一个档位，所以这两组同档；
#:    谁只把发言或只把决策偷偷降一档，这里就红）。
_bad_tier = {sid: (p["sp"], p["de"]) for sid, p in probes.items() if p["sp"] != p["de"]}
check("1h 每个座位的发言档与决策档**一致**（不许只偷偷降其中一组）",
      not _bad_tier, str(_bad_tier))


def _thinks(p: dict) -> bool:
    """这组参数是不是「真的在思考」（闸门用）。

    ⚠️ 2026-10-11 重测后**只放宽一格**：`reasoning_effort: low` 也算「开思考」。
      · mimo 的 low 档现在是 `thinking.type=enabled + reasoning_effort=low`
        （实测同一份最大包 12,204 字 / 82.8s —— 确实在想，只是比 high 的
         26,562 字 / 166.9s 少一档）；
      · HAIKU（走 openai 风格）同样只有 `reasoning_effort: low` 可说。
      它们都不是"关思考"，旧判据（必须 `enable_thinking: true` 或 `thinking.type=enabled`）
      会把这两条**正常**路径判红。
    ⚠️ 但**不许再往下放宽**：`off`（openai 是 `reasoning_effort: none`，
      ark/glm/mimo 是 `thinking.type: disabled`）和「空档」（一个思考参数都不发）
      必须继续判 False —— 这条闸门的全部价值就是「拦住有人偷偷把思考关掉/配成空档」。
    """
    if p.get("enable_thinking") is True:
        return True                     # qwen 的开法
    if (p.get("thinking") or {}).get("type") == "enabled":
        return True                     # ark / glm / mimo 的开法
    return str(p.get("reasoning_effort") or "").strip().lower() in ("low", "high")


_no_think = {sid: p["sp"] for sid, p in probes.items()
             if EXPECT.get(sid) not in ("off", "disabled") and not _thinks(p["sp"])}
check("1i 凡**不是**「有意关思考」的座位，参数都真的是「开思考」"
      "（off / 空档仍然拦；low 只算低一档、不算关）",
      not _no_think, str(_no_think))
#: 辅助组（复盘笔记 / 连通性自检 —— **不是座位上的行动**）走低档，别跟着满思考白等。
#: ⚠️ 这条从「只看最后一个座位」改成「**每个座位**都看」：座位级档位写法变了以后，
#:    逐个座位才对得上（以前只取循环结束后那个 cli，等于只查了一家）。
#: ⚠️ 判据是「**不许是满档**」而不是「必须正好 low」：座位层那一句档位会**同时**作用于
#:    speech / decision / aux（`_merge_thinking` 的粒度就是"组"），所以座位 2/8 的 au x
#:    跟着是 off（比 low 更省）—— 那也是"没满思考"，不该判红；真正要拦的是「把 aux 抬成 high」。
_aux_bad = {sid: p["aux"] for sid, p in probes.items()
            if _tier_of(p["aux"], p["style"]) not in ("off", "low")}
check("1j 非座位的活（reflect / 自检）**每个座位**都不是满档"
      "（火山系一般是 low、座位 2/8 因为座位层 off 而连 aux 一起是 off、"
      "Haiku 是 reasoning_effort=low）—— 拦的是「有人把 aux 抬成 high」",
      not _aux_bad, str(_aux_bad))

#: HAIKU（4/10 号）：`config.yaml` 的 `llm.thinking_style` 表和 `routing.thinking_style_for`
#: 的内置表里**都没有 HAIKU 这一项**，所以它走兜底分支返回 `openai` —— 这是**对的**，
#: 不是"漏配"：快米兔的 claude-haiku-5-5 是 OpenAI 兼容端点、认 `reasoning_effort`
#: （`config.yaml:131` 记着实测：`reasoning_effort=high` 有效，完成 token 881 → 1653）。
#: ⚠️ 座位 4/10 在**座位层**写的是 `thinking: low`（作者 2026-10-11 深夜拍板：Haiku 走中档 ——
#:    D 组实测中/低档都是 77%、高档只有 73% 且慢一倍），座位层优先级最高 ⇒ 覆盖全局的 high
#:    ⇒ 发言组与决策组都发 `reasoning_effort: low`。
#:    所以 1i / 2f 那两条闸门必须认「low 也是在思考」，否则会把这条正常路径判红。
_haiku_seats = sorted(sid for sid, p in probes.items() if p["prof"] == "HAIKU")
_haiku_detail = {sid: probes[sid]["sp"] for sid in _haiku_seats}
check("1k HAIKU 按 openai 风格发 reasoning_effort（表里没它、兜底 openai 是有意的）；"
      "4/10 号座位层 thinking: low ⇒ 发 reasoning_effort: low",
      routing.thinking_style_for(cfg, "HAIKU") == "openai"
      and _haiku_seats == [4, 10]
      and all(probes[sid]["sp"].get("reasoning_effort") == "low"
              and probes[sid]["de"].get("reasoning_effort") == "low"
              and "thinking" not in probes[sid]["sp"]
              and "enable_thinking" not in probes[sid]["sp"] for sid in _haiku_seats),
      f"风格={routing.thinking_style_for(cfg, 'HAIKU')} 发言={_haiku_detail}")

#: ⚠️ 座位层现在**确实**在写 `thinking: low / off`（作者 2026-10-11 深夜定的阵容），
#:    那是有意的；这条拦的是**手写思考参数**（`extra_body` 里的 `enable_thinking` /
#:    `thinking.type` / `reasoning_effort`）—— 那种写法会整张盖掉档位表（2026-10-07 实测：
#:    1/7/4/10 号因此一直满血思考）。想降档请写 `thinking: <档位名>`（`off / low / high`）。
check("1l 座位层不再手写思考参数（extra_body 里不许出现；要降档请写 `thinking: <档位名>`）",
      all(not (v or {}).get("extra_body") for v in (cfg["llm"].get("seats") or {}).values()),
      str({k: (v or {}).get("extra_body") for k, v in (cfg["llm"].get("seats") or {}).items()}))

#: 1m 正面钉死座位 2/8「有意关思考」：档位是 **`off`**，ARK 风格下**必须**发出
#:   `{"thinking": {"type": "disabled"}}`（且不许再夹带 `reasoning_effort`）。
#:   为什么这条要单独写（2026-10-11 深夜的坑）：座位 2/8 一度写的是 `thinking: disabled` ——
#:   `disabled` 是**发出去的参数值**、不是档位名；`level_name()`（wolf/llm.py:204-214）不认它，
#:   合法档位只有 off/low/high（wolf/llm.py:26-31），于是 `normalize_thinking()`
#:   （wolf/llm.py:186-189）把它当错名**丢掉**、该座位**静默回落到全局 high** ⇒ 以为关了、
#:   其实满血在想。现在 config 已改成 `off`（t_routing_config.py 3a 也随之恢复零问题）。
#:   以后谁把 `off` 写成别的（或让 level_name 不认 `off`），这条立刻红。
_ds_off = {"thinking": {"type": "disabled"}}
_ds_seats = sorted(sid for sid, p in probes.items() if p["prof"] == "DEEPSEEK")
_ds_now = {sid: probes[sid]["de"] for sid in _ds_seats}
#: 1m 座位 2/8（DEEPSEEK）的**发言与决策参数**必须正好等于「本座位档位 × 该档案风格」该发的那组。
#:   ⚠️ 2026-10-15：作者把 2/8 由 off（关）改成 low（中）——依据：34 道终版卷 中 76% > 高 74% > 关 62%
#:   （旧 26 道卷上关 88% 最优 ⇒ 两卷冲突），且真局里 DS 那次「同一份判断自相矛盾」（座位 8）正发生在关档下。
#:   本条**不再钉死某一档**，而是钉「真发出去的参数必须与 EXPECT 一致」⇒ 以后改档位只需改 EXPECT 一处。
check("1m 座位 2/8（DEEPSEEK）的发言与决策参数都等于「EXPECT 档位 × 档案风格」该发的那组",
      _ds_seats == [2, 8]
      and all(probes[sid]["de"] == dict(levels_for(probes[sid]["style"])[EXPECT[sid]]) and probes[sid]["sp"] == dict(levels_for(probes[sid]["style"])[EXPECT[sid]]) for sid in _ds_seats),
      "座位=%s 决策=%s" % (_ds_seats, {sid: probes[sid]["de"] for sid in _ds_seats}))

#: 1o 覆盖自查：EXPECT 必须**逐个**覆盖每个非真人座位（少写一个座位 = 那个座位没人守）。
check("1o EXPECT 逐个覆盖了每个座位（本测试里非真人的 10 个）",
      set(probes) <= set(EXPECT) and set(EXPECT) - set(probes) <= {6},
      f"座位={sorted(probes)} EXPECT={sorted(EXPECT)}")

check("1n levels_for 认不出风格时退回 openai",
      levels_for("nonsense") == levels_for("openai"),
      str(levels_for("nonsense")))

print("\n=== 二、整手预算 vs 单次上限（挂住一次也要有机会换家）===")
call_t = float(game_cfg.get("call_timeout", 0))
dec_t = float(game_cfg.get("decision_timeout", 0))
check("2a decision_timeout > call_timeout", dec_t > call_t, f"{call_t} → {dec_t}")
check("2b 差值够跑一次替补（≥60 秒）", dec_t - call_t >= 60, f"余量 {dec_t - call_t} 秒")

print("\n=== 二之二、替补（换家）也必须按**目标平台**的风格发档位（P2-1）===")
# 真缺陷：`SeatLLM` 没有 `thinking_style` 字段，`build_client` 的默认值又是 "openai" ——
# 替补路径（server.py / run_game.py 的 _build_fallbacks）没传这个参数，
# 于是不管换到哪家都发 `reasoning_effort`：
#   · 换到 QWEN → 它认 enable_thinking/thinking_budget，`reasoning_effort` 不生效，
#     而且 config.yaml 里明确记过「给千问发 reasoning_effort=high 会偶发 503」；
#   · 换到 ARK / GLM → 它们认 thinking.type，同样失效。
# 线上 `game.fallback` 是 [DEEPSEEK, MIMO, GLM, QWEN, ARK]（非空），这条路上真会走。

#: 每种风格「合法形态」的判据（跟 wolf/llm.py 的 THINKING_LEVELS_BY_STYLE 对齐）：
#: 判的是**参数名/形态属不属于这家**；「档位对不对」由 2l 逐对精确比对（那才是最强的那条）。
STYLE_PROBE = {
    #: qwen 只有 `enable_thinking`（低/高档再带 `thinking_budget`）；off 档是 `enable_thinking: False`。
    "qwen": lambda p: ("enable_thinking" in p and "reasoning_effort" not in p
                       and "thinking" not in p),
    #: ark（豆包 / dsv4.1 / glm5.3f 走火山）：只有 `thinking.type`（enabled/disabled）
    #:   ± `reasoning_effort`。**off 档就是 `thinking.type: disabled`** —— 座位 2/8 的档位是 off，
    #:   换家到 ARK / MIMO 时照样发 disabled：换家**不换档位**（`routing.resolve_profile` 的口径），
    #:   所以「disabled 也是合法的 ark 形态」，这是对的、不是误发。
    "ark": lambda p: ("thinking" in p
                      and str((p.get("thinking") or {}).get("type") or "") in ("enabled", "disabled")
                      and p.get("reasoning_effort") in (None, "low", "high")
                      and "enable_thinking" not in p and "thinking_budget" not in p),
    #: glm（52pay 中转那套）：off 档**什么都不发**（火山上 glm 不接受 disabled，发它会 400
    #:   InvalidParameter）⇒ **空 dict 是这一家的合法形态之一**，不能当成「空档事故」；
    #:   enabled 时必须带 `reasoning_effort`，而且**绝不许**出现 `disabled`。
    "glm": lambda p: (p == {} or ("thinking" in p and "disabled" not in str(p)
                                  and p.get("reasoning_effort") in ("low", "high")
                                  and "enable_thinking" not in p and "thinking_budget" not in p)),
    "openai": lambda p: ("reasoning_effort" in p and "enable_thinking" not in p
                         and "thinking_budget" not in p and "thinking" not in p),
    #: 小米 MiMo（2026-10-11 深夜**重测后更正**）：开思考**必须**走
    #:   `thinking.type=enabled`；`reasoning_effort` 只允许**缺席**（high 档：只开思考
    #:   26,562 字/166.9s）或 **"low"**（low 档：`thinking.type=enabled` +
    #:   `reasoning_effort=low`，12,204 字/82.8s）；off 档是 `thinking.type=disabled`。
    #: ⚠️ 旧判据写的是「绝不能带 reasoning_effort」—— 那是按**已被推翻**的旧结论写的
    #:   （旧记录：「小米没有低档、发 reasoning_effort 等于没控制」），于是 4/10 号
    #:   （座位层 `thinking: low`）换到 MIMO 这条**正常**路径被误判成"风格不对"。
    #: ⚠️ 判据仍然拦得住真问题：只有 `reasoning_effort` 而没有 `thinking.type`
    #:   （那才是"按 openai 误发"）、混进 `enable_thinking` / `thinking_budget`、以及"什么都不发"。
    "mimo": lambda p: ((p.get("thinking") or {}).get("type") in ("enabled", "disabled")
                       and p.get("reasoning_effort") in (None, "low")
                       and "enable_thinking" not in p
                       and "thinking_budget" not in p),
}


def _probe(spec, kind="speech", day=1) -> dict:
    """这个 spec 真发给上游的思考参数（**不传 thinking_style**，走 P2-1 之后的默认路径）。"""
    return routing.build_client(spec, label="probe").extra_for(kind, day)


def _level_at(days: dict, day: int) -> str:
    """「第几天 → 档位」表在第 day 天用哪一档（天数向上取，与 `LLMClient.extra_for` 同口径）。"""
    level = None
    for key in sorted(days or {}):
        if day >= key:
            level = days[key]
    if level is None and days:
        level = days[min(days)]
    return level or "?"


def _expect_params(style: str, tier: str) -> dict:
    """「平台风格 × 档位」**该**发出什么 —— 照 `LLMClient.extra_for` 的规则算。

    ⚠️ 曾经有一格特殊：`glm` 的 off 档在表里是**空 dict**（火山上 glm 关不掉思考 → 表里写「什么都不发」）。
       **2026-10-12 已修**：`extra_for` 原先写 `table.get(level) or table["low"]`，空 dict 是 falsy
       ⇒ glm 的 off 被兜成 low；现在「**档位存在就用它，哪怕空 dict**，只有档位名不在表里才兜 low」
       ⇒ 座位 2/8（off）换家到 GLM 时真的什么都不发，与表里注释一致。
       ⚠️ 这里**不允许**放宽成"随便什么 glm 形态都行"：`disabled` 会被 STYLE_PROBE["glm"] 拦下
         （它在火山上会 400），openai 形态（只有 reasoning_effort）也会被 2f 拦下。
    """
    table = levels_for(style)
    # ⚠️ 与 `extra_for` 保持同一口径（2026-10-12 同步修）：档位存在就用它（空 dict 也算存在）。
    return dict(table[tier] if tier in table else table["low"])


pairs: list[tuple[int, str, str, dict]] = []
spec_style_bad: list[str] = []
exact_bad: list[str] = []
for seat in g.seats:
    if seat.is_human:
        continue
    prof = routing._profile_of(cfg, seat)
    mine = routing.resolve(cfg, seat)
    want_mine = routing.thinking_style_for(cfg, prof)
    if mine.thinking_style != want_mine:
        spec_style_bad.append(f"{seat.id}号 主档 {prof}: "
                              f"{mine.thinking_style} != {want_mine}")
    for name, spec in routing.fallback_specs(cfg, seat, game_cfg.get("fallback")):
        want = routing.thinking_style_for(cfg, name)
        if spec.thinking_style != want:
            spec_style_bad.append(f"{seat.id}号 →{name}: "
                                  f"spec {spec.thinking_style} != {want}")
        cli = routing.build_client(spec, label="probe")
        tier_sp = _level_at(cli.thinking.get("speech") or {}, 1)
        tier_de = _level_at(cli.thinking.get("decision") or {}, 5)
        if cli.extra_for("speech", 1) != _expect_params(want, tier_sp) \
                or cli.extra_for("vote", 5) != _expect_params(want, tier_de):
            exact_bad.append(
                f"{seat.id}号→{name}（{want}，档位 {tier_sp}/{tier_de}）："
                f"发出 {cli.extra_for('speech', 1)} / {cli.extra_for('vote', 5)}，"
                f"应为 {_expect_params(want, tier_sp)} / {_expect_params(want, tier_de)}")
        pairs.append((seat.id, prof, name, _probe(spec)))

check("2c 每个座位 × 每个替补目标都真的算出来了一对（名单非空）",
      len(pairs) >= 13, f"{len(pairs)} 对："
      f"{sorted({(p, n) for _, p, n, _ in pairs})}")
check("2d 每个 spec 的 thinking_style == 那个平台的应有风格（座位 + 替补都算）",
      spec_style_bad == [], "；".join(spec_style_bad[:4]))

mis = []
for sid, prof, name, params in pairs:
    want = routing.thinking_style_for(cfg, name)
    if not STYLE_PROBE[want](params):
        mis.append((sid, prof, name, want, params))
    print(f"    {sid:>2}号 {prof:<9} → 替补{name:<9} want={want:<7} 参数={params}")
    check(f"2e {sid}号 → 替补{name}：参数与目标平台风格一致（{want}）",
          STYLE_PROBE[want](params), str(params))

check("2f 全部对齐（一个都没有按 openai 误发）", mis == [],
      f"错位 {len(mis)}/{len(pairs)} 对：{[(m[0], m[2], m[4]) for m in mis[:3]]}")
#: 2l 是这一节**最强**的一条：不只是"参数名像那家"，而是**逐对精确等于**
#:   「目标平台风格 × 本座位档位」该发出的那一组。座位 2/8 的档位是 off，
#:   所以它们换到任何一家都必须是那一家的 **off 形态**（ark/mimo 是 `disabled`、
#:   qwen 是 `enable_thinking: false`、glm 是什么都不发）—— 换家不换档位。
check("2l 每个「座位 × 替补」发出的参数 == 「目标平台风格 × 本座位档位」的精确翻译",
      exact_bad == [], "；".join(exact_bad[:3]))

# 逐风格点名（把审查要求的三类各钉一条，免得"全对齐"是靠判据写松了）
by_style: dict[str, list[dict]] = {}
for _sid, _prof, name, params in pairs:
    by_style.setdefault(routing.thinking_style_for(cfg, name), []).append(params)
check("2g 换到 QWEN 时发的是 Qwen 那套参数名（`enable_thinking` ± `thinking_budget`），"
      "绝不是 reasoning_effort",
      bool(by_style.get("qwen")) and all(STYLE_PROBE["qwen"](p) for p in by_style["qwen"])
      and len(by_style["qwen"]) >= 4,
      f"{len(by_style.get('qwen', []))} 对：{by_style.get('qwen', [])[:2]}")
check("2h 换到 ARK / GLM 时发的是 thinking.type",
      bool(by_style.get("ark")) and bool(by_style.get("glm"))
      and all(STYLE_PROBE["ark"](p) for p in by_style["ark"])
      and all(STYLE_PROBE["glm"](p) for p in by_style["glm"]),
      f"ark {len(by_style.get('ark', []))} 对 / glm {len(by_style.get('glm', []))} 对")
#: ⚠️ 2026-10-11 重测更正：mimo 的 low 档**带** `reasoning_effort=low`（实测生效，
#:    12,204 字/82.8s）—— 旧断言写「换过去**不带** reasoning_effort」是旧结论的产物，
#:    会把 4/10 号（座位层 `thinking: low`）换到 MIMO 这条**正常**路径判红。
#:    新口径：**必须**发 `thinking.type`（`enabled`，或座位档位为 off 时的 `disabled`）；
#:    `reasoning_effort` 只准缺席或 low。
#:    下面第一条 `all(...)` 是**故意重复** STYLE_PROBE 里的判据的：就算以后有人把
#:    `STYLE_PROBE["mimo"]` 改松了，「换到小米必须发 thinking.type」这一半也丢不掉。
check("2i 换到 MIMO（mimo 风格）时**发的是** thinking.type"
      "（low 档可以再带 reasoning_effort=low；座位 2/8 那样 off 档的就是 disabled —— "
      "但绝不是只发 reasoning_effort / 或空档）",
      bool(by_style.get("mimo"))
      and all((p.get("thinking") or {}).get("type") in ("enabled", "disabled")
              for p in by_style["mimo"])
      and all(STYLE_PROBE["mimo"](p) for p in by_style["mimo"]),
      f"{len(by_style.get('mimo', []))} 对：{by_style.get('mimo', [])[:2]}")
# ⚠️ 反向钉：DEEPSEEK 在**生产 config 里**是 ark 风格（走火山方舟，认 thinking.type），
#    不是 openai —— 这条注释写在 config.yaml:64，别哪天被"想当然"改回 reasoning_effort。
check("2j 生产 config 里 DEEPSEEK 的风格就是 ark（config.yaml:64 那条注释的口径）",
      routing.thinking_style_for(cfg, "DEEPSEEK") == "ark",
      routing.thinking_style_for(cfg, "DEEPSEEK"))
check("2k 显式传 thinking_style 仍然能覆盖 spec（老调用方没被弄坏）",
      routing.build_client(routing.resolve(cfg, g.seats[1]), label="p",
                           thinking_style="qwen").thinking_style == "qwen"
      and routing.build_client(routing.resolve(cfg, g.seats[1]), label="p").thinking_style
      == routing.resolve(cfg, g.seats[1]).thinking_style)


print("\n=== 三、ask_done：服务端收到行动后要广播 ===")
src = io.open("server.py", encoding="utf-8").read()
check("3a server.py 里有 ask_done 广播", '"ask_done"' in src)
check("3b 前端处理 ask_done（清掉面板）", "case 'ask_done'" in io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read())
# ⚠️ 2026-10-25 修：以前是全文件 `src.index('"ask_done"')` —— 只要文件里**别处**
#    再出现一次 "ask_done"（哪怕在另一个方法里），这条就会假红。现在只截出
#    `action` 分支（`elif kind == "action":` 到下一个 `elif kind == "sync":`），
#    在**这一段内部**比先后顺序：`set_result` 必须先于 `ask_done`。
_action_branch = src[src.index('elif kind == "action":'):].split('elif kind == "sync"')[0]
check("3c ask_done 在传入 future **之后**发（否则客户端还没收到你的行动就先清了）",
      0 < _action_branch.find('future.set_result(') < _action_branch.find('"ask_done"'),
      "顺序正确（2026-10-31 联机改成按座位 future）")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for n, _, d in bad:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
