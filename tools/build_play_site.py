# coding=utf-8
"""把仓库打成一份"国内静态托管"目录：`dist-play/` + `dist/werewolf-<版本>-在线试玩站.zip`。

2026-11-03：之前这份产物是**手工拷进仓库**的（13.9MB 的 wolf/ 副本），
既占仓库、又会跟主源码漂移。现在改成脚本生成：
  · `dist-play/` 与 `dist/` 都已进 `.gitignore`；
  · 要更新托管时跑一次本脚本，把 zip 传上去即可。

跑法：python tools/build_play_site.py
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "dist-play")
#: 2026-11-03（作者要求）：静态站包也带版本 + 中文后缀
def _site_version() -> str:
    try:
        import json
        with open(os.path.join(ROOT, "web", "site.json"), encoding="utf-8-sig") as fh:
            return str(json.load(fh).get("version") or "1.0.0")
    except Exception:                                    # noqa: BLE001
        return "1.0.0"


ZIP = os.path.join(ROOT, "dist", f"werewolf-{_site_version()}-在线试玩站.zip")

PY_FILES = [
    "__init__.py", "engine.py", "roles.py", "context.py", "rules.py", "words.py",
    "ledger.py", "tabletalk.py", "persona.py", "llm.py", "agents.py", "browser_glue.py",
    "skills.py",
]
MD_FILES = ["01_规则书.md", "04_角色书.md", "05_战术书.md", "06_概念库.md", "08_技能书.md"]


def main() -> int:
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # 页面放在根目录（play.html 的取文件逻辑先试 ../ 再试同级，所以同级也能用）
    shutil.copy2(os.path.join(ROOT, "web", "play.html"), os.path.join(OUT, "index.html"))
    shutil.copy2(os.path.join(ROOT, "web", "skill_panels.js"),
                  os.path.join(OUT, "skill_panels.js"))

    os.makedirs(os.path.join(OUT, "wolf"))
    for name in PY_FILES:
        shutil.copy2(os.path.join(ROOT, "wolf", name), os.path.join(OUT, "wolf", name))

    os.makedirs(os.path.join(OUT, "content", "personas"))
    for name in MD_FILES:
        shutil.copy2(os.path.join(ROOT, "content", name), os.path.join(OUT, "content", name))

    pyodide_src = os.path.join(ROOT, "web", "pyodide")
    if not os.path.isdir(pyodide_src):
        print("⚠️  web/pyodide/ 不存在 —— 先把它下载回来（见 README §6.5）", file=sys.stderr)
        return 1
    shutil.copytree(pyodide_src, os.path.join(OUT, "pyodide"))

    os.makedirs(os.path.dirname(ZIP), exist_ok=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(OUT):
            for name in files:
                full = os.path.join(base, name)
                zf.write(full, os.path.relpath(full, OUT))

    total = sum(os.path.getsize(os.path.join(base, f))
                for base, _d, files in os.walk(OUT) for f in files)
    print(f"[OK] dist-play/ 已生成（{total / 1024 / 1024:.1f} MB）")
    print(f"[OK] zip: {ZIP}（{os.path.getsize(ZIP) / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())