# coding=utf-8
"""临时：判定三份「思考档J2-*-20261013」报告是修题前还是修题后的产物。"""
from __future__ import annotations

import io
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from exam_hard3 import build_hard3_bank                     # noqa: E402

cur = {q.qid: q for q in build_hard3_bank()}

#: 修题后独有的字样（修题前不可能出现）
POST_MARKS = {
    "J201": ["投自己报的金水", "4、7、10、12 四张票"],
    "J202": ["高风险低收益", "9 号不接是对的"],
    "J203": ["第 1 夜狼刀了 6 号"],
    "J204": ["说不出刀口在第几号身上"],
    "J205": ["这条人数关系", "放弃这一轮的发言权、过麦"],
    "J206": ["刀口一直在**平民**那一侧"],
    "J207": ["12 号不在你的可选范围里"],
    "J208": ["两边票数一模一样，没有人出局"],
}

FILES = [
    "reports/思考档J2-Haiku-20261013.raw.json",
    "reports/思考档J2-MiMo-20261013.raw.json",
    "reports/思考档J2-火山三家-20261013.raw.json",
]

for rel in FILES:
    path = os.path.join(HERE, rel)
    if not os.path.exists(path):
        print(f"[缺] {rel}")
        continue
    raw = json.loads(io.open(path, encoding="utf-8").read())
    blob = json.dumps(raw, ensure_ascii=False)
    hits = {qid: [m for m in marks if m in blob] for qid, marks in POST_MARKS.items()}
    n_post = sum(1 for v in hits.values() if v)
    print("=" * 78)
    print(f"{rel}")
    if isinstance(raw, list):
        print(f"  顶层是列表：{len(raw)} 项；第 1 项的键：{sorted(raw[0]) if raw and isinstance(raw[0], dict) else type(raw[0])}")
    elif isinstance(raw, dict):
        print(f"  顶层键：{sorted(raw)[:12]}")
        for k in ("bank", "models", "arms", "meta"):
            if k in raw and isinstance(raw[k], (list, dict)):
                print(f"  {k}: {len(raw[k])}")
    print(f"  命中「修题后」字样的题数：{n_post}/8")
    for qid, v in hits.items():
        if v:
            print(f"    {qid}: {v}")
    # 逐题比：报告里的题干是否与当前题库完全一致
    qtext = {}
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("question", "题干", "q") and isinstance(v, str) and v.startswith("【本题按通用"):
                    qid = None
                    for kk in ("qid", "id", "题号"):
                        if isinstance(node.get(kk), str):
                            qid = node[kk]
                    if qid:
                        qtext.setdefault(qid, v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(raw)
    same, diff = [], []
    for qid, txt in sorted(qtext.items()):
        if qid in cur:
            (same if txt == cur[qid].question else diff).append(qid)
    print(f"  与当前题库逐字相同：{same}")
    print(f"  与当前题库不同：{diff}")
