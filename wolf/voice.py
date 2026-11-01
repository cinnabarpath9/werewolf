# coding=utf-8
"""语音层：发言转语音（TTS）+ 把你说的话转成文字（ASR）。

为什么单独一个文件：语音这套接口的形状和"大模型那样 OpenAI 兼容"完全不一样，
踩过的坑都写在这里了（下面每条都是实测出来的，不是猜的）：

TTS —— 用百炼官方 SDK，但必须是 **tts_v2** 这个类：
    · `dashscope.audio.tts_v2.SpeechSynthesizer(model=..., voice=...)` 然后 `.call(文本)`
    · ⚠️ 不是 `dashscope.audio.qwen_tts.SpeechSynthesizer`（那个类收参数的方式不同，
      用错了会一直报 `url error`，看起来像权限问题，其实是类用错了）
    · 音色和模型是**绑定**的，写错直接报错；同一把 key 上实测可用的音色见 VOICES
    · 返回的是 MP3 字节

ASR —— 这个模型**没有** OpenAI 那种 /audio/transcriptions，走的是：
    POST {base}/api/v1/services/aigc/multimodal-generation/generation
    · 必须带 `X-DashScope-SSE: disable`
    · 音频放在 `input.messages[].content[].input_audio.data`，而且**必须是 data: URI**
      （裸 base64 会被当成 URL 去解析，报 `FILE_DOWNLOAD_FAILED: domain resolution failed`）
    · `parameters` 里要写对 `format`（wav/mp3/pcm…），写错会解码失败
    · 另一种做法（异步 filetrans + 公网 URL）我们**用不了**：手机在局域网，
      给不出阿里能下载的地址；用 SDK 传本地路径也不行（它会原样发路径，报
      `REQUEST_INVALID_FILE_URL_VALUE`）

⚠️ 密钥不进代码：都从环境变量读（.env 由 server.py 加载）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx

#: 实测在这个模型（qwen-audio-3.1-tts-flash）上可用的音色 —— 604 个候选名里只有这 12 个过。
#: 名字都是 `<拼音>_v3.1` 形式；不带后缀、或换成 OpenAI 那套（alloy/Cherry/Ethan）**一律报错**。
#: 想再多就只能走"声音复刻"（用一段真人录音克隆一个音色）。
VOICES: tuple[str, ...] = (
    "longanhuan_v3.1",     # 女声，温柔平和
    "longmiao_v3.1",       # 女声，偏甜
    "longxing_v3.1",       # 女声，清亮
    "longwan_v3.1",        # 女声，成熟
    "longhua_v3.1",        # 女声，稳重
    "longxiaoxia_v3.1",    # 女声，年轻
    "longyuan_v3.1",       # 女声
    "longjielidou_v3.1",   # 女声，偏活泼
    "longhuohuo_v3.1",     # 女声，偏明亮
    "longsanshu_v3.1",     # 男声，年长
    "longzhe_v3.1",        # 男声
    "longhan_v3.1",        # 男声，偏低沉
)

#: 座位 → 音色。座位 6 是真人，不用配。
#: 默认按"座位号"分配（不是按角色！），这样跨局同一个人声音不变，听感才稳定。
DEFAULT_SEAT_VOICES: dict[int, str] = {
    1: "longmiao_v3.1",
    2: "longzhe_v3.1",
    3: "longxiaoxia_v3.1",
    4: "longyuan_v3.1",
    5: "longsanshu_v3.1",
    7: "longxing_v3.1",
    8: "longhua_v3.1",
    9: "longwan_v3.1",
    10: "longhan_v3.1",
    11: "longmiao_v3.1",     # 十一人场才有 11 号；默认给个女声，config 里可覆盖
    # 备用（想换就改这里，或用 config.yaml 的 voice.seats 覆盖）：
    # longanhuan_v3.1 / longjielidou_v3.1 / longhuohuo_v3.1
}

#: 什么算"该配音的发言"。公告/投票结果不配（用户定的：那些用短提示音）。
SPEECH_KINDS = ("speech", "pk_speech")


class VoiceError(RuntimeError):
    """语音相关失败。message 是可以直接给用户看的人话。"""


def _redact(text: str) -> str:
    """把 key 从报错里抹掉再往外抛（报错会进日志、进手机屏幕）。

    思路跟 `wolf/llm.py` 的 `redact()` 一致（那边先修好，这里对齐），三层：
      ① 字段名白名单：api_key / access_key / secret / token / authorization / x-api-key
         这类**字段名**后面跟的值，一律抹掉 —— 不管它长什么样。
         为什么必须有这层（审计实测）：`key=<AWS-key>` 里没有 `sk-` 前缀、
         也不是 Bearer，老的两条正则原样放行。
      ② Bearer 后面的 token 允许含 base64 的 `+ / =`：
         老正则 `[A-Za-z0-9._\\-]+` 遇到 `+` `/` `=` 会只隐去一半，甚至一个字都不匹配。
      ③ 长随机串兜底：20 位以上、字母数字混着的裸 key（没有字段名、也没有 Bearer）。
    """
    # ⚠️ Bearer 那条必须在字段名白名单**之前**跑：`Authorization: Bearer abc+def/ghi==`
    # 里 `authorization` 后面第一个值就是 "Bearer" 这个词，白名单先跑会把它吃掉，
    # Bearer 规则就再也没机会匹配到真 token 了（llm.py 里同样踩过，一起修）。
    text = re.sub(r"(?i)\b(bearer)\s+[^\s\"',;}]{6,}", r"\1 <已隐去>", str(text or ""))
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|authorization|x-api-key)"
                  r"[\"'\s:=]{1,6})[^\s\"',;}]{6,}", r"\1<已隐去>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9._\-]{6,}", "sk-<已隐去>", text)
    text = re.sub(r"\b(?=[A-Za-z0-9+/=._\-]{20,}\b)(?=[^\s]*[0-9])(?=[^\s]*[A-Za-z])"
                  r"[A-Za-z0-9+/=._\-]{20,}", "<已隐去>", text)
    return text[:300]


@dataclass
class SeatVoice:
    """一个座位的声音：音色名 + 可选微调。

    为什么要这几项：预设音色一共就 12 个，而座位有 10~11 个，
    "换音色名"很快就没得换了。DashScope 的 SpeechSynthesizer 本身支持
    `speech_rate / pitch_rate / volume / instruction / seed` ——
    同一个音色加上不同的语速、音高、语气指令，听感就是不同的人，
    **不用装任何新东西、也不多花钱**（就是同一次合成）。

    `instruction` 是"用自然语言描述怎么念"（音色设计/指令控制），
    例："语气冷、语速偏慢、句尾不上扬"。留空就不发这个参数。
    """
    voice: str
    rate: float = 1.0            # 0.5~2.0；>1 更快
    pitch: float = 1.0           # 0.5~2.0；<1 更低
    volume: int = 50             # 0~100
    instruction: str = ""
    seed: int | None = None      # 固定 seed → 同一句话每次念得一样（复现用）

    def key(self) -> str:
        """进缓存键：改了语速/音高/指令，缓存必须失效（否则改了没反应）。"""
        return (f"{self.voice}|{self.rate:g}|{self.pitch:g}|{self.volume}"
                f"|{self.instruction}|{self.seed if self.seed is not None else ''}")

    def describe(self) -> str:
        bits = [self.voice]
        if self.rate != 1.0:
            bits.append(f"语速×{self.rate:g}")
        if self.pitch != 1.0:
            bits.append(f"音高×{self.pitch:g}")
        if self.volume != 50:
            bits.append(f"音量{self.volume}")
        if self.instruction:
            bits.append(f"指令「{self.instruction[:16]}」")
        return " · ".join(bits)

    def kwargs(self) -> dict:
        """只把**非默认值**发给 SDK：少发少错，也免得平台对默认值有别的解释。"""
        out: dict = {}
        if self.rate != 1.0:
            out["speech_rate"] = self.rate
        if self.pitch != 1.0:
            out["pitch_rate"] = self.pitch
        if self.volume != 50:
            out["volume"] = self.volume
        if self.instruction:
            out["instruction"] = self.instruction
        if self.seed is not None:
            out["seed"] = self.seed
        return out


#: 官方规范（2026-10-08 查证，见 `docs/HANDOFF.md` §33）：热词含非 ASCII 字符时
#: **总长 ≤15 字符**；纯 ASCII 时按空格切分 **≤7 段**。超了不会报错，但那个词等于白写。
HOT_WORD_MAX_CJK = 15
HOT_WORD_MAX_ASCII_SEGS = 7
#: 权重：1~5 是普通热词，**50 = 超级热词**（最多 50 个）。官方建议普通词先用 4。
HOT_WORD_WEIGHTS = (1, 2, 3, 4, 5, 50)
HOT_WORD_DEFAULT_WEIGHT = 4
HOT_WORD_MAX_SUPER = 50
HOT_WORD_MAX_TOTAL = 2000          # 每个模型上限（超了官方随机取 2000，不报错）


def parse_hot_words(raw) -> dict[str, int]:
    """把 config 里的热词表解析成 `{词: 权重}`（不合规的词**直接丢掉并打一行提示**）。

    支持两种写法（都写在 `config.yaml` 的 `voice.asr_hot_words`）：
        asr_hot_words: {预言家: 4, 悍跳: 4, 倒钩: 50}     # 显式权重
        asr_hot_words: [预言家, 女巫, 警徽]                # 只列词，权重默认 4

    为什么要在这里卡规范：官方对词形与权重有硬要求，写错了**不报错、只是静默失效** ——
    那种"以为加了热词其实没加"的坑，比不加热词更难查。
    """
    items: list[tuple[str, object]]
    if isinstance(raw, dict):
        items = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = [(str(k), HOT_WORD_DEFAULT_WEIGHT) for k in raw]
    else:
        return {}
    out: dict[str, int] = {}
    super_count = 0
    for word, weight in items:
        word = word.strip()
        if not word:
            continue
        if any(ord(ch) > 127 for ch in word):
            if len(word) > HOT_WORD_MAX_CJK:
                print(f"[voice] 热词太长（非 ASCII 上限 {HOT_WORD_MAX_CJK} 字符），已跳过：{word!r}")
                continue
        elif len(word.split()) > HOT_WORD_MAX_ASCII_SEGS:
            print(f"[voice] 热词段数太多（ASCII 上限 {HOT_WORD_MAX_ASCII_SEGS} 段），已跳过：{word!r}")
            continue
        try:
            num = int(weight)
        except (TypeError, ValueError):
            num = HOT_WORD_DEFAULT_WEIGHT
        if num not in HOT_WORD_WEIGHTS:
            # 权重写飞了就按普通词处理 —— 宁可弱一点，也别整条请求被平台拒掉
            num = HOT_WORD_DEFAULT_WEIGHT
        if num == 50:
            if super_count >= HOT_WORD_MAX_SUPER:
                num = HOT_WORD_DEFAULT_WEIGHT
            else:
                super_count += 1
        out[word] = num
        if len(out) >= HOT_WORD_MAX_TOTAL:
            break
    return out


@dataclass
class VoiceConfig:
    base_url: str = ""
    api_key: str = ""
    tts_model: str = "qwen-audio-3.1-tts-flash"
    asr_model: str = "qwen-audio-3.1-asr-flash"
    seat_voices: dict[int, SeatVoice] = field(default_factory=dict)
    #: ASR **即时热词**：`{词: 权重}`。`qwen-audio-3.x-asr-flash` 系列支持，
    #: 权重 1~5（官方建议 4 起），**50 = 超级热词**（召回率大幅提升，最多 50 个）。
    #: ⚠️ `qwen3-asr-flash` 那一路**不支持热词**、参数名也不同（game.asr_model 换模型时
    #: 记得一起清空这里，否则等于给不支持的模型塞一个它不认的字段）。
    asr_vocabulary: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, cfg: dict | None = None) -> "VoiceConfig":
        cfg = cfg or {}
        voice_block = cfg.get("voice") or {}
        seats: dict[int, SeatVoice] = {sid: SeatVoice(voice=name)
                                       for sid, name in DEFAULT_SEAT_VOICES.items()}
        for k, v in (voice_block.get("seats") or {}).items():
            try:
                sid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict):
                # 新版写法：{voice: longmiao_v3.1, rate: 1.15, instruction: "语气冷"}
                name = str(v.get("voice") or v.get("name") or "").strip()
                if not name:
                    continue
                seats[sid] = SeatVoice(
                    voice=name,
                    rate=float(v.get("rate") or v.get("speech_rate") or 1.0),
                    pitch=float(v.get("pitch") or v.get("pitch_rate") or 1.0),
                    volume=int(v.get("volume") or 50),
                    instruction=str(v.get("instruction") or "").strip(),
                    seed=(int(v["seed"]) if v.get("seed") is not None else None),
                )
            else:
                # 老写法：直接写音色名（照样能用）
                seats[sid] = SeatVoice(voice=str(v))
        return cls(
            base_url=(os.environ.get("VOICE_BASE_URL") or "").rstrip("/"),
            api_key=os.environ.get("VOICE_API_KEY") or "",
            tts_model=os.environ.get("TTS_MODEL") or "qwen-audio-3.1-tts-flash",
            asr_model=os.environ.get("ASR_MODEL") or "qwen-audio-3.1-asr-flash",
            seat_voices=seats,
            asr_vocabulary=parse_hot_words(voice_block.get("asr_hot_words")),
        )

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key)

    def seat_voice(self, seat: int) -> SeatVoice:
        got = self.seat_voices.get(seat)
        if got is not None:
            return got
        return SeatVoice(voice=VOICES[0])

    def voice_of(self, seat: int) -> str:
        """兼容老接口：只要音色名。"""
        return self.seat_voice(seat).voice

    def problems(self) -> list[str]:
        out = []
        if not self.base_url:
            out.append("没配 VOICE_BASE_URL（语音服务的地址）")
        if not self.api_key:
            out.append("没配 VOICE_API_KEY（语音服务的 key）")
        for seat, sv in sorted(self.seat_voices.items()):
            v = sv.voice
            if v not in VOICES and not re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", v or ""):
                out.append(f"座位 {seat} 的音色名 {v!r} 不像有效值（应为 <拼音>_v3.1 形式）")
            if not (0.5 <= sv.rate <= 2.0):
                out.append(f"座位 {seat} 的语速 ×{sv.rate:g} 超出 0.5~2.0")
            if not (0.5 <= sv.pitch <= 2.0):
                out.append(f"座位 {seat} 的音高 ×{sv.pitch:g} 超出 0.5~2.0")
            if not (0 <= sv.volume <= 100):
                out.append(f"座位 {seat} 的音量 {sv.volume} 超出 0~100")
        return out


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #

_tts_lock = asyncio.Lock()          # SDK 是同步阻塞的，串行化以免卡住事件循环


def _synth_blocking(model: str, sv: "SeatVoice", text: str, api_key: str) -> bytes:
    import dashscope                                     # 延迟导入：不装也能跑没语音的局
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    dashscope.api_key = api_key
    # ⚠️ 必须把 WS 地址改掉，否则 SDK 会去连阿里官方域名。
    # 2026-10-31：以前默认写死成项目作者的 `maas.qianwenaiapi.com` 中转；开源之后
    #   别人填的是官方 DashScope 地址，TTS 却仍然去连那台中转 —— 看起来像 key 不对。
    #   现在按 VOICE_BASE_URL 推（https→wss + /api-ws/v1/inference），
    #   `VOICE_WS_URL` 仍可显式覆盖；都没配才回落到官方地址。
    base = (os.environ.get("VOICE_BASE_URL") or "").rstrip("/")
    derived = ""
    if base:
        derived = base.replace("https://", "wss://").replace("http://", "ws://") + "/api-ws/v1/inference"
    dashscope.base_websocket_api_url = (
        os.environ.get("VOICE_WS_URL") or derived or "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
    # 只发非默认的微调项（语速/音高/音量/语气指令/seed），见 SeatVoice.kwargs
    synth = SpeechSynthesizer(model=model, voice=sv.voice, **sv.kwargs())
    audio = synth.call(text)
    if not audio:
        raise VoiceError("语音合成没返回音频（音色名或模型名可能不对）")
    return audio


async def tts(text: str, voice: str, cfg: VoiceConfig, seat: int | None = None) -> bytes:
    """把一段文字合成为 MP3 字节。失败抛 VoiceError（带人话）。

    `voice` 可以给音色名字符串（老用法），也可以给座位号（会连带那套语速/音高/语气指令）。
    """
    if not cfg.ready:
        raise VoiceError("语音服务还没配好（缺地址或 key）")
    clean = (text or "").strip()
    if not clean:
        raise VoiceError("没内容可合成")
    if len(clean) > 600:
        clean = clean[:600]          # 发言本来就有字数限制，这里再兜一层
    sv = cfg.seat_voice(seat) if seat is not None else SeatVoice(voice=voice)
    try:
        async with _tts_lock:
            return await asyncio.to_thread(_synth_blocking, cfg.tts_model, sv, clean, cfg.api_key)
    except VoiceError:
        raise
    except Exception as exc:                              # noqa: BLE001
        raise VoiceError(f"语音合成失败：{_redact(f'{type(exc).__name__}: {exc}')}") from None


# --------------------------------------------------------------------------- #
# ASR
# --------------------------------------------------------------------------- #

#: 手机录音常见的容器 → 告诉服务端该怎么解码
_EXT_FORMAT = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-mp3": "mp3",
    "audio/webm": "webm", "video/webm": "webm",
    "audio/ogg": "ogg", "application/ogg": "ogg", "audio/opus": "opus",
    "audio/mp4": "mp4", "video/mp4": "mp4", "audio/m4a": "mp4", "audio/x-m4a": "mp4",
    # ⚠️ 注意：MIME 里 **没有** `audio/m4a` 这种正式写法，浏览器给的是 `audio/mp4`
    # （音频轨是 AAC）。这里把 m4a → mp4 归一到服务端认得的名字。
    "audio/aac": "aac", "audio/x-aac": "aac", "audio/aacp": "aac",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/pcm": "pcm", "audio/x-pcm": "pcm", "audio/l16": "pcm",
    # 手机另一路常见格式（安卓录音笔、功能机、微信语音）
    "audio/3gpp": "3gp", "video/3gpp": "3gp", "audio/3gpp2": "3gp",
    "audio/amr": "amr", "audio/x-amr": "amr",
}


def _sniff_format(data: bytes) -> str:
    """只看文件头猜格式；认不出来返回空串。

    ⚠️ 顺序重要：MP3 的 `0xFFF3` 跟 ADTS 的 `0xFFF1` 都带 0xFFF 同步字，
    所以 MP3 先判、AAC 后判，各判各的位。
    """
    if not data:
        return ""
    head = bytes(data[:16])
    if head[:4] == b"RIFF":
        return "wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "mp3"
    if head[:4] == b"\x1a\x45\xdf\xa3":                 # EBML（webm/mkv 的容器头）
        return "webm"
    if len(head) >= 8 and head[4:8] == b"ftyp":         # ISO-BMFF：mp4 / m4a / 3gp
        brand = head[8:12]
        if brand.startswith(b"3g"):
            return "3gp"
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:2] == b"\xff\xf1":                         # ADTS 的 syncword（MPEG-4 AAC）
        return "aac"
    return ""


def guess_format(mime: str, data: bytes = b"") -> str:
    """猜音频格式：**先看文件头魔数，认不出来才信 Content-Type**。

    为什么要猜：ASR 的 `parameters.format` 写错会直接解码失败，
    而手机浏览器录出来的可能是 webm/opus 或 mp4/aac，不是 wav。

    ⚠️ 为什么魔数优先（审计实测反例）：手机端把 Content-Type 写成了 `audio/webm`，
    但实际发的是 mp4 容器（`\x00\x00\x00\x18ftypM4A`）—— 按声明值返回 "webm"，
    服务端就会拿 webm 解码器解 mp4，直接解码失败。**文件头比声明可信**。
    """
    hit = _sniff_format(data)
    if hit:
        return hit
    return _EXT_FORMAT.get((mime or "").split(";")[0].strip().lower(), "") or "wav"


def _asr_payload(cfg: VoiceConfig, fmt: str, data_uri: str) -> dict:
    """ASR 的请求体（抽成函数是为了能被**离线断言** —— 见 `tests/t_voice.py` 第四节）。

    ⚠️ 只在词表非空时才带 `vocabulary`：`qwen3-asr-flash` 那一路不支持热词、参数名也不同，
    给它塞一个不认的字段，轻则被忽略、重则整个请求 400。
    """
    params: dict = {"format": fmt, "sample_rate": "16000"}
    if cfg.asr_vocabulary:
        params["vocabulary"] = dict(cfg.asr_vocabulary)
    return {
        "model": cfg.asr_model,
        "input": {"messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_uri}}]}]},
        "parameters": params,
    }


async def asr(data: bytes, mime: str, cfg: VoiceConfig, fmt: str = "") -> str:
    """把音频转成文字。返回识别结果（可能为空字符串=没听到人声）。"""
    if not cfg.ready:
        raise VoiceError("语音服务还没配好（缺地址或 key）")
    if not data:
        raise VoiceError("没收到音频")
    fmt = fmt or guess_format(mime, data)
    b64 = base64.b64encode(data).decode()
    # ⚠️ 必须写成 data: URI。裸 base64 会被服务端当成 URL 去解析 → FILE_DOWNLOAD_FAILED
    data_uri = f"data:audio/{fmt};base64,{b64}"
    url = f"{cfg.base_url}/api/v1/services/aigc/multimodal-generation/generation"
    payload = _asr_payload(cfg, fmt, data_uri)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",        # ⚠️ 少了它拿不到一次性结果
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception as exc:                              # noqa: BLE001
        raise VoiceError(f"连不上语音服务：{_redact(f'{type(exc).__name__}: {exc}')}") from None
    body = resp.text or ""
    if resp.status_code != 200:
        # 语音这块的报错是 JSON，能捞出来就捞
        try:
            msg = json.loads(body).get("message") or body
        except Exception:                                 # noqa: BLE001
            msg = body
        if "HAVE_NO_WORDS" in body:
            return ""                                     # 静音/没说话：不算错误
        raise VoiceError(f"识别失败（HTTP {resp.status_code}）：{_redact(str(msg))}")
    try:
        j = resp.json()
    except Exception:                                     # noqa: BLE001
        raise VoiceError("识别服务返回的不是 JSON") from None
    sent = j.get("sentence") or {}
    text = (sent.get("text") or "").strip()
    return text


#: 中文数字 → 阿拉伯数字。ASR 有时给"四号"有时给"4号"，统一成数字才好解析目标。
_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 座位相关的动词。数字**紧挨着**这些字（前面或后面）时，才算"在说号码"。
#: 只收"动作 + 座位"这个搭配在狼人杀里说得通的字。
#: ⚠️ 故意不收 `走`：`"我们先走3个人"` 里的 "走3" 会被误判成 3 号 ——
#: 多收一个字省下的那点召回，不值得拿"投错人"去换。
_ACTION_WORDS = "投刀验查枪守出杀带毒救票打猎崩"

#: 「…号」：数字后面紧跟"号"才算候选。
#: ⚠️ 中文那条的分支顺序必须是"长的在前"：`十二号` 要一次吃掉两个字，
#: 否则 `十二号` 会被拆成 `十`（=10）—— 数字错一位，票就投给别人了。
_RE_SEAT_AR = re.compile(r"(\d{1,2})\s*号")
_RE_SEAT_CN = re.compile(r"([零一两二三四五六七八九十]{1,2})\s*号")
#: 没有"号"时的兜底：数字前后紧邻动词（`投4` / `4投` / `出 7`）。
#: 两条分支写在同一个 alternation 里，`finditer` 才会"从左往右、取最先出现的那个"。
_RE_SEAT_VERB = re.compile(r"(?:[" + _ACTION_WORDS + r"]\s*(\d{1,2})(?!\d))"
                           r"|(?:(?<!\d)(\d{1,2})\s*[" + _ACTION_WORDS + r"])")


def _cn_seat(s: str) -> int | None:
    """中文数字串 → 数字；`十二` → 12。认不出来返回 None。"""
    n = _CN_NUM.get(s)
    if n is None and len(s) == 2 and s[0] == "十":
        n = 10 + _CN_NUM.get(s[1], 0)
    return n


def _first_in(values, legal_set: set[int]) -> int | None:
    for n in values:
        if n in legal_set:
            return n
    return None


def parse_seat(text: str, legal: list[int]) -> int | None:
    """从一句话里猜"他要投/打几号"。

    只认明确提到号码的说法（"投4号""打三号""出 7"），**不做模糊猜测** ——
    猜错的代价是把票投给无辜的人，比让用户重点一次按钮糟得多。
    返回 None 表示"没把握"，调用方应该让用户自己确认。

    ⚠️ 不能"见数字就当号码"（审计实测）：合法目标是 1..9 时，`'我觉得2个狼'`
    里的 "2 个" 会被当成"投 2 号" —— 这是一句**分析**，不是投票。
    所以：① 数字后必须紧跟"号"；② 没有"号"时，只有数字前后紧邻
    `投/刀/验/枪/守/出/杀/带` 这类动词才算（后面是"个/张/点/天"就肯定不是号码）。

    顺序：先找带"号"的（最明确），再找挨着动词的；同类里取**第一个**。
    """
    if not text:
        return None
    legal_set = set(legal)
    # ① 阿拉伯数字 + 号（含 "4号" "4 号" "10号"）
    hit = _first_in((int(m.group(1)) for m in _RE_SEAT_AR.finditer(text)), legal_set)
    if hit is not None:
        return hit
    # ② 中文数字 + 号（"四号" "十二号"）
    hit = _first_in((n for n in (_cn_seat(m.group(1)) for m in _RE_SEAT_CN.finditer(text))
                     if n is not None), legal_set)
    if hit is not None:
        return hit
    # ③ 没有"号"：数字必须紧挨着动词（"投4""4投""出 7"）
    hit = _first_in((int(m.group(1) or m.group(2)) for m in _RE_SEAT_VERB.finditer(text)),
                    legal_set)
    if hit is not None:
        return hit
    # ⚠️ 这里就是终点：不认"光秃秃一个数字"。没有"号"、也不挨动词的数字（"剩余3个人"）
    # 一律返回 None —— 让用户自己点一下，比猜个号码投错人便宜得多。
    return None
