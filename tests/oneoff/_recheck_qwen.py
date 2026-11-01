# coding=utf-8
"""复核单独一条：千问（快米兔）用火山那套 `thinking.type` 到底是"参数不认"还是"刚才撞限流"。

做法：歇 20 秒，只发两条（thinking.type=enabled / enable_thinking+budget），
看**原始返回**（HTTP 状态 + 正文前 120 字），不再靠思考链字数猜。
"""
import asyncio
import io
import json
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import httpx

env = {}
for line in io.open(".env", encoding="utf-8", errors="ignore"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")


async def one(label, params):
    body = {"model": env["QWEN_MODEL"], "messages": [{"role": "user", "content": "1+1=?只回一个数字"}],
            "max_tokens": 32, "stream": False, **params}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as c:
        r = await c.post(f"{env['QWEN_BASE_URL'].rstrip('/')}/chat/completions",
                         headers={"Authorization": f"Bearer {env['QWEN_API_KEY']}",
                                  "Content-Type": "application/json"}, json=body)
    raw = r.text
    try:
        j = json.loads(raw)
    except Exception:                                                   # noqa: BLE001
        print(f"  {label:<34} HTTP {r.status_code}  非 JSON：{raw[:120]!r}")
        return
    if r.status_code != 200 or j.get("error"):
        print(f"  {label:<34} HTTP {r.status_code}  错误：{str(j.get('error'))[:120]}")
        return
    msg = ((j.get("choices") or [{}])[0].get("message") or {})
    reason = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
    print(f"  {label:<34} HTTP 200  思考链 {len(reason)} 字  正文={str(msg.get('content'))[:30]!r}")


async def main():
    print("  等 20 秒避开限流…")
    await asyncio.sleep(20)
    await one("① thinking.type=enabled（火山写法）", {"thinking": {"type": "enabled"}})
    await asyncio.sleep(8)
    await one("② enable_thinking+budget（我们现在的写法）",
              {"enable_thinking": True, "thinking_budget": 4096})
    await asyncio.sleep(8)
    await one("③ 什么思考参数都不发（对照）", {})


asyncio.run(main())
