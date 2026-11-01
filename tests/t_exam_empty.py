# coding=utf-8
"""考核工具链回归：**空回复**（HTTP 200 + 正文空 + finish_reason=length + 无 error）。

为什么要单开一个文件守它（2026-10-13 实诊）：
  1. 空回复**不走重试**：`Model.ask` 的重试循环只在 `except` 里 continue，空回复在 try 里
     直接 return ⇒ attempts 恒为 1、一次就定生死（上游抖一下，整组题判 0）。
  2. 空回复被当成**答错**：逐题表画 ❌、why 写"空回复（工具问题…）"，于是同一臂出现
     "总分 62% vs 有效分 91%" 的撕裂；各臂被压低的程度还不一样（GLM 关 21 题、MiMo 高 3、
     DS 高 2、其余 0）⇒ 档位横向对比被污染。
  3. 劈半重问的回填 Bug：`ask_group` 里写死 `"empty": False, "attempts": 1` ⇒
     劈半过的组 harness 恒为 False，真没拿到正文的那一半落到 grade() 变成"（空答案）"。

这里全部用**本机假上游**（127.0.0.1 上自己现挑的空闲端口，只回本地假 JSON），
绝不打真接口、不花作者的钱，也不碰 8077（PID 3040 那台）。

跑法： python tests/t_exam_empty.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)                                         # wolf.* 在项目根
sys.path.insert(0, os.path.join(ROOT, "tools"))                  # exam_bank 在 tools/

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 本机假上游：起在 127.0.0.1 的**空闲端口**上，按脚本逐次作答
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:                           # 别把噪音打进测试输出
        pass

    def do_POST(self) -> None:                                   # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(raw.decode("utf-8"))
            prompt = "\n".join(str(m.get("content") or "")
                               for m in (payload.get("messages") or []))
            self.server.plan.record(prompt)                      # type: ignore[attr-defined]
            text, finish = self.server.plan.next_reply(prompt)    # type: ignore[attr-defined]
            body = {"id": "fake", "object": "chat.completion", "model": "fake",
                    "choices": [{"index": 0, "finish_reason": finish,
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
            out = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as exc:                                 # noqa: BLE001
            out = json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Plan:
    """这一次请求回什么。

    · `replies` 是逐次脚本（先弹出的先用）；用完了按 `repeat` 兜底；
    · `always_empty_for` = 题号集合：只要这次请求问的题全在这个集合里，就一直回空
      （用来造"劈半之后只有一半拿不到正文"）。
    """

    def __init__(self, replies: list, repeat=("", "length"), answer_by_qid=None,
                 always_empty_for=()) -> None:
        self.replies = list(replies)
        self.repeat = tuple(repeat)
        self.answer_by_qid = dict(answer_by_qid or {})
        self.always_empty_for = set(always_empty_for)
        self.prompts: list[str] = []
        self.asked: list[str] = []

    def record(self, prompt: str) -> None:
        self.prompts.append(prompt)

    def count_of(self, *qids: str) -> int:
        """问的题号正好是这些的请求有几次（用来数"同一组被重试了几遍"）。"""
        want = ",".join(qids)
        return sum(1 for a in self.asked if a == want)

    def next_reply(self, prompt: str) -> tuple[str, str]:
        qids = re.findall(r"^([A-Z]+\d+[a-z]?)\. ", prompt, flags=re.M)
        self.asked.append(",".join(qids))
        if qids and self.always_empty_for and set(qids) <= self.always_empty_for:
            return "", "length"
        if self.replies:
            text, finish = self.replies.pop(0)
        else:
            text, finish = self.repeat
        if not str(text).strip():
            # ⚠️ 空回复必须**真的没有正文**（不是空对象）：被测的正是"正文为空"这条路口。
            return "", finish or "length"
        return json.dumps({"answers": [{"q": q, "a": self.answer_by_qid.get(q, "？")}
                                       for q in qids]}, ensure_ascii=False), finish or "stop"


def start_fake(plan: Plan):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.plan = plan                                             # type: ignore[attr-defined]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}"


def free_port_blocked(port: int) -> bool:
    """8077（作者那台在跑的实例）绝不能碰：这里只断言我们**没有**用它。"""
    with socket.socket() as s:
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


class quiet_sleep:
    """把退避等待压成 0（只改睡眠，不改重试次数/顺序）—— 测试要秒级。"""

    def __enter__(self):
        self._orig = asyncio.sleep
        async def _noop(_t, *a, **k):                            # noqa: ANN001
            return None
        asyncio.sleep = _noop                                    # type: ignore[assignment]
        return self

    def __exit__(self, *a):
        asyncio.sleep = self._orig                               # type: ignore[assignment]
        return False


# --------------------------------------------------------------------------- #
# 测试用的小题库（判分器走 exam.grade 同一套，不另写规则）
# --------------------------------------------------------------------------- #

def load_exam(name: str, path: str = os.path.join(ROOT, "tools", "exam.py")):
    """从指定文件加载一份 exam 模块（变异自证要用"改坏的那一份"）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                 # type: ignore[union-attr]
    return mod


EX = load_exam("exam_empty_subject")


def make_bank(ex) -> list:
    return [ex.Q(qid="E01", category="检索", question="1 号投给了几号？请只回答数字。",
                 expect="2", grader="exact", why="测试题（真值固定）"),
            ex.Q(qid="E02", category="检索", question="2 号投给了几号？请只回答数字。",
                 expect="3", grader="exact", why="测试题（真值固定）")]


def group_for(ex, bank, qs=None) -> dict:
    return {"context": "（测试用公开记录：1 号投 2 号，2 号投 3 号）", "inject": "",
            "closed": False, "qs": list(qs if qs is not None else bank)}


def make_model(ex, base_url: str, max_retries: int = 1):
    return ex.Model(name="假上游模型", base_url=base_url, api_key="fake-key",
                    model="fake", max_retries=max_retries, timeout=10.0)


# --------------------------------------------------------------------------- #
# 一、空回复必须走重试（第一次空、第二次答对）
# --------------------------------------------------------------------------- #

def sec_retry(ex) -> None:
    print("\n=== 一、空回复走与异常同一条重试路（第一次空 → 第二次答对）===")
    bank = make_bank(ex)
    # 第一次：正文空 + finish_reason=length（就是实诊里那种回复）；第二次（重试）：照答案念。
    plan = Plan([["", "length"], ["answer", "stop"]],
                answer_by_qid={"E01": "2", "E02": "3"})
    srv, url = start_fake(plan)
    try:
        model = make_model(ex, url, max_retries=1)
        dump: list = []
        with quiet_sleep():
            res = asyncio.run(ex.run_model(model, [group_for(ex, bank)], dump))
        rows = {r["qid"]: r for r in res["rows"]}
        print(f"    假上游收到 {len(plan.prompts)} 次请求；每次问的题号：{plan.asked}")
        check("1a 空回复会重试（不是一次就定生死）",
              len(plan.prompts) >= 2, f"请求 {len(plan.prompts)} 次（修复前恒为 1 次）")
        check("1b dump 记的是**重试后**那一次的真实 attempts（>=2），且这组不再是空的",
              bool(dump) and int(dump[0].get("attempts") or 0) >= 2
              and dump[0]["empty"] is False and dump[0]["ok"] is True,
              str({k: dump[0].get(k) for k in ("attempts", "empty", "ok", "finish")})
              if dump else "没有 dump")
        check("1c 重试后拿到正文 ⇒ 最终判对（不是被当成没答上）",
              all(r["ok"] is True for r in rows.values()),
              str({k: (v["ok"], v["why"]) for k, v in rows.items()}))
        check("1d 真实 attempts 被带出来（>=2），不再写死 1",
              bool(dump) and int(dump[0].get("attempts") or 0) >= 2,
              f"attempts={dump[0].get('attempts') if dump else '?'}")
        check("1e 逐题表不是「未答」（这题真答上了）",
              all(not r.get("unanswered") for r in rows.values())
              and all(ex.mark_cell(r) == "✅" for r in rows.values()),
              str([ex.mark_cell(r) for r in res["rows"]]))
        # 一次调用只算一次 usage.calls（重试不该把调用次数也乘 5）
        check("1f attempts 累加但 usage.calls 只记 1 次请求",
              model.usage["calls"] == 1 and model.usage["retries"] >= 1,
              f"calls={model.usage['calls']} retries={model.usage['retries']}")
        check("1g 假上游在本机、且没碰 8077", url.startswith("http://127.0.0.1:"),
              f"{url}（8077 在跑={free_port_blocked(8077)}，本测试不连它）")
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- #
# 二、重试后仍空 ⇒ 记「未答」（不画 ❌、成绩单有「未答 N」）
# --------------------------------------------------------------------------- #

def sec_unanswered(ex) -> None:
    print("\n=== 二、重试后仍然空 ⇒ 记为「未答」，不算答错 ===")
    bank = make_bank(ex)
    plan = Plan([], repeat=("", "length"))                       # 永远空
    srv, url = start_fake(plan)
    try:
        model = make_model(ex, url, max_retries=2)
        dump: list = []
        with quiet_sleep():
            res = asyncio.run(ex.run_model(model, [group_for(ex, bank)], dump))
        res["summary"] = ex.summarize(res["rows"])
        rows = res["rows"]
        print(f"    请求 {len(plan.prompts)} 次：整组 {plan.count_of('E01', 'E02')} 次、"
              f"单题 E01 {plan.count_of('E01')} 次、单题 E02 {plan.count_of('E02')} 次"
              f"（max_retries=2 ⇒ 每次调用最多问 3 遍）")
        check("2a 每次都重试到 max_retries 用尽才放弃（两遍都空才劈半、单题也问满 3 遍）",
              plan.count_of("E01", "E02") == 3 and plan.count_of("E01") == 3
              and plan.count_of("E02") == 3,
              f"整组 {plan.count_of('E01', 'E02')} 次 / E01 {plan.count_of('E01')} 次 / "
              f"E02 {plan.count_of('E02')} 次")
        check("2b 全部记为「未答」：ok 不是「答错」语义（None），也不是答对",
              all(r["ok"] is None for r in rows) and all(r["unanswered"] for r in rows),
              str([(r["qid"], r["ok"], r["unanswered"]) for r in rows]))
        check("2c 未答的理由写明是工具问题（不是「答错」）",
              all("空回复" in r["why"] and "工具问题" in r["why"] for r in rows),
              str([r["why"] for r in rows]))
        check("2d 逐题表画 ⬜（不是 ❌、也不是 ✅）",
              [ex.mark_cell(r) for r in rows] == ["⬜", "⬜"],
              str([ex.mark_cell(r) for r in rows]))
        # ⚠️ 2026-10-25（作者口径）：空回复当**未答**，且**不计入总分分母**。
        #    旧断言是"未答仍进总分分母"——那正是会造成"总分 62% vs 有效分 91%"撕裂的口径。
        _rows = [
            {"qid": "X1", "category": "规则", "ok": True, "harness": False},
            {"qid": "X2", "category": "规则", "ok": None, "harness": True},
        ]
        _sum = ex.summarize(_rows)
        check("2e 未答**不计入总分分母**（1 对 + 1 未答 ⇒ acc 仍是 100%，不是 50%）",
              _sum["ok"] == 1 and _sum["acc"] == 1.0 and _sum["unanswered"] == 1,
              f"ok={_sum['ok']} acc={_sum['acc']} unanswered={_sum['unanswered']}")
        check("2f 未答数与「扣掉未答后的分」都单列，而且与总分同口径",
              _sum["unanswered"] == 1
              and _sum["acc_ignoring_unanswered"] == _sum["acc"],
              f"unanswered={_sum['unanswered']} "
              f"扣未答后={_sum['acc_ignoring_unanswered']} acc={_sum['acc']}")
        # 成绩单（控制台）
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            ex.print_scorecard({"假上游模型": res}, len(bank))
        card = buf.getvalue()
        check("2g 成绩单里有「未答」列与「未答 N 题」的显著提示",
              "未答" in card and "未答 2 题" in card, card.strip().splitlines()[-4:])
        # 报告（markdown）
        out = os.path.join(HERE, "tmp_exam_empty_report.md")
        ex.write_report(out, {"假上游模型": res}, bank, "games/game-fake.json", dump)
        rep = open(out, encoding="utf-8").read()
        os.remove(out)
        raw = out[:-3] + ".raw.json"
        if os.path.exists(raw):
            os.remove(raw)
        check("2h 报告的逐题表里是 ⬜，且带图例说明",
              "| ⬜ |" in rep and "⬜ = **未答**" in rep,
              [ln for ln in rep.splitlines() if "⬜" in ln][:2])
        check("2i 报告成绩单里有「未答」列（一眼看出低分里有多少是工具问题）",
              "未答" in rep and "扣未答后" in rep,
              [ln for ln in rep.splitlines() if "未答" in ln][:3])
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- #
# 三、劈半路径：attempts 累加、empty 不写死
# --------------------------------------------------------------------------- #

def sec_split(ex) -> None:
    print("\n=== 三、劈半重问：attempts 累加、empty 如实传递 ===")
    bank = make_bank(ex)
    # 第 1、2 次：整组（2 题）重试两遍都空 ⇒ 触发劈半；两个半组各空一次后答对。
    # ⚠️ 劈半的条件是"**重试完**还是空"（只空一次会被 Model.ask 自己救回来，轮不到劈半）——
    #    这正是修复 1 和修复 3 的接缝，测试必须按这条路走。
    plan = Plan([["", "length"], ["", "length"], ["", "length"], ["answer", "stop"],
                 ["", "length"], ["answer", "stop"]],
                answer_by_qid={"E01": "2", "E02": "3"})
    srv, url = start_fake(plan)
    try:
        model = make_model(ex, url, max_retries=1)
        dump: list = []
        with quiet_sleep():
            res = asyncio.run(ex.run_model(model, [group_for(ex, bank)], dump))
        rows = {r["qid"]: r for r in res["rows"]}
        n_req = len(plan.prompts)
        # 劈半那几行是"父行的账"（= 两半之和），别和子行重复相加
        d_ask = sum(int(d.get("attempts") or 0) for d in dump if not d.get("split"))
        splits = [d for d in dump if d.get("split")]
        print(f"    请求 {n_req} 次：{plan.asked}；非劈半行 attempts 合计 {d_ask}")
        check("3a 真的劈半了（整组空 → 两个半组各问一次）",
              bool(splits) and len(splits[0]["qids"]) == 2,
              str([d.get("qids") for d in splits]))
        check("3b 劈半组的 attempts 是**累加**的（不再写死 1）",
              bool(splits) and int(splits[0].get("attempts") or 0) >= 2,
              f"attempts={splits[0].get('attempts') if splits else '?'}"
              f"（两半各问了几次就是几）")
        check("3c 每一次请求都被 attempts 记到了（非劈半行合计 == 假上游收到的请求数）",
              d_ask == n_req, f"attempts 合计 {d_ask} vs 请求 {n_req}")
        check("3d 劈半组的 empty 取自子结果（不是写死的 False）",
              bool(splits) and splits[0]["empty"] is False and splits[0]["empty_qids"] == [],
              str({k: splits[0].get(k) for k in ("empty", "empty_qids")} if splits else "?"))
        check("3e 劈半后两边都答上了 ⇒ 两题都判对（劈半不埋答案）",
              all(r["ok"] is True for r in rows.values()),
              str({k: (v["ok"], v["why"]) for k, v in rows.items()}))
    finally:
        srv.shutdown()
        srv.server_close()

    # 3f：一半永远空、另一半答对 ⇒ **只有没答上的那半**记未答，答上的照常判分
    # 请求次序（max_retries=1 ⇒ 每次调用最多问两遍）：
    #   ① ② 整组（E01+E02）两遍都空 ⇒ 劈半；③ ④ 左半 E01 两遍都空 ⇒ 这半记未答；
    #   ⑤ 右半 E02 答对 ⇒ 照常判分。
    # （修复前：劈半组的 empty 被写死成 False ⇒ 左半那题落到 grade() 变成"（空答案）"，
    #  看起来像答错，还躲过了"未答"的统计。）
    plan2 = Plan([["", "length"], ["", "length"], ["", "length"], ["", "length"],
                  ["answer", "stop"]],
                 answer_by_qid={"E02": "3"})
    srv2, url2 = start_fake(plan2)
    try:
        model2 = make_model(ex, url2, max_retries=1)
        dump2: list = []
        with quiet_sleep():
            res2 = asyncio.run(ex.run_model(model2, [group_for(ex, bank)], dump2))
        res2["summary"] = ex.summarize(res2["rows"])
        r2 = {r["qid"]: r for r in res2["rows"]}
        check("3f 劈半后只有没拿到正文的那半记「未答」，另一半照常判分",
              r2["E01"]["unanswered"] is True and r2["E02"]["ok"] is True,
              str({k: (v["ok"], v["unanswered"]) for k, v in r2.items()}))
        check("3g 未答数只算那一题（不是整组一起算）",
              res2["summary"]["unanswered"] == 1,
              f"unanswered={res2['summary']['unanswered']}")
    finally:
        srv2.shutdown()
        srv2.server_close()


# --------------------------------------------------------------------------- #
# 四、finish=length 的组要在报告里标出来
# --------------------------------------------------------------------------- #

def sec_length(ex) -> None:
    print("\n=== 四、finish=length（预算吃满）在报告里可见 ===")
    bank = make_bank(ex)
    plan = Plan([], repeat=("", "length"))                       # 永远 length
    srv, url = start_fake(plan)
    try:
        model = make_model(ex, url, max_retries=0)
        dump: list = []
        with quiet_sleep():
            res = asyncio.run(ex.run_model(model, [group_for(ex, bank)], dump))
        res["summary"] = ex.summarize(res["rows"])
        out = os.path.join(HERE, "tmp_exam_length_report.md")
        ex.write_report(out, {"假上游模型": res}, bank, "games/game-fake.json", dump)
        rep = open(out, encoding="utf-8").read()
        os.remove(out)
        raw = out[:-3] + ".raw.json"
        if os.path.exists(raw):
            os.remove(raw)
        check("4a 逐题表里有 length 记号", "| length |" in rep,
              [ln for ln in rep.splitlines() if "length" in ln][:2])
        check("4b 成绩单里有「预算吃满」列，且这臂的组数 >= 1",
              "预算吃满" in rep and res.get("length_groups", 0) >= 1,
              f"length_groups={res.get('length_groups')}")
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- #
# 五、变异自证：把"空回复不再重试"改回去，本文件的闸门必须变红
# --------------------------------------------------------------------------- #

def mutate_source(src: str) -> str:
    """把「空回复也重试」改回旧写法（空正文直接 return、attempts 恒为 1）。"""
    anchor = '                    if not text.strip():'
    if anchor not in src:
        raise AssertionError("变异锚点没找到（exam.py 的 Model.ask 段落变了）")
    # 旧写法：空正文**不重试**，直接当成"成功但正文空"返回（attempts 也就是 1）。
    src = src.replace(
        anchor,
        '                    if False:            # ⟵ 变异：空回复不再重试\n'
        '                        last_error = ""', 1)
    # 顺带把"未答"改回"答错"（旧行为：工具问题伪装成答错，逐题表画 ❌）。
    src = src.replace('ok = None\n                why = ("空回复（工具问题：连单题都拿不到正文）"',
                      'ok = False\n                why = ("空回复（工具问题：连单题都拿不到正文）"', 1)
    return src


def sec_mutation(ex) -> None:
    print("\n=== 五、变异自证：改回旧写法后，本文件的闸门必须变红 ===")
    src_path = os.path.join(ROOT, "tools", "exam.py")
    src = open(src_path, encoding="utf-8").read()
    try:
        mutated = mutate_source(src)
    except AssertionError as exc:
        check("5a 变异锚点还在（改回旧写法可执行）", False, str(exc))
        return
    check("5a 变异锚点还在（能真的改回旧写法）", mutated != src, "已生成变异版 exam.py")
    tmp = os.path.join(HERE, "tmp_exam_mutated.py")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    try:
        mex = load_exam("exam_empty_mutated", tmp)
        bank = make_bank(mex)
        # ⚠️ 用**单题组**回放：2 题的组会走"劈半"那条路，把"一次就定生死"这个症状盖住
        #    （劈半本身会再发请求，看起来像重试过了）。单题组劈无可劈，症状最干净：
        #    修复前 = 只问 1 次、attempts=1、判成没答上；修复后 = 问 2 次、attempts=2、判对。
        plan = Plan([["", "length"], ["answer", "stop"]],
                    answer_by_qid={"E01": "2", "E02": "3"})
        srv, url = start_fake(plan)
        try:
            model = make_model(mex, url, max_retries=1)
            dump: list = []
            with quiet_sleep():
                res = asyncio.run(mex.run_model(model, [group_for(mex, bank, bank[:1])], dump))
            rows = {r["qid"]: r for r in res["rows"]}
            retried = len(plan.prompts) >= 2
            judge_ok = all(r["ok"] is True for r in rows.values())
            print(f"    变异版：请求 {len(plan.prompts)} 次、attempts="
                  f"{dump[0].get('attempts') if dump else '?'}、"
                  f"判对={ {k: v['ok'] for k, v in rows.items()} }")
            check("5b 变异后「空回复会重试」变红（请求数回到 1 次）",
                  not retried, f"请求 {len(plan.prompts)} 次（正是修复前那个症状）")
            check("5c 变异后「最终判对」变红（空回复被判成没答上）",
                  not judge_ok, str({k: (v["ok"], v["why"]) for k, v in rows.items()}))
            check("5d 变异后 attempts 回到 1（证明 1d 那条闸门钉的是这件事）",
                  bool(dump) and int(dump[0].get("attempts") or 0) == 1,
                  f"attempts={dump[0].get('attempts') if dump else '?'}")
            check("5e 变异后这题被画成 ❌（旧行为：工具问题伪装成答错）",
                  all(mex.mark_cell(r) == "❌" for r in res["rows"]),
                  str([mex.mark_cell(r) for r in res["rows"]]))
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(tmp + "c"):
            os.remove(tmp + "c")
    check("5f 变异用的是副本，tools/exam.py 没被动过",
          open(src_path, encoding="utf-8").read() == src, "源文件字节相同")


# --------------------------------------------------------------------------- #

def main() -> int:
    sec_retry(EX)
    sec_unanswered(EX)
    sec_split(EX)
    sec_length(EX)
    sec_mutation(EX)
    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
