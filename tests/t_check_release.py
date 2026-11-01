# coding=utf-8
"""发布闸门（tools/check_release.py）回归：该拦的拦、该放的放。

为什么要测这个
--------------
对标某商业 Demo 时发现它的正式包里带着内部资料与整套测试。这个工具就是本项目的
同类闸门 —— 但它自己必须是**会变红**的：一条永远为真的检查等于没有检查。
所以除了「违规能检出」，这里还做一次**变异自证**：把 BANNED 里的一条删掉，
同一个违规目录必须变成通过（证明那条判据真的在起作用）。

跑法：python t_check_release.py        （纯本地，秒级，不联网、不起服务）
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "check_release.py")
TMP = os.path.join(HERE, "tmp", "release_check")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, TOOL, *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def make_pkg(name: str, files: dict[str, str], required: bool = True) -> str:
    root = os.path.join(TMP, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    if required:
        base = {
            "server.py": "# stub\n",
            "run_game.py": "# stub\n",
            "config.yaml": "game:\n  board: eleven_wolfking\n",
            "web/index.html": "<html></html>\n",
            "wolf/__init__.py": "\n",
            "content/01_规则书.md": "# 规则书\n",
        }
        base.update(files)
        files = base
    for rel, text in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(full) else None
        with io.open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return root


def make_zip(name: str, files: dict[str, str]) -> str:
    import zipfile
    path = os.path.join(TMP, name)
    if os.path.isfile(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(rel, text)
    return path


def main() -> int:
    if not os.path.isfile(TOOL):
        print(f"缺少工具：{TOOL}")
        return 1
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP, exist_ok=True)

    # ── 1. 干净的包：必须通过 ──────────────────────────────────────────
    clean = make_pkg("clean", {})
    code, out = run(clean)
    check("1a 干净包通过（退出 0）", code == 0, f"退出 {code}")
    check("1b 结论写明可以发布", "可以发布" in out, out.strip().splitlines()[-1] if out else "")

    # ── 2. 该拦的：.env / 配对码 / 存档 / docs / tests ────────────────
    dirty = make_pkg("dirty", {
        ".env": "ARK_API_KEY=xxx\n",
        "配对码.txt": "123456\n",
        "games/game-20261030.json": '{"transcript": []}\n',
        "docs/交接主文档.md": "# 交接\n",
        "tests/t_rules_new.py": "# test\n",
        "reports/报告.md": "# 报告\n",
    })
    code, out = run(dirty)
    check("2a 违规包被拦（退出 1）", code == 1, f"退出 {code}")
    check("2b 点名 .env", ".env" in out and "环境变量" in out, "")
    check("2c 点名配对码", "配对码.txt" in out, "")
    check("2d 点名声档", "game-20261030.json" in out, "")
    check("2e 点名 docs/ 与 tests/", "docs/" in out and "tests/" in out, "")
    check("2f 结论写明不能发布", "不能发布" in out, "")

    # ── 3. 硬编码密钥：按内容抓，而不是按文件名 ────────────────────────
    keyed = make_pkg("keyed", {
        "config.yaml": "llm:\n  api_key: sk-abcdefghijklmnopqrstuvwxyz012345\n",
    })
    code, out = run(keyed)
    check("3a 硬编码 key 被拦", code == 1 and "key" in out.lower(), f"退出 {code}")
    check("3b 占位符不算 key（${...}）",
          run(make_pkg("placeholder", {"config.yaml": "llm:\n  api_key: ${ARK_API_KEY}\n"}))[0] == 0,
          "")

    # ── 4. 必备清单 ────────────────────────────────────────────────────
    slim = make_pkg("slim", {"web/index.html": "<html></html>\n"}, required=False)
    code, out = run(slim)
    check("4a 缺 server.py 等必备文件 → 拦", code == 1 and "缺：" in out, f"退出 {code}")
    check("4b --no-required 时不因缺文件报错", run(slim, "--no-required")[0] == 0, "")

    # ── 5. zip 包同口径 ────────────────────────────────────────────────
    z = make_zip("pkg.zip", {"server.py": "# stub\n", "docs/x.md": "# x\n"})
    code, out = run(z)
    check("5a zip 里的 docs/ 也能检出", code == 1 and "docs/x.md" in out, f"退出 {code}")

    # 2026-10-31：Release 的 zip 通常多包一层目录（werewolf-1.0.0/...）。
    # 必备文件判据要能剥掉唯一的顶层目录再判，否则一个完全干净的发版包会被判「缺 server.py」。
    wrapped = make_zip("wrapped.zip", {
        f"werewolf-1.0.0/{rel}": text for rel, text in {
            "server.py": "# stub\n",
            "run_game.py": "# stub\n",
            "config.yaml": "game:\n  board: eleven_wolfking\n",
            "web/index.html": "<html></html>\n",
            "wolf/__init__.py": "\n",
            "content/01_规则书.md": "# 规则书\n",
        }.items()
    })
    code, out = run(wrapped)
    check("5b 多包一层目录的 zip：仍认得出必备文件（退出 0）",
          code == 0 and "6 项都在" in out, f"退出 {code}｜{out.strip().splitlines()[-1] if out else ''}")

    # ── 6. 变异自证：删掉一条判据，同一个违规目录必须变绿 ──────────────
    src = io.open(TOOL, encoding="utf-8").read()
    mutated = src.replace('("docs/", "维护文档（交接主文档/历史日志/复核报告）"),', "", 1)
    if mutated == src:
        check("6a 变异自证（能改坏判据）", False, "没找到可替换的那一行，测试需要更新")
    else:
        mpath = os.path.join(TMP, "_mutant.py")
        with io.open(mpath, "w", encoding="utf-8", newline="") as f:
            f.write(mutated)
        proc = subprocess.run(
            [sys.executable, mpath, dirty], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        check("6a 删掉 docs/ 判据后，同一目录里 docs/ 不再被点名",
              "docs/交接主文档.md" not in (proc.stdout or ""), "")

    bad = [x for x in RESULTS if not x[1]]
    print("=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
