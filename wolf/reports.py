# coding=utf-8
"""对局结果上报与统计（2026-10-31 加）。

为什么要它：作者想判断胜率，但一局一局看太慢；朋友愿意贡献自己的对局结果，就能攒出样本。

设计口径
--------
· **只存摘要**：局 id、板子、胜负、天数、座位数、真人数、各座位角色、胜方剩几人 ——
  **不存发言、不存思考链、不存 key、不存 IP/名字**。
· **存在房主本机**：`var/reports/reports.jsonl`（一行一局，gitignore，不进发布包）。
· **去重**：同一局 + 同一上报座位只记一次（多人点「贡献」不会记多份）。
· **可导出**：`export_json()` 给房主拿去跨机器合并。
"""
from __future__ import annotations

import io
import json
import os
import threading
import time

MAX_BYTES = 4096                 # 单条上限，超了直接拒（防止有人拿它当文件上传）
FILENAME = os.path.join("var", "reports", "reports.jsonl")
_LOCK = threading.Lock()


def path(base_dir: str) -> str:
    return os.path.join(base_dir, FILENAME)


def summary_from_save(save: dict, game_id: str, reporter: str = "") -> dict:
    """从存档算一份摘要。**只取白名单字段**，保证不会把发言/思考带出去。"""
    seats = save.get("seats") or []
    winner = str(save.get("winner") or "")
    wolf_camp = {"wolf", "wolf_king"}
    roles = {}
    humans = 0
    for s in seats:
        sid = str(s.get("id"))
        roles[sid] = str(s.get("role") or "")
        if s.get("is_human"):
            humans += 1
    alive_winner = sum(1 for s in seats
                       if s.get("alive") and ((str(s.get("role")) in wolf_camp) == (winner == "wolf")))
    return {
        "v": 1,
        "game_id": game_id,
        "board": str(save.get("board") or ""),
        "winner": winner,
        "days": int(save.get("day") or 0),
        "seats": len(seats),
        "humans": humans,
        "roles": roles,
        "winner_alive": alive_winner,
        "reporter": reporter[:24],
        "at": int(time.time()),
    }


def append(base_dir: str, item: dict) -> tuple[bool, str]:
    """追加一条。返回 (是否新写入, 原因)。"""
    if not isinstance(item, dict) or not item.get("game_id") or not item.get("winner"):
        return False, "缺 game_id 或 winner"
    if len(json.dumps(item, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        return False, "太大"
    full = path(base_dir)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with _LOCK:
        seen = set()
        if os.path.isfile(full):
            with io.open(full, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        old = json.loads(line)
                    except ValueError:
                        continue
                    seen.add((old.get("game_id"), old.get("reporter")))
        if (item.get("game_id"), item.get("reporter")) in seen:
            return False, "这局已经收过了"
        with io.open(full, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return True, ""


def load(base_dir: str) -> list[dict]:
    full = path(base_dir)
    out: list[dict] = []
    if not os.path.isfile(full):
        return out
    with io.open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


def aggregate(items: list[dict]) -> dict:
    """算胜率。按「局」去重（同一局多人上报只算一局）。"""
    by_game: dict[str, dict] = {}
    for it in items:
        by_game.setdefault(str(it.get("game_id")), it)
    games = list(by_game.values())
    total = len(games)
    wolf = sum(1 for g in games if g.get("winner") == "wolf")
    good = sum(1 for g in games if g.get("winner") == "good")
    draw = total - wolf - good
    days = [int(g.get("days") or 0) for g in games if g.get("days")]
    boards: dict[str, dict] = {}
    for g in games:
        b = str(g.get("board") or "?")
        row = boards.setdefault(b, {"games": 0, "wolf": 0, "good": 0})
        row["games"] += 1
        if g.get("winner") == "wolf":
            row["wolf"] += 1
        elif g.get("winner") == "good":
            row["good"] += 1
    return {
        "games": total,
        "wolf": wolf,
        "good": good,
        "draw": draw,
        "wolf_rate": round(wolf / total, 3) if total else 0.0,
        "good_rate": round(good / total, 3) if total else 0.0,
        "avg_days": round(sum(days) / len(days), 1) if days else 0.0,
        "by_board": boards,
    }


def export_json(base_dir: str) -> str:
    return json.dumps(load(base_dir), ensure_ascii=False, indent=1)
