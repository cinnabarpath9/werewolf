# coding=utf-8
"""第三批题库（`tools/exam_hard3.py`：**J2 类 · 真实复盘取材的模糊信息判断题**）的闸门。

它要守的不是「答案写没写对」，而是**这批题有没有退化成送分题 / 有没有偷偷以结果论英雄**：

  1. 题量在 6~10 之间、出题过程零告警、每题「来源 URL / why / 陷阱 / forbid」齐全；
  2. `why` 里**必须**写清「当时的最优解 + 为什么」与「事后结果 ≠ 当时最优」，
     并且带一个**真实复盘的来源 URL**（本批题的取材凭证）；
  3. 题面九条要求逐条可查：题干非空且自成一体、只用通用规则（不许出现本项目房规字样）、
     不把真实选手名字带进来、`forbid` 能挡住「结论反了」；
  4. **要点真的可命中**：每个要点都有一条「像模型会说的话」的**正例**（写了就判对）
     与一条**反例**（说反了/说岔了就不判对）；
  5. **反送分**：任何「一律答同一个词/同一句话」的命中率必须 ≤15%；而且**逐题查**
     「把别人的答案抄过来」也不许过（交叉命中为 0）；
  6. 题号不撞号（基础题库 + 第一批 + 第二批 + 退役名单），格式能被判分器认出来；
  7. 中文文本只用「」（本项目规范 §8），源文件里中文串内部也不许出现 ASCII 双引号；
  8. **接线**：`--bank hard` 必须带上这 8 道、必须把它们放进闭卷集合、
     `exam.py` 的 `SOLO_GRADIERS` 必须让它们**一题一次调用**（不许挤在一组里）；
  9. **题面不许把「被提问的句子」写进 forbid**（2026-10-13 加）—— J205 原来在题干③里
     原样复述了「三跳里必出两张狼」，而 forbid 里也写着它 ⇒ 模型**答对**（说它不成立）
     时把这句话抄进答案，反而被一票否决（「判分自杀」）。以后同一类毛病由这条闸门拦下；
     另外每条命中词都**逐词**验一遍正例与否定式反例（5j/5k），保证否定保护真的生效；
 10. **人数自洽**（2026-10-13 加）—— 板子总人数 == 存活 + 夜里出局 + 被放逐，
     三份名单不重不漏，**同一个人不许既被夜刀又被放逐**，题面自己写的存活人数与
     角色构成都要和账对得上，题面里也不许出现板子外的座位号。

跑法： python tests/check_exam_hard3.py
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
import exam_hard3                                                 # noqa: E402

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
bank = exam_hard3.build_hard3_bank()
by_qid = {q.qid: q for q in bank}
print(f"  题库：{len(bank)} 道 —— " + "　".join(f"{c} {n} 道"
                                                for c, n in exam_hard3.class_counts(bank).items()))

check("1a 出题过程零告警", not exam_hard3.notes(), str(exam_hard3.notes()[:3]))
check(f"1b 题量在 {exam_hard3.TOTAL_MIN}~{exam_hard3.TOTAL_MAX} 之间",
      exam_hard3.TOTAL_MIN <= len(bank) <= exam_hard3.TOTAL_MAX, str(len(bank)))
check("1c 题号全部用 J2 前缀", all(q.qid.startswith(exam_hard3.QID_PREFIX) for q in bank),
      str([q.qid for q in bank if not q.qid.startswith(exam_hard3.QID_PREFIX)][:4]))
check("1d 每道题都是 rubric 判分（本批唯一的判分方式）",
      all(q.grader == "rubric" for q in bank),
      str([q.qid for q in bank if q.grader != "rubric"][:4]))
check(f"1e 每道题恰好 {exam_hard3.J_POINTS} 个要点、min_points = {exam_hard3.J_NEED}",
      all(len(q.rubric) == exam_hard3.J_POINTS and q.min_points == exam_hard3.J_NEED
          for q in bank),
      str([q.qid for q in bank
           if len(q.rubric) != exam_hard3.J_POINTS or q.min_points != exam_hard3.J_NEED][:4]))

print("\n=== 二、取材凭证：来源 URL / 当时最优 / 事后结果 / 陷阱 ===")
no_source = [q.qid for q in bank
             if not str(getattr(q, "source", "")).startswith("http")]
check("2a 每题都写了取材来源 URL", not no_source, str(no_source[:4]))
bad_src_why = [q.qid for q in bank
               if str(getattr(q, "source", "")) not in q.why]
check("2b 来源 URL 同时写进了 why（报告里逐题可查）", not bad_src_why,
      str(bad_src_why[:4]))
no_best = [q.qid for q in bank if "当时的最优解" not in q.why]
check("2c 每题的 why 都写清了「当时的最优解 + 为什么」", not no_best, str(no_best[:4]))
no_outcome = [q.qid for q in bank if "事后结果" not in q.why]
check("2d 每题的 why 都显式区分「事后结果 ≠ 当时最优」（禁止以结果论英雄）",
      not no_outcome, str(no_outcome[:4]))
no_trap = [q.qid for q in bank if not str(getattr(q, "trap", "")).strip()]
check("2e 每题都写了「预计模型会踩的坑」（分档理由）", not no_trap, str(no_trap[:4]))
no_book = [q.qid for q in bank if str(getattr(q, "book", "")) != "closed"]
check("2f 每题都是闭卷（题面自带通用规则，不附本项目规则书）", not no_book,
      str(no_book[:4]))
not_generic = [q.qid for q in bank if str(getattr(q, "mechanic", "")) != "通用"]
check("2g 每题都标了「通用」机制（不依赖本项目房规）", not not_generic,
      str(not_generic[:4]))

print("\n=== 三、题面：自成一体 / 只用通用规则 / 不带真人 / 无答案字样 ===")
no_rules = [q.qid for q in bank if "通用狼人杀规则" not in q.question]
check("3a 每题题面都自带那份「通用狼人杀规则」公告", not no_rules, str(no_rules[:4]))
no_scenario = [q.qid for q in bank
               if not str(getattr(q, "scenario", "")).strip()
               or not str(getattr(q, "ask", "")).strip()]
check("3b 每题都有非空的「局面 + 问句」两段", not no_scenario, str(no_scenario[:4]))
# 本项目房规字样不许出现在这批题里。
# ⚠️ 只扫**真的在给规则的那一段**（`exam_hard3.RULES_TEXT`）：免责声明里会点名房规
#    「本题不使用」，扫它会把声明本身当成违规（第一版就这样误报了 8 道题）。
HOUSE = ("愚者", "狼王", "摄梦人", "养鸦人", "守墓人", "幸运儿", "首夜无刀", "双算", "奶穿")


def _no_house(text: str) -> bool:
    return not any(w in text for w in HOUSE)


# ① 通用规则原文里不许有房规；② 题目自己写的部分（题面去掉公告 + why）也不许有
rules_clean = _no_house(exam_hard3.RULES_TEXT)
own_house = sorted({q.qid for q in bank
                    if not _no_house(q.question.replace(exam_hard3.RULES, ""))
                    or not _no_house(q.why)})
check("3c 通用规则原文里不含本项目房规字样", rules_clean,
      str([w for w in HOUSE if w in exam_hard3.RULES_TEXT][:4]))
check("3c2 题目自己写的部分（题面正文 + why）也不含房规字样", not own_house,
      str(own_house[:4]))
# 真实选手 / 战队名不许带进来（题面只用座位号）
names = ("JY", "申屠", "小鹿酱", "许二木", "灵宝", "雨天", "墨白", "番茄", "帷幕",
         "战队", "门派", "WPL")
name_hit = sorted({q.qid for q in bank for w in names
                   if w in getattr(q, "scenario", "")})
check("3d 题面里没有真实选手 / 战队 / 赛事名（只用座位号）", not name_hit, str(name_hit[:4]))
# 「答案字样」不许出现在局面段里（否则一眼看穿）
leak: list[tuple[str, str]] = []
for q in bank:
    scen = str(getattr(q, "scenario", ""))
    for w in (getattr(q, "giveaways", None) or []):
        if w and w in scen:
            leak.append((q.qid, w))
check("3e 局面段里不出现任何「答案字样」（giveaways）", not leak, str(leak[:4]))
no_give = [q.qid for q in bank if not (getattr(q, "giveaways", None) or [])]
check("3f 每题都声明了「不许出现在局面里」的答案字样", not no_give, str(no_give[:4]))

print("\n=== 四、题号：唯一 / 不撞号 / 判分器认得出 ===")
dup = [k for k, v in Counter(q.qid for q in bank).items() if v > 1]
check("4a 题号在本库内唯一", not dup, str(dup[:5]))
bad_fmt = [q.qid for q in bank if not re.fullmatch(r"[A-Za-z]{1,3}\d{1,4}[a-z]?", q.qid)]
check("4b 题号符合判分器认的格式", not bad_fmt, str(bad_fmt[:5]))
try:
    _demo = exam_bank.demo_game()
    base_qs, _bp = exam_bank.build_bank(_demo, extra=((_demo, "@1", "X"),))
    other = {q.qid for q in base_qs}
    other |= {q.qid for q in exam_hard.build_hard_bank()}
    other |= {q.qid for q in exam_hard2.build_hard2_bank()}
    try:
        from exam_retired import RETIRED                      # noqa: PLC0415
        other |= set(RETIRED)
    except Exception:                                         # noqa: BLE001
        pass
    clash = sorted({q.qid for q in bank} & other)
    check(f"4c 题号不与已有题库/退役名单撞号（对面 {len(other)} 个题号）", not clash,
          str(clash[:6]))
except Exception as exc:                                      # noqa: BLE001
    skip("4c 题号不与已有题库撞号", f"取已有题库失败（{type(exc).__name__}: {exc}）")

print("\n=== 五、判分器：双向可判 + 逐要点正例/反例 ===")
try:
    import exam as _exam                                      # noqa: E402
    grader_ok = True
except Exception as exc:                                      # noqa: BLE001
    _exam = None
    grader_ok = False
    print(f"  ⚠️ 没导入 exam.py（{type(exc).__name__}: {exc}），判分器相关检查会 SKIP")


def _score(q, text: str) -> tuple[bool, int]:
    """判一次分，返回 (是否判对, 命中几个要点)。判分说明里的「命中 x/y 要点」直接读回来。"""
    ok, why = _exam.grade(q, text)
    m = re.search(r"命中\s*(\d+)/(\d+)", why or "")
    return bool(ok), (int(m.group(1)) if m else -1)


def _point_hit(q, text: str, name: str) -> bool:
    """某一次判分里，指定**要点**有没有被命中（判分说明里会打 ✓要点名）。"""
    _ok, why = _exam.grade(q, text)
    return ("✓" + str(name)) in (why or "")


if grader_ok:
    unknown = [q.qid for q in bank if q.grader not in exam_bank.GRADERS]
    check("5a 每题的判分方式都是判分器支持的", not unknown, str(unknown[:4]))
    full_bad = [q.qid for q in bank
                if not _exam.grade(q, "。".join(ws[0] for _n, ws in q.rubric))[0]]
    check("5b 照要点清单第一顺位写必须判对", not full_bad, str(full_bad[:4]))
    rev_bad = []
    for q in bank:
        ok_rev, _w = _exam.grade(q, f"我的结论正好相反：{q.forbid[0]}。")
        if ok_rev:
            rev_bad.append(q.qid)
    check("5c 把结论写反（命中 forbid）必须判错 —— 一票否决", not rev_bad, str(rev_bad[:4]))
    empty_bad = [q.qid for q in bank if _exam.grade(q, "")[0]]
    check("5d 空回答一律判错", not empty_bad, str(empty_bad[:4]))
    # ⚠️ 这条是**给整卷自检兜底**的：`tools/exam.py --selftest` 的「照答案念」假模型就是
    #    拿 `q.expect` 当回答去判分（`selftest` 的 E5：完美模型必须拿满分）。如果某道题的
    #    `expect` 摘要自己命中不了 3 个要点，整卷自检就会红 —— 而那个红看起来像"工具坏了"。
    expect_bad = [q.qid for q in bank if not _exam.grade(q, str(q.expect))[0]]
    check("5i 每道题的 expect 摘要自己就能判对（照答案念必须满分，exam.py --selftest 靠它）",
          not expect_bad, str(expect_bad[:4]))
    half_q = bank[0]
    half = "。".join(ws[0] for _n, ws in half_q.rubric[:exam_hard3.J_NEED - 1])
    half_ok, half_why = _exam.grade(half_q, half)
    check("5e 只差一个要点时必须判错，且说明里写清「命中几个 / 缺哪个」",
          (not half_ok) and ("命中" in half_why) and ("没命中" in half_why), half_why[:70])

    # 逐要点：正例（像模型会说的话）必须被认出来（命中 ≥1）；
    # 反例（同义改述、一个命中词都不出现）必须命中 0。
    # ⚠️ 单条正例**不该**判「整题答对」（5 个要点只命中 1 个 ⇒ 20% < 60% 阈值），
    #    所以这里查的是「被认出来」而不是「整题判对」；整题判对由 5b 那条守着。
    pos_fail: list[tuple[str, int]] = []
    neg_fail: list[tuple[str, int]] = []
    DETAIL: list[str] = []
    for q in bank:
        for i, (_name, ws) in enumerate(q.rubric):
            pos = f"我的判断是：{ws[0]}，这就是这一轮我要做的事。"
            _ok_p, n_p = _score(q, pos)
            if n_p < 1:
                pos_fail.append((q.qid, i))
            # 反例：把这条要点**只换一种说法**讲出来（同义改述，命中词一个都不出现）
            neg_sent = q.negatives[i]
            ok_n, n_n = _score(q, neg_sent)
            if ok_n or n_n != 0:
                neg_fail.append((q.qid, i))
            DETAIL.append(f"      {q.qid}#{i + 1} 正例命中={n_p}　反例命中={n_n}"
                          f"　反例：{neg_sent[:34]}")
    check("5f 每个要点都有一条「像模型会说的话」的正例，写了就被认出来", not pos_fail,
          str(pos_fail[:4]))
    check("5g 每个要点都有一条同义改述的反例，说岔了就不判对（要点不是靠碰词）",
          not neg_fail, str(neg_fail[:4]))
    # 40 条正例拼起来 = 一个「每一项都答到位」的完整答案 ⇒ 必须判对
    combos_bad = [q.qid for q in bank
                  if not _exam.grade(
                      q, "。".join(f"我的判断是：{ws[0]}" for _n, ws in q.rubric))[0]]
    check("5h 把 40 条正例按题拼成完整答案，8 道必须全部判对", not combos_bad,
          str(combos_bad[:4]))

    # ⚠️ 2026-10-13 加（作者点名的「新增命中词要同时给正例与反例」）：
    #    上面 5f/5g 是**逐要点**的（正例只用第一个词），新补的词其实没被验过。
    #    这一条**逐词**验：每个命中词写进一句正常的话里必须命中该要点；
    #    把同一个词套进否定句（「我并不觉得…」）之后**不许**再命中 —— 这条同时
    #    把判分器的否定保护与每个词绑在一起（J202 那种"不划算被算成划算"就是它守的）。
    word_pos_bad: list[tuple[str, str]] = []
    word_neg_bad: list[tuple[str, str]] = []
    for q in bank:
        for _name, ws in q.rubric:
            for w in ws:
                if not _point_hit(q, f"我的判断是：{w}。", _name):
                    word_pos_bad.append((q.qid, w))
                # 反例：把否定词**紧贴**在这个词前面（判分器的否定保护认的正是"紧贴"，
                # 见 exam.py::_negated —— 太远的否定不算，宁可少拦不可错拦）。
                # ⚠️ 这里按**逐词**查（`exam._hits`），不按要点查：同一个要点里可能有
                #    「归票 11」和「票 11」这种互为子串的词，按要点查会被另一个词命中。
                if _exam._hits(f"我的看法是：并不{w}，这一轮我不这么打。", str(w)):
                    word_neg_bad.append((q.qid, w))
    check(f"5j 每个命中词都有正例（共 {sum(len(ws) for q in bank for _n, ws in q.rubric)} 个"
          "词，写进正常句子里必须命中本条要点）", not word_pos_bad, str(word_pos_bad[:4]))
    check("5k 每个命中词的否定式都不算命中（否定词紧贴该词时必须判 0，否定保护逐词生效）",
          not word_neg_bad, str(word_neg_bad[:4]))
    print("      —— 逐要点正/反例明细（每条要点各一行）——")
    for line in DETAIL[:12]:
        print(line)
    print(f"      （共 {len(DETAIL)} 条，此处只印前 12 条）")

    print("\n=== 六、反送分（一律答同一个词 / 抄别人的答案 都不许过）===")
    WORDS = ["1", "0", "2", "3", "4", "5", "7", "8", "9", "10", "11", "12",
             "能", "不能", "会", "不会", "可能", "不可能", "是", "不是", "无法确定",
             "有", "没有", "对", "不对", "狼", "好人", "女巫", "预言家", "守卫", "猎人",
             "对账", "票型", "轮次", "跳身份", "自证", "归票", "毒", "刀", "开毒", "不毒",
             "先看发言和票型，再判断谁是狼，我会站边。",
             "这是一道信息模糊的题，我需要更多信息才能判断，我先不站边。"]
    rates = {w: sum(1 for q in bank if _exam.grade(q, w)[0]) / max(1, len(bank))
             for w in WORDS}
    best = max(rates.items(), key=lambda kv: kv[1])
    check("6a 任何「一律答同一个词/同一句话」的命中率 ≤15%", best[1] <= 0.15,
          f"最强的是「{best[0]}」= {best[1]:.0%}")
    check("6b 一律答「无法确定」的命中率 = 0", rates.get("无法确定") == 0.0,
          f"{rates.get('无法确定'):.0%}")
    check("6c 一律答「核账」「核实对方」或「票型」都不许过半（这几个是本题库的高频概念）",
          rates.get("对账", 0) <= 0.15 and rates.get("票型", 0) <= 0.15,
          f"对账={rates.get('对账'):.0%} 票型={rates.get('票型'):.0%}")
    check("6d 一律答数字座位号里最强的那一个 ≤15%",
          max(rates.get(w, 0) for w in ("1", "0", "2", "3", "4", "5", "7", "8", "9",
                                        "10", "11", "12")) <= 0.15,
          str({w: f"{rates.get(w):.0%}" for w in ("4", "7", "9", "10")}))
    # 交叉命中：把 A 题的答案抄给 B 题，必须判错（否则「一套模板打天下」）
    cross: list[tuple[str, str]] = []
    answers = {q.qid: "。".join(ws[0] for _n, ws in q.rubric) for q in bank}
    for q in bank:
        for other_id, text in answers.items():
            if other_id != q.qid and _exam.grade(q, text)[0]:
                cross.append((q.qid, other_id))
    check("6e 把「别的题的答案」原样抄过来一律判错（交叉命中 0）", not cross,
          str(cross[:4]))
    # 关键词表本身：同一个词不许出现在两道题里（否则一个词能吃两道题的要点）
    kw_owner: dict[str, set[str]] = {}
    for q in bank:
        for _n, ws in q.rubric:
            for w in ws:
                kw_owner.setdefault(w, set()).add(q.qid)
    shared = {w: sorted(v) for w, v in kw_owner.items() if len(v) > 1}
    check("6f 同一个命中词不许跨题重复（免得一个词同时喂饱两道题）", not shared,
          str(list(shared.items())[:3]))
else:
    skip("5a~5g / 6a~6f 判分器与反送分", "import exam.py 失败")

print("\n=== 七、中文文本只用「」 ===")
quote_bad = [q.qid for q in bank
             if '"' in (q.question + str(q.expect) + q.why + str(getattr(q, "trap", ""))
                        + "".join(n + "".join(ws) for n, ws in q.rubric))]
check("7a 题面/答案/why/要点里没有 ASCII 双引号", not quote_bad, str(quote_bad[:4]))
_cjk = r"[\u4e00-\u9fff\u3001\u3002\u3010\u3011\uff01-\uff5e]"
src = io.open(os.path.join(ROOT, "tools", "exam_hard3.py"),
              encoding="utf-8").read().split("\n")
src_bad = []
for i, line in enumerate(src, 1):
    for m in re.finditer('"', line):
        if m.start() == 0 or m.start() + 1 >= len(line):
            continue
        if re.match(_cjk, line[m.start() - 1]) and re.match(_cjk, line[m.start() + 1]):
            src_bad.append(i)
            break
check("7b 源文件里中文串内部也没有 ASCII 双引号", not src_bad, str(src_bad[:4]))
no_ascii_single = [q.qid for q in bank
                   if re.search(r"[\u4e00-\u9fff]'|'[\u4e00-\u9fff]",
                                q.question + q.why + str(getattr(q, "trap", "")))]
check("7c 题面/why 里中文之间也没有 ASCII 单引号", not no_ascii_single,
      str(no_ascii_single[:4]))

print("\n=== 八、接线：--bank hard 必须带上这 8 道、且一题一次调用 ===")
if grader_ok:
    h1, p1, n1 = _exam.load_hard_bank(None)
    h2, p2, n2 = _exam.load_hard2_bank(None)
    h3, p3, n3 = _exam.load_hard3_bank(None)
    check("8a exam.load_hard3_bank 能取到题库且零问题",
          bool(h3) and not p3 and len(h3) == len(bank), f"{len(h3 or [])} 道 {p3[:2]}")
    merged, mprob, closed, note = _exam.pick_banks(
        "hard", [], [], h1, p1, n1, h2, p2, n2, h3, p3, n3)
    j2_ids = {q.qid for q in bank}
    check("8b --bank hard 会把 J2 题并进卷子", bool(merged) and j2_ids <= {q.qid for q in merged},
          f"卷子 {len(merged or [])} 道；说明：{note}")
    check("8c J2 题全部进了闭卷集合（否则会被「上下文取不到」静默丢题）",
          j2_ids <= set(closed),
          str(sorted(j2_ids - set(closed))[:4]))
    check("8d 组卷自检零问题", not mprob, str(mprob[:3]))
    groups = _exam.group_questions(merged, [], chunk=10, closed_qids=closed)
    kept = sum(len(g["qs"]) for g in groups)
    check("8e group_questions 之后题数不变（一道都不许被吞）", kept == len(merged),
          f"{kept}/{len(merged)}（{len(groups)} 组）")
    solo_ids = {g["qs"][0].qid for g in groups
                if len(g["qs"]) == 1 and g["qs"][0].grader in _exam.SOLO_GRADERS}
    check("8f rubric 判分被登记在 SOLO_GRADERS 里（一题一次调用，不挤在一组）",
          "rubric" in _exam.SOLO_GRADERS, str(_exam.SOLO_GRADERS))
    check("8g 每道 J2 题都独占一组（组内只有它一道）", j2_ids <= solo_ids,
          str(sorted(j2_ids - solo_ids)[:4]))
    # 老签名（6 个位置参数）必须一字不变地继续工作 —— tools/exam_merge.py 还在用它
    try:
        old, _op, _oc, _on = _exam.pick_banks(
            "base", [exam_bank.Q(qid="ZZ9", category="x", question="q", expect="1")], [],
            None, [], "n/a")
        old_ok = bool(old) and [q.qid for q in old] == ["ZZ9"]
    except Exception as exc:                                  # noqa: BLE001
        old_ok = False
        print(f"      ⚠️ 老签名调用抛了：{type(exc).__name__}: {exc}")
    check("8h 老签名（6 个位置参数）仍然可用（exam_merge.py 不受影响）", old_ok)
else:
    skip("8a~8h 接线", "import exam.py 失败")

print("\n=== 九、题面不许把「被提问的句子」写进 forbid（防判分自杀）===")
# ⚠️ 2026-10-13 加（本轮最值钱的一条闸门）：J205 原来在题干③里**原样复述**
#    「三跳里必出两张狼」，而 forbid 里也写着这一句 ⇒ 模型**答对**（说它不成立）
#    时把这句话抄进答案，反而被 forbid 一票否决（"判分自杀"）。
#    判据两条：
#      ① 任何一条 forbid 都不许在题面里出现到「能被原样复述」的程度
#         （整条包含，或者与题面有 ≥6 个字的连续重叠）；
#      ② 问句里用「」引起来的说法，不许和任何 forbid 互相包含（引号里的一般就是
#         出题人自己递给模型的"那个说法"，最容易被抄回去）。
_FORBID_PUNCT = r"[\s*`_（）()「」【】·、，。；：！？,.;:!?\-—…～~]+"


def _flat(s: str) -> str:
    """去掉标点/空格/markdown 记号后的纯文本（比"字面重叠"用）。"""
    return re.sub(_FORBID_PUNCT, "", str(s or ""))


def _lcs(a: str, b: str, cap: int = 8) -> int:
    """a 与 b 的最长公共子串长度（只找到 cap 就停）。"""
    best = 0
    for i in range(len(a)):
        if len(a) - i <= best:
            break
        for j in range(i + best + 1, min(len(a), i + cap) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


_selfkill: list[tuple[str, str, str]] = []
for q in bank:
    face = str(getattr(q, "scenario", "")) + str(getattr(q, "ask", ""))
    whole = _flat(face)
    for bad in q.forbid:
        b = _flat(bad)
        if not b:
            continue
        # ⚠️ 用**判分器自己的**命中判据（带否定保护）：「你自己不能守自己」这种
        #    **否定式出现**不算把这条 forbid 递给了模型（模型照抄也是否定式，不会中招）。
        if _exam is not None and _exam._hits(face, str(bad)):
            _selfkill.append((q.qid, str(bad), "整条 forbid 能被题面原样复述（非否定式）"))
            continue
        n = _lcs(b, whole)
        if n >= 6:
            _selfkill.append((q.qid, str(bad), f"与题面有 {n} 个字连续重叠"))
check("9a 没有 forbid 能被题面原样复述（否定式出现不算；≥6 字连续重叠也算）",
      not _selfkill, str(_selfkill[:3]))
_quoted_bad: list[tuple[str, str, str]] = []
for q in bank:
    for seg in re.findall(r"「([^」]+)」", str(getattr(q, "ask", ""))):
        s = _flat(seg)
        if len(s) < 4:
            continue
        for bad in q.forbid:
            b = _flat(bad)
            if b and (s in b or b in s):
                _quoted_bad.append((q.qid, seg, str(bad)))
check("9b 问句里「」引起来的说法不许和任何 forbid 互相包含", not _quoted_bad,
      str(_quoted_bad[:3]))
# 回归锚点：J205 的题干③不许再出现那句人数口诀（改回原样这条就会红）
_j205_face = str(by_qid["J205"].scenario) + str(by_qid["J205"].ask)
_j205_slogan = [w for w in ("三跳里必出两张狼", "三张里必有两张是狼", "必出两狼")
                if w in _j205_face]
check("9c 回归锚点：J205 题干里不再复述那句人数口诀", not _j205_slogan,
      str(_j205_slogan))

print("\n=== 十、人数自洽：板子总人数 = 存活 + 夜里出局 + 被放逐 ===")
# ⚠️ 2026-10-13 加：J203 原来让 2 号**既被夜刀又被放逐**、J208 的存活名单与死亡记录
#    差 3 个人 —— 模型只能把一大段回答花在"题面算不平"上（那一轮 6 道 0% 里，
#    每一份回答的开头都在投诉这件事）。判据：
#      ① 通用规则公告里的板子：人数 == 各角色数之和（守卫算进 4 神，不再算成 13 个身份）；
#      ② 每题都挂了 ledger（板子 / 存活 / 夜里出局 / 被放逐）；
#      ③ 三份名单正好把 1…板子人数 分完（不重不漏），夜刀与放逐**不许有交集**；
#      ④ 题面自己写出来的存活人数 == ledger 里的存活人数；
#         （题面里如果还写了「4 狼 + 4 神 + 2 平民」这种构成，加起来也要等于存活人数）
#      ⑤ 题面里出现的每个座位号都 ≤ 板子人数（防"12 人局里冒出 13 号"）。
_rules = exam_hard3.RULES_TEXT
_m_total = re.search(r"(\d+)\s*人局", _rules)
_BOARD = int(_m_total.group(1)) if _m_total else 0
_ROLE_PAT = (("狼人", r"狼人\s*(\d+)\s*名"), ("预言家", r"预言家\s*(\d+)\s*名"),
             ("女巫", r"女巫\s*(\d+)\s*名"), ("猎人", r"猎人\s*(\d+)\s*名"),
             ("守卫", r"守卫\s*(\d+)\s*名"), ("平民", r"平民\s*(\d+)\s*名"))
_roles = {name: int(re.search(pat, _rules).group(1))
          for name, pat in _ROLE_PAT if re.search(pat, _rules)}
check("10a 通用规则公告里写全了板子，且各角色数之和 == 人数",
      _BOARD > 0 and len(_roles) == len(_ROLE_PAT) and sum(_roles.values()) == _BOARD,
      f"{_BOARD} 人 vs {_roles} = {sum(_roles.values())}")

_no_led = sorted(q.qid for q in bank if not getattr(q, "ledger", None))
check("10b 每道题都挂了人数账 ledger（板子/存活/夜里出局/被放逐）", not _no_led,
      str(_no_led[:4]))
_led_bad: list[tuple[str, str]] = []
_alive_bad: list[tuple[str, str]] = []
_seat_bad: list[tuple[str, str]] = []
for q in bank:
    led = dict(getattr(q, "ledger", None) or {})
    if not led:
        continue
    alive, night, exile = (list(led.get(k) or []) for k in ("alive", "night", "exile"))
    total = int(led.get("board") or 0)
    if total != _BOARD:
        _led_bad.append((q.qid, f"板子 {total} ≠ 规则公告里的 {_BOARD}"))
    if len(alive) + len(night) + len(exile) != total:
        _led_bad.append((q.qid, f"{len(alive)}+{len(night)}+{len(exile)} ≠ {total}"))
    if set(alive) | set(night) | set(exile) != set(range(1, total + 1)):
        _led_bad.append((q.qid, "三份名单没把 1…总人数 分完"))
    both = sorted(set(night) & set(exile))
    if both:
        _led_bad.append((q.qid, f"{both} 既被夜刀又被放逐"))
    scen = str(getattr(q, "scenario", ""))
    said = [int(x) for x in re.findall(r"场上[^0-9]{0,8}?(\d+)\s*张牌", scen)]
    if not said or said[0] != len(alive):
        _alive_bad.append((q.qid, f"题面写 {said[:1] or '没写'}，账上是存活 {len(alive)}"))
    # 构成括号（可选）：「（4 狼 + 4 神 + 2 平民）」里的数字加起来要等于存活人数
    m = re.search(r"场上[^0-9]{0,8}?\d+\s*张牌[^（(]{0,8}[（(]([^）)]*)[）)]", scen)
    if m and re.search(r"[狼神民]", m.group(1)):
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if nums and sum(nums) != len(alive):
            _alive_bad.append((q.qid, f"构成 {m.group(1)} 加起来 {sum(nums)} ≠ 存活 {len(alive)}"))
    for seat in alive + night + exile:
        if str(seat) not in scen:
            _seat_bad.append((q.qid, f"{seat} 号在题面里没出现"))
    over = sorted({int(x) for x in re.findall(r"(\d+)\s*号", scen)} - set(range(1, total + 1)))
    if over:
        _seat_bad.append((q.qid, f"题面里出现了板子外的座位号 {over}"))
check("10c 每题的 ledger 都算得平（存活+夜刀+放逐 == 板子，名单不重不漏、夜刀≠放逐）",
      not _led_bad, str(_led_bad[:3]))
check("10d 题面自己写的存活人数（与构成）== ledger 里的人数", not _alive_bad,
      str(_alive_bad[:3]))
check("10e 账上每个座位号都在题面里出现，且题面里没有板子外的号", not _seat_bad,
      str(_seat_bad[:3]))

print("\n=== 十一、本轮修题（2026-10-14 审计）的回归锚点：改过的要点必须认得出对的、也要挡得住错的 ===")
# 这一节钉的是**这一轮实际改过的那几处**，正例抄的是模型原文（就存在
# `reports/思考档J2-*-修题后-20261014.raw.json` 里），反例钉的是**这一轮实际抓到的误命中**。
# 它比 5j/5k 那两条更硬的地方：正例不是把命中词塞进句子造出来的，而是真实作答里的话；
# 反例也不是随便写的否定句，而是「说岔了 / 结论反了」那种以前会被算成命中的写法。
# ⚠️ 旧 J 类的 J01（`exam_hard2.py`）上一轮（2026-10-14 首轮）改的是要点词表与 forbid，
#    它的锚点**放在这里**（`check_exam_hard2.py` 当时的口径是「52/52」不动）。
#    ⚠️ 2026-10-14 第二轮起：J01 改的是**问句**（新增第 ⑤ 问），它的锚点放到
#    `check_exam_hard2.py` 第十节（那边的口径已放宽成「52+ 项」）。
_ANCHORS: list[tuple[str, int, str, str, str]] = [
    # (题号, 要点序号, 正例（模型原文那一路的写法）, 反例（必须 0 命中）, 这一处改了什么)
    ("J201", 3,
     "今天最硬的一条是核账：把两天的票型摆在一起，4 号那条链子前后对不上。",
     "我主要看谁的发言更流畅、谁的起跳姿势更整齐。",
     "撤掉 P5 的「先走查杀」（那是题面里那句**错误原则**的原话）"),
    ("J201", 5,
     "我归票 4 号：他把自己刚报的金水投了出去。",
     "今天有查杀先走查杀，顺着昨天的惯性把票压过去就行。",
     "题面里不再出现 P3 的命中词「票型」（已进 giveaways）"),
    ("J204", 5,
     "票用在票型上最说不通的位置，同时要求两张跳预言家的牌把验人链说完整。",
     "我今天想归票 10 号。",
     "撤掉 P5 的「归票 10 / 归票10」（那是本题的**错答案**）"),
    # ⚠️ 2026-10-14 第二轮加（本轮 J204 改的是题面、不是要点）：
    #    正例抄的是「模型自己去算两张女巫的账」那一路的写法（P1 的词表没动），
    #    反例钉的是**不答这条账**的和稀泥写法。
    ("J204", 1,
     "4 号的账更完整：救的是谁、毒药还在手里，他都交代了；10 号说不出刀口在谁身上。",
     "两个人都说自己是女巫，谁真谁假我说不准，先听听再说。",
     "P1 的命中词原来就写在题面里（已改题面措辞 + 收进 giveaways，见 11f）"),
    ("J206", 1,
     "前两夜刀口都是平民（2号、9号），狼明显在走屠民路线。",
     "这一局我完全看不出狼队想打哪一边。",
     "补 P1 的「刀口都是平民 / 屠民路线」（GLM-高、DS-中 原文）"),
    ("J206", 2,
     "场上仅剩1个平民存活，狼队只要再刀中最后一个平民即可达成屠边胜利。",
     "场上还剩2张平民牌，民边暂时还算宽裕。",
     "补 P2 的「仅剩1个平民 / 最后一个平民」（豆包-关、DS-中 原文）"),
    # ⚠️ 2026-10-14 第二轮加（作者拍板：**故意不补词**）：本要点是 J206 的题眼，
    #    词表只收「1 张 / 1 个」这一路。反例钉的是「顺手放宽」会放进来的那种
    #    模糊说法（「平民不多了」）—— 它必须继续 0 命中。
    ("J206", 2,
     "场上仅剩 1 个平民了，狼队下一刀就屠边成功。",
     "平民数量不多了，我们得赶紧把狼找出来。",
     "P2 **故意不补词**（分档点）：模糊的「平民不多了」不算命中"),
    ("J206", 3,
     "① 跳女巫。理由：前两夜刀口都是平民，再藏下去民边就没了。",
     "① 今天不跳女巫，先潜伏。理由：我跳出来会和 7 号正面对跳。",
     "P3 撤「跳出来」（不跳的答案曾被它送分）、补带题号锚点的结论写法"),
    # ⚠️ 2026-10-14 第二轮重锚：P1 不再认「复述守人链」，改认「规则怎么落到这一夜」。
    #    正例抄的是模型原话那一路（DS-中「4号不在连续守限制内」+ DS-关「可以守4号」），
    #    反例钉的正是**以前白送分的那种写法**：把题面的守人链原样复述一遍。
    ("J207", 1,
     "4号不在连续守限制内，前两夜都没守过他，所以这一夜可以守4号；7号隔了一夜也可以再守。",
     "① 守人链：第1夜守7号，第2夜守12号，第3夜待定；我今晚按老习惯挑一个守。",
     "P1 重锚：撤掉「第1夜守7 / 守的是 7 / 不能守12」等**题面回声词**，"
     "改收「4号这一夜为什么有资格」的推理写法"),
    ("J207", 3,
     "不守 10 号：他跳了女巫，狼队更愿意在白天用票解决他，夜里先拔信息位。",
     "女巫也可以守一守，两条路我觉得都行。",
     "P3 **故意不补词**（分档点）：和稀泥的「女巫也可以守」不算命中"),
    ("J207", 4,
     "关于「狼队会猜我守谁」：我故意错开他们的预判，别让他们读死规律。",
     "守人这件事说白了就是碰运气，谁被刀全看天意。",
     "补 P4 的「预判 / 会猜」（12 份作答都写了这一整段，旧词表只中过 1 次）"),
    ("J207", 5,
     "明晚改守 10 号女巫，别把守人目标钉死在同一个人身上。",
     "④ 如果明晚还有机会，我到明晚再看情况定。",
     "P5 撤「明晚」（那是问句④的原词，12/12 白得一分）"),
    ("J208", 1,
     "主持 9 号和 7 号的对跳对质，用已知翻牌去核对谁在说谎，要求 9 号报出完整验人记录。",
     "我先把自己这两天的想法从头到尾讲一遍，证明我不是坏人。",
     "补 P1 的「对质 / 核对 / 核验 / 验人记录 / 逼预言家」（5 份原文）"),
    ("J208", 4,
     "狼人每晚白拿一刀，场上就变成 3 狼对 2 好，好人基本必败。",
     "现在场上人还够，我们还有好几轮可以慢慢磨。",
     "补 P4 的「白拿一刀 / 必败 / 败局」（4 份原文）"),
    ("J01", 1,
     "10号 这个女巫我暂认：我选择暂认她是女巫，目前没有人对跳。",
     "10号 这个位置我先完全不信，直接当狼打。",
     "补 P1 的「认她是女巫 / 认下10号女巫」（3 份原文，旧词表只认「真女巫」）"),
    ("J01", 5,
     "④这一票投7号：他推掉的是已知的好人 3号。",
     "④ 这一票我先不表态，等下一轮再说。",
     "补 P5 的「投7号 / 投 7号」（10 份原文写的就是不打空格的「投7号」）"),
    ("J01", None,
     "反过来，如果7号是真预言家，那『真预言家+真金水一起误推好人』只能是巧合，"
     "解释力弱得多。所以 5号 更像真预言家，7号 更像悍跳狼。",
     "我的结论正好相反：7号 才是真预言家。",
     "改 forbid 的头两条为断言式（反事实引用不再被一票否决）"),
]
_anchor_bad: list[str] = []
for _qid, _pi, _pos, _neg, _what in _ANCHORS:
    _q = by_qid.get(_qid) if _qid.startswith("J2") else None
    if _q is None and _qid == "J01":
        _q = next(q for q in exam_hard2.build_hard2_bank() if q.qid == "J01")
    if _q is None:
        _anchor_bad.append(f"{_qid} 找不到这道题")
        continue
    if _pi is None:                      # 只钉 forbid 那一处（不指定要点）
        if not _exam._hits(_neg, str(_q.forbid[0])):
            _anchor_bad.append(f"{_qid} forbid 反例没被拦下")
        if _exam._hits(_pos, str(_q.forbid[0])) or _exam._hits(_pos, str(_q.forbid[1])):
            _anchor_bad.append(f"{_qid} 正例被 forbid 误杀")
        continue
    _name, _ws = _q.rubric[_pi - 1]
    if not _exam._hit_words(_pos, _ws):
        _anchor_bad.append(f"{_qid} P{_pi} 正例（模型原文那一句）没被认出来：{_pos[:20]}")
    _neg_hits = [str(w) for _n, ws in _q.rubric for w in _exam._hit_words(_neg, ws)]
    if _neg_hits:
        _anchor_bad.append(f"{_qid} P{_pi} 反例命中了 {_neg_hits[:3]}")
    if _exam.grade(_q, _neg)[0]:
        _anchor_bad.append(f"{_qid} P{_pi} 反例被判成答对了")
check(f"11a 本轮 {len(_ANCHORS)} 条逐句锚点：正例（模型原文）认得出、反例 0 命中且判错",
      not _anchor_bad, str(_anchor_bad[:3]))
for _qid, _pi, _pos, _neg, _what in _ANCHORS:
    print(f"      {_qid}{'' if _pi is None else '#P' + str(_pi)}　{_what}")
# J208 第 3 天票型的**人数账**：当时场上 7 张牌，票型里必须把三张弃票都交代掉
_j208_scen = str(getattr(by_qid.get("J208"), "scenario", ""))
_d3 = [ln for ln in _j208_scen.split("\n") if "弃票" in ln]
check("11b J208 第 3 天的票型算得平：弃票那一句要把当时还活着的 6 号、10 号也交代掉",
      bool(_d3) and all(("6" in ln and "10" in ln) for ln in _d3), str(_d3)[:90])
# J201：改了题面措辞之后，「票型」必须留在 giveaways 里长期守着
check("11c J201 局面里不再出现 P3 的命中词「票型」，且它已登记进 giveaways",
      "票型" not in str(getattr(by_qid.get("J201"), "scenario", ""))
      and "票型" in (getattr(by_qid.get("J201"), "giveaways", None) or []))
# J206 / J207：题面与问句里不许再出现本轮撤掉的那两个送分词
check("11d J206 局面里不再出现 P5 的命中词「药都还在」（已进 giveaways）",
      "药都还在" not in str(getattr(by_qid.get("J206"), "scenario", ""))
      and "药都还在" in (getattr(by_qid.get("J206"), "giveaways", None) or []))
check("11e J207 的 P5 不再收问句④里的原词「明晚」（问句不改，改的是命中词）",
      "明晚" not in [w for _n, ws in by_qid["J207"].rubric for w in ws]
      and "明晚" in str(getattr(by_qid["J207"], "ask", "")))

# --------------------------------------------------------------------------- #
# 十一之二、2026-10-14 **第二轮**审计（四处「题目设计决定」）的回归锚点
# --------------------------------------------------------------------------- #
# 这一节钉的是这一轮实际改的那四处（J204 题面 / J206 P2 与 J207 P3 的「故意不补词」/
# J207 P1 重锚 / J01 问句新增第 ⑤ 问，最后那处在 check_exam_hard2.py 第十节）。
# ⚠️ 判据不是「要点写没写对」，而是「**白送分的那条通道有没有被堵上**」：
#    ① 题面里不许再躺着命中词（照抄题面即得分）—— 11f / 11g；
#    ② 该窄的要点不许被顺手放宽（词表冻结 + 模糊说法继续 0 命中）—— 11i / 11j；
#    ③ 重锚过的要点必须**认得出推理、挡得住复述** —— 11h（逐句锚点见上面的 _ANCHORS）。
_E1 = by_qid["J204"]


def _nows(s: str) -> str:
    """去掉半角/全角空格与制表符 —— 判分器 `exam._hits` 就是这么归一化再比子串的
    （「守7号」与「守 7 号」算同一个词），所以闸门也必须按归一化后的文本查，
    否则题面里躺着「守 7 号」而词表写「守7号」时闸门会漏报。"""
    return re.sub(r"[ \u3000\t]", "", str(s))


_J204_ECHO = ("毒药还在", "说不出", "说不出刀口")
_J204_WORDS = [str(w) for _n, ws in _E1.rubric for w in ws]
_J204_SCEN = _nows(getattr(_E1, "scenario", ""))
_J204_LEFT = [w for w in _J204_WORDS if _nows(w) in _J204_SCEN]
check("11f J204 局面段里不再躺着 P1 的命中词（题面回声道已堵），且原措辞进了 giveaways",
      not _J204_LEFT
      and all(w in (_E1.giveaways or []) for w in _J204_ECHO)
      and not any(_nows(w) in _J204_SCEN for w in _J204_ECHO),
      f"局面里剩下的命中词={_J204_LEFT}；"
      f"giveaways 缺={[w for w in _J204_ECHO if w not in (_E1.giveaways or [])]}")

# J207 P1 重锚：旧的**题面回声词**（复述守人链 / 题面自己写的「12 号不可选」）一个字都不许留下；
# 新词必须是「这一夜为什么轮得到 4 号」那一路（认得出推理、挡得住纯复述）。
_J207_P1_ECHO = ("第1夜守7", "第1夜守", "第 1 夜守", "守的是7", "守的是 7", "守过7", "守过 7",
                 "第2夜守", "第 2 夜守", "不能守12", "不能守 12", "12号不可选", "12 号不可选",
                 "不能连守", "连守禁止", "可以守他")
_J207_P1 = [str(w) for w in by_qid["J207"].rubric[0][1]]
_J207_P1_LEFT = [w for w in _J207_P1 if _nows(w) in {_nows(x) for x in _J207_P1_ECHO}]
_J207_SCEN = _nows(getattr(by_qid["J207"], "scenario", ""))
_J207_P1_IN_SCEN = [w for w in _J207_P1 if _nows(w) in _J207_SCEN]
check("11g J207 的 P1 已重锚：题面回声词一个不剩，新词也不许躺在局面段里",
      not _J207_P1_LEFT and not _J207_P1_IN_SCEN,
      f"残留回声词={_J207_P1_LEFT}；新词出现在局面里={_J207_P1_IN_SCEN}")
_RECITE = "① 守人链：第1夜守7号，第2夜守12号，第3夜待定。"
_RECITE_HITS = [str(w) for _n, ws in by_qid["J207"].rubric for w in _exam._hit_words(_RECITE, ws)]
_REASON = "4号不在连续守限制内，前两夜都没守过他，所以这一夜可以守4号。"
_REASON_HITS = _exam._hit_words(_REASON, _J207_P1)
check("11h J207 的 P1 只认推理：纯复述守人链 0 命中，说清「4 号这一夜为什么有资格」才命中",
      not _RECITE_HITS and bool(_REASON_HITS),
      f"复述命中={_RECITE_HITS}；推理命中={_REASON_HITS}")

# J206 P2 / J207 P3：作者拍板**故意不补词**（这是这两道题真正的题眼），
# 词表在这里**冻结** —— 以后谁想「顺手放宽」，往这两条里多写一个词就会当场红。
_FROZEN = {
    "J206": (2, ["平民只剩一张", "民边只剩一张", "只剩一张民", "只剩一个平民", "下一刀就",
                 "当场就输", "清空民边", "民边被清空", "仅剩1个平民", "只剩1个平民",
                 "民只剩1", "最后一个平民"]),
    "J207": (3, ["不守 10", "不是 10", "放一放 10", "白天解决", "用票解决", "先不守女巫"]),
}
_FROZEN_BAD: list[str] = []
_SRC3 = io.open(os.path.join(ROOT, "tools", "exam_hard3.py"), encoding="utf-8").read()
for _qid, (_pi, _want) in _FROZEN.items():
    _got = [str(w) for w in by_qid[_qid].rubric[_pi - 1][1]]
    if _got != _want:
        _FROZEN_BAD.append(f"{_qid} P{_pi} 词表被改：多 {sorted(set(_got) - set(_want))}、"
                           f"少 {sorted(set(_want) - set(_got))}")
check("11i J206 的 P2 与 J207 的 P3 词表被冻结（**故意不补词**，分档点不许顺手放宽）",
      not _FROZEN_BAD and _SRC3.count("故意不补词") >= 2,
      f"{_FROZEN_BAD[:2]}；源码里「故意不补词」出现 {_SRC3.count('故意不补词')} 次（要求 ≥2）")
_LENIENT = ["平民数量不多了，我们得赶紧把狼找出来。", "女巫也可以守一守，两条路我觉得都行。"]
_LENIENT_BAD = [(qid, str(w)) for qid, sent in zip(("J206", "J207"), _LENIENT)
                for _n, ws in by_qid[qid].rubric for w in _exam._hit_words(sent, ws)]
check("11j 「顺手放宽」会放进来的那种模糊说法（平民不多了 / 女巫也可以守）继续 0 命中",
      not _LENIENT_BAD, str(_LENIENT_BAD[:3]))

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
if not grader_ok:
    print("（注意：判分器/接线那几条因为 import exam.py 失败被 SKIP，不算通过）")
sys.exit(1 if BAD else 0)
