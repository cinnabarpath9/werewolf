# coding=utf-8
"""把"本轮新增代码 vs 原有代码"的自查再机械扫一遍（含这轮新加的换家/自动重试）。"""
import ast
import inspect
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


print("=== 一、全仓语法 + Game(...)/LLMAgent(...) 关键字无重复 ===")
bad: list[str] = []
sites = {"Game": 0, "LLMAgent": 0}
for base, _d, names in os.walk(ROOT):
    if any(x in base for x in ("__pycache__", "var", "games", "legacy", "_logs")):
        continue
    for n in names:
        if not n.endswith(".py"):
            continue
        path = os.path.join(base, n)
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(io.open(path, encoding="utf-8-sig").read())
        except SyntaxError as exc:
            bad.append(f"{rel}:{exc.lineno} 语法错误")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if fname in sites:
                    sites[fname] += 1
                    keys = [k.arg for k in node.keywords if k.arg]
                    dup = sorted({k for k in keys if keys.count(k) > 1})
                    if dup:
                        bad.append(f"{rel}:{node.lineno} {fname} 重复参数 {dup}")
check("1a 没有语法错误 / 重复关键字", not bad, "；".join(bad[:4]))
check("1b 调用点都扫到了", sites["Game"] >= 10 and sites["LLMAgent"] >= 4, str(sites))

print("\n=== 二、换家相关的签名与调用点（AST 精确核对）===")
check("2a LLMAgent 的 fallbacks 是可选参数",
      "fallbacks" in str(inspect.signature(__import__("wolf.agents", fromlist=["LLMAgent"])
                                           .LLMAgent.__init__)))
check("2b hasattr 判断的是 use_fallback（引擎侧鸭子类型）",
      'getattr(agent, "use_fallback"' in read("wolf/engine.py"))
for rel in ("server.py", "run_game.py", r"tools\exam_live.py"):
    src = read(rel)
    check(f"2c {rel} 的替补是懒建（出厂函数）",
          "lambda sp=spec" in src and "build_client(sp, label" in src)
    check(f"2d {rel} 传了 carry_reasoning",
          "carry_reasoning=" in src)

print("\n=== 三、Recorder（考试包装）透出的能力是否够引擎用 ===")
ex = read(r"tools\exam_live.py")
for need in ("ask", "last_meta", "set_context", "uses_model", "use_fallback", "model_now"):
    check(f"3 Recorder 透出 {need}", need in ex.split("class Recorder")[1][:2200])

print("\n=== 四、配置键都有人读（连这轮新增的）===")
import yaml                                              # noqa: E402
cfg = yaml.safe_load(read("config.yaml"))
# ⚠️ 扫描范围要覆盖**整个仓库的 .py**：第一版只挑了 5 个文件，
#    `player_prompt` 是在 wolf/tabletalk.py 里读的，于是误报"没人读"。
allsrc = ""
for _base, _d, _names in os.walk(ROOT):
    if any(x in _base for x in ("__pycache__", "var", "games", "legacy", "_logs")):
        continue
    for _n in _names:
        if _n.endswith(".py"):
            allsrc += io.open(os.path.join(_base, _n), encoding="utf-8-sig").read()
for k in sorted((cfg.get("game") or {})):
    if k in ("board", "boards", "seed", "human_seat", "save_dir", "max_days", "experience",
             "experience_dir", "sheriff", "sheriff_min_seats", "pk_on_tie", "wolf_control",
             "human_timeout", "stall_timeout", "rules_mode", "rules_detail", "personas",
             "personas_dir", "player_prompt", "meta_book", "meta_book_path", "playbook",
             "playbook_path", "edge_order", "call_timeout", "decision_timeout",
             "auto_retries", "retry_backoff", "retry_jitter", "fallback",
             "carry_reasoning", "carry_reasoning_chars"):
        check(f"4 game.{k} 出现在代码里", f'"{k}"' in allsrc or f"'{k}'" in allsrc)

print("\n=== 五、确认没有「配了但不生效」的开关 ===")
check("5a fallback 配置真的被 _build_fallbacks 用", "game_cfg.get(\"fallback\")" in read("server.py")
      or "game_cfg.get(\"fallback\")" in read("run_game.py"))
check("5b carry_reasoning 真的被 engine 用",
      "self.carry_reasoning" in read(r"wolf\engine.py"))
check("5c auto_retries/retry_backoff 真的被 engine 用",
      "self.auto_retries" in read(r"wolf\engine.py") and "self.retry_backoff" in read(r"wolf\engine.py"))
check("5d retry 事件前端有人处理", "case 'retry'" in read(r"web\index.html"))

badlist = [x for x in RESULTS if not x[1]]
print("\n" + "=" * 74)
print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(badlist)}，失败 {len(badlist)}")
for n, _, d in badlist:
    print(f"  [FAIL] {n}  {d}")
print("=" * 74)
raise SystemExit(1 if badlist else 0)
