#!/usr/bin/env bash
# 在安卓手机上启动狼人杀服务（Termux）。
# 用法：bash ANDROID-START.sh
set -e

cd "$(dirname "$0")"

# Termux 默认会在锁屏后挂起；有 termux-api 就申请唤醒锁，没有也不影响启动。
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

HOST_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
if [ -z "$HOST_IP" ]; then
  HOST_IP="$(ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print $2; exit}')"
fi

echo
echo "狼人杀服务启动中…"
echo "手机自己打开： http://127.0.0.1:8077"
if [ -n "$HOST_IP" ]; then
  echo "同一个 Wi-Fi 的朋友打开： http://$HOST_IP:8077"
fi
echo "联机码和配对码会在启动后打印，也写在目录里的 联机码.txt / 配对码.txt。"
echo "停止服务：按 Ctrl+C"
echo

exec python server.py --host 0.0.0.0 --port 8077
