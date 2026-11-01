# coding=utf-8
"""线上/临时实例的**配对码**从哪来 —— 不再写死在测试里。

2026-10-31：以前十几个浏览器测试把 `?token=<配对码>` 直接写在源码里。这样有两个问题：
  1. 配对码是**凭证**（能改设置、看复盘），写进源码等于写进公开仓库；
  2. 轮换配对码（删 `配对码.txt` 重启）会让这十几个测试一起红，测的人还以为功能坏了。

现在的口径（与 `server.py::_load_or_make_token` 一致）：
  · 先看环境变量 `WOLF_TOKEN`（CI / 临时换码时用）；
  · 否则读项目根的 `配对码.txt`，取里面第一个 6 位大写码；
  · 都没有就**明确报错**，不要悄悄跑一个空码（空码只会得到 401，看起来像功能坏了）。

用法：
    from live_token import TOKEN              # 直接拿码
    from live_token import read_token, live_url
    URL = live_url("/")                       # http://127.0.0.1:8077/?token=XXXXXX
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
TOKEN_FILE = os.path.join(PROJECT, "配对码.txt")

_CACHE: dict[str, str] = {}


def read_token(required: bool = True) -> str:
    """读这台实例的配对码。`required=False` 时找不到返回空串（给"只想拼 URL"的脚本用）。"""
    if "token" in _CACHE:
        return _CACHE["token"]

    env = str(os.environ.get("WOLF_TOKEN") or "").strip().upper()
    if env:
        _CACHE["token"] = env
        return env

    token = ""
    try:
        with open(TOKEN_FILE, encoding="utf-8-sig") as fh:
            m = re.search(r"[A-Z0-9]{6}", fh.read().upper())
            if m:
                token = m.group(0)
    except OSError:
        token = ""

    if not token and required:
        raise SystemExit(
            f"找不到配对码：{TOKEN_FILE} 不存在或里面没有 6 位码。\n"
            f"  要么先启动一次服务（会自动生成），要么设环境变量 WOLF_TOKEN=<码>。"
        )
    _CACHE["token"] = token
    return token


def live_url(path: str = "/", port: int = 8077, host: str = "127.0.0.1") -> str:
    """拼一个带配对码的 http 地址（浏览器测试都用它）。"""
    p = path if path.startswith("/") else "/" + path
    return f"http://{host}:{port}{p}?token={read_token()}"


def live_ws(port: int = 8077, host: str = "127.0.0.1") -> str:
    return f"ws://{host}:{port}/ws?token={read_token()}"


TOKEN = read_token()
