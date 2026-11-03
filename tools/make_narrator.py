# coding=utf-8
"""一次性生成「法官播报」音频（预录，不再每次实时 TTS）。

为什么：公告（天黑/天亮/竞选/出局/平安夜…）是固定台词，实时 TTS 既慢又花钱；
预录成 mp3 后前端直接播放，实时 TTS 只留给 AI 发言。

用法：python tools/make_narrator.py            # 用 .env 里现有 VOICE_* 配置
      python tools/make_narrator.py --force    # 强制重生成
产物：web/audio/narrator/*.mp3（随包发布；改台词必须重跑本脚本）
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                     # noqa: E402

from wolf import voice                                          # noqa: E402

OUT = os.path.join(ROOT, "web", "audio", "narrator")
VOICE = "longmiao_v3.1"          # 主持/法官：稳、字正腔圆（座位 1 的默认音色）

CLIPS = {
    "night_fall": "天黑请闭眼，狼人请睁眼。",
    "day_break": "天亮了，请睁眼。",
    "badge_election": "现在开始警长竞选，请想要竞选警长的玩家举手。",
    "badge_pk": "出现平票，进入警长 PK。",
    "vote_start": "请投票，放逐你认为最像狼的玩家。",
    "peaceful_night": "昨晚是平安夜，没有人出局。",
    "someone_died": "昨晚有人出局。",
    "game_over": "对局结束。",
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8-sig")) or {}
    # `.env` 要先读进环境变量：VoiceConfig.from_env 只看 os.environ（和服务端同一套）
    sys.path.insert(0, ROOT)
    import server                                            # noqa: E402
    server.load_env(os.path.join(ROOT, ".env"))
    vc = voice.VoiceConfig.from_env(cfg)
    if not (vc.base_url and vc.api_key):
        print("❌ 没读到 VOICE_BASE_URL / VOICE_API_KEY，先配 .env")
        return 1
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    for name, text in CLIPS.items():
        path = os.path.join(OUT, f"{name}.mp3")
        if os.path.isfile(path) and not args.force:
            print(f"跳过（已存在）{name}.mp3")
            ok += 1
            continue
        try:
            audio = await voice.tts(text, VOICE, vc)
            with open(path, "wb") as fh:
                fh.write(audio)
            print(f"✅ {name}.mp3  {len(audio)//1024} KB  「{text}」")
            ok += 1
        except Exception as exc:                                # noqa: BLE001
            print(f"❌ {name}: {type(exc).__name__}: {exc}")
    print(f"\n完成 {ok}/{len(CLIPS)} → {OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))