# coding=utf-8
"""发布链契约回归（第 4 轮外审的 P1 修复）。

守四件事：
1. `build_release.py --out <dist 内>` **能**打 zip，且 zip 落在 out 旁边（以前固定打 DIST/旧名，会崩或打旧内容）；
2. 包里的 `config.yaml` 是**清洗后**的（seats 空、兜底档案合法），而且解析得开；
3. `check_release.py` 会扫 `.env.example`（以前 splitext(".env.example") 返回 ('.env','.example') → 整段跳过）；
4. `check_release.py` 会验兜底档案（以前只验 seats）。

跑法：python tests/t_package_contract.py
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                                      # noqa: E402

from tools.release_rules import ALLOWED_DEFAULT_PROFILES          # noqa: E402

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


OUT = os.path.join(ROOT, "dist", "_contract_test")
ZIP = OUT + ".zip"
PY = sys.executable


def clean() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    try:
        os.remove(ZIP)
    except OSError:
        pass


print("=== 一、--out 出 dist 内 + zip 跟 out 走 ===")
clean()
proc = subprocess.run([PY, os.path.join(ROOT, "tools", "build_release.py"),
                       "--out", OUT, "--zip"], cwd=ROOT,
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
check("1a 打包成功（退出 0）", proc.returncode == 0,
      (proc.stdout or "")[-160:].replace("\n", " "))
check("1b zip 落在 out 旁边", os.path.isfile(ZIP), ZIP)
check("1c out 目录里有 config.yaml 与 server.py",
      os.path.isfile(os.path.join(OUT, "config.yaml")) and
      os.path.isfile(os.path.join(OUT, "server.py")))

print("\n=== 二、包里 config 是清洗后的 ===")
cfg = yaml.safe_load(io.open(os.path.join(OUT, "config.yaml"), encoding="utf-8-sig"))
check("2a seats 为空", ((cfg.get("llm") or {}).get("seats") or {}) == {},
      str((cfg.get("llm") or {}).get("seats")))
prof = str(((cfg.get("llm") or {}).get("default") or {}).get("profile") or "").upper()
check("2b 兜底档案合法", prof in ALLOWED_DEFAULT_PROFILES, prof)

print("\n=== 三、check_release 会扫 .env.example ===")
envp = os.path.join(OUT, ".env.example")
# ⚠️ 不要把"像真 key 的字面量"写进仓库：GitHub push protection 会直接拦。
#    这里用拼接造一个只在本测试里生效的假 key。
_fake = "sk-" + ("a" * 28)
io.open(envp, "w", encoding="utf-8").write(f'DEEPSEEK_API_KEY="{_fake}"\n')
r = subprocess.run([PY, os.path.join(ROOT, "tools", "check_release.py"), OUT],
                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
check("3a 模板里粘了真 key → 拦", r.returncode != 0 and ".env.example" in (r.stdout or ""),
      f"exit={r.returncode}")
os.remove(envp)

print("\n=== 四、check_release 会验兜底档案 ===")
cfgp = os.path.join(OUT, "config.yaml")
raw = yaml.safe_load(io.open(cfgp, encoding="utf-8-sig"))
raw["llm"]["default"]["profile"] = "QIANFAN_DS"          # 不在模板集合里
io.open(cfgp, "w", encoding="utf-8").write(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
r2 = subprocess.run([PY, os.path.join(ROOT, "tools", "check_release.py"), OUT],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
check("4a 非模板兜底档案 → 拦", r2.returncode != 0 and "兜底档案" in (r2.stdout or ""),
      f"exit={r2.returncode}")

clean()
print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)