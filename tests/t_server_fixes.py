# coding=utf-8
"""回归：服务端这一轮修的三处（服务/前端审查的 P1-1 / P2-1 / P2-2）。

1. 卡住弹窗**不能**带 `task.kind`（它等于身份：seer_check=预言家）
2. 存档目录：写和读必须锚在同一个地方（相对路径 → 项目目录）
3. Host 白名单要认本机机器名（`http://DESKTOP-XXX:8077` 是最常见的打开方式）
"""
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import server as srv                                     # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


print("=== 一、卡住弹窗不带身份信息 ===")
leaky = {"wolf_talk", "wolf_kill", "seer_check", "witch", "guard", "hunter_shoot"}
mapped = set(srv.NEUTRAL_TASK.values())
check("1a 所有会暴露身份的任务都映射成了与身份无关的名字",
      not (leaky & mapped), f"映射里出现了 {leaky & mapped}")
check("1b 夜间动作统一叫「夜间行动」",
      all(srv.NEUTRAL_TASK.get(k) == "夜间行动" for k in leaky),
      str({k: srv.NEUTRAL_TASK.get(k) for k in sorted(leaky)}))
src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("1c 源码里不再把 task.kind 直接推给手机",
      '"task": task.kind' not in src, "已换成 NEUTRAL_TASK")

print("\n=== 二、存档目录读写一致 ===")
room_cfg = {"game": {"save_dir": "games"}}
got = srv.resolve_save_dir(room_cfg) if "cfg" in srv.resolve_save_dir.__code__.co_varnames else None
# resolve_save_dir 既支持传 cfg 也支持用 ROOM 的配置
srv.ROOM = type("R", (), {"game_cfg": {"save_dir": "games"}})()
rel = srv.resolve_save_dir()
abs_dir = srv.resolve_save_dir()
check("2a 相对路径被锚到项目目录（不是进程 CWD）",
      os.path.isabs(abs_dir) and abs_dir.rstrip("\\/").endswith("games"), abs_dir)
check("2b 绝对路径原样保留",
      srv.resolve_save_dir({"game": {"save_dir": r"C:\tmp\x"}}) == r"C:\tmp\x",
      srv.resolve_save_dir({"game": {"save_dir": r"C:\tmp\x"}}))
src_api = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
check("2c /api/games 与 /api/game 都走同一个解析函数",
      src_api.count("resolve_save_dir()") >= 3, f"出现 {src_api.count('resolve_save_dir()')} 次")

print("\n=== 三、Host 白名单 ===")


class FakeReq:
    def __init__(self, host: str) -> None:
        self.headers = {"host": host}


hostname = socket.gethostname()
cases = [
    ("127.0.0.1:8077", True), ("localhost:8077", True),
    (f"{hostname}:8077", True), (f"{hostname.split('.')[0]}:8077", True),
    ("my-pc.local:8077", True),
    ("evil.example.com:8077", False), ("10.0.0.5.evil.com:8077", False),
]
for host, want in cases:
    got_ok = srv.host_ok(FakeReq(host))
    check(f"3 {host:<30} → {'放行' if want else '拒绝'}", got_ok == want,
          f"实际 {'放行' if got_ok else '拒绝'}")

bad = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for name, _, detail in bad:
    print(f"  [FAIL] {name}  {detail}")
print("=" * 74)
raise SystemExit(1 if bad else 0)
