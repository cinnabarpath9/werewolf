# coding=utf-8
"""假的 DashScope ASR 端点：把收到的请求原样记下来，回一句固定识别结果。

用途：验证 server.py 把 `await request.body()` 改成"边读边数"之后，**正常的语音识别这条路
还通不通**（不花钱：真正的语音服务被换成了本机这个假端点）。
"""
from __future__ import annotations

import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "fake_asr_hits.jsonl")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:                                # noqa: BLE001
            payload = {"_raw_len": len(raw)}
        content = ((payload.get("input") or {}).get("messages") or [{}])[0].get("content") or [{}]
        data_uri = ((content[0] or {}).get("input_audio") or {}).get("data") or ""
        import base64
        import hashlib
        decoded_len, decoded_sha1 = -1, ""
        if "base64," in data_uri:
            try:
                raw_audio = base64.b64decode(data_uri.split("base64,", 1)[1])
                decoded_len = len(raw_audio)
                decoded_sha1 = hashlib.sha1(raw_audio).hexdigest()
            except Exception:                            # noqa: BLE001
                pass
        rec = {
            "path": self.path,
            "model": payload.get("model"),
            "format": (payload.get("parameters") or {}).get("format"),
            "sample_rate": (payload.get("parameters") or {}).get("sample_rate"),
            "sse_header": self.headers.get("X-DashScope-SSE"),
            "auth_shape": (self.headers.get("Authorization") or "")[:12] + "…",
            "body_bytes": length,
            "data_uri_prefix": data_uri[:32],
            "data_uri_len": len(data_uri),
            "decoded_len": decoded_len,
            "decoded_sha1": decoded_sha1,
            "uri_is_data_scheme": data_uri.startswith("data:audio/"),
        }
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        body = json.dumps({"sentence": {"text": "四号"}}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                           # 别往控制台刷
        pass


if __name__ == "__main__":
    if os.path.exists(LOG):
        os.remove(LOG)
    print("假 ASR 端点起在 127.0.0.1:8099")
    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
