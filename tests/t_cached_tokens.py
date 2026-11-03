# coding=utf-8
"""cached_tokens 记账（2026-10-30 加）。

为什么单开一条：一局的输入 token 是百万量级，静态前缀（规则书/打法书/概念库）
能不能命中平台侧前缀缓存，直接决定账单。以前只记 `prompt_tokens`，于是
「靠前缀缓存省钱」只是结构上的推测 —— **没有数据能证明它真的命中了**。

这条闸门钉三件事：
  ① 各家平台的字段写法都要认（火山方舟 / DeepSeek / Anthropic 风格中转 / 散装）；
  ② 平台没返回时必须记 **0**（宁可缺数据，也不许编一个命中率出来）；
  ③ 记账要一路传到 `agent.last_meta()`（引擎再抄进 transcript，复盘才看得到）。

变异自证：把 `wolf/llm.py::cached_tokens_of` 改成恒返回 0，第一节与第二节必须变红。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf.agents import LLMAgent                                  # noqa: E402
from wolf.llm import LLMClient, cached_tokens_of                  # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


BASE = dict(base_url="http://127.0.0.1:8098/v1", api_key="test-key", model="test-model",
            timeout=5.0, max_retries=0)


print("=== 一、字段写法（各家平台不一样）===")
CASES = [
    ("火山方舟 / OpenAI 兼容：prompt_tokens_details.cached_tokens",
     {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 64}}, 64),
    ("DeepSeek 官方：prompt_cache_hit_tokens",
     {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}, 80),
    ("Anthropic 风格中转：cache_read_input_tokens",
     {"prompt_tokens": 100, "cache_read_input_tokens": 40}, 40),
    ("散装：直接给 cached_tokens",
     {"prompt_tokens": 100, "cached_tokens": 50}, 50),
    ("平台没返回：必须 0（不许猜）",
     {"prompt_tokens": 100}, 0),
    ("脏数据（字符串/负数/None）：当 0",
     {"prompt_tokens": 100, "cached_tokens": "abc"}, 0),
    ("报得比输入还大：夹到输入值",
     {"prompt_tokens": 100, "cached_tokens": 120}, 100),
]
for label, used, want in CASES:
    got = cached_tokens_of(used)
    check(label, got == want, f"{used} → {got}（期望 {want}）")
check("None / 非 dict 都当 0",
      cached_tokens_of(None) == 0 and cached_tokens_of("x") == 0)


print("\n=== 二、流式调用：累加 + 不沿用上一次 ===")


async def stream_case():
    client = LLMClient(stream=True, **BASE)
    frames = [
        ('{"speech":"x"}', "", {"prompt_tokens": 100, "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 64}}),
        ('{"speech":"y"}', "", {"prompt_tokens": 100, "completion_tokens": 5}),   # 平台这次没返回缓存
    ]

    async def fake_stream(payload):
        return frames.pop(0)

    client._post_stream = fake_stream
    await client.chat_json("p1")
    first = client.last_call.get("cached_tokens")
    await client.chat_json("p2")
    second = client.last_call.get("cached_tokens")
    usage = dict(client.usage)
    await client.aclose()
    return first, second, usage


first, second, usage = asyncio.run(stream_case())
check("第一次 last_call 记到 64", first == 64, str(first))
check("平台没返回那次记 0（不是沿用上一次的 64）", second == 0, str(second))
check("usage 累加两次且只加命中的那次",
      usage["calls"] == 2 and usage["prompt_tokens"] == 200 and usage["cached_tokens"] == 64,
      str(usage))
check("usage 一开始就有 cached_tokens 字段", "cached_tokens" in usage)


print("\n=== 三、非流式调用也要记 ===")


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": '{"speech":"x"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 3,
                          "prompt_tokens_details": {"cached_tokens": 30}}}


class _StubHTTP:
    async def post(self, url, json=None):
        return _Resp()

    async def aclose(self):
        pass


async def nonstream_case():
    client = LLMClient(stream=False, **BASE)
    await client.aclose()                 # 关掉真的连接池，换桩
    client._client = _StubHTTP()
    await client.chat_json("p")
    return client.last_call.get("cached_tokens"), client.usage["cached_tokens"]


last, total = asyncio.run(nonstream_case())
check("非流式 last_call 记到 30", last == 30, str(last))
check("非流式 usage 累加 30", total == 30, str(total))


print("\n=== 四、一路传到 agent.last_meta（引擎从这抄进存档）===")


async def agent_case():
    client = LLMClient(stream=True, **BASE)

    async def fake_stream(payload):
        return ('{"speech":"x"}', "", {"prompt_tokens": 100, "completion_tokens": 5,
                           "prompt_tokens_details": {"cached_tokens": 64}})

    client._post_stream = fake_stream
    agent = LLMAgent(client, seat_id=3)
    task = SimpleNamespace(kind="vote", required=(), extra={})
    await agent.ask("p", task)
    meta = agent.last_meta()
    await client.aclose()
    return meta


meta = asyncio.run(agent_case())
check("last_meta 带 cached_tokens=64", meta.get("cached_tokens") == 64, str(meta))


print("\n=== 五、下游接线（存档 / lite / usage 汇总）===")


def read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


engine_src = read("wolf/engine.py")
check("engine 把 cached_tokens 抄进 transcript",
      '"cached_tokens": meta.get("cached_tokens", 0)' in engine_src)

server_src = read("server.py")
m = re.search(r"_LITE_CALL_KEYS\s*=\s*\(([^)]*)\)", server_src)
check("lite 白名单带 cached_tokens（复盘首屏能看到）",
      bool(m) and "cached_tokens" in (m.group(1) if m else ""),
      (m.group(1).strip()[:80] if m else "没找到 _LITE_CALL_KEYS"))
check("usage 汇总带 cached_tokens", '"cached_tokens": sum(' in server_src)

review_src = read("tools/review.py")
check("复盘命令行会显示缓存命中", "缓存命中" in review_src)

print("=" * 80)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
