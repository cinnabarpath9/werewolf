# coding=utf-8
"""打桌面客户端（Windows，免装 Python）：给 itch.io / 后面 Steam 用。

用法
----
    python tools/build_exe.py            # 生成 dist-exe/werewolf-<版本>-win64/（含 exe + 配置 + 说明）
    python tools/build_exe.py --zip      # 再压成 dist-exe/werewolf-<版本>-win64-itch.zip

它做什么
--------
1. 用 PyInstaller 把 `launcher.py` + 服务端 + web/ + content/ 打成 **onedir**（一个文件夹，双击 exe 启动）；
2. **配置放在 exe 旁边**（可写、可改）—— 用 `release_rules.sanitize_config` 清洗：
   `seats: {}`、不含作者个人阵容、不含任何 key；
3. 不带 `web/pyodide`（那是浏览器试玩页用的 13MB，桌面版走本地服务，不需要）；
4. 附一份 `怎么玩.txt`（自备 key / 离线演练 / 局域网多人），给 itch 页面用。

⚠️ 打包机器需要装过 PyInstaller（`pip install pyinstaller`）。产物不入库（dist-exe/ 已 gitignore）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools.release_rules import sanitize_config                       # noqa: E402

OUT = os.path.join(ROOT, "dist-exe")
SKIP_WEB = {"pyodide"}                     # 桌面版不需要 wasm 试玩页


def version() -> str:
    with open(os.path.join(ROOT, "web", "site.json"), encoding="utf-8-sig") as fh:
        return str(json.load(fh).get("version") or "1.0.0")


def stage_web(dst: str) -> None:
    """把 web/ 拷到打包暂存区，但甩掉 pyodide。"""
    for name in os.listdir(os.path.join(ROOT, "web")):
        if name in SKIP_WEB:
            continue
        src = os.path.join(ROOT, "web", name)
        target = os.path.join(dst, name)
        if os.path.isdir(src):
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)


def build(ver: str) -> str:
    stage = os.path.join(OUT, "stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(os.path.join(stage, "web"))
    os.makedirs(os.path.join(stage, "build"), exist_ok=True)
    stage_web(os.path.join(stage, "web"))

    work = os.path.join(OUT, "work")
    icon = os.path.join(ROOT, "web", "icon-512.png")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        # ⚠️ 2026-11-04：onefile 在部分机器上报 "Could not create temporary directory"
        #    （bootloader 解压阶段就挂）。改成 **onedir**：产物是一个文件夹，
        #    启动更快、不依赖 %TEMP% 可写，itch 上照样整包上传。
        "--onedir", "--console", "--name", "werewolf",
        "--distpath", os.path.join(stage, "build"),
        "--workpath", work,
        "--specpath", stage,
        f"--add-data={os.path.join(stage, 'web')};web",
        f"--add-data={os.path.join(ROOT, 'content')};content",
        "--collect-submodules=uvicorn",
        "--collect-submodules=fastapi",
        "--collect-submodules=starlette",
        "--hidden-import=websockets",
        "--hidden-import=httpx",
        "--hidden-import=yaml",
        "--exclude-module=tkinter",
        "--exclude-module=pyodide",
        os.path.join(ROOT, "launcher.py"),
    ]
    if os.path.isfile(icon):
        cmd.insert(cmd.index("--exclude-module=tkinter"), f"--icon={icon}")
    print("== PyInstaller ==")
    subprocess.run(cmd, check=True, cwd=ROOT)

    out = os.path.join(OUT, f"werewolf-{ver}-win64")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    built = os.path.join(stage, "build", "werewolf")
    shutil.copytree(built, out, dirs_exist_ok=True)

    # 配置：清洗后放 exe 旁边（可写）
    cfg_text, packaged_cfg = sanitize_config(
        open(os.path.join(ROOT, "config.yaml"), encoding="utf-8-sig").read())
    with open(os.path.join(out, "config.yaml"), "w", encoding="utf-8", newline="") as fh:
        fh.write(cfg_text)
    # 再解析一次写盘后的文件：--no-required 会跳过 check_release 的 config 闸门，
    # 这里自己兜住"文件写坏了"的情况。
    import yaml
    _parsed = yaml.safe_load(io.open(os.path.join(out, "config.yaml"), encoding="utf-8").read()) or {}
    from tools.release_rules import assert_packaged
    assert_packaged(_parsed)
    print("配置清洗：seats 空 / 兜底 =",
          ((packaged_cfg.get("llm") or {}).get("default") or {}).get("profile"))

    with open(os.path.join(out, "怎么玩.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"""AI 狼人杀 · 桌面版（v{ver}）

【怎么开始】
1. 双击 werewolf.exe。会弹出一个黑色窗口，并自动打开浏览器；
   黑窗口里会打印「本机 / 同一 Wi-Fi 的朋友用」两个地址（端口被占会自动换，以窗口为准）。
2. 不想花 API 钱：在大厅点「离线演练」，不需要任何 key 就能打完一局。
3. 想用真模型：进「设置 → 模型 API」，选一个平台、填你自己的 API key、选模型。
   费用由你的模型平台按 token 计费，本游戏不代收任何费用。

【一个人玩 / 和朋友一起玩】
- 一个人：直接开始；想让 AI 打你的座位，就用「托管这一手」。
- 同一台电脑：大厅可以开联机房，朋友在同一台机器上开浏览器加入。
- 同一 Wi-Fi：房主开着 werewolf.exe，朋友用**黑窗口里打印的那个局域网地址**打开浏览器，
  用「联机码」加入房间（配对码只给房主改设置用）。

【没有联网 / 没有任何 key】
也能玩：离线演练不调用任何模型。

【数据在哪】
都在这个文件夹里：config.yaml（配置）、games/（对局存档）、配对码.txt / 联机码.txt、.env（你填的 key）。
不玩了直接删掉整个文件夹（如果用过真模型，记得先清 .env）。

【许可】
源码可见 + 个人非商业；本包是作者本人发布的 itch 版本。
""")

    # ⚠️ 2026-11-04 审查 P1：客户端出口以前**不跑整包复检**（发布包跑），
    #    必备文件/密钥/config 解析三条闸门对 dist-exe 全失效。这里补上。
    print("\n== 复检：tools/check_release.py ==", flush=True)
    # ⚠️ 客户端是 onedir：`server.py` 在 `_internal/` 里，不在包根，所以用 `--no-required`
    #    （必备文件那条对源码包有意义，对 exe 包不适用）；密钥扫描与"禁带物"仍然生效。
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check_release.py"),
                           out, "--no-required"])
    if proc.returncode != 0:
        print("❌ 客户端复检没通过，先别发。")
        raise SystemExit(proc.returncode or 1)
    print(f"\n✅ 客户端：{out}")
    return out


def pack(out: str, ver: str) -> str:
    zip_path = os.path.join(OUT, f"werewolf-{ver}-win64-itch.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(out):
            for name in files:
                p = os.path.join(dirpath, name)
                z.write(p, os.path.relpath(p, os.path.dirname(out)))
    print(f"✅ itch 上传包：{zip_path}（{int(os.path.getsize(zip_path)/1024)} KB）")
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="同时压一个 itch 上传包")
    args = ap.parse_args()
    ver = version()
    out = build(ver)
    if args.zip:
        pack(out, ver)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())