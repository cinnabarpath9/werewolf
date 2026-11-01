# coding=utf-8
"""座位路由自检：证明「每个座位可以来自不同平台、不同 key、不同模型」。

不需要真 key，只用假环境变量验证解析逻辑：
    python tools/test_routing.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wolf import routing  # noqa: E402

os.environ["QWEN_API_KEY"] = "sk-qwen-fake"
os.environ["DEEPSEEK_API_KEY"] = "sk-ds-fake"
os.environ["ZHIPU_API_KEY"] = "sk-zhipu-fake"

CFG = {
    "llm": {
        "default": {
            "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
            "api_key_env": "QWEN_API_KEY",
            "model": "qwen3.7-plus",
            "temperature": 1.0,
            "max_tokens": 800,
        },
        "roles": {
            "wolf": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "model": "deepseek-chat",
                "extra_body": {"enable_thinking": True},
            },
        },
        "seats": {
            3: {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key_env": "ZHIPU_API_KEY",
                "model": "glm-4.6",
                "temperature": 0.6,
            },
        },
    }
}


class FakeSeat:
    def __init__(self, seat_id, role_key, is_human=False):
        self.id, self.role_key, self.is_human = seat_id, role_key, is_human


SEATS = [
    FakeSeat(1, "wolf"), FakeSeat(2, "villager"), FakeSeat(3, "witch"),
    FakeSeat(4, "seer"), FakeSeat(5, "hunter"),
]

EXPECT = {
    1: ("api.deepseek.com", "deepseek-chat", "sk-ds-fake", 1.0),      # 角色覆盖
    2: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-fake", 1.0),  # 走默认
    3: ("open.bigmodel.cn", "glm-4.6", "sk-zhipu-fake", 0.6),           # 座位覆盖优先
    4: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-fake", 1.0),
    5: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-fake", 1.0),
}

failures = 0
print("=" * 78)
print("座位路由解析结果")
print("=" * 78)
for seat in SEATS:
    spec = routing.resolve(CFG, seat)
    host, model, key, temp = EXPECT[seat.id]
    ok = (spec.host == host and spec.model == model
          and spec.api_key == key and abs(spec.temperature - temp) < 1e-6)
    failures += 0 if ok else 1
    print(f"  {seat.id}号 {seat.role_key:<9} → {spec.host:<32} {spec.model:<16} "
          f"key=…{spec.api_key[-8:]} temp={spec.temperature}  {'✅' if ok else '❌ 期望 ' + str(EXPECT[seat.id])}")

# extra_body 应该被角色级合并进来
wolf_extra = routing.resolve(CFG, SEATS[0]).extra_body
print(f"\n  角色级 extra_body 透传：{wolf_extra} {'✅' if wolf_extra.get('enable_thinking') else '❌'}")
failures += 0 if wolf_extra.get("enable_thinking") else 1

# 经验存档的键应该是 平台/模型
keys = {seat.id: routing.resolve(CFG, seat).key for seat in SEATS}
print("\n  经验存档键（同一个平台的同一个模型算同一个「脑子」）：")
for seat_id, key in keys.items():
    print(f"    {seat_id}号 → {key}")
failures += 0 if keys[1] != keys[2] and keys[2] == keys[4] else 1

# 缺配置时应该报出人话
bad_cfg = {"llm": {"default": {"base_url": "", "api_key_env": "NOT_SET_ANYWHERE", "model": ""}}}
issues = routing.problems(bad_cfg, SEATS)
print(f"\n  缺配置检查：{len(issues)} 个座位报错 {'✅' if len(issues) == 5 else '❌'}")
print(f"    例：{issues[0] if issues else '（无）'}")
failures += 0 if len(issues) == 5 else 1

# ── 平台档案（profile）：地址 / key / 模型都写在 .env 里，config 只写档案名 ──

os.environ.update({
    "QWEN_BASE_URL": "https://maas.qianwenaiapi.com/compatible-mode/v1",
    "QWEN_API_KEY": "sk-qwen-profile",
    "QWEN_MODEL": "qwen3.7-plus",
    "ZHIPU_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
    "ZHIPU_API_KEY": "sk-zhipu-profile",
    "ZHIPU_MODEL": "glm-4.6",
})

PROFILE_CFG = {
    "llm": {
        "default": {"profile": "QWEN", "temperature": 1.0},
        "seats": {
            3: {"profile": "ZHIPU", "temperature": 0.6},          # 整档换成智谱
            4: {"profile": "ZHIPU", "model": "glm-4.6-air"},      # 档案 + 显式覆盖模型
        },
    }
}

PROFILE_EXPECT = {
    # 座位: (host, model, key)
    1: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-profile"),
    2: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-profile"),
    3: ("open.bigmodel.cn", "glm-4.6", "sk-zhipu-profile"),
    4: ("open.bigmodel.cn", "glm-4.6-air", "sk-zhipu-profile"),   # 显式 model 压过档案
    5: ("maas.qianwenaiapi.com", "qwen3.7-plus", "sk-qwen-profile"),
}

print()
print("=" * 78)
print("平台档案（profile）：地址 / key / 模型都在 .env 里，config 只写档案名")
print("=" * 78)
for seat in SEATS:
    spec = routing.resolve(PROFILE_CFG, seat)
    host, model, key = PROFILE_EXPECT[seat.id]
    ok = spec.host == host and spec.model == model and spec.api_key == key
    failures += 0 if ok else 1
    print(f"  {seat.id}号 {seat.role_key:<9} → {spec.host:<28} {spec.model:<14} "
          f"temp={spec.temperature}  {'✅' if ok else '❌ 期望 ' + str(PROFILE_EXPECT[seat.id])}")

seat3_temp = routing.resolve(PROFILE_CFG, SEATS[2]).temperature
ok = abs(seat3_temp - 0.6) < 1e-6
failures += 0 if ok else 1
print(f"\n  座位层还能单独覆盖采样参数：3号 temperature={seat3_temp} {'✅' if ok else '❌'}")

# 档案缺 key 时要报出人话，并指出该看哪个变量
saved = os.environ.pop("ZHIPU_API_KEY")
issues = routing.problems(PROFILE_CFG, SEATS)
os.environ["ZHIPU_API_KEY"] = saved
ok = len(issues) == 2 and "ZHIPU_API_KEY" in issues[0]
failures += 0 if ok else 1
print(f"  档案缺 key 时的提示：{issues[0] if issues else '（没报错）'} {'✅' if ok else '❌'}")

print()
print("❌ 有 %d 项不符合预期" % failures if failures else "✅ 座位路由全部符合预期")
raise SystemExit(1 if failures else 0)
