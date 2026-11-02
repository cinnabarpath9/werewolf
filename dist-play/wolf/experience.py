# coding=utf-8
"""跨局经验：每局结束后让模型写一份「去身份化」的复盘笔记。

为什么不是"继承上一局的对话"
--------------------------
API 是无状态的，所谓"继承上下文"实际就是把上一局的文本塞进这一局的输入里。
狼人杀里身份、编号、阵营每局都重排，直接把旧对话续上必然混淆：
它会把"上一局我是预言家"和"这一局我是狼"混在一起，也会把旧的玩家编号当成现在的编号。

正确做法是"经验"而不是"记忆"：
局后写一份**不含本局具体的人和局面**的策略心得，下一局以「过往经验」注入，
并在上下文里明确标注"这不是本局的事实"。经验按**模型**存，不按座位存 —— 同一个模型
坐哪个位置学到的策略是相通的，而不同模型的经验互不通用（给 GLM 看 DeepSeek 的心得没意义）。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
import json
import os
import re
import time

from .context import Task
from .engine import truthy          # 用同一张"是/否"表，别再自己写裸真值判断
from .roles import camp_label, role_name

EXPERIENCE_SYSTEM = """你在复盘自己刚打完的一局狼人杀，目的是给「未来的自己」留下可复用的经验。

硬性要求：
1. 只写**行为模式与策略**，例如："第一天就跳预言家的人，如果之后没人保他，多半是真的。"
2. **绝对不要**写本局的具体座位号、具体玩家、本局谁是什么身份 —— 下一局的编号和身份全都不同，
   写这些只会误导未来的自己。
3. 可以写"我当狼时……""我当好人时……""我下次应该……"这类能迁移到任何一局的结论。
4. 用中文写 3-6 条短句，总长不超过 400 字。
5. 只输出一个 JSON 对象：{"note": "<经验正文>"}"""


def slug(model: str) -> str:
    """文件名前缀：把「平台/模型」洗成能直接当文件名的可读片段（**不保证唯一**）。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(model or "unknown"))[:80]


def _path(store_dir: str, key: str) -> str:
    """一个 key（"平台/模型"）对应一个固定文件名。

    ⚠️ 光用 slug(key) 会**碰撞**：slug 截断到 80 字符，两个只有末尾不同的 key
    （实测：同一网关下两个长模型名，前 80 字符一模一样）会落到同一个文件里，
    后写的把先写的抹掉 —— 而且看起来一切正常，只是经验莫名其妙丢了。
    所以后面再接 sha1(key)[:12]：既保持人眼可读的前缀，又保证一一对应。
    """
    digest = hashlib.sha1(str(key or "").encode("utf-8")).hexdigest()[:12]
    return os.path.join(store_dir, f"{slug(key)}-{digest}.json")


def write_note(store_dir: str, key: str, payload: dict) -> str:
    """原子写：先写同目录的临时文件，再 os.replace 顶上去。

    直接 open(path, "w") 的话，进程在写一半时被打断（Ctrl-C、关机、崩溃）
    会留下一个**半截 JSON**；load_all 读到坏文件只能整份跳过 —— 上一次的经验就没了。
    os.replace 在同一文件系统内是原子的：读到的要么是旧文件、要么是新文件。

    ⚠️ 临时文件名必须**每次唯一**（pid + 线程号 + 随机段）。第一版只带 pid：
    同一进程里两处并发写同一个 key 会撞同一个 tmp，实测 4 线程里 3 个在 os.replace 上
    抛 `PermissionError: 另一个程序正在使用此文件`（Windows）—— 文件没坏是运气，
    但那些**写入其实失败了**。
    """
    os.makedirs(store_dir, exist_ok=True)
    path = _path(store_dir, key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        # 目标文件可能被索引/杀毒短暂占用（Windows 常见）：重试几次，别把写入丢掉
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if os.path.exists(tmp):            # replace 成功后 tmp 已经不在了
                os.remove(tmp)
        except OSError:
            pass
    return path


def load_all(store_dir: str) -> dict[str, str]:
    """读回所有模型的体验笔记：{"平台/模型": 经验正文}。

    键统一成 `平台/模型`：新文件用 key 字段；没有 key 的老文件用 host/model 拼回来，
    再退回 `model` 单独一段 —— 以前老文件直接拿 `model` 当键（"deepseek-chat"），
    而调用方查的是 `platform/model`，于是永远匹配不上、笔记等于没写。

    ⚠️ 文件名改版（加了 sha1 后缀）之后，同一个 key 可能**同时**存在旧文件名和新文件名。
    两个都读的话，谁赢就取决于 os.listdir 的返回顺序 —— 最新写下的那份可能被旧文件顶掉。
    所以这里分两趟：先把规范文件名（_path 算出来的那个）里的笔记收下，
    再收旧文件名的，且**不覆盖**已有的键。
    """
    notes: dict[str, str] = {}
    if not os.path.isdir(store_dir):
        return notes
    parsed: list[tuple[str, str, str]] = []          # (文件名, key, note)
    for name in os.listdir(store_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(store_dir, name), encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                continue
            host = str(payload.get("host") or "").strip()
            model = str(payload.get("model") or "").strip()
            key = str(payload.get("key") or "").strip()
            if not key:
                # 老格式：key 字段是后来才加的。有 host 就拼成 host/model（这才是调用方查的键），
                # 只有 model 就退回 model 单段。
                key = f"{host}/{model}" if host and model else (model or name[:-5])
            note = str(payload.get("note") or "").strip()
            if key and note:
                parsed.append((name, key, note))
        except Exception:                     # noqa: BLE001 —— 坏文件直接跳过
            continue
    # 第一趟：规范文件名（当前真正在写的那份）
    for name, key, note in parsed:
        if name == os.path.basename(_path(store_dir, key)):
            notes[key] = note
    # 第二趟：旧文件名的，只补空位（不覆盖刚读到的规范文件）
    for _name, key, note in parsed:
        notes.setdefault(key, note)
    return notes


def seat_summary(game, seat, max_calls: int = 40) -> str:
    """这一局这个座位自己经历了什么（只用它自己看得到的信息）。"""
    lines = [f"你这一局的身份是：{role_name(seat.role_key)}（{camp_label(seat.camp)}）"]
    if not seat.alive:
        lines.append(f"你在第 {seat.death_day} 天出局了。")
    lines.append(f"本局结果：{'狼人' if game.winner == 'wolf' else '好人' if game.winner == 'good' else '判和'}阵营获胜。")
    lines.append("")
    lines.append("你这局的行动记录（你当时的判断与选择）：")
    count = 0
    for row in game.transcript:
        if row["seat"] != seat.id or count >= max_calls:
            continue
        count += 1
        reply = row.get("reply") or {}
        if row["task"] == "speech":
            detail = f"发言：{str(reply.get('speech', ''))[:120]}"
        elif row["task"] == "vote":
            voted = reply.get("vote")
            detail = ("弃权（这一轮没投票）" if voted in (None, "", 0, "0")
                      else f"投票给 {voted}号（你的理由：{str(reply.get('reason', ''))[:60]}）")
        elif row["task"] == "wolf_kill":
            detail = f"提议击杀 {reply.get('target')}号"
        elif row["task"] == "seer_check":
            detail = f"查验 {reply.get('target')}号"
        elif row["task"] == "witch":
            detail = (f"解药={'用' if truthy(reply.get('save'), default=False) else '不用'}，"
                      f"毒药={reply.get('poison') or '不用'}")
        elif row["task"] == "guard":
            detail = f"守护了 {reply.get('target') or '（没守）'}号"
        elif row["task"] == "hunter_shoot":
            detail = f"开枪={reply.get('shoot') or '没开'}"
        else:
            detail = str(reply)[:120]
        lines.append(f"  第 {row['day']} 天 {row['task']}：{detail}")
    return "\n".join(lines)


async def update_all(game, store_dir: str, old_notes: dict[str, str] | None = None,
                     max_note_chars: int = 1200) -> dict[str, str]:
    """让每个「真实模型座位」写一份新的经验笔记；返回 {模型名: 新笔记}。

    同一个模型只更新一次（多个座位共享一份经验）。
    """
    os.makedirs(store_dir, exist_ok=True)
    # 没显式传旧笔记就从磁盘读回来 —— 否则"和上一局合并"这段永远不生效、每局整份覆盖
    old_notes = dict(old_notes or {}) or load_all(store_dir)
    updated: dict[str, str] = {}
    done_models: set[str] = set()

    for seat in game.seats:
        agent = seat.agent
        client = getattr(agent, "client", None)
        if client is None:                     # Mock / Human 座位跳过
            continue
        # ⚠️ 归账要用**原模型**，不是"当前客户端"：换过家的座位，`agent.client` 已经指向替补，
        #    直接拿它当 key 的话，这一局的复盘会记到替补名下 —— 而一局里绝大多数手是原模型打的，
        #    等于原模型白打一局、替补白捡一份经验（实测发现）。
        owner = getattr(agent, "primary", None) or client
        key = owner.key                        # "平台/模型"
        if key in done_models:
            continue
        done_models.add(key)

        packet = (
            EXPERIENCE_SYSTEM
            + "\n\n———— 你这一局的情况 ————\n"
            + seat_summary(game, seat)
        )
        swapped = int(getattr(agent, "switch_count", 0) or 0)
        if swapped:
            packet += (f"\n\n（提醒：这一局你有 {swapped} 次调用因为上游失败换成了别的模型代打，"
                       f"复盘时以你自己做出的那些判断为主。）")
        previous = old_notes.get(key)
        if previous:
            packet += (
                "\n\n———— 你之前积累的经验 ————\n"
                + previous
                + "\n\n请把「之前的经验」和「这一局的新体会」合并重写成一份更精炼的笔记："
                  "保留仍然成立的，去掉被这一局推翻的，别让笔记越滚越长。"
            )
        try:
            reply = await agent.ask(packet, Task(kind="reflect", text="复盘", schema='{"note": "..."}'))
        except Exception:                      # noqa: BLE001
            continue
        note = str((reply or {}).get("note") or "").strip()
        if not note:
            continue
        note = note[:max_note_chars]
        updated[key] = note
        # 原子落盘（tmp + os.replace）：见 write_note 的注释。
        # ⚠️ 单个模型写失败**不能牵连其它模型**：以前这里没有 try，一处抛异常
        #    整个 update_all 就中断了，后面那些模型的经验全丢。
        try:
            write_note(store_dir, key, {
                "key": key, "model": owner.model, "host": owner.host,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
            })
        except Exception:                      # noqa: BLE001
            continue                           # 写不进去就跳过这一份，别的模型照写
    return updated
