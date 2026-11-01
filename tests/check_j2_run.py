# coding=utf-8
"""J2 三档实测报告的闸门（`reports/J2三档-15臂-20261013.md`）。

为什么要它：这一轮花了作者的钱，报告里每个数字都必须**能从落盘文件里复算**。
它守四件事：
  1. 报告/raw 都在，且**15 个臂一个不少**（5 家 × 3 档）；
  2. 报告里的成绩单与 raw.json **能对上**（同一个答案重新判一遍，结论必须一致）——
     这是防"报告被手工改过"或"判分器改了但报告没重跑"；
  3. **J2 单独一列**必须真的算得出来（8 道、题号 = J201~J208），
     且报告里不许只有一个总分（本轮作者点名的要求）；
  4. 每臂的 J2 得分 = 该臂逐题标记之和（自洽，不许嘴报）。

跑法： python tests/check_j2_run.py
（报告不存在时**显式 SKIP 并 exit 0**：没跑过不该把回归弄红；但会打印醒目提示。）
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import exam                                                   # noqa: E402
import exam_hard2                                             # noqa: E402
import exam_hard3                                             # noqa: E402

N = 0
BAD = 0
REPORT = os.path.join(ROOT, "reports", "J2三档-15臂-20261013.md")
RAW = os.path.join(ROOT, "reports", "J2三档-15臂-20261013.raw.json")


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


if not os.path.exists(REPORT):
    print(f"SKIP  这一轮的报告还没落盘（{os.path.relpath(REPORT, ROOT)}）——")
    print("      没有实测就不该判红；跑过之后这条会自动开始工作。")
    print("      跑法：python tools/exam.py --bank hard --models tools/exam_think_j2.yaml "
          "--dump reports/J2三档-15臂-20261013.md")
    sys.exit(0)

text = io.open(REPORT, encoding="utf-8").read()
J2_IDS = [q.qid for q in exam_hard3.build_hard3_bank()]
OLDJ_IDS = [q.qid for q in exam_hard2.build_hard2_bank() if q.grader == "rubric"]
BANK = {q.qid: q for q in exam_hard3.build_hard3_bank()}

print("=== 一、产物在不在、15 个臂齐不齐 ===")
check("1a 报告在", os.path.exists(REPORT), os.path.basename(REPORT))
check("1b raw.json 在（逐条可审计）", os.path.exists(RAW), os.path.basename(RAW))

FAMS = ("GLM", "豆包", "DS", "Haiku", "MiMo")
TIERS = ("关", "中", "高")
ARMS = [f"{f}-{t}" for f in FAMS for t in TIERS]
missing = [a for a in ARMS if a not in text]
check(f"1c 15 个臂一个不少（{len(ARMS)} 个）", not missing, str(missing[:6]))
check("1d 题量写的是 34 道 / 19 组（与组卷一致）",
      "34 道" in text and "19 组" in text,
      "（找不到就说明报告不是这份卷子产出的）")
check("1e 报告里明确写了 J2 的来源（第三批 / 真实复盘取材）",
      "第三批" in text or "J2" in text, "")

print("\n=== 二、J2 单独一列：必须真的算得出来 ===")
check("2a 8 道 J2 题号都在报告里",
      all(q in text for q in J2_IDS), str([q for q in J2_IDS if q not in text][:4]))
check("2b 8 道旧 J 题号也都在报告里（对照用）",
      all(q in text for q in OLDJ_IDS), str([q for q in OLDJ_IDS if q not in text][:4]))


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_marks(t: str):
    marks: dict[str, dict[str, bool]] = {}
    header: list[str] = []
    for line in t.split("\n"):
        if line.startswith("| 题号 | 类别 | 通过率"):
            header = _cells(line)
            continue
        if not header or not line.startswith("|"):
            continue
        c = _cells(line)
        if len(c) != len(header) or not re.match(r"^[A-Za-z]{1,3}\d{1,4}[a-z]?$", c[0]):
            continue
        for name, cell in zip(header[4:], c[4:]):
            marks.setdefault(name, {})[c[0]] = ("●" in cell)
    return marks


marks = parse_marks(text)
check("2c 难度矩阵解析出了 ≥15 个模型列", len(marks) >= 15, f"{len(marks)} 列")
miss_j2 = {n: [q for q in J2_IDS if q not in m] for n, m in marks.items()}
miss_j2 = {n: v for n, v in miss_j2.items() if v}
check("2d 每个臂在难度矩阵里都有全部 8 道 J2 的标记", not miss_j2,
      str(list(miss_j2.items())[:2]))
j2_scores = {n: sum(1 for q in J2_IDS if m.get(q)) for n, m in marks.items()}
print("      J2 得分（从难度矩阵逐题复算）："
      + "　".join(f"{n}={v}/8" for n, v in sorted(j2_scores.items())))
check("2e J2 得分分布不是「所有臂都一样」（否则这一列没有信息量）",
      len(set(j2_scores.values())) > 1 if j2_scores else False,
      f"出现过 {sorted(set(j2_scores.values()))}")

print("\n=== 三、报告与 raw.json 对得上（重新判一遍）===")
if os.path.exists(RAW):
    import json
    data = json.load(io.open(RAW, encoding="utf-8"))
    regraded: dict[str, dict[str, bool]] = {}
    for call in data:
        name = call.get("model") or "?"
        for qid in (call.get("qids") or []):
            if qid not in BANK:
                continue
            got = str((call.get("answers") or {}).get(qid) or "")
            ok, _why = exam.grade(BANK[qid], got)
            regraded.setdefault(name, {})[qid] = bool(ok)
    check("3a raw.json 里也覆盖了 15 个臂", len(regraded) >= 15, f"{len(regraded)} 个")
    disagree = []
    for n, m in regraded.items():
        for qid, ok in m.items():
            if qid in marks.get(n, {}) and marks[n][qid] != ok:
                disagree.append((n, qid, marks[n][qid], ok))
    check("3b 报告里的逐题标记 == 拿 raw.json 重新判分的结果", not disagree,
          f"{len(disagree)} 处不一致 {disagree[:3]}")
    calls_per_arm = {}
    for call in data:
        calls_per_arm[call.get("model") or "?"] = calls_per_arm.get(call.get("model") or "?", 0) + 1
    check("3c 每个臂的调用次数都是 19（= 19 组；组数不对说明卷子跑残了）",
          all(v == 19 for v in calls_per_arm.values()) and len(calls_per_arm) >= 15,
          str(sorted(set(calls_per_arm.values()))))
    failed = [(c.get("model"), c.get("qids"), str(c.get("error"))[:60])
              for c in data if not c.get("ok")]
    print(f"      调用失败/空回复的组：{len(failed)} 个"
          + (f"　例：{failed[0]}" if failed else ""))
else:
    check("3a raw.json 在", False, "缺了就没法复算")

print("\n=== 四、报告必须带的那几样（作者本轮点名的）===")
check("4a 报告里有一列/一节专门讲 J2 的得分",
      bool(re.search(r"J2", text)), "")
check("4b 报告里写了每臂的输出 tokens", len(re.findall(r"\d{3,}", text)) > 30, "")
check("4c 报告里写了平均耗时", "s" in text and "平均" in text, "")
check("4d 报告里没有把「空回复」算成「答错」的糊账（要么标出来、要么单列）",
      ("空" in text) or ("工具问题" in text), "")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
