# coding=utf-8
"""本地设置存储（API / 座位 / 语音）的纯本地回归。

守的是什么
----------
2026-10-31 加了「页面设置」：用户第一次能在网页上填 API、把 1~12 号座位指过去，
不用改 config.yaml 再重启。它有三个容易静默坏掉的地方：

  ① **key 留空 = 不改**：页面不回显 key，编辑已有 API 时 key 框是空的。
     如果这里当成「把 key 清空」，用户改一次地址就把 key 弄丢了。
  ② **座位映射落成 `WOLF_SEAT_PROFILES`**：拼串顺序/格式错了，路由会静默回落
     config.yaml —— 页面显示「12/12 已分配」，实际一个都没生效。
  ③ **思考风格**：页面选了「通义千问」却写不进 `llm.thinking_style`，思考档位
     会按 OpenAI 的参数发出去（轻则不生效，重则上游 400）。

这个脚本只碰临时目录与进程环境变量，不联网、不写项目文件。

跑法：python tests/check_settings_store.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import settings_store as store  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wolf_settings_")
    env_backup = {k: os.environ.get(k) for k in
                  ("ZZTEST_BASE_URL", "ZZTEST_API_KEY", "ZZTEST_MODEL",
                   "WOLF_SEAT_PROFILES", "VOICE_BASE_URL", "VOICE_API_KEY",
                   "TTS_MODEL", "ASR_MODEL")}
    try:
        print("=== 一、名字与校验 ===")
        check("1a 中文 API 名会被拒绝（提示可读）",
              _raises(lambda: store.normalize_id("豆包"), ValueError),
              "")
        check("1b GLM-4.6 这类带点/减号的名字可用",
              store.normalize_id("glm-4.6") == "GLM-4.6")
        check("1c 地址不以 http(s) 开头会被拒绝",
              _raises(lambda: store.normalize_api(
                  {"id": "X", "base_url": "api.example.com", "model": "m"}), ValueError))

        print("\n=== 二、新增 / 编辑：key 留空不能把原来的 key 冲掉 ===")
        data = store.empty()
        data = store.upsert_api(data, {"id": "zztest", "name": "ZZTEST",
                                       "base_url": "https://api.example.com/v1",
                                       "model": "m1", "key": "sk-original-123456",
                                       "style": "qwen"})
        check("2a 新增一条", len(data["apis"]) == 1 and data["apis"][0]["id"] == "ZZTEST")
        data = store.upsert_api(data, {"id": "ZZTEST", "base_url": "https://api.example.com/v2",
                                       "model": "m2", "key": "", "style": ""})
        api = data["apis"][0]
        check("2b 编辑时 key 留空 = 保留旧 key", api["key"] == "sk-original-123456")
        check("2c 地址/模型被覆盖", api["base_url"].endswith("/v2") and api["model"] == "m2")
        check("2d 风格显式传空串 = 改回自动（不保留旧 qwen）", api["style"] == "",
              f"style={api['style']!r}")

        print("\n=== 三、落盘 / 读回 ===")
        data = store.set_seats(data, {"1": "ZZTEST", "2": "zztest", "12": "ZZTEST"})
        data = store.set_voice(data, {"base_url": "https://voice.example.com",
                                      "tts_model": "tts-m", "asr_model": "asr-m",
                                      "api_key": "vk-5678"})
        path = store.save(tmp, data)
        check("3a 文件写出来了", os.path.isfile(path))
        raw = json.loads(open(path, encoding="utf-8").read())
        check("3b 座位按字符串键存（JSON 里 1/2/12 都在）",
              set(raw["seats"]) == {"1", "2", "12"}, str(raw["seats"]))
        loaded = store.load(tmp)
        check("3c 读回结构与写进去一致",
              loaded["seats"] == {"1": "ZZTEST", "2": "ZZTEST", "12": "ZZTEST"}
              and loaded["apis"][0]["key"] == "sk-original-123456")
        pub = store.public(loaded)
        check("3d 公开版只给 key 后四位",
              pub["apis"][0]["key_tail"] == "3456" and "sk-original" not in json.dumps(pub))
        check("3e 公开版里 voice 的 key 也只给后四位",
              pub["voice"]["key_tail"] == "5678")

        print("\n=== 四、坏文件不能把服务带崩 ===")
        bad = os.path.join(tmp, "bad.local.json")
        open(bad, "w", encoding="utf-8").write("{not json")
        real_path = store.path
        store.path = lambda base: bad  # type: ignore[assignment]
        try:
            got = store.load(tmp)
            check("4a 坏 JSON：返回空设置", got == store.empty())
        finally:
            store.path = real_path  # type: ignore[assignment]
        check("4b 一条脏 API 不会连累其它条目",
              len(store.normalize({"apis": [{"id": "好", "base_url": "x"},
                                             {"id": "GOOD", "base_url": "https://a.b/v1",
                                              "model": "m"}]})["apis"]) == 1)

        print("\n=== 五、生效：环境变量 / cfg / 座位串 ===")
        cfg: dict = {}
        styles = store.apply(cfg, loaded)
        check("5a 写了 BASE_URL / MODEL / KEY",
              os.environ.get("ZZTEST_BASE_URL") == "https://api.example.com/v2"
              and os.environ.get("ZZTEST_MODEL") == "m2"
              and os.environ.get("ZZTEST_API_KEY") == "sk-original-123456")
        check("5b 择座串是 1:ID,2:ID,…（按座位号排序）",
              os.environ.get("WOLF_SEAT_PROFILES") == "1:ZZTEST,2:ZZTEST,12:ZZTEST",
              os.environ.get("WOLF_SEAT_PROFILES", ""))
        check("5c 自动风格按地址猜：example.com → openai", styles.get("ZZTEST") == "openai",
              str(styles))
        check("5d cfg.llm.thinking_style 写进去了",
              (cfg.get("llm") or {}).get("thinking_style", {}).get("ZZTEST") == "openai")

        print("\n=== 六、清空 / 删除 ===")
        empty_seats = store.set_seats(loaded, {})
        store.apply({}, empty_seats)
        check("6a 座位清空后 WOLF_SEAT_PROFILES 必须消失",
              "WOLF_SEAT_PROFILES" not in os.environ)
        store.forget("ZZTEST")
        check("6b forget 清掉该档案的三件套",
              not os.environ.get("ZZTEST_API_KEY") and not os.environ.get("ZZTEST_BASE_URL"))
        after = store.delete_api(loaded, "ZZTEST")
        check("6c 删 API 同时清掉指向它的座位",
              not after["apis"] and not after["seats"], str(after["seats"]))

        # ⚠️ 2026-10-31 外部审查 P1-03 的回归：页面"清空"字段以前只写不删 ——
        #    清空语音/地址后旧值还留在进程环境里，继续生效到重启。
        print("\n=== 六之2、页面清空字段 vs 启动加载（口径必须分开）===")
        os.environ["ZZTEST_BASE_URL"] = "https://old.example.com"
        os.environ["ZZTEST_API_KEY"] = "sk-keep-me-9999"
        os.environ["VOICE_BASE_URL"] = "https://old-voice.example.com"
        cleared = store.normalize({
            "apis": [{"id": "ZZTEST", "name": "ZZTEST", "base_url": "", "model": "", "key": ""}],
            "seats": {}, "voice": {},
        })
        store.apply({}, cleared)                       # 启动口径
        check("6d 启动时（clear_missing=False）不动环境变量",
              os.environ.get("ZZTEST_BASE_URL") == "https://old.example.com"
              and os.environ.get("VOICE_BASE_URL") == "https://old-voice.example.com")
        store.apply({}, cleared, clear_missing=True)   # 页面保存口径
        check("6e 页面保存时，清空的地址/语音必须从环境变量消失",
              not os.environ.get("ZZTEST_BASE_URL")
              and not os.environ.get("VOICE_BASE_URL")
              and not os.environ.get("ZZTEST_MODEL"),
              f"base={os.environ.get('ZZTEST_BASE_URL')!r} "
              f"voice={os.environ.get('VOICE_BASE_URL')!r}")
        check("6f 但 key 永远不删（契约：编辑时留空 = 保留旧 key）",
              os.environ.get("ZZTEST_API_KEY") == "sk-keep-me-9999",
              os.environ.get("ZZTEST_API_KEY", ""))

        # ── 七、模型的「参数」二级页（2026-10-31 加）──
        #   契约：填了才生效、留空/越界一律当没填；保存时整份替换；没带 tuning 键则不动。
        print("\n=== 七、每条 API 的模型参数（温度/上限/超时/重试/卡住/思考）===")
        tuned = store.upsert_api(store.empty(), {
            "id": "TUNE1", "name": "TUNE1", "base_url": "https://x.example.com/v1",
            "model": "m", "key": "sk-1",
            "tuning": {"temperature": "1.35", "max_tokens": 9000, "timeout": 120,
                       "max_retries": 1, "idle_timeout": 12, "thinking": "low"},
        })
        got = tuned["apis"][0]["tuning"]
        check("7a 六项都收进来了，数字按类型存",
              got == {"temperature": 1.35, "max_tokens": 9000, "timeout": 120.0,
                      "max_retries": 1, "idle_timeout": 12.0, "thinking": "low"}, str(got))

        bounds = store.normalize_tuning({
            "temperature": 9, "max_tokens": 10, "timeout": 5, "max_retries": 99,
            "idle_timeout": 999, "thinking": "crazy",
        })
        check("7b 越界/写错的项一律丢掉（不夹到边界，免得以为生效了）",
              bounds == {}, str(bounds))

        empty_vals = store.normalize_tuning({
            "temperature": "", "max_tokens": None, "timeout": "  ", "thinking": "",
        })
        check("7c 留空 = 没填", empty_vals == {}, str(empty_vals))

        kept = store.upsert_api(tuned, {"id": "TUNE1", "name": "TUNE1",
                                        "base_url": "https://x.example.com/v1", "model": "m"})
        check("7d 没带 tuning 键 = 不动原来的参数",
              kept["apis"][0]["tuning"] == got, str(kept["apis"][0]["tuning"]))

        cleared2 = store.upsert_api(kept, {"id": "TUNE1", "name": "TUNE1",
                                           "base_url": "https://x.example.com/v1",
                                           "model": "m", "tuning": {}})
        check("7e 带了空 tuning = 整份清空（「恢复默认」靠这条）",
              cleared2["apis"][0]["tuning"] == {}, str(cleared2["apis"][0]["tuning"]))

        store.apply({}, tuned, clear_missing=True)
        raw = os.environ.get("WOLF_API_TUNING", "")
        check("7f 保存后 WOLF_API_TUNING 里有这条 API 的参数",
              '"TUNE1"' in raw and '"temperature":1.35' in raw, raw[:120])
        store.apply({}, cleared2, clear_missing=True)
        check("7g 参数清空后 WOLF_API_TUNING 必须消失（否则旧参数还在生效）",
              "WOLF_API_TUNING" not in os.environ, os.environ.get("WOLF_API_TUNING", ""))
        check("7h 参数里的 key 不会进 public()（页面不回显）",
              "tuning" in store.public(tuned)["apis"][0]
              and "sk-1" not in str(store.public(tuned)["apis"][0].get("tuning")), "")

        print("\n=== 七、语音环境变量 ===")
        store.apply({}, loaded)
        check("7a VOICE_* / TTS_MODEL / ASR_MODEL 都写进去了",
              os.environ.get("VOICE_BASE_URL") == "https://voice.example.com"
              and os.environ.get("VOICE_API_KEY") == "vk-5678"
              and os.environ.get("TTS_MODEL") == "tts-m"
              and os.environ.get("ASR_MODEL") == "asr-m")
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if bad else 0


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
