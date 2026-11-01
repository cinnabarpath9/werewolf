# coding=utf-8
"""复盘：把一局存档里的每一次模型调用摊开给你看。

用法：
    python review.py                      # 复盘最近一局
    python review.py --list               # 列出所有存档
    python review.py --seat 3             # 只看 3 号座位
    python review.py --day 2              # 只看第 2 天
    python review.py --reasoning          # 连模型的思考链一起打印
    python review.py --packet             # 连它当时看到的完整上下文一起打印
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):          # GBK 控制台友好
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠️ 角色/任务的名字表要跟引擎同步：引擎 transcript 里记的是**英文 key**
#    （role_key / task.kind），表里缺一个就会在复盘里直接打印英文。
#    2026-10-06 补：白痴（idiot）与警长/遗言/警徽那几个新任务种类。
#: ⚠️ 2026-10-09 修：以前手写一份，角色改名（白痴→愚者）与新增角色（摄梦人/守墓人/
#:    养鸦人/幸运儿）都不会跟上，复盘页就会把新角色打成英文 key。现在从 `ROLES` 派生。
sys.path.insert(0, os.path.dirname(HERE))
try:
    from wolf.roles import ROLES as _R
    ROLE_NAME = {k: v.name for k, v in _R.items()}
    ROLE_NAME["idiot_old"] = "白痴"          # 老存档里可能出现的旧写法，留着兜底
except Exception:                              # noqa: BLE001
    ROLE_NAME = {"wolf": "狼人", "seer": "预言家", "witch": "女巫", "hunter": "猎人",
                 "guard": "守卫", "idiot": "愚者", "villager": "村民"}
TASK_NAME = {
    "analyze": "分析（投票前决策）",
    "speech_plan": "发言前决策",
    "wolf_intent": "狼队定刀","speech": "发言", "vote": "投票", "wolf_kill": "狼刀", "seer_check": "查验",
             "witch": "女巫用药", "guard": "守卫", "hunter_shoot": "猎人开枪",
             "wolf_talk": "狼队讨论", "sheriff_run": "上警决定",
             "sheriff_speech": "竞选发言", "sheriff_vote": "投警长",
             "last_words": "遗言", "badge": "警徽去向",
             "reflect": "复盘写笔记"}


def latest_game(save_dir: str) -> str | None:
    # ⚠️ 2026-10-12：这条 glob 是**非递归**的，所以 mock 局所在的 `<save_dir>/_mock/` 子目录
    #    天然不会被当成本局（这是有意的：演练局不该出现在用户的复盘列表里，见
    #    `server.py::_mock_save_dir`）。要复盘 mock 局就显式指过去：
    #      python tools/review.py --save-dir games/_mock --list
    files = sorted(glob.glob(os.path.join(save_dir, "game-*.json")))
    return files[-1] if files else None


def load(path: str) -> dict:
    """读存档；读不了就给人话并正常退出，不要甩一坨 traceback。"""
    data = load_safe(path)
    if data is None:
        raise SystemExit(1)
    return data


def load_safe(path: str) -> dict | None:
    """读存档，坏了返回 None（--list 要用它跳过坏文件，不能因为一个坏文件就中断）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ 这个存档读不了：{os.path.basename(path)}")
        print(f"   {type(exc).__name__}: {exc}")
        print("   （多半是上次写盘只写了一半；可以直接把这个文件删掉）")
        return None


def hr(char: str = "─", width: int = 78) -> str:
    return char * width


def show_ledger(game: dict) -> None:
    print(hr("="))
    print("公开对局记录（说出口的话）")
    print(hr("="))
    rows = [e for e in game.get("ledger", []) if "public" in (e.get("visible_to") or [])]
    last_day = None
    for entry in rows:
        if entry["day"] != last_day:
            last_day = entry["day"]
            print(f"\n──── 第 {last_day} 天 ────")
        who = f"{entry['seat']}号：" if entry.get("seat") is not None else ""
        print(f"  [{entry['kind']}] {who}{entry['text']}")


def show_transcript(game: dict, seat: int | None, day: int | None,
                    with_reasoning: bool, with_packet: bool) -> None:
    rows = game.get("transcript") or []
    if not rows:
        print("\n（这份存档里没有调用留痕：可能是用 --mock 跑的，或者存档来自旧版本。）")
        return

    print("\n" + hr("="))
    print("模型调用复盘（每次调用它看到了什么、说了什么、想了什么）")
    print(hr("="))

    current_seat = None
    shown = 0
    for row in rows:
        if seat is not None and row["seat"] != seat:
            continue
        if day is not None and row["day"] != day:
            continue
        shown += 1
        if row["seat"] != current_seat:
            current_seat = row["seat"]
            role = ROLE_NAME.get(row.get("role"), row.get("role"))
            print(f"\n{'=' * 78}\n{current_seat}号（{role}）· 模型：{row.get('model') or '未记录'}\n{'=' * 78}")
        head = (f"\n▸ 第 {row['day']} 天 {row.get('phase')} · {TASK_NAME.get(row['task'], row['task'])}"
                f" · {row.get('elapsed')}s · 输入 {row.get('prompt_tokens')} / 输出 {row.get('completion_tokens')} tokens")
        if row.get("cached_tokens"):
            head += f"（其中缓存命中 {row['cached_tokens']}）"
        thinking = row.get("thinking") or {}
        if thinking:
            head += f" · 思考档 {thinking}"
        if (row.get("reply") or {}).get("_replaced"):
            head += " · （这一手是补齐重写后的结果）"
        print(head)
        if row.get("error"):
            print(f"   ⚠️ 调用失败：{row['error']}")
        reply = row.get("reply") or {}
        if reply.get("speech"):
            print(f"   发言：{reply['speech']}")
        if reply.get("claim") and str(reply["claim"]) not in ("无", "None"):
            print(f"   声称身份：{reply['claim']}")
        for key in ("target", "vote", "shoot", "save", "poison"):
            if key in reply and reply[key] is not None:
                print(f"   {key}：{reply[key]}")
        if reply.get("reason"):
            print(f"   它自己给的理由：{reply['reason']}")
        if with_reasoning:
            reasoning = (row.get("reasoning") or "").strip()
            print("   思考链：" + (reasoning if reasoning else "（这次调用没有返回思考链）"))
        if with_packet:
            print("   —— 它当时看到的上下文 ——")
            for line in (row.get("packet") or "").splitlines():
                print("   | " + line)
    print(f"\n共 {shown} 次调用记录。")


def main() -> int:
    parser = argparse.ArgumentParser(description="复盘一局狼人杀")
    parser.add_argument("--game", default=None, help="存档路径；不给就用最近一局")
    parser.add_argument("--save-dir", default=os.path.join(HERE, "games"))
    parser.add_argument("--seat", type=int, default=None)
    parser.add_argument("--day", type=int, default=None)
    parser.add_argument("--reasoning", action="store_true", help="打印模型的思考链")
    parser.add_argument("--packet", action="store_true", help="打印它当时看到的完整上下文")
    parser.add_argument("--list", action="store_true", help="列出所有存档")
    parser.add_argument("--ledger-only", action="store_true")
    args = parser.parse_args()

    if args.list:
        for path in sorted(glob.glob(os.path.join(args.save_dir, "game-*.json"))):
            game = load_safe(path)          # 坏文件只跳过它自己，后面的还要列出来
            if game is None:
                continue
            print(f"{os.path.basename(path)}  第 {game.get('day')} 天 · 结果 {game.get('winner')} "
                  f"· 调用留痕 {len(game.get('transcript') or [])} 条")
        return 0

    path = args.game or latest_game(args.save_dir)
    if not path or not os.path.exists(path):
        print("没找到存档。先跑一局：python run_game.py --mock")
        return 1

    game = load(path)
    print(f"存档：{path}")
    print("真实身份：" + "、".join(
        f"{s['id']}号={ROLE_NAME.get(s['role'], s['role'])}" for s in game.get("seats", [])))
    print(f"结果：{game.get('winner')}（第 {game.get('day')} 天结束）")

    show_ledger(game)
    if not args.ledger_only:
        show_transcript(game, args.seat, args.day, args.reasoning, args.packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
