# coding=utf-8
"""模型考试：三套卷子，一个脚本。

题库在 `tools/exam_papers.json`（和这个脚本放在一起，发布包也带）：

  · `basic`   基础卷 30 题：只考输出格式、基础规则、概念、角色技能。
              要求 ≥95%；90%~95% 是边缘；<90% 不建议用这个模型玩本项目。
  · `strength` 实力卷 30 题：多步组合、残局、票权、边界、注入与信息隔离。
              分越高，通常越能撑住真实对局；没有硬门槛，但报告会给分档。
  · `full`    全量卷 60 题 = 基础 30 + 实力 30。
              不想跑两套的人用这一套：先看基础是否过线，再看实力分。

用法
----
    python tools/exam_paper.py --list
    python tools/exam_paper.py --selftest

    # 模型清单（YAML，key 建议只写环境变量名）
    python tools/exam_paper.py --paper basic --models my_models.yaml
    python tools/exam_paper.py --paper full  --models my_models.yaml --out reports/exam.md

    # 不想写 YAML？直接读项目根 .env 里的档案（DEEPSEEK / QWEN / GLM ...）
    python tools/exam_paper.py --paper basic --profile DEEPSEEK
    python tools/exam_paper.py --paper strength --profile DEEPSEEK,GLM

    # 离线演练（不联网，用假模型验证流程和判分）
    python tools/exam_paper.py --paper full --dry-run

模型 YAML 示例：

    models:
      - name: ds
        base_url: https://api.deepseek.com/v1
        api_key: env:DEEPSEEK_API_KEY      # 或 api_key_env: DEEPSEEK_API_KEY
        model: deepseek-v4.1-flash
        temperature: 0.2
        max_tokens: 4000
        # extra_body: {reasoning_effort: high}   # 需要时原样透传

⚠️ key 只从环境变量/你的 YAML 读，报告和 raw.json 都不写 key。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

import httpx
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BANK_PATH = os.path.join(HERE, "exam_papers.json")

SYSTEM_PROMPT = (
    "你正在参加《AI 狼人杀》项目的模型考试。"
    "只能依据题目和规则摘要作答，不要联网、不要输出表格或长篇解释。"
    "每题只选一个选项，严格按要求输出一个 JSON 对象。"
)

#: 基础卷门槛
BASIC_PASS = 0.95
BASIC_WARN = 0.90


# ────────────────────────────────────────────────────────────────────────── #
# 题库
# ────────────────────────────────────────────────────────────────────────── #
def load_bank() -> dict:
    with io.open(BANK_PATH, encoding="utf-8") as fh:
        bank = json.load(fh)
    return bank


def paper_ids(bank: dict, paper: str) -> list[str]:
    papers = bank.get("papers") or {}
    if paper not in papers:
        raise SystemExit(f"没有这套卷子：{paper}；可选 {', '.join(papers)}")
    return list(papers[paper]["questions"])


def build_prompt(bank: dict, q: dict) -> str:
    opts = q["options"]
    lines = [
        "【规则摘要】",
        bank["rule_digest"],
        "",
        f"【题目 {q['id']}】（{q['category']}）",
        q["question"],
        "",
    ]
    for key in ("A", "B", "C", "D"):
        if key in opts:
            lines.append(f"{key}. {opts[key]}")
    lines += [
        "",
        f"只输出一个 JSON 对象，不要输出别的：{{\"id\": \"{q['id']}\", \"answer\": \"A\"}}",
        "answer 必须是 A/B/C/D 中的一个。",
    ]
    return "\n".join(lines)


def extract_objects(text: str) -> list[dict]:
    """从回复里找出所有能解析的 JSON 对象（容忍代码块和前后废话）。"""
    out: list[dict] = []
    candidates: list[str] = []
    fenced = re.findall(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates.extend(fenced)
    candidates.append(text)
    for src in candidates:
        i = 0
        while True:
            start = src.find("{", i)
            if start < 0:
                break
            for end in range(len(src), start, -1):
                chunk = src[start:end]
                if not chunk.endswith("}"):
                    continue
                try:
                    obj = json.loads(chunk)
                except Exception:                         # noqa: BLE001
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
                break
            i = start + 1
    return out


def parse_answer(text: str) -> tuple[str | None, str]:
    """返回 (选项字母或 None, 说明)。"""
    for obj in extract_objects(text):
        raw = obj.get("answer")
        if raw is None and len(obj) == 1:
            raw = next(iter(obj.values()))
        if raw is None:
            continue
        s = str(raw).strip().upper()
        m = re.match(r"^([A-D])\b", s)
        if m:
            return m.group(1), "json"
    m = re.search(r'(?i)"answer"\s*:\s*["\']?([A-D])', text)
    if m:
        return m.group(1).upper(), "regex"
    s = text.strip().upper()
    if s in ("A", "B", "C", "D"):
        return s, "bare"
    return None, "unparsed"


# ────────────────────────────────────────────────────────────────────────── #
# 配置
# ────────────────────────────────────────────────────────────────────────── #
def load_env_file(path: str) -> None:
    """把 .env 读进 os.environ（不覆盖已有值）。"""
    if not os.path.isfile(path):
        return
    with io.open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def _resolve_key(item: dict) -> str:
    key = str(item.get("api_key") or "").strip()
    if key.startswith("env:"):
        key = os.environ.get(key[4:].strip(), "")
    if not key:
        env_name = str(item.get("api_key_env") or "").strip()
        if env_name:
            key = os.environ.get(env_name, "")
    return key


def load_models(args) -> list[dict]:
    load_env_file(os.path.join(ROOT, ".env"))
    models: list[dict] = []
    if args.models:
        with io.open(args.models, encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
        rows = data.get("models") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SystemExit("模型 YAML 要是列表，或 {models: [...]}")
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise SystemExit(f"模型 YAML 第 {i} 项不是对象")
            item = {
                "name": str(row.get("name") or f"model-{i}"),
                "base_url": str(row.get("base_url") or "").strip(),
                "model": str(row.get("model") or "").strip(),
                "api_key": _resolve_key(row),
                "temperature": row.get("temperature", 0.2),
                "max_tokens": int(row.get("max_tokens") or 4000),
                "extra_body": row.get("extra_body") or {},
            }
            models.append(item)
    elif args.profile:
        for name in [x.strip() for x in args.profile.split(",") if x.strip()]:
            up = name.upper()
            item = {
                "name": name,
                "base_url": os.environ.get(f"{up}_BASE_URL", ""),
                "model": os.environ.get(f"{up}_MODEL", ""),
                "api_key": os.environ.get(f"{up}_API_KEY", ""),
                "temperature": 0.2,
                "max_tokens": 4000,
                "extra_body": {},
            }
            models.append(item)
    else:
        raise SystemExit("要么给 --models 模型清单，要么给 --profile 档案名（读 .env）")

    bad = [m["name"] for m in models if not (m["base_url"] and m["model"] and m["api_key"])]
    if bad:
        raise SystemExit(
            "这些模型缺 base_url / model / api_key：" + ", ".join(bad)
            + "\n（用 --profile 时，.env 里要有 <名字>_BASE_URL / _API_KEY / _MODEL）"
        )
    return models


# ────────────────────────────────────────────────────────────────────────── #
# 调用
# ────────────────────────────────────────────────────────────────────────── #
async def ask_one(client: httpx.AsyncClient, model: dict, q: dict, bank: dict,
                  timeout: float, retries: int) -> dict:
    url = model["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(bank, q)},
        ],
        "stream": False,
    }
    if model.get("temperature") is not None:
        payload["temperature"] = model["temperature"]
    if model.get("max_tokens"):
        payload["max_tokens"] = model["max_tokens"]
    payload.update(model.get("extra_body") or {})
    headers = {"Authorization": f"Bearer {model['api_key']}", "Content-Type": "application/json"}

    last_err = ""
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = f"HTTP {r.status_code}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return {"answer": None, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                        "raw": "", "latency": round(time.time() - t0, 2)}
            data = r.json()
            content = ""
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
                if not content:
                    content = msg.get("reasoning_content") or ""
            ans, how = parse_answer(content)
            return {"answer": ans, "how": how, "error": "" if ans else "未解析出答案",
                    "raw": content[:4000], "latency": round(time.time() - t0, 2),
                    "usage": data.get("usage") or {}}
        except Exception as e:                            # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    return {"answer": None, "error": last_err, "raw": "", "latency": 0.0}


async def run_model(model: dict, qs: list[dict], bank: dict, args) -> dict:
    sem = asyncio.Semaphore(max(1, args.concurrency))
    limits = httpx.Limits(max_connections=max(2, args.concurrency),
                          max_keepalive_connections=max(2, args.concurrency))

    async with httpx.AsyncClient(limits=limits) as client:
        async def one(q: dict) -> dict:
            async with sem:
                res = await ask_one(client, model, q, bank, args.timeout, args.retries)
                ok = res.get("answer") == q["answer"]
                print(f"  {model['name']:<18} {q['id']}  {'✓' if ok else '✗'}"
                      f"  {res.get('answer') or '-'} / {q['answer']}", flush=True)
                return {"id": q["id"], "expected": q["answer"], "got": res.get("answer"),
                        "ok": ok, "error": res.get("error", ""), "how": res.get("how", ""),
                        "latency": res.get("latency", 0.0), "raw": res.get("raw", "")}

        rows = await asyncio.gather(*(one(q) for q in qs))
    return {"name": model["name"], "rows": rows}


def fake_run(model: dict, qs: list[dict], correct_ratio: float) -> dict:
    """离线演练：按固定比例假装答对/答错，验证判分和报告。"""
    n_ok = round(len(qs) * correct_ratio)
    rows = []
    for i, q in enumerate(qs):
        ok = i < n_ok
        rows.append({"id": q["id"], "expected": q["answer"],
                     "got": q["answer"] if ok else ("A" if q["answer"] != "A" else "B"),
                     "ok": ok, "error": "", "how": "dry-run", "latency": 0.0, "raw": ""})
    return {"name": model["name"], "rows": rows}


# ────────────────────────────────────────────────────────────────────────── #
# 判分与报告
# ────────────────────────────────────────────────────────────────────────── #
def pct(ok: int, total: int) -> float:
    return (ok / total) if total else 0.0


def verdict_basic(rate: float) -> str:
    if rate >= BASIC_PASS:
        return "✅ 通过（≥95%）"
    if rate >= BASIC_WARN:
        return "⚠️ 边缘（90%~95%，只差一点，建议换模型或重测）"
    return "❌ 不建议（<90%，别用这个模型玩本项目）"


def verdict_strength(rate: float) -> str:
    if rate >= 0.90:
        return "A · 强"
    if rate >= 0.75:
        return "B · 可玩"
    if rate >= 0.60:
        return "C · 偏弱（建议开 game.fallback）"
    return "D · 不建议"


def score(run: dict, bank: dict) -> dict:
    qmap = {q["id"]: q for q in bank["questions"]}
    out = {"name": run["name"], "rows": run["rows"], "categories": {}}
    for section in ("basic", "strength"):
        ids = [q["id"] for q in bank["questions"] if q["section"] == section]
        rr = [r for r in run["rows"] if r["id"] in ids]
        ok = sum(1 for r in rr if r["ok"])
        out[section] = {"ok": ok, "total": len(rr), "rate": pct(ok, len(rr))}
        cats: dict[str, list[int]] = {}
        for r in rr:
            cat = qmap[r["id"]]["category"]
            cats.setdefault(cat, [0, 0])
            cats[cat][1] += 1
            cats[cat][0] += 1 if r["ok"] else 0
        out["categories"] = cats
    out["verdict"] = ""
    failed = [r for r in run["rows"] if (not r.get("ok")) and r.get("error")]
    if run["rows"] and len(failed) == len(run["rows"]):
        out["verdict"] = "⚠️ 全部请求失败（检查 base_url / key / 网络），这不是能力分"
    if out["basic"]["total"]:
        if out["basic"]["total"] < 30:
            out["verdict"] = f"⚠️ 基础只跑了 {out['basic']['total']}/30 题，不足以判定"
        elif not out["verdict"]:
            out["verdict"] = verdict_basic(out["basic"]["rate"])
    if out["strength"]["total"]:
        if out["strength"]["total"] < 30:
            part = f"⚠️ 实力只跑了 {out['strength']['total']}/30 题，不足以判定"
        elif out["verdict"].startswith("⚠️ 全部请求失败"):
            part = "实力分不可用"
        else:
            part = "实力 " + verdict_strength(out["strength"]["rate"])
        out["verdict"] = (out["verdict"] + "；" + part) if out["verdict"] else part
    return out


def write_report(results: list[dict], bank: dict, paper: str, out_md: str,
                 args) -> str:
    title = bank["papers"][paper]["title"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 模型考试报告 · {title}",
        "",
        f"- 时间：{stamp}",
        f"- 题库版本：{bank.get('version', '?')}；{len(paper_ids(bank, paper))} 题",
        f"- 采样：temperature={args.temperature_note}；单题超时 {args.timeout:.0f}s；并发 {args.concurrency}",
        "",
        "## 总表",
        "",
    ]
    if paper == "basic":
        lines += ["| 模型 | 基础答对 | 基础正确率 | 判定 |", "|---|---:|---:|---|"]
    elif paper == "strength":
        lines += ["| 模型 | 实力答对 | 实力正确率 | 判定 |", "|---|---:|---:|---|"]
    else:
        lines += ["| 模型 | 基础答对 | 基础正确率 | 实力答对 | 实力正确率 | 判定 |",
                  "|---|---:|---:|---:|---:|---|"]
    scored = [score(r, bank) for r in results]
    for s in scored:
        b, st = s["basic"], s["strength"]
        if paper == "basic":
            lines.append(f"| {s['name']} | {b['ok']} | {b['rate']:.0%} | {s['verdict']} |")
        elif paper == "strength":
            lines.append(f"| {s['name']} | {st['ok']} | {st['rate']:.0%} | {s['verdict']} |")
        else:
            lines.append(f"| {s['name']} | {b['ok']} | {b['rate']:.0%} | "
                         f"{st['ok']} | {st['rate']:.0%} | {s['verdict']} |")
    lines += ["", "> 基础卷 ≥95% 才建议直接玩；90%~95% 是边缘；<90% 不建议。实力卷分数越高，通常越能撑住真实对局。", ""]

    # 分类表
    lines += ["## 分类得分", ""]
    for s in scored:
        lines.append(f"### {s['name']}")
        lines.append("")
        lines.append("| 类别 | 答对/总数 | 正确率 |")
        lines.append("|---|---:|---:|")
        for cat, (ok, total) in sorted(s["categories"].items(), key=lambda x: (-x[1][1], x[0])):
            lines.append(f"| {cat} | {ok}/{total} | {pct(ok, total):.0%} |")
        lines.append("")

    # 错题
    lines += ["## 错题与未答", ""]
    any_wrong = False
    for s in scored:
        wrong = [r for r in s["rows"] if not r["ok"]]
        if not wrong:
            continue
        any_wrong = True
        lines.append(f"### {s['name']}")
        lines.append("")
        lines.append("| 题号 | 类别 | 期望 | 实答 | 备注 |")
        lines.append("|---|---|:---:|:---:|---|")
        qmap = {q["id"]: q for q in bank["questions"]}
        for r in wrong:
            note = "未答" if not r["got"] else ("解析失败" if r["error"] else "选错")
            lines.append(f"| {r['id']} | {qmap[r['id']]['category']} | {r['expected']} | {r['got'] or '—'} | {note} |")
        lines.append("")
    if not any_wrong:
        lines.append("（没有错题）")
        lines.append("")

    lines += [
        "## 说明",
        "",
        "- 本工具只做客观选择题判分，不评发言质量、不跑真实对局。",
        "- 分数是参考，不是承诺；同一个模型在不同渠道、不同限流下表现会变。",
        "- 原始回答在旁边的 `.raw.json`，含每题耗时与解析方式，方便复核。",
        "",
    ]
    text = "\n".join(lines)
    os.makedirs(os.path.dirname(out_md) or ".", exist_ok=True)
    io.open(out_md, "w", encoding="utf-8", newline="\n").write(text)

    raw_path = os.path.splitext(out_md)[0] + ".raw.json"
    io.open(raw_path, "w", encoding="utf-8", newline="\n").write(
        json.dumps({"paper": paper, "bank_version": bank.get("version"),
                    "time": stamp, "models": scored}, ensure_ascii=False, indent=2))
    return raw_path


# ────────────────────────────────────────────────────────────────────────── #
# 自检 / CLI
# ────────────────────────────────────────────────────────────────────────── #
def selftest() -> int:
    bank = load_bank()
    errs: list[str] = []
    qs = bank.get("questions") or []
    if len(qs) != 60:
        errs.append(f"题量不是 60：{len(qs)}")
    ids = [q.get("id") for q in qs]
    if len(set(ids)) != len(ids):
        errs.append("题号有重复")
    basic = [q for q in qs if q.get("section") == "basic"]
    strength = [q for q in qs if q.get("section") == "strength"]
    if len(basic) != 30:
        errs.append(f"基础卷不是 30 题：{len(basic)}")
    if len(strength) != 30:
        errs.append(f"实力卷不是 30 题：{len(strength)}")
    for q in qs:
        if q.get("answer") not in (q.get("options") or {}):
            errs.append(f"{q.get('id')} 答案不在选项里")
        if len(q.get("options") or {}) != 4:
            errs.append(f"{q.get('id')} 选项不是 4 个")
        if not q.get("why"):
            errs.append(f"{q.get('id')} 缺解析")
    p = bank.get("papers") or {}
    if len(p.get("basic", {}).get("questions", [])) != 30:
        errs.append("papers.basic 不是 30 题")
    if len(p.get("strength", {}).get("questions", [])) != 30:
        errs.append("papers.strength 不是 30 题")
    if set(p.get("full", {}).get("questions", [])) != set(ids):
        errs.append("papers.full 不等于全部 60 题")
    if not bank.get("rule_digest"):
        errs.append("缺 rule_digest")

    # 判分自检：28/30 必须是边缘、26/30 必须不建议
    fake_basic = {"name": "selftest", "rows": [
        {"id": q["id"], "expected": q["answer"], "got": q["answer"], "ok": True,
         "error": "", "how": "selftest", "latency": 0, "raw": ""} for q in basic
    ]}
    if verdict_basic(1.0) != "✅ 通过（≥95%）":
        errs.append("100% 判定不对")
    if "边缘" not in verdict_basic(28 / 30):
        errs.append("28/30 判定不是边缘")
    if "不建议" not in verdict_basic(26 / 30):
        errs.append("26/30 判定不是不建议")
    if score(fake_basic, bank)["basic"]["ok"] != 30:
        errs.append("基础判分计数不对")

    if errs:
        print("❌ self-test 失败：")
        for e in errs:
            print("  -", e)
        return 1
    print("✅ self-test 通过：60 题（基础 30 / 实力 30 / 全量 60），答案与选项齐全。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 狼人杀 · 模型考试（三套卷）")
    ap.add_argument("--paper", default="basic", choices=["basic", "strength", "full"])
    ap.add_argument("--models", help="模型 YAML 清单")
    ap.add_argument("--profile", help="从 .env 读档案，逗号分隔，例如 DEEPSEEK,GLM")
    ap.add_argument("--out", help="报告路径（默认 reports/exam-<paper>-<时间>.md）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=120.0, help="单题超时秒数")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    ap.add_argument("--dry-run", action="store_true", help="不联网，用假模型演练")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    bank = load_bank()
    if args.selftest:
        return selftest()
    if args.list:
        for key, p in bank["papers"].items():
            print(f"{key:<9} {p['title']}  {len(p['questions'])} 题")
        return 0

    ids = paper_ids(bank, args.paper)
    if args.limit:
        ids = ids[:args.limit]
    qmap = {q["id"]: q for q in bank["questions"]}
    qs = [qmap[i] for i in ids]
    args.temperature_note = "0.2" if not args.dry_run else "dry-run"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_md = args.out or os.path.join(ROOT, "reports", f"exam-{args.paper}-{stamp}.md")

    if args.dry_run:
        models = [
            {"name": "fake-strong"}, {"name": "fake-mid"}, {"name": "fake-weak"},
        ]
        results = [fake_run(models[0], qs, 0.97), fake_run(models[1], qs, 0.82),
                   fake_run(models[2], qs, 0.55)]
    else:
        models = load_models(args)
        print(f"考试：{bank['papers'][args.paper]['title']}，{len(qs)} 题，{len(models)} 个模型")
        results = []
        for m in models:
            print(f"\n== {m['name']} ==")
            results.append(asyncio.run(run_model(m, qs, bank, args)))

    raw_path = write_report(results, bank, args.paper, out_md, args)
    print(f"\n报告：{out_md}\n原始：{raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())