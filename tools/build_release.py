# coding=utf-8
"""把项目打成一个「别人拿到就能跑」的干净运行包。

为什么要有它
------------
`tools/check_release.py` 只负责**检查**产物，不负责组装。手工 copy 很容易漏掉
`web/site.json`、图标，或者顺手把 `.env`、`games/`、`settings.local.json` 带进去。
这里按白名单组装，再由 `check_release.py` 复检一遍。

用法
----
    python tools/build_release.py                 # 生成 dist/werewolf-desktop-<版本>/
    python tools/build_release.py --flavor android --zip   # 安卓 Termux 包
    python tools/build_release.py --zip           # 再打一个 zip
    python tools/build_release.py --out D:\\pkg   # 指定输出目录（默认 dist/）
    python tools/build_release.py --no-check      # 跳过复检（不建议）

运行包里**会有**：server.py / run_game.py / config.yaml / wolf/ / content/ / web/ /
tools/（**只有白名单里的几个**，见 TOOLS_KEEP）/ start.bat / start.sh / README.md /
部署说明.md / LICENSE / .env.example / assets/。
**不会有**：.env / settings.local.json / 配对码.txt / 联机码.txt / games/ / var/ / docs/ / tests/ /
reports/ / .git / __pycache__ / 外部审查报告。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from release_rules import sanitize_config, assert_packaged  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
TOOLS = os.path.join(ROOT, "tools")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                   # noqa: BLE001
    pass

#: 单文件白名单
FILES = (
    "server.py",
    "run_game.py",
    "config.yaml",
    "requirements.txt",
    "README.md",
    "部署说明.md",
    "LICENSE",
    ".env.example",
    "start.bat",
    "start.sh",
)

#: 整目录白名单（目录不存在就跳过）
DIRS = ("wolf", "content", "web", "tools", "assets")

#: `tools/` 是整目录拷贝，但**不是每个工具都该发给玩家**（2026-10-31 打包清洗）：
#: 考题/基准/参数扫描那一族只在源码仓库里用，发出去既没用、还可能被人当成"官方跑分工具"。
#: 这里改成白名单：只带"玩家自己会用"的几个。
TOOLS_KEEP = (
    "README.md",          # 目录说明
    "review.py",          # 复盘（README 里就让人跑它）
    "packet_size.py",     # 量上下文包体量（排查"为什么贵/为什么慢"）
    "test_boards.py",     # 板子与技能自检（README §0 第一条命令）
    "test_routing.py",    # 座位路由自检（换模型后确认没配错）
    "voice_check.py",     # 语音连通性自检
    "build_release.py",   # 想自己再打包的人用得上
    "check_release.py",   # 打包后的复检
)

#: 打包时要**就地清洗**的内容：(相对路径, [(正则, 替换), ...])
#: 目的：发布包里不带作者本机/个人信息。源码仓库里保留原样（本机跑得通）。
SANITIZE: dict[str, list[tuple[str, str]]] = {
    "config.yaml": [
        # 作者自己的 https 提示地址（Tailscale 域名），别人装上不该看到
        (r'(?m)^public_https_base:.*$',
         'public_https_base: ""          # 留空即可；要提示条就写 .env 的 WOLF_PUBLIC_HTTPS_BASE'),
        # ⚠️ 2026-11-02（作者拍板）：**发布包不带作者的个人阵容**。
        #    阵容是作者本机怎么分座位、用哪家模型、什么思考档 —— 那是个人玩的事；
        #    发出去只留一份「模板 config」，使用者按自己的渠道自己配。
        (r'(?m)^    profile: QIANFAN_DS.*$',
         '    profile: DEEPSEEK            # 兜底平台（模板值：用 .env 里的 DEEPSEEK 档案，按你的渠道改）'),
        (r'(?ms)^  seats:.*?(?=^  # 想换成别的平台)',
         """  seats: {}
  # ↑ 发布包**不带任何阵容**：11 个座位全部跟随上面的 llm.default。
  #   想按座位分模型，在 seats 下加：`1: { profile: 你的档案名, thinking: low }`；
  #   档案名在 .env 里定义（<档案名>_BASE_URL / <档案名>_API_KEY / <档案名>_MODEL）。

"""),
    ],
}

#: 目录内永远不带的东西
IGNORE = shutil.ignore_patterns(
    # 2026-11-02：web/pyodide/ 是给**静态托管**用的运行环境（约 13MB），不进发布包；
    # 发布包里的客户端走的是自己电脑上的服务端，不需要它。
    "pyodide",
    "__pycache__", "*.pyc", "*.pyo", ".DS_Store", "Thumbs.db", "*.log",
    "personas_backups", "legacy", "外部审查", "_tmp_*", "_retired_draft.json",
    "settings.local.json", ".env", "配对码.txt", "联机码.txt",
)


def version() -> str:
    try:
        with open(os.path.join(ROOT, "web", "site.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        got = str(data.get("version") or "").strip()
        if got:
            return got
    except (OSError, ValueError):
        pass
    return "1.0.0"


def inside_dist(path: str) -> bool:
    """只允许清理 dist/ 下面的东西 —— 防止 --out 写错把用户目录删了。"""
    try:
        return os.path.commonpath([os.path.abspath(DIST), os.path.abspath(path)]) == os.path.abspath(DIST)
    except ValueError:                                  # 不同盘符
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="组装开源运行包")
    parser.add_argument("--flavor", choices=("desktop", "android"), default="desktop",
                        help="desktop=电脑/服务器包；android=安卓 Termux 手机包（默认 desktop）")
    parser.add_argument("--out", default="", help="输出目录（默认 dist/werewolf-<flavor>-<版本>）")
    parser.add_argument("--zip", action="store_true", help="同时打一个 zip")
    parser.add_argument("--no-check", action="store_true", help="跳过 check_release 复检")
    args = parser.parse_args()

    ver = version()
    flavor = args.flavor
    # 2026-11-03（作者要求）：包名带中文后缀，一眼看出该装在哪种设备上。
    #   desktop = 电脑上跑服务（Windows）→ 「电脑端」
    #   android = 手机 Termux 跑服务      → 「手机端」
    flavor_label = {"desktop": "电脑端", "android": "手机端"}[flavor]
    out = os.path.abspath(args.out or os.path.join(DIST, f"werewolf-{ver}-{flavor_label}"))
    if os.path.exists(out):
        if not inside_dist(out):
            print(f"❌ 输出目录已存在，而且不在 dist/ 下，拒绝删除：{out}")
            print("   请换一个目录，或自己手动清理。")
            return 2
        print(f"清理旧产物：{out}")
        shutil.rmtree(out)
    os.makedirs(DIST, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    print(f"== 组装运行包（{flavor}）：{out} ==")
    copied = 0
    for name in FILES:
        src = os.path.join(ROOT, name)
        if not os.path.isfile(src):
            print(f"   跳过（不存在）：{name}")
            continue
        shutil.copy2(src, os.path.join(out, name))
        copied += 1
        print(f"   + {name}")
    for name in DIRS:
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            print(f"   跳过（不存在）：{name}/")
            continue
        if name == "tools":
            # 白名单：只带玩家会用到的几个（见 TOOLS_KEEP）
            os.makedirs(os.path.join(out, "tools"), exist_ok=True)
            kept = 0
            for item in TOOLS_KEEP:
                fp = os.path.join(src, item)
                if os.path.isfile(fp):
                    shutil.copy2(fp, os.path.join(out, "tools", item))
                    kept += 1
            copied += 1
            print(f"   + tools/（白名单 {kept} 个：{', '.join(TOOLS_KEEP)}）")
            continue
        shutil.copytree(src, os.path.join(out, name), ignore=IGNORE)
        copied += 1
        print(f"   + {name}/")

    # 就地清洗：config.yaml 走结构化改写 + 断言（2026-11-03 审查 P1-10）
    cfg_path = os.path.join(out, "config.yaml")
    if os.path.isfile(cfg_path):
        raw_cfg = open(cfg_path, encoding="utf-8-sig").read()
        text, cfg = sanitize_config(raw_cfg)
        assert_packaged(cfg)                 # 阵容没清干净 → 直接非 0 退出
        open(cfg_path, "w", encoding="utf-8", newline="\n").write(text)
        print(f"   ~ 已清洗：config.yaml（seats 清空，兜底 "
              f"{(cfg.get('llm') or {}).get('default', {}).get('profile')}）")

    for rel, rules in SANITIZE.items():
        if rel == "config.yaml":
            continue
        full = os.path.join(out, rel)
        if not os.path.isfile(full):
            continue
        text = open(full, encoding="utf-8-sig").read()
        before = text
        for pattern, repl in rules:
            text = re.sub(pattern, repl, text)
        if text != before:
            open(full, "w", encoding="utf-8", newline="\n").write(text)
            print(f"   ~ 已清洗：{rel}（{len(rules)} 条规则）")

    # 手机部署说明两个包都带一份：电脑包用户也可能想把同一份代码搬到安卓上。
    termux = os.path.join(ROOT, "deploy", "termux")
    if os.path.isfile(os.path.join(termux, "README.md")):
        shutil.copy2(os.path.join(termux, "README.md"), os.path.join(out, "ANDROID-README.md"))
        os.makedirs(os.path.join(out, "deploy", "termux"), exist_ok=True)
        shutil.copy2(os.path.join(termux, "README.md"),
                     os.path.join(out, "deploy", "termux", "README.md"))
        print("   + ANDROID-README.md（手机部署）")
    for name, dst_name in (("install.sh", "ANDROID-INSTALL.sh"),
                           ("start.sh", "ANDROID-START.sh")):
        sp = os.path.join(termux, name)
        if os.path.isfile(sp):
            shutil.copy2(sp, os.path.join(out, dst_name))
            print(f"   + {dst_name}（手机部署）")

    # 必备文件自检（check_release 也会查，但先给一句更直白的话）
    missing = [p for p in ("server.py", "run_game.py", "config.yaml",
                           "web/index.html", "wolf/__init__.py", "content/01_规则书.md")
               if not os.path.exists(os.path.join(out, p))]
    if missing:
        print("❌ 运行包缺文件：" + "、".join(missing))
        return 1

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _dn, fn in os.walk(out) for f in fn)
    print(f"\n共 {copied} 项，{size / 1024 / 1024:.1f} MB → {out}")

    if not args.no_check:
        print("\n== 复检：tools/check_release.py ==", flush=True)
        sys.stdout.flush()
        proc = subprocess.run([sys.executable, os.path.join(TOOLS, "check_release.py"), out])
        if proc.returncode != 0:
            print("\n❌ 复检没通过，先别发。")
            return proc.returncode or 1

    if args.zip:
        base = os.path.join(DIST, f"werewolf-{ver}-{flavor_label}")
        print(f"\n== 打 zip：{base}.zip ==")
        shutil.make_archive(base, "zip", root_dir=DIST, base_dir=os.path.basename(out))
        print(f"   {base}.zip")

    print("\n下一步：")
    print("  · 源码仓库（GitHub）推整个仓库即可；.env / settings.local.json / games / 配对码已在 .gitignore")
    print("  · Release 上传 dist/*.zip；首次启动会自动生成 .env、配对码.txt 与 联机码.txt")
    print("  · 电脑包和手机包是同一份源码，只是启动说明/依赖包装不同；聊天、存档不通用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
