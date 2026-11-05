# coding=utf-8
"""思考强度 A/B：用**我们自己的真实 packet** 重放，不靠通用笔试。

为什么不用现成题库：`34 题终版卷` 与 `v2 9 题` 对 DS 的结论相反（low 76% vs high 74% / high 75% vs low 62%）。
题库测的是"懂不懂规则"，而定档位要测的是"**在这套包、这个任务上，多想想是帮忙还是帮倒忙**"。

三种用法（默认都不花钱）：
    python tools/thinking_ab.py --list                  # 看看存档里有哪些局面可抽
    python tools/thinking_ab.py --prepare 8             # 抽 8 个局面，写 dist-ab/thinking/scenarios.json
    python tools/thinking_ab.py --plan --profiles QIANFAN_DS,GLM   # 打印矩阵与调用数（不发请求）
    python tools/thinking_ab.py --run --yes --profiles QIANFAN_DS,GLM --levels off,low,high
    python tools/thinking_ab.py --report                # 汇总最近一次结果

判据（同一 packet，只改思考档）：
    · JSON 合法率；· 决策字段齐不齐；· 发言字数；· 延迟；· completion tokens；
    · 复述/编造（可选，用更强模型对照台账判，另行计费）。
注意：一次采样说明不了问题；同格至少 3 次、局面 ≥8 个，先看方向再看显著性。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

OUT = os.path.join(ROOT, "dist-ab", "thinking")
SCEN = os.path.join(OUT, "scenarios.json")

#: 值得测的任务（越靠前越影响胜负）
TASK_ORDER = ("speech_plan", "vote", "seer_check", "witch", "sheriff_speech",
              "wolf_talk", "wolf_explode", "last_words", "sheriff_vote", "sheriff_run",
              "speech", "dream", "lucky", "wolf_intent")


def load_env() -> None:
    import server                                           # noqa: E402
    server.load_env(os.path.join(ROOT, ".env"))


def profile_env(profile: str) -> dict:
    p = profile.strip().upper()
    return {
        "base_url": os.environ.get(f"{p}_BASE_URL", ""),
        "api_key": os.environ.get(f"{p}_API_KEY", ""),
        "model": os.environ.get(f"{p}_MODEL", ""),
    }


def saves() -> list[str]:
    return sorted(glob.glob(os.path.join(ROOT, "games", "game-*.json")), key=os.path.getmtime, reverse=True)


def candidates() -> list[dict]:
    out = []
    for path in saves():
        try:
            data = json.load(io.open(path, encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            continue
        for row in data.get("transcript") or []:
            task = str(row.get("task") or "")
            pkt = str(row.get("packet") or "")
            if task not in TASK_ORDER or len(pkt) < 500:
                continue
            out.append({
                "save": os.path.basename(path), "task": task, "day": row.get("day"),
                "seat": row.get("seat"), "role": row.get("role"), "packet": pkt,
                "model": row.get("model"),
            })
    return out


def pick(n: int) -> list[dict]:
    rows = candidates()
    picked, seen = [], set()
    for task in TASK_ORDER:
        for r in rows:
            if r["task"] != task:
                continue
            key = (r["task"], r["role"])
            if key in seen:
                continue
            seen.add(key)
            picked.append(r)
            break
        if len(picked) >= n:
            break
    return picked[:n]


def cmd_list() -> int:
    rows = candidates()
    from collections import Counter
    print(f"存档里的可用局面：{len(rows)}")
    for task, cnt in Counter(r["task"] for r in rows).most_common():
        print(f"  {task:<16} {cnt}")
    return 0


def cmd_prepare(n: int) -> int:
    os.makedirs(OUT, exist_ok=True)
    picked = pick(n)
    if not picked:
        print("❌ 没有可用局面（先打几局真模型）")
        return 1
    json.dump(picked, io.open(SCEN, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✅ {len(picked)} 个局面 → {SCEN}")
    for r in picked:
        print(f"  {r['task']:<14} {r['role']:<8} d{r['day']} 号{r['seat']:<2} {len(r['packet'])//1000}k 字  {r['save']}")
    return 0


def cmd_plan(profiles: list[str], levels: list[str], repeats: int) -> int:
    picked = json.load(io.open(SCEN, encoding="utf-8")) if os.path.isfile(SCEN) else []
    if not picked:
        print("先跑 --prepare N")
        return 1
    calls = len(picked) * len(levels) * len(profiles) * repeats
    print(f"局面 {len(picked)} × 档位 {len(levels)} × 档案 {len(profiles)} × 重复 {repeats} = **{calls} 次调用**")
    for pf in profiles:
        env = profile_env(pf)
        state = "有 key" if env["api_key"] else "**缺 key**"
        print(f"  {pf:<14} {state:<10} model={env['model'] or '缺'}")
    est = calls * 0.02
    print(f"\n粗估成本：¥{est:.1f}~¥{est*4:.1f}（按每次 2~8 分钱；只算生成，不含判定）")
    print("确认后再：--run --yes")
    return 0


async def cmd_run(profiles: list[str], levels: list[str], repeats: int, tag: str = "") -> int:
    from wolf import butler
    from wolf.llm import LLMClient, LLMError, extract_json

    picked = json.load(io.open(SCEN, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT, f"results-{stamp}{('-' + tag) if tag else ''}.json")
    results = []
    total = len(picked) * len(levels) * len(profiles) * repeats
    done = 0
    for pf in profiles:
        env = profile_env(pf)
        if not (env["base_url"] and env["api_key"] and env["model"]):
            print(f"跳过 {pf}：档案不全")
            continue
        style = butler.infer_thinking_style(env["base_url"])
        for level in levels:
            client = LLMClient(env["base_url"], env["api_key"], env["model"],
                               temperature=1.0, max_tokens=20000, timeout=180,
                               max_retries=0, stream=False,
                               thinking=butler.thinking_table(level), thinking_style=style)
            for scen in picked:
                for k in range(repeats):
                    done += 1
                    print(f"[{done}/{total}] {pf} {level} {scen['task']} …", end="", flush=True)
                    t0 = time.time()
                    try:
                        reply = await client.chat_json(scen["packet"])
                        ok, err = True, ""
                    except LLMError as exc:
                        reply, ok, err = {}, False, str(exc)[:160]
                    elapsed = round(time.time() - t0, 2)
                    speech = str((reply or {}).get("speech") or "")
                    results.append({
                        "profile": pf, "level": level, "task": scen["task"], "role": scen["role"],
                        "save": scen["save"], "day": scen["day"], "seat": scen["seat"],
                        "valid": ok, "error": err, "elapsed": elapsed,
                        "speech_len": len(speech), "keys": sorted((reply or {}).keys()),
                        "reply": reply, "packet_len": len(scen["packet"]),
                        "completion_tokens": int((client.last_call or {}).get("completion_tokens") or 0),
                        "prompt_tokens": int((client.last_call or {}).get("prompt_tokens") or 0),
                        "speech": speech[:4000],   # 盲评要看完整发言；封顶 4000 字
                    })
                    print(f" {'✅' if ok else '❌'} {elapsed}s  {len(speech)}字")
            await client.aclose()
    json.dump(results, io.open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ {len(results)} 条 → {out}\n再跑 --report {os.path.basename(out)}")
    return 0


def cmd_report(path: str = "") -> int:
    files = sorted(glob.glob(os.path.join(OUT, "results-*.json")))
    path = path or (files[-1] if files else "")
    if not path or not os.path.isfile(path):
        print("没有结果文件")
        return 1
    rows = json.load(io.open(path, encoding="utf-8"))
    print(f"结果：{os.path.basename(path)}（{len(rows)} 条）\n")
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["profile"], r["level"]), []).append(r)
    print(f"{'档案':<14}{'档位':<6}{'成功':<8}{'延迟中位':<10}{'completion中位':<16}{'发言字数中位'}")
    for (pf, lv), rs in sorted(groups.items()):
        ok = sum(1 for r in rs if r["valid"])
        lat = statistics.median(r["elapsed"] for r in rs)
        tok = statistics.median(r["completion_tokens"] for r in rs)
        sp = statistics.median(r["speech_len"] for r in rs)
        print(f"{pf:<14}{lv:<6}{ok}/{len(rs):<5}{lat:<10}{tok:<16.0f}{sp:<.0f}")
    print("\n先看方向：成功率高、延迟可接受、字数没有塌。样本小，别当定论。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prepare", type=int, default=0)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="确认会真花钱")
    ap.add_argument("--report", nargs="?", const="", default=None)
    ap.add_argument("--profiles", default="QIANFAN_DS,QIANFAN_GLM,MINIMAX,HAIKU,GEMINI38")
    ap.add_argument("--levels", default="off,low,high")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    load_env()
    profiles = [x.strip() for x in args.profiles.split(",") if x.strip()]
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]

    if args.list:
        return cmd_list()
    if args.prepare:
        return cmd_prepare(args.prepare)
    if args.plan:
        return cmd_plan(profiles, levels, args.repeats)
    if args.run:
        if not args.yes:
            print("这会真花钱。确认后加 --yes。先用 --plan 看调用数与粗估成本。")
            return 2
        import asyncio
        return asyncio.run(cmd_run(profiles, levels, args.repeats, args.tag))
    if args.report is not None:
        return cmd_report(args.report)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())