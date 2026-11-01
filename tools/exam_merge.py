# coding=utf-8
"""把**多次**笔试的原始作答（`*.raw.json`）合并成一份成绩单。

为什么需要它（2026-10-11 起因）：`tools/exam.py` 的难度标定是**在一次运行内跨模型**算的
（每题通过率 = 答对模型数 / 参与模型数），一旦某个模型太慢、必须单独跑一轮，
它的成绩就没法跟别的模型放进同一张标定表。这个工具就是干这件事：
**读多份 raw.json → 用同一个题库与判分器重新判 → 合并出跨模型通过率、标定与对错矩阵。**

用法：
    python tools/exam_merge.py reports/笔试-GLM-*.raw.json reports/笔试-4模型-*.raw.json
    python tools/exam_merge.py <raw...> --out reports/笔试-合并.md [--bank both] [--game <存档>]

⚠️ 判分**完全走 `exam.grade()` 同一套**（不另写规则），题库也用 `exam.pick_banks()` 同一套，
   否则合并出来的分数会和单轮报告对不上。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")

import exam                                                     # noqa: E402


def load_answers(paths: list[str]) -> dict[str, dict[str, str]]:
    """读若干 raw.json → {模型名: {qid: 作答文本}}（同名模型会合并，后读的覆盖同题）。"""
    out: dict[str, dict[str, str]] = {}
    chars: dict[str, int] = {}
    for p in paths:
        rows = json.load(io.open(p, encoding="utf-8"))
        for row in rows:
            name = str(row.get("model") or "未命名")
            bucket = out.setdefault(name, {})
            bucket.update({str(k): str(v) for k, v in (row.get("answers") or {}).items()})
            chars[name] = chars.get(name, 0) + int(row.get("prompt_chars") or 0)
    out["__prompt_chars__"] = chars                     # type: ignore[assignment]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", nargs="+", help="一份或多份 raw.json（支持通配符）")
    ap.add_argument("--out", default="", help="合并报告写到哪（默认不写文件）")
    ap.add_argument("--bank", default="both", choices=["auto", "base", "hard", "both"])
    ap.add_argument("--game", default="", help="用哪一局存档出题（默认最新一局）")
    args = ap.parse_args()

    paths: list[str] = []
    for pat in args.raw:
        paths.extend(sorted(glob.glob(pat)) or [pat])
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        print("❌ 一份 raw.json 都没找到")
        return 1

    game_path = args.game
    if not game_path:
        from pick_save import pick_game                          # noqa: PLC0415
        game_path, why = pick_game(os.path.join(ROOT, "games"))
        if not game_path:
            print(f"❌ 找不到对局存档：{why}")
            return 1
        print(f"用存档：{why}")
    game = json.load(io.open(game_path, encoding="utf-8"))
    # ⚠️ 必须和 `exam.py` 一样**带上引擎自打的演示局**（`@1` = 新机制那份），
    #    否则基础题库里那些 X* 题（警长/遗言/开枪公示）就少一批，合并分数会和单轮报告对不上。
    demo = exam.demo_game()
    base_bank, base_problems = exam.build_bank(game, extra=((demo, "@1", "X"),))
    hard_bank, hard_problems, hard_note = (None, [], "")
    if args.bank in ("auto", "hard", "both"):
        hard_bank, hard_problems, hard_note = exam.load_hard_bank(game)
    bank, problems, closed, note = exam.pick_banks(args.bank, base_bank, base_problems,
                                                  hard_bank, hard_problems, hard_note)
    if not bank:
        print("❌ 题库组装失败：", (problems or ["未知"])[0])
        return 1
    by_qid = {q.qid: q for q in bank}
    if closed:
        print(f"（其中 {len(closed)} 道是闭卷题，题号：{'、'.join(sorted(closed)[:6])}…）")

    got = load_answers(paths)
    chars = got.pop("__prompt_chars__", {})             # type: ignore[assignment]
    models = sorted(got)
    print(f"合并 {len(paths)} 份原始作答｜模型 {len(models)} 个：{'、'.join(models)}")
    print(f"题库：{len(bank)} 道（{note}）")

    # 逐题判分：correct[qid][model] = True/False
    correct: dict[str, dict[str, bool]] = {q.qid: {} for q in bank}
    offscore: dict[str, dict[str, dict[str, int]]] = {}
    for name in models:
        per_cat: dict[str, dict[str, int]] = {}
        for q in bank:
            ans = got[name].get(q.qid, "")
            ok, _why = exam.grade(q, ans) if ans != "" else (False, "没答上")
            correct[q.qid][name] = bool(ok)
            cell = per_cat.setdefault(q.category, {"ok": 0, "n": 0})
            cell["n"] += 1
            cell["ok"] += 1 if ok else 0
        offscore[name] = per_cat

    # 跨模型通过率 → 标定
    labels: dict[str, str] = {}
    for q in bank:
        hits = [correct[q.qid][m] for m in models]
        rate = sum(hits) / len(hits) if hits else 0.0
        if len(models) < 3:
            labels[q.qid] = "标定仅供参考"
        elif rate >= 0.9:
            labels[q.qid] = "saturated（体检题）"
        elif rate <= 0.2:
            labels[q.qid] = "too_hard（待复核）"
        else:
            labels[q.qid] = "discriminating"

    cats = sorted({q.category for q in bank})
    width = max(len(m) for m in models) + 2
    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit("=" * 100)
    emit(f"合并成绩单（{len(bank)} 道题；模型 {len(models)} 个）")
    emit("=" * 100)
    head = "模型".ljust(width) + "".join(c.ljust(9) for c in cats) + "总分".ljust(9) + "有效分".ljust(9) + "没答上"
    emit(head)
    emit("-" * len(head))
    disc_qids = [q.qid for q in bank if labels[q.qid] == "discriminating"]
    for name in models:
        row = name.ljust(width)
        tot_ok = tot_n = 0
        for c in cats:
            cell = offscore[name].get(c, {"ok": 0, "n": 0})
            pct = (cell["ok"] / cell["n"] * 100) if cell["n"] else 0
            row += f"{pct:.0f}%".ljust(9)
            tot_ok += cell["ok"]; tot_n += cell["n"]
        miss = sum(1 for q in bank if got[name].get(q.qid, "") == "")
        d_ok = sum(1 for qid in disc_qids if correct[qid][name])
        row += f"{tot_ok / tot_n * 100:.0f}%".ljust(9)
        row += (f"{d_ok / len(disc_qids) * 100:.0f}%" if disc_qids else "—").ljust(9)
        row += str(miss)
        emit(row)
    emit("-" * len(head))
    emit(f"每题分档：discriminating {len(disc_qids)} 道"
         + (f"（排名只看这些）；" if disc_qids else "（模型数 <3，标定仅供参考）；")
         + f"saturated {sum(1 for v in labels.values() if v.startswith('saturated'))} 道；"
         + f"too_hard {sum(1 for v in labels.values() if v.startswith('too_hard'))} 道")
    emit()
    emit("逐题对错矩阵（● 对 / ○ 错；最后一列是跨模型通过率与分档）")
    emit("-" * len(head))
    for cat in cats:
        emit(f"[{cat}]")
        for q in bank:
            if q.category != cat:
                continue
            marks = "".join("●" if correct[q.qid][m] else "○" for m in models)
            hits = sum(correct[q.qid][m] for m in models)
            emit(f"  {q.qid:<7}{marks:<{len(models) + 2}}"
                 f"{hits}/{len(models)}  {labels[q.qid]}")
    emit()
    if len(models) < 3:
        emit("⚠️ 模型数不足 3 个 ⇒ 标定仅供参考（不下 saturated/discriminating 结论）")
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write("\r\n".join(lines) + "\r\n")
        print(f"\n合并报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
