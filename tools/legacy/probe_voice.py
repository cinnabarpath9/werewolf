# coding=utf-8
"""验一下网关的语音能力：TTS 能不能出声、ASR 能不能听写、音色对不对得上。

为什么需要它：
  · 网关的语音端点是 OpenAI 那套（/v1/audio/speech、/v1/audio/transcriptions），
    但**每把 key 是逐模型授权的** —— 没授权时报的是 403「no access to model」，
    看起来像"功能不存在"，其实是权限。
  · 音色和 TTS 模型是**绑定**的（`longanhuan_v3.1` 这类音色只在特定模型下有效），
    写错了直接 InvalidParameter。这个脚本把"音色 × 模型"逐个试一遍，输出可用矩阵。

跑法：
    python tools/probe_voice.py                          # 用 .env 里 TTS_* 那组
    python tools/probe_voice.py --tts-model tts-1 --voices alloy,echo,nova
    python tools/probe_voice.py --asr-model whisper-1    # 顺带做一次 TTS→ASR 往返

它会往 %TEMP% 里写一个 wav 供试听（不写进项目目录）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):          # GBK 控制台友好
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass

import httpx  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.environ.get("TEMP") or "/tmp"

#: 常见音色名，纯粹用来"试"，不通就说明这个模型不认它
DEFAULT_VOICES = [
    "alloy", "echo", "fable", "onyx", "nova", "shimmer",   # OpenAI 系
    "longxiaochun", "longxiaochun_v2", "longanhuan", "longanhuan_v3.1",
    "longwan", "longcheng", "longhua", "zhichu", "zhiyan",  # 阿里/百炼系
    "male-qn-qingse", "female-shaonv", "presenter_male",    # MiniMax 系
]


def load_env_file(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def pick(env: dict, *names: str) -> str:
    for n in names:
        if env.get(n):
            return env[n]
    return os.environ.get(names[0], "")


def probe_tts(client, base, key, model, voice, text) -> dict:
    started = time.time()
    try:
        r = client.post(f"{base}/audio/speech",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": model, "input": text, "voice": voice})
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}", "secs": round(time.time() - started, 1)}
    secs = round(time.time() - started, 1)
    if r.status_code == 200 and r.content:
        ctype = r.headers.get("content-type", "")
        return {"ok": True, "bytes": len(r.content), "ctype": ctype, "secs": secs,
                "audio": r.content}
    return {"ok": False, "status": r.status_code, "secs": secs,
            "why": re.sub(r"\s+", " ", r.text)[:160]}


def write_wav(raw: bytes, path: str) -> str:
    """把返回的音频落盘；返回的若不是 wav 就原样写（后缀按 content-type 猜）。"""
    ext = ".wav" if raw[:4] == b"RIFF" else ".mp3"
    out = os.path.splitext(path)[0] + ext
    with open(out, "wb") as fh:
        fh.write(raw)
    return out


def probe_asr(client, base, key, model, audio_path) -> dict:
    started = time.time()
    try:
        with open(audio_path, "rb") as fh:
            r = client.post(f"{base}/audio/transcriptions",
                            headers={"Authorization": f"Bearer {key}"},
                            files={"file": (os.path.basename(audio_path), fh, "audio/wav")},
                            data={"model": model})
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
    secs = round(time.time() - started, 1)
    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:                         # noqa: BLE001
            data = {"text": r.text[:200]}
        return {"ok": True, "secs": secs, "text": (data.get("text") or "").strip()[:200]}
    return {"ok": False, "status": r.status_code, "secs": secs,
            "why": re.sub(r"\s+", " ", r.text)[:160]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=os.path.join(HERE, ".env"))
    parser.add_argument("--base-url", default="", help="不填就读 .env 的 TTS_BASE_URL")
    parser.add_argument("--key", default="", help="不填就读 .env 的 TTS_API_KEY")
    parser.add_argument("--tts-model", default="", help="不填就读 .env 的 TTS_MODEL")
    parser.add_argument("--asr-model", default="", help="不填就读 .env 的 ASR_MODEL")
    parser.add_argument("--voices", default="", help="逗号分隔；不填就试一整套常见音色")
    parser.add_argument("--text", default="天黑请闭眼，昨晚是一个平安夜。")
    parser.add_argument("--yes", action="store_true",
                        help="确认执行：会真实调用语音服务（按字符计费）。不加它只打印计划")
    args = parser.parse_args()

    env = load_env_file(args.env)
    # ⚠️ 变量名要跟项目一致：server.py 存的是 VOICE_BASE_URL / VOICE_API_KEY /
    # TTS_MODEL / ASR_MODEL。以前这里只认 TTS_BASE_URL/TTS_API_KEY → 永远报"还没配"。
    base = (args.base_url or pick(env, "VOICE_BASE_URL", "TTS_BASE_URL")).rstrip("/")
    key = args.key or pick(env, "VOICE_API_KEY", "TTS_API_KEY")
    tts_model = args.tts_model or pick(env, "TTS_MODEL")
    asr_model = args.asr_model or pick(env, "ASR_MODEL")

    print("=" * 78)
    print("语音能力探测")
    print("=" * 78)
    if not base or not key:
        print("❌ 还没配：在 .env 里加上 VOICE_BASE_URL / VOICE_API_KEY"
              "（TTS_MODEL / ASR_MODEL 也写上好；这些名字手机面板里也能填）")
        return 1
    print(f"Base URL：{base}")
    print(f"key     ：…{key[-4:]}")        # 只显示后 4 位（以前显示 6 位）
    print(f"TTS 模型：{tts_model or '(没写，用 --tts-model 指定)'}")
    print(f"ASR 模型：{asr_model or '(没写，跳过往返测试)'}")
    print()

    if not tts_model:
        print("先告诉我 TTS 的完整模型名（控制台里那个），我再往下试音色。")
        return 2

    voices = [v.strip() for v in args.voices.split(",") if v.strip()] or DEFAULT_VOICES
    # ⚠️ 这一步会真实合成语音（按字符计费），以前没有任何提示就直接跑 19 发。
    # 现在必须先加 --yes；不加就只打印计划。
    if not args.yes:
        print(f"预计真实合成 {len(voices)} 句（每句一句「{args.text}」，按字符计费）"
              + ("　+ 1 次识别（TTS→ASR 往返）" if asr_model else ""))
        print("确认要跑就加 --yes：python tools/probe_voice.py --tts-model <模型> --yes")
        return 0
    good, bad = [], []
    with httpx.Client(timeout=120) as client:
        print(f"逐个试音色（共 {len(voices)} 个，每个合成一句「{args.text}」）")
        print("-" * 78)
        for v in voices:
            res = probe_tts(client, base, key, tts_model, v, args.text)
            if res["ok"]:
                path = write_wav(res["audio"], os.path.join(TMP, f"voice_{v}"))
                good.append(v)
                print(f"  ✅ {v:<24} {res['bytes']:>7} 字节  {res['ctype']:<18} {res['secs']}s  → {path}")
            else:
                bad.append(v)
                print(f"  ❌ {v:<24} {res.get('status', '')} {res['why'][:90]}")
        print("-" * 78)
        print(f"可用音色 {len(good)} 个：{good}")
        if bad:
            print(f"不可用 {len(bad)} 个：{bad}")

        if asr_model and good:
            print()
            print("TTS → ASR 往返（验证识别是否真的听得懂中文）")
            print("-" * 78)
            again = probe_tts(client, base, key, tts_model, good[0], args.text)
            # ⚠️ 不能直接 res["audio"]：合成失败时返回的字典里**没有** audio 这个键，
            # 会炸成 KeyError —— 用户看到的是一坨 traceback，而不是"哪一步为什么失败"。
            # 而且这时该**跳过往返测试**：TTS 都不出声，再拿一个不存在的音频去识别没有意义。
            if not again.get("ok") or not again.get("audio"):
                why = again.get("why") or "合成没返回音频"
                print(f"  ❌ 合成失败（{again.get('status', '')} {str(why)[:90]}），"
                      f"跳过 TTS→ASR 往返测试")
            else:
                first = write_wav(again["audio"], os.path.join(TMP, "roundtrip"))
                res = probe_asr(client, base, key, asr_model, first)
                if res["ok"]:
                    print(f"  合成：{args.text}")
                    print(f"  识别：{res['text']}")
                    same = res["text"].replace("，", "").replace("。", "")[:8] in args.text.replace("，", "").replace("。", "")
                    print(f"  {'✅ 基本一致，链路通了' if same else '⚠️ 识别结果对不上，看看是不是模型/语言选错了'}")
                else:
                    print(f"  ❌ {res.get('status', '')} {res['why'][:140]}")
    print()
    print("下一步：把可用音色按座位写进 config.yaml 的 voice 段（我可以帮你写）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
