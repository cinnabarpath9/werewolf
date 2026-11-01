# coding=utf-8
"""查这一局（刚开就失败的）里那一条 300 秒的失败调用：座位、任务、错误原文。"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
files = sorted((os.path.join("games", n) for n in os.listdir("games")
                if n.endswith(".json") and os.path.isfile(os.path.join("games", n))),
               key=os.path.getmtime, reverse=True)[:2]
for path in files:
    g = json.load(io.open(path, encoding="utf-8"))
    print(f"  ══ {os.path.basename(path)}：day={g.get('day')} winner={g.get('winner')} "
          f"aborted={g.get('aborted')} 人类座位={g.get('human_seat')}")
    tr = g.get("transcript") or []
    print(f"     共 {len(tr)} 次调用")
    for t in tr:
        el = float(t.get("elapsed") or 0)
        err = str(t.get("error") or "")
        flag = "❌" if err or el > 120 else "  "
        print(f"     {flag} {t.get('seat')}号 d{t.get('day')} {t.get('phase')} "
              f"{str(t.get('task')):<12} model={str(t.get('model')):<22} {el:>6.1f}s "
              f"fallback={t.get('fallback') or '-'}")
        if err:
            print(f"        错误：{err[:300]}")
    # 台账末尾，看流程走到哪
    led = g.get("ledger") or []
    print("     ── 台账最后 4 条：")
    for e in led[-4:]:
        print(f"        d{e.get('day')} {e.get('kind')}: {str(e.get('text'))[:90]}")
    print()
