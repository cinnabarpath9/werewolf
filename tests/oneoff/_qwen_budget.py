# coding=utf-8
"""千问（快米兔）的 `thinking_budget` 到底管不管用？

前一次用"1+1"这种小题，思考链只有几十字，量不出差别（还撞了一次限流，误判过）。
这次换一道**需要想很久**的题，比三个设置：不发参数 / 512 / 4096。
判据：思考链字数 + 总耗时（这直接决定手机等多久）。
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

Q = ("有 12 个外观完全一样的球，其中恰有一个重量不同（可能偏重也可能偏轻）。"
     "用一台只能显示左重/右重/平衡的天平，最少称几次一定能找出那个球并判断它是重还是轻？"
     "给出次数和称法要点，200 字以内。")


async def one(label, params):
    body = {"model": env["QWEN_MODEL"], "messages": [{"role": "user", "content": Q}],
            "max_tokens": 3000, "stream": False, **params}
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as c:
        r = await c.post(f"{env['QWEN_BASE_URL'].rstrip('/')}/chat/completions",
                         headers={"Authorization": f"Bearer {env['QWEN_API_KEY']}",
                                  "Content-Type": "application/json"}, json=body)
    el = time.time() - t0
    j = json.loads(r.text)
    if r.status_code != 200 or j.get("error"):
        print(f"  {label:<30} HTTP {r.status_code} {str(j.get('error'))[:90]}")
        return
    msg = ((j.get("choices") or [{}])[0].get("message") or {})
    u = j.get("usage") or {}
    det = (u.get("completion_tokens_details") or {})
    reason = str(msg.get("reasoning_content") or "")
    print(f"  {label:<30} {el:>6.1f}s  思考链 {len(reason):>5} 字  "
          f"reasoning_tokens={det.get('reasoning_tokens')}  completion={u.get('completion_tokens')}  "
          f"答={str(msg.get('content'))[:26]!r}")


async def main():
    await one("① 不发思考参数", {})
    await asyncio.sleep(10)
    await one("② thinking_budget=512", {"enable_thinking": True, "thinking_budget": 512})
    await asyncio.sleep(10)
    await one("③ thinking_budget=4096", {"enable_thinking": True, "thinking_budget": 4096})


asyncio.run(main())
