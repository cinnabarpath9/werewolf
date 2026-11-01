#!/usr/bin/env bash
# AI 狼人杀 · 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "[错误] 没找到 python3。请先安装 Python 3.10+。" >&2
  exit 1
fi

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "[提示] 已生成 .env 模板。可以在网页「设置 → 模型 API」里填，也可以直接编辑 .env。"
fi

if ! "$PY" -c "import fastapi, uvicorn, httpx, yaml, dashscope" >/dev/null 2>&1; then
  echo "[1/2] 正在安装依赖..."
  "$PY" -m pip install -r requirements.txt
fi

echo "[2/2] 启动服务：http://127.0.0.1:8077"
echo "      手机用同一 Wi-Fi 打开 http://<这台电脑的局域网IP>:8077"
echo "      配对码会打印在下面，也写在 配对码.txt 里。"
exec "$PY" -u server.py --port 8077
