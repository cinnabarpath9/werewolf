# coding=utf-8
"""手机端服务：FastAPI + WebSocket + 一个手机竖屏单页。

跑法：
    python server.py                 # 默认 0.0.0.0:8077
    python server.py --port 8077 --mock-default

手机连：手机和电脑在同一个 WiFi 下，浏览器打开 http://<电脑局域网IP>:8077
（Windows 第一次会弹防火墙，选"允许"。麦克风要等 HTTPS，下一步再接。）

隔离纪律：服务器推给手机的，是这个座位的视角 —— 复用的还是 ledger 的可见性过滤，
所以手机端天然看不到狼队夜间讨论、也看不到别人的私有信息。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
# ⚠️ 客户端上传中途断开时 `request.stream()` 抛的就是它（P1 的根因，2026-10-08 修）：
#    从 starlette 里直接 import，而不是 `except Exception` 一把抓 —— 抓宽了会把真正的
#    代码 bug 也吞成"上传中断"，那就再也查不出来了。
from starlette.requests import ClientDisconnect  # noqa: E402

from wolf import (experience, persona, reports, routing, rules, settings_store,  # noqa: E402
                  tabletalk, voice)
from wolf.agents import LLMAgent, MockAgent, WebHumanAgent  # noqa: E402
from wolf.engine import CAUSE_TEXT, Game, parse_sheriff_setting, timeouts_from_cfg  # noqa: E402, CAUSE_TEXT
from wolf.llm import LLMError, no_real_calls  # noqa: E402
from wolf.roles import (BUILTIN_BOARDS, ROLES, board_summary, camp_label,  # noqa: E402
                        make_board, role_name, validate_board)

HERE = os.path.dirname(os.path.abspath(__file__))

def _data_home() -> str:
    """配对码 / 联机码这类**用户要看的文件**放哪。

    源码运行 = `server.py` 旁边；打包成 exe（launcher 会设 `WOLF_HOME`）= exe 旁边。
    不这么分的话，冻结后 `__file__` 指向 `_internal` 目录，用户根本找不到 `配对码.txt`。
    """
    return os.environ.get("WOLF_HOME") or HERE
WEB_DIR = os.path.join(HERE, "web")


MAX_ENV_VALUE = 4096          # 一个值最多这么长（.env 单行超过 32767 会让 os.environ 直接抛错）


# --------------------------------------------------------------------------- #
# Windows 上"一边读一边写"的坑：读的人会挡住改名，改名的一瞬间读的人也会被拒。
# ⚠️ 2026-10-06 复核实测：一边不停读 .env、一边连写 40 次，**失败 27 次**
# （每次都从 .bak 回滚，内容不会坏，但这一次保存对用户就是失败的）。
# 读取间隔 ≥10ms 时 0 次失败 —— 说明这就是毫秒级的抢占，退一步再试即可。
# 真实触发场景：手机面板在读配置的同时，电脑上正好在跑 tools/fill_from_txt.py 写 .env。
# --------------------------------------------------------------------------- #

def _open_text_retry(path: str, attempts: int = 24, delay: float = 0.02,
                     encoding: str = "utf-8-sig", errors: str | None = None):
    """打开文本文件的重试版（只在"被占用/拒绝访问"时重试；文件真不在就直接抛）。

    ⚠️ 尝试次数与退避都往宽了给：Windows 上"对方正以不共享删除的方式打开着"时，
    重试窗口太短会整段落在对方占用的时间片里 —— 把 12 次/≈0.9 秒放宽到
    24 次/≈3 秒（带抖动，避免和对面的读写节奏共振）之后，敌意读写的失败率明显下降。

    ⚠️ 默认编码是 `utf-8-sig` 而不是 `utf-8`：记事本存「UTF-8 带签名」时文件头有 BOM，
    用 `utf-8` 读出来首行的键名会变成 `\\ufeffKEY`，`.env` 的第一条变量就**静默读不到**
    （2026-10-31 外部审查 P1-01）。`utf-8-sig` 对没有 BOM 的文件行为完全一样。
    """
    last: OSError | None = None
    kwargs: dict = {"encoding": encoding}
    if errors:
        kwargs["errors"] = errors
    for i in range(attempts):
        try:
            return open(path, **kwargs)
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
            time.sleep(min(delay * (i + 1), 0.2) + random.uniform(0, 0.02))
    assert last is not None
    raise last


def _replace_with_retry(src: str, dst: str, attempts: int = 24, delay: float = 0.02) -> None:
    """`os.replace` 的重试版：被别人"以不共享删除的方式"打开着读时，Windows 会拒绝改名。

    调用方（`_update_env_file_locked`）拿不到这一层就会抛 PermissionError 给用户，
    所以这里宁可多等一会儿（≈3 秒）也别立刻失败。
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
            time.sleep(min(delay * (i + 1), 0.2) + random.uniform(0, 0.02))
    assert last is not None
    raise last


def load_env(path: str, force: bool = False) -> None:
    """读 .env。force=True 时覆盖已有环境变量（用于"手机上点一下重读 key"）。

    ⚠️ 单行值太长（>32767）时 `os.environ[k]=v` 会抛 ValueError。这里必须兜住：
    否则 .env 里只要有一行脏数据，服务**每次启动都会崩**，只能手改文件才能救回来。
    """
    if not os.path.exists(path):
        return
    skipped: list[str] = []
    # ⚠️ 2026-10-30：.env 被记事本存成 GBK/ANSI 时，迭代时会抛 UnicodeDecodeError
    #    （它不是 OSError，`_open_text_retry` 兜不住）→ 手机点「重读 .env」直接断连。
    #    这里兜成"按 GBK 尽力读 + 打一行警告"，别再让一个编码问题炸掉整条 WS。
    try:
        with _open_text_retry(path) as fh:
            text = fh.read()
    except UnicodeDecodeError:
        print(f"[env] {path} 不是 UTF-8，按 GBK 兜底读；建议另存为 UTF-8")
        # ⚠️ 兜底这一路也要走重试版：否则「文件是 GBK + 同时被记事本占用」时
        #    PermissionError 会直接冒到 /api/env 的「重读 .env」上（2026-10-31 外部审查 P2）。
        with _open_text_retry(path, encoding="gbk", errors="replace") as fh:
            text = fh.read()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # `\\ufeff` 再兜一次：万一有人用 utf-8 以外的方式把 BOM 读进来了，键名也别带它。
        key, value = key.strip().lstrip("\ufeff"), value.strip()
        if not value:
            continue
        try:
            if force:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
        except (ValueError, OSError) as exc:       # 名字或值不合法：跳过这一行，别把服务带崩
            skipped.append(f"{key}（{type(exc).__name__}）")
    if skipped:
        print(f"[env] 有 {len(skipped)} 行没读进来：" + "、".join(skipped[:5]))


def pick(mapping, *keys):
    if not mapping:
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
        if str(key) in mapping:
            return mapping[str(key)]
    return None


def _block_has_key(block: dict | None) -> bool:
    block = block or {}
    if block.get("api_key"):
        return True
    env_name = block.get("api_key_env")
    if env_name and os.environ.get(str(env_name)):
        return True
    profile = block.get("profile")               # 平台档案：<名字>_API_KEY
    if profile and os.environ.get(f"{str(profile).strip().upper()}_API_KEY"):
        return True
    return False


# --------------------------------------------------------------------------- #
# 在手机/浏览器上直接填 .env（key 不经过对话，只写进这台电脑）
# --------------------------------------------------------------------------- #

ENV_PATH = os.path.join(HERE, ".env")
#: 只允许写这几类变量，防止有人在局域网上往 .env 里塞别的东西。
#: ⚠️ 2026-10-31 外部审查 P1-02：这里以前只认 `[A-Z0-9_]`，而 `SAFE_NAME_RE`（178 行）
#: 与 `settings_store.SAFE_ID` 都允许 `.`/`-` —— 页面能存 `GLM-4.6`、写入口却拒收，
#: 而 `profile_vars()` 又会把 `.`/`-` 删掉再拼 env 名，三套口径对不上。
#: 现在统一成"允许 . 和 -"，与那两个正则同一口径。
ENV_NAME_RE = re.compile(r"^[A-Z0-9_.\-]*_(?:BASE_URL|API_KEY|MODEL|WS_URL)$")
#: 变量名只要**不破坏 .env 的行格式**就行：字母数字下划线开头，后面允许 . 和 -
#: （`fill_from_txt.py` 会给 `GLM-4.6` 这类模型生成 `GLM-4.6_BASE_URL` 这样的档案名，
#:   卡成"必须是合法 shell 标识符"会把那个工具弄坏。真正要防的是 = 空白 换行 # 这些
#:   能把一行拆成两行的字符。）
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
#: 写 .env 时禁止出现的字符。
#: ⚠️ 光挡 \r\n\x00 是不够的：`str.splitlines()` 还会在 \x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029
#: 上断行 —— 攻击者把 U+2028 藏进"值"里，第一次写入落成一行，
#: 第二次普通写入读回时就会被切成两行、注入出任意变量（审计实测把 PATH 改掉了）。
#: 所以这份名单必须跟 splitlines 的分行字符集对齐。
BAD_VALUE_CHARS = "\r\n\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
#: 我们自己页面上带的头。跨站请求要加这个头就必须先发预检，而本服务没有 CORS 中间件
CLIENT_HEADER = "x-wolf-client"
ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
SUFFIXES = ("_BASE_URL", "_API_KEY", "_MODEL", "_WS_URL")


def mask(name: str, value: str) -> str:
    if not value:
        return ""
    if name.endswith("API_KEY"):
        return "****" + value[-4:] if len(value) > 4 else "****"
    return value


def env_entries() -> list[dict]:
    """列出 .env 里我们关心的变量（key 只回显后四位）。"""
    entries: list[dict] = []
    if not os.path.exists(ENV_PATH):
        return entries
    with _open_text_retry(ENV_PATH) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if not ENV_NAME_RE.match(name):
                continue
            entries.append({
                "name": name,
                "kind": ("key" if name.endswith("API_KEY")
                         else "url" if name.endswith(("BASE_URL", "WS_URL")) else "model"),
                "filled": bool(value),
                "masked": mask(name, value),
            })
    return entries


def env_profiles() -> list[dict]:
    """把 <档案名>_BASE_URL / _API_KEY / _MODEL 归成一个平台。"""
    out: dict[str, dict] = {}
    for entry in env_entries():
        for suffix in SUFFIXES:
            if entry["name"].endswith(suffix):
                name = entry["name"][:-len(suffix)]
                out.setdefault(name, {"profile": name, "base_url": "", "model": "",
                                      "has_key": False, "key_tail": ""})
                if suffix == "_BASE_URL":
                    out[name]["base_url"] = entry["masked"]
                elif suffix == "_MODEL":
                    out[name]["model"] = entry["masked"]
                elif suffix == "_API_KEY":
                    out[name]["has_key"] = entry["filled"]
                    # 2026-10-31 外部审查 S18：`.env` 来源的行以前 `key_tail` 恒为空，
                    # 页面没法核对"这条 key 到底是哪一把"。`masked` 里已经是 `****后四位`。
                    out[name]["key_tail"] = entry["masked"]
                break
    return [out[k] for k in sorted(out)]


#: 写 .env 的进程内锁。
# ⚠️ 为什么必须要有：`update_env_file` 是"读整个文件 → 改 → 再写回去"，
# 没有锁的时候两个写入者会互相覆盖（审计实测：6 个线程同写，最后只活下来 1 个变量）。
# HTTP 路径本身是串行的，但工具脚本会并发调用同一个函数，所以锁加在函数里而不是路由里。
# 说明：这把锁只挡**同一个进程内**的并发；跨进程（同时跑两个脚本）仍是后写的赢，
# 要挡那个得用锁文件，本次没做（审计证据是 6 线程，属于进程内）。
_ENV_LOCK = threading.Lock()


def update_env_file(updates: dict[str, str], remove=()) -> dict:
    """把值写回 .env：保留原来的注释和顺序，没有的变量追加到末尾。先备份成 .env.bak。

    `remove`：要从 .env 里删掉的变量名集合（页面删除 `.env` 档案时用）。

    ⚠️ 这里自己做一遍校验，而不是只依赖 HTTP 层的白名单：
    早先的漏洞是"变量名合法、但值里带 \\n"，拼出来就多出一行、等于写进任意变量。

    三个保证：
      · 整个"读-改-写"在 `_ENV_LOCK` 里，并发的调用者不会互相覆盖；
      · 落盘走 `临时文件 + os.replace`（同目录改名是原子的），读的人永远看不到半个文件；
      · 写盘失败就从 `.env.bak` 回滚，不会把 .env 留成半截。
    另外：Windows 上"正好有人在读这个文件"会让改名被拒，所以改名与读取都带毫秒级重试
    （见 `_replace_with_retry` / `_open_text_retry`）。
    """
    for name, value in updates.items():
        if not SAFE_NAME_RE.match(str(name)):
            raise ValueError(f"变量名不合法：{name!r}")
        if not ENV_NAME_RE.match(str(name).upper()):
            # 白名单：只允许写「<平台>_BASE_URL / _API_KEY / _MODEL / _WS_URL」这几种。
            # 不然直调这个函数的入口（比如工具脚本）能写 PATH、WOLF_SEAT_PROFILES 之类。
            raise ValueError(f"不允许写这个变量名：{name}")
        text = str(value)
        if any(ch in text for ch in BAD_VALUE_CHARS):
            raise ValueError(f"{name} 的值里不能有换行或空字符")
        if len(text) > MAX_ENV_VALUE:
            raise ValueError(f"{name} 的值太长了（{len(text)} 字符，上限 {MAX_ENV_VALUE}）")

    remove_names: set[str] = set()
    for name in remove or ():
        raw = str(name or "").strip()
        if not SAFE_NAME_RE.match(raw) or not ENV_NAME_RE.match(raw.upper()):
            raise ValueError(f"不允许删除这个变量名：{name!r}")
        remove_names.add(raw.upper())
    with _ENV_LOCK:
        return _update_env_file_locked(updates, remove_names)


def _update_env_file_locked(updates: dict[str, str], remove=frozenset()) -> dict:
    """真正读-改-写的那一段（调用前必须已经拿到 `_ENV_LOCK`）。"""
    backup = ENV_PATH + ".bak"
    #: 这一次调用有没有**真的**做出一份新鲜备份。
    #: ⚠️ 2026-10-31 外部审查 S6：以前备份失败被吞掉后照样往下写，写失败时又去拿
    #:    `.env.bak` 回滚 —— 可那可能是**好几天前**的一份，等于用旧内容盖掉中间若干次
    #:    成功保存的新值（比"没回滚"更糟）。现在只回滚"这一次刚拍下来的"那份。
    made_backup = False
    if os.path.exists(ENV_PATH):
        # ⚠️ 2026-10-30：Windows 上 .env.bak 可能正被编辑器/杀毒软件占用，
        #    copyfile 会抛 PermissionError 冒到路由（那边只兜 ValueError）→ 500。
        #    备份失败不该阻断保存：打一行日志，继续往下写。
        try:
            shutil.copyfile(ENV_PATH, backup)
            made_backup = True
        except OSError as exc:
            print(f"[env] 备份 {backup} 失败（继续保存）：{type(exc).__name__}: {exc}")
        with _open_text_retry(ENV_PATH) as fh:
            lines = fh.read().splitlines()
    else:
        lines = []

    replaced: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        match = None if stripped.startswith("#") else ENV_LINE_RE.match(stripped)
        if match and match.group(1).upper() in remove:
            removed.append(match.group(1))
        elif match and match.group(1) in updates:
            name = match.group(1)
            out.append(f"{name}={updates[name]}")
            replaced.append(name)
            seen.add(name)
        else:
            out.append(line)

    appended: list[str] = []
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={value}")
            appended.append(name)

    data = ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")
    tmp = f"{ENV_PATH}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())          # 先落盘再改名：断电也不会留下空文件
        _replace_with_retry(tmp, ENV_PATH)  # 原子替换（被人挡住会自动退一步重试）
    except BaseException:                  # noqa: BLE001 —— 包括磁盘满/权限/被杀
        try:
            os.remove(tmp)
        except OSError:
            pass
        try:                               # 回滚：把**这一次的**备份盖回去，别留半截 .env
            if made_backup and os.path.exists(backup):
                shutil.copyfile(backup, ENV_PATH)
                print("[env] 写入失败，已从 .env.bak 回滚")
        except OSError:
            pass
        raise
    return {"replaced": sorted(replaced), "appended": sorted(appended),
            "removed": sorted(removed)}


def needs_token(request: Request) -> bool:
    """读/写配置一律要配对码。

    早先这里放过了"本机来源"，但那只看来访 IP，有两个洞：
      · 同机反代 / 隧道（nginx、tailscale serve、ssh -R）会让外部请求的来源变成 127.0.0.1
      · 浏览器里的任意网页也能以 127.0.0.1 的身份打过来（CSRF）
    所以现在一律要码。参数保留是为了 /api/status 告诉前端"要不要显示配对码输入框"。
    """
    return True


def client_ok(request: Request) -> bool:
    """要求请求带上我们自己页面的标识头，且 Host 得是 IP/localhost。

    两道合起来才成立：
      · 标识头 → 跨站请求加它就要预检，而本服务不应答预检
      · Host 白名单 → 挡住 DNS rebinding（攻击者域名解析到 127.0.0.1 后，
        浏览器会把它当同源，自定义头就随便加了；那种请求的 Host 是域名，这里直接拒）
    """
    return host_ok(request) and request.headers.get(CLIENT_HEADER, "") == "panel"


#: 配对码输错的记录：{客户端 IP: [失败次数, 上次失败时间]}
#: 读接口和写接口**共用这一张表**：只读接口也要退避，否则可以无限撞码。
_TOKEN_FAILS: dict[str, list] = {}
#: 多久没再出错就把这条记录清掉（失败计数本来 120 秒就重置，退避锁只锁 30 秒，
#: 所以 300 秒前的记录已经没有任何作用了）。以前这张表以 IP 为键**永久**保留。
_TOKEN_FAIL_TTL = 300.0
#: 兜底上限：真被大量来源 IP 灌也不让它无限涨
_TOKEN_FAIL_MAX = 4096
#: ⚠️ 2026-10-31 外部审查 S7：这张表是**跨线程共享**的（同步路由跑在 FastAPI 的线程池里），
#: 而 `_prune_token_fails` 会 `sorted(...items())` —— 裁剪的同时别的线程 insert/pop，
#: 会抛 `RuntimeError: dictionary changed size during iteration`（表现成 500，而不是退避提示）。
#: 读-改-写也会丢更新，使「8 次退避」可以被并发绕过去。所有进出都走这把锁。
_TOKEN_LOCK = threading.Lock()
#: 撞码撞太多次的提示（读写接口共用同一句，前端不用分别处理）
_TOKEN_BACKOFF_MSG = "配对码错太多次了，等 30 秒再试"
_TOKEN_WRONG_MSG = ("配对码不对。配对码写在电脑上的 werewolf\\配对码.txt 里，"
                    "也打印在服务窗口里（重启服务不会变）")


def _prune_token_fails(now: float) -> None:
    """清掉过期的失败记录，并给表一个硬上限。**调用方必须已经持有 `_TOKEN_LOCK`。**"""
    for who, rec in list(_TOKEN_FAILS.items()):
        try:
            last = float(rec[1])
        except (TypeError, ValueError, IndexError, KeyError):
            _TOKEN_FAILS.pop(who, None)         # 结构不对（理论上不会）：直接扔
            continue
        if now - last > _TOKEN_FAIL_TTL:
            _TOKEN_FAILS.pop(who, None)
    if len(_TOKEN_FAILS) > _TOKEN_FAIL_MAX:
        newest = sorted(list(_TOKEN_FAILS.items()), key=lambda kv: kv[1][1], reverse=True)
        _TOKEN_FAILS.clear()
        _TOKEN_FAILS.update(dict(newest[: _TOKEN_FAIL_MAX // 2]))


def _client_key(request: Request) -> str:
    return (request.client.host if request.client else "") or "?"


def _token_problem(request: Request, token) -> str | None:
    """配对码校验的**唯一入口**：读写接口共用同一张失败表和同一套退避文案。

    返回 None = 通过；否则返回一句直接能给用户看的人话。
    """
    if ROOM is None or not needs_token(request):
        return None
    given = str(token or "").strip().upper()
    if not given:
        # ⚠️ 2026-10-30：**没填码 ≠ 猜错码**。以前两者共用一张失败表，
        #    前端没填码时每开一个面板（规则/人设/复盘）都记一次失败，
        #    很快把计数顶到 8 —— 之后即使填对码，也要再等 30 秒退避。
        return "还没填配对码：填一下电脑上 `配对码.txt` 里的那一串。"
    now = time.time()
    who = _client_key(request)
    with _TOKEN_LOCK:                        # 见 `_TOKEN_LOCK` 的注释：读写裁剪都要串行
        _prune_token_fails(now)
        rec = _TOKEN_FAILS.get(who) or [0, 0.0]
        fails, last = int(rec[0]), float(rec[1])
        if fails >= 8 and now - last < 30:
            return _TOKEN_BACKOFF_MSG
        if given == str(ROOM.env_token or "").strip().upper():
            _TOKEN_FAILS.pop(who, None)
            return None
        fails = fails + 1 if now - last < 120 else 1
        _TOKEN_FAILS[who] = [fails, now]
        return _TOKEN_WRONG_MSG


def token_error(request: Request, body: dict) -> str | None:
    """写接口：配对码不对就返回一句人话，对了返回 None。带失败退避。"""
    return _token_problem(request, body.get("token"))


#: Host 头白名单：只接受 IP 字面量、localhost、mDNS/Tailscale 名。
#: 为什么要管这个：`X-Wolf-Client` 那道闸靠"跨站要加自定义头就得预检"，
#: 但 DNS rebinding 会把攻击者域名解析到 127.0.0.1、让浏览器当成同源 —— 头就能随便加。
#: 拒绝域名（只认 IP/localhost/内网名）能把这条路直接堵死。
_HOST_OK = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|[\w.\-]+\.(?:local|ts\.net)|"
                      r"\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\])(?::\d+)?$")

#: 本机自己的名字（Windows 上常见的 `http://DESKTOP-XXX:8077`）。
#: ⚠️ 2026-10-06 修（服务/前端审查的 P2-2）：只认带点号的域名后缀时，
#:    用机器名打开页面会**所有 /api/* 403、WS 被拒**，而前端把 403 当成"配对码不对"，
#:    于是用户填对了码也连不上、排查方向被彻底带偏。这里把机器名也放行。
_LOCAL_HOSTNAMES: set[str] = {h.lower() for h in (
    os.environ.get("COMPUTERNAME") or "", socket.gethostname() or "",
    (socket.gethostname() or "").split(".")[0]) if h}


def host_ok(request: Request) -> bool:
    host = (request.headers.get("host") or "").strip()
    if not host:
        return False
    bare = host.split(":")[0].strip("[]").lower()
    if bare in _LOCAL_HOSTNAMES:
        return True
    return bool(_HOST_OK.match(host))


def profile_vars(profile: str) -> tuple[str, str, str]:
    # ⚠️ 2026-10-31 外部审查 P1-02：以前这里把 `.`/`-` 和空格一起删掉再拼变量名，
    #    于是档案 id `GLM-4.6` 去找 `GLM46_API_KEY` —— 而 `settings_store.apply()`
    #    写进进程环境的是 `GLM-4.6_API_KEY`，两边永远对不上（页面能存、路由找不到 key）。
    #    现在只挡"能破坏 env 名/行格式"的字符，`.`/`-` 原样保留。
    prefix = re.sub(r"[^A-Z0-9_.\-]", "", str(profile or "").strip().upper())
    return f"{prefix}_BASE_URL", f"{prefix}_API_KEY", f"{prefix}_MODEL"


#: 配对码文件的内容。**只留这一份模板**。
# ⚠️ 以前 `_load_or_make_token()` 和 `main()` 各写一份一模一样的文案，改的时候只改了前者，
# 于是每次启动 main() 都拿旧文案（"每次重启会换新的码"）把正确的覆盖掉 ——
# 用户打开 配对码.txt 看到的就是错的那句（2026-10-06 复核发现）。现在两个入口共用这一个模板。
TOKEN_FILE_TEMPLATE = (
    "手机端填 key 的配对码：{token}\n\n"
    "用法：打开游戏页面 →「填 API Key / 换平台」→ 把它填进「配对码」框。\n"
    "手机和电脑浏览器都要填（浏览器会记住，只需填一次）。\n"
    "⚠️ 这个码会一直用下去（重启服务不变）。想换新的就删掉这个文件再启动。\n"
)

#: 联机码文件的内容。联机码只允许「加入房间」，**不等于**配对码（配对码能改设置、开服务接口）。
#: 2026-10-31 加：多人联机时把「房间」和「管理」两把钥匙分开。
JOIN_CODE_TEMPLATE = (
    "联机码：{code}\n\n"
    "给朋友加入房间用：打开 http://<主机IP>:8077/?join={code} ，填名字就能加入。\n"
    "⚠️ 它不能改设置、不能看复盘；想换码就删掉这个文件再重启服务。\n"
)


def write_token_file(path: str, token: str) -> None:
    """把配对码写进文件（启动时、以及第一次现生成时都走这里）。

    ⚠️ 2026-10-31 外部审查 S12：以前是裸 `open(path, "w")` —— 先截断再写，
       进程在中途被杀就留下空文件/半截文件，下次启动会当成"没有码"重新生成一个，
       手机上记住的旧码**全部失效**。现在与 `.env` 同一套：临时文件 + fsync + 原子改名。
    """
    payload = TOKEN_FILE_TEMPLATE.format(token=token)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8-sig") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #

#: 卡住/报错时推给手机的动作名 —— **必须与身份无关**。
#: ⚠️ 2026-10-06 修（服务/前端审查的 P1）：以前直接把 `task.kind` 推给手机，
#:    而 kind 与身份一一对应（`seer_check`=预言家、`wolf_kill`=狼、`witch`=女巫…），
#:    只有要调模型的座位才会卡住、真人座位不会 —— 等于**每条弹窗都在公开别人的身份**。
#:    同一个文件里 packet 事件早就特意剥掉了 task（注释写着"那等于实时公开身份表"），
#:    这里补上同样的纪律：夜间一律叫"夜间行动"。
NEUTRAL_TASK: dict[str, str] = {
    "wolf_talk": "夜间行动", "wolf_kill": "夜间行动", "seer_check": "夜间行动",
    "witch": "夜间行动", "guard": "夜间行动", "hunter_shoot": "夜间行动",
    "speech": "发言", "sheriff_speech": "发言", "last_words": "发言",
    "vote": "投票", "sheriff_vote": "投票", "sheriff_run": "上警决定",
    "badge": "警徽去向", "reflect": "复盘",
}


def _build_fallbacks(cfg: dict, seat, order: list | None) -> list:
    """按 config 的 `game.fallback` 顺序，给这个座位备好替补客户端。

    返回 [(平台名, 出厂函数), ...]（**懒建**：真要换家时才创建客户端）。

    ⚠️ 2026-10-31 外部审查 S9：这里原来套了一层 `try/except: continue`，注释说
       「没配好（缺 BASE_URL/KEY/MODEL）就跳过」—— 但循环体只 append 一个 lambda，
       构造期什么都不会抛，那段 try 是空转：缺 key 的替补会被留到**开局换家那一刻**才炸。
       现在如实说明：这里只管"名单拿不到就整体不要"，真正的可用性由 `routing.build_client`
       在换家时判；它抛错会被 `agent.use_fallback()` 那条路径捕获并提示。
    """
    out = []
    try:
        specs = routing.fallback_specs(cfg, seat, order)
    except Exception as exc:                              # noqa: BLE001
        print(f"[engine] 替补名单取不到，本局不换家：{type(exc).__name__}: {exc}")
        return []
    for name, spec in specs:
        # 存**出厂函数**而不是客户端：连接池等到真要换家时才建
        out.append((name, (lambda sp=spec, nm=name, sid=seat.id:
                           routing.build_client(sp, label=f"{sid}号/替补{nm}"))))
    return out


def _say_ok(kind: str, seat, visible_to, human_camp: str) -> bool:
    """这一条台账**该不该配音**（三个调用点共用，口径必须一致：推送 / 存档留档 / `/api/tts`）。

    · 公开发言（`voice.SPEECH_KINDS` = speech / pk_speech）→ 配（老口径不变）；
    · **狼队夜间讨论**（`kind='night'` 且只狼可见）→ **只有真人坐在狼队时才配**
      （用户 2026-10-08 要求）：没人在狼队里，配了也没人听、纯花钱；
      有人在狼队里，那是他在夜里唯一能"听见"的现场发言。
    ⚠️ 改这里要三个调用点一起改 —— 只改推送的话，前端点 🔊 会被 `/api/tts` 拒掉，
      存档留档也会漏（复盘里那条就重放不了）。
    """
    if kind in voice.SPEECH_KINDS:
        return seat is not None
    if kind == "night" and "wolf" in tuple(visible_to or ()):
        return seat is not None and human_camp == "wolf"
    return False


#: 台账 entry.data 允许进前端的键。实时/历史/轻量复盘共用一份，避免三处漂移。
_ENTRY_DATA_KEYS = ("target", "round", "sheriff_vote", "invalid", "prefetched")


class Room:
    """单房间：只有你一个人玩，所以全局就一个实例。"""

    def __init__(self, cfg: dict, mock_default: bool) -> None:
        self.cfg = cfg or {}
        self.game_cfg = self.cfg.get("game") or {}
        self.llm_cfg = self.cfg.get("llm") or {}
        self.mock_default = mock_default
        self.clients: dict[WebSocket, asyncio.Queue] = {}
        self.senders: dict[WebSocket, asyncio.Task] = {}   # 每连接的发送协程，断开时要收掉
        #: 队列塞满被丢过消息的连接 → 丢了几条。用来在它下次 sync 时提示"你错过了 N 条"。
        self.dropped: dict[WebSocket, int] = {}
        self.game: Game | None = None
        #: 当前这局的代号（= `game.run_token`）。引擎的每条事件都带 `_run`，
        #: 不等于它就丢掉 —— 防"上一局/已硬复位的任务还在发事件"污染新局。
        #: None = 现在没有"合法的局"（放弃/硬复位后置空），一切带代号的事件都不收。
        self.game_token: str | None = None
        self.task: asyncio.Task | None = None
        #: 兼容旧代码/测试的"当前那一手"；联机并发提问时真正的 future 在 `asks` 里。
        self.awaiting: asyncio.Future | None = None
        #: 座位 → 正在等回答的 future。投票/狼队决策是 gather 并发问的，多个真人会同时在等。
        self.asks: dict[int, asyncio.Future] = {}
        #: 座位 → 当前那条 ask（重连时只补发给这个座位的人）。
        self.pending_asks: dict[int, dict] = {}
        self.pending_ask: dict | None = None               # 最近一条，兼容旧代码/测试
        #: 2026-11-06「全程自动托管」：这些座位轮到时自动调托管模型，不再等玩家点。
        #: 每局重置；可在游戏页随时开/关（关闭会取消正在跑的那一手）。
        self.auto_butler_seats: set[int] = set()
        self.auto_butler_tasks: dict[int, asyncio.Task] = {}
        #: 所有托管调用（手动/自动）的在途座位；手动连点/自动+手动并发都靠它去重。
        self.butler_inflight: set[int] = set()
        #: 「轮到你」的报文序号（单调递增，新局重置）。客户端把它原样回传，
        #: 服务端在 `ask_done` 里带回同一个 id —— 客户端据此只清自己那一条面板
        #: （详见 broadcast 里的注释：补发的旧 ask 不许清掉玩家正在操作的新面板）。
        self.ask_seq = 0
        #: 模型连续失败导致**整局暂停**时的状态（用户要求：4 次重试还失败就暂停并弹窗）。
        #: {"seat": 3, "task": "vote", "error": "...", "stalled": 1} —— 重连时补发，前端据此弹窗。
        self.stall: dict | None = None
        #: 用户在弹窗里选的那一项（retry / skip / abort）。引擎正 await 它。
        self.stall_decision: asyncio.Future | None = None
        self.running = False
        #: 收尾中（存档 / 写复盘笔记 / 关客户端）。这段时间对局已经结束了，
        #: 但房间还不能接新局，所以**对外要报"正在收尾"**而不是"对局进行中"。
        self.cleaning = False
        #: 真人坐哪个座位。**默认值取 config.yaml 的 game.human_seat** ——
        #: 以前硬编码 1，结果是"配置里写了 6 号，手机上不选座位就坐到 1 号去了"
        #: （1 号有人设有模型，等于把 AI 挤掉一个、自己坐错位置）。
        self.human_seat = max(1, int(self.game_cfg.get("human_seat") or 1))
        self.human_timeout = float(self.game_cfg.get("human_timeout", 900))
        #: 上游挂住时的两层保险（秒）：单次硬上限 / 整手预算。
        #: ⚠️ **建局与 status 必须用同一对数**（2026-10-08 收敛）：以前这里是 150、
        #:    status 那边是 300 —— 配置里漏写键名时，手机会显示一个引擎根本不会遵守的秒数
        #:    （详见 `wolf/engine.py::timeouts_from_cfg` 上面那段）。
        self.call_timeout, self.decision_timeout = timeouts_from_cfg(self.game_cfg)
        #: **自动存盘**（用户 2026-10-08 的血泪教训：打了一个半小时的局，进程一没就全没了）：
        #: 每过一个阶段覆盖写一次同一个存档，最坏只损失最后一个阶段。`game.autosave: false` 可关。
        self.autosave = bool(self.game_cfg.get("autosave", True))
        #: 卡住之后等用户点按钮的上限（秒）。到点没人理就按"跳过（随机处理）"继续，
        #: 免得用户睡了、对局永远挂在那儿。默认 30 分钟。
        self.stall_timeout = float(self.game_cfg.get("stall_timeout", 1800))
        self.clients_llm: list[LLMClient] = []
        #: 2026-11-02「公共体验实例」：访客自带 base_url/model/key，**只存在内存**，
        #: 一局用完就清（不写 settings.local.json、不写 .env）。见 WOLF_DEMO。
        self.session_provider: dict = {}
        #: 公共体验模式：不允许改服务端设置；访客必须自带 key（或玩离线演练）。
        self.demo_mode = str(os.environ.get("WOLF_DEMO") or "").strip() not in ("", "0", "false")
        self.mock = mock_default
        self.rng = random.Random()
        self.last_save: str | None = None
        self.last_save_mock: bool = False
        #: 本局存档名（自动存盘与终局存盘共用同一个文件）；开新局时在 start() 里清空
        self.save_stem: str | None = None
        self.autosave_count = 0
        self.personas = persona.load(cfg, HERE)
        #: 手机/电脑端填 key 都要用的配对码（一律要，见 needs_token）
        #: ⚠️ **跨重启保持不变**：以前每次重启都换新码，结果手机上刚填好的码
        #: 一会儿就"失效"了（用户看到的就是"配对码不生效"）。现在从文件读，
        #: 没有才生成一次并写回去。想强制换新码：删掉 配对码.txt 再启动。
        self.env_token = self._load_or_make_token()
        # ⚠️ 这一行以前写在 `_load_or_make_token` 的 `return token` **之后**（死代码），
        # 于是 experience: true 时开局会抛 AttributeError、手机只看到 WS 被断开、没有任何提示。
        self.experience_dir = os.path.join(HERE, self.game_cfg.get("experience_dir") or "experience")

        # ---------- 联机（2026-10-31） ----------
        #: 玩家表：pid → {pid, name, seat, online, host}。
        #: 主机默认坐 config 的 human_seat（单人局的老口径），也可以换座 / 空出来当观众。
        self.players: dict[str, dict] = {
            "host": {"pid": "host", "name": "主机", "seat": self.human_seat,
                     "online": False, "host": True},
        }
        #: 访客令牌 → pid。只在内存里：服务重启后访客要重新加入（联机局本来也会断）。
        self.guest_tokens: dict[str, str] = {}
        #: 每个 WebSocket 是谁：{role: host/guest, pid: ...}
        self.client_info: dict[WebSocket, dict] = {}
        #: 给朋友加入用的联机码（与配对码分开，不能改设置）。
        self.join_code = self._load_or_make_join_code()
        #: 这一局是不是联机局（有人在房间里 → True）。单人局保持 False，旧行为不变。
        self.multiplayer = False
        #: 联机局等真人的上限：单人 900 秒（默认，允许长考），联机时一个人去倒水就会晾住全场。
        self.human_timeout_solo = int(self.game_cfg.get("human_timeout", 900))
        self.multiplayer_timeout = int(self.game_cfg.get("multiplayer_human_timeout", 180))
        self.human_timeout = self.human_timeout_solo
        #: 房间大厅里主机正在挑的板子（还没开局时给访客看座位数用）。
        self.lobby_board = self.game_cfg.get("board") or "eleven_wolfking"
        #: 联机局开始后，哪些座位是真人（pid 反查用）。
        self.seat_owner: dict[int, str] = {}

    def _load_or_make_token(self) -> str:
        import re as _re
        # 云端部署（Render/Railway 这类容器）文件系统是临时的：每次重启都会生成新码，
        # 手机上刚填的码就失效了。所以允许用环境变量钉死：WOLF_PAIR_CODE=ABCDEF。
        env_code = (os.environ.get("WOLF_PAIR_CODE") or "").strip().upper()
        if _re.fullmatch(r"[A-Z0-9]{6}", env_code):
            return env_code
        path = os.path.join(_data_home(), "配对码.txt")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                m = _re.search(r"[A-Z0-9]{6}", fh.read())
                if m:
                    return m.group().upper()
        except OSError:
            pass
        token = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
        try:
            write_token_file(path, token)
        except OSError:
            pass
        return token

    # ---------- 联机：身份 / 房间 ----------

    def _load_or_make_join_code(self) -> str:
        """联机码：只用于「加入房间」，与配对码分开。

        跨重启保持不变（写 `联机码.txt`）；删掉文件才会换新的。
        极端情况下生成到和配对码一样的串，就再抽一次 —— 否则访客能顺手拿到管理权限。
        """
        import re as _re
        path = os.path.join(_data_home(), "联机码.txt")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                m = _re.search(r"[A-Z0-9]{6}", fh.read())
                if m and m.group().upper() != self.env_token:
                    return m.group().upper()
        except OSError:
            pass
        code = ""
        for _ in range(20):
            code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
            if code != self.env_token:
                break
        if not code or code == self.env_token:
            # 2026-10-31 外部审查 S17：20 次都撞上配对码（概率≈24⁻²⁰）时以前会带着
            # 一个"没定义/等值"的 code 继续往下写，现在给一个确定结果（直接换字母表第一段）。
            code = ("Z" * 6) if self.env_token != "ZZZZZZ" else "YYYYYY"
        try:
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(JOIN_CODE_TEMPLATE.format(code=code))
        except OSError:
            pass
        return code

    def player(self, pid: str) -> dict | None:
        return (getattr(self, "players", None) or {}).get(str(pid or ""))

    def identify(self, token: str) -> tuple[str, str] | None:
        """把 WS / HTTP 带来的 token 认成 (role, pid)。认不出返回 None。

        ⚠️ 配对码是大写 6 位；访客令牌是 `secrets.token_urlsafe` 生成的**大小写敏感**串
           （含 - 和 _）。这里不能统一 upper —— 否则访客 token 永远对不上（2026-10-31 实测踩到）。
        """
        raw = str(token or "").strip()
        if not raw:
            return None
        if raw.upper() == str(self.env_token or "").strip().upper():
            return ("host", "host")
        tokens = getattr(self, "guest_tokens", None) or {}
        # ⚠️ 2026-10-31 外部审查 S14：以前这里还有 `tokens.get(raw.upper())` /
        #    `tokens.get(raw.lower())` 两个回退，等于把"大小写敏感"的访客令牌变成模糊匹配，
        #    与本函数 docstring 和契约（不许统一 upper）直接相反。现在只认原串。
        pid = tokens.get(raw)
        if pid and pid in (getattr(self, "players", None) or {}):
            return ("guest", pid)
        return None

    def viewer_info(self, ws: WebSocket) -> dict:
        return (getattr(self, "client_info", None) or {}).get(ws) or {}

    def viewer_seat(self, ws: WebSocket) -> int:
        info = self.viewer_info(ws)
        p = self.player(info.get("pid") or "")
        return int((p or {}).get("seat") or 0)

    def viewer_is_host(self, ws: WebSocket) -> bool:
        return self.viewer_info(ws).get("role") == "host"

    def host_seat(self) -> int:
        return int((self.player("host") or {}).get("seat") or 0)

    def room_players_public(self) -> list[dict]:
        rows = []
        for p in sorted(self.players.values(),
                        key=lambda x: (0 if x.get("host") else 1, x.get("seat") or 99, x.get("pid"))):
            rows.append({"pid": p["pid"], "name": p["name"], "seat": p.get("seat"),
                         "online": bool(p.get("online")), "host": bool(p.get("host"))})
        return rows

    def room_public(self, viewer_pid: str = "", is_host: bool = False) -> dict:
        """房间大厅快照。访客拿不到联机码以外的管理信息，也拿不到别人的底牌。"""
        you = self.player(viewer_pid)
        seats = {}
        for pid, p in self.players.items():
            if p.get("seat"):
                seats[str(p["seat"])] = {"pid": pid, "name": p["name"], "host": bool(p.get("host"))}
        payload = {
            "multiplayer": self.multiplayer,
            "running": bool(self.running) and not self.cleaning,
            "cleaning": bool(self.cleaning),
            "board": (getattr(self, "board_name", "") if (self.running or self.cleaning)
                      else (getattr(self, "lobby_board", "")
                            or self.game_cfg.get("board") or "eleven_wolfking")),
            "host_pid": "host",
            "players": self.room_players_public(),
            "seats": seats,
            "you": ({"pid": you["pid"], "name": you["name"], "seat": you.get("seat"),
                     "host": bool(you.get("host"))} if you else None),
            "max_seats": 12,
            "join_code": self.join_code if is_host else "",
        }
        return payload

    def broadcast_room(self) -> None:
        for ws in list(self.clients.keys()):
            info = self.viewer_info(ws)
            self.send_to(ws, {"type": "room",
                              "room": self.room_public(info.get("pid") or "",
                                                       info.get("role") == "host")})

    def _claim_seat(self, pid: str, seat: int) -> tuple[bool, str]:
        p = self.player(pid)
        if p is None:
            return False, "没有这个玩家"
        if self.running:
            return False, "对局已经开始，不能换座位"
        try:
            seat = int(seat)
        except (TypeError, ValueError):
            return False, "座位号必须是数字"
        if not 1 <= seat <= 12:
            return False, "座位号只能是 1~12"
        for other in self.players.values():
            if other["pid"] != pid and other.get("seat") == seat:
                return False, f"{seat} 号已经被「{other['name']}」坐了"
        p["seat"] = seat
        self.multiplayer = True
        self.seat_owner = {int(q["seat"]): q["pid"] for q in self.players.values() if q.get("seat")}
        return True, ""

    def _release_seat(self, pid: str) -> tuple[bool, str]:
        p = self.player(pid)
        if p is None:
            return False, "没有这个玩家"
        if self.running:
            return False, "对局已经开始，不能换座位"
        p["seat"] = None
        self.seat_owner = {int(q["seat"]): q["pid"] for q in self.players.values() if q.get("seat")}
        return True, ""

    # ---------- 连接管理 ----------

    async def connect(self, ws: WebSocket, role: str = "host", pid: str = "host") -> asyncio.Queue:
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients[ws] = queue
        self.client_info[ws] = {"role": role, "pid": pid}
        p = self.player(pid)
        if p is not None:
            p["online"] = True
        self.senders[ws] = asyncio.create_task(self._sender(ws, queue))
        self.broadcast_room()
        return queue

    def disconnect(self, ws: WebSocket) -> None:
        info = self.client_info.pop(ws, None) or {}
        self.clients.pop(ws, None)
        self.dropped.pop(ws, None)
        task = self.senders.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()          # 不收掉的话，每次刷新页面都会留一个孤儿协程
        pid = info.get("pid")
        if pid:
            still = any((self.client_info.get(w) or {}).get("pid") == pid for w in self.clients)
            if not still:
                p = self.player(pid)
                if p is not None:
                    p["online"] = False
                self.broadcast_room()

    async def _sender(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        try:
            while True:
                payload = await queue.get()
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            self.clients.pop(ws, None)
            # 必须真的把连接关掉：不关的话前端 onclose 不触发 → 不会自动重连 →
            # 界面永远停在最后一帧（绿点还亮着），玩家以为"别人在思考"。
            try:
                await ws.close()
            except Exception:                      # noqa: BLE001
                pass
        finally:
            self.senders.pop(ws, None)

    def send_to(self, ws: WebSocket, payload: dict) -> None:
        """只发给某一个连接（补发历史记录用；广播会把别人的记录也推给所有人）。"""
        queue = self.clients.get(ws)
        if queue is None:
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:                  # noqa: PERF203
            self._mark_dropped(ws)

    def _mark_dropped(self, ws: WebSocket) -> None:
        """队列满 = 这条消息真的发不出去了：记一笔，等它下次 sync 时告诉它。

        ⚠️ 以前这里是 `pass` —— 静默丢消息。手机上表现为"少了几条发言/少了结局"，
        而且没有任何提示，看起来像服务端没发过。
        """
        self.dropped[ws] = self.dropped.get(ws, 0) + 1

    def _drain_stale(self, ws: WebSocket) -> int:
        """把某连接积压里"过期就没人看"的流水消息丢掉，返回丢掉几条。

        只丢台账条目（entry）和"谁在思考"（thinking）：这类消息可以靠补发历史重建，
        积压几百条反而会把手机淹掉。`end` / `ask` / `notice` 这类"少一条就出错"的消息保留。
        """
        queue = self.clients.get(ws)
        if queue is None:
            return 0
        kept: list[dict] = []
        dropped = 0
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, dict) and item.get("type") in ("entry", "thinking"):
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        return dropped

    def you_payload(self, seat: int) -> dict | None:
        game = self.game
        me = self._viewer_seat_obj(int(seat or 0))
        if game is None or me is None or not me.is_human:
            return None
        mates = [s.id for s in game.seats
                 if s.camp == me.camp and s.id != me.id] if me.camp == "wolf" else []
        # 2026-11-03：狼王禁爆（11 人板）——**能不能爆由引擎判定**，别让前端按角色名硬编码
        try:
            can_explode = bool(game.can_explode(me))
        except Exception:                          # noqa: BLE001 —— 老引擎没有这个方法
            can_explode = me.camp == "wolf"
        return {"type": "you", "seat": me.id, "role": role_name(me.role_key),
                "camp": camp_label(me.camp), "mates": mates,
                "can_explode": can_explode,
                "name": getattr(me, "card_name", "") or ""}

    def send_you(self, ws: WebSocket, seat: int) -> None:
        payload = self.you_payload(seat)
        if payload:
            self.send_to(ws, payload)

    def _entry_msg(self, entry, idx: int, me) -> dict:
        """把一条台账做成给手机的消息（实时推送和历史补发共用同一个形状）。"""
        return {"type": "entry", "entry": {
            "day": entry.day, "kind": entry.kind, "seat": entry.seat,
            "text": entry.text, "private": "public" not in entry.visible_to,
            "n": idx,
            "action": getattr(entry, "action", entry.kind),
            "summary": getattr(entry, "summary", ""),
            "data": {k: entry.data[k] for k in _ENTRY_DATA_KEYS
                     if isinstance(getattr(entry, "data", None), dict) and k in entry.data},
            "say": _say_ok(entry.kind, entry.seat, entry.visible_to,
                           getattr(me, "camp", "")) and entry.seat != me.id,
        }}

    def _entry_msg_public(self, entry, idx: int) -> dict:
        """观众（没坐座位）看到的版本：只发 public 条目，不带任何私有内容。"""
        return {"type": "entry", "entry": {
            "day": entry.day, "kind": entry.kind, "seat": entry.seat,
            "text": entry.text, "private": False, "n": idx,
            "action": getattr(entry, "action", entry.kind),
            "summary": getattr(entry, "summary", ""),
            "data": {k: entry.data[k] for k in _ENTRY_DATA_KEYS
                     if isinstance(getattr(entry, "data", None), dict) and k in entry.data},
            "say": _say_ok(entry.kind, entry.seat, entry.visible_to, ""),
        }}

    def _viewer_seat_obj(self, seat: int):
        game = self.game
        if not game or not seat or seat < 1 or seat > len(game.seats):
            return None
        return game.seat_by_id(seat)

    def broadcast_entry(self, entry, idx: int) -> None:
        """台账按**每个连接自己的座位**过滤后再发（联机局不能一份广播给所有人）。"""
        game = self.game
        if game is None:
            return
        if not self.clients:
            # 没有连接时（历史测试/收尾窗口）退回旧行为：按主机自己的座位造一条广播。
            me = self._viewer_seat_obj(self.human_seat)
            if me is not None and me.is_human and entry.visible(me.id, me.camp):
                self.broadcast(self._entry_msg(entry, idx, me))
            elif "public" in entry.visible_to:
                self.broadcast(self._entry_msg_public(entry, idx))
            return
        for ws in list(self.clients.keys()):
            seat = self.viewer_seat(ws)
            me = self._viewer_seat_obj(seat)
            if me is not None and me.is_human:
                if not entry.visible(me.id, me.camp):
                    continue
                self.send_to(ws, self._entry_msg(entry, idx, me))
            elif "public" in entry.visible_to:
                self.send_to(ws, self._entry_msg_public(entry, idx))

    def replay_history(self, ws: WebSocket, seat: int, limit: int = 400) -> None:
        """把已经发生过的台账补发给这个连接（按它的座位过滤）。

        ⚠️ 为什么需要：手机上切到别的应用再回来，页面会被浏览器重载，
        而重载后页面只显示"连上之后"的条目 —— 看起来就像"这一局重跑了"。
        台账一直好好躺在服务端，补发一遍就接上了（这也是"做成 APP 也没必要"的底气：
        只要补发做对，任何浏览器切来切去都不丢东西）。
        """
        if self.game is None:
            return
        me = self._viewer_seat_obj(int(seat or 0))
        if me is not None and not me.is_human:
            me = None
        entries = self.game.ledger.entries
        start = max(0, len(entries) - limit)
        for i in range(start, len(entries)):
            entry = entries[i]
            if me is not None:
                if not entry.visible(me.id, me.camp):   # 隔离照旧：补发也不能越权
                    continue
                msg = self._entry_msg(entry, i, me)
            elif "public" in entry.visible_to:
                msg = self._entry_msg_public(entry, i)
            else:
                continue
            # ⚠️ 打上"这是补发的"：前端据此**不配音**。以前补发和实时用同一个形状，
            #    于是每次切页面回来，历史里的每条发言都会各发一次 /api/tts
            #    （等于把整局语音重新合成一遍；"只播当前说话人"那条规则也白写了）。
            msg["replay"] = True
            self.send_to(ws, msg)

    def ask(self, seat_id: int, payload: dict) -> asyncio.Future:
        """把「轮到你」只发给坐这个座位的人，并返回**这个座位专属**的 future。

        - 同一个人开两台设备（手机 + 电脑）都会收到，答一次两边都清面板；
        - 观众/主机不坐这个座位就收不到，避免偷看底牌；
        - 投票/狼队决策是 gather 并发问的：每个座位各有一个 future，互不覆盖。
        """
        payload = dict(payload)
        payload["seat"] = int(seat_id)
        self.ask_seq = int(getattr(self, "ask_seq", 0) or 0) + 1
        payload["ask_id"] = self.ask_seq
        self.pending_asks[int(seat_id)] = payload
        self.pending_ask = payload
        self.ask_seat = int(seat_id)
        future = asyncio.get_running_loop().create_future()
        self.asks[int(seat_id)] = future
        self.awaiting = future          # 兼容旧代码/测试：指向最近开的那一手
        for ws in list(self.clients.keys()):
            if self.viewer_seat(ws) == int(seat_id):
                self.send_to(ws, payload)
        # 全程自动托管：面板照发（玩家看得到发生了什么），后台同时替这一手决策。
        if int(seat_id) in self.auto_butler_seats:
            self.kick_auto_butler(int(seat_id))
        return future

    def close_ask(self, seat_id: int, ask_id=None) -> None:
        """这一手作废（超时/放弃）：删掉待回答记录，别在重连时补发过期面板。"""
        seat = int(seat_id or 0)
        cur = self.pending_asks.get(seat)
        if ask_id is not None and cur is not None and cur.get("ask_id") != ask_id:
            return
        self.pending_asks.pop(seat, None)
        if self.pending_ask is cur:
            self.pending_ask = next(iter(self.pending_asks.values()), None)
        future = self.asks.pop(seat, None)
        if future is not None and not future.done():
            future.cancel()
    # ---------- 托管（2026-11-02） ----------
    #
    # 玩家这一手不想自己想（或者根本不知道该怎么决策）时，点「托管」：
    # **调用一次**单独配置的托管模型，看玩家自己的 packet（含他自己的发言与全场公示栏），
    # 替玩家出一手，然后当成玩家的答案交给引擎。
    def butler_client(self, seat_id: int | None = None, api_name: str | None = None):
        """按「设置 → 模型 API → 托管模型」选的那条 API 建一个客户端。返回 (client, 错误说明)。

        2026-11-03 审查 P2：以前固定拿 1 号座位解析画像（多真人房里替别人托管会选错角色画像），
        现在接收真正要托管的座位。

        2026-11-06：`api_name` 允许调用方指定替补档案（托管主模型失败时换家）。
        托管是"替真人出这一手"，不是主座位决策，所以只给 1 次客户端重试 ——
        上游卡住时别把 4×25 秒全烧完，交给 `autopilot` 换家。
        """
        local = settings_store.load(HERE)
        cfg_b = local.get("butler") or {}
        name = str(api_name or cfg_b.get("api") or "").strip().upper()
        if not name:
            return None, "还没配托管模型：设置 → 模型 API → 最下面「托管模型」选一条 API"
        game = getattr(self, "game", None)
        want = int(seat_id or 0)
        seat = next((s for s in (game.seats if game else []) if s.id == want), None)
        if seat is None:
            seat = next((s for s in (game.seats if game else []) if s.id == 1), None)
        if seat is None:
            class _Seat:                                  # resolve_profile 只要 id/role_key
                id = 1
                role_key = "villager"
            seat = _Seat()
        try:
            spec = routing.resolve_profile(self.cfg, seat, name)
        except Exception as exc:                          # noqa: BLE001
            return None, f"托管模型「{name}」解析失败：{type(exc).__name__}: {exc}"
        if not (spec.base_url and spec.api_key and spec.model):
            return None, f"托管模型「{name}」缺 base_url / key / model，先去设置里补齐"
        try:
            _mr = int(getattr(spec, "max_retries", 1))
        except (TypeError, ValueError):
            _mr = 1
        spec.max_retries = max(0, min(_mr, 1))
        if cfg_b.get("temperature") is not None:
            try:
                spec.temperature = float(cfg_b["temperature"])
            except (TypeError, ValueError):
                pass
        if cfg_b.get("max_tokens") is not None:
            try:
                spec.max_tokens = int(cfg_b["max_tokens"])
            except (TypeError, ValueError):
                pass
        # 2026-11-03：托管以前固定 plain 风格 → 配置的思考档根本没发出去（模型话少/想得浅）。
        # 现在按渠道推断它能认哪套参数，再把档位真的发下去；推断不出来的仍用 plain（不发）。
        from wolf import butler as butler_mod                     # noqa: PLC0415
        level = str(cfg_b.get("thinking") or "").strip().lower()
        if level in ("off", "low", "high"):
            spec.thinking = butler_mod.thinking_table(level)
            spec.thinking_style = butler_mod.infer_thinking_style(spec.base_url)
        try:
            client = routing.build_client(spec, label=f"butler/{name}")
        except Exception as exc:                          # noqa: BLE001
            return None, f"托管模型「{name}」建客户端失败：{type(exc).__name__}: {exc}"
        return client, ""

    def _auto_task_done(self, seat: int, task: asyncio.Task) -> None:
        inflight = getattr(self, "butler_inflight", None)
        if inflight is not None:
            inflight.discard(seat)
        if self.auto_butler_tasks.get(seat) is task:
            self.auto_butler_tasks.pop(seat, None)
        if task.cancelled():
            return
        try:
            task.exception()          # 取走异常，别让 asyncio 打 "never retrieved" 日志
        except Exception:             # noqa: BLE001
            pass

    def kick_auto_butler(self, seat: int) -> None:
        """全程托管：这个座位现在有 ask 就起一个后台任务。

        由 `ask()` 在把面板发给玩家之后调用；也可以由开关打开时补一刀。
        同一座位同一时间只跑一个自动托管任务，避免手动/自动重复花钱。
        """
        seat = int(seat or 0)
        if not seat or seat not in self.auto_butler_seats:
            return
        if seat not in self.pending_asks:
            return
        inflight = getattr(self, "butler_inflight", None)
        if inflight is None:
            inflight = self.butler_inflight = set()
        if seat in inflight:
            return
        old = self.auto_butler_tasks.get(seat)
        if old is not None and not old.done():
            return
        ws = next((w for w in list(self.clients.keys())
                   if self.viewer_seat(w) == seat), None)
        try:
            task = asyncio.create_task(self.autopilot(ws, seat, auto=True))
        except RuntimeError:              # 没有运行中的事件循环（测试/退出）；不自动
            return
        inflight.add(seat)
        self.auto_butler_tasks[seat] = task
        task.add_done_callback(lambda t, s=seat: self._auto_task_done(s, t))

    def set_auto_butler(self, seat: int, on: bool) -> None:
        """开/关这个座位的全程托管；关闭时取消正在跑的那一手。"""
        seat = int(seat or 0)
        if not seat:
            return
        if on:
            self.auto_butler_seats.add(seat)
            self.kick_auto_butler(seat)
        else:
            self.auto_butler_seats.discard(seat)
            task = self.auto_butler_tasks.get(seat)
            if task is not None and not task.done():
                task.cancel()

    def clear_auto_butler(self) -> None:
        """新局/收尾：清掉自动托管开关和后台任务，别让下一局意外替你打。"""
        for task in list(self.auto_butler_tasks.values()):
            if not task.done():
                task.cancel()
        self.auto_butler_tasks.clear()
        self.auto_butler_seats.clear()

    async def autopilot(self, ws, seat: int, auto: bool = False) -> None:
        """调一次托管模型，替玩家出这一手。

        2026-11-03 审查修复：
          · P1-5：落地前核对 `ask_id` —— 慢模型期间旧问题可能已经被放掉、同座位来了新问题，
            旧答案绝不能解掉新 future；
          · P1-6：本函数由 WS 处理器用 `create_task` 起后台任务，**不再阻塞该连接的读循环**
            （以前托管 180 秒内玩家手答/ping 全部堆着）；
          · P2：client 用完即关（以前没进 clients_llm，永远不释放）。

        2026-11-06：`auto=True` 是「全程自动托管」触发的。每个 await 回来都要再看一眼
        开关还在不在——玩家随时可以关掉，不能因为模型已经跑了 50 秒就硬塞答案。
        """
        from wolf.context import Task                       # noqa: PLC0415
        seat = int(seat)
        pending = self.pending_asks.get(seat)
        if not pending:
            self.send_to(ws, {"type": "notice", "text": "现在没有等你的行动，托管用不上。"})
            return
        if auto and seat not in self.auto_butler_seats:
            self.send_to(ws, {"type": "notice", "text": "🤖 全程托管已经关了，这一手交回给你。"})
            return
        ask_id = pending.get("ask_id")
        self.send_to(ws, {"type": "notice", "text": "🤖 托管模型正在替你想这一手…"})
        kind = pending.get("kind") or ""
        task = Task(kind=kind, text=pending.get("text") or "",
                    schema=pending.get("schema") or "",
                    legal=list(pending.get("legal") or []),
                    extra=dict(pending.get("extra") or {}),
                    required=tuple(pending.get("required") or ()))
        # 2026-11-03（作者要求）：托管发言**复用真实模型座位那一套上下文** ——
        # 用 context.build() 重新拼 packet（规则书/打法书/概念库/台账齐全），
        # 任务说明也按 speech_plan 口径，而不是人类面板那句短提示。
        from wolf import butler as butler_mod                     # noqa: PLC0415
        seat_obj = self.game.seat_by_id(seat) if self.game else None
        if (kind == "speech" and not (getattr(task, "extra", None) or {}).get("pk")
                and seat_obj is not None and self.game is not None):
            task, packet = butler_mod.build_speech_call(self.game, seat_obj,
                                                        int(self.game.day or 1))
            kind = task.kind
        else:
            packet = butler_mod.augment_packet(pending.get("packet") or "", kind)

        # 2026-11-06 真机复盘 P1：以前只试托管配置的一条 API，失败就把 `_error`
        # 当成玩家的答案交给引擎（D2 6 号因此变成一张无效票）。
        # 现在：主档案 → `game.fallback` 里最多两条替补；全失败就**不提交**，
        # 保留待答让真人自己答，并把失败写进 faults。
        cfg_b = settings_store.load(HERE).get("butler") or {}
        primary = str(cfg_b.get("api") or "").strip().upper()
        if not primary:
            self.send_to(ws, {"type": "notice",
                              "text": "🤖 还没配托管模型：设置 → 模型 API → 最下面「托管模型」选一条 API"})
            return
        names = [primary]
        for raw in ((self.cfg.get("game") or {}).get("fallback") or []):
            name = str(raw or "").strip().upper()
            if name and name not in names:
                names.append(name)
        names = names[:3]

        action = None
        used_name = ""
        last_err = ""
        agent = None
        for idx, name in enumerate(names):
            client, err = self.butler_client(seat, name)
            if client is None:
                last_err = err
                continue
            agent = LLMAgent(client, seat)
            try:
                action = await asyncio.wait_for(
                    agent.ask(packet, task), timeout=150 if idx == 0 else 100)
            except Exception as exc:                      # noqa: BLE001
                action = None
                last_err = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    await client.aclose()
                except Exception:                          # noqa: BLE001
                    pass
            if action and "_error" not in action:
                used_name = name
                break
            if action and action.get("_error"):
                last_err = str(action.get("_error") or "空错误")
            action = None
            if idx + 1 < len(names):
                self.send_to(ws, {"type": "notice",
                                  "text": f"🤖 托管「{name}」没成功，换替补……"})
        if not action or not used_name:
            try:
                self.game.faults.append({
                    "day": int(getattr(self.game, "day", 0) or 0),
                    "phase": str(getattr(self.game, "phase", "day") or "day"),
                    "seat": seat,
                    "task": kind,
                    "reason": f"托管失败（已试 {len(names)} 个档案）：{last_err[:300]}",
                })
            except Exception:                              # noqa: BLE001
                pass
            self.send_to(ws, {"type": "notice",
                              "text": f"🤖 托管失败（已试 {len(names)} 个模型）：{last_err[:160]}。"
                                      f"这一手没提交，你可以自己答，或再点一次托管。"})
            return
        if used_name != primary:
            self.send_to(ws, {"type": "notice",
                              "text": f"🤖 主托管模型没成功，已用替补「{used_name}」答完这一手。"})
        if auto and seat not in self.auto_butler_seats:
            self.send_to(ws, {"type": "notice",
                              "text": "🤖 全程托管已经关了，这一手不提交；你可以自己答。"})
            return
        current = self.pending_asks.get(seat)
        if not current or current.get("ask_id") != ask_id:
            self.send_to(ws, {"type": "notice",
                              "text": "🤖 你想的这一手已经过期（问题被重发/超时了），这次不提交。"})
            return
        future = self.asks.get(seat)
        if future is None or future.done():
            self.send_to(ws, {"type": "notice", "text": "🤖 这一手已经答过了。"})
            return
        # 2026-11-03：把这次托管调用的用量交给"人类代理"，让引擎记进 transcript
        # （以前这手在存档里是 model='' / tokens=0，看不出托管花了多少）。
        try:
            meta = agent.last_meta() or {}
            meta = dict(meta)
            meta["butler"] = True
            if used_name and used_name != primary:
                meta["fallback"] = used_name
            meta["packet"] = packet          # 真正发给模型的那份（复盘要看这个，不是人类面板提示）
            seat_obj = self.game.seat_by_id(seat) if self.game else None
            human_agent = getattr(seat_obj, "agent", None) if seat_obj else None
            if human_agent is not None:
                human_agent._last_meta = meta
        except Exception:                                  # noqa: BLE001
            pass
        self.pending_asks.pop(seat, None)
        if self.pending_ask is current:
            self.pending_ask = next(iter(self.pending_asks.values()), None)
        future.set_result(dict(action or {}))
        done = {"type": "ask_done", "seat": seat, "ask_id": ask_id}
        for other in list(self.clients.keys()):
            if self.viewer_seat(other) == seat:
                self.send_to(other, done)
        self.send_to(ws, {"type": "notice", "text": "🤖 托管已替你出完这一手（不满意可以下一手自己来）。"})

    def drop_ask(self, seat_id: int, future=None) -> None:
        """一手答完/超时之后清理（只清自己那一手，别误删同座位更新的一手）。"""
        seat = int(seat_id or 0)
        cur = self.asks.get(seat)
        if future is not None and cur is not future:
            return
        self.asks.pop(seat, None)
        self.awaiting = next(iter(self.asks.values()), None)

    def broadcast(self, payload: dict) -> None:
        for ws, queue in list(self.clients.items()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._mark_dropped(ws)         # 丢了的消息要记账，下次 sync 告诉它

    def notify_seat(self, seat_id: int, payload: dict) -> int:
        """只发给**坐在这个座位上的**连接，返回发给了几条。

        ⚠️ 2026-10-31 外部审查 P1-04：夜里"某个真人超时没行动"这类通知不能全房广播 ——
        夜里只有狼/预言家/女巫/摄梦人/守卫在动，别人收到就等于知道这个真人属于哪一类身份。
        同一个人开两台设备时两边都会收到（与 `ask()` 的口径一致）。
        """
        seat = int(seat_id or 0)
        sent = 0
        if not seat:
            return 0
        for ws in list(self.clients.keys()):
            if self.viewer_seat(ws) != seat:
                continue
            self.send_to(ws, payload)
            sent += 1
        return sent

    # ---------- 局面快照 ----------

    async def _close_llm_clients(self) -> None:
        """关掉本房间已建的 httpx 客户端。

        ⚠️ 2026-10-30：开局失败（配置缺 key / 预建代理报错）时以前只把 `self.game`
        置 None，客户端留着；下一次 `start()` 开头又直接 `self.clients_llm = []`，
        引用一丢就再也关不掉了 —— 反复失败重试会漏连接。
        """
        clients = list(getattr(self, "clients_llm", []) or [])
        self.clients_llm = []
        for client in clients:
            try:
                await asyncio.wait_for(client.aclose(), 3)
            except Exception:                    # noqa: BLE001
                pass

    def rules_setting(self) -> str:
        """config 的 `game.rules_mode`：规则书放哪儿 —— room（开局公示栏，默认）/ packet（座位提示词）
        / both / off。两种放置方式的实测对比见 `tools/rules_ab.py`。"""
        raw = str(self.game_cfg.get("rules_mode", "room")).strip().lower()
        return raw if raw in ("room", "packet", "both", "off") else "room"

    def sheriff_setting(self) -> bool | None:
        """config 的 `game.sheriff`：`auto`（默认，11 人及以上才开）/ true / false。

        解析逻辑在 `wolf.engine.parse_sheriff_setting()` —— 和 run_game.py 共用一份，
        免得两处对"带引号的 false"这种写法处理不一致（`bool("false")` 是 True）。
        """
        return parse_sheriff_setting(self.game_cfg.get("sheriff", "auto"))

    def board_has_sheriff(self, seats: int) -> bool:
        """这个座位数的板子会不会有警长竞选（给开局面板的提示用）。"""
        setting = self.sheriff_setting()
        if setting is None:
            return seats >= int(self.game_cfg.get("sheriff_min_seats", 11))
        return setting

    def status(self) -> dict:
        """HTTP / 旧客户端的快照：默认按主机自己的座位（单人局就是老行为）。"""
        return self.status_for(self.host_seat() or self.human_seat, host=True)

    def status_for(self, seat: int | None = None, host: bool = False) -> dict:
        """按**看的人**生成状态。联机局每个玩家只能看到自己那一份私有信息。

        ⚠️ `human_seat` 保留为「这个连接坐几号」的老字段（前端 `curSeat()` 还在用），
           联机局里它就是 viewer 自己的座位；观众是 0。
        """
        game = self.game
        seat = int(seat or 0)
        seat_rows = {}
        for pid, p in self.players.items():
            if p.get("seat"):
                seat_rows[int(p["seat"])] = p["name"]
        base = {
            "demo": bool(self.demo_mode),
            "type": "status",
            # ⚠️ 收尾期间对外报 running=False + cleaning=True：
            # 以前收尾时仍然报 running=True，手机底部显示"对局进行中，等别人发言…"，
            # 用户以为还能操作（其实局已经结束了，开新局也会被拒）。
            "running": bool(self.running) and not self.cleaning,
            "cleaning": bool(self.cleaning),
            "mock": self.mock,
            "human_seat": seat,
            "your_seat": seat,
            "multiplayer": self.multiplayer,
            "players": self.room_players_public(),
            "human_timeout": self.human_timeout,
            #: 这个连接坐的座位是否开了「全程自动托管」；换页/重连后按钮状态靠它。
            "auto_butler": seat in self.auto_butler_seats,
            # 手机上"X号 正在思考…"的提示要用到：让它知道最多还会等多久（见 web/index.html）
            # ⚠️ 这两个数与真正建局时传给 `Game(...)` 的是**同一对**（Room.__init__ 里算好，
            #    见 wolf/engine.py::timeouts_from_cfg）—— 别再在这里各写一份兜底默认值。
            "call_timeout": self.call_timeout,
            "decision_timeout": self.decision_timeout,
            "has_key": self.has_any_key(),
        "seats_ready": self._seats_ready(),
        "faults": self._faults_summary(host),
        "director": self.director_enabled(),
            "key_env": str((self.llm_cfg.get("default") or {}).get("api_key_env") or "QWEN_API_KEY"),
            "board": getattr(self, "board_name", self.game_cfg.get("board") or "eleven_wolfking"),
            #: 给前端拼「请用 https 打开」的提示条 —— http 页面里浏览器**不给麦克风 API**、
            #: 也**不允许安装 PWA**（这就是"语音用不了 / 装不上"的根因）。
            #: 值来自 config 的 `game.public_https_base`（Tailscale serve 的 https 地址），
            #: 也可以写在 `.env` 的 `WOLF_PUBLIC_HTTPS_BASE` 里 —— 后者不进仓库，
            #: 所以开源仓库/发布包里的 `config.yaml` 可以保持为空（2026-10-31 打包清洗）。
            "public_https_base": str(self.game_cfg.get("public_https_base")
                                     or os.environ.get("WOLF_PUBLIC_HTTPS_BASE") or ""),
            # ⚠️ `board` 是**上一局房间**用的板子（没跑过房间时才是配置默认值）。
            #    手机端挑板子时应该用下面这个"配置里的默认板子"，否则跑过一次别的板子之后，
            #    默认板子就被那一局带偏了（实测踩到：跑过一次 eleven_standard 的测试局之后，
            #    手机开局面板默认变成了十一人标准板）。
            "default_board": self.game_cfg.get("board") or "eleven_wolfking",
            "boards": self.board_options(),
            # 上游失败导致暂停时：手机刷新页面也能靠它把弹窗补回来
            "stall": self.stall,
            #: 最近一次收尾存档（含 mock 标记）；底部「看上一局的复盘留痕」用它，
            #: 否则 mock 局落在 `_mock/`、不在 `/api/games` 列表里，按钮会拿不到名字。
            "last_save": self.last_save,
            "last_save_mock": bool(self.last_save_mock),
        }
        if host:
            base["join_code"] = self.join_code
        if not game:
            return base
        if seat > 0 and seat <= len(game.seats) and game.seat_by_id(seat).is_human:
            me_now = game.seat_by_id(seat)
            mates = [s.id for s in game.seats
                     if s.camp == "wolf" and s.id != me_now.id] if me_now.camp == "wolf" else []
            base.update({
                "your_role": role_name(me_now.role_key),
                "your_camp": camp_label(me_now.camp),
                "your_mates": mates,
            })
        base.update({
            "day": game.day,
            "phase": game.phase,
            "alive": game.alive_ids(),
            # 「出局亮身份」的板子（6 人屠城明牌）要把他亮出的身份牌一起给手机 ——
            # 引擎已经把它写进公开台账了，这里只是让顶栏座位条也能直接标出来。
            "dead": [{"seat": s.id, "day": s.death_day,
                      **({"role": role_name(s.role_key)}
                         if getattr(game, "reveal_on_death", False) else {})}
                     for s in game.seats if not s.alive],
            "winner": game.winner,
            "seats": [{"id": s.id, "alive": s.alive, "is_human": s.is_human,
                       # 角色卡上的姓名（表达层，公开的东西）：手机上让"3号 陈默"一眼可读，
                       # 人类玩家也能跟上 AI 之间的称呼。没写卡的座位就是空字符串。
                       "name": ((self.personas.personas.get(s.id).name
                                 if self.personas.personas.get(s.id) else "") or ""),
                       "owner": self.seat_owner.get(s.id, ""),
                       "owner_name": seat_rows.get(s.id, "")}
                      for s in game.seats],
            # 警徽在谁手上（公开信息）：手机顶栏用它显示"警长 X号"
            "sheriff": getattr(game, "sheriff_id", None),
            "sheriff_enabled": bool(getattr(game, "sheriff_enabled", False)),
        })
        return base

    def broadcast_status(self) -> None:
        """按每个连接自己的座位发状态（联机局不能用一份全局快照）。"""
        if not self.clients:
            self.broadcast(self.status())      # 没有连接时保持旧行为（历史测试用桩接）
            return
        for ws in list(self.clients.keys()):
            info = self.viewer_info(ws)
            self.send_to(ws, self.status_for(self.viewer_seat(ws),
                                             host=(info.get("role") == "host")))

    async def on_stall(self, seat, task, error: str, stalled: int = 1) -> str:
        """上游连续失败（客户端已自动重试 max_retries 次）→ **暂停整局**，问用户怎么办。

        返回 "retry"（再发一次）/ "skip"（按规则随机处理，继续打）/ "abort"（放弃本局）。
        用户的原话："如果四次重试还失败，暂停游戏弹窗提示我看看。"

        实现要点：
        · 暂停期间游戏 task 仍然活着（它 await 在这个 future 上），所以不会被"看门狗"当死局重置；
        · 手机上收到 `{"type": "stall", ...}` 弹窗，按哪个按钮就回 `{"type": "stall_decision"}`；
        · 重连时补发 stall，免得用户切走再回来就看不到这个弹窗了；
        · 谁都不理（`stall_timeout`，默认 30 分钟）→ 按"跳过"继续，别让对局永远挂着。

        ⚠️ **并发卡住要共用同一个决定**：引擎在投票/狼刀那几处是 `asyncio.gather` 并发问的，
        中转站一抖就是一整排座位同时失败（实测一夜里 10 次 on_stall，其中 4 次狼刀是同时进来的）。
        以前每次调用都新建一个 future 覆盖上一个 → 用户点一次按钮只有最后一个座位收到，
        其余座位要干等满 stall_timeout（默认 30 分钟）；而 finally 又无条件把字段清空，
        点击落在清空之后还会 `set_result` 到 None 上抛 AttributeError（被 WS 兜底 except 吞掉、
        表现成"手机断线、没有任何提示"）。
        现在改成：**同一批卡住共享一个 future + 计数**，一次点击释放所有等待者；
        最后一个等待者退出时才清空，`handle()` 那边也对 None 做了保护。
        """
        night = self._is_night()
        # ⚠️ 夜里的座位号同样不能给（跟 `_thinking_msg` / 上面 `retry` 一个口径）：
        #    夜里会卡住的只可能是狼 / 预言家 / 女巫 / 守卫，「N号 的模型连续失败」
        #    等于点名「这个人有夜间身份」。任务名早就中性化了（NEUTRAL_TASK），
        #    座位号是这条弹窗上最后一个漏点 —— 弹窗/提示里一起抹掉。
        # ⚠️ 2026-10-30 修：**白天也有一类任务必须抹** —— 狼专属任务（自爆/夜聊/定刀）。
        #    白天问狼「要不要自爆」卡住时点名，等于告诉真人这号是狼。判据统一用
        #    `SECRET_ASK_KINDS`（与 `_thinking_msg` 同一张表），不要再各自列一遍。
        secret = night or str(task.kind) in self.SECRET_ASK_KINDS
        payload = {"type": "stall", "seat": None if secret else seat.id, "night": night,
                   "task": NEUTRAL_TASK.get(task.kind, "行动"),
                   "error": error, "stalled": stalled,
                   "model": str(getattr(getattr(seat, "agent", None), "client", None)
                                and getattr(seat.agent.client, "label", "") or ""),
                   "timeout": self.stall_timeout}
        loop = asyncio.get_running_loop()
        first = self.stall_decision is None
        if first:
            self.stall_decision = loop.create_future()
            self.stall_waiters = 0
        self.stall_waiters += 1
        mine = self.stall_decision
        self.stall = payload
        # 只有主机能拍板；访客看到公开的 notice 就够了（弹窗带按钮，不该发给所有人）
        clients = getattr(self, "clients", None) or {}
        if clients:
            for ws in list(clients.keys()):
                if self.viewer_info(ws).get("role") == "host":
                    self.send_to(ws, payload)
        else:
            self.broadcast(payload)       # 测试里的半成品 Room 没有 clients，走旧桩
        if first:
            lead = "有人" if secret else f"{seat.id}号"
            self.broadcast({"type": "notice",
                            "text": f"⏸ {lead}的模型连续失败，对局已暂停等你决定。"})
        else:
            also = "又有人" if secret else f"又有 {seat.id}号"
            self.broadcast({"type": "notice",
                            "text": f"⏸ {also} 卡在同一个地方"
                                    f"（{NEUTRAL_TASK.get(task.kind, '行动')}），"
                                    f"一共 {self.stall_waiters} 个座位在等你的决定。"})
        try:
            # shield：某个等待者超时不该把共享的 future 取消掉（别人还在等）
            decision = await asyncio.wait_for(asyncio.shield(mine), timeout=self.stall_timeout)
        except asyncio.TimeoutError:
            decision = "skip"
            self.broadcast({"type": "notice",
                            "text": f"等了 {int(self.stall_timeout // 60)} 分钟没人回应，"
                                    f"这一手按「跳过（随机处理）」继续。"})
        except asyncio.CancelledError:
            self.stall = None
            self.stall_decision = None
            self.stall_waiters = 0
            self.broadcast({"type": "stall_cleared", "action": "abort"})
            raise
        finally:
            self.stall_waiters = max(0, getattr(self, "stall_waiters", 1) - 1)
            if self.stall_waiters == 0 and self.stall_decision is mine:
                self.stall = None
                self.stall_decision = None
        self.broadcast({"type": "stall_cleared", "action": decision})
        self.broadcast({"type": "notice",
                        "text": {"retry": "▶ 再试一次…", "skip": "▶ 跳过这一手，按规则随机处理，继续对局。",
                                 "abort": "▶ 放弃本局。"}.get(decision, "▶ 继续对局。")})
        return decision

    def board_options(self) -> list[dict]:
        out: list[dict] = []

        def tag_of(roles: list) -> str:
            """同名人数（比如两个 11 人板子）在面板上要能区分开：给它一个短标签。

            ⚠️ 2026-10-09 修：以前只认 idiot / guard，而且名字写死"白痴"（角色已改名愚者）——
            默认板子换到 eleven_dream（愚者 + 摄梦人 + 三张民牌）之后这个标签就失灵了。
            现在按"这张板子最独特的那个角色"取标签，名字一律从 `ROLES` 取。
            """
            from wolf.roles import ROLES as _R
            # ⚠️ 顺序 = 优先用"这张板子最独特的那个角色"当标签。
            #    `wolf_king` 必须排在最前：狼王版与预女梦愚版**角色几乎一样**（都带摄梦人），
            #    不把它放前面的话两个 11 人板子会显示成同一个「11人·摄梦人」。
            # 2026-10-30：现在四张板子人数各不相同（6/9/11/12），标签只用来在"同名人数"时
            # 区分；12 人 → 愚者、9 人 → 猎人、6 人 → 女巫（都取最独特的那张牌）。
            for key in ("wolf_king", "dreamer", "guard", "idiot", "lucky", "crow",
                        "gravekeeper", "hunter", "witch", "seer"):
                if key in roles:
                    return _R[key].name if key in _R else key
            return ""

        def board_row(key: str, board, custom: bool) -> dict:
            """一张板子给前端挑卡片用的完整公开信息。

            2026-10-31 加：首页的板子卡要显示「几狼、几张神、几张民、什么胜负条件」，
            以前只有一句 label 长文本，前端只能整段糊上去。这里把结构一起给了，
            前端不必再解析中文摘要（解析一旦改字就碎）。
            """
            role_rows = []
            for rk in board.roles:
                role = ROLES.get(rk)
                role_rows.append({"key": rk,
                                  "name": role.name if role else rk,
                                  "camp": role.camp if role else "",
                                  "kind": role.kind if role else ""})
            return {
                "name": key, "seats": len(board.roles), "tag": tag_of(list(board.roles)),
                "label": board_summary(board)
                         + ("　｜有警长竞选" if self.board_has_sheriff(len(board.roles)) else ""),
                "desc": board.desc, "custom": custom,
                "roles": role_rows,
                "win_rule": board.win_rule,
                "first_night_quiet": bool(getattr(board, "first_night_quiet", False)),
                "reveal_on_death": bool(getattr(board, "reveal_on_death", False)),
            }

        for key, item in BUILTIN_BOARDS.items():
            out.append(board_row(key, item, False))
        for key in (self.game_cfg.get("boards") or {}):
            try:
                board = make_board(key, self.game_cfg.get("boards"))
            except Exception:                       # noqa: BLE001
                continue
            out.append(board_row(key, board, True))
        return out

    def _faults_summary(self, host: bool) -> dict:
        """重连补状态用：fault 是 live-only 事件，刷新后前端要能从 status 恢复角标。"""
        rows = list(getattr(self.game, "faults", []) or []) if self.game else []
        seats: dict[str, int] = {}
        for row in rows:
            key = str(row.get("seat") or 0)
            seats[key] = seats.get(key, 0) + 1
        return {"total": len(rows), "seats": seats if host else {}}

    def director_enabled(self) -> bool:
        """导演模式（作弊）：设置里开着、且对局中才允许查身份。默认关。"""
        try:
            return bool(settings_store.load(HERE).get("director"))
        except Exception:                       # noqa: BLE001 —— 设置坏了就当没开
            return False

    def _seats_ready(self) -> list[dict]:
        """开局预检（借 dsh-game 的"逐项打勾"）：每个座位用的档案有没有 key。

        只做提示，不拦开局：页面上的「座位分配」可能走 settings.local.json / WOLF_SEAT_PROFILES，
        这里读的是环境变量这一层；前端用 `ready=false` 标出来，让玩家自己决定。
        """
        out: list[dict] = []
        seats_cfg = self.llm_cfg.get("seats")
        if not isinstance(seats_cfg, dict):
            seats_cfg = {}
        default = str((self.llm_cfg.get("default") or {}).get("profile") or "")
        limit = int(getattr(settings_store, "MAX_SEATS", 18) or 18)
        for seat in range(1, limit + 1):
            block = seats_cfg.get(seat)
            if block is None:
                block = seats_cfg.get(str(seat))
            if not isinstance(block, dict):
                block = {}
            # 优先级对齐 routing.resolve：`.env 座位分配 > 座位块 > 角色 > default`
            prof = str(routing.seat_profile_overlay(seat) or block.get("profile") or default)
            ready = bool(prof and (os.environ.get(f"{prof.upper()}_API_KEY") or _block_has_key(block)))
            out.append({"seat": seat, "profile": prof, "ready": ready})
        return out

    def has_any_key(self) -> bool:
        """给界面用的小提示：配置里至少有一条 key 能读到。"""
        llm = self.llm_cfg
        if _block_has_key(llm.get("default")):
            return True
        for group in ("roles", "seats"):
            for block in (llm.get(group) or {}).values():
                if _block_has_key(block):
                    return True
        #: 页面上的「座位分配」走 `WOLF_SEAT_PROFILES`，它不在 config 的任何 block 里。
        #: 不走这一步的话：用户只填了一个 API、把 1~12 号全指过去，顶部却仍然报
        #: 「没读到 API Key」—— 看起来像设置没保存。
        for seat in range(1, 13):
            profile = routing.seat_profile_overlay(seat)
            if profile and os.environ.get(f"{profile}_API_KEY"):
                return True
        return False

    def routing_table(self) -> list[dict]:
        game = self.game
        if not game:
            return []
        rows = []
        for seat in game.seats:
            if seat.is_human:
                rows.append({"seat": seat.id, "role": role_name(seat.role_key), "human": True})
                continue
            spec = routing.resolve(self.cfg, seat)
            rows.append({"seat": seat.id, "role": role_name(seat.role_key), "human": False,
                         "host": spec.host, "model": spec.model, "ready": spec.ready})
        return rows

    # ---------- 起局 ----------

    def factory(self, seat):
        if seat.is_human:
            # ServerHumanAgent = WebHumanAgent + "超时也补一条 ask_done"（见类注释）
            return ServerHumanAgent(seat.id, self)
        if self.mock:
            return MockAgent(seat, self.rng)
        if self.session_provider:
            # 公共体验实例：用**访客自己填的那条**渠道（只在本局进程内用，不落盘）
            _prov = self.session_provider
            _base = _prov.get("base_url") or ""
            _model = _prov.get("model") or ""
            _secret = _prov.get("api_key") or ""
            _temp = _prov.get("temperature")
            _tokens = _prov.get("max_tokens")
            _style = str(_prov.get("style") or "plain")
            spec = routing.SeatLLM(
                base_url=_base,
                api_key=_secret,
                model=_model,
                # 审查 P2：`or` 会把合法的 0 当缺省；这里只在"没填"时兜底
                temperature=(1.0 if _temp is None else float(_temp)),
                max_tokens=(20000 if _tokens is None else int(_tokens)),
                timeout=float(self.call_timeout),
                # plain 风格本来就不发任何思考参数，配档位是空转（审查 P2）—— 干脆留空
                thinking=({} if _style == "plain" else
                          {"speech": {1: "low"}, "decision": {1: "low"}, "aux": {1: "low"}}),
                thinking_style=_style,
            )
            client = routing.build_client(spec, label=f"{seat.id}号/{spec.model}")
            self.clients_llm.append(client)
            return LLMAgent(client, seat.id)
        spec = routing.resolve(self.cfg, seat)
        # ⚠️ 要带上"这家平台认哪种思考参数"（qwen/ark/glm/openai），否则档位表对 Qwen/豆包
        #    这类平台不生效 —— 详见 wolf/llm.py 的 THINKING_LEVELS_BY_STYLE 注释。
        client = routing.build_client(
            spec, label=f"{seat.id}号/{spec.model}",
            thinking_style=routing.thinking_style_for(self.cfg, routing._profile_of(self.cfg, seat)))
        self.clients_llm.append(client)
        return LLMAgent(client, seat.id,
                     fallbacks=_build_fallbacks(self.cfg, seat,
                                                self.game_cfg.get("fallback")))

    async def start_safe(self, mock: bool, human_seat, seed: int = 0,
                         board: str | None = None, human_seats=None,
                         multiplayer: bool = False) -> dict:
        """`start()` 的包装：把"参数/板子写错"变成一条**看得见的错误**，而不是静默断线。

        ⚠️ 2026-10-06 修（服务/前端审查的 P3-1）：以前 `int(message.get("human_seat"))`
        在参数不是数字时抛 ValueError、`start()` 里 `make_board` 只兜 KeyError
        （配置写成 `roles: 9` 会抛 TypeError）—— 异常一路逃到最外层，
        只把 WebSocket 关掉、**一条提示都不发**：用户看到的是"点了开始没反应"。
        """
        # ⚠️ 2026-11-02：`human_seat: 0` 是**全 AI**（config 里也是这个语义，
        #    run_game --human 0 同理）。以前写成 `int(human_seat or 1)`，0 是假值 ⇒
        #    被悄悄改成 1 号真人，于是"全 AI 跑一局"永远卡在等人。
        #    没传这个字段（老页面/脚本）仍然按 config 的 human_seat 走。
        if human_seat is None or human_seat == "":
            seat_no = int(self.human_seat)
        else:
            try:
                seat_no = int(human_seat)
            except (TypeError, ValueError):
                seat_no = int(self.human_seat)
        try:
            return await self.start(bool(mock), seat_no, int(seed or 0), board,
                                    human_seats=human_seats, multiplayer=multiplayer)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": f"开局失败：{type(exc).__name__}: {exc}"}

    async def start(self, mock: bool, human_seat: int, seed: int = 0,
                    board: str | None = None, human_seats=None,
                    multiplayer: bool = False) -> dict:
        if self.running:
            return {"ok": False, "error": "已经有一局在跑了"}
        # 新局不继承上一局的自动托管：避免开新局后没注意、AI 又替你打了一整局。
        self.clear_auto_butler()

        board_name = str(board or self.game_cfg.get("board") or "eleven_wolfking")
        try:
            board_def = make_board(board_name, self.game_cfg.get("boards"))
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        issues = validate_board(board_def)
        if issues:
            return {"ok": False, "error": "板子有问题：" + "；".join(issues)}

        # ---- 联机：真人座位集合 ----
        # 单人局走旧口径（human_seat 一个座位）；联机局用玩家实际认领的座位。
        if human_seats is None:
            # ⚠️ 2026-11-02：`human_seat: 0` = **全 AI**（与 config / run_game 同语义）。
            # 以前这里 `max(1, ...)` 会把 0 抬成 1 号真人，于是"全 AI 跑一局"永远卡在等人。
            if int(human_seat or 0) <= 0:
                seat_no = 0
                human_set = set()
            else:
                seat_no = max(1, min(len(board_def.roles), int(human_seat)))
                human_set = {seat_no}
        else:
            try:
                human_set = sorted({int(x) for x in human_seats if int(x or 0) > 0})
            except (TypeError, ValueError):
                return {"ok": False, "error": "座位号必须是数字"}
            bad = [x for x in human_set if x > len(board_def.roles)]
            if bad:
                return {"ok": False,
                        "error": f"有玩家选了 {bad[0]} 号，但这张板子只有 {len(board_def.roles)} 个座位"}
            seat_no = human_set[0] if human_set else 0
        self.multiplayer = bool(multiplayer)
        if self.multiplayer:
            # 以玩家表为准（UI 只发 board，不信任前端传的座位）
            claimed = sorted({int(p["seat"]) for p in self.players.values() if p.get("seat")})
            bad = [x for x in claimed if x > len(board_def.roles)]
            if bad:
                return {"ok": False,
                        "error": f"有玩家选了 {bad[0]} 号，但这张板子只有 {len(board_def.roles)} 个座位"}
            human_set = claimed
            seat_no = human_set[0] if human_set else 0
        else:
            # 单人局：主机就坐这一号（旧前端/测试的 human_seat 口径）
            host = self.player("host")
            if host is not None:
                host["seat"] = seat_no
        self.human_seats = set(human_set)
        # 联机局把"等你行动"的上限压到 3 分钟：一个人挂机不该让全场等 15 分钟。
        self.human_timeout = (min(self.human_timeout_solo, self.multiplayer_timeout)
                              if self.multiplayer else self.human_timeout_solo)
        self.seat_owner = {}
        for pid, p in self.players.items():
            if p.get("seat") in human_set:
                self.seat_owner[int(p["seat"])] = pid
        if self.multiplayer and not self.seat_owner:
            # 主机可以只当观众（全部 AI 打一局）——这不影响开局，但提示一句
            print("[room] 联机开局：没有真人座位（主机以观众身份旁观）")

        self.board_name = board_name
        self.lobby_board = board_name
        self.mock = mock
        # ⚠️ 2026-10-31 外部审查 X1：`human_seat` 的老语义是"**主机自己的**座位"，
        #    而联机局以前把它设成 `min(真人座位集合)` —— 主机坐 5、访客坐 2 时它变成 2，
        #    于是 `status()` / `broadcast_entry()` 这些仍把它当"主机座位"的地方就指到别人身上。
        #    现在：主机自己坐着就取他的号，没坐（观众）就是 0。
        _host = self.player("host") or {}
        self.human_seat = int(_host.get("seat") or 0) if self.multiplayer else seat_no
        self.rng = random.Random(seed or None)
        for _f in list(self.asks.values()):
            if not _f.done():
                _f.cancel()
        self.asks.clear()
        self.pending_asks.clear()
        self.clear_auto_butler()
        self.awaiting = None
        # ⚠️ 2026-10-31 外部审查 X3：这里以前把 `ask_seq` 归零（"新局从 1 重数"）。
        #    但客户端那边可能还压着上一局没送出去的 action（断线时点的确认），
        #    重连时如果新局正好也数到同一个号，那条旧答案就会被当成新问题的答案。
        #    现在**只增不减**：同一个进程里 ask_id 永不复用，跨局撞号从根上不可能。
        #    （`pending_asks` 仍然每局清空；旧客户端不带 ask_id 的兼容路径不受影响。）
        self.pending_ask = None
        self.stall = None                   # 新局绝不该带着上一局的"卡住了"弹窗
        self.stall_decision = None
        self.cleaning = False               # 新局绝不带着上一局的"收尾中"标记
        self.clients_llm = []
        # 语音（说话/听写）。没配就自动降级成"纯文字"，不影响开局
        self.voice = voice.VoiceConfig.from_env(self.cfg)

        # ⚠️ 建客户端是**边造座位边发生**的（factory 在 Game 构造里被调用）：中途任何一处
        #    抛错（某个座位配置写坏、路由解析失败）都会留下已经建好的 httpx 客户端没人关，
        #    而下一局开头 `self.clients_llm = []` 会把引用抹掉 —— 连接就漏在那儿了。
        #    这里兜一手：构造失败就把已建的都关掉，再把错误往上抛（start_safe 会变成人话）。
        try:
            game = Game(
                board=board_def,
                human_seat=self.human_seat,
                human_seats=self.human_seats,
                agent_factory=self.factory,
                # ⚠️ seed=0 必须变成 None（= 每局随机发牌）：
                #    config.yaml 写的是「seed: 0 = 每次随机发牌（每局身份都会重排）」，
                #    手机开局也**固定**发 seed=0；可引擎那层的语义是"显式给的 0 就是固定种子"
                #    （`seed if seed is not None else None`）—— 两层语义没对齐的后果是
                #    **从手机开的每一局发牌都一模一样**（实测三局全是 vvwwwwiswvh）。
                #    这里在边界上把 0 归一成 None，引擎那层保留"0 是合法固定种子"给测试用。
                seed=(int(seed) or None),
                max_days=int(self.game_cfg.get("max_days", 20)),
                # 往日发言压缩（2026-11-04）：off = 全文 / extract = 摘录；默认关，配置里改
                context_compress=str(self.game_cfg.get("context_compress") or "off"),
                on_event=self.on_event,
                pk_on_tie=bool(self.game_cfg.get("pk_on_tie", True)),
                wolf_control=bool(self.game_cfg.get("wolf_control", True)),
                sheriff=self.sheriff_setting(),      # auto（11 人及以上才开）/ true / false
                sheriff_min_seats=int(self.game_cfg.get("sheriff_min_seats", 11)),
                # 警长每天自选发言方向（顺序/逆序）—— 用户 2026-10-08 加；关掉退回老口径
                sheriff_picks_order=bool(self.game_cfg.get("sheriff_picks_order", True)),
                # 狼人自爆（变版规则）—— 用户 2026-10-08 加；关掉就没有这个窗口
                wolf_explode=bool(self.game_cfg.get("wolf_explode", True)),
                # 狼队夜间成本折中（作者 2026-10-09 批准）：
                # 四头狼刀口**完全一致**时跳过夜聊（省 len(wolves) 次调用）
                wolf_talk_skip_when_unanimous=bool(
                    self.game_cfg.get("wolf_talk_skip_when_unanimous", True)),
                # 预言家锁定上警（作者 2026-10-10）：不给他发上警调用
                seer_auto_run=bool(self.game_cfg.get("seer_auto_run", True)),
                on_stall=self.on_stall,              # 上游连续失败 → 暂停整局，弹窗问用户
                # 上游挂住不回包时的两层保险（见 engine.act）：
                #   单次上限：到点算这一手失败，进入重试/暂停链路
                #   整手预算：超过就不再重试，直接把决定权交给你
                # ⚠️ 与 status 报给手机的是同一对数（Room.__init__ 里算好的），别再写死数字
                call_timeout=self.call_timeout,
                decision_timeout=self.decision_timeout,
                # 所有座位共用的文本（见 wolf/tabletalk.py）：
                #   player_prompt = 通用玩家提示词（人人一样）
                #   playbook_text = 打法书（按角色分发）
                #   meta_text     = 概念库的进包段落（位置概率参考，按阵营分发）
                player_prompt=tabletalk.player_prompt(self.cfg, HERE),
                playbook_text=(tabletalk.load_playbook(
                    HERE, str(self.game_cfg.get("playbook_path") or ""))
                    if self.game_cfg.get("playbook", True) else ""),
                edge_order=str(self.game_cfg.get("edge_order", "sides_first")),
                # 自动重试（合计 auto_retries+1 次尝试，退避 3s→8s，受整手预算约束）
                auto_retries=int(self.game_cfg.get("auto_retries", 2)),
                retry_backoff=list(self.game_cfg.get("retry_backoff") or [3, 8]),
                retry_jitter=float(self.game_cfg.get("retry_jitter", 0.3)),
                # 换家后把替补的思路摘要留给原模型（下一手能看到它怎么想的）
                carry_reasoning=bool(self.game_cfg.get("carry_reasoning", True)),
                carry_reasoning_chars=int(self.game_cfg.get("carry_reasoning_chars", 300)),
                # 白天流程：决策在前、发言在后（见 wolf/engine.py 的 analyze_round / decide_for_speech）
                decision_first=bool(self.game_cfg.get("decision_first")),
                meta_text=(tabletalk.load_meta_book(HERE, str(self.game_cfg.get("meta_book_path") or ""))
                           if self.game_cfg.get("meta_book", True) else ""),
                rules_mode=self.rules_setting(),     # 规则书放哪儿：room / packet / both / off
                rules_detail=str(self.game_cfg.get("rules_detail", "brief")),
            )
        except Exception:
            await self._close_llm_clients()
            raise
        self.game = game
        #: 从这一刻起，只有这一局的事件会被广播（`emit` 里盖的 `_run` 必须等于它）。
        #: ⚠️ 必须在下面写规则公告**之前**设好 —— 那条公告会立刻走 `on_event("ledger")`。
        self.game_token = game.run_token
        #: 新的一局：清空上一局的存档名与自动存盘计数（否则新局会覆盖上一局的存档）
        self.save_stem = None
        self.autosave_count = 0

        # 规则书 · 公示栏版：开局把它当**公开公告**写进台账（所有座位每次调用都能看到），
        # 手机上另有一个「规则」按钮可以随时翻全文（GET /api/rules）。
        # 文本是按**本局板子**组装好的（只有本局真有的角色规则），见 wolf/rules.py。
        if self.rules_setting() in ("room", "both"):
            notice = rules.room_notice(game.rules_text)
            if notice:
                game.ledger.add(1, "day", "announce", notice)
                self.broadcast({"type": "notice",
                                "text": "本局规则已公示（手机右上角「规则」随时可看）"})

        # 座位人设（表达层）：每局重新加载，改了文件下一局就生效
        personas = persona.load(self.cfg, HERE)
        # 设置页的「人设模块」（2026-11-05）：开着才覆盖；关着完全不影响竞技配置
        local_personas, persona_warns = _persona_overrides()
        for sid, card in local_personas.items():
            personas.texts[sid] = persona.render(card)
            personas.personas[sid] = card
            personas.sources[sid] = "settings.local.json"
        personas.warnings.extend(persona_warns)
        self.personas = personas
        for seat in game.seats:
            if seat.id in personas:
                seat.persona = personas.get(seat.id)
                # 姓名要单独带上：角色卡里的关系按名字写，模型得知道"谁在几号"
                seat.card_name = personas.names().get(seat.id, "")
        for line in personas.warnings:
            self.broadcast({"type": "notice", "text": "人设提醒：" + line})
        loaded = sum(1 for s in game.seats if s.persona)
        if loaded:
            self.broadcast({"type": "notice", "text": f"已加载 {loaded} 份座位人设"})

        if not mock:
            issues = routing.problems(self.cfg, game.seats)
            if issues:
                await self._close_llm_clients()
                self.game = None
                self.game_token = None
                return {"ok": False, "error": "有座位拿不到模型配置：" + "；".join(issues[:4])}
            try:
                for seat in game.seats:            # 预建代理：配置有问题现在就报
                    game.agent_for(seat)
            except LLMError as exc:
                await self._close_llm_clients()
                self.game = None
                self.game_token = None
                return {"ok": False, "error": str(exc)}

        # 跨局经验（可选）：每局仍然是全新对话，只注入「去身份化」的复盘笔记
        if self.game_cfg.get("experience"):
            notes = experience.load_all(self.experience_dir)
            injected = 0
            for seat in game.seats:
                client = getattr(seat.agent, "client", None)
                if client is not None and notes.get(client.key):
                    seat.experience = notes[client.key]
                    injected += 1
            if injected:
                self.broadcast({"type": "notice",
                                "text": f"已给 {injected} 个座位注入过往复盘笔记（每局仍是全新对话）"})

        self.running = True
        # 联机：每个真人只收到自己的身份牌 + 自己的那份状态
        for ws in list(self.clients.keys()):
            seat = self.viewer_seat(ws)
            if seat:
                self.send_you(ws, seat)
        self.broadcast_status()
        self.broadcast_room()

        self.task = asyncio.create_task(self._run())
        return {"ok": True}

    async def _run(self) -> None:
        game = self.game
        clients = self.clients_llm          # 绑死本局的客户端，避免和下一局串
        abandoned = False
        try:
            await game.run()
        except asyncio.CancelledError:
            abandoned = True                # 被"放弃本局"掐掉了
            game.abort()
        except Exception as exc:            # noqa: BLE001
            self.broadcast({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            # ⚠️ 只在"我还是当前那一局"时才动房间状态：否则放弃后马上开的新局
            # 会被旧局的收尾抹成脏状态（游戏对象被清、引擎变孤儿）。
            if self.game is game:
                self.awaiting = None
                self.asks.clear()
                self.pending_asks.clear()
                self.pending_ask = None
                self.task = None
        try:
            await self._cleanup(game, clients, abandoned)
        finally:
            # ⚠️ running 必须等清理做完再放开：否则清理窗口里（写经验笔记可能要几秒）
            # 用户能开新局，而旧局的 end 事件会晚一步到达、把新局的行动面板清掉，
            # 新局就只能干等到 human_timeout。
            # ⚠️ 收尾完成后再放开 running，并且只在"我还是当前那一局"时才动房间状态。
            # 以前是无条件写：放弃后 0.4 秒内开新局的话，旧局的收尾会把新局的
            # game/task/running 全抹掉，而新局引擎变成孤儿继续跑 —— 手机永远收不到它的事件，
            # 它还会抢走全局的提问槽，把正在进行的局弄死（审计实测到这条完整链路）。
            if self.game is game:
                self.cleaning = False
                self.running = False
                self.broadcast_status()
                self.broadcast_room()

    def _save_game(self, game: Game) -> str | None:
        """把这一局写盘，并按**本房间自己的**留档策略清理旧档。返回存档路径（失败返回 None）。

        ⚠️ **存档目录只能用本房间自己的配置**（2026-10-08 修，代价是用户一局真存档）：
        以前这里是内联的、且调的是不带参数的 `resolve_save_dir()` —— 它会回退到**全局 ROOM
        的配置**，于是"传了临时 save_dir 的 Room"照样往生产目录写。实测后果：
        `check_deal_seed` / `t_deal_shuffle` / `t_inproc` 这类用临时目录建 Room 的测试，
        每跑一次回归就往用户的 `games/` 里塞 3+ 局 mock（实测 `--fast` 一轮 +8）；
        mock 越积越多，`prune_saves()`（只留 100 局、**从最旧的开始删**）就把真局挤掉删了。

        另：**独立成方法是为了能被直接测**（见 `tests/t_save_dir_isolated.py`）——
        整局 mock 要 40 秒以上，让回归"打完整局再检查存档落在哪"太慢。
        """
        return self._write_save(game, in_progress=False)

    def _write_save(self, game: Game, in_progress: bool) -> str | None:
        """真正落盘的那一段（`_save_game` = 终局存盘，`_autosave` = 途中存盘）。

        ⚠️ 两者**共用同一个存档名**（`self.save_stem`）：
        自动存盘每过一个阶段就**覆盖**同一个文件，收尾时再覆盖一次并清掉 `in_progress`。
        不这么做的话，一局会被写成十几个半截存档 —— 既把 `keep_games` 的保留窗口冲掉，
        复盘列表也会被同一局刷屏（`prune_saves` 还是「先删 mock、再从最旧的删」）。

        ⚠️ 2026-10-12 加：**mock 局（离线演练 / 页面测试）改落 `<save_dir>/_mock/`**。
        根因见 `_mock_save_dir`：真局与 mock 混在一个目录时，`prune_saves` 按时间从最旧删，
        最旧的真局正好是第一批被删的。`tests/t_mock_save_isolated.py` 守着这一条。
        """
        try:
            room_cfg = {"game": self.game_cfg}
            save_dir = resolve_save_dir(room_cfg)
            if self.mock:
                # ⚠️ mock 局（离线演练 / 页面测试）**一律落到 `<save_dir>/_mock/`**：
                #    为什么不许写根目录、为什么是子目录而不是临时目录，见 `_mock_save_dir` 的说明。
                save_dir = _mock_save_dir(save_dir)
            if not self.save_stem:
                # ⚠️ 2026-10-30：存档名只到秒。收尾后的终局存档与"紧接着开的新局"
                #    落在同一秒时会直接覆盖上一局（prune 也补不回来）—— 撞名就加序号。
                base_stem = f"game-{time.strftime('%Y%m%d-%H%M%S')}"
                stem_try, n = base_stem, 1
                while os.path.exists(os.path.join(save_dir, f"{stem_try}.json")):
                    n += 1
                    stem_try = f"{base_stem}-{n}"
                self.save_stem = stem_try

            def _enrich(data: dict, stem: str) -> None:
                """写盘前：给快照打上"是否还在打"的标记，并把发言语音硬链接进存档目录。"""
                data["in_progress"] = bool(in_progress)
                # ⚠️ 2026-10-12：把「这是 mock 局」写进存档本身。`prune_saves` 判 mock 主要靠
                #    `transcript` 里有没有非空 model（历史存档只能这么认），这一条是给将来
                #    留的显式信号 —— 两个信号取「或」，见 `_save_is_mock`。
                data["mock"] = bool(self.mock)
                if not (self.game_cfg.get("audio_in_save", True)):
                    return
                # 联机局可能有多个真人（甚至同时有狼和好人）：只要有一个真人坐狼，
                # 狼队夜聊就值得留语音；否则按第一个真人的阵营。全 AI 局传空。
                camps = []
                for sid in sorted(getattr(game, "human_seats", set()) or set()):
                    if 1 <= sid <= len(game.seats):
                        camps.append(game.seat_by_id(sid).camp)
                camp = "wolf" if "wolf" in camps else (camps[0] if camps else "")
                made = _archive_audio(save_dir, stem, data, camp)
                if made:
                    print(f"[save] {stem}：留档 {made} 条发言语音")

            path = game.save(save_dir, enrich=_enrich, stem=self.save_stem)
            self.last_save = os.path.basename(path)
            self.last_save_mock = bool(self.mock)
            if not in_progress:
                # 留档条数 + 磁盘预算：**先删 mock、再删真局**（各自最旧的先删），见 prune_saves
                rep = prune_saves(room_cfg)
                if rep["removed"] or rep["removed_mock"]:
                    print(f"[save] 清理旧存档 {len(rep['removed'])} 个"
                          f"（另有 mock 存档 {len(rep['removed_mock'])} 个），"
                          f"现存 {rep['kept']} 局 / {rep['mb']} MB")
            return path
        except Exception as exc:              # noqa: BLE001
            # ⚠️ 2026-10-25 修：以前这里是裸 `return None` —— 磁盘满 / 无权限 / 序列化失败时
            #    存档根本没写上去，而界面一切正常（用户以为存上了）。
            #    现在：日志一定打；收尾存盘额外给前端一条 notice（自动存盘每个阶段都写，
            #    失败时只在日志里说，避免每过一个阶段弹一次）。
            print(f"[save] 存档失败（{'途中' if in_progress else '收尾'}）："
                  f"{type(exc).__name__}: {exc}")
            if not in_progress:
                try:
                    self.broadcast({"type": "notice",
                                    "text": f"⚠️ 这一局没能写进存档（{type(exc).__name__}）："
                                            f"复盘里可能看不到它。请检查磁盘空间与目录权限。"})
                except Exception:            # noqa: BLE001
                    pass
            return None

    def _autosave(self) -> None:
        """**对局中途**存一次盘（用户 2026-10-08 的血泪教训）。

        为什么必须要有：以前存档只在"收尾"时写一次 —— 用户打了一个半小时的局，
        浏览器关掉/进程没了/机器重启，**整局一个字都不剩**（`games/` 里连文件都没有，
        因为那一局从来没被收尾过）。现在每过一个阶段（`phase` 事件）覆盖写一次同一份存档，
        最坏情况也只损失最后一个阶段。

        · 覆盖同一个 `save_stem`（见 `_write_save` 的说明），不会产生半截存档堆积；
        · 失败**绝不影响对局**（整段包在 try 里，只打一行日志）；
        · `game.autosave: false` 可关（关了就退回老行为）。
        """
        if not self.autosave or self.game is None:
            return
        try:
            path = self._write_save(self.game, in_progress=True)
            if path:
                self.autosave_count += 1
        except Exception as exc:              # noqa: BLE001
            print(f"[save] 自动存盘失败（不影响对局）：{type(exc).__name__}: {exc}")

    async def _cleanup(self, game: Game, clients: list, abandoned: bool) -> None:
        """收尾：存档、可选的经验笔记、关客户端、把结果推给手机。

        ⚠️ 进来的第一件事就是把自己标成"收尾中"，并广播一条 cleaning 状态。
        以前整段收尾期间对外仍然报 running=True，手机底部写"对局进行中，等别人发言…"，
        看起来像还能操作；实际局已经结束、开新局也会被服务端拒绝。
        """
        if self.game is game:
            self.cleaning = True
            self.clear_auto_butler()
            self.broadcast_status()    # running=False + cleaning=True → 前端显示"正在收尾"

        path = None
        try:
            path = self._save_game(game)
        except Exception as exc:            # noqa: BLE001
            # `_save_game` 内部已经兜住异常并打过日志；这里只是最后一道，别再静默。
            print(f"[save] 收尾存盘抛出了未预期的异常：{type(exc).__name__}: {exc}")

        notes: dict = {}
        if (self.game_cfg.get("experience") and not self.mock
                and game.winner in ("good", "wolf")):     # 放弃/判和的局不写经验
            self.broadcast({"type": "notice", "text": "正在让每个模型写这一局的复盘笔记…"})
            # ⚠️ 必须给**总时长**一个上限（服务/前端审查的 P2-3）：这一步是串行挨个调模型，
            #    单次上限 timeout×(重试+1)（默认 200×5 秒），满座就是几十分钟。
            #    这段时间 cleaning=True → start 被拒、看门狗和 heal 又都刻意跳过 cleaning →
            #    用户没有任何入口能提前结束，只能重启服务。这里封顶 90 秒，超了就带着已写好的走。
            try:
                notes = await asyncio.wait_for(
                    experience.update_all(game, self.experience_dir), timeout=90)
            except asyncio.TimeoutError:
                self.broadcast({"type": "notice",
                                "text": "复盘笔记写太久了（超过 90 秒），先跳过 —— 对局本身不受影响。"})
            except Exception as exc:        # noqa: BLE001
                self.broadcast({"type": "notice", "text": f"复盘笔记生成失败：{exc}"})

        if clients:
            # ⚠️ 关客户端必须带超时：aclose() 卡住（对端不回包/网络黑洞）会把整个收尾挂死，
            # 房间永远停在 cleaning，用户回不到主页面。
            # ⚠️ 而且要**并发**关、并且给**整体**一个上限：以前是"每个 5 秒、串行"，
            # 满座 9 个客户端全卡住时收尾实测 45.1 秒，而前端写的是"几秒"（2026-10-06 复核实测）。
            async def _close_one(client) -> None:
                try:
                    await asyncio.wait_for(client.aclose(), 5)
                except asyncio.TimeoutError:
                    print("[cleanup] 关客户端超时（单个 5 秒），先继续")
                except Exception:           # noqa: BLE001
                    pass

            try:
                await asyncio.wait_for(
                    asyncio.gather(*[_close_one(c) for c in clients]), 6)
            except asyncio.TimeoutError:
                print("[cleanup] 关客户端整体超时（6 秒），先继续")
        if self.clients_llm is clients:
            self.clients_llm = []

        if self.game is not game:
            return                          # 已经开了新局：别再往新局推旧局的结局
        self.session_provider = {}          # 公共体验：一局用完就清，不留在内存里
        self.broadcast({"type": "end", "winner": game.winner, "day": game.day,
                        "save": path,
                        "abandoned": abandoned or game.winner == "aborted",
                        "experience_updated": sorted(notes),
                        # 2026-11-03：结算页要的「座位 + 全部身份 + 胜负」；对局结束才公开
                        "settlement": settlement_payload(game)})
        # ⚠️ 这里以前会广播一条 running=True 的 status（实测报文顺序 end → running=True → running=False），
        # 中间那段手机底部又变回"对局进行中"。现在这仍旧是一条 cleaning 状态：
        # running=False、cleaning=True，前端显示"正在收尾"，直到收尾真正结束。
        self.broadcast_status()

    async def abandon(self) -> dict:
        """放弃当前对局：标停引擎 + 掐掉正在等的输入 + 取消后台任务。

        ⚠️ 实测发现：光靠 cancel 有时**掐不死**引擎（任务还活着、running 一直 True），
        结果手机永远卡在"对局进行中，等别人发言…"，只能重启服务。
        所以这里加一道**硬复位兜底**：给 3 秒体面收尾的时间，超时就强行清空状态
        —— 用户必须能回到主页面，这件事比"引擎是否优雅退出"重要。
        """
        if not self.running or self.game is None:
            return {"ok": False, "error": "现在没有正在进行的对局"}
        self.broadcast({"type": "notice", "text": "已放弃本局，正在收拾残局…"})
        self.game.abort()
        # 放弃之后不再收这一局的任何事件（旧任务可能还挂着，回来就丢）。
        # ⚠️ 放在 abort() **之后**：abort 写的那条"———— 玩家主动放弃了本局 ————"还要广播。
        self.game_token = None
        for _f in list(self.asks.values()):
            if not _f.done():
                _f.cancel()                 # 把所有"等你输入"的那几手立刻放掉
        self.asks.clear()
        self.pending_asks.clear()
        self.clear_auto_butler()
        self.awaiting = None
        if self.stall_decision is not None and not self.stall_decision.done():
            # 正卡在"上游失败"的弹窗上：直接放掉，别让它把引擎吊在那儿
            self.stall_decision.cancel()
        self.stall = None
        task = self.task
        if task is not None and not task.done():
            task.cancel()
        asyncio.get_running_loop().create_task(self._hard_reset_watchdog(task))
        return {"ok": True}

    async def _hard_reset_watchdog(self, task, grace: float = 3.0) -> None:
        """给收尾留 grace 秒；之后如果还说自己在对局中，就强行复位并通知手机。"""
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=grace)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await asyncio.sleep(0.3)
            if self.cleaning:
                # 收尾是**正常**的一步（存档、写笔记、关客户端），不是在卡死。
                # 这时候强行复位会把还没写完的东西和结局一起抹掉，只广播状态就好。
                self.broadcast_status()
                return
            if self.running or (self.task is not None and not self.task.done()):
                # 硬复位 = 不再承认这一局：先 abort（让在途回复作废），再清状态与代号。
                if self.game is not None:
                    self.game.abort("服务端硬复位（引擎没乖乖退出）")
                self.game_token = None
                self.running = False
                self.cleaning = False
                self.awaiting = None
                self.asks.clear()
                self.pending_asks.clear()
                self.pending_ask = None
                self.game = None
                self.task = None
                self.broadcast({"type": "notice", "text": "已强制复位（引擎没乖乖退出，状态已清空）"})
            self.broadcast_status()        # 让手机底部回到开局面板
        except Exception:                        # noqa: BLE001
            pass

    # ---------- 引擎事件 → 手机 ----------

    def _is_night(self) -> bool:
        """现在是不是夜里（看引擎的 phase）。

        ⚠️ 开局阶段（`init`）**也算夜里**：发牌之后第一个会动的必然是夜间身份
        （狼 / 预言家 / 女巫 / 守卫），而这时引擎还没把 phase 设成 "night"
        （`run()` 是在循环里才设的）。只看 `== "night"` 的话，「发牌那一瞬间」就成了
        脱敏的空窗 —— 任何带座位号的事件（谁在思考 / 谁失败了 / 谁在重试）都会被
        当成白天信息原样推给手机。所以按「引擎没把阶段推公开，就先藏着」处理。
        """
        # ⚠️ 用 getattr 兜底：`t_stall_concurrent.py` 这类测试会用 `Room.__new__()` 绕过
        #    `__init__` 造一个只有部分字段的 Room（历史上就这么写的），直接 `self.game`
        #    会 AttributeError —— 而这条函数是"夜间脱敏"的唯一判据，绝不能因为
        #    "对象没初始化完"就抛异常（那会把 on_stall 整条链路带崩）。
        game = getattr(self, "game", None)
        return bool(game is not None
                    and str(getattr(game, "phase", "") or "") in ("night", "init"))

    #: **这些任务的提问对象本身就是身份**：
    #:   · 狼队内部（自爆 / 定刀口 / 夜聊 / 出刀）—— 只有狼会被问；
    #:   · 白天最典型的是 `wolf_explode`：公示上警名单之后逐个问狼"要不要自爆"，
    #:     这时点名"X号 正在思考"就等于**公开这号是狼**（用户 2026-10-10 实测发现）。
    #:   白天的其它任务（发言、投票、上警）本来就是公开动作，不在此列。
    SECRET_ASK_KINDS = ("wolf_explode", "wolf_intent", "wolf_talk", "wolf_kill")

    def _thinking_msg(self, seat: int, kind: str = "") -> dict:
        """「谁在思考」的对外报文：白天给座位号，夜里/狼专属任务只给一个中性说法。

        ⚠️ 夜里连座位号都不能给（2026-10-07 用户发现的信息泄露）：夜里只有狼 /
        预言家 / 女巫行动，「几号在思考」等于直接点名谁有夜间身份。白天不用藏 ——
        发言顺序与投票顺序本来就是公开的。
        ⚠️ 但**白天的狼专属任务**（自爆）照样要点名 → 一并脱敏，标签写成「狼队正在思考」。
        """
        k = str(kind or "")
        if k in self.SECRET_ASK_KINDS:
            return {"type": "thinking", "seat": None, "night": True,
                    "label": "狼队正在思考"}
        if self._is_night():
            return {"type": "thinking", "seat": None, "night": True}
        # 2026-10-31：白天把「这个座位用的是哪个模型」一起发出去（右上角显示）。
        # 只给座位号与模型名，不给 key/host；夜里与狼专属任务仍然走上面两个脱敏分支。
        return {"type": "thinking", "seat": seat, "model": self._seat_model(seat)}

    def _seat_model(self, seat_id: int) -> str:
        """这个座位用的模型名（缓存；人类座位返回空串）。

        为什么缓存在 ROOM 上：一次对局里座位模型不会变，而 thinking 每次行动都会发一条。
        """
        cache = getattr(self, "_model_cache", None)
        if cache is None:
            cache = self._model_cache = {}
        if seat_id in cache:
            return cache[seat_id]
        name = ""
        game = getattr(self, "game", None)
        seat = next((s for s in game.seats if s.id == seat_id), None) if game else None
        if seat is not None and not getattr(seat, "is_human", False):
            try:
                name = str(routing.resolve(self.cfg, seat).model or "")
            except Exception:                          # noqa: BLE001
                name = ""
        cache[seat_id] = name
        return name

    def _secret_worthy(self, task_kind=None, night_hint: bool = False) -> bool:
        """夜里 / 狼专属任务的失败事件不点名（借 dsh-game 的 fault 脱敏）。

        三处（warn / retry / fault）共用一份判据 —— 以前各抄一份，fault 就是这么抄漂的。
        `night_hint` 用引擎 emit 时带的 phase，避免"服务端以为白天、引擎已在夜里"的边界漂移。
        """
        return (bool(night_hint)
                or self._is_night()
                or str(task_kind or "") in self.SECRET_ASK_KINDS)

    def on_event(self, event: str, **data) -> None:
        # 先看"这条事件属于哪一局"（引擎在 emit 里盖的 `_run`）。不是当前这一局 → 直接丢。
        # ⚠️ 不能省：硬复位之后旧任务可能还在跑，它发来的 packet/ledger 会按**新局**的座位
        #    与台账下标去广播（表现为"新局里突然冒出上一局的一句话"）。
        token = data.pop("_run", None)
        if token is not None and token != self.game_token:
            return
        game = self.game
        if game is None:
            return

        if event == "ledger":
            entry = data["entry"]
            # `n` 是这条在台账里的序号：前端拿它去 /api/tts 换语音。
            # 隔离在 broadcast_entry 里按每个连接自己的座位做（联机局不能一份广播给所有人）。
            self.broadcast_entry(entry, len(game.ledger.entries) - 1)
            if entry.kind == "announce":
                # ⚠️ 公告经常改变"谁还活着"（昨晚出局 / 放逐 / 警徽），而**前端只在 status 里
                #    读存活**（`S.status.seats[].alive`）—— 不补一条 status，顶部座位条就会
                #    一直停在旧状态。用户 2026-10-08 报的「1、2 出局后顶部没划掉」就是这个：
                #    夜里死亡只有一条 announce，下一次 status 要等到阶段切换，中间一直是旧的。
                #    一天只有几条公告，补这几条 status 的开销可以忽略。
                self.broadcast_status()
        elif event == "packet":
            # 只推"谁在思考"，**不要**带上 task（wolf_kill/seer_check/witch…）——
            # 那等于实时公开身份表，看一眼报文就知道谁是什么角色。
            # 不发给正在行动的那个人（他自己有面板）；联机局按座位判断。
            msg = self._thinking_msg(data["seat"], str(data.get("task") or ""))
            for ws in list(self.clients.keys()):
                if self.viewer_seat(ws) != int(data["seat"] or 0):
                    self.send_to(ws, msg)
        elif event == "phase":
            self.broadcast({"type": "phase", "day": data["day"], "phase": data["phase"]})
            self.broadcast_status()
            # 每个阶段交界处自动存一次盘（见 _autosave：崩溃/断网也只丢最后一个阶段）
            self._autosave()
        elif event == "death":
            # 有人**当场出局**（目前是狼人自爆）：立刻补一条 status，手机顶部才能马上把他划掉。
            # 跟 ledger 分支里那条"公告后补推 status"同一个道理（见那里的说明）。
            self.broadcast_status()
        elif event == "warn":
            # ⚠️ 2026-10-30：白天也有必须脱敏的任务（狼专属：自爆/夜聊/定刀）。
            #    判据统一走 `_secret_worthy`（见上方）。
            secret = self._secret_worthy(data.get("task_kind"))
            who = "有人" if secret else f"{data['seat']}号"
            self.broadcast({"type": "notice", "text": f"{who} 调用失败，已按规则随机处理。"})
        elif event == "fault":
            # 座位级"这一步没做成"（借 dsh-game 的 ⚠ 角标）：夜里/狼专属任务不点名，
            # 连 reason 也要脱敏 —— 缺字段名（poison/check_id/take…）本身就能反推角色。
            secret = self._secret_worthy(data.get("task_kind"), bool(data.get("night")))
            msg = {"type": "fault", "seat": None if secret else data.get("seat"),
                   "night": bool(secret),
                   "reason": ("（夜间/隐藏任务失败，细节已隐藏）" if secret else data.get("reason"))}
            self.broadcast(msg)
        elif event == "retry":
            # 引擎层的自动重试（可见退避，见 engine.act）：以前这里**没有分支**，            # `retry` 事件被静默丢掉 —— 前端那条「上游失败，N 秒后自动重试」的提示永远是死的，
            # 手机只能看着"X号 正在思考…"一动不动，分不清它在退避、还是真卡住了。
            msg = {"type": "retry", "seat": data.get("seat"),
                   "attempt": data.get("attempt"), "total": data.get("total"),
                   "wait": data.get("wait"), "why": data.get("why"),
                   "switched_to": data.get("switched_to"), "model": data.get("model")}
            night = self._secret_worthy(data.get("task_kind"))
            if night or str(data.get("task_kind") or "") in self.SECRET_ASK_KINDS:
                # ⚠️ 夜里的失败者就是那几个有夜间身份的人（狼 / 预言家 / 女巫 / 守卫）；
                #    白天则是狼专属任务（自爆）。跟 thinking / warn 同一条纪律 ——
                #    不点名，只说「有人在重试」；**why/model 也要脱敏**（可能带模型原文/模型名）。
                msg["seat"] = None
                msg["night"] = night
                msg["why"] = "隐藏任务失败，细节已隐藏"
                msg["model"] = None
            self.broadcast(msg)

    def on_human_timeout(self, seat_id: int, ask_id) -> None:
        """真人那一手超时了：补一条 `ask_done`，把手机上的面板收掉。

        由 `wolf/agents.py::WebHumanAgent.ask` 在**它自己的**超时分支里调用
        （2026-10-25 从"临时替换全局 `asyncio.wait_for`"改成这个显式钩子）。

        `ask_id` 是它清 `pending_ask` **之前**抓下来的那条编号 —— 前端据此只清对应那一个面板，
        不会误清玩家刚收到的新面板（老客户端不带 id，行为不变）。
        """
        print(f"[ws] 真人超时（弃权）：补发 ask_done #{ask_id}，把面板收掉（座位 {seat_id}）")
        for ws in list(self.clients.keys()):
            if self.viewer_seat(ws) == int(seat_id):
                self.send_to(ws, {"type": "ask_done", "seat": seat_id, "ask_id": ask_id})

    # ---------- 手机来的消息 ----------

    async def handle(self, ws: WebSocket, message: dict) -> None:
        kind = message.get("type")
        if kind == "director_peek":
            if not self.viewer_is_host(ws):
                self.send_to(ws, {"type": "error", "text": "只有主机能用导演模式"})
                return
            if not self.director_enabled():
                self.send_to(ws, {"type": "director_seat", "ok": False,
                                  "text": "导演模式没开：先去「设置 → 导演模式」打开"})
                return
            if not self.running or self.game is None:
                self.send_to(ws, {"type": "director_seat", "ok": False, "text": "现在没有进行中的对局"})
                return
            try:
                want = int(message.get("seat") or 0)
            except (TypeError, ValueError):
                want = 0
            seat = next((s for s in self.game.seats if s.id == want), None)
            if seat is None:
                self.send_to(ws, {"type": "director_seat", "ok": False, "text": f"没有 {want} 号"})
                return
            self.send_to(ws, {
                "type": "director_seat", "ok": True, "seat": seat.id,
                "role": role_name(seat.role_key), "camp": seat.camp,
                "alive": bool(seat.alive),
            })
            return

        if kind == "start":
            # ⚠️ 2026-10-30：seed 的 int() 以前写在 try 外面 —— 手改报文/老页面发个
            #    非数字 seed 就 ValueError，WS 被静默断开、一条提示都没有。
            try:
                seed = int(message.get("seed") or 0)
            except (TypeError, ValueError):
                seed = 0
            info = self.viewer_info(ws)
            if info.get("role") != "host":
                self.send_to(ws, {"type": "error", "text": "只有主机能开局"})
                return
            prov = message.get("provider")
            if self.demo_mode:
                if isinstance(prov, dict) and prov.get("api_key") and prov.get("base_url") and prov.get("model"):
                    self.session_provider = {
                        "base_url": str(prov.get("base_url") or "").strip(),
                        "model": str(prov.get("model") or "").strip(),
                        "api_key": str(prov.get("api_key") or "").strip(),
                        "style": str(prov.get("style") or "").strip().lower(),
                        "temperature": prov.get("temperature"),
                        "max_tokens": prov.get("max_tokens"),
                    }
                elif not message.get("mock"):
                    self.send_to(ws, {"type": "error",
                                      "text": "这是公共体验实例：先在首页填你自己的 base_url / 模型名 / key（只存在你自己的浏览器里），"
                                              "或者勾「离线演练」先看流程。"})
                    return
            multi = bool(message.get("multiplayer")) or self.multiplayer
            try:
                result = await self.start_safe(bool(message.get("mock")), message.get("human_seat"),
                                               seed, message.get("board"), multiplayer=multi)
            except Exception:                            # noqa: BLE001
                self.session_provider = {}              # 开不起来就把访客 key 丢掉
                raise
            if not result.get("ok"):
                self.session_provider = {}
                self.broadcast({"type": "error", "text": result.get("error") or "开局失败"})
        elif kind == "hello":
            # 玩家进来/改名字。名字只做展示，限长、去控制字符。
            p_info = self.player(self.viewer_info(ws).get("pid") or "")
            if p_info is not None:
                name = str(message.get("name") or "").strip()
                name = "".join(ch for ch in name if ch.isprintable())[:12]
                if name:
                    p_info["name"] = name
                self.broadcast_room()
        elif kind == "claim":
            ok, why = self._claim_seat(self.viewer_info(ws).get("pid") or "",
                                       message.get("seat"))
            if not ok:
                self.send_to(ws, {"type": "notice", "text": why})
            self.broadcast_room()
            self.broadcast_status()
        elif kind == "release":
            ok, why = self._release_seat(self.viewer_info(ws).get("pid") or "")
            if not ok:
                self.send_to(ws, {"type": "notice", "text": why})
            self.broadcast_room()
            self.broadcast_status()
        elif kind == "kick":
            if self.viewer_info(ws).get("role") != "host":
                self.send_to(ws, {"type": "notice", "text": "只有主机能移出玩家"})
                return
            if self.running:
                self.send_to(ws, {"type": "notice", "text": "对局进行中不能移出玩家"})
                return
            pid = str(message.get("pid") or "")
            if pid and pid != "host" and pid in self.players:
                gone = self.players.pop(pid)
                for tok, owner in list(self.guest_tokens.items()):
                    if owner == pid:
                        self.guest_tokens.pop(tok, None)
                # 2026-10-31 外部审查 X2：以前只把他从玩家表里删掉，`seat_owner` 仍指着他 ——
                # 房间/status 里的 `owner` 会显示一个已经被移出的人（幽灵占座）。
                self.seat_owner = {int(q["seat"]): q["pid"]
                                   for q in self.players.values() if q.get("seat")}
                self.broadcast({"type": "notice", "text": f"已移出「{gone.get('name')}」"})
            self.broadcast_room()
            self.broadcast_status()
        elif kind == "room":
            self.send_to(ws, {"type": "room",
                              "room": self.room_public(self.viewer_info(ws).get("pid") or "",
                                                       self.viewer_info(ws).get("role") == "host")})
        elif kind == "board":
            if self.viewer_info(ws).get("role") != "host" or self.running:
                return
            want = str(message.get("board") or "").strip()
            try:
                picked = make_board(want, self.game_cfg.get("boards"))
            except Exception:                  # noqa: BLE001
                return
            self.lobby_board = want
            # 换到更小的板子时，超出座位数的玩家自动让出（否则他们卡在一个看不见的号上）。
            for p in self.players.values():
                if p.get("seat") and int(p["seat"]) > len(picked.roles):
                    p["seat"] = None
            self.seat_owner = {int(p["seat"]): p["pid"] for p in self.players.values() if p.get("seat")}
            self.broadcast_room()
            self.broadcast_status()
        elif kind == "reload":
            if self.viewer_info(ws).get("role") != "host":
                self.send_to(ws, {"type": "error", "text": "只有主机能改设置"})
                return
            load_env(os.path.join(HERE, ".env"), force=True)
            ok = self.has_any_key()
            self.broadcast({"type": "notice",
                            "text": "已重新读取 .env：" + ("读到 key 了 ✅" if ok else "还是没读到 key ❌")})
            self.broadcast_status()
        elif kind == "abandon":
            if getattr(self, "multiplayer", False) and self.viewer_info(ws).get("role") != "host":
                self.send_to(ws, {"type": "error", "text": "只有主机能放弃本局"})
                return
            result = await self.abandon()
            if not result.get("ok"):
                self.broadcast({"type": "error", "text": result.get("error") or "放弃失败"})
        elif kind == "stall_decision":
            if getattr(self, "multiplayer", False) and self.viewer_info(ws).get("role") != "host":
                self.send_to(ws, {"type": "error", "text": "只有主机能决定卡住的对局"})
                return
            # 用户在上游失败弹窗里点了按钮（retry / skip / abort）
            action = str(message.get("action") or "").strip().lower()
            if action not in ("retry", "skip", "abort"):
                self.broadcast({"type": "notice", "text": f"不认识的选项：{action}"})
            elif self.stall_decision is not None and not self.stall_decision.done():
                self.stall_decision.set_result(action)
            else:
                self.broadcast({"type": "notice", "text": "现在没有卡住的对局。"})
        elif kind == "auto_butler":
            seat = self.viewer_seat(ws)
            if not seat:
                self.send_to(ws, {"type": "notice", "text": "只有坐座位的玩家才能开全程托管。"})
                return
            if not self.running or self.game is None:
                self.send_to(ws, {"type": "notice", "text": "开局后才有你的行动，开全程托管用不上。"})
                return
            try:
                is_human = self.game.seat_by_id(seat).is_human
            except Exception:                          # noqa: BLE001
                is_human = False
            if not is_human:
                self.send_to(ws, {"type": "notice", "text": "这个座位不是真人座位，开不了全程托管。"})
                return
            on = bool(message.get("on"))
            self.set_auto_butler(seat, on)
            self.send_to(ws, {"type": "notice",
                              "text": ("🤖 全程托管已开启：接下来轮到你时自动让托管模型决策；"
                                       "再点一次可关闭。") if on else
                                      "🤖 全程托管已关闭：接下来轮到你时会等你亲自操作。"})
            self.broadcast_status()
        elif kind == "autopilot":
            seat = self.viewer_seat(ws)
            if not seat:
                self.send_to(ws, {"type": "notice", "text": "只有坐座位的玩家才能托管。"})
            else:
                task = self.auto_butler_tasks.get(seat)
                inflight = getattr(self, "butler_inflight", None)
                if inflight is None:
                    inflight = self.butler_inflight = set()
                if (task is not None and not task.done()) or seat in inflight:
                    self.send_to(ws, {"type": "notice",
                                      "text": "🤖 托管正在处理这一手，不用重复点。"})
                else:
                    # P1-6：后台跑，别把这条连接的读循环堵住
                    task = asyncio.create_task(self.autopilot(ws, seat))
                    inflight.add(seat)
                    task.add_done_callback(lambda t, s=seat: self._auto_task_done(s, t))
        elif kind == "action":
            # ⚠️ 2026-10-08 修（自查发现的 P2-1，真协议缺陷）：
            #    客户端回传的 `ask_id` 以前**只被回显、从不校验**。断线时玩家在旧面板上
            #    点了确认（那一手进了 OUTBOX），服务端按超时把 ask N 放掉、引擎已经问了
            #    ask N+1；重连补发时，那条**旧编号**的 action 会直接把 ask N+1 的 future
            #    解掉 —— 玩家真正的答案反而收到「现在不是你行动的时间。」，
            #    他那一票**全程没送到**（这是"丢一票=丢一局"的另一条路）。
            #    现在：编号对不上就当"迟到的旧答案"忽略掉，并明确回一句；
            #    只有当客户端**没带**编号、或者服务端**还没记下面板编号**时才沿用老行为
            #    （老客户端 / 老服务端的兼容口径）。
            # 2026-10-31 联机：按**这个连接坐的座位**找它自己的 ask，别人解不掉你的面板。
            seat = self.viewer_seat(ws)
            pending = self.pending_asks.get(seat)
            if not seat or not pending:
                self.send_to(ws, {"type": "notice", "text": "现在不是你行动的时间。"})
                return
            echo = message.get("ask_id")
            cur = pending.get("ask_id")
            if isinstance(echo, int) and not isinstance(echo, bool) and cur is not None and echo != cur:
                print(f"[ws] 丢弃迟到的旧答案：ask_id={echo}，当前面板是 {cur}（座位 {seat}）")
                self.send_to(ws, {"type": "notice",
                                  "text": f"这一手是上一条问题（#{echo}）的答案，已忽略；"
                                          f"现在等你回答的是 #{cur}。"})
                return
            future = self.asks.get(seat)
            if future is not None and not future.done():
                self.pending_asks.pop(seat, None)
                if self.pending_ask is pending:
                    self.pending_ask = next(iter(self.pending_asks.values()), None)
                # 玩家自己答了：把还挂着的全程托管任务收掉，别再多花一次模型钱。
                auto_task = self.auto_butler_tasks.pop(seat, None)
                if auto_task is not None and not auto_task.done():
                    auto_task.cancel()
                future.set_result(message.get("payload") or {})
                # ⚠️ 发"这一手已经收到"：手机切出去又切回来时，服务端会在 sync 里补发
                #    pending_ask，而用户离线期间点的那一手在 OUTBOX 里排队 —— 两者撞上就会出现
                #    "票投出去了、投票面板还赖在屏幕上"。有了这条，客户端任何情况下都能清掉面板。
                # ⚠️ 但「清面板」必须**只清对应那一条**（2026-10-07）：带上客户端回传的 ask_id，
                #    客户端就能判断「这条 ask_done 说的是不是我正在操作的那个面板」——
                #    否则排队到达的旧答案会清掉玩家**新收到**的面板（点了没反应的另一种形态）。
                #    老客户端不带 ask_id：那就照旧发不带 id 的形式，行为完全不变。
                done = {"type": "ask_done", "seat": seat}
                if isinstance(echo, int) and not isinstance(echo, bool):
                    done["ask_id"] = echo
                for other in list(self.clients.keys()):
                    if self.viewer_seat(other) == seat:
                        self.send_to(other, done)
            else:
                self.send_to(ws, {"type": "notice", "text": "现在不是你行动的时间。"})
        elif kind == "sync":
            self.heal_stale_state()
            # 这个连接之前被塞满、丢过消息吗？丢了就先清掉积压的流水消息（它们能靠下面的
            # 历史补发重建），再明确告诉用户"你错过了 N 条" —— 不能像以前那样静默丢掉。
            backlog = self._drain_stale(ws) + int(self.dropped.pop(ws, 0) or 0)
            if backlog:
                self.send_to(ws, {"type": "notice",
                                  "text": f"你错过了 {backlog} 条消息，已重新同步到最新状态"})
            info = self.viewer_info(ws)
            seat = self.viewer_seat(ws)
            is_host = info.get("role") == "host"
            self.send_to(ws, self.status_for(seat, host=is_host))
            self.send_to(ws, {"type": "room",
                              "room": self.room_public(info.get("pid") or "", is_host)})
            if self.game is not None and seat:
                self.send_you(ws, seat)
            # 补发已经发生过的记录：手机上切走再回来、页面重载之后，记录不会丢
            # （以前只发 status，页面就是一片空白 + 只有"之后"的新消息，看着像重开了一局）
            if self.game is not None:
                self.replay_history(ws, seat)
            if seat and seat in self.pending_asks:     # 只补发**你自己的**那一手
                self.send_to(ws, self.pending_asks[seat])
            if is_host and self.stall:                  # 上游失败卡住的弹窗只给主机
                self.send_to(ws, self.stall)

    def heal_stale_state(self) -> None:
        """自愈：如果"说在对局中"但其实没有在跑的任务，就把它复位。

        为什么需要：`放弃本局` 这条路实测出现过僵尸状态 ——
        running 还是 True，但对局任务已经没了、提问也没了，
        结果手机永远卡在"对局进行中，等别人发言…"，只能重启服务。
        这里做一道兜底：只要没有活着的任务，就不该说自己还在对局中。
        （真正的病根还没查清，所以先保证"错了也能自己爬出来"。）

        ⚠️ 但**收尾窗口里不能这么判**：`_run` 进 `_cleanup` 之前就把 `self.task` 置成了 None，
        那一局其实还在正常收尾（存档 / 写复盘笔记 / 关客户端）。这时手机只要发一条 `sync`
        （切走再回来、刷新页面都会发），就会把 `self.game` 清成 None，接着 `_cleanup` 里
        `if self.game is not game: return` 直接把结局的 `end` 广播跳过 ——
        用户永远看不到「🏁 谁赢了」（2026-10-06 复核实测复现）。
        所以这里必须排除 `self.cleaning`：收尾有自己的兜底（`_hard_reset_watchdog`）。
        """
        task_alive = self.task is not None and not self.task.done()
        if self.running and not task_alive and not self.cleaning:
            self.running = False
            self.cleaning = False
            self.awaiting = None
            self.asks.clear()
            self.pending_asks.clear()
            self.pending_ask = None
            self.game = None


# --------------------------------------------------------------------------- #

app = FastAPI(title="AI 狼人杀")
ROOM: "Room | None" = None


class ServerHumanAgent(WebHumanAgent):
    """`WebHumanAgent` 的服务端版（`Room.factory` 建的就是它）。

    ⚠️ 2026-10-25 重构：这个类以前**把模块级的 `asyncio.wait_for` 整个换掉**
    （`asyncio.wait_for = _spy`）来侦测"真人超时"。那是一个全局副作用：
    在那段时间里，同一事件循环上**任何**别的协程超时都会被记到真人头上，
    于是发出假的 `ask_done`（把玩家正在操作的面板清掉），而引擎那边还以为真人弃权。

    现在侦测点回到 `wolf/agents.py::WebHumanAgent.ask` 内部：它超时时会调用
    `Room.on_human_timeout(seat_id, ask_id)` 补那一条 `ask_done`。
    这个子类只保留类型名（工厂与注释都在用它），行为与父类完全一致，
    **不要再往这里加 `asyncio` 打桩**。
    """


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """参数不合法时也回 `{"error": "..."}`。

    ⚠️ FastAPI 默认回 422 + `{"detail": [...]}`，而前端只读 `.error` ——
    手机上会连一句提示都看不到（看起来像点了没反应）。任何校验失败都统一成
    前端看得懂的形状，免得以后每加一个参数就再踩一次。
    """
    fields = []
    for item in (exc.errors() or [])[:4]:
        loc = ".".join(str(x) for x in (item.get("loc") or ()) if x not in ("query", "body", "path"))
        fields.append(f"{loc or '参数'}：{item.get('msg') or '不合法'}")
    return JSONResponse({"error": "请求参数不对（" + "；".join(fields) + "）"}, status_code=400)


@app.get("/")
async def index():
    # ⚠️ 2026-10-08 加 `Cache-Control: no-store`：这一页是**改完就想立刻生效**的开发页
    #    （手机会话、PWA 全屏跑着），而 `FileResponse` 默认不带缓存指令 ——
    #    浏览器于是按启发式规则把整页 HTML 缓存下来，改了前端刷新还是旧的那份。
    #    实测踩到：`detailOf` 已经修好，无头浏览器里跑的还是旧页面，
    #    表现成"点一条没反应"，排查了很久（见 tests/t_p_age_review 的第一段断言）。
    return FileResponse(os.path.join(WEB_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate",
                                 "Pragma": "no-cache"})


# --- PWA：让手机"添加到主屏幕"之后像个 APP 一样全屏打开，不显示地址栏 ---
# （不需要打包 APK、也不需要往 APP 里塞浏览器内核；图标/清单走静态文件）
_PWA_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}


@app.get("/audio/{name}")
async def audio_file(name: str):
    """默认背景音乐（2026-11-03）。只允许 web/audio/ 下的 mp3/wav/ogg，别的一律 404。

    用户"导入自己的音乐"是**纯前端**的事（File API + IndexedDB），不占服务器存储。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(mp3|wav|ogg)", name or ""):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(WEB_DIR, "audio", name)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(os.path.join(WEB_DIR, "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_sw():
    # service worker 必须从根路径提供，作用域才覆盖整个站点
    return FileResponse(os.path.join(WEB_DIR, "sw.js"), media_type="text/javascript")


@app.get("/icon-{size}.png")
async def pwa_icon(size: str):
    if size not in ("192", "512"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(os.path.join(WEB_DIR, f"icon-{size}.png"), media_type="image/png")


@app.get("/api/status")
async def api_status(request: Request):
    """状态接口 —— **必须也要配对码**。

    ⚠️ 这里以前是裸奔的，而 `status()` 的正文里带着 `your_role` 和 `your_mates`
    （"你是狼人，狼队友 3号、4号"）。也就是说：任何网页/任何设备都能 GET 一下就拿到你的底牌；
    再叠上 DNS rebinding（把自己域名解析到 127.0.0.1，浏览器当同源）就能直接读走。
    审计实测：无 token、无自定义头 → 200 + 完整身份。这是信息隔离漏在 HTTP 层的一个洞。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"running": False, "error": problem}, status_code=401)
    return JSONResponse(ROOM.status() if ROOM else {"running": False})


@app.get("/api/usage")
async def api_usage(request: Request):
    """这一局**每个模型**的用量与思考情况（实时，不用等打完）。

    数据来自引擎的 transcript（每次调用一条：输入/输出 token、耗时、思考链长度、
    用的哪个思考档位、有没有失败）。手机上打一局想看看"谁最贵/谁最慢"就看这个。
    要配对码 —— 它包含每条的座位与任务名，属于对局内部信息。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    if ROOM is None or ROOM.game is None:
        return JSONResponse({"error": "还没有对局"}, status_code=409)

    rows = list(getattr(ROOM.game, "transcript", []) or [])
    by_model: dict[str, dict] = {}
    by_seat: dict[int, dict] = {}

    def bump(bucket: dict, key, row: dict) -> None:
        got = bucket.setdefault(key, {"calls": 0, "ok": 0, "failed": 0,
                                      "prompt_tokens": 0, "completion_tokens": 0,
                                      "cached_tokens": 0,
                                      "seconds": 0.0, "thinking_chars": 0,
                                      "max_seconds": 0.0})
        got["calls"] += 1
        if row.get("error"):
            got["failed"] += 1
        else:
            got["ok"] += 1
        got["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        got["completion_tokens"] += int(row.get("completion_tokens") or 0)
        got["cached_tokens"] += int(row.get("cached_tokens") or 0)
        sec = float(row.get("elapsed") or 0)
        got["seconds"] = round(got["seconds"] + sec, 1)
        got["max_seconds"] = round(max(got["max_seconds"], sec), 1)
        got["thinking_chars"] += len(str(row.get("reasoning") or ""))

    for row in rows:
        model = str(row.get("model") or "（未知/人类）")
        bump(by_model, model, row)
        bump(by_seat, int(row.get("seat") or 0), row)

    total = {
        "calls": len(rows),
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in rows),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in rows),
        # 缓存命中的输入 token（老存档没有这个字段 ⇒ 记 0，不猜）
        "cached_tokens": sum(int(r.get("cached_tokens") or 0) for r in rows),
        "seconds": round(sum(float(r.get("elapsed") or 0) for r in rows), 1),
        "thinking_chars": sum(len(str(r.get("reasoning") or "")) for r in rows),
        "failed": sum(1 for r in rows if r.get("error")),
    }
    total["cache_hit_rate"] = round(
        total["cached_tokens"] / total["prompt_tokens"], 3) if total["prompt_tokens"] else None
    for got in list(by_model.values()) + list(by_seat.values()):
        got["avg_seconds"] = round(got["seconds"] / max(1, got["calls"]), 1)
        # 每家平台的缓存口径不一样（有的根本不返回）—— 分开算，别用一个总命中率糊过去
        got["cache_hit_rate"] = round(
            got["cached_tokens"] / got["prompt_tokens"], 3) if got["prompt_tokens"] else None
    return JSONResponse({"day": ROOM.game.day, "board": ROOM.game.board,
                         "total": total,
                         "by_model": dict(sorted(by_model.items(),
                                                 key=lambda kv: -kv[1]["seconds"])),
                         "by_seat": {str(k): v for k, v in sorted(by_seat.items())}})


def read_token_error(request: Request) -> str | None:
    """只读接口的配对码校验：返回 None = 通过，否则是给用户看的一句话。

    ⚠️ 以前只读接口是"错就错、没有退避"：可以无限撞码，而写接口有 8 次/30 秒的退避。
    现在两边走同一个 `_token_problem`，失败表、文案、退避窗口完全一致。

    为什么**只读接口也必须**要码（这里以前叫 `read_token_ok`，已经合并进来）：
    存档里每个座位的 `transcript[].packet` 明写着「【你的身份】狼人」「【你的狼队友】8号、9号」，
    ledger 里还有 `visible_to=["wolf"]` 的夜间讨论 —— 不设闸的话，局域网里任何设备
    都能把这一局的底牌全读走，前面给 .env / WebSocket 加的码也就白加了。
    浏览器里直接看用 `?token=xxxxxx`。
    """
    if ROOM is None:
        return "需要配对码"
    token = (request.query_params.get("token")
             or request.headers.get("x-wolf-token") or "")
    return _token_problem(request, token)


def viewer_of_request(request: Request) -> dict | None:
    """读接口的"看的人"：主机配对码或访客令牌都认。认不出返回 None。

    只给**游戏内**接口用（规则 / 人设 / TTS / ASR）。设置、复盘、存档仍只认配对码。
    """
    if ROOM is None:
        return None
    token = (request.query_params.get("token")
             or request.headers.get("x-wolf-token") or "").strip()
    identify = getattr(ROOM, "identify", None)
    if not callable(identify):
        # 测试里的 in-proc 假房间只带 env_token/human_seat，没有完整 Room。
        if token.upper() and token.upper() == str(getattr(ROOM, "env_token", "")).upper():
            return {"role": "host", "pid": "host",
                    "seat": int(getattr(ROOM, "human_seat", 0) or 0)}
        return None
    ident = identify(token)
    if ident is None:
        return None
    role, pid = ident
    player_of = getattr(ROOM, "player", None)
    p = (player_of(pid) if callable(player_of) else None) or {}
    seat = p.get("seat")
    if seat is None and role == "host":
        seat = getattr(ROOM, "human_seat", 0)
    return {"role": role, "pid": pid, "seat": int(seat or 0)}


def read_viewer_error(request: Request) -> str | None:
    """访客也能用的只读接口校验（不是配对码错误，而是一句人话）。"""
    if not host_ok(request):
        return "不认识的 Host"
    if viewer_of_request(request) is not None:
        return None
    # ⚠️ 认不出来时仍要走配对码的失败退避：这些接口（规则/人设/TTS/ASR）现在
    #    也对外开着，不能变成"绕过 /api/games 的退避去暴力撞 6 位配对码"的新入口。
    token = (request.query_params.get("token")
             or request.headers.get("x-wolf-token") or "")
    problem = _token_problem(request, token)
    return problem or "需要配对码或有效的访客令牌"


# ---------------------------------------------------------------- 存档留档与语音
#: 存档留档策略（config 的 game.keep_games / game.save_budget_mb）：
#:   keep_games：最多留几局（0 = 不限）。**每局约 3 MB JSON + 8~16 MB 语音**。
#:   save_budget_mb：存档目录（含语音）的总预算，超了从最旧的开始删。
#: 为什么要两个：条数管"够看多少局"，预算管"别把硬盘写满"——
#: 用户给了 5 GB 额度，实测一局 12~20 MB，所以 10 局才 200 MB 左右，预算基本不会触发。
#: ⚠️ 2026-10-12：这套窗口只算**根目录**里的存档；mock 局（演练/测试）走
#:   `<save_dir>/_mock/` 子目录（见 `_mock_save_dir`），并在 `prune_saves` 里**先于真局被删**。
def settlement_rows(game) -> list[dict]:
    """结算页要用的一份"全部身份"快照（2026-11-03）。

    ⚠️ **只在对局结束（end）时发给客户端** —— 中途给出去就是作弊面。
    每行：座位 / 身份名 / 阵营 / 角色类别（wolf/god/villager）/ 是否存活 / 死因。
    """
    rows = []
    for seat in getattr(game, "seats", []) or []:
        role = ROLES.get(seat.role_key)
        rows.append({
            "seat": int(seat.id),
            "role": role_name(seat.role_key),
            "camp": camp_label(seat.camp),
            "kind": (role.kind if role else ""),
            "alive": bool(seat.alive),
            "death_cause": str(getattr(seat, "death_cause", "") or ""),
            # 死因文案的**唯一来源**是引擎的 CAUSE_TEXT；前端只用它，不再自己维护一份表
            "death_text": CAUSE_TEXT.get(str(getattr(seat, "death_cause", "") or ""), ""),
        })
    return rows


def settlement_payload(game) -> dict:
    """结算页的完整载荷：身份分布 + 座位 + 胜负（不含发言/票型，那些去复盘看）。"""
    rows = settlement_rows(game)
    counts = {"wolf": 0, "god": 0, "villager": 0}
    for row in rows:
        if row["kind"] in counts:
            counts[row["kind"]] += 1
    return {
        "winner": getattr(game, "winner", "") or "",
        "day": int(getattr(game, "day", 0) or 0),
        "board": str(getattr(getattr(game, "board_def", None), "name", "") or ""),
        "seats": rows,
        "counts": counts,
    }


def _save_policy(cfg: dict | None = None) -> tuple[int, int]:
    """留档策略。`cfg` 给了就用它（**房间自己的**配置），没给才回退到全局 ROOM。

    ⚠️ 2026-10-08 加 `cfg`：以前只看全局 ROOM —— 测试用临时 save_dir 建的 Room
    在清理时会被全局配置的策略管，两边不是同一份配置（与存档目录那个坑同源）。
    """
    cfg = ((cfg or {}).get("game") if cfg else None) or (ROOM.game_cfg if ROOM else {}) or {}
    try:
        keep = int(cfg.get("keep_games") or 0)
    except (TypeError, ValueError):
        keep = 0
    try:
        budget = int(cfg.get("save_budget_mb") or 0)
    except (TypeError, ValueError):
        budget = 0
    return max(0, keep), max(0, budget)


#: mock 局（离线演练 / 页面测试）的存档子目录名 —— 落在 **`<save_dir>/_mock/`**，绝不写生产根目录。
MOCK_SAVE_DIRNAME = "_mock"


def _mock_save_dir(save_dir: str) -> str:
    """mock 局的存档目录：`<save_dir>/_mock/`（**子目录，不是「不写」**）。

    ⚠️ 2026-10-12 修（浏览器页面测试把 mock 局写进用户的生产 `games/`，代价是那 9 份真局的
    保留窗口被挤到边缘）：mock 局与真局原来混在**同一个目录**里，而 `prune_saves()` 只按文件名
    （= 时间戳）从最旧的开始删 —— mock 一多，**最旧的那几份真局正好是第一批被删的**。
    所以从"写"这一步就把 mock 局跟真局分开。

    为什么选**子目录**而不是临时目录（`tempfile.mkdtemp`）：
      ① 临时目录在 Windows 上谁清、什么时候清都不确定，开发者调完 mock 局**找不到**存档，
         那正是「不要静默丢弃」要避免的；子目录就在存档目录旁边，
         `python tools/review.py --save-dir games/_mock --list` 就能列出来复盘。
      ② 清理口径全都锚在 `<save_dir>`（`prune_saves` / `_prune_orphan_audio`）：用临时目录
         要么永不清理（磁盘只涨）、要么再写一套「临时目录在哪」的清理逻辑；子目录直接复用同一套。
      ③ 复盘列表读的是 `games/*.json`（`/api/games` 与 `tools/review.py::latest_game` 都是
         **非递归** glob）⇒ 子目录**天然不会被扫进来**：mock 局不会进用户的复盘列表，
         也不占 `keep_games` 的保留窗口；要它们就明确指出 `_mock`（要排除的路径只有这一条）。
    """
    return os.path.join(save_dir, MOCK_SAVE_DIRNAME)


#: `_save_is_mock()` 的判定缓存：路径 →（文件大小, mtime_ns, 是不是 mock）。
#: 为什么要缓存：prune 每收一局都要判一遍整个存档目录（实测 100 局 / 355 MB 全解析要 1.2 秒），
#: 而其中绝大多数文件这一轮根本没动过。mtime 是 100ns 精度，只有真被改写过的那几份会重解析。
_MOCK_FLAG_CACHE: dict[str, tuple[int, int, bool]] = {}


def _save_is_mock(path: str) -> bool:
    """这份存档是不是 mock 局 —— 判据用**存档内容**，不是文件名 / 目录名。

    判据（两条取「或」）：
      · 存档里有显式标记 `mock: true`（2026-10-12 起 `_write_save` 会写上）；
      · 或者 `transcript` 里**没有一个非空的 `model`** —— mock 局是 `MockAgent` 顶上的，
        它的调用记录 `model` 一律是空串；真局每一条都带路由解析出来的模型名。
    第二条是给**已经躺在生产目录里那 91 份历史 mock** 用的（它们文件名跟真局一模一样，
    只能靠内容认；这正是「能处理已经存在的 mock」那条要求）。

    读不出来（半截 JSON / 被别的工具改坏）一律当**真局**：宁可多留，不可误删。
    """
    try:
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    hit = _MOCK_FLAG_CACHE.get(path)
    if hit is not None and key is not None and (hit[0], hit[1]) == key:
        return hit[2]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False                                   # 读不出来 = 当"真局"，排在最后删
    if not isinstance(data, dict):
        mock = False                                   # 不是我们写的那种存档 → 不猜，当"真局"
    elif data.get("mock") is True:
        mock = True
    elif data.get("mock") is False:
        mock = False
    else:
        tr = data.get("transcript")
        has_model = isinstance(tr, list) and any(
            str(e.get("model") or "").strip() for e in tr if isinstance(e, dict))
        mock = not has_model
    if key is not None:
        if len(_MOCK_FLAG_CACHE) > 4096:               # 目录被搬过/删过时别让缓存无限长
            _MOCK_FLAG_CACHE.clear()
        _MOCK_FLAG_CACHE[path] = (key[0], key[1], mock)
    return mock


def _prune_plain_dir(dir_path: str, keep: int, budget: int) -> list[str]:
    """把「一整个目录都是 mock」的那种目录也按同一套保留策略清一清（最旧的先删）。

    ⚠️ 为什么要顺带清 `<save_dir>/_mock/`：不清就等于「把无限增长从根目录挪到了子目录」——
    页面测试每跑一轮就多一份 mock。这里删的**全是 mock**（真局永远不进这个目录），
    所以不可能碰到真局；返回删掉的文件名（给日志用）。
    """
    try:
        names = sorted(f for f in os.listdir(dir_path)
                       if f.endswith(".json") and os.path.isfile(os.path.join(dir_path, f)))
    except OSError:
        return []                                      # 目录还不存在（还没跑过 mock 局）
    removed: list[str] = []

    def drop(name: str) -> None:
        try:
            os.remove(os.path.join(dir_path, name))
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[save] mock 存档删不掉 {name}：{type(exc).__name__}: {exc}")
            return
        side = os.path.join(dir_path, name[:-len(".json")])
        if os.path.isdir(side):
            shutil.rmtree(side, ignore_errors=True)
        removed.append(name)

    remaining = len(names)
    if keep:
        while remaining > keep and names:
            drop(names.pop(0))
            remaining -= 1
    if budget:
        while names and _dir_size_mb(dir_path) > budget and remaining > 1:
            drop(names.pop(0))
            remaining -= 1
    return removed


def _dir_size_mb(path: str) -> float:
    total = 0
    for base, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total / 1024 / 1024


def _archive_audio(save_dir: str, stem: str, data: dict, human_camp: str = "") -> int:
    """把这一局每条发言的语音留档到 `<save_dir>/<stem>/audio/<序号>.mp3`。

    用**硬链接**（同一分区不占额外空间）；不支持时退回复制。
    缓存里没有的那条（当时语音没开 / 已经清掉）就跳过 —— 存档里也不会标 audio。
    `human_camp` 决定"狼队夜聊要不要一起留档"（与 `_say_ok` 同一口径）。
    """
    try:
        vcfg = voice.VoiceConfig.from_env(ROOM.cfg if ROOM else {})
    except Exception:                                     # noqa: BLE001
        return 0
    if not vcfg.ready:
        return 0
    entries = data.get("ledger") or []
    dest_dir = os.path.join(save_dir, stem, "audio")
    made = 0
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        seat = entry.get("seat")
        # 口径与推送、/api/tts 共用（真人坐狼时，狼队夜聊也要留档，否则复盘重放不了）
        if not _say_ok(kind, seat, entry.get("visible_to"), human_camp) or not seat:
            continue
        text = str(entry.get("text") or "")
        if not text:
            continue
        try:
            sv = vcfg.seat_voice(int(seat))
            cache = _voice_cache_path(int(seat), sv.key(), text, vcfg.tts_model)
        except Exception:                                 # noqa: BLE001
            continue
        if not os.path.exists(cache):
            continue
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, f"{idx}.mp3")
        try:
            if os.path.exists(target):
                os.remove(target)
            try:
                os.link(cache, target)                    # 硬链接：不占额外空间
            except OSError:
                shutil.copyfile(cache, target)            # 跨盘 / 不支持时复制
            entry["audio"] = True
            made += 1
        except OSError:
            continue
    if made:
        data["audio_count"] = made
    return made


def _prune_orphan_audio(save_dir: str, grace_sec: float = 60.0) -> list[str]:
    """清掉「语音目录还在、同名 json 已经没了」的孤儿目录（删不掉就留着，不影响对局）。

    ⚠️ 只认**同时满足**这三条的目录：名字以 `game-` 开头、里面有 `audio` 子目录、
    且没有同名 `.json`。存档目录里可能还有别的东西（用户自己放的、别的工具建的），
    宁可漏清也不能误删 —— 所以条件写死，不猜、不递归、不碰别的子目录。

    ⚠️ 还要躲开「正在写」的那一局：`save()` 是**先建语音目录、再写 json**（见
    `_archive_audio` → `to_dict`/`json.dump` 的顺序），中间那一小段窗口里，
    新局的目录看起来就跟孤儿一模一样。所以刚改过（`grace_sec` 秒内）的目录一律放过，
    真孤儿下一轮清理时自然会被扫到。
    """
    out: list[str] = []
    now = time.time()
    try:
        names = os.listdir(save_dir)
    except OSError:
        return out
    for name in names:
        if not name.startswith("game-"):
            continue
        path = os.path.join(save_dir, name)
        if not os.path.isdir(path):
            continue
        if os.path.exists(os.path.join(save_dir, name + ".json")):
            continue                                  # 还有存档：跟着存档一起删（见 drop）
        if not os.path.isdir(os.path.join(path, "audio")):
            continue                                  # 不是我们建的那种语音目录 → 不碰
        try:
            if now - os.path.getmtime(path) < grace_sec:
                continue                              # 太新：可能正是「先建目录、后写 json」那一局
            shutil.rmtree(path)
            out.append(name)
        except OSError as exc:
            print(f"[save] 孤儿语音目录删不掉 {name}：{type(exc).__name__}: {exc}")
    return out


def prune_saves(cfg: dict | None = None) -> dict:
    """按「留几局 + 总预算」清理存档：**先删 mock、再删真局**，各自从最旧的开始删。

    `cfg` 给了就用**这个房间自己的**配置（存档目录与留档策略都从它取）；
    没给才回退到全局 ROOM —— 见 `_save_policy` 的说明。

    ⚠️ 2026-10-12 修（「真局被 mock 挤出保留窗口」）：原来只按**文件名**排一条队、从最旧的
    开始删，而 mock 局（测试/演练）比真局多得多 —— 于是**最旧的真局正好是第一批被删的**
    （用户那 9 份真局正坐在这个窗口边缘）。现在按**存档内容**分两拨（见 `_save_is_mock`），
    删除顺序 = mock（旧→新）+ 真局（旧→新）：真局只有在「连 mock 都删光了还超窗口」时才动。
    mock 局还有自己的子目录 `<save_dir>/_mock/`（见 `_mock_save_dir`），顺带按同一套策略清。

    返回 {"removed": [...], "failed": [...], "orphans": [...], "removed_mock": [...],
    "kept": n, "mb": x}，方便写日志和测试断言。`removed` 只装**根目录**里删掉的；
    `_mock/` 子目录删掉的那些单独进 `removed_mock`（真局不可能出现在那里）。

    ⚠️ `removed` 只装**真的不在了**的存档（删成功、或本来就不存在）：
    以前删失败也往里塞，日志和测试都会以为「清理干净了」，实际磁盘上一局没少。
    现在删不掉单独进 `failed` 并打一行日志。
    """
    save_dir = resolve_save_dir(cfg)
    if not os.path.isdir(save_dir):
        return {"removed": [], "failed": [], "orphans": [], "removed_mock": [],
                "kept": 0, "mb": 0.0}
    keep, budget = _save_policy(cfg)
    # mock 局自己的子目录**先**清：根目录那条预算检查（`_dir_size_mb` 是递归的）会把它的体积
    # 算进来，先清掉才不至于让根目录里的真局替 mock 的体积买单。
    removed_mock = _prune_plain_dir(os.path.join(save_dir, MOCK_SAVE_DIRNAME), keep, budget)
    files = sorted(f for f in os.listdir(save_dir)
                   if f.endswith(".json") and os.path.isfile(os.path.join(save_dir, f)))
    removed: list[str] = []
    failed: list[str] = []

    def drop(name: str) -> None:
        try:
            os.remove(os.path.join(save_dir, name))
        except FileNotFoundError:
            pass                          # 本来就不存在 = 这一局已经没了，照样算「删掉了」
        except OSError as exc:
            failed.append(name)           # 删不掉：别记进 removed（日志会骗人）
            print(f"[save] 存档删不掉 {name}：{type(exc).__name__}: {exc}")
            return
        side = os.path.join(save_dir, name[:-len(".json")])   # 同名语音目录
        if os.path.isdir(side):
            shutil.rmtree(side, ignore_errors=True)
        removed.append(name)

    # ⚠️ 2026-10-12 修：**删除顺序 = mock（旧→新）再真局（旧→新）**。
    #    原来只有一条按文件名排的队（= 时间顺序），mock 一多，最旧的真局就是第一批被删的。
    #    判据看存档内容（`_save_is_mock`），所以已经躺在目录里的历史 mock 也一样认得出。
    mocks = [f for f in files if _save_is_mock(os.path.join(save_dir, f))]
    mock_set = set(mocks)
    real = [f for f in files if f not in mock_set]
    queue = mocks + real
    remaining = len(files)                # 还没「处理掉」的存档数（删失败的也算处理过，与旧口径一致）
    if keep:
        # ⚠️ 这里**不能**写 len(files) - len(removed)：files 是一开始的全量列表，边 pop 边让
        #    removed 增长，等于同一批文件被扣了两次 —— 114 局只删掉 7 个就停了（自测抓到的）。
        while remaining > keep and queue:
            drop(queue.pop(0))
            remaining -= 1
    if budget:
        # 预算兜底：同样先删 mock、后删真局，直到总量落回预算内（至少留 1 局）
        while queue and _dir_size_mb(save_dir) > budget and remaining > 1:
            drop(queue.pop(0))
            remaining -= 1
    orphans = _prune_orphan_audio(save_dir)
    # `kept` 报**磁盘上真实剩几局**：删失败的那些还躺在目录里，按上面那个列表算会少报
    # （「已经清理干净了」的假象 —— 跟 removed 记错是同一类问题）。
    kept = len([f for f in os.listdir(save_dir)
                if f.endswith(".json") and os.path.isfile(os.path.join(save_dir, f))])
    return {"removed": removed, "failed": failed, "orphans": orphans,
            "removed_mock": removed_mock, "kept": kept,
            "mb": round(_dir_size_mb(save_dir), 1)}


@app.get("/api/saved_audio")
async def api_saved_audio(request: Request, game: str = "", n: str = "-1"):
    """播**存档里**那条发言的语音（复盘用，不再重新合成）。

    `game` 是存档文件名（如 game-20261007-174335.json），`n` 是台账序号。
    名字严格校验 + 只允许读 <save_dir>/<局名>/audio/<数字>.mp3 —— 防目录穿越。

    ⚠️ `n` 是负数、或超出这一局的台账条数（序列号从 0 数起）时回 **400**：
    那是「请求参数不对」，不是「这一条没留语音」（404 的那句话会把排查方向带偏：
    明明是这个序号根本不存在，看起来却像「语音没存上/被清理了」）。
    只有序号**确实在这局里**、但那条没留语音时才给 404。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    if not re.fullmatch(r"game-[0-9A-Za-z_\-]+\.json", game or ""):
        return JSONResponse({"error": "存档名不合法"}, status_code=400)
    try:
        idx = int(str(n).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": "n 必须是台账序号（整数）"}, status_code=400)
    if idx < 0:
        return JSONResponse({"error": f"n 不能是负数（台账序号从 0 数起，收到 {idx}）"},
                            status_code=400)
    # 越界判定要用**这一局自己的**台账条数：存档 json 读得到才判，读不到就退回老行为
    # （不在参数上冤枉一个可能合法的序号）。
    total = _save_ledger_len(os.path.join(resolve_save_dir(), game))
    if total is not None and idx >= total:
        return JSONResponse({"error": f"n 超出这一局的台账条数（共 {total} 条，序号 0~{total - 1}）"},
                            status_code=400)
    path = os.path.join(resolve_save_dir(), game[:-len(".json")], "audio", f"{idx}.mp3")
    if not os.path.exists(path):
        return JSONResponse({"error": "这一条没有留语音（当时语音没开，或存档已清理）"},
                            status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


def _save_ledger_len(save_path: str) -> int | None:
    """读存档 json，返回它的台账条数；读不到（文件没了 / 坏了 / 不是 json）就返回 None。

    只读一次、只取长度：`/api/saved_audio` 每次点一条语音都要判越界，
    不能为了几个数字把整份存档（可能几 MB）常驻内存。
    """
    try:
        with open(save_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ledger = data.get("ledger")
    return len(ledger) if isinstance(ledger, list) else None


@app.get("/api/games")
async def api_games(request: Request):
    if not host_ok(request):
        return JSONResponse({"games": [], "error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"games": [], "error": problem}, status_code=401)
    # ⚠️ 2026-10-25：这里原来先算一次 save_dir、紧接着又被 `resolve_save_dir()` 覆盖（死赋值），
    #    已经删掉那行 —— 读接口与写接口必须锚在同一个函数上（见 `resolve_save_dir` 的说明）。
    save_dir = resolve_save_dir()
    if not os.path.isdir(save_dir):
        return JSONResponse({"games": []})
    # ⚠️ 这里**只列根目录**（非递归）：mock 局在 `<save_dir>/_mock/` 子目录里，故意不在复盘
    #    列表出现 —— 否则演练局会把用户的真局刷下去（见 `_mock_save_dir`）。想看它们：
    #    `python tools/review.py --save-dir games/_mock --list`。
    files = sorted(f for f in os.listdir(save_dir) if f.endswith(".json"))
    return JSONResponse({"games": files[-200:]})


@app.get("/api/guidance")
async def api_guidance(request: Request):
    """手机上翻「模型被告知了什么」：通用提示词 / 打法书 / 概念库（进包段落）。

    2026-10-07 起这三层取代了按座位的角色卡（人设），所以「人设」面板改成展示它们。
    **给的是未过滤的全文**：每个座位实际只收到"通用 + 自己那个角色/阵营"的部分，
    但这里不能按座位过滤 —— 那样等于告诉看的人"这个座位是狼/预言家"（隔离要求）。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_viewer_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    cfg = ROOM.cfg if ROOM is not None else {}
    game_cfg = (ROOM.game_cfg if ROOM is not None else {}) or {}
    prompt = tabletalk.player_prompt(cfg, HERE)
    book = (tabletalk.load_playbook(HERE, str(game_cfg.get("playbook_path") or ""))
            if game_cfg.get("playbook", True) else "")
    meta = (tabletalk.load_meta_book(HERE, str(game_cfg.get("meta_book_path") or ""))
            if game_cfg.get("meta_book", True) else "")
    return JSONResponse({
        "player_prompt": prompt,
        "playbook": book,
        "meta": meta,
        "chars": {"player_prompt": len(prompt), "playbook": len(book), "meta": len(meta)},
        # 面板顶上那段说明：写清"每个座位每次调用到底收到哪些块"，以及**为什么这里是全文**。
        # ⚠️ 2026-10-11 重写（原来漏了规则书/私有信息两块，而且没说清"什么时候生效"）。
        # ⚠️ 生效时机要分开写（实测口径）：`.md` 内容文件每次建包都现读 ⇒ **下一局生效、不用重启**；
        #    `config.yaml` 是**启动时读进内存**的（player_prompt / playbook 开关 / rules_mode 都在里面）
        #    ⇒ **必须重启服务**才生效。旧文档一律写成"下一局生效，不用重启"，对 config 那半是错的
        #    （2026-10-11 实测：改完 config 里的 player_prompt，运行中的服务仍然发旧文案）。
        "note": ("每个座位每次调用实际收到的：身份 + 你的定位（下面第一块，人人一样）"
                 "+ 打法书（通用章 + 自己那个角色） + 概念与概率（通用 + 自己阵营）"
                 "+ 开局公示栏里的规则书 + 自己的私有信息（经历 / 队友 / 推理笔记）。"
                 "这里给的是**未过滤的全文** —— 按座位过滤等于告诉你「这个座位是狼还是神」"
                 "（信息隔离要求）。生效时机：**下面这三个 .md 下一局生效（不用重启）**；"
                 "`config.yaml` 里的那两段（player_prompt / playbook 开关等）"
                 "**要重启服务**才读得到；改引擎也要重启。"),
    })


@app.get("/api/rules")
async def api_rules(request: Request):
    """规则书（手机上的「规则」按钮 / 电脑端都能随时翻）。

    要配对码：和对局接口一致 —— 这是个只读文本接口，但没必要给不认识的来访者。
    · `for_game`：**按当前板子组装**的那份（只含本局真有的角色规则）—— 模型和公示栏看的就是它
    · `full`：给人看的完整说明（规则书.md 下半部分）
    · `mode`：规则放哪儿（room 公示栏 / packet 座位提示词 / both / off）
    · `board`：有对局 = 那一局的板子；没对局 = 手机**当前选中的板子**（预览，见下）
    · `running` / `preview`：前者=有对局在跑；后者=这份是按"你选中的板子"现组装的预览

    ⚠️ 2026-10-11 加 `?board=`：以前没有对局时这里回的是"上一局房间"的板子（`ROOM.game`
    跑完也还在），于是手机上会出现**面板写着 eleven_standard、选择器里选着 11人·狼王**
    的对不上（作者截图反馈）。现在没有对局时按**你选中的那张板子**组装，并标 `preview`,
    有对局时**忽略参数**（本局就是本局，不能让你看着别的板子的规则打这一局）。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_viewer_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    _frags, human = rules.load()
    for_game = ""
    running = bool(ROOM is not None and ROOM.game is not None)
    want = str(request.query_params.get("board") or "").strip()
    preview = False
    if running:
        board_name = ROOM.game.board
        for_game = ROOM.game.rules_text
    else:
        # 没有对局（或对局已结束）：按"选中的板子"预览；参数非法就退回默认板子
        # ⚠️ 2026-10-25 修：`make_board` 必须带 `custom` —— 以前只传名字，
        #    `game.boards` 里的自定义板子会抛 KeyError，被下面 except 吃掉后
        #    **静默回退成默认板子**（手机上选自定义板子却看到默认板子的规则）。
        _custom = (ROOM.game_cfg if ROOM is not None else {}).get("boards")
        board_name = want or str((ROOM.game_cfg if ROOM is not None else {}).get("board")
                                 or "eleven_wolfking")
        try:
            _bd = make_board(board_name, _custom)
        except Exception:                               # noqa: BLE001
            board_name = str((ROOM.game_cfg if ROOM is not None else {}).get("board")
                             or "eleven_wolfking")
            _bd = make_board(board_name, _custom)
        try:
            # ⚠️ 2026-10-30：预览也要跟开局走同一个口径。以前写死 `len(roles)>=11`，
            #    配置里 `sheriff: false` 或改过 `sheriff_min_seats` 时，预览的规则和
            #    真正发给模型的规则不一致。
            _setting = ROOM.sheriff_setting() if ROOM is not None else None
            _min_seats = int((ROOM.game_cfg if ROOM is not None else {})
                             .get("sheriff_min_seats", 11) or 11)
            _sheriff = _setting if _setting is not None else len(_bd.roles) >= _min_seats
            for_game, _ = rules.for_game(
                _bd, sheriff=_sheriff,
                detail=str((ROOM.game_cfg if ROOM is not None else {}).get("rules_detail", "brief")),
                seer_auto_run=bool((ROOM.game_cfg if ROOM is not None else {})
                                   .get("seer_auto_run", True)))
            preview = True
        except Exception as exc:                        # noqa: BLE001
            for_game = f"（组装不了这张板子的规则：{type(exc).__name__}: {exc}）"
    return JSONResponse({
        "mode": ROOM.rules_setting() if ROOM is not None else "room",
        "detail": str((ROOM.game_cfg if ROOM is not None else {}).get("rules_detail", "brief")),
        "board": board_name,
        "running": running,
        "preview": preview,
        "for_game": for_game, "full": human,
        "chars": {"for_game": len(for_game), "full": len(human)},
    })


@app.get("/api/personas")
async def api_personas(request: Request):
    """当前会加载哪些座位人设、来自哪个文件、有没有越界。

    要配对码：虽然这里只有人设文本、不含身份，但它会暴露"每个座位的人设"，
    配上"座位号=谁"就能做社工；而且不设闸的接口越多，审计时的盲区越大。
    """
    if not host_ok(request):
        return JSONResponse({"personas": [], "warnings": [], "error": "不认识的 Host"}, status_code=403)
    problem = read_viewer_error(request)
    if problem:
        return JSONResponse({"personas": [], "warnings": [], "error": problem}, status_code=401)
    if ROOM is None:
        return JSONResponse({"personas": [], "warnings": []})
    loaded = persona.load(ROOM.cfg, HERE)          # 现读现取，改了文件立刻能看到
    return JSONResponse({
        "dir": str(ROOM.game_cfg.get("personas_dir") or ""),
        "warnings": loaded.warnings,
        "personas": [
            {"seat": seat_id, "source": loaded.sources.get(seat_id, ""),
             "chars": len(text), "text": text,
             # 结构化字段（面板按字段展示，看得清写了什么）
             "card": {k: getattr(loaded.personas[seat_id], k, "")
                      for k in ("name", "age", "job", "traits", "background", "relations")}}
            for seat_id, text in sorted(loaded.texts.items())
        ],
    })


#: 合成结果按"音色 + 文本"缓存到磁盘：同一句话不重复花钱，重放也不用等
# ⚠️ 缓存目录可配（默认 var/voice_cache）：以前写死在项目根，跟源码混在一起。
#    老路径 voice_cache/ 仍然能用 —— 只要 config.yaml 里写 voice_cache_dir: voice_cache。
#    注意：这个常量在**模块顶层**就要算好（`_voice_cache_path` 是模块级函数），
#    而 config 是 main() 里读的 —— 所以这里现读一次配置，别去引用不存在的全局。
#
# ⚠️ 2026-10-08 修（审查发现 P3-5）：这里以前**只读项目根的 config.yaml**，
#    完全无视 `--config`。后果：`tests/t_tts_cached.py` 那类会真的合成/写缓存的测试脚本
#    跑起来时，写的还是**线上那份共用缓存**（`var/voice_cache`）—— 今天只是"恰好没撞上"，
#    哪天有人把线上音色抄进 tests/test-config.yaml，测试就会覆盖线上真实缓存条目再删掉。
#    现在：`--config` 指到哪份配置，就按那份配置的 `voice_cache_dir` 算
#    （测试实例于是可以有自己的缓存目录；命令行没写 --config 时行为完全不变）。
def _voice_cache_dir() -> str:
    name = os.path.join("var", "voice_cache")
    cfg_path = os.path.join(HERE, "config.yaml")
    argv = list(sys.argv)
    for i, item in enumerate(argv):
        if item == "--config" and i + 1 < len(argv):
            cfg_path = argv[i + 1]
            break
        if item.startswith("--config="):
            cfg_path = item.split("=", 1)[1]
            break
    if not os.path.isabs(cfg_path):
        cfg_path = os.path.join(HERE, cfg_path)
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        name = str(((cfg.get("game") or {}).get("voice_cache_dir")) or name)
    except Exception:                                 # noqa: BLE001
        pass
    if os.path.isabs(name):
        return name
    # 相对路径锚在**那份配置所在的目录**：测试用 tests/xxx.yaml 时，缓存放 tests/ 下，
    # 不会跟项目根的 var/voice_cache 混在一起（老路径仍然按项目根算，行为不变）。
    base = HERE if os.path.basename(cfg_path) == "config.yaml" else os.path.dirname(cfg_path)
    return os.path.join(base, name)


VOICE_CACHE = _voice_cache_dir()


def resolve_save_dir(cfg: dict | None = None) -> str:
    """存档目录：**写和读必须锚在同一个地方**。

    ⚠️ 2026-10-06 修（服务/前端审查的 P2-1）：写存档时把相对路径拼到了 `HERE`（项目目录），
    而 `/api/games`、`/api/game/{name}` 直接把相对路径交给 os.listdir → 锚在**进程 CWD**。
    只要服务不是"在 werewolf 目录里"启动（快捷方式/别的 CWD），
    存档照写不误、但手机「复盘」永远说"还没有存档"；CWD 恰好有同名目录时还会列出别人的文件。
    """
    raw = str(((cfg or {}).get("game") or {}).get("save_dir")
              or ((ROOM.game_cfg.get("save_dir") if ROOM else "") or "games"))
    return raw if os.path.isabs(raw) else os.path.join(HERE, raw)


def _voice_cache_path(seat: int, voice_name: str, text: str, model: str = "") -> str:
    # ⚠️ 缓存键要带上**合成模型**：换了 TTS_MODEL（或网关）之后，同一句话得重新合成，
    #    否则手机还放旧模型生成的音频（"改了不生效"，2026-10-06 服务侧审查指出）。
    key = hashlib.sha1(f"{seat}|{model}|{voice_name}|{text}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(VOICE_CACHE, f"{key}.mp3")


@app.get("/api/tts")
async def api_tts(request: Request, n: str = "-1", cached: str = ""):
    """把某条台账发言合成为语音。`n` 是台账里的序号（前端从 entry 事件里拿到）。

    只有"发言"会合成；公告、投票结果之类不合成（用户定的：那些用短提示音，
    而且一条条读出来又慢又费钱）。

    `cached=1`：**只读缓存**（2026-10-07 加，给前端的「🔊 重放」用）——
    命中就回 mp3；没命中直接 404，**绝不现场合成、也不写缓存**。
    为什么要有这个口径：重放已经听过的那条本来就该是零花费，可要是走了老路径，
    缓存恰好被清过/换了语音配置，点一下就会**当场花一次合成的钱**。

    ⚠️ `n` 故意声明成 str 再自己转 int：以前写成 `n: int = -1`，参数不合法时
    FastAPI 会先返回 422，body 是 `{"detail": [...]}`，而前端只读 `.error` ——
    于是手机上什么提示都没有。现在任何非法输入都保证回 `{"error": "..."}`。
    `cached` 同理声明成 str：认不出来的值一律当「没开」（= 老行为），不 422。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_viewer_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    viewer = viewer_of_request(request) or {}
    seat = int(viewer.get("seat") or 0)
    try:
        index = int(str(n).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": "n 必须是台账里的序号（整数）"}, status_code=400)
    only_cached = str(cached or "").strip().lower() in ("1", "true", "yes", "on", "cached")
    if ROOM is None or ROOM.game is None:
        return JSONResponse({"error": "还没有对局"}, status_code=409)
    entries = ROOM.game.ledger.entries
    if not (0 <= index < len(entries)):
        return JSONResponse({"error": "没有这条"}, status_code=404)
    entry = entries[index]
    me = ROOM._viewer_seat_obj(seat)
    if me is None or not me.is_human:
        return JSONResponse({"error": "你还没坐下（房间观众不能点播语音）"}, status_code=403)
    if not entry.visible(me.id, me.camp):           # 隔离：别人看不到的条目也不能合成给他听
        return JSONResponse({"error": "没有这条"}, status_code=404)
    if not _say_ok(entry.kind, entry.seat, entry.visible_to, getattr(me, "camp", "")):
        return JSONResponse({"error": "这条不是发言"}, status_code=400)
    if entry.seat == me.id:
        return JSONResponse({"error": "这是你自己说的"}, status_code=400)   # 不用给自己配音

    # 每次现读：这样你在手机面板里改完语音 key，下一句就生效（不用重启服务）
    cfg: voice.VoiceConfig = voice.VoiceConfig.from_env(ROOM.cfg)
    sv = cfg.seat_voice(entry.seat)
    # 缓存键要带上语速/音高/语气指令：不然改了这些，手机还是放旧的音频
    path = _voice_cache_path(entry.seat, sv.key(), entry.text, cfg.tts_model)
    if os.path.exists(path):
        return FileResponse(path, media_type="audio/mpeg")
    if only_cached:
        # 只读口径：到这儿就是「当时没合成过」。**不许**往下走到合成那几行。
        return JSONResponse({"error": "这条没有缓存（当时没合成过）"}, status_code=404)
    if not cfg.ready:
        return JSONResponse({"error": "语音服务还没配好（缺 VOICE_BASE_URL / VOICE_API_KEY）"},
                            status_code=503)
    try:
        audio = await voice.tts(entry.text, sv.voice, cfg, seat=entry.seat)
    except voice.VoiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    os.makedirs(VOICE_CACHE, exist_ok=True)
    # ⚠️ 2026-10-30：临时文件名不能固定成 `<path>.part` —— 同一条发言被两个连接
    #    同时点播时，两边会共写同一个临时文件，先写完的那边 rename 出去的可能是半截。
    #    用带 pid + 纳秒的唯一名字；rename 仍是原子的，谁先到谁生效。
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(audio)
        os.replace(tmp, path)             # 先写临时文件再改名：并发请求不会读到半个文件
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/asr")
async def api_asr(request: Request):
    """把手机录的音频转成文字。

    ⚠️ 只返回"听到了什么"，**不直接替你行动**：ASR 把「四号」听成「十号」的代价太大，
    所以前端拿到文字后是填进输入框让你确认，而不是自动投票。
    `legal` 传当前合法目标（逗号分隔）时，顺便给一个"号码"猜测供前端高亮。

    体积上限 25MB 的检查**在读 body 之前**：
      · 先看 `content-length`，超了直接 413（一个字节都不读）；
      · 没带/带了也不可信时改成边读边数，一超限立刻停手。
        以前是 `await request.body()` 把整个包读进内存之后才判断 —— 有人发 2GB 就先把内存吃满。
    """
    if not host_ok(request):
        return JSONResponse({"ok": False, "error": "不认识的 Host"}, status_code=403)
    problem = read_viewer_error(request)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    if not ROOM:
        return JSONResponse({"ok": False, "error": "服务还没准备好"}, status_code=409)
    vcfg = voice.VoiceConfig.from_env(ROOM.cfg)
    if not vcfg.ready:
        return JSONResponse({"ok": False, "error": "语音服务还没配好（缺 VOICE_BASE_URL / VOICE_API_KEY）"},
                            status_code=503)

    limit = 25 * 1024 * 1024
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        return JSONResponse({"ok": False, "error": "录音太大了"}, status_code=413)

    chunks: list[bytes] = []
    total = 0
    # ⚠️ 2026-10-08 修（P1：手机语音识别 45 秒超时）：
    #    手机端上传超时后 **abort**，连接就这么断了，而 `request.stream()` 在读的过程中
    #    会抛 `starlette.requests.ClientDisconnect`。以前这个异常没人接 —— 它一路冒到
    #    ASGI 顶层，服务端打一整页 traceback（`_logs/srv8077.err.log` 里实测有），
    #    客户端什么也收不到，看起来就是"识别永远不回来 / 服务端 500"。
    #    现在：这是"客户端提前断开"，属于**可预期**的一种结果，回一条明确的话就行。
    #    （499 是 nginx 对"客户端主动断开"的约定码；连接已经没了的话这条响应发不出去，
    #      但至少服务端自己不再把它当崩溃。）
    try:
        async for chunk in request.stream():    # 分块读：超限就立刻中断，不把整个包吞进内存
            total += len(chunk)
            if total > limit:
                return JSONResponse({"ok": False, "error": "录音太大了"}, status_code=413)
            chunks.append(chunk)
    except ClientDisconnect:
        print(f"[asr] 上传中断：客户端提前断开（已收到 {total} 字节，未识别）")
        return JSONResponse({"ok": False, "error": "上传中断（客户端提前断开）"}, status_code=499)
    except asyncio.CancelledError:
        # 服务端这一次请求被取消（关窗口 / 客户端 abort 引发的取消）。
        # ⚠️ 这里**不重新抛**：`uvicorn` 是 `except BaseException` 一把抓，
        #    重抛等于把这一整页 traceback 又打回 stderr（P1 要修的就是这个观感）。
        #    对客户端而言这就是"这次上传没成"，和上面那条断开是同一种结果。
        print(f"[asr] 上传被取消（已收到 {total} 字节，未识别）")
        return JSONResponse({"ok": False, "error": "上传中断（请求被取消）"}, status_code=499)
    except OSError as exc:
        print(f"[asr] 上传中断：{type(exc).__name__}: {exc}（已收到 {total} 字节）")
        return JSONResponse({"ok": False, "error": f"上传中断（{type(exc).__name__}）"},
                            status_code=499)
    data = b"".join(chunks)
    if not data:
        return JSONResponse({"ok": False, "error": "没收到音频"}, status_code=400)
    if len(data) > limit:                       # 兜底（正常到不了这里）
        return JSONResponse({"ok": False, "error": "录音太大了"}, status_code=413)
    try:
        text = await voice.asr(data, request.headers.get("content-type", ""), vcfg,
                               request.query_params.get("format", ""))
    except voice.VoiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    legal: list[int] = []
    for piece in (request.query_params.get("legal") or "").split(","):
        piece = piece.strip()
        if piece.isdigit():
            legal.append(int(piece))
    return JSONResponse({"ok": True, "text": text,
                         "seat": voice.parse_seat(text, legal) if legal else None})


def _game_name_problem(name: str) -> str | None:
    """存档名合法性：返回 None = 通过，否则是给用户看的一句话。

    ⚠️ 以前这段是抄在 `/api/game/{name}` 里的两行（`"/" in name or "\\" in name or not
    name.endswith(".json")`）。2026-10-08 加了 `/api/game/{name}/call/{i}` 与 `?lite=1`
    两条口径 —— 三处各抄一份的话，早晚会有一处忘了挡目录穿越，
    所以**统一走这一个函数**（挡住 `/`、`\\`、`..`，且要求 `.json` 结尾；
    和 `/api/saved_audio` 的 `SAFE_NAME_RE` 是同一个意图）。

    ⚠️ 2026-10-08 补（审查发现的小漏洞）：还要挡住 **Windows 盘符相对路径**。
    `os.path.join(save_dir, "D:evil.json")` 在 Windows 上会**把前半段丢掉**、
    变成 `D:evil.json`（锚在 D 盘那个进程的 CWD）—— 也就是能读到存档目录**外面**的同名
    `.json`。影响不大（只读、要配对码、还得同名文件存在），但确实是漏点。
    判据用 `name == os.path.basename(name)` + 没有盘符：白名单比黑名单可靠。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return "存档名不合法"
    if not name.endswith(".json"):
        return "存档名不合法"
    if os.path.splitdrive(name)[0]:
        return "存档名不合法（不能带盘符）"
    if name != os.path.basename(name):
        return "存档名不合法"
    return None


def _game_save_path(name: str, mock: bool = False) -> str:
    base = resolve_save_dir()
    if mock:
        base = _mock_save_dir(base)
    return os.path.join(base, name)


def _mock_requested(request: Request) -> bool:
    """显式读 mock 存档：`?mock=1`。默认 false，普通复盘列表/真局行为不变。"""
    return str(request.query_params.get("mock") or "").strip().lower() in ("1", "true", "yes", "on")


def _read_save(name: str, mock: bool = False) -> dict | None:
    """读一份存档（读不到 / 坏了 → None）。调用方自己决定回什么码。"""
    try:
        with open(_game_save_path(name, mock), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reply_summary(reply) -> str:
    """把一条回复压成**一行**摘要（复盘列表上显示的那句话）。

    只挑"人一眼能看懂这一手干了什么"的那个字段：发言 → 票 → 目标 → 兜底。
    完整内容仍然在存档里，点开那一条（`/api/game/<name>/call/<i>`）才取。
    """
    if not isinstance(reply, dict):
        return str(reply or "")[:80]
    speech = str(reply.get("speech") or "").strip()
    if speech:
        return "发言：" + speech.replace("\n", " ")[:80]
    if reply.get("vote") not in (None, ""):
        return f"投 {reply['vote']}号"
    if reply.get("target") not in (None, ""):
        return f"目标 {reply['target']}号"
    try:
        return json.dumps(reply, ensure_ascii=False)[:80]
    except (TypeError, ValueError):
        return str(reply)[:80]


#: `lite=1` 里每条调用留痕保留的字段：都是"列表上要显示的小东西"（数字/短串），
#: **一个字节的 reasoning/raw/packet 都不带**（那是 2.9MB 的全部来源）。
_LITE_CALL_KEYS = ("seat", "day", "phase", "task", "model", "elapsed",
                   "prompt_tokens", "completion_tokens", "cached_tokens", "error")


def _lite_game(data: dict) -> dict:
    """把一份存档压成复盘首屏需要的样子（`/api/game/<name>?lite=1`）。

    为什么要有（2026-10-08 修 P2）：`/api/game/<name>` 原样吐 2.9MB，手机上"复盘"要等
    十几秒 —— 而首屏真正要显示的只有"这一局的发言"和一个调用留痕列表。
    实测这份存档：全文 3.09MB，其中 `transcript` 2.98MB（reasoning 平均 4744 字、
    最长 44360 字），压完之后 **37KB**。

    保留什么：
      · 元信息（板子/天数/结果/语音条数）；
      · `ledger` 里 **kind=speech** 的行（day/seat/text/audio）—— 复盘要列出这一局的发言、
        并且给每条挂一个 🔊（`audio` 为真才有）；
      · `timeline`：全量台账的**轻量**版（动作/summary/投票 data），给票型矩阵与流程图用；
      · `transcript` 每条只留 `_LITE_CALL_KEYS` + 一行回复摘要 + `reasoning_len`（长度，不是内容！）
        + `i`（它在原始 transcript 里的下标 —— 点开那一条就靠它去取全文）。
    另外标一个 `lite: true`：前端据此知道"这是轻量口径、全文要按需再取"。
    """
    rows: list[dict] = []
    for i, row in enumerate(data.get("transcript") or []):
        if not isinstance(row, dict):
            continue
        item = {k: row.get(k) for k in _LITE_CALL_KEYS}
        item["i"] = i
        item["reply_summary"] = _reply_summary(row.get("reply"))
        item["reasoning_len"] = len(str(row.get("reasoning") or ""))
        rows.append(item)
    speech: list[dict] = []
    timeline: list[dict] = []
    for n, entry in enumerate(data.get("ledger") or []):
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        compact = {"n": n, "kind": kind, "day": entry.get("day"),
                   "phase": entry.get("phase"), "seat": entry.get("seat"),
                   "text": entry.get("text"),
                   "action": entry.get("action") or kind,
                   "summary": entry.get("summary") or "",
                   "audio": bool(entry.get("audio"))}
        if kind == "vote":
            raw = entry.get("data") or {}
            compact["data"] = {k: raw.get(k) for k in _ENTRY_DATA_KEYS
                               if k in raw}
        timeline.append(compact)
        if kind == "speech":
            speech.append({"n": n, "kind": "speech", "day": entry.get("day"),
                           "seat": entry.get("seat"), "text": entry.get("text"),
                           "audio": bool(entry.get("audio"))})
    compact_seats = []
    for s in (data.get("seats") or []):
        if not isinstance(s, dict):
            continue
        compact_seats.append({k: s.get(k) for k in
                              ("id", "role", "is_human", "alive", "death_day", "death_cause")
                              if k in s})
    return {
        "lite": True,
        "name": None,                       # 调用方补（存档名不在文件里）
        "board": data.get("board"),
        "day": data.get("day"),
        "phase": data.get("phase"),
        "winner": data.get("winner"),
        "aborted": data.get("aborted"),
        "started_at": data.get("started_at"),
        "audio_count": data.get("audio_count"),
        "call_count": len(data.get("transcript") or []),
        "sheriff_id": data.get("sheriff_id"),
        "seats": compact_seats,
        "ledger": speech,
        "timeline": timeline,
        "transcript": rows,
    }


# ⚠️ 注册顺序要紧：这一条必须**排在** `/api/game/{name}` 前面。
#    `{name}` 只吃一个路径段，`/api/game/x.json/call/3` 本来也匹配不上它，
#    但把更具体的路由放前面是 FastAPI/Starlette 的通用纪律（少一层"到底命中哪个"的猜疑）。
@app.get("/api/game/{name}/call/{index}")
async def api_game_call(name: str, index: str, request: Request):
    """取某一条调用留痕的**全文**（reasoning + raw + reply + packet）—— 复盘点开一条才拉。

    为什么要单独一条路由（2026-10-08 修 P2）：一局 66 条的 reasoning 加起来 2.9MB，
    全量传给手机要十几秒，而用户一次只会看其中一两条。所以列表走 `?lite=1`（37KB），
    点开某一条才按需把这**一条**的全文取回来（几十 KB 级）。

    参数 `index` 故意声明成 `str` 再自己转 int：写成 `int` 时非法值会被 FastAPI 先拦成 422
    `{"detail": ...}`，而前端只读 `.error` —— 手机上会变成"点了没反应"（`/api/tts` 踩过同一个坑）。

    闸门与 `/api/game/{name}` 完全一致：Host 不认 403、缺码 401、档名非法 400、
    序号越界 400（**不是** 404：那是"请求参数不对"，说成"没有这条"会把排查方向带偏）、
    存档不在 404。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    bad = _game_name_problem(name)
    if bad:
        return JSONResponse({"error": bad}, status_code=400)
    try:
        idx = int(str(index).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": f"序号必须是整数（收到 {index}）"}, status_code=400)
    mock = _mock_requested(request)
    data = _read_save(name, mock)
    if data is None:
        if not os.path.exists(_game_save_path(name, mock)):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"error": "存档读不出来（文件坏了或不是 JSON）"}, status_code=500)
    rows = data.get("transcript") or []
    if idx < 0 or idx >= len(rows):
        return JSONResponse({"error": f"序号超出这一局的调用条数（共 {len(rows)} 条，"
                                      f"序号 0~{len(rows) - 1}）"}, status_code=400)
    row = rows[idx] if isinstance(rows[idx], dict) else {}
    return JSONResponse({
        "name": name, "i": idx, "total": len(rows),
        "seat": row.get("seat"), "day": row.get("day"), "phase": row.get("phase"),
        "task": row.get("task"), "role": row.get("role"), "model": row.get("model"),
        "elapsed": row.get("elapsed"), "error": row.get("error"),
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "cached_tokens": row.get("cached_tokens"),
        "thinking": row.get("thinking"), "fallback": row.get("fallback"),
        # 这三样是"全文"，也正是 lite 口径里被剥掉的东西
        "reasoning": row.get("reasoning") or "",
        "raw": row.get("raw") or "",
        "reply": row.get("reply"),
        "packet": row.get("packet") or "",
    })


@app.get("/api/game/{name}")
async def api_game(name: str, request: Request):
    """读一份存档。默认**完全不变**（原样返回整份 JSON）；`?lite=1` 走轻量口径。

    `lite=1` 只给复盘首屏要的东西（见 `_lite_game`）：实测 3.09MB → 37KB。
    默认不带参数时行为与以前一模一样 —— 别的脚本/工具（`tools/`、测试）都靠它拿全文。
    """
    if not host_ok(request):
        return JSONResponse({"error": "不认识的 Host"}, status_code=403)
    problem = read_token_error(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=401)
    bad = _game_name_problem(name)
    if bad:
        return JSONResponse({"error": bad}, status_code=400)
    mock = _mock_requested(request)
    path = _game_save_path(name, mock)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    if str(request.query_params.get("lite") or "").strip().lower() in ("1", "true", "yes", "on"):
        data = _read_save(name, mock)
        if data is None:
            return JSONResponse({"error": "存档读不出来（文件坏了或不是 JSON）"}, status_code=500)
        out = _lite_game(data)
        out["name"] = name
        return JSONResponse(out)
    # ⚠️ 2026-10-25 修：默认分支原来裸 `json.load` —— 一份被写坏的存档（掉电 / 磁盘满）
    #    会直接抛未捕获异常变成 500 + 一整页栈。现在与 `lite` 分支走同一个 `_read_save` 兜底。
    data = _read_save(name, mock)
    if data is None:
        return JSONResponse({"error": "存档读不出来（文件坏了或不是 JSON）"}, status_code=500)
    return JSONResponse(data)


# --------------------------------------------------------------------------- #
# 页面设置：API 平台 / 座位分配 / 语音（2026-10-31 加）
#
# 为什么单独一套，而不是继续用 /api/settings/env：
#   · `/api/settings/env` 只许写 `<档案>_BASE_URL / _API_KEY / _MODEL`，写不了座位路由；
#   · 座位路由要靠 `WOLF_SEAT_PROFILES`，它不在白名单里（这是有意的：防任意变量注入）；
#   · 开源之后新用户要在页面上「填一个 API → 把 1~12 号都指过去 → 开打」，
#     不能要求他去改 config.yaml 再重启。
# 所以数据落到 `settings.local.json`（gitignore 挡着），启动时覆盖到 os.environ。
# --------------------------------------------------------------------------- #

_ENV_PROFILE_SKIP = {"WOLF", "VOICE", "TTS", "ASR"}


def _local_settings() -> dict:
    return settings_store.load(HERE)


def _settings_denied(request: Request, body: dict | None = None):
    """新设置接口共用的两道闸：客户端标识 + 配对码。返回 JSONResponse 或 None。

    ⚠️ 配对码允许放在 `?token=` 里：新页面把 token 统一拼在查询串上（跟 GET 一致），
       而 `token_error()` 只认 body.token。不补这一步的话，页面上每次保存都会回
       「还没填配对码」—— 用户会以为设置坏了。
    """
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识（只接受本页面发来的请求）"},
                            status_code=403)
    payload = dict(body) if isinstance(body, dict) else {}
    if not payload.get("token"):
        payload["token"] = request.query_params.get("token", "")
    problem = token_error(request, payload)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    return None


def _api_rows(data: dict) -> list[dict]:
    """本地 API + `.env` 里的档案，合成一份给页面看的列表。"""
    rows = list(settings_store.public(data)["apis"])
    have = {row["id"] for row in rows}
    for prof in env_profiles():
        name = prof["profile"].upper()
        if name in have or name in _ENV_PROFILE_SKIP:
            continue
        rows.append({
            "id": name, "name": name,
            "base_url": prof.get("base_url") or "",
            "model": prof.get("model") or "",
            "has_key": bool(prof.get("has_key")),
            "key_tail": prof.get("key_tail") or "",
            "style": "",
            # .env 来的档案在页面上还没被单独调过参数：给个空对象，
            # 免得前端拿到 null 还要各自兜（2026-10-31 模型参数页）
            "tuning": {},
            "source": "env",
        })
    rows.sort(key=lambda r: (r["source"] != "local", r["id"]))
    return rows


def _settings_payload(data: dict) -> dict:
    pub = settings_store.public(data)
    return {
        "ok": True,
        "apis": _api_rows(data),
        "seats": pub["seats"],
        "voice": pub["voice"],
        "director": bool(data.get("director")),
        "butler": pub.get("butler") or {},
        "persona": pub.get("persona") or {"enabled": False, "seats": {}},
        "persona_max_total": settings_store.MAX_PERSONA_TOTAL,
        "persona_limits": dict(settings_store.PERSONA_FIELDS),
        "token_required": True,
        # ⚠️ 2026-10-31 外部审查 S13：以前这里是**绝对路径**（含中文用户名目录），
        #    任何一次 GET 都能拿到；页面根本不用这个字段。现在只回文件名。
        "store": os.path.basename(settings_store.path(HERE)),
    }


async def _read_json_body(request: Request):
    try:
        body = await request.json()
    except ClientDisconnect:
        # ⚠️ 2026-10-31 外部审查 S8：客户端中途断开是**正常的中断信号**，不是"请求格式不对"。
        #    以前被 `except Exception` 一起吞成 None → 路由回 400，日志里查不出真实原因。
        raise
    except Exception:                          # noqa: BLE001 —— 真·坏 JSON 才走这里
        return None
    return body if isinstance(body, dict) else None


def _apply_local_settings(data: dict, note: str = "", clear_missing: bool = False) -> None:
    """存盘之后立刻生效：写环境变量 + config 里的思考风格，再广播一次状态。

    `clear_missing=True` 只给"用户保存设置"这条路口用：页面里空着的字段要把旧环境变量删掉
    （启动时不能传 —— 那一刻"空"只表示页面没配，删掉会把 `.env` 里配好的东西抹掉）。
    """
    settings_store.apply(ROOM.cfg if ROOM else None, data, clear_missing=clear_missing)
    if ROOM is not None:
        if note:
            ROOM.broadcast({"type": "notice", "text": note})
        ROOM.broadcast(ROOM.status())


@app.post("/api/settings/local/director")
async def api_settings_director(request: Request):
    """导演模式开关（作弊）。默认关；开了之后对局中点座位才能看身份。

    只写 settings.local.json（不进 config.yaml、不进发布包）。
    """
    denied = _settings_denied(request)
    if denied is not None:
        return denied
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    data = _local_settings()
    raw_on = body.get("on")
    if isinstance(raw_on, str):
        on = raw_on.strip().lower() in ("1", "true", "yes", "on")
    else:
        on = bool(raw_on)
    data["director"] = on
    try:
        settings_store.save(HERE, data)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"存设置失败：{exc}"}, status_code=500)
    if ROOM is not None:
        ROOM.broadcast(ROOM.status())
    return JSONResponse(_settings_payload(data))


def _persona_overrides() -> tuple[dict[int, "persona.Persona"], list[str]]:
    """读 settings.local.json 的「人设模块」；关着就返回空。

    ⚠️ 人设只进**该座位**的提示包（context.build 读 seat.persona），不是全局。
    ⚠️ 保存时已经用 render_checked 挡过身份词；这里再挡一次，防止手改 settings.local.json。
    """
    block = (_local_settings().get("persona") or {})
    if not block.get("enabled"):
        return {}, []
    out: dict[int, "persona.Persona"] = {}
    warnings: list[str] = []
    for key, fields in (block.get("seats") or {}).items():
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        if not 1 <= sid <= settings_store.MAX_SEATS:
            continue
        p = persona.from_dict(sid, fields)
        if p.is_empty():
            continue
        errors = persona.lint_persona(p)
        if errors:
            warnings.append(f"{sid}号人设含身份词，本局跳过：{'；'.join(errors)}")
            continue
        out[sid] = p
    return out, warnings


@app.post("/api/settings/local/persona")
async def api_settings_persona(request: Request):
    """人设模块开关 + 每座位自定义。默认关；开了之后**下一局**生效，只影响该座位。"""
    denied = _settings_denied(request)
    if denied is not None:
        return denied
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    raw = body.get("persona") if isinstance(body.get("persona"), dict) else body
    block = settings_store.normalize_persona(raw)
    errors = []
    for key, fields in block["seats"].items():
        _text, errs = persona.render_checked(int(key), fields)
        if errs:
            errors.append({"seat": int(key), "errors": errs})
    if errors:
        return JSONResponse({"ok": False,
                             "error": "有人设写了身份/角色词（会破坏信息隔离），请改掉再保存",
                             "details": errors}, status_code=400)
    data = _local_settings()
    data["persona"] = block
    try:
        settings_store.save(HERE, data)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"存设置失败：{exc}"}, status_code=500)
    if ROOM is not None:
        ROOM.broadcast({"type": "notice", "text": "人设设置已保存，下一局生效"})
    return JSONResponse(_settings_payload(data))


@app.get("/api/settings/local")
async def api_settings_local(request: Request):
    """页面设置的首屏数据：API 列表 / 座位映射 / 语音（key 只回后四位）。"""
    denied = _settings_denied(request)
    if denied is not None:
        return denied
    return JSONResponse(_settings_payload(_local_settings()))


@app.post("/api/settings/local/apis")
async def api_settings_api_save(request: Request):
    """新增/修改一条 API。key 留空 = 保留原来的（页面不回显 key）。"""
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    denied = _settings_denied(request, body)
    if denied is not None:
        return denied
    data = _local_settings()
    try:
        pid = settings_store.normalize_id(body.get("id") or body.get("name"))
        data = settings_store.upsert_api(data, body)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    api = next((item for item in data["apis"] if item.get("id") == pid), {})
    if not api:
        # 2026-10-31 外部审查 S10：以前这里会掉进下面那句"地址和模型名都要填"，
        # 而真实原因是"按规范化后的 id 找不到这一条"，用户改半天也改不出来。
        return JSONResponse({"ok": False,
                             "error": f"找不到刚保存的那条 API（规范化后的名字是 {pid!r}），"
                                      f"换个只用字母数字下划线点横线的名字再试"},
                            status_code=400)
    if not api.get("base_url") or not api.get("model"):
        return JSONResponse({"ok": False, "error": "接口地址和模型名都要填"}, status_code=400)
    settings_store.save(HERE, data)
    _apply_local_settings(data, f"已保存 API「{api.get('id')}」，下一局生效", clear_missing=True)
    print(f"[settings] 保存 API {api.get('id')}（key "
          f"{'已填' if api.get('key') or os.environ.get(str(api.get('id')) + '_API_KEY') else '未填'}）")
    return JSONResponse(_settings_payload(data))


def _config_profile_refs(profile: str) -> list[str]:
    """看当前启动配置是否还把这个档案当默认/座位/兜底用。

    `.env` 和 `settings.local.json` 删了，但 config 里写死的 profile 不会自动改；
    提前给页面一句警告，避免删完才发现某个座位没有 key。
    """
    if ROOM is None or not isinstance(ROOM.cfg, dict):
        return []
    cfg = ROOM.cfg
    llm = cfg.get("llm") or {}
    refs: list[str] = []
    default = llm.get("default")
    if isinstance(default, dict) and str(default.get("profile") or "") == profile:
        refs.append("llm.default")
    for role, block in (llm.get("roles") or {}).items():
        if isinstance(block, dict) and str(block.get("profile") or "") == profile:
            refs.append(f"llm.roles.{role}")
    for seat, block in (llm.get("seats") or {}).items():
        if isinstance(block, dict) and str(block.get("profile") or "") == profile:
            refs.append(f"llm.seats.{seat}")
    game = cfg.get("game")
    if isinstance(game, dict) and profile in [str(x) for x in game.get("fallback") or []]:
        refs.append("game.fallback")
    return refs


@app.post("/api/settings/local/apis/delete")
async def api_settings_api_delete(request: Request):
    """删一条 API：页面存的删页面；`.env` 里的也一起删（页面上的删除按钮对两种来源一视同仁）。"""
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    denied = _settings_denied(request, body)
    if denied is not None:
        return denied
    data = _local_settings()
    try:
        pid = settings_store.normalize_id(body.get("id"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    local_exists = any(a["id"] == pid for a in data["apis"])
    base_name, _, _ = profile_vars(pid)
    env_prefix = base_name[:-len("_BASE_URL")] if base_name.endswith("_BASE_URL") else ""
    env_names = {f"{env_prefix}{suffix}" for suffix in SUFFIXES} if env_prefix else set()
    env_exists = bool(env_names) and any(e["name"].upper() in env_names for e in env_entries())
    if not local_exists and not env_exists:
        return JSONResponse({"ok": False, "error": f"找不到 API「{pid}」"}, status_code=400)
    if env_prefix in _ENV_PROFILE_SKIP:
        return JSONResponse({"ok": False, "error": f"{env_prefix} 是保留档案，不允许从页面删除"},
                            status_code=400)

    if local_exists:
        data = settings_store.delete_api(data, pid)
        settings_store.save(HERE, data)
    if env_exists:
        # `.env` 里可能有同名的三/四行；一起删，否则页面删完、下次重启又冒出来。
        update_env_file({}, remove=env_names)
        # load_env 只覆盖、不清旧键；删完必须再 pop 一次，进程内才真的没有旧 key。
        load_env(ENV_PATH, force=True)
        for name in env_names:
            os.environ.pop(name, None)
    settings_store.forget(pid)
    refs = _config_profile_refs(pid)
    _apply_local_settings(data, f"已删除 API「{pid}」（含 .env 档案）", clear_missing=True)
    payload = _settings_payload(data)
    if refs:
        payload["warning"] = (f"API「{pid}」已删，但启动配置还在引用它：{'、'.join(refs)}。"
                              "请改配置后重启，否则这些座位会因缺 key 失败。")
    return JSONResponse(payload)


@app.post("/api/settings/local/seats")
async def api_settings_seats(request: Request):
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    denied = _settings_denied(request, body)
    if denied is not None:
        return denied
    data = _local_settings()
    try:
        data = settings_store.set_seats(data, body.get("seats"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    settings_store.save(HERE, data)
    _apply_local_settings(data, f"已保存座位分配（{len(data['seats'])} 个座位），下一局生效", clear_missing=True)
    return JSONResponse(_settings_payload(data))


@app.post("/api/settings/local/butler")
async def api_settings_butler(request: Request):
    """保存「托管模型」：选一条已配置的 API，玩家点托管时用它替玩家出一手。"""
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    body = await _read_json_body(request)
    if body is None:                       # 坏 JSON 不能当空体：那会把已存的托管设置清空
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    problem = token_error(request, body)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    if ROOM is not None and getattr(ROOM, "demo_mode", False):
        return JSONResponse({"ok": False,
                             "error": "这是公共体验实例，不允许改服务端设置（你的 key 请填在首页，只存你自己的浏览器）。"},
                            status_code=403)
    data = settings_store.load(HERE)
    # 只覆盖"请求里真的带了的字段"：页面只发 api/thinking 时，温度/上限不该被清空
    butler = dict(data.get("butler") or {})
    for key in ("api", "thinking", "temperature", "max_tokens"):
        if key in body:
            butler[key] = body.get(key)
    data["butler"] = butler
    try:
        settings_store.save(HERE, data)
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"保存失败：{exc}"}, status_code=500)
    settings_store.apply(ROOM.cfg if ROOM else None, data, clear_missing=True)
    saved = settings_store.load(HERE)["butler"]
    return JSONResponse({"ok": True, "butler": saved})


@app.post("/api/settings/local/voice")
async def api_settings_voice(request: Request):
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    denied = _settings_denied(request, body)
    if denied is not None:
        return denied
    data = _local_settings()
    try:
        data = settings_store.set_voice(data, body)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    settings_store.save(HERE, data)
    _apply_local_settings(data, "已保存语音配置", clear_missing=True)
    if ROOM is not None:
        ROOM.voice = voice.VoiceConfig.from_env(ROOM.cfg)
    return JSONResponse(_settings_payload(data))


@app.get("/healthz")
async def healthz():
    """平台健康检查专用（2026-11-02）：**不校验配对码、不要客户端头**。

    为什么单开一个：Render 这类平台的健康检查是**裸 GET**，不带 `X-Wolf-Client`，
    指向 `/api/status` 会被 CSRF 闸门 403 ⇒ 平台判定"服务不健康"、整次部署失败。
    这里只回一个"活着"，不泄露任何配置。
    """
    return JSONResponse({"ok": True})


@app.get("/api/site")
async def api_site():
    """开源仓库 / 交流群等公开信息（`web/site.json`）。

    ⚠️ 这个接口**故意不要配对码**：它只返回作者愿意公开的链接，
       首页在还没填码的时候也要能显示。文件不存在就回默认值。
    """
    data = {"repo": "", "qq_group": "", "qq_link": "", "author": "", "version": "1.0.0",
            "afdian": "", "pans": []}
    path = os.path.join(WEB_DIR, "site.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for key in data:
                if key in raw and isinstance(raw[key], str):
                    data[key] = raw[key].strip()
            # 其他下载渠道：只放行"有 http(s) 地址"的条目，其余字段截断，避免把奇怪的东西带上首页
            if isinstance(raw.get("pans"), list):
                pans = []
                for item in raw["pans"][:6]:
                    if isinstance(item, dict) and isinstance(item.get("url"), str) \
                            and item["url"].strip().lower().startswith(("http://", "https://")):
                        pans.append({"name": str(item.get("name") or "下载")[:20],
                                     "url": item["url"].strip()[:500],
                                     "code": str(item.get("code") or "")[:12]})
                data["pans"] = pans
    except (OSError, ValueError):
        pass
    return JSONResponse(data)


# --------------------------------------------------------------------------- #
# 对局结果统计（2026-10-31 加）：玩家自愿把「这一局的结果摘要」交给房主
#
# ⚠️ 只收摘要（胜负 / 天数 / 座位数 / 各座位角色 / 胜方剩几人），**不碰发言与思考**；
#    存在 `var/reports/reports.jsonl`（gitignore），不进发布包。
# --------------------------------------------------------------------------- #
@app.post("/api/report")
async def api_report(request: Request):
    """玩家点「贡献」时调它。访客令牌与配对码都认（访客没有配对码）。"""
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识（只接受本页面发来的请求）"},
                            status_code=403)
    body = await _read_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    if not bool((ROOM.game_cfg if ROOM else {}).get("collect_reports", True)):
        return JSONResponse({"ok": False, "error": "房主关掉了对局统计"}, status_code=403)
    tok = str(body.get("token") or request.query_params.get("token") or "")
    is_guest = bool(tok) and ROOM is not None and tok in (getattr(ROOM, "guest_tokens", {}) or {})
    if not is_guest:
        # 配对码允许放在 ?token= 上（页面统一拼查询串；token_error 只认 body.token）
        payload = dict(body)
        payload.setdefault("token", request.query_params.get("token", ""))
        problem = token_error(request, payload)
        if problem:
            return JSONResponse({"ok": False, "error": problem}, status_code=401)
    gid = str(body.get("game_id") or "").strip()
    if not re.match(r"^game-[\w\-]+\.json$", gid):
        return JSONResponse({"ok": False, "error": "存档名不对"}, status_code=400)
    full = os.path.join(resolve_save_dir(ROOM.game_cfg if ROOM else None), gid)
    if not os.path.isfile(full):
        return JSONResponse({"ok": False, "error": "找不到这一局的存档"}, status_code=404)
    try:
        with open(full, encoding="utf-8-sig") as fh:
            save = json.load(fh)
    except (OSError, ValueError):
        return JSONResponse({"ok": False, "error": "存档读不出来"}, status_code=400)
    item = reports.summary_from_save(save, gid, "guest" if is_guest else "host")
    wrote, why = reports.append(HERE, item)
    stats = reports.aggregate(reports.load(HERE))
    print(f"[report] {gid} 收到（{item.get('reporter')}）：{'已记录' if wrote else why}")
    return JSONResponse({"ok": True, "recorded": wrote, "why": why, "stats": stats})


@app.get("/api/stats")
async def api_stats(request: Request):
    """房主看统计（要配对码）。"""
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    problem = token_error(request, {"token": request.query_params.get("token", "")})
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    items = reports.load(HERE)
    return JSONResponse({"ok": True, **reports.aggregate(items), "recent": items[-10:][::-1]})


# --------------------------------------------------------------------------- #
# 联机：加入房间（访客令牌）/ 房间快照
#
# 两把钥匙分开：
#   · 配对码（env_token）：主机用，能改设置、能开局、能放弃；
#   · 联机码（join_code）：给朋友，只能加入房间/选座位，不能改任何设置。
# 访客拿到的 guest token 只存在内存里；服务重启后要重新加入（那一局本来也断了）。
# --------------------------------------------------------------------------- #

_JOIN_FAILS: dict[str, list] = {}
#: ⚠️ 2026-10-31 外部审查 S16：这张表原来只在"同一个 IP 又来撞码"时才清自己那一条，
#:    没有 TTL 扫描、没有容量上限 —— 大量一次性来源 IP 能把字典无限撑大。
#:    现在与 `_TOKEN_FAILS` 同一口径（300 秒 TTL + 4096 上限 + 同一把锁）。
_JOIN_LOCK = threading.Lock()
_JOIN_FAIL_TTL = 300.0
_JOIN_FAIL_MAX = 4096


def _join_key(request: Request) -> str:
    return (request.client.host if request.client else "") or "?"


def _prune_join_fails(now: float) -> None:
    """清过期记录 + 给表一个硬上限。**调用方必须已经持有 `_JOIN_LOCK`。**"""
    for who, rec in list(_JOIN_FAILS.items()):
        try:
            last = float(rec[1])
        except (TypeError, ValueError, IndexError, KeyError):
            _JOIN_FAILS.pop(who, None)
            continue
        if now - last > _JOIN_FAIL_TTL:
            _JOIN_FAILS.pop(who, None)
    if len(_JOIN_FAILS) > _JOIN_FAIL_MAX:
        newest = sorted(list(_JOIN_FAILS.items()), key=lambda kv: kv[1][1], reverse=True)
        _JOIN_FAILS.clear()
        _JOIN_FAILS.update(dict(newest[: _JOIN_FAIL_MAX // 2]))


def _join_limited(request: Request) -> str | None:
    key = _join_key(request)
    now = time.time()
    with _JOIN_LOCK:
        _prune_join_fails(now)
        rec = _JOIN_FAILS.get(key)
        if not rec:
            return None
        fails, last = int(rec[0]), float(rec[1])
        if now - last > 120:
            _JOIN_FAILS.pop(key, None)
            return None
        if fails >= 8 and now - last < 30:
            return "联机码试太多次了，等 30 秒再试"
        return None


def _join_fail(request: Request) -> None:
    key = _join_key(request)
    now = time.time()
    with _JOIN_LOCK:
        rec = _JOIN_FAILS.get(key) or [0, 0.0]
        _JOIN_FAILS[key] = [int(rec[0]) + 1, now]


def _viewer_of(request: Request, body: dict | None = None):
    """认访客/主机身份；返回 (role, pid) 或 None。"""
    if ROOM is None:
        return None
    token = ""
    if isinstance(body, dict):
        token = str(body.get("token") or "")
    token = token or str(request.query_params.get("token") or "")
    return ROOM.identify(token)


@app.post("/api/join")
async def api_join(request: Request):
    """用联机码 + 名字加入房间，换一张访客令牌。"""
    if ROOM is None:
        return JSONResponse({"ok": False, "error": "服务还没准备好"}, status_code=503)
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    limited = _join_limited(request)
    if limited:
        return JSONResponse({"ok": False, "error": limited}, status_code=429)
    try:
        body = await request.json()
    except Exception:                          # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    code = str(body.get("code") or "").strip().upper()
    name = str(body.get("name") or "").strip()
    name = "".join(ch for ch in name if ch.isprintable())[:12]
    if code != ROOM.join_code:
        _join_fail(request)
        return JSONResponse({"ok": False, "error": "联机码不对（问一下主机）"}, status_code=401)
    if ROOM.running or ROOM.cleaning:
        return JSONResponse({"ok": False, "error": "这一局已经开始了，等下一局再加入"}, status_code=409)
    if len([p for p in ROOM.players.values() if not p.get("host")]) >= 11:
        return JSONResponse({"ok": False, "error": "房间满了（最多 11 位访客 + 主机）"}, status_code=409)
    if not name:
        name = f"玩家{len(ROOM.players)}"
    pid = "g-" + secrets.token_hex(4)
    token = secrets.token_urlsafe(24)
    ROOM.guest_tokens[token] = pid
    ROOM.players[pid] = {"pid": pid, "name": name, "seat": None,
                         "online": False, "host": False}
    ROOM.multiplayer = True
    ROOM.broadcast_room()
    print(f"[room] 新访客「{name}」加入（{pid}）")
    return JSONResponse({"ok": True, "pid": pid, "token": token, "name": name,
                         "join_code": ROOM.join_code})


@app.get("/api/room")
async def api_room(request: Request):
    """房间大厅快照。主机/访客都能看；联机码只回给主机。"""
    if ROOM is None:
        return JSONResponse({"ok": False, "error": "服务还没准备好"}, status_code=503)
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    ident = _viewer_of(request)
    if ident is None:
        return JSONResponse({"ok": False, "error": "需要配对码或有效的访客令牌"}, status_code=401)
    role, pid = ident
    return JSONResponse({"ok": True, "role": role,
                         "room": ROOM.room_public(pid, role == "host")})


@app.post("/api/room/rotate")
async def api_room_rotate(request: Request):
    """主机换一个联机码（旧码立即失效）。"""
    if ROOM is None:
        return JSONResponse({"ok": False, "error": "服务还没准备好"}, status_code=503)
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:                      # noqa: BLE001 —— 空 body/坏 JSON 当没带
            body = {}
    ident = _viewer_of(request, body if isinstance(body, dict) else None)
    if ident is None or ident[0] != "host":
        return JSONResponse({"ok": False, "error": "只有主机能换联机码"}, status_code=403)
    code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
    while code == ROOM.env_token:
        code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
    ROOM.join_code = code
    try:
        with open(os.path.join(_data_home(), "联机码.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write(JOIN_CODE_TEMPLATE.format(code=code))
    except OSError:
        pass
    ROOM.broadcast_room()
    return JSONResponse({"ok": True, "join_code": code})


@app.get("/api/settings/env")
async def api_env_get(request: Request):
    """看 .env 里已经填了哪些平台（key 只回显后四位）。要配对码。"""
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识"}, status_code=403)
    body = {"token": request.query_params.get("token", "")}
    problem = token_error(request, body)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    return JSONResponse({
        "path": ENV_PATH,
        "entries": env_entries(),
        "profiles": env_profiles(),
        "token_required": needs_token(request),
    })


@app.post("/api/settings/env")
async def api_env_post(request: Request):
    """把 Base URL / Key / 模型名写进 .env。值不回显、不写日志。"""
    if ROOM is None:
        return JSONResponse({"ok": False, "error": "服务还没准备好"}, status_code=503)
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识（只接受本页面发来的请求）"},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:                          # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    problem = token_error(request, body)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)
    updates: dict[str, str] = {}
    raw_updates = body.get("updates")
    if not isinstance(raw_updates, dict):
        return JSONResponse({"ok": False, "error": "updates 必须是对象"}, status_code=400)
    for name, value in raw_updates.items():
        name = str(name).strip().upper()
        if not ENV_NAME_RE.match(name):
            return JSONResponse({"ok": False, "error": f"不允许写这个变量名：{name}"}, status_code=400)
        if any(ch in str(value) for ch in BAD_VALUE_CHARS):
            return JSONResponse({"ok": False, "error": f"{name} 的值里不能有换行"}, status_code=400)
        if len(str(value)) > MAX_ENV_VALUE:
            return JSONResponse({"ok": False,
                                 "error": f"{name} 的值太长了（{len(str(value))} 字符，"
                                          f"上限 {MAX_ENV_VALUE}）"}, status_code=400)
        updates[name] = str(value).strip()
    updates = {k: v for k, v in updates.items() if v}
    if not updates:
        return JSONResponse({"ok": False, "error": "没有要保存的内容（空值不会覆盖已填的）"},
                            status_code=400)

    try:
        result = update_env_file(updates)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    load_env(ENV_PATH, force=True)          # 立刻生效，不用重启
    print(f"[settings] 已写入 .env：{', '.join(sorted(updates))}（值不回显）")
    ROOM.broadcast({"type": "notice", "text": f"已保存 {len(updates)} 项配置，不用重启服务"})
    ROOM.broadcast(ROOM.status())
    return JSONResponse({"ok": True, **result,
                         "profiles": env_profiles(), "entries": env_entries()})


PROBE_PACKET = ('（连通性测试）【当前任务】确认你能收到请求。\n'
                '【输出格式】只输出一个 JSON 对象：{"ok": true}')


@app.post("/api/settings/test")
async def api_settings_test(request: Request):
    """拿刚存好的配置打一发最小请求，当场告诉你通没通。"""
    if not client_ok(request):
        return JSONResponse({"ok": False, "error": "缺少客户端标识（只接受本页面发来的请求）"},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:                          # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    problem = token_error(request, body)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=401)

    base_url, key_name, model_name = profile_vars(body.get("profile"))
    base_url_value = os.environ.get(base_url, "")
    api_key = os.environ.get(key_name, "")
    model = os.environ.get(model_name, "")
    if not (base_url_value and api_key and model):
        missing = [n for n, v in ((base_url, base_url_value), (key_name, api_key),
                                  (model_name, model)) if not v]
        return JSONResponse({"ok": False, "error": "还没填全：" + "、".join(missing)}, status_code=400)

    spec = routing.SeatLLM(base_url=base_url_value, api_key=api_key, model=model, timeout=45)
    client = routing.build_client(spec, label=f"测试/{model}")
    started = time.time()
    try:
        # 自检只是问一句「活着吗」→ 走**辅助档**（低思考），别让满思考的模型
        # 在这上面白等一分钟（作者 2026-10-10：非座位的活低档就够）
        reply = await client.chat_json(PROBE_PACKET, client.extra_for("reflect", 1))
        print(f"[settings] 测试 {spec.host}/{model} 通过（{time.time() - started:.1f}s）")
        return JSONResponse({"ok": True, "host": spec.host, "model": model,
                             "elapsed": round(time.time() - started, 1),
                             "reply": str(reply)[:160],
                             "usage": client.usage})
    except Exception as exc:                    # noqa: BLE001
        text = str(exc)[:300]
        hint = ""
        if "401" in text:
            hint = "key 不对，或者 key 和平台地区不匹配"
        elif "404" in text:
            hint = "Base URL 或模型名不对（地址通常要以 /v1 结尾）"
        elif "429" in text:
            hint = "撞限流了，等一会儿或换一条 key"
        elif "timeout" in text.lower():
            hint = "超时，检查网络"
        return JSONResponse({"ok": False, "error": text, "hint": hint,
                             "host": spec.host, "model": model})
    finally:
        await client.aclose()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if ROOM is None:                    # ⚠️ 别用 assert：python -O 会把断言整条删掉
        await ws.close(code=1011)
        return
    # ⚠️ WebSocket 必须也要配对码。它是能力最大的那条通道：
    # 连上就能读到你的身份、你视角的台账、以及每次行动的完整上下文包（packet），
    # 还能替你把这一手打完、甚至直接开一局真实模型局（花你的钱）。
    # 浏览器里的任意网页都能连 ws://（WS 没有同源策略），所以不能只靠"来的是本机"。
    if not host_ok(ws):
        await ws.close(code=1008)
        return
    token = (ws.query_params.get("token") or "").strip()
    ident = ROOM.identify(token)
    if ident is None:
        await ws.close(code=1008)          # 1008 = policy violation
        return
    role, pid = ident
    queue = await ROOM.connect(ws, role, pid)
    # 记一行 UA：手机端说"不知道浏览器版本/切回来页面重跑"时，这是唯一的线索来源。
    # （只写服务窗口日志，不进任何存档，也不回传给前端）
    try:
        # ⚠️ 截断上限放到 300：**厂商标识在 UA 结尾**（MiuiBrowser / HeyTapBrowser / Quark / wv
        #    之类），原来截 160 正好把最关键的那段切掉，等于白记（诊断手机浏览器时吃过这个亏）。
        ua = (ws.headers.get("user-agent") or "?")[:300]
        cli = ws.client.host if ws.client else "?"
        print(f"[ws] 新连接 {cli}　UA：{ua}")
    except Exception:                      # noqa: BLE001
        pass
    await queue.put(ROOM.status_for(ROOM.viewer_seat(ws), host=(role == "host")))
    await queue.put({"type": "room",
                     "room": ROOM.room_public(pid, role == "host")})
    # 上面这条 status 是给"不发 sync 的客户端"兜底的；我们自己的页面连上就会发 sync，
    # 所以正常情况下你会先后看到两条 status（无害，前端只是重画一次）。
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await ROOM.handle(ws, message)
    except WebSocketDisconnect:
        ROOM.disconnect(ws)
    except Exception as exc:                         # noqa: BLE001
        # ⚠️ 2026-10-30：以前这里只断开、不打一行日志 —— handle() 里任何 bug
        #    （ValueError/AttributeError）在手机上只表现成"莫名断线"，现场无迹可查。
        print(f"[ws] 连接异常，已断开：{type(exc).__name__}: {exc}")
        ROOM.disconnect(ws)


def main() -> int:
    global ROOM
    parser = argparse.ArgumentParser(description="AI 狼人杀手机端服务")
    parser.add_argument("--host", default="0.0.0.0")
    # ⚠️ 2026-11-03 审查 P1-13：Render/Koyeb 这类平台会注入 `$PORT`，容器里写死 8077
    #    会被平台判"没有监听端口"。所以默认值改成先读环境变量，再退回 8077。
    _port_default = int(os.environ.get("PORT") or os.environ.get("WOLF_PORT") or 8077)
    parser.add_argument("--port", type=int, default=_port_default)
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--mock-default", action="store_true", help="新开局的默认模式是离线演练")
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    # 页面保存的本地设置（API / 座位 / 语音）覆盖 .env 与 config：文件不存在就等于没配。
    local = settings_store.load(HERE)
    settings_store.apply(cfg, local)
    if local["apis"] or local["seats"] or local["voice"]:
        print(f"[settings] 已加载 {settings_store.path(HERE)}："
              f"{len(local['apis'])} 个 API、{len(local['seats'])} 个座位、"
              f"语音{'已配' if local['voice'] else '未配'}")
    ROOM = Room(cfg, args.mock_default)
    if no_real_calls():
        print("[warn] WOLF_NO_REAL_CALLS=1：本进程**不会发出任何真模型请求**（测试/联调用）")

    # 配对码同时写到文件里，手机上看不到服务窗口也能拿到
    code_path = os.path.join(_data_home(), "配对码.txt")
    try:
        write_token_file(code_path, ROOM.env_token)
    except Exception:                       # noqa: BLE001
        code_path = "(配对码文件写入失败)"

    print(f"服务已起：本机 http://127.0.0.1:{args.port}   手机用 http://<这台电脑的局域网IP>:{args.port}")
    print(f"手机端填 key 的配对码：{ROOM.env_token}   （也写在 {code_path}）")
    print(f"朋友加入房间的联机码：{ROOM.join_code}   （也写在 {os.path.join(_data_home(), '联机码.txt')}）")
    # 调试/测试用的一行：机器上常常并存好几台实例，而 `配对码.txt` 是**共用**的
    # （每台启动时都会把码写回去）—— 谁最后启动，文件里就是谁的码。
    # 这一行让"到底哪台实例认哪个码"可以直接从日志里对出来，不用猜。
    print(f"[token] 本实例认的配对码 = {ROOM.env_token}（端口 {args.port}）")
    # ⚠️ 2026-11-02：云端免费档（Render free 等）**没有持久盘** —— 重启/休眠后
    #    存档与页面设置都会丢。这不是 bug，是免费档的代价；提前说清楚，别让人打完一局才发现。
    if str(os.environ.get("WOLF_EPHEMERAL") or "").strip() not in ("", "0", "false"):
        print("⚠️ 本实例运行在**临时文件系统**上（WOLF_EPHEMERAL=1）：")
        print("   · 存档、页面设置、语音缓存都**不会保留**，重启即丢；")
        print("   · 想要留档请换付费档并挂持久盘（或改用自己电脑/Termux 跑）。")
    print("（Windows 第一次会弹防火墙，点允许；Ctrl+C 停止）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
