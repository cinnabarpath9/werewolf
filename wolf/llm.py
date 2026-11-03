# coding=utf-8
"""OpenAI 兼容的大模型客户端：超时、重试、用量记账。

只做一件事：把一段 packet 发出去，拿回一个 JSON 对象。
任何异常都不往上抛到游戏流程里 —— 由调用方决定降级策略（见 agents.py / engine.py）。
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

try:                                    # 浏览器内（Pyodide）没有 httpx：
    import httpx                        # 那里只用得到本模块的 SYSTEM_PROMPT / extract_json，
except ImportError:                     # 真建 LLMClient 时才需要 httpx（会显式报错）
    httpx = None                        # type: ignore[assignment]


def _int0(value) -> int:
    """把平台给的数转成非负整数；缺字段/写坏了都当 0（记账不许因为脏数据崩）。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def cached_tokens_of(used: dict | None) -> int:
    """这次调用有多少**输入 token 命中了平台侧缓存**（平台没返回就是 0）。

    为什么要有它（2026-10-30）：一局的输入 token 是百万量级，静态前缀（规则书/打法书/
    概念库）能不能命中前缀缓存，直接决定账单。以前只记 `prompt_tokens`，于是
    「靠前缀缓存省钱」只是结构上的推测，**没有数据能证明它真的命中了**。

    各家字段名不一样，这里一并认：
      · OpenAI 兼容 / 火山方舟：`usage.prompt_tokens_details.cached_tokens`
      · DeepSeek 官方：`usage.prompt_cache_hit_tokens`（另有 `prompt_cache_miss_tokens`）
      · Anthropic 风格中转：`usage.cache_read_input_tokens`
      · 有的中转直接给 `usage.cached_tokens`
    ⚠️ 不认识的字段一律记 0（宁可缺数据，也不许编一个"命中率"出来）。
    """
    if not isinstance(used, dict):
        return 0
    found = [_int0(used.get(key)) for key in
             ("cached_tokens", "prompt_cache_hit_tokens", "cache_read_input_tokens")]
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = used.get(key)
        if isinstance(details, dict):
            found.append(_int0(details.get("cached_tokens")))
    value = max(found) if found else 0
    prompt = _int0(used.get("prompt_tokens"))
    # 有的中转会把 cached 报得比 prompt 还大（单位错/口径不同）—— 夹住，别让它污染命中率。
    return min(value, prompt) if prompt else value


#: 思考档位 → 真正发给模型的参数
#: 思考档位 → 具体参数。⚠️ **每家平台的参数名不一样**，所以按"风格"分组：
#:   openai（默认，DeepSeek / MiMo 这类）：reasoning_effort
#:   qwen（通义/百炼）：enable_thinking + thinking_budget（它不认 reasoning_effort）
#:   ark（豆包/火山）：thinking.type
#:   glm（智谱）：thinking.type（也接受 reasoning_effort）
#: 2026-10-07 踩过的坑：以前只发 `reasoning_effort`，而 Qwen/豆包座位又在座位层手写了
#: `enable_thinking` / `thinking.type` —— 座位层优先级最高，于是**档位表对这两家完全失效**：
#: 说话时也在满血思考。实测 1号/7号（Qwen）平均 65~69 秒、10号（豆包）89 秒。
THINKING_LEVELS_BY_STYLE: dict[str, dict[str, dict]] = {
    "openai": {
        "off": {"reasoning_effort": "none"},
        "low": {"reasoning_effort": "low"},
        "high": {"reasoning_effort": "high"},
    },
    "qwen": {
        "off": {"enable_thinking": False},
        #: ⚠️ 2026-10-10：**高档不再设 `thinking_budget`**。
        #:    Qwen 的 `thinking_budget` 是「思考 token **上限**」，写小了等于替模型设盖子 ——
        #:    而作者要的是"一直开最高思考强度"。真实对局的包约 1.5 万 token，
        #:    那道题的思考很容易超过 4096（小题量不出来，所以别看小题的实测）。
        #:    不传预算 = 交给平台默认；外层还有座位的 max_tokens 兜底。
        "low": {"enable_thinking": True, "thinking_budget": 512},
        #: ⚠️ 2026-10-10 晚回补上限：**完全不给上限**实测会让快米兔千问顶穿 300 秒
        #:    （第一次调用直接 300.0s 失败 → 换家 DeepSeek）。
        #:    带 4096 上限时的历史实测是 avg 92s / max 256s —— 说明那个上限一直在保命。
        #:    现在给 **16384**（原来的 4 倍）：既比"无限想"有边界，又远高于旧值。
        "high": {"enable_thinking": True, "thinking_budget": 16384},
    },
    "ark": {
        "off": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    },
    "glm": {
        #: ⚠️ 实测（2026-10-10）：火山上的 `glm-5.3-flash` **不接受** disabled ——
        #:    发 `thinking.type: disabled` 会 400（InvalidParameter: not supported by this model）。
        #:    所以 off 档**什么都不发**（关不掉就只能让它开着，至少别把请求打挂）。
        "off": {},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    },
    #: 小米 MiMo（api.xiaomimimo.com）：官方文档只说 `thinking.type` = enabled/disabled，
    #: **没有**档位/预算参数，而且 `mimo-v2.6-flash` 等**默认就开着思考**。
    #: ⚠️ 2026-10-10 实测：给它发 `reasoning_effort` 等于**什么都没控制**（思考照旧开着，
    #:    "发言低档"根本降不下来，白等时间）；发 `thinking.type=disabled` 才真的关掉
    #:    （思考链 36 字 → 0 字）。
    #: 另外官方明说：思考开启时**忽略 temperature / top_p**（强制 1.0 / 0.95）。
    "mimo": {
        "off": {"thinking": {"type": "disabled"}},
        #: ⚠️ 2026-10-11 深夜**重测后更正**：旧记录写的「小米没有低档：开就是开；发
        #:    `reasoning_effort` 等于什么都没控制」**是错的**（会让以后的人把 low 配成 high）。
        #:    同一份最大包（28,914 字，真局后期那种）实测**四档都生效、都正常出正文**：
        #:      `disabled`              → 思考      0 字 / 13.4s
        #:      `budget_tokens: 8192`   → 思考  7,912 字 / 69.7s   ← 想更省用这个
        #:      `reasoning_effort: low` → 思考 12,204 字 / 82.8s   ← **本表 low 档用这个**
        #:      只开思考（= 本表 high）  → 思考 26,562 字 / 166.9s
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}},
    },
}
#: 兼容旧名字（有些地方还在用 THINKING_LEVELS 做校验/展示）
#: 2026-11-02「公共体验实例」：访客自带任意 OpenAI 兼容渠道时，**不发任何思考参数** ——
#: 我们不知道他那家认哪套字段，发错就是 400/重试到超时。三档都映射成空 dict = 什么都不带。
THINKING_LEVELS_BY_STYLE["plain"] = {"off": {}, "low": {}, "high": {}}

THINKING_LEVELS: dict[str, dict] = THINKING_LEVELS_BY_STYLE["openai"]


def levels_for(style: str) -> dict[str, dict]:
    """按平台风格取档位参数表（认不出来就用 openai 那套）。"""
    return THINKING_LEVELS_BY_STYLE.get(str(style or "").strip().lower(),
                                        THINKING_LEVELS_BY_STYLE["openai"])

#: 哪些任务算「发言」（写作类，走低思考档），其余都算「决策」（走高思考档）。
#: ⚠️ 2026-10-07 扩全：遗言 / 竞选发言 / 狼队讨论也是"发言"—— 它们都要先说清立场，
#:    而"说什么"由前面那次决策调用定好了（见 engine.decide_for_speech）。
#:    以前这三类落在"决策"档，等于让发言层自己又高思考一遍，还容易被自己带跑。
#: ⚠️ 2026-10-07 自查修正：遗言 / 竞选发言**不在**这里 —— 它们的提示词是写死的，
#:    没有把「发言前决策」的内容拼进去（`_speech_hint` 只服务轮次发言）。
#:    把它们降到发言档等于"降了档却没有决策可照着说"，白花一次高思考还丢了推理。
#: ⚠️ 2026-10-08 **用户拍板把 wolf_talk 升回决策档**（原来是发言档）：夜聊的职能是
#:    协商与分工（谁跳、谁钩、票投哪），而"降档的理由"（前面没有决策可照着说）在
#:    `decision_first: true` 之下已不成立 —— 每头狼讨论前都刚做过一次 high 的私下决策。
#:    代价是真实的：夜聊是**串行**的（4 头狼挨个说），每头多思考 20~40 秒 ≈ 每晚多 1.5~2.5 分钟。
#:    要退回低档只需把 "wolf_talk" 加回这个元组。
#: 「发言组」＝**座位上的单问发言**（PK 轮、模型没在决策里给正文时的退回路径）。
#: ⚠️ 2026-10-10：`reflect`（复盘笔记）**不再算发言组** —— 它不是座位上的行动，见 AUX_TASKS。
SPEECH_TASKS = ("speech",)

#: 「辅助组」＝**不是座位行动**的模型调用（总结/复盘/自检这类"干活的活"）。
#: 作者口径：这类"没事儿就低档思考，他就干个总结的活"。座位上的行动一律满档。
AUX_TASKS = ("reflect",)

#: 决策类任务（每个行动前各一次，全部最高思考）——列在这里只为文档与测试引用
DECISION_TASKS = ("analyze", "speech_plan", "wolf_intent", "seer_check", "guard",
                  "witch", "hunter_shoot", "vote", "sheriff_run", "badge",
                  # 狼王出局时"带走谁"（2026-10-10 新板子）：座位上的行动 → 满档
                  "wolf_king_take")

#: 默认策略：按「任务类型 + 第几天」决定思考档位（天数向上取，第 5 天用第 3 天那档）。
#: 为什么这么分（实测见 tools/bench_thinking.py）：
#:   - 发言是"把已经拿到的东西说人话"，关思考 8s 就够，开高思考只会更啰嗦
#:   - 决策（投票/验人/用药/刀人/开枪）才是 72% → 100% 那 30 个百分点所在
#:   - 第 3 天起台账涨到 2~3k tokens、矛盾集中爆发，这时候才值得上高思考
#: ⚠️ 2026-10-10 作者口径：**全程满思考**。兜底也必须是满的 ——
#:    以前这里是 {speech: off/low, decision: low/high}，配置一漏写就悄悄降档，
#:    跟"一直开最高思考强度"的目标正好相反（而且不会报错，很难发现）。
DEFAULT_THINKING: dict[str, dict] = {
    "speech": {1: "high"},        # 座位上的单问发言：满
    "decision": {1: "high"},      # 座位上的决策 / 决策+发言合并：满
    "aux": {1: "low"},            # 非座位的总结/复盘/自检：低档就够
}


def normalize_thinking(table, fill_defaults: bool = True) -> tuple[dict, list[str]]:
    """把配置里的思考档位表规范化，并返回（规范化结果, 人话问题清单）。

    支持两种写法：
      {1: "off", 3: "low"}            # 按天升级
      "off" / "low" / "high"          # 一个字符串 = 所有任务、所有天都用这一档

    设计原则：**配置写错时不能静默"什么都不发"** —— 那等于落到平台默认，
    对推理模型可能是单次 150 秒（比高思考还慢）。这里一律给出明确档位 + 一条问题。

    fill_defaults=False 时不补齐缺失的组/天数（给"多层合并"用，免得浅层的默认值
    把深层没写的东西也一起填上）。
    """
    issues: list[str] = []
    valid = "/".join(THINKING_LEVELS)

    if table is None:
        table = {}
    #: ⚠️ 2026-10-11 深夜修：YAML 会把**裸 `off`** 解析成布尔 `False`（on/off/yes/no 都是 YAML 1.1 的
    #:    布尔字面量），而这里原先只认 str / dict ⇒ 座位层写 `thinking: off` 会被整段丢掉、
    #:    **静默回落到全局默认 high**（以为关了其实在满血思考，连告警都没有 —— 比写错档位名更危险）。
    #:    现在：`False` 视为 off（无歧义）；`True` **不猜**，报一条人话问题后按默认处理。
    if isinstance(table, bool):
        if table is False:
            table = "off"
        else:
            issues.append("思考档位写成了布尔 true（YAML 把 on/yes 也当布尔）—— 档位只能是 "
                          "off/low/high，已按默认处理；想表达「关」就用 off 并加引号")
            table = {}
    if isinstance(table, str):
        lv = level_name(table)
        if lv in THINKING_LEVELS:
            table = {"speech": {1: lv}, "decision": {1: lv}, "aux": {1: lv}}
        else:
            issues.append(f"llm.thinking 写成了 {table!r}，不是合法档位（只能是 {valid}），已按默认处理")
            table = {}
    if not isinstance(table, dict):
        issues.append(f"llm.thinking 应该写成 {{天数: 档位}} 或一个档位名，"
                      f"实际是 {type(table).__name__}，已按默认处理")
        table = {}

    out: dict[str, dict] = {}
    # ⚠️ 三组都要认：`aux` 是"非座位的活"（复盘笔记 / 连通性自检），
    #    2026-10-10 作者口径 = 低档；漏认这一组会让配置里写的 aux 变成死配置（只能靠代码兜底）。
    for group in ("speech", "decision", "aux"):
        spec = table.get(group)
        if spec is None:
            if not fill_defaults:
                continue                      # 这一层没写这组 → 交给合并逻辑（不要去填默认值）
            spec = DEFAULT_THINKING[group]
        if isinstance(spec, str):
            spec = {1: spec}
        if not isinstance(spec, dict) or not spec:
            issues.append(f"llm.thinking.{group} 应该写成 {{天数: 档位}}，已按默认处理")
            if not fill_defaults:
                continue
            spec = DEFAULT_THINKING[group]
        days: dict[int, str] = {}
        for raw_day, raw_level in spec.items():
            try:
                day = int(raw_day)
            except (TypeError, ValueError):
                issues.append(f"llm.thinking.{group} 的天数键 {raw_day!r} 不是数字，已忽略这一行")
                continue
            if day < 1:
                issues.append(f"llm.thinking.{group} 的天数键 {raw_day!r} 不合理（要从第 1 天起），"
                              f"已忽略这一行")
                continue
            lv = level_name(raw_level)
            if lv not in THINKING_LEVELS:
                issues.append(f"llm.thinking.{group} 第 {day} 天的档位 {raw_level!r} 不认识"
                              f"（只能是 {valid}），已忽略这一行")
                continue
            days[day] = lv
        if not days:
            if not fill_defaults:
                continue
            days = dict(DEFAULT_THINKING[group])
        if fill_defaults and min(days) > 1:
            # 只写了第 3 天起？那把最早那档补到第 1 天，别让前几天"什么都不发"
            days[1] = days[min(days)]
            issues.append(f"llm.thinking.{group} 没有第 1 天的档位，已把第 {min(days)} 天的"
                          f"{days[min(days)]!r} 补到第 1 天")
        out[group] = days
    return out, issues


def level_name(value) -> str:
    """把配置里写的档位统一成 'off' / 'low' / 'high'。

    注意：**YAML 会把 off/on/yes/no 解析成布尔值**，所以 config.yaml 里写
    `{1: off}` 拿到的其实是 False。这里兜一下，不然"关思考"会静默变成"默认开思考"。
    """
    if value is False:
        return "off"
    if value is True:
        return "high"
    return str(value if value is not None else "").strip().lower()

SYSTEM_PROMPT = """你在参与一局中文狼人杀。

- **本局的具体规则以信息包里的【本局规则】/【本局规则公示】为准**（每个板子的规则不一样：
  有没有守卫、有没有警长、遗言怎么给，都可能不同）。这里不重复规则细节，免得两处说法打架。
- 你只能看到信息包里给你的内容。不要假设自己知道别人不知道的事，也不要提及信息包本身。
- 发言要像一个真人玩家：有明确观点、有怀疑对象、有理由；口语化、**简洁但不空** ——
  至少两三句（先给结论，再给 1~2 条理由），别只甩一句结论。
- **时间线纪律（2026-11-03 真局复盘加的）**：汇报夜间信息必须用**过去时**——
  「昨晚我验的是 X 号」「昨晚被刀的是 X 号」；**不能用今天才公开的信息**
  （谁上警、前置/后置位、今天的发言、今天的票型）去解释你**昨晚**为什么那样做。
  那是时间线穿帮，好人一眼就抓（实测：7 号、10 号就是这么抓到悍跳狼的）。
- **言行一致（身份口径）**：说了「先不跳身份 / 不报身份」就**不要在发言里报出自己的身份**，
  `claim` 字段也要留空（写「无」）；要么报身份、要么不报，二选一 —— 自相矛盾会被好人当破绽。
- 不要用「X号。」开头复述自己的座位号（那是填表，不是发言）。
  不要旁白、不要心理描写、不要 markdown 标题、不要复述规则。
- 严格按【输出格式】输出**一个完整** JSON 对象，**必须包含任务要求的全部字段**，缺字段视为失败。"""


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status            # HTTP 状态码（有的话），重试策略要用


def redact(text: str) -> str:
    """把疑似密钥从文本里抹掉。

    为什么需要：错误信息会一路走下去 ——
    `LLMError` 的文字 → 写进 transcript → **存进 games/*.json** → 还作为"私有经历"
    回灌给这个座位的下一次调用。如果某家网关在错误体里回显 key（现在这家实测不会），
    key 就会落盘并且喂给模型。这里做一层兜底，换平台也不用担心。
    """
    # ⚠️ 只认 `Bearer xxx` 和 `sk-xxx` 是不够的（审计实测）：
    #   · 智谱/GLM 的 key 是 `32位hex.16位字母数字`，不含 sk- 前缀
    #   · `Bearer` 后面的 token 里只要出现 base64 的 + / =，老正则只隐去一半甚至完全不匹配
    # 所以先做一条"白名单式"的兜底：凡是 key/token/authorization 这类字段后面的值，一律抹掉；
    # 再叠上原有的两条（它们能处理"没有字段名、孤零零一个 key"的情况）。
    # ⚠️ Bearer 那条必须排在最前面：`Authorization: Bearer abc+def/ghi==` 里
    # `authorization` 后面第一个值就是 "Bearer" 本身，白名单先跑会把它吃掉，
    # Bearer 规则便再也匹配不到真 token，token 就原样漏出去了。
    text = re.sub(r"(?i)\b(bearer)\s+[^\s\"',;}]{6,}", r"\1 <已隐去>", str(text or ""))
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|authorization|x-api-key)"
                  r"[\"'\s:=]{1,6})[^\s\"',;}]{6,}", r"\1<已隐去>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9._\-]{6,}", "sk-<已隐去>", text)
    # 长随机串（20 位以上、字母数字混着 base64 的 +/=）—— 典型的裸 key
    text = re.sub(r"\b(?=[A-Za-z0-9+/=._\-]{20,}\b)(?=[^\s]*[0-9])(?=[^\s]*[A-Za-z])"
                  r"[A-Za-z0-9+/=._\-]{20,}", "<已隐去>", text)
    return text


def _match_end(cleaned: str, start: int) -> int | None:
    """从 cleaned[start] 的 '{' 出发，找配对的 '}'，返回它的下标 +1；找不到返回 None。"""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _first_json_object(cleaned: str) -> dict | None:
    """找到第一段**能解析成功**的 JSON 对象。

    不能只看第一个 `{`：模型常先写一句"我的思路 {先看3号} 如下："，
    那种花括号也配平，但解析会失败 —— 要跳过它继续往后找。
    """
    pos = 0
    while True:
        start = cleaned.find("{", pos)
        if start < 0:
            return None
        end = _match_end(cleaned, start)
        if end is not None:
            try:
                return json.loads(cleaned[start:end])
            except Exception:                  # noqa: BLE001 —— 这个 { 不是 JSON 起点，继续找
                pass
        pos = start + 1


#: 整串替换就够了、不会误伤的字符（全角 → 半角）。
#: ⚠️ 故意**不含** `'`：单引号要靠下面的 `_single_to_double()` 按"是不是定界符"判断，
#: 直接全局替换会把字符串内部的单引号一起改坏。
_FULLWIDTH = {
    "｛": "{", "｝": "}", "［": "[", "］": "]", "【": "[", "】": "]",
    "：": ":", "，": ",", "　": " ",
    "“": '"', "”": '"', "＂": '"', "‘": "'", "’": "'", "＇": "'",
}

#: Python 字面量 → JSON 字面量（只在"作为独立的值"出现时替换，不碰字符串内部）。
#: ⚠️ 必须带左右边界：`True` 要换，`TrueName` 不能碰；`None` 同理。
_PY_LITERAL = re.compile(r"(?<![A-Za-z0-9_])(True|False|None)(?![A-Za-z0-9_])")
_PY_LITERAL_MAP = {"True": "true", "False": "false", "None": "null"}

#: 对象/数组末尾的多余逗号：`{"a": 1,}`（空白里含换行，所以用 \s）
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _sig(text: str, pos: int, step: int) -> str:
    """取 pos 前面(step=-1)/后面(step=+1)第一个非空白字符；没有就返回空串。"""
    i = pos + step
    while 0 <= i < len(text):
        if not text[i].isspace():
            return text[i]
        i += step
    return ""


def _single_to_double(text: str) -> str:
    """把**当定界符用**的单引号换成双引号，字符串内部的单引号原样保留。

    模型有时整段用 Python 风格的单引号包键/值：`{'save': false}`。
    直接 `text.replace("'", '"')` 会把 `"他说'你好'"` 里的单引号也改坏，
    所以这里必须先判断每个成对的单引号"是不是定界符"：

      · 键：`{'save': ...}` —— 开头前面是 `{` 或 `,`，结尾后面紧跟 `:`
      · 值：`{'k': 'v'}` —— 开头前面是 `:` 或 `,`，结尾后面是 `,` `}` `]` 或串尾
      · 其余（比如 `"他说'你好'"` 里的那对）**一律不动**

    ⚠️ 判定要**跳过空白**（`{'a': 1, 'b': 2}` 里 `'b'` 前面隔着一个空格），
    否则只会修好紧贴着 `,` 的第一个键，后面的键原样留着 → 照样解析失败。

    只在"原样解析已经失败"之后才会走到这里，所以可以放心按 Python 字面量的读法
    处理转义：单引号包起来的字符串里 `\\'` 就是单引号本身、`\\"` 就是双引号本身，
    而 JSON 的 `\\` 和 `\\n` 要原样留着。
    """
    spans: list[tuple[int, int]] = []          # 每个单引号串的 (开头下标, 结尾下标)
    i, n = 0, len(text)
    while i < n:
        if text[i] != "'":
            i += 1
            continue
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2                          # 跳过被转义的字符
                continue
            if text[j] == "'":
                break
            j += 1
        if j >= n:
            break                               # 没配对的单引号：剩下的都不动
        spans.append((i, j))
        i = j + 1

    out = text
    for start, end in reversed(spans):          # 从后往前改，前面的下标才不会失效
        before = _sig(out, start, -1)
        after = _sig(out, end, +1)
        is_key = before in "{," and after == ":"
        is_value = before in ":," and (after in ",}]" or after == "")
        if not (is_key or is_value):
            continue                            # 疑似内容里的单引号 —— 保持原样
        body: list[str] = []
        k = start + 1
        while k < end:
            if out[k] == "\\" and k + 1 < end and out[k + 1] in ("'", '"'):
                body.append(out[k + 1])         # Python 转义 → 字符本身
                k += 2
                continue
            body.append(out[k])
            k += 1
        out = out[:start] + '"' + "".join(body) + '"' + out[end + 1:]
    return out


#: 裸的「N号」（模型很爱这么写：`{"vote": 2号}` —— 值没加引号 → JSON 非法）
_BARE_SEAT = re.compile(r"\d{1,3}\s*号")


def _quote_bare_seats(text: str) -> str:
    """把**不在字符串里的**裸 `N号` 加上引号：`{"vote": 2号}` → `{"vote": "2号"}`。

    ⚠️ 只做**语法**修复，不改语义（下游 `coerce_seat` 本来就用正则从 "2号" 里抠数字）。
    ⚠️ 必须**跳过字符串内部** —— 否则公开发言里的「先出2号」会被改成「先出"2号"」，
       那段是要进台账、给所有人看的原文。
    ⚠️ 只在"值该出现的位置"动手：前一个非空字符是 : , [ ，后一个非空字符是 , } ] 或行尾。

    实测来源（2026-10-10）：11号（小米 mimo）投警徽票时回了
    `{"vote": 2号, "reason": "…"}` → 解析失败 → 自动换家到 DeepSeek 才答上。
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        m = _BARE_SEAT.match(text, i)
        if m:
            before = next((c for c in reversed(out) if not c.isspace()), "")
            j = m.end()
            while j < n and text[j].isspace():
                j += 1
            after = text[j] if j < n else ""
            if before in ":,[" and (after in ",}]" or after == ""):
                out.append('"' + m.group(0).strip() + '"')
                i = m.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _gentle_repair_steps(text: str) -> list[str]:
    """把"温和修复"拆成**四步累积结果**（每步只多做一件事），让调用方一步步试解析。

    顺序 = 改动从小到大：
      1. 去掉对象/数组末尾多余的逗号（`{"save": false,}`）
      2. 当定界符用的单引号 → 双引号（`{'save': false}`）
      3. Python 字面量 True/False/None → true/false/null
      4. 裸的 `N号` 值加引号（`{"vote": 2号}` → `{"vote": "2号"}`）——
         不碰字符串内部，只认"值该出现的位置"（排在下一句前面，见那里的说明）
      5. 全角括号/冒号/逗号/引号 → 半角（`｛"save"：false｝`）——
         这一步会改掉中文标点，所以放在最后

    ⚠️ 2026-10-06 修：以前是把四种替换**一次叠满**再解析，结果互相拆台 ——
        {"speech": "3号，我信他“不是狼”，先不动", "claim": "无",}
    只去尾逗号本来能解析；一旦叠上"全角→半角"，发言正文里的中文标点会被静默改成英文
    （这段是要进台账当公开发言的），而单引号替换遇到正文里带 ASCII 双引号时还会产出
    非法 JSON —— 一份**本来救得回来**的回复整份作废 → 重发 5 次（每次最高思考档）→
    还修不好就暂停整局。现在"哪一步能解析就用哪一步"，后面的步骤不会执行。

    依然**不做**"猜字段名/补字段/正则抠值"这类会改变语义的动作：
    宁可降级成随机行动，也不要返回一个猜出来的对象（女巫用药猜错比随机还糟）。
    """
    steps: list[str] = []
    cur = text
    for fn in (
        lambda s: _TRAILING_COMMA.sub(r"\1", s),
        _single_to_double,
        lambda s: _PY_LITERAL.sub(lambda m: _PY_LITERAL_MAP[m.group(1)], s),
        #: 4. 裸的 `N号` 值加引号（纯语法修复；实测小米就这么写过）。
        #:    ⚠️ 必须排在"全角→半角"**前面**：那一步会改掉中文标点（，→,），
        #:    而中文标点是要进台账当公开发言的原文 —— 只需要加引号的回复不该被它牵连。
        _quote_bare_seats,
        lambda s: ("".join(_FULLWIDTH.get(ch, ch) for ch in s)
                   if any(ch in s for ch in _FULLWIDTH) else s),
    ):
        cur = fn(cur)
        steps.append(cur)
    return steps


def _extract_once(candidates: list[str]) -> dict | None:
    """在一组候选文本里找第一个能解析的 JSON 对象；找不到返回 None。"""
    for cleaned in candidates:
        found = _first_json_object(cleaned)
        if found is not None:
            return found
    return None


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。容忍 ```json 代码块和前后废话。

    两轮：
      ① 先按现在的方式解析（能解析的**一个字节都不动**，好例子不会退化）；
      ② 只有①失败，才走一层温和修复（全角字符 / Python 字面量 / 尾逗号 / 单引号）重试。
    两轮都失败才抛 LLMError（调用方据此降级）。

    为什么要②：模型偶尔把 JSON 写成 Python 字面量（`True`/`None`）、单引号、
    带尾逗号、混进全角符号，这些都只是"写法不标准"，语义完全清楚。
    以前整次作废 → 引擎降级成**随机行动**（女巫最容易踩：`{"save": True, "poison": None}`
    被丢掉等于"这瓶药白瞎了"）。现在能救回来就救。
    """
    if not text:
        raise LLMError("empty response")
    raw = text.strip()
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        # 代码块内容优先，**但代码块里没有 JSON 时要回退到全文**：
        # 实测过「JSON 在前、代码块在后」被误判成"完全没有 JSON"，整个回合作废。
        candidates.append(fence.group(1).strip())
    candidates.append(raw)

    found = _extract_once(candidates)
    if found is not None:
        return found

    # ①失败 → ②**逐步**修复：每做一步就试解析，取第一个能解析的结果
    # （不做满全套替换 —— 见 _gentle_repair_steps 的注释：叠满会把好文本改坏）
    steps = [_gentle_repair_steps(c) for c in candidates]
    # ⚠️ 以前这里写死 `range(4)` —— 而 `_gentle_repair_steps` 后来加到 5 步，
    #    第 5 步（裸 `N号` 加引号）**一次都没被跑过**：修了等于没修（2026-10-10 实测踩到）。
    #    现在按实际步数走，以后再加修复步骤不会再被这行静默吃掉。
    _depths = max((len(s) for s in steps), default=0)
    for depth in range(_depths):
        attempt = [s[depth] if depth < len(s) else c for s, c in zip(steps, candidates)]
        found = _extract_once(attempt)
        if found is not None:
            return found

    # 原文（没修过的）用来判错因：这样报错信息跟以前一样，别把修过的文本塞进报错里
    raw = candidates[-1]
    if "{" not in raw:
        raise LLMError(f"no json object in response: {text[:160]!r}")
    raise LLMError(f"unbalanced json in response: {text[:160]!r}")


class LLMClient:
    """一个座位一个客户端（可以各自不同的 base_url / key / model）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 1.0, max_tokens: int = 4000,
                 #: ⚠️ 2026-10-30：默认值与 `routing.resolve()` 的兜底对齐（200 秒）。
                 #: 以前这里是 90、routing 是 200 —— 绕过 routing 直接构造时口径就飘了。
                 timeout: float = 200.0, max_retries: int = 4,
                 extra_body: dict | None = None, thinking: dict | None = None,
                 thinking_style: str = "openai",
                 #: 用**流式**请求（2026-10-10 晚加）：为了"上游卡住"能早点发现。
                 #: 非流式下，上游接了请求却不吐字节时只能干等到总超时（实测 300 秒），
                 #: 那时整手预算已经烧光、没机会重试（快米兔客服口径：抖动时第二次就通）。
                 #: 流式下思考链是连续吐的，所以"静默 N 秒"＝真卡住。
                 stream: bool = True,
                 #: 流式下"连续多少秒没有字节"就判定卡住（默认 **50 秒** —— 作者 2026-10-10 定：
                 #: 思考链是连续吐的，50 秒不吐字基本就是上游抖了）。
                 idle_timeout: float = 50.0,
                 #: 客户端**自动重试之间的退避基数**（秒）。第 n 次重试等 `min(base*n, base*4)`：
                 #: base=1.5（默认）→ 1.5 / 3 / 4.5 / 6 秒，**跟以前一模一样**。
                 #: 为什么做成可配（2026-10-11）：这个等待以前写死在代码里，于是
                 #: `t_stall_ws.py`（要在两局里把"客户端重试 → 引擎重试 → 弹窗"整条链走完）
                 #: 光 sleep 就要 ~150 秒 —— 而它断言的是**次数**（从配置算），不是秒数。
                 #: 引擎那边早就有同样的口子（`game.retry_backoff`，测试用 0.3/0.8 跑），
                 #: 这里补上同款：测试配置写小值即可，**生产不写就是原值**。
                 retry_sleep: float = 1.5,
                 label: str = "") -> None:
        if not api_key:
            raise LLMError(f"{label or model}: 没有 API Key")
        self.base_url = base_url.rstrip("/")
        try:
            self.host = urlparse(self.base_url).netloc or self.base_url
        except ValueError as exc:
            # 复制粘贴时混进全角冒号/中文字符就会走到这里；给一句人话，别抛裸 ValueError
            raise LLMError(f"{label or model}: Base URL 有问题（{exc}）。"
                           f"常见原因是复制时混进了全角冒号或空格：{base_url!r}") from exc
        self.model = model
        self.key = f"{self.host}/{model}"       # 经验存档用：同平台同模型 = 同一个"脑子"
        self.label = label or model
        self.temperature = temperature
        #: 这个座位的客户端超时（秒）。流式请求会用它构造 per-request 超时：
        #: 总时长仍按它，但「两段数据之间」的等待收到 idle_timeout（见 _post_stream）。
        self.timeout = float(timeout or 0)
        self.max_tokens = max_tokens
        self.max_retries = max(0, int(max_retries))
        self.extra_body = dict(extra_body or {})
        #: 思考强度策略（已规范化）：{"speech": {1: "off", 3: "low"}, "decision": {...}}
        #: 规范化会把"写错的档位名 / 非数字天数键 / 漏写第 1 天"修好并记进 thinking_issues，
        #: 运行时绝不因为配置写错而静默"一个参数都不发"（那等于落到平台默认，可能慢 20 倍）
        self.thinking, self.thinking_issues = normalize_thinking(thinking)
        #: 这个平台认哪种思考参数（qwen / ark / glm / openai）—— 见 THINKING_LEVELS_BY_STYLE
        self.thinking_style = str(thinking_style or "openai").strip().lower() or "openai"
        self.stream = bool(stream)
        self.idle_timeout = float(idle_timeout or 0)
        #: 重试退避基数（秒）：`_retry_wait(attempt)` 用它算"这一次该等多久"。
        #: 默认 1.5 ⇒ 1.5/3/4.5/6 秒，与 2026-10-07 定的口径一致（见 __init__ 的说明）。
        self.retry_sleep = max(0.0, float(retry_sleep if retry_sleep is not None else 1.5))
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "cached_tokens": 0, "errors": 0, "seconds": 0.0}
        #: 最近一次调用的原始材料，供复盘使用（推理链、原始回复、用量）
        self.last_call: dict = {}
        if httpx is None:
            raise LLMError("这个环境里没有 httpx（浏览器版不走 LLMClient，请用 browser_glue）")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=20.0),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def extra_for(self, task_kind: str, day: int) -> dict:
        """按「任务类型 + 第几天」算出这次调用该带什么思考档位。

        天数向上取：表里写 {1: low, 3: high}，那第 1~2 天是 low、第 3 天及以后是 high。
        表已经过 normalize_thinking 规范化，所以这里**一定会落到一个明确档位**。
        """
        if task_kind in AUX_TASKS:
            group = "aux"
        elif task_kind in SPEECH_TASKS:
            group = "speech"
        else:
            group = "decision"
        days = self.thinking.get(group) or DEFAULT_THINKING.get(group) or {}
        level = None
        for key in sorted(days):
            if day >= key:
                level = days[key]
        if level is None:
            level = days[min(days)] if days else DEFAULT_THINKING[group][1]
        # ⚠️ 按**平台风格**取参数：Qwen 认 enable_thinking/thinking_budget，豆包与智谱认
        #    thinking.type，其余（DeepSeek/MiMo）认 reasoning_effort。以前只有开放一套，
        #    导致 Qwen/豆包座位的档位实际没生效（见 THINKING_LEVELS_BY_STYLE 的注释）。
        table = levels_for(self.thinking_style)
        # ⚠️ 2026-10-12 修：原先写的是 `table.get(level) or table["low"]` —— 空 dict 是 falsy，
        #    于是 **GLM 的 off 档（= 什么都不发，空 dict）会被兜成 low**，换家到 GLM 时真发
        #    `enabled + reasoning_effort: low`，与表里注释（什么都不发）不一致。
        #    现在：**档位存在就用它**（哪怕它是空 dict）；只有档位名压根不在表里才兜 low。
        params = dict(table[level] if level in table else table["low"])
        # 手写 extra_body 的优先级最高：某层显式写了 reasoning_effort/enable_thinking，
        # 就以它为准（档位表是"默认策略"，extra_body 是"我就要这样"）。
        # ⚠️ 还要把档位给的**另一个**思考类参数丢掉：否则会同时发出
        # `enable_thinking: false` + `reasoning_effort: high`（一个说别想、一个说使劲想），
        # 到底哪个生效由平台决定，等于没配。
        _THINK_KEYS = ("enable_thinking", "reasoning_effort", "thinking_budget", "thinking")
        explicit = [k for k in _THINK_KEYS if k in self.extra_body]
        if explicit:
            for key in _THINK_KEYS:
                if key not in explicit:
                    params.pop(key, None)
        params.update(self.extra_body)
        return params

    async def _post_stream(self, payload: dict) -> tuple[str, str, dict]:
        """流式发一次，返回（正文, 思考链, usage）。

        ⚠️ 判"卡住"的判据是**静默**（连续 idle_timeout 秒没有任何 chunk），
           不是总时长 —— 思考模型想 200 秒很正常，但那 200 秒里它一直在吐思考链。
        """
        body = dict(payload)
        body["stream"] = True
        # 让最后一帧带 usage（快米兔文档里写明了支持；不支持的话拿不到 token 数，不影响正文）
        body["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        think_parts: list[str] = []
        usage: dict = {}
        got_any = False
        # ⚠️ 2026-10-10 晚修：**等响应头那一段也要算进"静默"**。
        #    以前只盯"收到头之后的收流"，上游连响应头都不发时会一路撑到 httpx 的总超时
        #    （450 秒）—— 实测 11号 就这么卡满了引擎的 420 秒。
        #    httpx 的 read 超时同时覆盖"等响应头"和"两帧之间"，正好是我们要的判据。
        _req_timeout = httpx.Timeout(self.timeout, connect=20.0,
                                     read=max(5.0, self.idle_timeout), write=30.0, pool=30.0)
        async with self._client.stream("POST", "/chat/completions", json=body,
                                       timeout=_req_timeout) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "ignore")[:200]
                raise LLMError(redact(f"HTTP {resp.status_code}: {raw}"),
                               status=resp.status_code)
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in ctype:
                # ⚠️ 上游忽略了 `stream: true`，回了一段整 JSON（实测有的中转站这样）。
                #    这时按 SSE 解析只会得到"0 个字节"，把**成功**的调用误判成失败 ——
                #    所以这里退回普通解析。
                raw = (await resp.aread()).decode("utf-8", "ignore")
                try:
                    data = json.loads(raw)
                except ValueError as exc:
                    raise LLMError(f"上游既不是 SSE 也不是 JSON（content-type={ctype!r}）: "
                                   f"{raw[:120]}") from exc
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
                text = msg.get("content") or ""
                if not isinstance(text, str):
                    text = str(text)
                think = msg.get("reasoning_content") or msg.get("reasoning") or ""
                return text, str(think), (data.get("usage") or {})
            lines = resp.aiter_lines()
            while True:
                try:
                    line = await asyncio.wait_for(lines.__anext__(), timeout=self.idle_timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise LLMError(
                        f"上游连续 {self.idle_timeout:.0f} 秒没有吐出任何字节"
                        f"（判定卡住，准备重试；已收到 {sum(len(x) for x in think_parts)} 字思考链）",
                    ) from exc
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk in ("", "[DONE]"):
                    continue
                try:
                    j = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(j.get("usage"), dict) and j["usage"]:
                    usage = j["usage"]
                delta = ((j.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content")
                if piece:
                    # ⚠️ 2026-10-30：非流式分支早就处理了"content 是数组式 parts"，
                    #    流式这边以前直接 str(piece) —— list 会被转成 Python 字面量字符串，
                    #    后面 extract_json 必然失败、白烧 5 次重试。两分支现在同一口径。
                    if not isinstance(piece, str):
                        if isinstance(piece, list):
                            piece = "".join(str(p.get("text") or "") if isinstance(p, dict)
                                            else str(p) for p in piece)
                        else:
                            piece = str(piece)
                    text_parts.append(piece)
                    got_any = True
                think = delta.get("reasoning_content")
                if think:
                    think_parts.append(str(think))
                    got_any = True
        if not got_any:
            raise LLMError("流式收到 0 个字节（上游接了请求但什么都没回）")
        return "".join(text_parts), "".join(think_parts), usage

    def _retry_wait(self, attempt: int) -> float:
        """客户端自动重试之间的退避（秒）：`min(base*(attempt+1), base*4)`。

        `base` = `self.retry_sleep`（默认 1.5 ⇒ 1.5/3/4.5/6 秒，与 2026-10-07 定的口径一致）。
        测试配置把它写小（`llm.default.retry_sleep: 0.1`）就能把"重试链路"跑得很快 ——
        与引擎层 `game.retry_backoff` 是同一个套路（见 `t_auto_retry.py` 用的 0.3/0.8）。
        """
        base = float(getattr(self, "retry_sleep", 1.5) or 0.0)
        return min(base * (attempt + 1), base * 4)

    async def chat_json(self, packet: str, extra: dict | None = None) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": packet},
            ],
            "max_tokens": self.max_tokens,
        }
        # ⚠️ 有的通道（52pay 的 claude-haiku-5-5 实测）**不接受自定义 temperature**：
        # 配置里把温度写成 `temperature: null` 就整个字段不发。
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload.update(self.extra_body)
        if extra:
            payload.update(extra)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                started = asyncio.get_running_loop().time()
                # ⚠️ 2026-10-10 晚：默认走**流式**（理由见 `_post_stream` 的注释）——
                #    非流式下"上游憋住不回"只能干等到总超时（实测 300 秒），
                #    那时整手预算已经烧光、没机会重试（快米兔客服：抖动时第二次就通）。
                if self.stream:
                    text, _reason, used = await self._post_stream(payload)
                    elapsed = asyncio.get_running_loop().time() - started
                    cached = cached_tokens_of(used)
                    self.usage["calls"] += 1
                    self.usage["seconds"] += elapsed
                    self.usage["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
                    self.usage["completion_tokens"] += int(used.get("completion_tokens") or 0)
                    self.usage["cached_tokens"] += cached
                    self.last_call = {
                        "model": self.model,
                        "raw": text,
                        "reasoning": _reason,
                        "thinking": dict(extra or {}),
                        "prompt_tokens": int(used.get("prompt_tokens") or 0),
                        "completion_tokens": int(used.get("completion_tokens") or 0),
                        "cached_tokens": cached,
                        "elapsed": round(elapsed, 2),
                        "stream": True,
                    }
                    return extract_json(text)
                resp = await self._client.post("/chat/completions", json=payload)
                elapsed = asyncio.get_running_loop().time() - started
                if resp.status_code >= 400:
                    raise LLMError(redact(f"HTTP {resp.status_code}: {resp.text[:200]}"),
                                   status=resp.status_code)
                data = resp.json()
                message = (data.get("choices") or [{}])[0].get("message", {}) or {}
                text = message.get("content") or ""
                # ⚠️ 有的平台会把 content 返回成数组式 parts（[{"type":"text","text":...}]）。
                # 那样 text 就不是字符串，下面 .strip() 会 AttributeError ——
                # 被宽 except 当成"上游波动"，白重试 5 次、还可能暂停整局。
                if not isinstance(text, str):
                    if isinstance(text, list):
                        text = "".join(str(p.get("text") or "") if isinstance(p, dict) else str(p)
                                       for p in text)
                    else:
                        text = str(text)
                used = data.get("usage") or {}
                cached = cached_tokens_of(used)
                self.usage["calls"] += 1
                self.usage["seconds"] += elapsed
                self.usage["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
                self.usage["completion_tokens"] += int(used.get("completion_tokens") or 0)
                self.usage["cached_tokens"] += cached
                self.last_call = {
                    "model": self.model,
                    "raw": text,
                    # 思考模型（开了 enable_thinking）会额外返回推理过程，能拿到就存下来
                    "reasoning": message.get("reasoning_content") or message.get("reasoning") or "",
                    "thinking": dict(extra or {}),          # 这次用的思考档位，复盘能看到
                    "prompt_tokens": int(used.get("prompt_tokens") or 0),
                    "completion_tokens": int(used.get("completion_tokens") or 0),
                    "cached_tokens": cached,
                    "elapsed": round(elapsed, 2),
                }
                return extract_json(text)
            except Exception as exc:            # noqa: BLE001 —— 网络/超时/格式全都要兜住
                last_error = exc
                self.usage["attempts"] = self.usage.get("attempts", 0) + 1
                self.usage["failed_seconds"] = round(
                    self.usage.get("failed_seconds", 0.0)
                    + (asyncio.get_running_loop().time() - started), 2)
                status = getattr(exc, "status", None)
                if isinstance(status, int) and 400 <= status < 500 and status != 429:
                    break                       # 401/400 这类重试也不会好，别再白等
                # ⚠️ **挂住（读超时）不在这一层重试**（2026-10-07 定的分工）：
                #    上游"接了请求但一个字节都不回"时，同一家再撞一次基本还是挂 ——
                #    实测 GLM 那次跑了 724 秒、MiMo 那次直接不返回。
                #    继续在这里硬试只会把时间烧光、而且手机端看不到任何事件。
                #    这类失败交给**引擎层**：带可见退避（3s→8s）再试，且受整手预算约束。
                # ⚠️ 流式的"静默超时"抛的是 **LLMError**（不是 httpx.TimeoutException）——
                #    所以它会落进下面的重试分支（这正是我们要的：卡住就换一次再来）。
                #    这里列的是**真正的读超时**（整个请求超时），那种仍然交给引擎换家。
                if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
                    # ⚠️ 2026-10-10 晚：**流式**下的读超时就是"静默到点"（我们自己的
                    #    50 秒判据）→ 当成"上游抖了"允许重试（客服口径：第二次通常就通）。
                    #    非流式的读超时仍按老规矩：不在客户端硬撞，交给引擎换家。
                    if self.stream and isinstance(exc, httpx.ReadTimeout):
                        if attempt < self.max_retries:
                            await asyncio.sleep(self._retry_wait(attempt))
                            continue
                    self.usage["hangs"] = self.usage.get("hangs", 0) + 1
                    break
                if attempt < self.max_retries:
                    # 退避：默认 1.5s → 3s → 4.5s → 6s（最多 4 次重试，总等待 ≤15s）。
                    # 上游波动（中转站 5xx / 超时 / 限流）大多一两秒就恢复，别用固定间隔硬撞。
                    await asyncio.sleep(self._retry_wait(attempt))
                    continue
        self.usage["errors"] += 1
        # 把 HTTP 状态码带上（137 行的注释说重试策略要用它；以前 raise 时丢了，
        # 上层只能去字符串里找 "HTTP 401"）
        raise LLMError(f"{self.label}: {last_error}",
                       status=getattr(last_error, "status", None))
