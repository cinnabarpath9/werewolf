# coding=utf-8
"""细查这局每一次调用的健康度：错误、兜底、思考档、耗时、token。"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
path = sorted((os.path.join("games", n) for n in os.listdir("games") if n.endswith(".json")),
              key=os.path.getmtime)[-1]
g = json.load(io.open(path, encoding="utf-8"))
tr = g.get("transcript") or []
print(f"  存档 {os.path.basename(path)}  对局第 {g.get('day')} 天，进行中={g.get('in_progress')}")
print(f"  共 {len(tr)} 次调用\n")

bad = []
for t in tr:
    seat, kind = t.get("seat"), t.get("task")
    day, phase = t.get("day"), t.get("phase")
    err, fb = (t.get("error") or ""), (t.get("fallback") or "")
    el = float(t.get("elapsed") or 0)
    pt, ct = t.get("prompt_tokens"), t.get("completion_tokens")
    th = t.get("thinking") or {}
    th = (th.get("type") if isinstance(th, dict) else str(th)) or "-"
    flag = ""
    if err:
        flag += " ❌有错误"
        bad.append((seat, kind, "error", err[:120]))
    if fb:
        flag += f" ⚠️兜底={fb}"
        bad.append((seat, kind, "fallback", str(fb)[:120]))
    if not (t.get("reply") or t.get("raw")):
        flag += " ⚠️空回复"
        bad.append((seat, kind, "empty", ""))
    if el >= 60:
        flag += f" 🐢{el:.0f}s"
    print(f"    d{day} {str(phase):<5} {seat:>2}号 {str(kind):<13} {el:>7.1f}s "
          f"tok={pt}/{ct} think={th:<6}{flag}")

print()
if bad:
    print(f"  ⚠️ 有问题的调用 {len(bad)} 条：")
    for seat, kind, what, detail in bad:
        print(f"    {seat}号 {kind} [{what}] {detail}")
else:
    print("  ✅ 没有错误、没有兜底、没有空回复")

# 耗时排行
rows = sorted(tr, key=lambda t: -float(t.get("elapsed") or 0))[:6]
print("\n  最慢的 6 次：")
for t in rows:
    print(f"    {float(t.get('elapsed') or 0):>7.1f}s  {t.get('seat')}号 {t.get('task')} "
          f"({t.get('model')})")
tot = sum(float(t.get("elapsed") or 0) for t in tr)
print(f"\n  累计耗时 {tot:.0f} 秒（{tot/60:.1f} 分钟）")
print(f"  token 合计：prompt {sum(int(t.get('prompt_tokens') or 0) for t in tr):,} / "
      f"completion {sum(int(t.get('completion_tokens') or 0) for t in tr):,}")
