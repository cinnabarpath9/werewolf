# coding=utf-8
"""本地设置存储：API 平台、座位分配、语音服务。

为什么要有这一层
----------------
以前的"配平台"只能手改 `.env`，座位分配只能手改 `config.yaml`。
手机上虽然能填 key（`/api/settings/env`），但那个接口刻意只允许写
`<档案>_BASE_URL / _API_KEY / _MODEL`，写不了座位路由；而且改完 `config.yaml`
必须重启。开源之后，新用户上手第一件事就是"填一个 API、把座位都指过去"——
这两步必须能在页面上完成、且立刻生效。

数据放在 `settings.local.json`（与 `.env` 同级，**已在 .gitignore 里**）：
    {
      "version": 1,
      "apis": [{"id": "DEEPSEEK", "name": "DeepSeek", "base_url": "…",
                "model": "…", "key": "…", "style": ""}],
      "seats": {"1": "DEEPSEEK", "2": "DEEPSEEK"},
      "voice": {"base_url": "…", "api_key": "…",
                "tts_model": "…", "asr_model": "…"}
    }

加载顺序：`.env` → `settings.local.json`。后者覆盖前者（同一个档案名时）。
这样既保留老用户"全写在 .env"的习惯，又让页面保存的值说了算。

⚠️ 这个文件里有明文 key，跟 `.env` 一个级别：不要提交、不要发到群里、
   不要打进发布包（`tools/check_release.py` 的 `.env*` 规则不管它，
   所以 `.gitignore` 里单独挡了一条）。
"""

from __future__ import annotations

import json
import os
import re
import threading

FILENAME = "settings.local.json"
MAX_KEY = 4096
MAX_NAME = 64
MAX_MODEL = 160
MAX_URL = 2048

#: 档案标识（同时也是 .env 变量前缀）：字母数字开头，允许 . - _。
#: 与 server.py 的 SAFE_NAME_RE 保持同一口径。
SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,47}$")

#: `llm.thinking_style` 认的风格。空字符串 = 按 Base URL / 模型名自动猜。
STYLE_CHOICES = ("openai", "qwen", "ark", "glm", "mimo")

#: 每条 API 可以单独覆盖的「打法参数」（2026-10-31 加，页面的「模型参数」二级页）。
#: 名字 → (类型, 最小值, 最大值)。**空着 = 跟随 config.yaml 的内置默认**：
#: 温度 1.0 / max_tokens 16000 / 超时 450 秒 / 重试 4 次 / 卡住 25 秒 / 思考按档位表。
#: 为什么做成"每条 API 一套"：用户接的渠道各不相同，把作者那套硬套过去不一定对
#: （有的中转吞 `reasoning_effort`、有的思考链前 60 秒一个字节都不吐）。
TUNING_NUMS = {
    "temperature": (float, 0.0, 2.0),
    "max_tokens": (int, 1000, 64000),
    "timeout": (float, 30.0, 1800.0),
    "max_retries": (int, 0, 10),
    "idle_timeout": (float, 5.0, 120.0),
}
#: 思考档：空 = 按任务档位表（config 的 llm.thinking），其余四档直传引擎。
THINKING_CHOICES = ("off", "low", "high")

_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #

def empty() -> dict:
    #: `butler` = 托管模型（2026-11-02）：玩家不想自己决策时，调它替玩家出一手。
    return {"version": 1, "apis": [], "seats": {}, "voice": {}, "butler": {}}


def _text(value, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def normalize_id(value) -> str:
    """把用户填的名字变成档案标识。中文之类会产生 ValueError（交给页面提示）。"""
    text = str(value or "").strip().upper()
    if not SAFE_ID.match(text):
        raise ValueError("API 名只能用字母、数字、下划线、点和减号（例如 DEEPSEEK）")
    return text


def normalize_tuning(item) -> dict:
    """把「模型参数」页填的东西收敛成 dict。空值/越界/写错类型一律**丢掉**（= 跟随默认）。

    ⚠️ 越界不夹到边界：夹了用户会以为"我填 5000 秒生效了"，其实被改成 1800。
    丢掉的话那一项等于没填，页面上的提示也写着"留空 = 跟随内置默认"。
    """
    if not isinstance(item, dict):
        return {}
    out: dict = {}
    for name, (typ, lo, hi) in TUNING_NUMS.items():
        raw = item.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = typ(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
        if val < lo or val > hi:
            continue
        out[name] = val
    thinking = str(item.get("thinking") or "").strip().lower()
    if thinking in THINKING_CHOICES:
        out["thinking"] = thinking
    return out


def normalize_api(item) -> dict:
    if not isinstance(item, dict):
        raise ValueError("API 配置必须是一个对象")
    api_id = normalize_id(item.get("id") or item.get("name"))
    base_url = _text(item.get("base_url"), MAX_URL).rstrip("/")
    if base_url and not re.match(r"^https?://", base_url, re.I):
        raise ValueError(f"{api_id} 的接口地址必须以 http:// 或 https:// 开头")
    style = _text(item.get("style"), 16).lower()
    if style not in STYLE_CHOICES:
        style = ""
    return {
        "id": api_id,
        "name": _text(item.get("name"), MAX_NAME) or api_id,
        "base_url": base_url,
        "model": _text(item.get("model"), MAX_MODEL),
        "key": _text(item.get("key"), MAX_KEY),
        "style": style,
        "tuning": normalize_tuning(item.get("tuning")),
    }


def normalize_voice(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    voice = {
        "base_url": _text(raw.get("base_url"), MAX_URL).rstrip("/"),
        "api_key": _text(raw.get("api_key"), MAX_KEY),
        "tts_model": _text(raw.get("tts_model"), MAX_MODEL),
        "asr_model": _text(raw.get("asr_model"), MAX_MODEL),
    }
    return {k: v for k, v in voice.items() if v}


def normalize(raw) -> dict:
    """把磁盘上的东西洗成可信结构：坏条目丢掉，不因为一条脏数据整个设置打不开。"""
    data = empty()
    if not isinstance(raw, dict):
        return data
    seen: dict[str, int] = {}
    for item in raw.get("apis") or []:
        try:
            api = normalize_api(item)
        except ValueError:
            continue
        if api["id"] in seen:
            data["apis"][seen[api["id"]]] = api     # 同名后来者覆盖
        else:
            seen[api["id"]] = len(data["apis"])
            data["apis"].append(api)
    seats = raw.get("seats")
    if isinstance(seats, dict):
        for key, value in seats.items():
            try:
                seat = int(str(key).strip())
            except (TypeError, ValueError):
                continue
            if not 1 <= seat <= 12:
                continue
            try:
                pid = normalize_id(value)
            except ValueError:
                continue
            data["seats"][str(seat)] = pid
    data["voice"] = normalize_voice(raw.get("voice"))
    data["butler"] = normalize_butler(raw.get("butler"))
    return data


def normalize_butler(raw) -> dict:
    """托管模型的设置：`{api, thinking, temperature?, max_tokens?}`。

    · `api` 必须是一条已配置 API 的标识（空 = 没开托管）；
    · `thinking` 只认 off/low/high，别的丢掉（= 跟随该 API 自己的默认）；
    · 数字项越界就丢（与「模型参数」页同一口径：不夹边界，免得用户以为生效了）。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    try:
        name = normalize_id(raw.get("api"))
    except ValueError:
        name = ""
    if name:
        out["api"] = name
    level = str(raw.get("thinking") or "").strip().lower()
    if level in THINKING_CHOICES:
        out["thinking"] = level
    tun = normalize_tuning({"temperature": raw.get("temperature"),
                            "max_tokens": raw.get("max_tokens")})
    out.update(tun)
    return out


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #

def path(base_dir: str) -> str:
    return os.path.join(base_dir, FILENAME)


def load(base_dir: str) -> dict:
    """读本地设置。文件不存在/写坏都返回空设置（服务照常能起）。"""
    full = path(base_dir)
    if not os.path.exists(full):
        return empty()
    try:
        with open(full, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[settings] 读不了 {full}（{type(exc).__name__}: {exc}），这次按空设置处理")
        return empty()
    return normalize(raw)


def save(base_dir: str, data: dict) -> str:
    """原子写盘：临时文件 + os.replace。Windows 上被占用时退一步重试。"""
    data = normalize(data)
    full = path(base_dir)
    tmp = f"{full}.tmp{os.getpid()}"
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with _LOCK:
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            for i in range(12):
                try:
                    os.replace(tmp, full)
                    break
                except OSError:
                    if i >= 11:
                        raise
                    import time as _time
                    _time.sleep(0.03 * (i + 1))
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return full


# --------------------------------------------------------------------------- #
# 生效
# --------------------------------------------------------------------------- #

def guess_style(base_url: str, model: str) -> str:
    """按地址/模型名猜这家认哪种思考参数。

    猜错不会报错，只会让「思考档位」不生效或发错参数 —— 所以页面允许手动指定。
    """
    blob = f"{base_url or ''} {model or ''}".lower()
    if "dashscope" in blob or "qwen" in blob:
        return "qwen"
    if "volces" in blob or "ark" in blob or "doubao" in blob:
        return "ark"
    if "bigmodel" in blob or "zhipu" in blob or "glm" in blob:
        return "glm"
    if "mimo" in blob or "xiaomi" in blob:
        return "mimo"
    return "openai"


def apply(cfg: dict | None, data: dict, clear_missing: bool = False) -> dict:
    """把设置写进 `os.environ` 与 `cfg`，让后面的路由立刻能用。

    返回一份"实际用到的风格"表（给接口回显/日志用）。

    `clear_missing`：**只有"用户在页面上保存"这条路**才传 True。
      · True：页面里空着的 base_url / model / 语音四项，会把同名环境变量**删掉**
        （2026-10-31 外部审查 P1-03：以前只写不删，用户清空后旧地址旧 key 继续生效到重启）；
      · False（启动时）：不动环境变量 —— 启动时 `.env` 刚读进来，此刻"空"只表示
        "页面没配这一项"，删掉等于把 `.env` 里配好的东西抹了。
    注意 **key 永远不删**：契约是"编辑时留空 = 保留旧 key"。
    """
    data = normalize(data)
    styles: dict[str, str] = {}
    llm = None
    if isinstance(cfg, dict):
        llm = cfg.setdefault("llm", {})
        if not isinstance(llm, dict):
            llm = {}
            cfg["llm"] = llm
    table = None
    if llm is not None:
        table = llm.setdefault("thinking_style", {})
        if not isinstance(table, dict):
            table = {}
            llm["thinking_style"] = table

    for api in data["apis"]:
        pid = api["id"]
        for suffix, value in (("_BASE_URL", api["base_url"]), ("_MODEL", api["model"])):
            if value:
                os.environ[f"{pid}{suffix}"] = value
            elif clear_missing:
                os.environ.pop(f"{pid}{suffix}", None)
        if api["key"]:
            os.environ[f"{pid}_API_KEY"] = api["key"]
        style = api["style"] or guess_style(api["base_url"], api["model"])
        styles[pid] = style
        if table is not None:
            table[pid] = style

    # 座位路由：`routing.seat_profile_overlay` 认的就是这个变量。
    # 空映射时必须删掉（否则页面上"全部清空"之后老路由还在生效）。
    if data["seats"]:
        os.environ["WOLF_SEAT_PROFILES"] = ",".join(
            f"{seat}:{pid}" for seat, pid in sorted(data["seats"].items(), key=lambda kv: int(kv[0])))
    else:
        os.environ.pop("WOLF_SEAT_PROFILES", None)

    # 每条 API 的「模型参数」：routing 解析座位时会叠加到 seat 块上（空项不写，
    # 于是没填的仍走 config.yaml 的内置默认）。为空就删掉变量，避免旧值残留。
    tuning = {api["id"]: api["tuning"] for api in data["apis"] if api.get("tuning")}
    if tuning:
        os.environ["WOLF_API_TUNING"] = json.dumps(tuning, ensure_ascii=False,
                                                   separators=(",", ":"), sort_keys=True)
    else:
        os.environ.pop("WOLF_API_TUNING", None)

    voice = data["voice"]
    voice_env = {
        "VOICE_BASE_URL": voice.get("base_url", ""),
        "VOICE_API_KEY": voice.get("api_key", ""),
        "TTS_MODEL": voice.get("tts_model", ""),
        "ASR_MODEL": voice.get("asr_model", ""),
    }
    for name, value in voice_env.items():
        if value:
            os.environ[name] = value
        elif clear_missing and name != "VOICE_API_KEY":   # key 不删，同上面的口径
            os.environ.pop(name, None)
    return styles


def forget(api_id: str) -> None:
    """删掉一个 API 之后，把它留在进程环境里的三件套也清掉。

    ⚠️ 只清**进程内**；`.env` 里同名档案下次启动还会被读回来 —— 这是有意的：
       页面删的是"页面保存的那条"，不是替用户改 `.env`。
    """
    pid = str(api_id or "").strip().upper()
    if not SAFE_ID.match(pid):
        return
    for suffix in ("_BASE_URL", "_API_KEY", "_MODEL"):
        os.environ.pop(pid + suffix, None)


# --------------------------------------------------------------------------- #
# 页面用的读写小工具
# --------------------------------------------------------------------------- #

def _tail(value: str) -> str:
    value = str(value or "")
    return value[-4:] if len(value) > 4 else ("****" if value else "")


def public(data: dict) -> dict:
    """给前端的版本：key 只回后四位。"""
    data = normalize(data)
    apis = []
    for api in data["apis"]:
        key = api["key"] or os.environ.get(f"{api['id']}_API_KEY", "")
        apis.append({
            "id": api["id"],
            "name": api["name"],
            "base_url": api["base_url"],
            "model": api["model"],
            "has_key": bool(key),
            # 2026-10-31 外部审查 S18：key 存在 `.env` 里时也要能看出尾号（以前恒空）。
            "key_tail": _tail(key) if key else "",
            "style": api["style"],
            "tuning": dict(api.get("tuning") or {}),
            "source": "local",
        })
    voice = data["voice"]
    voice_key = voice.get("api_key") or os.environ.get("VOICE_API_KEY", "")
    return {
        "apis": apis,
        "seats": dict(data["seats"]),
        "voice": {
            "base_url": voice.get("base_url") or os.environ.get("VOICE_BASE_URL", ""),
            "tts_model": voice.get("tts_model") or os.environ.get("TTS_MODEL", ""),
            "asr_model": voice.get("asr_model") or os.environ.get("ASR_MODEL", ""),
            "has_key": bool(voice_key),
            "key_tail": _tail(voice_key) if voice_key else "",
        },
    }


def upsert_api(data: dict, payload: dict) -> dict:
    """新增/修改一条 API。key 留空 = 保留原来的（页面不回显 key，这是唯一能编辑的办法）。

    说明：新 API **可以不填 key 先存**（地址/模型先落盘，key 稍后补）。页面据此显示
    「已有地址、还没有 key」；以前这里留了一个 `if not api["key"]: pass` 的空分支，
    注释说"要让人知道现在还不能用"其实什么都没做（2026-10-31 外部审查 D 批 P2），已删。
    """
    data = normalize(data)
    api = normalize_api(payload)
    for i, old in enumerate(data["apis"]):
        if old["id"] != api["id"]:
            continue
        if not api["key"]:
            api["key"] = old["key"]
        # 页面每次都带 style 字段（空串 = 自动），所以只有"没带"才保留旧值 ——
        # 不然用户把风格改回「自动」永远不生效。
        if "style" not in payload:
            api["style"] = old["style"]
        # 参数页没带 tuning 这个键 = 不动原来的；带了（哪怕是空 dict）= 整份替换，
        # 这样"恢复默认"才能真的清空。
        if "tuning" not in payload:
            api["tuning"] = old.get("tuning") or {}
        if not payload.get("name"):
            api["name"] = old["name"]
        data["apis"][i] = api
        return data
    data["apis"].append(api)
    return data


def delete_api(data: dict, api_id: str) -> dict:
    data = normalize(data)
    pid = normalize_id(api_id)
    data["apis"] = [a for a in data["apis"] if a["id"] != pid]
    data["seats"] = {k: v for k, v in data["seats"].items() if v != pid}
    return data


def set_seats(data: dict, seats) -> dict:
    data = normalize(data)
    if not isinstance(seats, dict):
        raise ValueError("seats 必须是对象")
    clean: dict[str, str] = {}
    for key, value in seats.items():
        if value in (None, ""):
            continue
        seat = int(str(key).strip())
        if not 1 <= seat <= 12:
            raise ValueError("座位号只能是 1~12")
        clean[str(seat)] = normalize_id(value)
    data["seats"] = clean
    return data


def set_voice(data: dict, payload) -> dict:
    data = normalize(data)
    if not isinstance(payload, dict):
        raise ValueError("voice 必须是对象")
    old = data.get("voice") or {}
    voice = dict(old)
    for key in ("base_url", "tts_model", "asr_model"):
        if key in payload:
            voice[key] = _text(payload.get(key), MAX_URL if key == "base_url" else MAX_MODEL).rstrip("/")
    if payload.get("api_key"):
        voice["api_key"] = _text(payload.get("api_key"), MAX_KEY)
    data["voice"] = {k: v for k, v in voice.items() if v}
    return data
