# coding=utf-8
"""高难度题库（`tools/exam_hard.py`）的闸门。

为什么要它：老题库的毛病**不是「答案写错了」，而是「题本身不能分档」** ——
开卷检索式的二选一、一律能答「无法确定」、答案与引擎脱节（引擎改了答案还留着）。
这个闸门把规格书 §4 那几条判据落成可执行的检查，任何一条红都说明新题库退化：

 1. 六类（轮次/边界/对账/决策/13人/查规则）**每类 ≥6 道**，且出题过程零告警；
 2. 每题 `expect` / `why` 非空、题号唯一且**能被 `exam.py` 的 `parse_answers` 认出来**；
 3. `engine_checked=True` 的题**必须真有校验代码路径** ——
    除了登记表，还做**变异测试**：把引擎改坏，答案必须跟着变（写死的答案过不了这一关）；
 4. **反送分**：任何单一固定答案占比 ≤30%，任何「一律答同一个词」的策略命中率 ≤50%，
    「无法确定」的命中率必须是 0；
 5. **反饱和**：答案/结论字样不许直接出现在题干的局面里（问句不算 —— 不点出动作就没法问）；
 6. **与引擎一致性**：随机抽 ≥5 道 W 类题，**重新构造局面 + 重新用引擎推导**；
 7. **13 人场基线现算**：闸门自己再枚举一遍 13 座的类型排列，与题目答案对齐，
    并且明确检查**没有沿用 11 座的 36.4%**；
 8. **开卷/闭卷口径**（规格书 §1.5）：X 类、V 类**必须**带 `rules:` 规则上下文；
    凡标 `book="open"`（依赖本项目特设机制）的题也必须带；每题 why 里要写清理由；
 9. **判分器兼容**：把所有题的 `expect` 喂给现有判分器必须判对，`legal` 题的 `forbid` 必须判错；
10. **换题库不会静默丢题**：每题上下文都能在**没有存档**的情况下取到正文；
11. 中文文本里不许出现 ASCII 双引号（本项目规范 §8，只用「」）。

⚠️ 与 `exam.py` 有关的几条（判分器、`context_for`）在 import 失败时**显式 SKIP**并说明原因
（`exam.py` 由另一个会话在改；这里绝不假装通过）。
"""
from __future__ import annotations

import io
import itertools
import os
import random
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import exam_hard                                                  # noqa: E402
from exam_hard import MIN_PER_CLASS                               # noqa: E402
import exam_bank                                                  # noqa: E402

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
bank = exam_hard.build_hard_bank()
by_qid = {q.qid: q for q in bank}
by_cat: dict[str, int] = {}
for q in bank:
    by_cat[q.category] = by_cat.get(q.category, 0) + 1
print(f"  题库：{len(bank)} 道 —— " + "　".join(f"{c} {by_cat.get(c, 0)}"
                                            for c in exam_hard._CLASSES))

check("1a 出题过程零告警（引擎行为与题库答案一致）",
      not exam_hard.notes(), str(exam_hard.notes()[:3]))
check("1b 题量 ≥ 6 类 × 6 道", len(bank) >= MIN_PER_CLASS * 6, str(len(bank)))
for cat in exam_hard._CLASSES:
    check(f"2[{cat}] 题量 ≥{MIN_PER_CLASS}", by_cat.get(cat, 0) >= MIN_PER_CLASS,
          f"{by_cat.get(cat, 0)} 道")

print("\n=== 三、每题的 expect / why / 题号 ===")
no_expect = [q.qid for q in bank if not str(q.expect).strip()]
no_why = [q.qid for q in bank if not str(q.why).strip()]
dup = [k for k, v in Counter(q.qid for q in bank).items() if v > 1]
check("3a 每题 expect 非空", not no_expect, str(no_expect[:5]))
check("3b 每题 why 非空（推理链要留给人复核）", not no_why, str(no_why[:5]))
check("3c 题号全局唯一", not dup, str(dup[:5]))
# 题号必须能被 exam.parse_answers 的 `[A-Za-z]{1,3}\d{1,4}[a-z]?` 认出来，否则模型答了也判不到
bad_qid = [q.qid for q in bank if not re.fullmatch(r"[A-Za-z]{1,3}\d{1,4}[a-z]?", q.qid)]
check("3d 题号符合判分器认的格式", not bad_qid, str(bad_qid[:5]))

print("\n=== 四、engine_checked 的题真的有校验代码路径 ===")
checked = [q for q in bank if q.engine_checked]
verified = exam_hard.verified_qids()
check("4a engine_checked=True 的题全部登记在现算登记表里",
      all(q.qid in verified for q in checked), f"{len(checked)} 道现算 / {len(verified)} 条登记")
if not [q for q in bank if not q.engine_checked]:
    check("4b 没有「人工写死答案」的题（本库全部要求引擎现算）", True, "全部 engine_checked")
else:
    check("4b 没有「人工写死答案」的题（本库全部要求引擎现算）", False,
          str([q.qid for q in bank if not q.engine_checked][:4]))

import wolf.engine as _eng                                        # noqa: E402

before_ctl = exam_hard.recheck_w("W02")
_orig_gcsk = _eng.Game.good_can_still_kill
try:
    _eng.Game.good_can_still_kill = lambda self: True              # 改坏引擎：好人永远有反击
    after_ctl = exam_hard.recheck_w("W02")
finally:
    _eng.Game.good_can_still_kill = _orig_gcsk
check("4c 变异测试①：改坏 good_can_still_kill，控场题的期望答案必须跟着变",
      before_ctl != after_ctl, f"正常 {before_ctl} → 改坏后 {after_ctl}")

before_gap = exam_hard.recheck_w("W04")
_orig_sides = _eng.Game._sides_done
try:
    _eng.Game._sides_done = lambda self, good: "村民全部出局"          # 改坏引擎：屠边永远成立
    after_gap = exam_hard.recheck_w("W04")
finally:
    _eng.Game._sides_done = _orig_sides
check("4d 变异测试②：改坏 _sides_done，屠边账题的期望答案必须跟着变",
      before_gap != after_gap, f"正常 {before_gap} → 改坏后 {after_gap}")

print("\n=== 五、反送分（不许「一律答某个词就能过半」）===")
# 判分器：优先用真的 `exam.py`；import 不到就用一份等价的最小实现（并 SKIP 掉判分器那几条）
try:
    import exam as _exam                                          # noqa: E402
    _norm = _exam.norm
    _grade = _exam.grade
    grader_ok = True
except Exception as _exc:                                          # noqa: BLE001
    _exam = None
    grader_ok = False
    print(f"  ⚠️ 没导入 exam.py（{type(_exc).__name__}: {_exc}），判分器相关检查会 SKIP")

    def _norm(value) -> str:                                       # 只为下面的统计兜底
        text = str(value or "").strip()
        digits = re.findall(r"\d+", text)
        return digits[0] if digits else re.sub(r"[^\u4e00-\u9fff]", "", text)

    def _grade(q, answer):                                        # noqa: ANN001
        got, want = _norm(answer), _norm(q.expect)
        if q.grader == "legal":
            try:
                return (bool(got) and int(got) in [int(x) for x in q.legal]), ""
            except ValueError:
                return False, "不是数字"
        return (got == want and got != ""), ""


def _rate(word: str) -> float:
    hits = sum(1 for q in bank if _grade(q, word)[0])
    return hits / max(1, len(bank))


cnt = Counter(_norm(q.expect) for q in bank)
top_word, top_n = cnt.most_common(1)[0]
check("5a 单一固定答案的占比 ≤30%", top_n / len(bank) <= 0.30,
      f"最多的是「{top_word}」{top_n} 道 = {top_n / len(bank):.0%}")
best_word, best_rate = "", 0.0
for word in sorted({_norm(q.expect) for q in bank} | {"1", "无法确定", "能", "不会"}):
    r = _rate(word)
    if r > best_rate:
        best_word, best_rate = word, r
check("5b 任何「一律答同一个词」的策略命中率 ≤50%", best_rate <= 0.50,
      f"最强的是「{best_word}」= {best_rate:.0%}")
check("5c 一律答「无法确定」的命中率 = 0", _rate("无法确定") == 0.0,
      f"{_rate('无法确定'):.0%}")
check("5d 「无法确定」类的题一道都没有（老题库就是靠它白拿一半分）",
      not [q.qid for q in bank if q.grader == "refuse"], str(len(bank)))

print("\n=== 六、反饱和：题干局面里不许直接出现答案 ===")
generic_verbs = ("带人", "带走", "开枪", "摄梦", "会死", "获胜", "赢下", "上警", "计票", "有效票")
leaks: list[tuple[str, str]] = []
for q in bank:
    forms = set(getattr(q, "giveaways", []) or [])
    expect = str(q.expect)
    numeric = bool(re.fullmatch(r"-?\d+(\.\d+)?%?", expect))
    if not numeric:
        forms.add(expect)
        for verb in generic_verbs:
            forms.add(expect + verb)
        forms.add("答案是" + expect)
    else:
        head = expect.rstrip("%").split(".")[0]
        forms |= {f"答案是 {head}", f"答案是{head}", f"只有 {head} 号", f"必须 {head} 号",
                  f"{head} 号才能", f"唯一是 {head}"}
    for form in forms:
        if form and form in getattr(q, "scenario", ""):
            leaks.append((q.qid, form))
check("6a 答案字样不出现在题干局面里", not leaks, str(leaks[:4]))
check("6b 每题都留下了「局面」那一段（供 6a 检查 + 供人复核）",
      all(len(getattr(q, "scenario", "")) >= 20 for q in bank),
      str([q.qid for q in bank if len(getattr(q, "scenario", "")) < 20][:4]))

print("\n=== 七、与引擎一致性：随机抽 ≥5 道 W 类重跑引擎 ===")
w_ids = [q.qid for q in bank if q.category == "轮次"]
pick = random.Random(20261011).sample(w_ids, min(5, len(w_ids)))
mismatch = []
for qid in pick:
    got = exam_hard.recheck_w(qid)
    if got != by_qid[qid].expect:
        mismatch.append((qid, by_qid[qid].expect, got))
check("7a 重跑引擎后 W 类期望答案不变", not mismatch, f"抽查 {pick} ｜ {mismatch[:3]}")
check("7b 抽查数量 ≥5", len(pick) >= 5, str(len(pick)))
amb = [q.qid for q in bank if q.category == "轮次" and getattr(q, "recheck", None) is None]
check("7c 每道 W 题都留了 recheck 入口（闸门才能重跑）", not amb, str(amb[:4]))

print("\n=== 八、13 人场基线必须现算，且不许沿用 11 座的 36.4% ===")
N13, W13, G13 = 13, 5, 5
total = p_wolf = p_three_good = p_second = p_three_gods = given = 0
for wolves in itertools.combinations(range(N13), W13):
    rest = [i for i in range(N13) if i not in wolves]
    for gods in itertools.combinations(rest, G13):
        total += 1
        kind = ["民"] * N13
        for i in wolves:
            kind[i] = "狼"
        for i in gods:
            kind[i] = "神"
        if kind[0] == "狼":
            p_wolf += 1
            given += 1
            if kind[1] == "狼":
                p_second += 1
        if all(kind[i] != "狼" for i in range(3)):
            p_three_good += 1
        if any(all(kind[(s + j) % N13] == "神" for j in range(3)) for s in range(N13)):
            p_three_gods += 1
gate_stats = {
    "P(1号是狼)": p_wolf / total,
    "P(1、2、3号全是好人)": p_three_good / total,
    "P(1号是狼→2号也是狼)": p_second / given,
    "全场至少一处3连神(成圈)": p_three_gods / total,
}
check("8a 枚举总数 = C(13,5)×C(8,5)", total == 72072, str(total))
mapping = {"S06": "P(1号是狼)", "S07": "P(1、2、3号全是好人)",
           "S08": "P(1号是狼→2号也是狼)", "S10": "全场至少一处3连神(成圈)"}
diff = []
for qid, key in mapping.items():
    q = by_qid.get(qid)
    if q is None:
        diff.append((qid, "题不在了"))
        continue
    want = gate_stats[key] * 100
    got = float(str(q.expect).rstrip("%"))
    if abs(got - want) > 0.06:
        diff.append((qid, key, q.expect, f"{want:.2f}%"))
check("8b 基线题的答案与闸门独立枚举一致", not diff, str(diff[:3]))
try:
    import meta_odds                                              # noqa: E402
    e11 = meta_odds.exact()
    p11_wolf = e11["P(某位是狼)"]
except Exception as exc:                                          # noqa: BLE001
    p11_wolf = 4 / 11
    print(f"  （没能跑 meta_odds.exact()：{type(exc).__name__}: {exc}，改用解析值 4/11）")
check("8c 13 座的「某位是狼」= 5/13 ≈ 38.5%，**不是** 11 座的 36.4%",
      abs(gate_stats["P(1号是狼)"] - 5 / 13) < 1e-9
      and abs(gate_stats["P(1号是狼)"] - p11_wolf) > 0.01
      and str(by_qid["S06"].expect) != "36.4%",
      f"13 座 {gate_stats['P(1号是狼)'] * 100:.2f}% vs 11 座 {p11_wolf * 100:.2f}%")
check("8d 13 人那几道题的基线数字**各不相同**（不是把一个数抄了四遍）",
      len({str(by_qid[q].expect) for q in mapping if q in by_qid}) == 4,
      str([by_qid[q].expect for q in mapping if q in by_qid]))

print("\n=== 九、开卷 / 闭卷口径（规格书 §1.5：项目特设规则必须开卷）===")
for cat in ("边界", "查规则"):
    qs = [q for q in bank if q.category == cat]
    bad = [q.qid for q in qs if not str(q.context).startswith("rules:")]
    check(f"9a[{cat}] 这一类**全部**带 rules: 规则上下文", not bad, str(bad[:4]))
bad_open = [q.qid for q in bank
            if getattr(q, "book", "") == "open" and not str(q.context).startswith("rules:")]
check("9b 凡标 book=open（依赖特设机制）的题必须带 rules: 上下文", not bad_open, str(bad_open[:4]))
no_reason = [q.qid for q in bank
             if ("开卷理由" not in q.why and "闭卷理由" not in q.why)]
check("9c 每题 why 里写清了「为什么开卷/为什么允许闭卷」", not no_reason, str(no_reason[:4]))
bad_book = [q.qid for q in bank if getattr(q, "book", "") not in ("open", "closed")]
check("9d 每题的 book 标记都在 open/closed 里", not bad_book, str(bad_book[:4]))
closed_n = sum(1 for q in bank if getattr(q, "book", "") == "closed")
check("9e 闭卷只留给纯官方机制，且量不大（少于全库一半）", closed_n < len(bank) / 2,
      f"闭卷 {closed_n} / 开卷 {len(bank) - closed_n}")

print("\n=== 十、判分器兼容 ===")
if grader_ok:
    wrong = []
    for q in bank:
        ok, why = _grade(q, q.expect)
        if not ok:
            wrong.append((q.qid, str(q.expect)[:20], why[:40]))
    check("10a 把所有题的 expect 喂给现有判分器，必须判对", not wrong, str(wrong[:3]))
    legal_bad = []
    for q in bank:
        if q.grader == "legal" and q.forbid:
            ok, _why = _grade(q, str(q.forbid[0]))
            if ok:
                legal_bad.append(q.qid)
    check("10b legal 题的 forbid 必须被判错（干扰项真的会扣分）", not legal_bad,
          str(legal_bad[:3]))
    unknown = [q.qid for q in bank if q.grader not in exam_bank.GRADERS]
    check("10c 每题的判分方式都是现有判分器支持的", not unknown, str(unknown[:4]))
else:
    skip("10a/10b/10c 判分器兼容", "import exam.py 失败（另一个会话在改它）")

if grader_ok:
    empty_ctx = [q.qid for q in bank if not _exam.context_for(q.context, [])]
    check("11 每题上下文在**没有存档**的情况下也能取到正文（换题库不会静默丢题）",
          not empty_ctx, str(empty_ctx[:4]))
else:
    skip("11 上下文可解析", "import exam.py 失败")

print("\n=== 十二、中文文本只用「」（本项目规范 §8）===")
quote_bad = [q.qid for q in bank if '"' in (q.question + str(q.expect) + q.why)]
check("12a 题面/答案/why 里没有 ASCII 双引号", not quote_bad, str(quote_bad[:4]))
_cjk = r"[\u4e00-\u9fff\u3001\u3002\u3010\u3011\uff01-\uff5e]"
src = io.open(os.path.join(ROOT, "tools", "exam_hard.py"), encoding="utf-8").read().split("\n")
src_bad = []
for i, line in enumerate(src, 1):
    for m in re.finditer('"', line):
        if m.start() == 0 or m.start() + 1 >= len(line):
            continue
        if re.match(_cjk, line[m.start() - 1]) and re.match(_cjk, line[m.start() + 1]):
            src_bad.append(i)
            break
check("12b 源文件里中文串内部也没有 ASCII 双引号", not src_bad, str(src_bad[:4]))

print("\n=== 十三、同一条口径在题里前后不矛盾 ===")
incons: list[str] = []
x04, x07 = by_qid["X04"].expect, by_qid["X07"].expect
if x04 == "能" and x07 != "0":
    incons.append(f"X04 说被毒能带人（{x04}），X07 却说有 {x07} 种不能带")
if str(by_qid["X01"].expect) != "1" or by_qid["X02"].expect != "不会" \
        or str(by_qid["X03"].expect) != "0":
    incons.append("首夜无刀那一组自相矛盾："
                  f"X01={by_qid['X01'].expect} X02={by_qid['X02'].expect} "
                  f"X03={by_qid['X03'].expect}")
if by_qid["X06"].expect != "没有":
    incons.append("X06 说狼王名单里有狼队友，与 king_targets 的硬规则冲突")
v05, v06 = by_qid["V05"].expect, by_qid["V06"].expect
if v05 == v06:
    incons.append(f"V05/V06 是同一句话在不同板子上，答案不该相同（都是 {v05}）")
check("13 同一口径的题互相不矛盾（狼王那一口 / 首夜无刀 / 屠边双算）", not incons,
      str(incons[:3]))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
if not grader_ok:
    print("（注意：判分器/上下文那几条因为 import exam.py 失败被 SKIP，不算通过）")
sys.exit(1 if BAD else 0)
