# coding=utf-8
"""按板子组装规则：确认每个板子只发它自己的角色规则，并统计省了多少 token。"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from exam_bank import rules_text_for, seer_auto_run_default   # noqa: E402
from wolf import rules as _rules_mod                      # noqa: E402
from wolf.roles import BUILTIN_BOARDS, ROLES              # noqa: E402

import yaml                                               # noqa: E402

print("题库按板子取规则：")
#: **有"能力声明"的角色名**（会让人以为这张牌在本局里的那些）。
#: ⚠️ 不含「狼人」「村民」：所有板子都有狼，村民是屠边的通用说法。
CAPABILITY_ROLES = ("预言家", "女巫", "猎人", "守卫", "愚者", "摄梦人",
                    "守墓人", "养鸦人", "幸运儿", "狼王")
for name, board in BUILTIN_BOARDS.items():
    text = rules_text_for(name)
    roles_cn = [ROLES[r].name for r in dict.fromkeys(board.roles)]
    # ⚠️ 2026-10-09：`dreamer` 也要在"多余角色"候选里 —— 否则"预女白梦"板子里
    #    混进【摄梦人】规则时这条闸门看不见（它只查 guard/idiot/wolf/seer/witch/hunter）。
    # ⚠️ 2026-10-10：`wolf_king` 同理加进来（狼王只应该出现在狼王版板子上）。
    extra = [ROLES[r].name for r in dict.fromkeys(
        ["guard", "idiot", "wolf", "seer", "witch", "hunter", "dreamer", "wolf_king"])
        if r not in board.roles]
    leaked = [cn for cn in extra if f"【{cn}】" in text]
    has_sheriff = "【警长" in text
    want_sheriff = len(board.roles) >= 11
    # 首夜无刀是**板子级**条件片段：有开关就必须发、没有开关就一个字都不许出现
    quiet = bool(getattr(board, "first_night_quiet", False))
    has_quiet = "首夜无刀" in text
    # 出局亮身份同样是**板子级**条件片段（2026-10-30 加）：明牌板发、暗牌板一个字都不许出现
    reveal = bool(getattr(board, "reveal_on_death", False))
    has_reveal = "出局亮身份" in text
    # 预言家锁定上警是**开局选项**的条件片段：开着才发；而且**没有警长竞选的板子不许发**
    # （9 / 6 人板没有这一环 —— 发这段等于给预言家一条假信息）
    has_lock = "锁定上警" in text
    want_lock = seer_auto_run_default() and want_sheriff
    # ⚠️ 2026-10-11 新增（**通用片段不许点名本局没有的角色**）：
    #    事故现场：core 片段里写着「本板子有幸运儿的守护 ＋摄梦人的梦守」——
    #    打到 `eleven_standard`（既没幸运儿也没摄梦人）就是**假情报**，
    #    而 `extra` 那条闸门只看 `【角色名】` 标题，看不见正文里的点名。
    #    判据：先去掉两处**允许点名角色**的地方 ——
    #      ①【术语速查】那一段（它就是本词典，任何行话都能解释）；
    #      ②**本局真有的角色**自己那一段（角色片段当然要写自己的名字，
    #        它内部还有【细则】这种小标题，所以按**原文件的片段整块**去掉，别按小标题切）。
    #    剩下的正文里，本局没有的"能力角色"一个字都不许出现。
    glossary = ""
    _m = re.search(r"【术语速查】(.*?)(?=\n【|$)", text, re.S)
    glossary = _m.group(0) if _m else ""
    body_wo_glossary = text.replace(glossary, "")
    _frags, _human = _rules_mod.load()
    for _rk in dict.fromkeys(board.roles):
        _chunk = _frags.get(f"role:{_rk}")
        if _chunk:
            # compose 会把 `**` 去掉，所以这里也先去星号再整块替换
            body_wo_glossary = body_wo_glossary.replace(_chunk.replace("**", ""), "")
    mentioned = [cn for cn in CAPABILITY_ROLES
                 if cn not in roles_cn and cn in body_wo_glossary]
    # 胜负口径：屠边板必须写「屠边」那条、屠城板必须写「屠城」那条（发反 = 规则写反）
    has_sides_win = "屠边：狼人杀光全部神职" in text
    want_sides_win = (board.win_rule == "sides")
    ok = (not leaked and has_sheriff == want_sheriff and has_quiet == quiet
          and has_reveal == reveal and has_lock == want_lock
          and has_sides_win == want_sides_win and not mentioned)
    all_ok = bool(globals().get('all_ok', True)) and bool(ok)
    print(f"  {name:<18}{len(text):>6} 字 ≈{len(text) * 0.75:>4.0f} tokens  本局角色："
          f"{'、'.join(roles_cn):<26} 警长={'有' if has_sheriff else '—'}  "
          f"胜负={'屠边' if has_sides_win else '屠城'}  "
          f"首夜无刀={'有' if has_quiet else '—'}  出局亮身份={'有' if has_reveal else '—'}  "
          f"锁定上警={'有' if has_lock else '—'}  "
          f"多余角色规则={leaked or '无'}  通用片段点名了没有的角色={mentioned or '无'}  "
          f"{'✅' if ok else '❌'}")
if not all_ok:
    print("  ❌ 有板子的规则片段发错了（多发了本局没有的角色，或首夜无刀/锁定上警发错对象）")

# ── 关键规则必须还在（2026-10-09 加）─────────────────────────────────────────────
# 为什么加：这一轮人工复核时发现规则书**落后引擎好几天**（自爆、上警三选一、警长选方向、
# 刀口含狼、同刀同毒、狼人控场全都没写）—— 而模型每次调用都在读它。
# 这里把"必须写、且写对了"的规则钉成字面闸门：以后谁重写规则书时删掉一条，立刻红。
print("\n关键规则是否写进模型片段（11 人板）：")
# ⚠️ 盯**默认板子**（2026-10-10 起是 eleven_wolfking 狼王版）——
#    以前写死 eleven_standard，换默认板子后这条闸门就管错了对象。
_default_board = (yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"),
                                         encoding="utf-8"))["game"].get("board")
                  or "eleven_wolfking")
_key_text = rules_text_for(_default_board)
print(f"（关键规则按**默认板子** {_default_board} 检查）")
MUST = {
    "狼人自爆": "自爆",                  # 变版规则：两个时机 + 后果
    "上警三选一": "三选一",
    "前置位/后置位": "后置位",
    "警长选发言方向": "逆序",
    "刀口可以是狼": "包括狼自己人",
    "同刀同毒": "同刀同毒",
    "狼人控场": "狼人控场",
    # ⚠️ 2026-10-11：这句原来钉的是「投他等于浪费一票」，而引擎按**无效票（不计票）**记
    #    （`engine.py` 那段注释自己都点名了这个说法不一致）—— 字面判据跟着改成引擎口径。
    "白痴不能被投": "无效票",
    "警徽 1.5 票": "1.5",
    "平票 PK": "PK",
    "遗言只在第一夜": "第一夜",
    "摄梦人：梦游者": "梦游者",
    "摄梦人：连续两晚会死": "连续两晚",
    "奶穿（摄梦+解药）": "奶穿",
    # ⚠️ 2026-10-11 加（重写规则书这一轮）：这三条是**新写进模型片段**的口径，
    #    按 `规范.md` §12.2.5 的要求逐条登记成字面判据 —— 以后谁删掉就立刻红。
    "每夜顺序（狼队先出刀）": "狼队先出刀",
    # ⚠️ 2026-10-11：流程那条字面跟着【每天的流程】改写换了 —— 原来钉「出局时能带走人的技能」，
    #    现在正文写的是「出局带走人的技能结算」（夜里的那一口在开头、白天被票出去的那一口在这里）。
    "流程里有『出局带走人』那一步": "出局带走人的技能结算",
    "预言家锁定上警（条件片段）": "锁定上警",
}
_missing = [k for k, mark in MUST.items() if mark not in _key_text]
for k, mark in MUST.items():
    print(f"  {k:<14} {'✅' if mark in _key_text else '❌ 没写'}")

# ── 板子专属的关键规则（2026-10-10 加）────────────────────────────────────────
# `MUST` 表是"所有板子都必须有"的公共规则；狼王版/首夜无刀这种**板子专属**的规则
# 用一张附加表管：**只有当默认板子真有这个角色/开关时**才要求它出现。
# ⚠️ 不能用"写死在 MUST 里"的写法：那样把默认板子切回 `eleven_dream` 时这条闸门会假红。
_king_text = rules_text_for("eleven_wolfking")
KING_MUST = {
    "狼王带人（任何死因）": "任何原因出局都能当场带走",
    "狼王不能带队友": "不能带走狼队友",
    # 2026-11-03：狼王禁爆新规 —— 原来钉的是旧的「自爆=白狼王式」，现在钉新口径
    "狼王不能自爆": "狼王不能自爆",
    "单狼自爆不吞警徽/双爆才吞": "双狼自爆",
    "结算次序（死亡→狼王带人→判屠边）": "死亡结算 → 狼王带人 → 再判屠边",
    "首夜无刀": "首夜无刀",
    "女巫第 2 夜才用药": "第 2 夜",
}
_king_missing = [k for k, mark in KING_MUST.items() if mark not in _king_text]
print("\n狼王版专属规则（按 eleven_wolfking 检查，与默认板子无关）：")
for k, mark in KING_MUST.items():
    print(f"  {k:<24} {'✅' if mark in _king_text else '❌ 没写'}")
if _king_missing:
    print(f"  ❌ 狼王版规则书缺这些关键规则：{_king_missing}")
all_ok = bool(all_ok) and not _king_missing

# ── 屠城 / 出局亮身份（2026-10-30 新增的两张板子）─────────────────────────────
# 屠城是**胜负口径**，写错模型会按屠边去打；出局亮身份是**信息公开口径**，写错推理全歪。
# 这两条只在真有这个规则的板子上要求出现（暗牌板子反过来要求**一个字都没有**，
# 上面 per-board 那段已经守着）。
_reveal_text = rules_text_for("six_all_reveal")
REVEAL_MUST = {
    "出局亮身份": "身份牌当场公开",
    "只亮身份、不亮死因": "死因仍然不公示",
}
_reveal_missing = [k for k, mark in REVEAL_MUST.items() if mark not in _reveal_text]
print("\n6 人明牌屠城板专属规则（按 six_all_reveal 检查）：")
for k, mark in REVEAL_MUST.items():
    print(f"  {k:<18} {'✅' if mark in _reveal_text else '❌ 没写'}")
if _reveal_missing:
    print(f"  ❌ 明牌板规则书缺这些关键规则：{_reveal_missing}")
all_ok = bool(all_ok) and not _reveal_missing

_nine_text = rules_text_for("nine_all")
print("\n9 人屠城板专属规则（按 nine_all 检查）：")
for label, mark in (("屠城胜负", "屠城"), ("杀光所有好人", "杀光所有好人")):
    ok = mark in _nine_text
    print(f"  {label:<12} {'✅' if ok else '❌ 没写'}")
    if not ok:
        all_ok = False

# 没有退出码 = 对 run_all 永远绿的"假回归门"（只读审查 2026-10-07 指出）。
# 只要有一块板子的片段发错/漏发（ok=False），这里就以非 0 退出。
if _missing:
    print(f"  ❌ 规则书里缺这些关键规则：{_missing}")
raise SystemExit(0 if all_ok else 1)
