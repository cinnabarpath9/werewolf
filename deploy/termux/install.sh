#!/usr/bin/env bash
# 安卓 Termux 一键装依赖（不建虚拟环境，Termux 里直接装到 Python 用户目录）。
# 用法：把整个 werewolf-android-*.zip 解压后，在 Termux 里 cd 进目录，执行：
#   bash ANDROID-INSTALL.sh
set -e

cd "$(dirname "$0")"

echo "== 1/3 更新软件源并安装 Python =="
pkg update -y
pkg install -y python

echo "== 2/3 升级 pip =="
python -m pip install --upgrade pip

echo "== 3/3 安装项目依赖 =="
python -m pip install -r requirements.txt

echo
echo "装好了。启动：bash ANDROID-START.sh"
echo "第一次启动会自动生成 .env / 配对码.txt / 联机码.txt。"
