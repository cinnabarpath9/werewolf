# coding=utf-8
"""变异验证（2026-10-08）：把每条修复**改回旧写法**，跑对应的测试脚本，看它是否变红。

为什么要这样验：新加的断言最容易写成"永远为真"（自证）。只有把代码改回旧写法、
看到它**真的变红**，才能证明那条断言确实钉住了这个缺陷。

做法：备份原文件（记 sha256）→ 做一处替换 → 跑一个测试脚本 → 数 FAIL 条数 → **按字节还原**
→ 再核对 sha256。全部跑完之后还会再核一遍所有文件都还原了。

跑法： python mutation_verify.py
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WW = os.path.dirname(HERE)
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read(path: str) -> str:
    return readb(path).decode("utf-8")


def readb(path: str) -> bytes:
    """⚠️ 必须**按字节**读：用文本模式读会把 CRLF 悄悄变成 LF
    （本仓 engine.py / tabletalk.py / run_game.py 都是 CRLF），
    还原时再写回去就**不是同一个文件**了 —— 实测踩过，这条断言当场抓到。"""
    with open(path, "rb") as fh:
        return fh.read()


def writeb(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def write(path: str, text: str) -> None:
    writeb(path, text.encode("utf-8"))
#: 一次变异：名字、文件、把 `new` 换回 `old`、跑哪个脚本
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── P2-1：替补思考参数永远按 openai ──────────────────────────────────────
    ("P2-1a 座位不再带平台风格（spec.thinking_style 恒为 openai）",
     r"wolf\routing.py",
     "        thinking_style=thinking_style_for(cfg, _profile_name(llm, seat, overlay)),\n",
     "        thinking_style=\"openai\",",
     "t_thinking_style.py"),
    ("P2-1b 替补 spec 不带目标平台风格（resolve_profile 改回不填）",
     r"wolf\routing.py",
     "        # ⚠️ 必须用**目标档案**的风格（不是这个座位原来那家）：换家之后参数名由新家决定。\n"
     "        thinking_style=thinking_style_for(cfg, profile),\n",
     "",
     "t_thinking_style.py"),
    ("P2-1c build_client 恢复 openai 默认值（不传就按 openai 发）",
     r"wolf\routing.py",
     "    style = spec.thinking_style if thinking_style is None else thinking_style\n",
     "    style = thinking_style or \"openai\"\n",
     "t_thinking_style.py"),

    # ── P3-1：认不出的 claim 说成「你还没报过」 ───────────────────────────────
    ("P3-1a 兜底判据改回「归一化成功才算报过」",
     r"wolf\context.py",
     "    return _claim_entry_seen(state, seat_id)\n",
     "    return bool(_claim_of(state, seat_id))\n",
     "t_claim_chapter.py"),
    ("P3-1b 抬头不给原文（认不出就还是说没报过）",
     r"wolf\context.py",
     "                    raw=_raw_claim_of(state, seat.id) if not _claim_of(state, seat.id) else \"\"))\n",
     "                    ))\n",
     "t_claim_chapter.py"),

    # ── P3-3：狼声称「狼人」→ 把自己那一章又附一遍 ───────────────────────────
    ("P3-3 去掉 claimed == seat.role_key 判断",
     r"wolf\context.py",
     "        if claimed and claimed != seat.role_key:\n",
     "        if claimed:\n",
     "t_claim_chapter.py"),

    # ── P3-2：狼刀提议夹带私有 reason ────────────────────────────────────────
    ("P3-2 出刀提议写回来路 + 私有理由原文",
     r"wolf\engine.py",
     "                return wolf, {\"target\": pre[\"target\"],\n"
     "                              \"reason\": \"（沿用今晚早先的决定）\",\n",
     "                why = pre.get(\"reason\") or \"\"\n"
     "                return wolf, {\"target\": pre[\"target\"],\n"
     "                              \"reason\": (f\"（沿用今晚早先的决定：{why}）\"\n"
     "                                         if why else \"（沿用今晚早先的决定）\"),\n",
     "t_night_calls.py"),

    # ── P3-4：公开条目指向「私有记录」+ 理由不进 mind ─────────────────────────
    ("P3-4a 投票文案改回「见自己的私有记录」",
     r"wolf\engine.py",
     "                                      \"reason\": \"（本轮已定票）\",\n",
     "                                      \"reason\": \"（本轮的投票决策，见自己的私有记录）\",\n",
     "t_night_calls.py"),
    ("P3-4b 不把该理由记进 mind",
     r"wolf\engine.py",
     "            if reply.get(\"_prefetched\"):\n"
     "                pre_reason = str(((prefetched or {}).get(voter.id) or {}).get(\"reason\") or \"\").strip()\n"
     "                if pre_reason:\n"
     "                    voter.mind = self._append_mind(\n"
     "                        voter.mind, f\"第 {day} 天我投 {target}号的理由：{pre_reason[:160]}\")\n",
     "",
     "t_night_calls.py"),

    # ── P3-5：配置里的「关」认不出来 ─────────────────────────────────────────
    ("P3-5 _as_bool 改回只认 false/no/off/0/空",
     r"wolf\context.py",
     "        if text in words.FALSE_WORDS:\n            return False\n"
     "        if text in words.TRUE_WORDS:\n            return True\n"
     "        return default                        # 认不出来的写法：宁可退回默认值，不瞎猜\n",
     "        return text not in (\"false\", \"no\", \"off\", \"0\")\n",
     "t_claim_chapter.py"),

    # ── P3-6：开关绕过 --config ─────────────────────────────────────────────
    ("P3-6 attach_claimed_chapter 不再读 set_config_path 指定的那份",
     r"wolf\context.py",
     "    if _CFG_PATH:\n"
     "        paths.append(_CFG_PATH[0])              # ⚠️ 显式指定的那份**优先**，别再拿猜的顶掉它\n",
     "",
     "t_claim_chapter.py"),

    # ── 狼王版（2026-10-10，新默认板子 `eleven_wolfking`）────────────────────
    # 这四条钉的是**最容易写错、错了还看不出来**的四个点：
    # 结算次序（会把狼王的技能吃掉）、首夜短路、不能带队友、只带一次。
    ("K1 结算次序退回「杀完就判胜负」（狼王临死那一口被吃掉）",
     r"wolf\engine.py",
     "        if seat.role_key != \"wolf_king\" and self.check_win():\n",
     "        if self.check_win():\n",
     "t_wolf_king.py"),
    ("K2 首夜短路失效（第 1 夜又去问狼队/女巫/摄梦人）",
     r"wolf\engine.py",
     "        if self.first_night_quiet and day == 1:\n"
     "            return await self._quiet_first_night(day)\n",
     "        if False:\n"
     "            return await self._quiet_first_night(day)\n",
     "t_wolf_king.py"),
    ("K3 狼王可以带走狼队友（名单不再排除自己人）",
     r"wolf\engine.py",
     "        return [s.id for s in self.alive if s.id != king.id and s.camp != WOLF_CAMP]\n",
     "        return [s.id for s in self.alive if s.id != king.id]\n",
     "t_wolf_king.py"),
    ("K4 去掉 king_done 闸门（同一次结算里会被重复触发）",
     r"wolf\engine.py",
     "        king.king_done = True                       # 先落闸：四条死亡路径都不许重复触发\n",
     "        pass\n",
     "t_wolf_king.py"),

    # ── 规则书重写（2026-10-11）：通用片段不许点名「本局没有的角色」──────────────
    # 事故原型：core 里写着「本板子有幸运儿的守护 ＋摄梦人的梦守」——
    # 打到 eleven_standard（没幸运儿、没摄梦人）就是假情报，而旧闸门只看【角色名】标题。
    # ⚠️ 锚点**故意钉在【每夜】这一行**：它是"角色中立"的规则句，很少跟着内容改写动；
    #    原来钉【狼人控场】那条，结果那条口径一改（先扩表、后改能力口径）锚点就飘了两次，
    #    每次都会静默跳过 —— 给变异选锚点要挑**最不容易改**的那一句。
    ("K5 通用片段里泄露本局没有的角色（守卫）",
     r"content\规则书.md",
     "【每夜】各角色按固定顺序行动：**狼队先出刀**，之后轮到谁由本局真有的角色决定；\n",
     "【每夜】各角色按固定顺序行动：**狼队先出刀**，之后轮到谁由本局真有的角色决定"
     "（本局还有守卫的守护，好人不必慌）；\n",
     "check_rules_per_board.py"),

    # ── 锁定上警 = 「开局选项」的条件片段（关着就不许发）────────────────────────
    ("K6 条件片段失效：开着 seer_auto_run 却不发那段",
     r"wolf\rules.py",
     "    if seer_auto_run:\n",
     "    if False:\n",
     "check_rules_per_board.py"),

    # ── 狼人控场的「夜间反击能力」扩表（2026-10-11，原 E31）─────────────────────
    # 退回旧表 = 只剩摄梦人/幸运儿时也判狼胜（作者拍板要改的那一条）。
    ("K7 控场反击表退回旧口径（不认摄梦人 / 幸运儿）",
     r"wolf\engine.py",
     '            if s.role_key in ("seer", "witch", "hunter", "dreamer"):\n'
     '                return True\n'
     '            if s.role_key == "lucky" and not s.lucky_used:\n'
     '                return True\n',
     '            if s.role_key in ("seer", "witch", "hunter"):\n'
     '                return True\n',
     "t_wolf_king.py"),

    # ── E58：换手（替补）那一手的思考档位 `llm.fallback_thinking` ──────────────
    # 两条一起钉，缺一不可：
    #   F1 = 配了要真的生效（覆盖层被摘掉 ⇒ 又变成"只能沿用座位层档位"的旧样子）；
    #   F2 = **不配时不许变**（有人让默认也覆盖 ⇒ 座位 2/8 的 off 被偷偷抬成 low，
    #        正是 2026-10-12 那次「DS 顶班发着思考跑」的成因之一）。
    ("F1 换手不再读 llm.fallback_thinking（把覆盖层摘掉）",
     r"wolf\routing.py",
     "                                  {\"thinking\": fallback_thinking_for(cfg, profile)}]),\n",
     "                                  ]),\n",
     "t_fallback_thinking.py"),
    ("F2 不配时也用 low（把「不写=现状」这条偷偷改掉）",
     r"wolf\routing.py",
     "    table = llm.get(\"fallback_thinking\")\n"
     "    if not isinstance(table, dict):\n"
     "        return None\n",
     "    table = llm.get(\"fallback_thinking\")\n"
     "    if not isinstance(table, dict):\n"
     "        return \"low\"\n",
     "t_fallback_thinking.py"),
]


def run_test(script: str) -> tuple[int, int, list[str]]:
    """跑一个测试脚本，返回（通过数, 失败数, 失败项名字）。

    ⚠️ 有些闸门脚本**只打逐项 ✅/❌、靠退出码报红**（如 `check_rules_per_board.py`），
    输出里没有「共 N 项…」那行 —— 以前这里直接当成"没变红"，于是
    K5/K6 这两条闸门变异**永远显示没变红**（2026-10-11 实测踩到）。
    现在:没有汇总行时按**退出码**判：非 0 = 变红了（失败数记 1，详情取输出尾巴）。
    """
    proc = subprocess.run([PY, script], cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    out = (proc.stdout or "") + (proc.stderr or "")
    total = re.search(r"共 (\d+) 项：通过 (\d+)，失败 (\d+)", out)
    names = re.findall(r"^\s*\[FAIL\]\s+(\S+)", out, re.M)
    if not total:
        if proc.returncode != 0:
            tail = [ln.strip() for ln in out.strip().splitlines() if "❌" in ln][:3]
            return 0, 1, (tail or [f"退出码 {proc.returncode}（脚本没有汇总行）"])
        return (-1, -1, [f"脚本没跑出结论（exit={proc.returncode}）：{out.strip()[-200:]}"])
    return int(total.group(2)), int(total.group(3)), names


def main() -> int:
    backups: dict[str, tuple[bytes, str]] = {}         # path -> (原字节, sha)
    for _name, rel, _new, _old, _script in MUTATIONS:
        path = os.path.join(WW, rel)
        if path not in backups:
            backups[path] = (readb(path), sha(path))

    rows: list[tuple[str, str, str, int, list[str]]] = []
    for name, rel, new, old, script in MUTATIONS:
        path = os.path.join(WW, rel)
        # ⚠️ 锚点里的 \n 要跟文件实际的换行符对上（CRLF 文件里锚点是 \r\n）
        raw = readb(path)
        use_crlf = b"\r\n" in raw
        nb = new.replace("\n", "\r\n").encode("utf-8") if use_crlf else new.encode("utf-8")
        ob = old.replace("\n", "\r\n").encode("utf-8") if use_crlf else old.encode("utf-8")
        if raw.count(nb) != 1:
            print(f"  [跳过] {name} —— 锚点出现 {raw.count(nb)} 次（期望 1 次），不敢盲替")
            rows.append((name, script, "锚点不唯一/找不到", -1, []))
            continue
        writeb(path, raw.replace(nb, ob, 1))
        try:
            passed, failed, names = run_test(script)
        finally:
            writeb(path, backups[path][0])
            assert sha(path) == backups[path][1], f"{rel} 还原失败！"
        print(f"  [变异] {name}\n         {script} → 通过 {passed} / 失败 {failed}"
              f"{'  ← 变红了 ✅' if failed > 0 else '  ← 没变红 ❌'}")
        if names:
            print(f"         失败项：{'、'.join(names[:6])}"
                  f"{' …' if len(names) > 6 else ''}")
        rows.append((name, script, "", failed, names))

    print("\n" + "=" * 78)
    print("变异验证汇总（每条新断言：改回旧写法后红了几项）")
    print("=" * 78)
    ok = 0
    for name, script, note, failed, _names in rows:
        if failed > 0:
            ok += 1
            print(f"  ✅ 红 {failed:>3} 项  {name}\n                   （{script}）")
        else:
            print(f"  ❌ 没变红    {name}  {note}\n                   （{script}）")
    print(f"\n{len(rows)} 个变异：{ok} 个让测试变红，{len(rows) - ok} 个没变红")

    # 收尾：所有被改过的文件都必须跟备份逐字节一致
    print("\n还原核对（sha256）：")
    bad = []
    for path, (_orig, want) in backups.items():
        got = sha(path)
        mark = "OK " if got == want else "❌ "
        if got != want:
            bad.append(path)
        print(f"  {mark} {os.path.relpath(path, WW)}")
    return 1 if bad else 0


raise SystemExit(main())
