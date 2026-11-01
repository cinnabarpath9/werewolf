# coding=utf-8
"""座位级模型路由：每个座位可以接**不同的平台、不同的 key、不同的模型**。

为什么按座位配
--------------
你手上的 API 可能来自好几家平台（千问AI平台、智谱、DeepSeek、自建中转……），
每家的 base_url、key、模型名都不一样。而狼人杀里不同座位也该用不同档次的模型：
吃精确回忆的位置（狼队复盘、预言家对跳）用强模型，气氛位用便宜的。

配置优先级：seats[座位号]  >  roles[角色]  >  default

⚠️ 路由不负责隔离
----------------
隔离永远来自上下文（见 context.py）：一条 key 还是九条 key，
跟"谁能看到什么"没有任何关系。多 key 的意义是**分摊限流、分开记账、故障隔离**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .llm import (DEFAULT_THINKING, SPEECH_TASKS, THINKING_LEVELS,  # noqa: F401
                  LLMClient, normalize_thinking)


@dataclass
class SeatLLM:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 1.0
    #: 开了思考时输出可能上千 token，给小了会把 JSON 截断（截断=这一手作废），
    #: 所以默认值就按"够用"来（config.yaml 里也是 4000）
    max_tokens: int = 4000
    timeout: float = 90.0
    #: 上游波动（中转站 5xx / 超时 / 限流）时的自动重试次数。
    #: 4 = 首次失败后再试 4 次（共 5 次请求，退避 1.5/3/4.5/6 秒）；
    #: 4 次都失败就交给 engine 的 on_stall：**暂停对局**问用户（见 config.yaml 的 game.stall_timeout）。
    max_retries: int = 4
    #: 客户端**重试之间的退避基数**（秒）。默认 1.5 ⇒ 1.5/3/4.5/6 秒（与 2026-10-07 的口径一致）。
    #: ⚠️ 2026-10-11 加：以前这个等待**写死在 `llm.py`**，于是跑"整条重试链"的测试
    #:    （`t_stall_ws.py`）光 sleep 就要 ~150 秒。引擎层早就有同款口子
    #:    （`game.retry_backoff`，测试用 0.3/0.8），这里补齐；**不写 = 原值不变**。
    retry_sleep: float = 1.5
    #: 用**流式**请求（2026-10-10 晚加，默认开）。见 `LLMClient.__init__` 的说明：
    #: 非流式下"上游接了请求却不吐字节"只能干等到总超时，那时没机会重试。
    stream: bool = True
    #: 流式下"连续多少秒没有任何字节"判定卡住（思考链是连续吐的，所以不会误判）。
    idle_timeout: float = 50.0
    extra_body: dict = field(default_factory=dict)
    thinking: dict = field(default_factory=dict)
    #: 这个 spec 对应的平台认哪种思考参数（qwen / ark / glm / openai）。
    #: ⚠️ 2026-10-08 补（P2-1）：以前这个字段**不存在**，`build_client` 的默认值又是
    #:    "openai" —— 于是替补（换家）那条路永远按 openai 发 `reasoning_effort`：
    #:    换到 QWEN 时该发 enable_thinking/thinking_budget、换到 ARK/GLM 时该发 thinking.type，
    #:    档位表在替补身上**完全失效**（线上 game.fallback 非空，这条路上真会走）。
    #:    `resolve` / `resolve_profile` 负责把它填对。
    thinking_style: str = "openai"

    @property
    def host(self) -> str:
        try:
            return urlparse(self.base_url).netloc or self.base_url
        except Exception:                       # noqa: BLE001
            return self.base_url

    @property
    def key(self) -> str:
        """经验存档的键：同一个平台上的同一个模型算同一个"脑子"。"""
        return f"{self.host}/{self.model}" if self.host else self.model

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def _lookup(mapping, *keys):
    """YAML 里 3 和 "3" 都可能出现，两种都试。"""
    if not mapping:
        return None
    if not isinstance(mapping, dict):
        return None                          # roles/seats 误写成字符串时别抛 TypeError
    for key in keys:
        if key in mapping:
            return mapping[key]
        if str(key) in mapping:
            return mapping[str(key)]
    return None


def _f(block: dict, key: str, default: float) -> float:
    """读数值配置：键在但值是空（YAML 里写成 `temperature:`）时用默认值，不抛裸异常。"""
    try:
        value = block.get(key, default)
        return float(default) if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return float(default)


def _i(block: dict, key: str, default: int) -> int:
    try:
        value = block.get(key, default)
        return int(default) if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return int(default)


#: 同一个字段的几种写法。深层只写了别名时，不能让浅层档案里的另一个别名把它顶掉。
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("api_key", "api_key_env"),
    ("base_url", "base_url_env"),
    ("model", "model_env"),
)


def _merge(base: dict | None, override: dict | None) -> dict:
    out = dict(base or {})
    override = override or {}
    for key, value in override.items():
        if value is None:
            continue
        if key == "extra_body" and isinstance(value, dict):
            merged = dict(out.get("extra_body") or {})
            merged.update(value)
            out["extra_body"] = merged
        else:
            out[key] = value
    # 深层显式写了同义字段（比如座位写 api_key_env=DEEPSEEK_API_KEY），
    # 就把从档案继承来的另一个同义字段删掉 —— 否则用户以为换了 key，实际还在用档案那把。
    for group in _ALIAS_GROUPS:
        if any(k in override and str(override.get(k) or "").strip() for k in group):
            for key in group:
                if key not in override:
                    out.pop(key, None)
    return out


def _api_key(block: dict) -> str:
    if block.get("api_key"):                    # 直接写死（不推荐，但允许）
        return str(block["api_key"])
    env_name = block.get("api_key_env")
    if env_name:
        return os.environ.get(str(env_name), "")
    return ""


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _expand(block: dict | None) -> dict:
    """把 profile 展开成具体字段。

    平台档案写在 .env 里，一组三行：

        QWEN_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1
        QWEN_API_KEY=sk-xxx
        QWEN_MODEL=qwen3.7-plus

    config.yaml 里只要写 `profile: QWEN`。key 和 Base URL 放一处，不容易配错。
    同一个块里显式写的 base_url / model / api_key / api_key_env 优先于档案。

    ⚠️ 这里必须挡住"写法不对"的配置：`llm: {default: QWEN}`（忘了写 `profile:`）、
    `extra_body: "..."` 这类会抛裸 ValueError/TypeError —— 它出现在开局预检与 status 广播里，
    被 server 的兜底 except 接住后**只断开手机 WebSocket、界面没有任何提示**（很难查）。
    不是 dict 就当成空块返回，由 `problems()` 报一句人话。
    """
    if block is not None and not isinstance(block, dict):
        return {}
    out = dict(block or {})
    name = out.get("profile")
    if not name:
        return out
    prefix = str(name).strip().upper()
    out["base_url"] = _first(out.get("base_url"), os.environ.get(f"{prefix}_BASE_URL"))
    out["model"] = _first(out.get("model"), os.environ.get(f"{prefix}_MODEL"))
    if not out.get("api_key") and not out.get("api_key_env"):
        out["api_key"] = os.environ.get(f"{prefix}_API_KEY", "")
    return out


def api_tuning(profile: str) -> dict:
    """页面上给某条 API 配的「模型参数」（`settings_store.apply` 写进 `WOLF_API_TUNING`）。

    形如 `{"ARK": {"temperature": 1.3, "max_tokens": 8000, "thinking": "low"}}`。
    没有、写坏、不是对象 → 一律返回 `{}`：**绝不因为一个坏 JSON 让整局起不来**，
    那会让"改了个参数"变成"服务打不开了"。
    """
    raw = os.environ.get("WOLF_API_TUNING", "")
    if not raw:
        return {}
    try:
        table = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(table, dict):
        return {}
    tag = str(profile or "").strip()
    got = table.get(tag.upper()) or table.get(tag.lower()) or table.get(tag)
    return dict(got) if isinstance(got, dict) else {}


def seat_profile_overlay(seat_id: int) -> str:
    """从 .env 的 WOLF_SEAT_PROFILES 里读"这个座位用哪个平台档案"。

    格式：`WOLF_SEAT_PROFILES=1:QWEN,3:DEEPSEEK,4:QWEN`
    有了它，在手机上就能给座位分配平台，不用去改 config.yaml。
    """
    raw = os.environ.get("WOLF_SEAT_PROFILES", "")
    for chunk in raw.replace("，", ",").replace("；", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        left, right = chunk.split(":", 1)
        try:
            if int(left.strip()) == int(seat_id):
                return right.strip().upper()
        except ValueError:
            continue
    return ""


def _merge_thinking(levels: list) -> dict:
    """把各层的 thinking 表合并：同一组、同一天，深层覆盖浅层（**逐天覆盖，不是整表替换**）。

    也就是说：深层只写了自己关心的那几天，浅层其余的天继续生效。例如
      llm.thinking:            {decision: {1: low, 3: high}}     # 全局：第 3 天起最高
      llm.roles.seer.thinking: {decision: {1: high}}             # 预言家：第 1 天就最高
    合起来是 {1: high, 3: high}（预言家），其他人是 {1: low, 3: high}。
    想把某组整个改掉，就把那几天都写全。
    """
    out: dict[str, dict] = {}
    for level in levels:
        spec = level.get("thinking") if isinstance(level, dict) else None
        if spec is None:   # ⚠️ 2026-10-11：布尔 False（YAML 裸 off）会被 falsy 判断吞掉
            continue
        norm, _ = normalize_thinking(spec, fill_defaults=False)
        for group, days in norm.items():
            out.setdefault(group, {}).update(days)
    if not out:
        return normalize_thinking(DEFAULT_THINKING)[0]
    # 补一个第 1 天的兜底，免得某组只有"第 3 天起"
    for group, days in out.items():
        if days and min(days) > 1:
            days[1] = days[min(days)]
    for group in ("speech", "decision"):
        out.setdefault(group, dict(DEFAULT_THINKING[group]))
    return out


def resolve(cfg: dict, seat) -> SeatLLM:
    """算出这个座位该用哪家平台、哪个 key、哪个模型。

    优先级：seats[座位号] > roles[角色] > default（每一层先各自展开 profile 再合并，
    所以座位层写的 profile 不会被 default 层的 base_url 顶掉）。
    最外层还有 .env 里的 WOLF_SEAT_PROFILES（手机端配的），它压过 config.yaml。
    """
    llm = (cfg or {}).get("llm") or {}
    role_block = _lookup(llm.get("roles"), seat.role_key)
    seat_block = _lookup(llm.get("seats"), seat.id)
    block = _merge(_expand(llm.get("default")), _expand(role_block))
    block = _merge(block, _expand(seat_block))

    overlay = seat_profile_overlay(seat.id)
    if overlay:
        block = _merge(block, _expand({"profile": overlay}))

    # 页面上给这条 API 配的「模型参数」（设置 → 模型 API → 参数）：跟着渠道走，
    # 只有在用这条 API 的座位上叠加，没填的项仍走 config.yaml 的内置默认。
    profile_name = _profile_name(llm, seat, overlay)
    tuning = api_tuning(profile_name)
    if tuning:
        block = dict(block)
        for key in ("temperature", "max_tokens", "timeout", "max_retries", "idle_timeout"):
            if key not in tuning:
                continue
            try:                                   # 手改过环境变量时也别把座位带崩
                float(tuning[key])
            except (TypeError, ValueError):
                continue
            block[key] = tuning[key]

    def _wants_profile(level) -> bool:
        if not isinstance(level, dict):
            return False
        return bool(str(level.get("profile") or level.get("profile_env") or "").strip())

    # 只要任何一层点名用了平台档案，就要求它完整：缺 *_BASE_URL 时留空、交给预检报错，
    # 而不是悄悄落到全局兜底 WOLF_BASE_URL —— 那会把这一座的 key 发到一台你没预期的机器。
    used_profile = (_wants_profile(llm.get("default")) or _wants_profile(role_block)
                    or _wants_profile(seat_block) or bool(overlay))
    base_url = str(block.get("base_url") or "")
    if not base_url and not used_profile:
        base_url = os.environ.get("WOLF_BASE_URL", "")
    # extra_body 必须是 dict：写成字符串/列表时以前直接在 dict() 上抛 TypeError，
    # 一路冒到 status 广播 → 手机静默断线。这里退回空字典，交给 problems() 报人话。
    extra_raw = block.get("extra_body")
    if extra_raw is not None and not isinstance(extra_raw, dict):
        extra_raw = {}
    thinking = _merge_thinking([{"thinking": llm.get("thinking")},
                                llm.get("default"), role_block, seat_block])
    # 参数页选了"思考档"= 这条渠道所有任务都用这一档（覆盖档位表；不填就别动）
    if tuning.get("thinking") in ("off", "low", "high"):
        level = tuning["thinking"]
        thinking = {group: {day: level for day in days} for group, days in thinking.items()}
    return SeatLLM(
        base_url=base_url,
        api_key=_api_key(block),
        model=str(block.get("model") or ""),
        temperature=_f(block, "temperature", 1.0),
        max_tokens=_i(block, "max_tokens", 4000),
        timeout=_f(block, "timeout", 200.0),
    # 流式 + 静默超时（2026-10-10 晚）：属于"这一座怎么打"，换家时保留
    stream=bool(block.get("stream", True)),
    idle_timeout=_f(block, "idle_timeout", 50.0),
        max_retries=_i(block, "max_retries", 4),
        retry_sleep=_f(block, "retry_sleep", 1.5),
        extra_body=dict(extra_raw or {}),
        # 思考档位按层合并，**顺序＝优先级（后面的覆盖前面的）**：
        # 全局 thinking → default 块 → 角色块 → 座位块。座位最优先。
        thinking=thinking,
        # 平台风格跟着"这个座位最终用哪个档案"走（.env 座位分配 > 座位 > 角色 > default）。
        # **不在 build_client 里现算**：换家时 spec 会被换成目标档案，风格必须跟着一起换。
        thinking_style=thinking_style_for(cfg, profile_name),
    )


def thinking_style_for(cfg: dict, profile: str) -> str:
    """这个平台认哪种思考参数（qwen / ark / glm / openai）。

    优先读 `llm.thinking_style.<平台>`；没写就按平台名猜一个（猜错也只是"档位不生效"，
    不会报错 —— 所以 config.yaml 里显式写一份更保险）。
    """
    llm = (cfg or {}).get("llm") or {}
    if not isinstance(llm, dict):              # 配置写错类型时别在预检里抛裸异常
        llm = {}
    table = llm.get("thinking_style")
    tag = str(profile or "").strip().upper()
    if isinstance(table, dict):
        got = str(table.get(tag) or table.get(tag.lower()) or "").strip().lower()
        if got:
            return got
    return {
        "QWEN": "qwen", "QWEN_TURBO": "qwen", "DASHSCOPE": "qwen",
        "ARK": "ark", "DOUBAO": "ark", "VOLC": "ark",
        "GLM": "glm", "ZHIPU": "glm",
    }.get(tag, "openai")


def build_client(spec: SeatLLM, label: str = "", thinking_style: str | None = None) -> LLMClient:
    """把 spec 变成一个真客户端。

    ⚠️ 2026-10-08（P2-1）：`thinking_style` **不再有 `"openai"` 默认值**。
    以前那个默认值是**静默**的：替补路径（server.py / run_game.py 的 `_build_fallbacks`）
    没传这个参数，于是不管换到哪家都按 openai 发 `reasoning_effort` ——
    换到 QWEN 时正好发的是 config.yaml 里记过「会偶发 503」的那个参数。
    现在不传就取 `spec.thinking_style`（由 resolve/resolve_profile 按目标档案算好），
    要覆盖就显式传 —— 两条路都拿得到正确的风格，不存在"忘了传就退回 openai"。
    """
    style = spec.thinking_style if thinking_style is None else thinking_style
    return LLMClient(
        # 流式 + 静默超时（2026-10-10 晚加）：卡住的请求能早点发现、早点重试
        stream=bool(getattr(spec, "stream", True)),
        idle_timeout=float(getattr(spec, "idle_timeout", 50.0)),
        base_url=spec.base_url,
        api_key=spec.api_key,
        model=spec.model,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout,
        max_retries=spec.max_retries,
        # 重试退避基数也属于"这一座怎么打"（换家不该变），见 SeatLLM.retry_sleep
        retry_sleep=float(getattr(spec, "retry_sleep", 1.5)),
        extra_body=spec.extra_body,
        thinking=spec.thinking,
        thinking_style=style,
        label=label or f"{spec.host}/{spec.model}",
    )


def fallback_thinking_for(cfg: dict, profile: str):
    """换手到 `profile` 这家顶班时，该用哪一档思考（`llm.fallback_thinking.<目标档案>`）。

    为什么要有这个口子（2026-10-12 真局实测，存档 `games/game-20261012-105959.json`）：
    5 号是 `MIMO` + `thinking: low`，第 1 天一次调用顶到 420 秒上限后换 DEEPSEEK 顶班 ——
    替补那一手发的是 `{thinking: {type: enabled}, reasoning_effort: low}`、36.09 秒 /
    5,384 tokens，而同局 DS 的两个**原生**座位（2/8，`thinking: "off"`）只要 1.5~2.0 秒 /
    36~824 tokens。⚠️ 那**不是**「继承了 MiMo 的参数」：参数名一直跟着**目标档案**走
    （见 `resolve_profile` 末尾那句 `thinking_style_for(cfg, profile)`），只有「哪一档」是
    本座位自己的（座位层 `thinking: low`）；而 DEEPSEEK 在 `llm.thinking_style` 里登记的是
    `ark`，ark 的 low 与 mimo 的 low **恰好同形** —— 所以从参数上看不出区别（换到 QWEN 就会发
    `enable_thinking` / `thinking_budget`，形态完全不同，见 `tests/t_fallback_thinking.py`）。
    真问题只是「作者没法给替补指定另一档」，于是有了这个口子。

    写法（与 `llm.thinking` / `llm.seats` 同风格；键 = 档案名，大小写不敏感）：

        llm:
          fallback_thinking:
            DEEPSEEK: off                 # 换到 DS 顶班时关思考（YAML 裸 off 也认，见 E56）
            QWEN: low                     # 换到千问时走中档
            MIMO: {decision: high}        # 也可以按组/按天写，只覆盖点名的那些

    **不写 = 与以前一字不差**（替补沿用本座位档位 × 目标档案风格）；写错由 `problems()` 报人话。
    """
    llm = (cfg or {}).get("llm") or {}
    if not isinstance(llm, dict):                 # 配置写错类型时别在预检里抛裸异常
        return None
    table = llm.get("fallback_thinking")
    if not isinstance(table, dict):
        return None
    tag = str(profile or "").strip()
    if not tag:
        return None
    for key in (tag, tag.upper(), tag.lower()):   # 档案名大小写不敏感（config 里两种写法都有）
        if key in table:
            return table[key]
    return None


def resolve_profile(cfg: dict, seat, profile: str) -> SeatLLM:
    """把某个座位**换成指定的平台档案**再解析一遍（"上游挂了就换一家顶上"用）。

    取哪些、丢哪些是有讲究的：

    * **跟着目标档案走的**：`base_url` / `api_key` / `model` / 档案自带的 `extra_body`
      —— 跨平台换家，这些必须一起换，否则就是把 A 家的 key 发到 B 家的模型名上。
    * **保留这个座位自己的**：`temperature` / `max_tokens` / `timeout` / `max_retries` /
      `thinking` —— 这些是"这一座怎么打"的设置，换家时不该变。
    * **档位默认沿用这个座位自己的**，但作者可以用 `llm.fallback_thinking.<目标档案>`
      点名覆盖（例如换到 DEEPSEEK 顶班时关思考，动机见 `fallback_thinking_for`）——
      **不写 = 与以前一字不差**（见 docs/待完善清单.md E58）。
    * **显式写在座位层的 `extra_body` 不带过去**：那些键常常是平台专用的
      （QWEN 的 `enable_thinking`、ARK 的 `thinking.type`），原样发给别家只会出怪问题。
      思考档位由 `thinking` 表达，客户端会自己翻译成对应平台的参数（见 `LLMClient.extra_for`）。
    * **`thinking_style` 跟着目标档案走**：换到家了就得按那家的参数名发档位
      （QWEN 认 enable_thinking/thinking_budget、ARK/GLM 认 thinking.type、其余认
      reasoning_effort）—— 见 P2-1。
    """
    llm = (cfg or {}).get("llm") or {}
    role_block = _lookup(llm.get("roles"), seat.role_key)
    seat_block = _lookup(llm.get("seats"), seat.id)
    mine = _merge(_merge(_expand(llm.get("default")), _expand(role_block)), _expand(seat_block))
    target = _expand({"profile": profile})
    extra_raw = target.get("extra_body")
    return SeatLLM(
        base_url=str(target.get("base_url") or ""),
        api_key=_api_key(target),
        model=str(target.get("model") or ""),
        temperature=_f(mine, "temperature", 1.0),
        max_tokens=_i(mine, "max_tokens", 4000),
        timeout=_f(mine, "timeout", 200.0),
    stream=bool(mine.get("stream", True)),
    idle_timeout=_f(mine, "idle_timeout", 50.0),
        max_retries=_i(mine, "max_retries", 4),
        retry_sleep=_f(mine, "retry_sleep", 1.5),
        extra_body=dict(extra_raw) if isinstance(extra_raw, dict) else {},
        thinking=_merge_thinking([{"thinking": llm.get("thinking")},
                                  llm.get("default"), role_block, seat_block,
                                  # ⚠️ 最深一层 = 作者点名「换到这家顶班用哪一档」
                                  #    （`llm.fallback_thinking.<目标档案>`，见 E58）。
                                  #    不写时它是 None ⇒ `_merge_thinking` 直接跳过这一层，
                                  #    行为与以前**一字不差**。逐组、逐天覆盖（同全局表口径）。
                                  {"thinking": fallback_thinking_for(cfg, profile)}]),
        # ⚠️ 必须用**目标档案**的风格（不是这个座位原来那家）：换家之后参数名由新家决定。
        thinking_style=thinking_style_for(cfg, profile),
    )


def fallback_specs(cfg: dict, seat, order: list | None) -> list[tuple[str, SeatLLM]]:
    """按 `order`（平台档案名的列表）算出这个座位的替补名单，**跳过它自己用的那家**。

    返回 [(档案名, spec), ...]，顺序就是重试时依次尝试的顺序。
    """
    current = _profile_of(cfg, seat).strip().upper()
    out: list[tuple[str, SeatLLM]] = []
    seen = {current}
    # ⚠️ `fallback: DEEPSEEK`（忘了写成列表）会被当成字符串逐字符迭代 ——
    #    每个字母都当档案名去查，结果"静默没有任何替补"，而用户以为配好了。这里按一家处理。
    if isinstance(order, str):
        order = [x for x in order.replace("，", ",").split(",") if x.strip()]
    for name in (order or []):
        tag = str(name or "").strip()
        if not tag or tag.upper() in seen:
            continue
        seen.add(tag.upper())
        spec = resolve_profile(cfg, seat, tag)
        if spec.base_url and spec.api_key and spec.model:      # 没配好的档案不进替补席
            out.append((tag.upper(), spec))
    return out


def _profile_name(llm: dict, seat, overlay: str = "") -> str:
    """这个座位最终用的是哪个平台档案（.env 座位分配 > config 座位 > 角色 > default）。

    ⚠️ `resolve` 里**必须先把这个名字算出来**再去建 spec：`thinking_style` 要按它取
    （见 P2-1），而 `resolve` 自己的 `block` 是"展开 + 合并"过的，合并之后
    `profile` 这个键可能被浅层顶掉、或者本来就只在 .env 的座位分配里。
    """
    if overlay:
        return overlay
    if not isinstance(llm, dict):
        return ""
    for block in (_lookup(llm.get("seats"), seat.id),
                  _lookup(llm.get("roles"), seat.role_key),
                  llm.get("default")):
        # ⚠️ block 可能是字符串/列表/数字（配置写错类型）—— 这里只认 dict
        if isinstance(block, dict) and block.get("profile"):
            return str(block["profile"]).strip().upper()
    return ""


def _profile_of(cfg: dict, seat) -> str:
    """这个座位最终用的是哪个平台档案（.env 座位分配 > config 座位 > 角色 > default）。"""
    return _profile_name((cfg or {}).get("llm") or {}, seat, seat_profile_overlay(seat.id))


def env_hint(cfg: dict, seat) -> str:
    """告诉用户该去改哪三个变量 —— 报错时最有用的一句话。"""
    name = _profile_of(cfg, seat)
    if name:
        return f".env 里的 {name}_BASE_URL / {name}_API_KEY / {name}_MODEL"
    return "config.yaml 的 llm.default（base_url / model / api_key_env，或者改用 profile）"


def problems(cfg: dict, seats) -> list[str]:
    """开跑前检查每个座位能不能拿到配置，返回人话问题清单。"""
    issues: list[str] = []
    # 思考档位表写错也要在这里报出来（运行时会被规范化兜住，但不能不告诉你）。
    llm = (cfg or {}).get("llm") or {}
    # ⚠️ 先查"块本身写错了类型"：`llm: {default: QWEN}`（忘了 profile:）这种写法以前
    #    会在 resolve() 里抛裸异常 → 手机静默断线。这里给人话，别让它到运行时。
    for where, block in (("llm.default", llm.get("default")),):
        if block is not None and not isinstance(block, dict):
            issues.append(f"{where} 应该是一个映射（profile/temperature/...），"
                           f"现在写成了 {type(block).__name__}：想指定平台请写 profile: QWEN")
    # 角色名拼错 / 座位号越界：以前静默不生效（那个座位照旧用 default 的模型）
    from .roles import ROLES
    roles_map_raw = llm.get("roles")
    if isinstance(roles_map_raw, dict):
        bad_roles = [k for k in roles_map_raw if k not in ROLES]
        if bad_roles:
            issues.append(f"llm.roles 里有不存在的角色名：{bad_roles}（可选：{sorted(ROLES)}）")
    seats_map_raw = llm.get("seats")
    seat_ids = {s.id for s in seats}
    if isinstance(seats_map_raw, dict):
        bad_seats = []
        for k in seats_map_raw:
            try:
                if int(k) not in seat_ids:
                    bad_seats.append(k)
            except (TypeError, ValueError):
                bad_seats.append(k)
        if bad_seats:
            issues.append(f"llm.seats 里有本局不存在的座位号：{bad_seats}"
                          f"（本局座位：{sorted(seat_ids)}）")
    tables: list[tuple[str, object]] = [("llm.thinking", llm.get("thinking"))]
    roles_map = roles_map_raw if isinstance(roles_map_raw, dict) else {}
    seats_map = seats_map_raw if isinstance(seats_map_raw, dict) else {}
    for name, block in roles_map.items():
        if isinstance(block, dict) and block.get("thinking") is not None:
            tables.append((f"llm.roles.{name}.thinking", block["thinking"]))
    for name, block in seats_map.items():
        # ⚠️ 2026-10-12：与上面角色那处对齐（原先座位这处是 falsy 判断 —— 布尔 False
        #    （YAML 裸 off）会被跳过，看着没问题其实一个字都没校验）。
        if isinstance(block, dict) and block.get("thinking") is not None:
            tables.append((f"llm.seats.{name}.thinking", block["thinking"]))
    for where, table in tables:
        if table is None:
            continue
        # fill_defaults=False：只报**真**问题（档位名不认识、天数键不是数字、类型不对），
        # 不报"没有第 1 天"—— 那在多层合并之后往往并不缺，孤立校验只会误报。
        for issue in normalize_thinking(table, fill_defaults=False)[1]:
            issues.append(f"思考档位配置（{where}）：{issue}")
    # 换手档位（`llm.fallback_thinking`，E58）写错也要报出来：档位名不认识、或者档案名拼错，
    # 都是「配了却永远不生效」——与 E56 那两个静默失效同类，不能只在运行时悄悄兜住。
    fallback_table = llm.get("fallback_thinking")
    if fallback_table is not None and not isinstance(fallback_table, dict):
        issues.append(f"llm.fallback_thinking 应该是一个映射（档案名 → 档位），"
                      f"现在写成了 {type(fallback_table).__name__}："
                      f"想表达「换到 DEEPSEEK 就关思考」请写 DEEPSEEK: off")
    elif isinstance(fallback_table, dict):
        style_table = llm.get("thinking_style")
        known = ({str(k).strip().upper() for k in style_table}
                 if isinstance(style_table, dict) else set())
        order = ((cfg or {}).get("game") or {}).get("fallback")
        if isinstance(order, str):                 # `fallback: DEEPSEEK` 这种写法
            order = order.replace("，", ",").split(",")
        known |= {str(x).strip().upper() for x in (order or []) if str(x or "").strip()}
        for raw_name, level in fallback_table.items():
            tag = str(raw_name).strip().upper()
            if level is None:
                issues.append(f"llm.fallback_thinking.{raw_name} 是空的 —— 想表达「关思考」"
                              f"请写 off（留空等于不覆盖，会静默沿用座位层档位）")
                continue
            for issue in normalize_thinking(level, fill_defaults=False)[1]:
                # `normalize_thinking` 的报错文案里写死了「llm.thinking」（它是通用校验器）；
                # 这里换成「这一项」，免得预检说的是 llm.thinking、而配错的地方是 fallback_thinking。
                text = issue.replace("llm.thinking.", "这一项.").replace("llm.thinking ", "这一项")
                issues.append(f"换手档位配置（llm.fallback_thinking.{raw_name}）：{text}")
            if tag and tag not in known and not os.environ.get(f"{tag}_BASE_URL"):
                issues.append(f"llm.fallback_thinking 里的档案名 {raw_name} 没见过：既不在 "
                              f"llm.thinking_style 表里、也不在 game.fallback 名单里，"
                              f".env 里也没有 {tag}_BASE_URL —— 是不是名字写错了？")
    for seat in seats:
        if getattr(seat, "is_human", False):
            continue
        try:
            spec = resolve(cfg, seat)
        except Exception as exc:                          # noqa: BLE001
            # 兜底：任何"配置写法怪"导致的异常都要变成人话，不能让预检自己崩掉
            issues.append(f"{seat.id}号（{seat.role_key}）：配置解析失败 —— "
                          f"{type(exc).__name__}: {exc}")
            continue
        missing = []
        if not spec.base_url:
            missing.append("Base URL")
        if not spec.model:
            missing.append("模型名")
        if not spec.api_key:
            missing.append("API Key")
        if missing:
            issues.append(f"{seat.id}号（{seat.role_key}）：缺 {'/'.join(missing)}，"
                          f"检查 {env_hint(cfg, seat)}")
    return issues
