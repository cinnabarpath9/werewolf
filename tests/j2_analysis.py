# coding=utf-8
"""J2 三档考核的分析器（**只读报告与 raw.json，不调用任何模型**）。

它回答四件事：
  1. 每一臂的**总分**与 **J2 单独一列**（8 道）的得分 —— 总分里 J2 只占 24%，会被稀释；
  2. 每一臂的输出 tokens / 平均耗时 / rubric 命中数分布 / 「整题抽不到答案」的情况；
  3. 逐家结论：模糊题上**高档是否反将一军**（高 vs 中 vs 关）；
  4. 与**旧 J 类（J01~J08，房规版模糊题）**的对照：高/中的相对关系是否一致。

跑法： python tests/j2_analysis.py reports/J2三档-15臂-20261013.md
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import exam                                                   # noqa: E402
import exam_hard2                                             # noqa: E402
import exam_hard3                                             # noqa: E402

REPORT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    ROOT, "reports", "J2三档-15臂-20261013.md")
RAW = REPORT[:-3] + ".raw.json" if REPORT.endswith(".md") else REPORT + ".raw.json"

J2_IDS = [q.qid for q in exam_hard3.build_hard3_bank()]
OLDJ_IDS = [q.qid for q in exam_hard2.build_hard2_bank() if q.grader == "rubric"]
BANK = {q.qid: q for q in exam_hard3.build_hard3_bank()}


# --------------------------------------------------------------------------- #
# 1) 解析报告
# --------------------------------------------------------------------------- #

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_scorecard(text: str) -> dict[str, dict]:
    """成绩单表：模型 → {总分, 输入 tokens, 输出 tokens, 平均耗时, 分类分}。"""
    out: dict[str, dict] = {}
    for line in text.split("\n"):
        if not line.startswith("|") or line.startswith("| 模型") or line.startswith("|---"):
            continue
        c = _cells(line)
        if len(c) < 5:
            continue
        name = c[0]
        if name in ("模型", "题号") or not name:
            continue
        # 末三列 = 输入 tokens / 输出 tokens / 平均耗时
        tail = c[-3:]
        if not re.match(r"^[\d,]+$", tail[0] or "x"):
            continue
        out[name] = {
            "total": int(re.sub(r"\D", "", c[-4]) or 0),
            "in_tok": int(tail[0].replace(",", "")),
            "out_tok": int(tail[1].replace(",", "")),
            "sec": float(re.sub(r"[^\d.]", "", tail[2]) or 0),
            "by_cat": {k: v for k, v in zip(
                [x for x in c[1:-4]], [x for x in c[1:-4]])},
        }
    return out


def parse_marks(text: str) -> tuple[dict[str, dict[str, bool]], dict[str, str]]:
    """难度矩阵：{模型: {题号: 答对?}} + {题号: 标定}。"""
    marks: dict[str, dict[str, bool]] = {}
    labels: dict[str, str] = {}
    header: list[str] = []
    for line in text.split("\n"):
        if line.startswith("| 题号 | 类别 | 通过率"):
            header = _cells(line)
            continue
        if not header or not line.startswith("|"):
            continue
        c = _cells(line)
        if len(c) != len(header) or not re.match(r"^[A-Za-z]{1,3}\d{1,4}[a-z]?$", c[0]):
            continue
        qid = c[0]
        labels[qid] = c[3]
        for name, cell in zip(header[4:], c[4:]):
            marks.setdefault(name, {})[qid] = ("●" in cell)
    return marks, labels


# --------------------------------------------------------------------------- #
# 2) 从 raw.json 重新判分（拿到 rubric 命中数与「整题抽不到答案」）
# --------------------------------------------------------------------------- #

def regrade(raw_path: str) -> dict[str, dict]:
    """{模型: {题号: {ok, hits, need, why, empty, err, prompt_chars, answered}}}。"""
    if not os.path.exists(raw_path):
        return {}
    data = json.load(io.open(raw_path, encoding="utf-8"))
    out: dict[str, dict] = {}
    for call in data:
        name = call.get("model") or "?"
        ans = call.get("answers") or {}
        for qid in (call.get("qids") or []):
            if qid not in BANK:
                continue
            q = BANK[qid]
            got = str(ans.get(qid) or "")
            ok, why = exam.grade(q, got)
            m = re.search(r"命中\s*(\d+)/(\d+)", why or "")
            out.setdefault(name, {})[qid] = {
                "ok": bool(ok),
                "hits": int(m.group(1)) if m else -1,
                "need": int(m.group(2)) if m else len(q.rubric),
                "why": why,
                "got": got,
                "empty": not got.strip(),
                "call_ok": bool(call.get("ok")),
                "call_empty": bool(call.get("empty")),
                "err": str(call.get("error") or ""),
                "prompt_chars": int(call.get("prompt_chars") or 0),
            }
    return out


# --------------------------------------------------------------------------- #
# 3) 报表
# --------------------------------------------------------------------------- #

def main() -> int:
    text = io.open(REPORT, encoding="utf-8").read()
    card = parse_scorecard(text)
    marks, labels = parse_marks(text)
    rg = regrade(RAW)

    print("=" * 96)
    print(f"报告：{os.path.basename(REPORT)}")
    print(f"raw ：{os.path.basename(RAW)}（{'有' if rg else '**没有**'}）")
    print(f"J2 题号：{', '.join(J2_IDS)}　　旧 J 题号：{', '.join(OLDJ_IDS)}")
    print("=" * 96)

    if not card:
        print("⚠️ 成绩单没解析出来 —— 先确认报告格式")
        return 1

    def cell(name: str, ids: list[str]) -> tuple[int, int, str]:
        m = marks.get(name) or {}
        got = [m[i] for i in ids if i in m]
        return sum(got), len(got), " ".join("●" if m.get(i) else "○" for i in ids)

    print("\n## 一、每一臂：总分 / J2 单独一列 / 旧 J 单独一列")
    print(f"{'模型':<10}{'总分':>7}{'J2(8道)':>12}{'J2 逐题':>34}{'旧J(8道)':>12}{'旧J 逐题':>34}"
          f"{'输入tok':>10}{'输出tok':>9}{'均耗时':>8}")
    rows = []
    for name in card:
        ok_all, n_all, _ = cell(name, list(marks.get(name) or {}))
        j2o, j2n, j2s = cell(name, J2_IDS)
        jo, jn, js = cell(name, OLDJ_IDS)
        c = card[name]
        rows.append((name, c, j2o, j2n, j2s, jo, jn, js))
        print(f"{name:<10}{c['total']:>6}%{f'{j2o}/{j2n}':>12}{j2s:>34}"
              f"{f'{jo}/{jn}':>12}{js:>34}{c['in_tok']:>10}{c['out_tok']:>9}"
              f"{c['sec']:>7.1f}s")

    print("\n## 二、J2 逐题 × 逐臂（●答对 ○答错 ×抽不到答案）")
    names = [r[0] for r in rows]
    print(f"{'题号':<7}" + "".join(f"{n:>10}" for n in names))
    for qid in J2_IDS:
        line = f"{qid:<7}"
        for n in names:
            if qid in (rg.get(n) or {}):
                d = rg[n][qid]
                line += f"{('●' if d['ok'] else '×' if d['empty'] else '○'):>10}"
            else:
                line += f"{('●' if (marks.get(n) or {}).get(qid) else '○'):>10}"
        print(line)
    print(f"{'标定':<7}" + "".join(
        f"{(labels.get(qid, '')[:4] or '-'):>10}" for qid in J2_IDS))

    print("\n## 三、rubric 命中数分布（只看 J2；每道题 5 个要点、命中 3 个算对）")
    print(f"{'模型':<10}{'0':>5}{'1':>5}{'2':>5}{'3':>5}{'4':>5}{'5':>5}"
          f"{'抽不到':>8}{'调用失败':>9}{'平均命中':>10}")
    for n in names:
        d = (rg.get(n) or {})
        js = [d[q] for q in J2_IDS if q in d]
        dist = {k: sum(1 for x in js if x["hits"] == k) for k in range(6)}
        empty = sum(1 for x in js if x["empty"])
        err = sum(1 for x in js if (not x["call_ok"]) or x["err"])
        avg = (sum(max(0, x["hits"]) for x in js) / len(js)) if js else 0
        print(f"{n:<10}" + "".join(f"{dist[k]:>5}" for k in range(6))
              + f"{empty:>8}{err:>9}{avg:>10.2f}")
    print("（hits = -1 表示判分说明里没写命中数 = 整题没抽到答案/题库写坏；已计入「0」列）")

    print("\n## 四、逐家对照：高档 vs 中档 vs 关档（J2 一列）")
    fams = ["GLM", "豆包", "DS", "Haiku", "MiMo"]
    print(f"{'家':<8}{'关':>8}{'中':>8}{'高':>8}   {'高-中':>7}{'高-关':>7}   "
          f"{'旧J 关':>8}{'旧J 中':>8}{'旧J 高':>8}  {'旧J 高-中':>10}   结论")
    for f in fams:
        get = lambda tier: next((r for r in rows if r[0] == f + "-" + tier), None)
        gk, gm, gh = get("关"), get("中"), get("高")
        if not (gk and gm and gh):
            print(f"{f:<8}  ⚠️ 三档没凑齐")
            continue
        dj = gh[2] - gm[2]
        dk = gh[2] - gk[2]
        do = gh[5] - gm[5]
        verdict = ("高档更好 ✅" if dj > 0 else
                   "高档更差 ✗" if dj < 0 else "高档与中档持平 ＝")
        same = ("新旧一致" if (dj > 0) == (do > 0) and dj != 0 and do != 0
                else "新旧不一致" if dj != 0 and do != 0 else "有一边持平，比不了")
        print(f"{f:<8}{gk[2]:>6}/8{gm[2]:>6}/8{gh[2]:>6}/8   {dj:>+7}{dk:>+7}   "
              f"{gk[5]:>6}/8{gm[5]:>6}/8{gh[5]:>6}/8  {do:>+10}   {verdict}；{same}")

    print("\n## 五、J2 逐题通过率（几个臂答对 / 参与的臂数）")
    n_models = len(names)
    for qid in J2_IDS:
        cnt = sum(1 for n in names
                  if (rg.get(n, {}).get(qid, {}).get("ok")
                      if qid in (rg.get(n) or {}) else (marks.get(n) or {}).get(qid)))
        bar = "█" * cnt + "·" * (n_models - cnt)
        print(f"  {qid}  {cnt:>2}/{n_models}  {bar}   {labels.get(qid, '')}")

    print("\n## 六、抽不到答案 / 调用失败的每一次（要单独看，不能混进「答错」）")
    bad = 0
    for n in names:
        for qid, d in sorted((rg.get(n) or {}).items()):
            if d["empty"] or not d["call_ok"] or d["err"]:
                bad += 1
                print(f"  {n:<10}{qid:<7} empty={d['empty']} call_ok={d['call_ok']} "
                      f"err={d['err'][:70]!r} got={d['got'][:40]!r}")
    if not bad:
        print("  （没有：19 次调用 × 15 臂全部拿到了非空回复）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
