# coding=utf-8
"""用 AST 精确核对：packet_block / persona.load / voice.tts / for_game 的调用参数
跟签名对不对得上（正则会被 `getattr(a, "b", "")` 里的逗号骗到）。"""
import ast
import io
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from wolf import persona, rules, voice                      # noqa: E402

TARGETS = {
    "packet_block": rules.packet_block,
    "for_game": rules.for_game,
    "persona.load": persona.load,
    "voice.tts": voice.tts,
}
print("=== 签名 ===")
for label, fn in TARGETS.items():
    sig = inspect.signature(fn)
    print(f"  {label}{sig}")

print("\n=== 各调用点（AST 数参数）===")
problems = []
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
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            key = None
            if name == "packet_block":
                key = "packet_block"
            elif name == "for_game":
                key = "for_game"
            elif name == "load" and getattr(f, "value", None) is not None \
                    and getattr(f.value, "id", "") == "persona":
                key = "persona.load"
            elif name == "tts" and getattr(f, "value", None) is not None \
                    and getattr(f.value, "id", "") == "voice":
                key = "voice.tts"
            if not key:
                continue
            fn = TARGETS[key]
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            n_pos = len(node.args)
            kw = [k.arg for k in node.keywords if k.arg]
            # 允许的参数个数范围（有默认值的可以省）
            required = sum(1 for p in params
                           if p.default is inspect.Parameter.empty
                           and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
            allowed = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD,
                                                            p.KEYWORD_ONLY)}
            ok_count = required <= n_pos <= len(params)
            bad_kw = [k for k in kw if k not in allowed]
            has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params)
            status = "OK" if ok_count and (not bad_kw or has_var_kw) else "❌"
            if status == "❌":
                problems.append(f"{rel}:{node.lineno} {key} 位置参数 {n_pos}、关键字 {kw}")
            print(f"  {status} {rel}:{node.lineno} {key}：位置 {n_pos}、关键字 {kw}")

print("\n=== 结论 ===")
if problems:
    for p in problems:
        print("  ❌", p)
else:
    print("  ✅ 所有调用点的参数都能对上签名（没有过时调用）")
raise SystemExit(1 if problems else 0)
