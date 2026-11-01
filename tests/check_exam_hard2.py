# coding=utf-8
"""第二批高难度题库（`tools/exam_hard2.py`）的闸门。

为什么要它：这批题（J 局势 / Z2 决策 / R 规则）是**为了分档**才写的 ——
上一批 121/134 道被 5 家模型全对、已经退役（`tools/exam_retired.py`）。
所以闸门要守住的东西不是"答案写没写对"，而是**这批题有没有退化回送分题**：

  1. 三类题量达标（J ≥8 / Z2 ≥5 / R ≥6，总数 18~22），出题过程零告警、引擎验证零失败；
  2. 每题 `expect`（或 rubric 要点）与 `why`、`trap` 都非空 —— `trap` = **预计模型会踩的坑**，
     没有它就没法回答作者那句「这批题凭什么能分档」；
  3. 题号全局唯一、**不与已有题库撞号**（基础题库 + 第一批难题 + 退役名单），格式能被判分器认出来；
  4. **rubric 题必须双向可判**：照要点写 ⇒ 判对；只把结论写反 ⇒ 必须判错（`forbid` 一票否决）；
  5. **反送分**：任何"一律答同一个词/同一句话"的策略命中率必须接近 0（含「无法确定」=0）；
  6. **与引擎一致性**：R 类每题都重新用引擎跑一遍（引擎改了口径必须变红），
     并做**变异测试**（改坏 `king_targets` / 鸦的诅咒 ⇒ 答案必须跟着变）；
  7. R 类与所有标 `book=open` 的题必须带 `rules:` 上下文，且上下文在没有存档时也取得到正文；
  8. 中文文本只用「」（本项目规范 §8），源文件里中文串内部也不许出现 ASCII 双引号。

跑法： python tests/check_exam_hard2.py
"""
from __future__ import annotations

import io
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import exam_bank                                                  # noqa: E402
import exam_hard                                                  # noqa: E402
import exam_hard2                                                 # noqa: E402

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


def skip(name: str, why: str) -> None:
    """显式跳过（不算通过、也不算失败）—— 绝不用「跳过」冒充绿。"""
    print(f"  SKIP  {name}   | {why}")


# --------------------------------------------------------------------------- #
# 出题（一次；后面的检查都看这一份）
# --------------------------------------------------------------------------- #
print("=== 一、出题 ===")
bank = exam_hard2.build_hard2_bank()
by_qid = {q.qid: q for q in bank}
by_cat = exam_hard2.class_counts(bank)
print(f"  题库：{len(bank)} 道 —— " + "　".join(f"{c} {n} 道" for c, n in by_cat.items())
      + f"　（引擎验证 {len(exam_hard2.checks())} 条）")

bad_checks = [(qid, what) for qid, what, ok in exam_hard2.checks() if not ok]
check("1a 出题过程零告警（引擎行为和题库答案一致）", not exam_hard2.notes(),
      str(exam_hard2.notes()[:3]))
check("1b 出题过程的引擎验证全部通过", not bad_checks, str(bad_checks[:3]))
check("1c 总题量在 18~22 之间", exam_hard2.TOTAL_MIN <= len(bank) <= exam_hard2.TOTAL_MAX,
      str(len(bank)))
for cat, least in exam_hard2.MIN_PER_CLASS.items():
    check(f"1d[{cat}] 题量 ≥{least}", by_cat.get(cat, 0) >= least, f"{by_cat.get(cat, 0)} 道")

print("\n=== 二、每题的 expect / rubric / why / trap ===")
no_expect = [q.qid for q in bank if not str(q.expect).strip() and not q.rubric]
no_why = [q.qid for q in bank if not str(q.why).strip()]
no_trap = [q.qid for q in bank if not str(getattr(q, "trap", "")).strip()]
check("2a 每题都有 expect 或 rubric 要点", not no_expect, str(no_expect[:5]))
check("2b 每题 why 非空（推理链要留给人复核）", not no_why, str(no_why[:5]))
check("2c 每题都写了「预计模型会踩的坑」（分档理由）", not no_trap, str(no_trap[:5]))
rubric_qs = [q for q in bank if q.grader == "rubric"]
bad_rubric = [q.qid for q in rubric_qs
              if len(q.rubric) != exam_hard2.J_POINTS
              or any(not n.strip() or not ws for n, ws in q.rubric)]
check(f"2d rubric 题要点数都 = {exam_hard2.J_POINTS} 且写法完整", not bad_rubric,
      str(bad_rubric[:4]))
bad_need = [q.qid for q in rubric_qs if q.min_points != exam_hard2.J_NEED]
check(f"2e rubric 题的 min_points 都 = {exam_hard2.J_NEED}（60% 要点）", not bad_need,
      str(bad_need[:4]))
bad_forbid = [q.qid for q in rubric_qs if not q.forbid]
check("2f rubric 题都配了 forbid（否则「两边都抄一遍」能蒙对）", not bad_forbid,
      str(bad_forbid[:4]))
q_default = exam_bank.Q(qid="T", category="x", question="x", expect="1")
check("2g Q 的新字段默认值没破坏老行为（rubric=[] / min_points=0）",
      q_default.rubric == [] and q_default.min_points == 0 and "rubric" in exam_bank.GRADERS,
      f"{q_default.rubric} {q_default.min_points}")

print("\n=== 三、题号：唯一 / 不撞号 / 判分器认得出 ===")
dup = [k for k, v in Counter(q.qid for q in bank).items() if v > 1]
check("3a 题号在本库内唯一", not dup, str(dup[:5]))
bad_fmt = [q.qid for q in bank if not re.fullmatch(r"[A-Za-z]{1,3}\d{1,4}[a-z]?", q.qid)]
check("3b 题号符合判分器认的格式", not bad_fmt, str(bad_fmt[:5]))
try:
    _demo = exam_bank.demo_game()
    base_qs, _bp = exam_bank.build_bank(_demo, extra=((_demo, "@1", "X"),))
    hard_qs = exam_hard.build_hard_bank()
    other = {q.qid for q in base_qs} | {q.qid for q in hard_qs}
    try:
        from exam_retired import RETIRED                      # noqa: PLC0415
        other |= set(RETIRED)
    except Exception:                                         # noqa: BLE001
        pass
    clash = sorted({q.qid for q in bank} & other)
    check(f"3c 题号不与已有题库/退役名单撞号（对面 {len(other)} 个题号）", not clash,
          str(clash[:6]))
except Exception as exc:                                      # noqa: BLE001
    skip("3c 题号不与已有题库撞号", f"取已有题库失败（{type(exc).__name__}: {exc}）")

print("\n=== 四、判分器 ===")
try:
    import exam as _exam                                      # noqa: E402
    grader_ok = True
except Exception as exc:                                      # noqa: BLE001
    _exam = None
    grader_ok = False
    print(f"  ⚠️ 没导入 exam.py（{type(exc).__name__}: {exc}），判分器相关检查会 SKIP")

if grader_ok:
    unknown = [q.qid for q in bank if q.grader not in exam_bank.GRADERS]
    check("4a 每题的判分方式都是判分器支持的", not unknown, str(unknown[:4]))
    wrong = [q.qid for q in bank
             if q.grader in ("exact", "nothijack") and not _exam.grade(q, q.expect)[0]]
    check("4b exact 题把 expect 喂回去必须判对", not wrong, str(wrong[:4]))
    legal_wrong = [q.qid for q in bank if q.grader == "legal"
                   and not _exam.grade(q, q.expect)[0]]
    check("4c legal 题的最优解必须判对", not legal_wrong, str(legal_wrong[:4]))
    legal_forbid = [q.qid for q in bank if q.grader == "legal" and q.forbid
                    and _exam.grade(q, str(q.forbid[0]))[0]]
    check("4d legal 题的 forbid 必须判错（干扰项真的会扣分）", not legal_forbid,
          str(legal_forbid[:4]))

    # rubric：双向可判（照要点写 ⇒ 对；只把结论写反 ⇒ 错）
    full_bad: list[str] = []
    rev_bad: list[str] = []
    for q in rubric_qs:
        full = "。".join(ws[0] for _n, ws in q.rubric)
        if not _exam.grade(q, full)[0]:
            full_bad.append(q.qid)
        rev = f"我的结论正好相反：{q.forbid[0]}。"
        ok_rev, _why_rev = _exam.grade(q, rev)
        if ok_rev:
            rev_bad.append(q.qid)
    check("4e rubric 题：照要点清单写必须判对", not full_bad, str(full_bad[:4]))
    check("4f rubric 题：把结论写反（命中 forbid）必须判错", not rev_bad, str(rev_bad[:4]))
    half_q = rubric_qs[0]
    half = "。".join(ws[0] for _n, ws in half_q.rubric[:exam_hard2.J_NEED - 1])
    half_ok, half_why = _exam.grade(half_q, half)
    check("4g rubric 题：只差一个要点时必须判错，且说明里写清「命中几个/缺哪个」",
          (not half_ok) and ("命中" in half_why) and ("没命中" in half_why), half_why[:70])
    empty_ok, empty_why = _exam.grade(half_q, "")
    check("4h rubric 题：空回答判错", not empty_ok, empty_why)
    bad_rubric_q = exam_bank.Q(qid="T9", category="x", question="x", expect="",
                               grader="rubric")
    rb_ok, rb_why = _exam.grade(bad_rubric_q, "随便答")
    check("4i 没有要点清单的 rubric 题判错并提示题库写坏了（不静默当对）",
          not rb_ok and "题库" in rb_why, rb_why[:60])
else:
    skip("4a~4i 判分器兼容 / rubric 双向可判", "import exam.py 失败")

print("\n=== 五、反送分（不许「一律答同一个词/同一句话」蒙分）===")
WORDS = ["1", "0", "2", "3", "5", "8", "10", "能", "不能", "会", "不会", "可能", "不可能",
         "是", "不是", "无法确定", "有", "没有", "成功", "没成功", "平票", "当选", "狼",
         "好人", "毒", "不毒", "刀", "同意", "反对", "女巫", "预言家", "先投票再说",
         "先看发言和票型，再判断谁是狼，我会站边。"]
if grader_ok:
    def _rate(text: str) -> float:
        return sum(1 for q in bank if _exam.grade(q, text)[0]) / max(1, len(bank))

    rates = {w: _rate(w) for w in WORDS}
    best = max(rates.items(), key=lambda kv: kv[1])
    check("5a 任何「一律答同一个词/同一句话」的命中率 ≤15%", best[1] <= 0.15,
          f"最强的是「{best[0]}」= {best[1]:.0%}")
    check("5b 一律答「无法确定」的命中率 = 0", rates.get("无法确定") == 0.0,
          f"{rates.get('无法确定'):.0%}")
    check("5c 一律答「能」或「不能」都接近 0",
          rates.get("能", 0) <= 0.05 and rates.get("不能", 0) <= 0.05,
          f"能={rates.get('能'):.0%} 不能={rates.get('不能'):.0%}")
    check("5d 一律答数字（1/2/3/5/8/10/0）里最强的那一个 ≤15%",
          max(rates.get(w, 0) for w in ("1", "0", "2", "3", "5", "8", "10")) <= 0.15,
          str({w: f"{rates.get(w):.0%}" for w in ("0", "1", "2", "3", "5", "8", "10")}))
    check("5e 一段通用的「场面话」蒙不到分（≤15%）",
          _rate("先看发言和票型，再判断谁是狼，我会站边。") <= 0.15,
          f"{_rate('先看发言和票型，再判断谁是狼，我会站边。'):.0%}")
else:
    skip("5a~5e 反送分", "import exam.py 失败")

print("\n=== 六、与引擎一致性：R 类重跑 + 变异测试 ===")
r_qs = [q for q in bank if q.category == "规则"]
r_qids = [q.qid for q in r_qs]
no_recheck = [qid for qid in r_qids if exam_hard2.recheck_r(qid) is None]
check("6a 每道 R 题都留了 recheck 入口", not no_recheck, str(no_recheck[:4]))
mismatch = []
for qid in r_qids:
    got = exam_hard2.recheck_r(qid)
    if got != str(by_qid[qid].expect):
        mismatch.append((qid, str(by_qid[qid].expect), got))
check(f"6b 重跑引擎后 {len(r_qids)} 道 R 题的期望答案全部不变", not mismatch, str(mismatch[:3]))

import wolf.engine as _eng                                        # noqa: E402

before_king = exam_hard2.recheck_r("R202")
_orig_kt = _eng.Game.king_targets
try:
    _eng.Game.king_targets = lambda self, king: []                 # 改坏引擎：狼王谁都不能带
    after_king = exam_hard2.recheck_r("R202")
finally:
    _eng.Game.king_targets = _orig_kt
check("6c 变异测试①：改坏 king_targets（狼王谁都不能带）⇒ R202 的答案必须跟着变",
      before_king != after_king, f"正常 {before_king} → 改坏后 {after_king}")

before_crow = exam_hard2.recheck_r("R204")
_orig_cc = _eng.Game._apply_crow_curse
try:
    _eng.Game._apply_crow_curse = lambda self, crow, reply: None   # 改坏引擎：诅咒永不生效
    after_crow = exam_hard2.recheck_r("R204")
finally:
    _eng.Game._apply_crow_curse = _orig_cc
check("6d 变异测试②：改坏养鸦人的诅咒 ⇒ R204 的计票必须跟着变",
      before_crow != after_crow, f"正常 {before_crow} → 改坏后 {after_crow}")

# 6e：把「愚者双算」这一条口径拿掉（also_kind=None）⇒ 13 座那道题的屠边账必须跟着翻
import dataclasses                                                # noqa: E402

from wolf.roles import ROLES as _ROLES                            # noqa: E402

before_dual = exam_hard2.recheck_r("R208")
_orig_role = _ROLES["idiot"]
try:
    # RoleDef 是 frozen dataclass（不能直接改字段）⇒ 换一个 also_kind=None 的副本进表
    _ROLES["idiot"] = dataclasses.replace(_orig_role, also_kind=None)
    after_dual = exam_hard2.recheck_r("R208")
finally:
    _ROLES["idiot"] = _orig_role
check("6e 变异测试③：拿掉愚者双算（also_kind=None）⇒ R208 的屠边结论必须跟着变",
      before_dual != after_dual, f"正常 {before_dual} → 改坏后 {after_dual}")
check("6f 变异测试之后引擎已被还原（愚者双算还在）",
      _ROLES["idiot"].also_kind == "villager", str(_ROLES["idiot"].also_kind))

print("\n=== 七、开卷上下文（规则公告）与换题库不会静默丢题 ===")
bad_ctx = [q.qid for q in bank if not str(q.context).startswith("rules:")]
check("7a 全部题的上下文都是 rules:<板子>（开卷）", not bad_ctx, str(bad_ctx[:4]))
bad_open = [q.qid for q in bank
            if getattr(q, "book", "") == "open" and not str(q.context).startswith("rules:")]
check("7b 凡标 book=open（依赖特设机制）的题必须带 rules: 上下文", not bad_open,
      str(bad_open[:4]))
r_bad = [q.qid for q in bank
         if q.category == "规则" and getattr(q, "book", "") != "open"]
check("7c R 类（必须读本项目规则）全部标成开卷", not r_bad, str(r_bad[:4]))
if grader_ok:
    empty_ctx = [q.qid for q in bank if not _exam.context_for(q.context, [])]
    check("7d 每题上下文在没有存档时也能取到正文（换题库不会静默丢题）", not empty_ctx,
          str(empty_ctx[:4]))
    dropped = [q.qid for q in bank
               if q.grader == "rubric" and not _exam.context_for(q.context, [])]
    check("7e rubric 题同样不会因为上下文取不到而被丢掉", not dropped, str(dropped[:4]))
    groups = _exam.group_questions(bank, [], chunk=10, closed_qids=frozenset())
    kept = sum(len(g["qs"]) for g in groups)
    check("7f group_questions 之后题数不变（22 道全在）", kept == len(bank),
          f"{kept}/{len(bank)}（{len(groups)} 组）")
else:
    skip("7d~7f 上下文 / 分组", "import exam.py 失败")

print("\n=== 八、中文文本只用「」 ===")
quote_bad = [q.qid for q in bank
             if '"' in (q.question + str(q.expect) + q.why
                        + "".join(n + "".join(ws) for n, ws in q.rubric))]
check("8a 题面/答案/why/要点里没有 ASCII 双引号", not quote_bad, str(quote_bad[:4]))
_cjk = r"[\u4e00-\u9fff\u3001\u3002\u3010\u3011\uff01-\uff5e]"
src = io.open(os.path.join(ROOT, "tools", "exam_hard2.py"), encoding="utf-8").read().split("\n")
src_bad = []
for i, line in enumerate(src, 1):
    for m in re.finditer('"', line):
        if m.start() == 0 or m.start() + 1 >= len(line):
            continue
        if re.match(_cjk, line[m.start() - 1]) and re.match(_cjk, line[m.start() + 1]):
            src_bad.append(i)
            break
check("8b 源文件里中文串内部也没有 ASCII 双引号", not src_bad, str(src_bad[:4]))
no_ascii_single = [q.qid for q in bank
                   if re.search(r"[\u4e00-\u9fff]'|'[\u4e00-\u9fff]", q.question + q.why)]
check("8c 题面/why 里中文之间也没有 ASCII 单引号", not no_ascii_single,
      str(no_ascii_single[:4]))

print("\n=== 九、这批题凭什么能分档（逐题留证据）===")
j_qids = [q.qid for q in bank if q.category == "局势"]
no_forbid_j = [qid for qid in j_qids if not by_qid[qid].forbid]
check("9a 每道 J 题都有 forbid（挡住「结论反了」的答案）", not no_forbid_j,
      str(no_forbid_j[:4]))
z_no_legal = [q.qid for q in bank if q.grader == "legal" and not q.legal]
check("9b 每道 Z2 题都给了最优解集合（legal 非空）", not z_no_legal, str(z_no_legal[:4]))
z_no_forbid = [q.qid for q in bank if q.grader == "legal" and not q.forbid]
check("9c 每道 Z2 题都给了次优/干扰集合（forbid 非空）", not z_no_forbid,
      str(z_no_forbid[:4]))
z_reason = [q.qid for q in bank if q.grader == "legal" and "收益函数" not in q.why]
check("9d 每道 Z2 题的 why 里写清了收益函数（不同意可以按它重算）", not z_reason,
      str(z_reason[:4]))
spec = [q.qid for q in bank if "开卷理由" not in q.why and "闭卷理由" not in q.why]
check("9e 每题 why 里写清了「为什么开卷/为什么允许闭卷」", not spec, str(spec[:4]))
r_trap = [qid for qid in r_qids if "官方" not in by_qid[qid].why
          and "旧口径" not in by_qid[qid].why]
check("9f 每道 R 题的 why 里点名了它要挡的官方/旧口径", not r_trap, str(r_trap[:4]))
# 报告里那句「我预计它会分档」：J 类的两面题（J02/J03）必须成对且结论相反
j02, j03 = by_qid.get("J02"), by_qid.get("J03")
paired = bool(j02 and j03 and any("不毒" in w for _n, ws in j02.rubric for w in ws)
              and any("今晚必须毒" in w for _n, ws in j03.rubric for w in ws))
check("9g 「优势局求稳 / 劣势局求收益」成对出题（固定风险姿态的模型必错一道）", paired,
      "J02 求稳 / J03 求收益")

print("\n=== 十、2026-10-14 第二轮：J01 新增第 ⑤ 问（位置学）的逐句锚点 ===")
# ⚠️ 为什么单独一节：这一轮 J01 改的是**问句**（P4「位置学只值一票、不能当硬证据」原来
#    根本没问 ⇒ 答对了也拿不到分）。判据三条：
#      ① 问句里确实有这个第 ⑤ 问；
#      ② 问句**不许把答案递出去**（P4 的命中词一个都不能出现在问句里）；
#      ③ 逐句锚点：正例是模型会说的那种话（写成一句话必须命中 P4），
#         反例是「拿座位关系直接锁人」的写法（0 命中、且整题判错）。
if grader_ok:
    _j01 = by_qid["J01"]
    _ask01 = str(getattr(_j01, "ask", "")) or _j01.question
    _p4 = [str(w) for w in _j01.rubric[3][1]]
    _leak01 = [w for w in _p4 if w in _ask01]
    check("10a J01 的问句里有第 ⑤ 问（位置学），且 P4 的命中词一个都没被递出去",
          "⑤" in _ask01 and not _leak01,
          f"⑤={'⑤' in _ask01}；问句里泄漏的命中词={_leak01}")
    _a_pos = "⑤ 位置学我只当辅助，不能拿它当硬证据，最后还是看发言和票型。"
    _a_neg = "⑤ 9号挨着8号和10号，这条座位关系本身就很说明问题，我按位置直接锁 8号。"
    _pos_hits = _exam._hit_words(_a_pos, _j01.rubric[3][1])
    _neg_hits = [str(w) for _n, ws in _j01.rubric for w in _exam._hit_words(_a_neg, ws)]
    check("10b J01 的 P4 逐句锚点：说清「只当辅助」必须命中，拿位置关系直接锁人必须 0 命中",
          bool(_pos_hits) and not _neg_hits and not _exam.grade(_j01, _a_neg)[0],
          f"正例命中={_pos_hits}；反例命中={_neg_hits}")
else:
    skip("10a~10b J01 第 ⑤ 问的锚点", "import exam.py 失败")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
if not grader_ok:
    print("（注意：判分器/上下文那几条因为 import exam.py 失败被 SKIP，不算通过）")
sys.exit(1 if BAD else 0)
