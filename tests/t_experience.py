# coding=utf-8
"""顺手复核代理 B 自己标了 ⚠️ 的那一条：experience 文件名改版后的两趟读取。

HANDOFF §3 说"复核时值得用不同思路再验一遍"，这里就用**强制打乱 os.listdir 顺序**的办法验：
同一个 key 同时存在旧文件名和新文件名时，结果必须永远是规范文件那份（最新写下的）。

另外顺手验：老文件名的键补全、slug 碰撞、原子写不残留 .tmp。不联网、不花钱。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wolf import experience as ex                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "tmp", "experience")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))


def main() -> int:
    shutil.rmtree(STORE, ignore_errors=True)
    os.makedirs(STORE, exist_ok=True)

    key = "gw/modelX"
    canonical = ex.write_note(STORE, key, {"key": key, "host": "gw", "model": "modelX",
                                          "note": "NEW-最新"})
    legacy = os.path.join(STORE, f"{ex.slug(key)}.json")   # 改版前的文件名（没有 sha1 后缀）
    io.open(legacy, "w", encoding="utf-8").write(json.dumps(
        {"host": "gw", "model": "modelX", "note": "OLD-旧的"}, ensure_ascii=False))
    print(f"  规范文件：{os.path.basename(canonical)}")
    print(f"  旧  文件：{os.path.basename(legacy)}")

    real_listdir = os.listdir
    seq = ["OLD 在前", "NEW 在前", "字母序"]
    got = {}
    for mode in seq:
        if mode == "OLD 在前":
            os.listdir = lambda d, _l=os.path.basename(legacy), _c=os.path.basename(canonical): (
                [_l, _c] if os.path.abspath(d) == os.path.abspath(STORE) else real_listdir(d))
        elif mode == "NEW 在前":
            os.listdir = lambda d, _l=os.path.basename(legacy), _c=os.path.basename(canonical): (
                [_c, _l] if os.path.abspath(d) == os.path.abspath(STORE) else real_listdir(d))
        else:
            os.listdir = real_listdir
        got[mode] = ex.load_all(STORE).get(key)
    os.listdir = real_listdir
    check("B-1 三种 listdir 顺序下，读到的都是规范文件里的最新内容",
          set(got.values()) == {"NEW-最新"}, f"{got}")
    check("B-2 旧文件不会被误当成'更权威'的那份", got["OLD 在前"] == "NEW-最新", str(got["OLD 在前"]))

    # 只有旧文件名（迁移场景）：键要从 host/model 补全
    shutil.rmtree(STORE, ignore_errors=True)
    os.makedirs(STORE, exist_ok=True)
    io.open(os.path.join(STORE, "gw_modelY.json"), "w", encoding="utf-8").write(json.dumps(
        {"host": "gw", "model": "modelY", "note": "legacy-only"}, ensure_ascii=False))
    io.open(os.path.join(STORE, "onlymodel.json"), "w", encoding="utf-8").write(json.dumps(
        {"model": "solo", "note": "model-only"}, ensure_ascii=False))
    notes = ex.load_all(STORE)
    check("B-3 只有旧文件名时，键也能补成 平台/模型（调用方查的就是这个键）",
          notes.get("gw/modelY") == "legacy-only", str(notes))
    check("B-4 连 host 都没有的老文件退回用 model 当键", notes.get("solo") == "model-only", str(notes))

    # slug 碰撞：前 80 字符一模一样的两个长 key
    shutil.rmtree(STORE, ignore_errors=True)
    os.makedirs(STORE, exist_ok=True)
    k1 = "gw/" + "a" * 120
    k2 = "gw/" + "a" * 120 + "TAIL"
    p1 = ex.write_note(STORE, k1, {"key": k1, "note": "K1"})
    p2 = ex.write_note(STORE, k2, {"key": k2, "note": "K2"})
    notes = ex.load_all(STORE)
    check("B-5 两个只差结尾的长模型名不再落到同一个文件",
          p1 != p2 and notes.get(k1) == "K1" and notes.get(k2) == "K2",
          f"{os.path.basename(p1)} vs {os.path.basename(p2)} → {notes}")

    # 原子写：不残留 .tmp、内容可解析
    left = [f for f in os.listdir(STORE) if ".tmp" in f]
    check("B-6 原子写没有残留 .tmp 文件", not left, str(left))

    # 坏文件不能带崩读取
    io.open(os.path.join(STORE, "broken.json"), "w", encoding="utf-8").write("{半截 JSON")
    ok_broken = True
    try:
        ex.load_all(STORE)
    except Exception as exc:                            # noqa: BLE001
        ok_broken = False
        print("     ", exc)
    check("B-7 半截 JSON 只被跳过，不会带崩整次读取", ok_broken)

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
