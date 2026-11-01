# coding=utf-8
"""流式 + 静默超时的单测（假上游，不联网）。"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import httpx                                                        # noqa: E402

from wolf.llm import LLMClient, LLMError                            # noqa: E402

N = 0
BAD: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    if not ok:
        BAD.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


class FakeResp:
    def __init__(self, lines, hang_after=None, ctype="text/event-stream", body=b""):
        self.status_code = 200
        # ⚠️ 2026-10-10：客户端现在按 content-type 判断"这是不是 SSE"——
        # 假响应必须给 headers，否则客户端会退回普通解析（第一版就栽在这里）。
        self.headers = {"content-type": ctype}
        self._lines = lines
        self._hang_after = hang_after
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for i, ln in enumerate(self._lines):
            if self._hang_after is not None and i >= self._hang_after:
                await asyncio.sleep(3600)          # 永远不吐 —— 模拟"卡住"
            yield ln


class FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, *a, **k):
        return FakeStream(self._resp)


def make(resp, idle=0.3) -> LLMClient:
    c = LLMClient(base_url="http://x/v1", api_key="k", model="m", max_tokens=100,
                  timeout=5, max_retries=0, stream=True, idle_timeout=idle, label="t")
    c._client = FakeClient(resp)                                   # 换掉真 HTTP 客户端
    return c


async def main() -> int:
    print("=== 一、正常流：正文 + 思考链 + usage ===")
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"想一下"}}]}',
        'data: {"choices":[{"delta":{"content":"{\\"answer\\":2}"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
        "data: [DONE]",
    ]
    got = await make(FakeResp(lines)).chat_json("hi", {"enable_thinking": True})
    lc = got  # chat_json 返回解析后的 JSON
    check("1a 流式正文能解析成 JSON", lc.get("answer") == 2, str(lc))

    print("\n=== 二、卡住：吐两帧后不再吐 → 抛可重试的错误 ===")
    # ⚠️ 假上游必须**给足帧**：`hang_after=2` 指的是"从第 3 帧开始不吐"。
    #    第一版写成 `lines[:2]`（只有两帧）→ 生成器正常结束 → 测试自己假红。
    c = make(FakeResp(lines, hang_after=2), idle=0.3)
    try:
        await c.chat_json("hi", None)
        check("2a 卡住时必须抛错", False, "居然没抛")
    except LLMError as exc:
        check("2a 卡住时抛的是 LLMError（不是 httpx 超时 → 会被重试）",
              not isinstance(exc, httpx.TimeoutException)
              and not isinstance(exc, asyncio.TimeoutError), f"{type(exc).__name__}")
        check("2b 错误里写明是「没有吐出任何字节」", "没有任何字节" in str(exc) or "没有吐出" in str(exc),
              str(exc)[:80])

    print("\n=== 三、什么都不吐 → 也必须是可重试的错误 ===")
    try:
        await make(FakeResp([], hang_after=0), idle=0.3).chat_json("hi", None)
        check("3a 空流必须抛错", False, "居然没抛")
    except LLMError as exc:
        check("3a 空流抛 LLMError", isinstance(exc, LLMError), f"{type(exc).__name__}: {exc}")

    print("\n=== 四、上游忽略 stream:true、回了整段 JSON → 必须照常成功 ===")
    body = ('{"choices":[{"message":{"content":"{\\"answer\\":2}"}}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":2}}').encode("utf-8")
    got4 = await make(FakeResp([], ctype="application/json", body=body)).chat_json("hi", None)
    check("4a 整段 JSON 也能解析（别把成功的调用误判成「0 个字节」）",
          got4.get("answer") == 2, str(got4))

    print(f"\n共 {N} 项：通过 {N - len(BAD)}，失败 {len(BAD)}")
    return 1 if BAD else 0


raise SystemExit(asyncio.run(main()))
