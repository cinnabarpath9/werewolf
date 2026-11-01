# coding=utf-8
"""假的中转站：前 N 次请求返回 500（模拟上游波动），之后返回一个"什么任务都能喂饱"的 JSON。

只给测试用（t_stall_ws.py 会拉起来），不联网、不花钱。

跑法：
    python fake_llm.py --port 8099 --fail-first 5
接口：
    POST /v1/chat/completions   → 500（前 fail-first 次）或一个 OpenAI 形状的成功回复
    GET  /stats                 → {"requests": n, "failed": m, "ok": k}
"""
from __future__ import annotations

import argparse
import json
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

#: 一个"超集"回复：所有任务的 schema 都能从里面读到东西（语音/投票/查验/刀人/上警/遗言/警徽…）
SUPERSET = {
    "speech": "（假上游）我是假模型，随便说一句。",
    "claim": "无",
    "suspect": [],
    "reason": "fake upstream",
    "vote": 2,
    "target": 2,
    "check": 2,
    "shoot": None,
    "save": False,
    "poison": None,
    "run": False,
    "to": None,
    "abstain": False,
}

app = FastAPI(title="fake upstream")
STATE = {"requests": 0, "failed": 0, "ok": 0, "fail_first": 5}


@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    body = await request.json()
    STATE["requests"] += 1
    if STATE["requests"] <= STATE["fail_first"]:
        STATE["failed"] += 1
        return JSONResponse({"error": {"message": "upstream boom（故意造的波动）",
                                       "type": "server_error"}}, status_code=500)
    STATE["ok"] += 1
    text = json.dumps(SUPERSET, ensure_ascii=False)
    return JSONResponse({
        "id": "fake-1",
        "object": "chat.completion",
        "model": body.get("model") or "fake",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


@app.get("/stats")
async def stats() -> dict:
    return dict(STATE)


@app.post("/reset")
async def reset(payload: dict | None = None) -> dict:
    STATE["requests"] = 0
    STATE["failed"] = 0
    STATE["ok"] = 0
    if payload and "fail_first" in payload:
        STATE["fail_first"] = int(payload["fail_first"])
    return dict(STATE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--fail-first", type=int, default=5)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    STATE["fail_first"] = args.fail_first
    import uvicorn
    print(f"假上游起来了：http://{args.host}:{args.port}/v1  前 {args.fail_first} 次请求会 500")
    sys.stdout.flush()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
