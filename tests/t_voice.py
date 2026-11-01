# coding=utf-8
"""音色层复核：配置解析（新老两种写法）+ 三件事
  1) 每个座位的音色/语速/音高/指令都读对了吗
  2) 缓存键有没有把旋钮算进去（否则改了语速手机还放旧音频）
  3) 真合成 2 个极端座位（最慢的 5 号、最快的 2 号），确认能出音频

跑法： python t_voice.py            # 只做 1、2（不花钱）
       python t_voice.py --live     # 再加 3（两次真合成）
"""
import asyncio
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from check_api import load_env                           # noqa: E402

# 先把 .env 读进来：problems() 会检查 VOICE_BASE_URL / VOICE_API_KEY 在不在，
# 不读就会"抱怨缺配置"（第一版漏了这句，白红一条）
load_env(os.path.join(ROOT, ".env"))

from wolf.voice import SeatVoice, VoiceConfig             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


cfg_yaml = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
cfg = VoiceConfig.from_env(cfg_yaml)

print("=== 一、配置解析 ===")
check("1a 11 个座位都解析出来了（6 号真人也在表里，无害）",
      len(cfg.seat_voices) >= 10, f"{len(cfg.seat_voices)} 个")
sv4 = cfg.seat_voice(4)
check("1b 新写法（字典）读到了旋钮",
      sv4.voice == "longxiaoxia_v3.1" and sv4.rate == 0.9 and sv4.pitch == 0.9
      and "冷淡" in sv4.instruction, sv4.describe())
sv1 = cfg.seat_voice(1)
check("1c 1 号语速/音高不是默认值", (sv1.rate, sv1.pitch) == (0.95, 0.95), sv1.describe())
sv9 = cfg.seat_voice(9)
check("1d 9 号故意不加指令（双魂人设）", not sv9.instruction and sv9.rate == 1.0,
      sv9.describe())
check("1e 音色名都在 12 个预设里（没拼错）", not [p for p in cfg.problems() if "音色名" in p],
      str([p for p in cfg.problems() if "音色名" in p])[:120])
check("1f problems() 没有别的抱怨", not cfg.problems(), str(cfg.problems())[:140])

print("\n=== 二、老写法兼容 + 缓存键 ===")
old = VoiceConfig.from_env({"voice": {"seats": {3: "longhua_v3.1"}}})
check("2a 老写法（只写音色名）照样能用",
      old.seat_voice(3).voice == "longhua_v3.1" and old.seat_voice(3).rate == 1.0,
      old.seat_voice(3).describe())
k1 = SeatVoice(voice="longmiao_v3.1", rate=1.0).key()
k2 = SeatVoice(voice="longmiao_v3.1", rate=1.15).key()
k3 = SeatVoice(voice="longmiao_v3.1", rate=1.0, instruction="语气冷").key()
check("2b 缓存键把旋钮算进去了（改语速/指令不会命中旧缓存）",
      len({k1, k2, k3}) == 3, f"{k1[:22]} / {k2[:22]} / {k3[:22]}")
kwargs = SeatVoice(voice="x", rate=1.0, pitch=1.0, volume=50).kwargs()
check("2c 默认值不发多余参数（少发少错）", kwargs == {}, str(kwargs))
kwargs2 = SeatVoice(voice="x", rate=1.1, instruction="冷").kwargs()
check("2d 非默认值才发", kwargs2 == {"speech_rate": 1.1, "instruction": "冷"}, str(kwargs2))

print("\n=== 四、ASR 热词（2026-10-08 加；用户反馈识别错别字多）===")
from wolf.voice import parse_hot_words, _asr_payload         # noqa: E402

check("4a config.yaml 里的热词表解析出来了", len(cfg.asr_vocabulary) >= 20,
      f"{len(cfg.asr_vocabulary)} 个词")
check("4b 权重都合法（1~5，或 50 = 超级热词）",
      all(w in (1, 2, 3, 4, 5, 50) for w in cfg.asr_vocabulary.values()),
      str(sorted(set(cfg.asr_vocabulary.values()))))
check("4c 超级热词不超过 50 个",
      sum(1 for w in cfg.asr_vocabulary.values() if w == 50) <= 50,
      str(sum(1 for w in cfg.asr_vocabulary.values() if w == 50)))
check("4d 词形合规（中文词 ≤15 字、英文 ≤7 段）",
      all(len(w) <= 15 for w in cfg.asr_vocabulary if any(ord(c) > 127 for c in w))
      and all(len(w.split()) <= 7 for w in cfg.asr_vocabulary if not any(ord(c) > 127 for c in w)))
check("4e 关键黑话给了超级热词（悍跳/金水/倒钩/警下预/同守同救）",
      all(cfg.asr_vocabulary.get(w) == 50 for w in ("悍跳", "金水", "倒钩", "警下预", "同守同救")),
      str({w: cfg.asr_vocabulary.get(w) for w in ("悍跳", "金水", "倒钩", "警下预", "同守同救")}))

pay = _asr_payload(cfg, "webm", "data:audio/webm;base64,AAAA")
check("4f 请求体里带上了 vocabulary",
      pay["parameters"].get("vocabulary") == cfg.asr_vocabulary,
      f"{len(pay['parameters'].get('vocabulary') or {})} 个词")
check("4g format / sample_rate 照旧没被挤掉",
      pay["parameters"]["format"] == "webm" and pay["parameters"]["sample_rate"] == "16000",
      str(sorted(pay["parameters"])))
check("4h 音频仍走 input_audio 的 data: URI 形状",
      pay["input"]["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/webm"),
      str(pay["input"])[:90])
empty = VoiceConfig(base_url="http://x", api_key="k")
check("4i 词表为空时**不发** vocabulary（换 qwen3-asr-flash 那种不支持的模型才安全）",
      "vocabulary" not in _asr_payload(empty, "webm", "data:audio/webm;base64,AA")["parameters"])
check("4j 只列词的写法（列表）权重默认 4",
      parse_hot_words(["预言家", "女巫"]) == {"预言家": 4, "女巫": 4},
      str(parse_hot_words(["预言家", "女巫"])))
check("4k 超长词 / 越界权重被拦住（写错只是静默失效，所以这里必须卡）",
      parse_hot_words({"这个词实在是太长了超过了十五个字符": 4, "好词": 99}) == {"好词": 4},
      str(parse_hot_words({"这个词实在是太长了超过了十五个字符": 4, "好词": 99})))
check("4l 乱七八糟的类型不炸（str/None 都当没有）",
      parse_hot_words("预言家") == {} and parse_hot_words(None) == {})

if "--live" in sys.argv:
    print("\n=== 三、真合成两个极端座位（很便宜）===")
    from wolf.voice import tts                         # noqa: E402

    live = VoiceConfig.from_env(cfg_yaml)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_seats")
    os.makedirs(out_dir, exist_ok=True)
    for seat in (5, 2):
        sv = live.seat_voice(seat)
        try:
            data = asyncio.run(tts("我这轮先过，听听别人怎么说。", sv.voice, live, seat=seat))
            io.open(os.path.join(out_dir, f"seat{seat}.mp3"), "wb").write(data)
            check(f"3{seat} {seat}号 合成成功（{sv.describe()[:40]}）", len(data) > 3000,
                  f"{len(data)} 字节")
        except Exception as exc:                       # noqa: BLE001
            check(f"3{seat} {seat}号 合成成功", False, str(exc)[:130])

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for name, _, detail in bad:
    print(f"  [FAIL] {name}  {detail}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
