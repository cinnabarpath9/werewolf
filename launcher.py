# coding=utf-8
"""桌面启动器：双击 exe → 起本地服务 → 自动开浏览器（itch / Steam 版的最小客户端）。

设计要点
--------
- **不依赖玩家装 Python**：PyInstaller 把解释器与依赖一起打进去；
- **数据落在 exe 旁边**（config.yaml / games/ / 配对码.txt / .env），卸载就是删文件夹；
- **端口被占自动换**（8077→8078…），并把实际地址打印在窗口里；
- **首次运行没有 key 也能玩**：服务照常起，页面里的「离线演练」不花任何 API 费用；
- **关窗口 = 退出**：控制台窗口就是"游戏的壳"，关掉它服务就停（不会留后台进程）。

这个文件只在"打包成 exe"时用；源码方式运行请继续 `python server.py`。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

TITLE = "AI 狼人杀 · 本地服务"
PORT_TRY = 20            # 8077 被占时，往后试 20 个端口


def _base_dir() -> str:
    """exe 旁边（打包后）或仓库根（源码跑）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _free_port(start: int) -> int:
    for port in range(start, start + PORT_TRY):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"{start}~{start + PORT_TRY - 1} 都被占了，请先关掉占用端口的程序")


def _wait_up(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:                                  # noqa: BLE001
            time.sleep(0.4)
    return False


def main() -> int:
    base = _base_dir()
    os.chdir(base)
    # 让 server.py 的"相对路径"都落在 exe 旁边（它本来就按 CWD 找 config/games）
    port = _free_port(int(os.environ.get("WOLF_PORT") or os.environ.get("PORT") or 8077))
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print(f"  {TITLE}")
    print(f"  地址：{url}")
    print("  关闭本窗口 = 退出游戏（不会留后台进程）")
    print("  没填 API key 也能玩：页面上选「离线演练」")
    print("=" * 60, flush=True)

    # ⚠️ 必须在 import server **之前**设：`server.py` 在模块级就会去读/生成 `配对码.txt`，
    #    晚一步设的话文件会落进 `_internal\`，用户找不到（2026-11-04 实测踩过）。
    os.environ.setdefault("WOLF_HOME", base)

    import server                                         # noqa: E402

    # ⚠️ 走 **server.main()** 而不是直接 uvicorn.run(app)：
    #    main() 里才建 Room、生成并落盘 `配对码.txt` / `联机码.txt`、加载 config 与本地设置。
    #    自己起 uvicorn 会绕过这些，结果"服务能开、配对码却没有"（2026-11-04 实测踩过）。
    # ⚠️ `server.py` 的 `--config` 默认是"**server.py 旁边**的 config.yaml"；
    #    打包后那是 `_internal\`（没有配置文件）→ 会静默回落到引擎兜底值（420/540）。
    #    必须显式指到 exe 旁边的 config.yaml（2026-11-04 实测：包内 150 却读到 420）。
    sys.argv = ["server.py", "--host", "127.0.0.1", "--port", str(port),
                "--config", os.path.join(base, "config.yaml")]
    threading.Thread(target=server.main, daemon=True, name="wolf-server").start()

    if not _wait_up(url):
        print("\n[出错] 本地服务 40 秒内没起来。请看同目录的「启动日志.txt」。")
        return 1

    try:
        webbrowser.open(url)
    except Exception as exc:                               # noqa: BLE001
        print(f"[提示] 自动开浏览器失败（{exc}），请手动打开上面的地址。")

    # 主线程挂着；控制台窗口被关掉 / Ctrl+C 时整个进程退出
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                                      # noqa: BLE001
        import traceback
        text = traceback.format_exc()
        try:
            with open(os.path.join(_base_dir(), "启动日志.txt"), "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:                                  # noqa: BLE001
            pass
        print(text)
        raise SystemExit(1)