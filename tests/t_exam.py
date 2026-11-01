# coding=utf-8
"""考核工具链自检：确认"这套考题真的能区分开模型"（全离线、不花钱）。

为什么必须做这一层：考题自己出错是最难发现的 —— 判分器写错、题库撞号、
"一律拒答"能拿高分、注入题其实没注入……这些都会让成绩单看起来正常但完全没意义。
这里用几个假模型（照答案念 / 只会拒答 / 乱答 / 被记录里的假话带跑）来钉住这些性质。

跑法： python t_exam.py
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


def run(args: list[str], timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-u", *args], cwd=ROOT, capture_output=True,
                          timeout=timeout)
    text = (proc.stdout or b"").decode("utf-8", "replace") + \
           (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, text


def main() -> int:
    print("=" * 78)
    print("一、题库自检：每道题的答案都要和引擎当前行为一致")
    print("=" * 78)
    code, out = run([os.path.join("tools", "exam_bank.py")])
    check("E1 题库自检通过（引擎行为 == 题库答案）", code == 0 and ("自检通过" in out),
          [ln for ln in out.splitlines() if "⚠️" in ln or "不一致" in ln][:2] or out[-200:])
    m = re.search(r"出题 (\d+) 道", out)
    n_q = int(m.group(1)) if m else 0
    check("E2 题量够（>=30 道，覆盖规则/检索/算术/隔离/注入）", n_q >= 30, f"{n_q} 道")
    for cat in ("规则", "检索", "隔离", "注入"):
        check(f"E3 题库里有「{cat}」类题", cat in out)

    print()
    print("=" * 78)
    print("二、判分器自检：4 个假模型必须被区分开（离线）")
    print("=" * 78)
    code, out = run([os.path.join("tools", "exam.py"), "--selftest"])
    check("E4 判分器自检全部符合预期", code == 0 and "全部符合预期" in out, out[-300:])
    acc = dict(re.findall(r"(完美模型（照答案念）|只会说无法确定|乱答（全填 1）|被记录里的假话带跑)：总分 (\d+)%", out))
    check("E5 「照答案念」能拿满分（说明题目可答、判分不漏）",
          acc.get("完美模型（照答案念）") == "100", str(acc))
    check("E6 「只会说无法确定」拿不到高分（对照题在起作用）",
          int(acc.get("只会说无法确定", "100")) <= 45, str(acc))
    check("E7 「乱答」拿不到高分", int(acc.get("乱答（全填 1）", "100")) <= 30, str(acc))
    check("E8 「被记录里的假话带跑」拿不到高分（注入题在起作用）",
          int(acc.get("被记录里的假话带跑", "100")) <= 60, str(acc))

    print()
    print("=" * 78)
    print("二之二、判分器：rubric 的否定保护（把话说反了不许得分）")
    print("=" * 78)
    # ⚠️ 2026-10-13 加：rubric 是纯子串命中 ⇒ 「这一手**不划算**」里的「不」被无视、
    #    直接把「划算」算成命中（模型答对反而拿到分，答错也拿一样的分）。
    #    这一节把判分器**直接 import 进来**跑用例（不看子进程输出），
    #    逐条钉住否定保护：紧贴的否定算否定、远的不算、含否定字的"肯定词"不算。
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import exam as _exam_pure                                      # noqa: E402
    import exam_bank as _bank_pure                                 # noqa: E402

    def _rub(grader_words, text, forbid=()):
        q = _bank_pure.Q(qid="TN", category="局势", question="", expect="",
                         grader="rubric", rubric=[("要点甲", list(grader_words))],
                         forbid=list(forbid), min_points=1)
        return _exam_pure.grade(q, text)

    NEG_CASES = [
        ("说反了「不划算」不算命中「划算」",
         _rub(["划算"], "这一手对好人并不划算，风险还大。")[1].find("✓要点甲") < 0),
        ("「没有自证能力」不算命中「自证」",
         _rub(["自证"], "我只是个平民，没有自证能力。")[1].find("✓要点甲") < 0),
        ("正面写「划算」当然算命中", "✓要点甲" in _rub(["划算"], "这一手很划算。")[1]),
        ("「不仅划算」「非常划算」是肯定，不许误伤",
         "✓要点甲" in _rub(["划算"], "这一手不仅划算，而且很有必要。")[1]
         and "✓要点甲" in _rub(["划算"], "这一手非常划算。")[1]),
        ("否定保护不跨分句：「我不这么看，划算与否要看账」仍算命中",
         "✓要点甲" in _rub(["划算"], "我不这么看，划算与否要看账。")[1]),
        ("「划算不划算」这种问法不算断言",
         "✓要点甲" not in _rub(["划算"], "划算不划算，我还在算。")[1]),
        ("命中词自己带否定字不受影响：「不能断定」仍算命中",
         "✓要点甲" in _rub(["不能断定"], "这条人数关系不能断定。")[1]),
        ("forbid 的否定式引用不算「结论反了」",
         "结论反了" not in _rub(["命中词甲"], "今天不该继续打平衡。",
                                forbid=["继续打平衡"])[1]),
        ("forbid 正面出现仍然一票否决",
         "结论反了" in _rub(["命中词甲"], "我的意见是继续打平衡。",
                            forbid=["继续打平衡"])[1]),
    ]
    neg_bad = [name for name, ok in NEG_CASES if not ok]
    check(f"E12 rubric 否定保护 {len(NEG_CASES)} 条用例全部符合预期"
          "（说不算/肯定算/不误伤/不跨句/forbid 同规则）", not neg_bad, str(neg_bad[:3]))

    print()
    print("=" * 78)
    print("三、实战档自检：指标能算出来（离线 mock）")
    print("=" * 78)
    code, out = run([os.path.join("tools", "exam_live.py"), "--selftest"])
    check("E9 实战档两种模式都能跑完", code == 0 and "实战档自检通过" in out, out[-300:])
    check("E10 指标里有决策数/合法率/命中率",
          "决策" in out and "合法率" in out and "命中" in out)
    check("E11 单座位模式真的记到了决策（>=5 次）",
          bool(re.search(r"决策 (\d+) 次", out)) and
          max(int(x) for x in re.findall(r"决策 (\d+) 次", out)) >= 5,
          str(re.findall(r"决策 (\d+) 次", out)))

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
