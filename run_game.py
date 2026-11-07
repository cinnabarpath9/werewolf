# coding=utf-8
"""命令行入口：离线自测（--mock）或接真实模型。

用法：
    python run_game.py --mock                        # 不调用任何模型，验证引擎与隔离
    python run_game.py --mock --dump-packet 3        # 打印某座位当时看到的上下文（隔离证据）
    python run_game.py --routing                     # 只看每个座位会用哪家平台/哪个模型
    python run_game.py --human 1                     # 你坐 1 号，其余由模型扮演
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

from wolf import context, experience, persona, routing, rules, settings_store, tabletalk  # noqa: E402
from wolf.agents import HumanAgent, LLMAgent, MockAgent  # noqa: E402
from wolf.engine import Game, parse_sheriff_setting  # noqa: E402
from wolf.llm import LLMError  # noqa: E402
from wolf.roles import (BUILTIN_BOARDS, board_summary, make_board,  # noqa: E402
                        role_name, validate_board)

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path: str) -> None:
    """极简 .env 读取，避免额外依赖。

    ⚠️ 2026-10-30：用 `utf-8-sig` 吃掉 BOM（记事本默认会写 BOM，否则第一个变量名
    会变成 `\ufeffKEY`，静默读不到），并且把值两侧的成对引号剥掉（`KEY="sk-x"`）。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key.strip().lstrip("\ufeff"), value)


class Reporter:
    def __init__(self, dump_seats: list[int]) -> None:
        self.dump_seats = set(dump_seats or [])
        self.dumped: set[int] = set()

    def __call__(self, kind: str, **data) -> None:
        if kind == "start":
            print("=" * 78)
            print("身份牌（上帝视角，只有你在看的这份日志里有）：")
            for seat_id, role, human in data["seats"]:
                print(f"  {seat_id}号  {role}{'   ← 这是你' if human else ''}")
            print("=" * 78)
        elif kind == "phase":
            label = "夜晚" if data["phase"] == "night" else "白天"
            print(f"\n{'─' * 26} 第 {data['day']} 天 · {label} {'─' * 26}")
        elif kind == "packet":
            if data["seat"] in self.dump_seats and data["seat"] not in self.dumped:
                self.dumped.add(data["seat"])
                print(f"\n### 隔离证据：{data['seat']}号 在「{data['task']}」时看到的全部上下文 ###")
                print("-" * 78)
                print(data["packet"])
                print("-" * 78)
        elif kind == "warn":
            print(f"  ⚠️  {data['seat']}号 模型调用失败：{data['message']}")
        elif kind == "night_result":
            detail = []
            if data.get("save"):
                detail.append("女巫用了解药")
            if data.get("poison"):
                detail.append(f"女巫毒了 {data['poison']}号")
            if detail:
                print(f"  （夜间内幕：{'，'.join(detail)}）")


def print_routing(cfg: dict, game: Game) -> None:
    print("座位路由（每个座位可以来自不同平台、用不同 key）：")
    for seat in game.seats:
        if seat.is_human:
            print(f"  {seat.id}号 {role_name(seat.role_key):<4} → 你（人类）")
            continue
        spec = routing.resolve(cfg, seat)
        status = "✅" if spec.ready else "❌"
        print(f"  {seat.id}号 {role_name(seat.role_key):<4} → {spec.host or '(未配 base_url)'}"
              f"/{spec.model or '(未配 model)'} {status}")
    print()


def _build_fallbacks(cfg: dict, seat, order: list | None) -> list:
    """按 config 的 `game.fallback` 顺序，给这个座位备好替补客户端。

    返回 [(平台名, 出厂函数), ...]（**懒建**：真要换家时才创建客户端）。任何一家没配好（缺 BASE_URL / KEY / MODEL）就跳过，
    绝不让"替补名单有问题"把开局搞崩 —— 最多就是没有替补可用。
    """
    out = []
    try:
        for name, spec in routing.fallback_specs(cfg, seat, order):
            try:
                    # ⚠️ 存**出厂函数**而不是客户端：连接池等到真要换家时才建
                    # ⚠️ 思考风格**不写死**（P2-1）：`spec` 是**目标档案**解析出来的，
                    #    它自己带着那家平台的风格（QWEN→enable_thinking、
                    #    ARK/GLM→thinking.type、其余→reasoning_effort），
                    #    `build_client` 不传就取 `spec.thinking_style`。
                    #    以前 build_client 的默认值是 openai → 替补一律发 reasoning_effort，
                    #    档位表在替补身上完全失效（换到 QWEN 时正好发的是记过 503 的那个参数）。
                    out.append((name, (lambda sp=spec, nm=name, sid=seat.id:
                                 routing.build_client(sp, label=f"{sid}号/替补{nm}"))))
            except Exception:                             # noqa: BLE001
                continue
    except Exception:                                     # noqa: BLE001
        return []
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="AI 狼人杀内核（命令行版）")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--mock", action="store_true", help="不调用模型，用随机代理自测")
    parser.add_argument("--human", type=int, default=None, help="你坐几号座位（1 起）")
    parser.add_argument("--dump-packet", type=int, action="append", default=[],
                        help="打印该座位某一次决策时看到的完整上下文，可重复")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子，可复现同一局")
    parser.add_argument("--board", default=None,
                        help="板子名：默认取 config.yaml 的 game.board（现在是 eleven_knight），"
                             "也可以点 twelve_standard/nine_all/six_all_reveal，或 config 里自定义的")
    parser.add_argument("--list-boards", action="store_true", help="列出所有可用的板子然后退出")
    parser.add_argument("--routing", action="store_true", help="只打印座位路由然后退出")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    cfg = load_config(args.config)
    # 页面「设置」里保存的 API / 座位 / 语音：命令行也照同一份走，免得两处结果不一样。
    settings_store.apply(cfg, settings_store.load(HERE))
    game_cfg = cfg.get("game") or {}
    # ⚠️ 把**真正读的那份**配置告诉 context：`game.attach_claimed_chapter` 这类开关在
    #    context 层读（它只按"模块旁边 / 当前目录"猜路径，`--config` 指到别处就绕过了）。
    #    必须在这里调到，否则 `run_game.py --config /tmp/x.yaml` 读的开关跟实际配置不是一份。
    context.set_config_path(args.config)

    board_name = args.board or game_cfg.get("board") or "eleven_knight"
    try:
        board_def = make_board(str(board_name), game_cfg.get("boards"))
    except KeyError as exc:
        print(f"❌ {exc}")
        return 2
    board_issues = validate_board(board_def)
    if board_issues:
        print("❌ 这个板子有问题：")
        for line in board_issues:
            print("   - " + line)
        return 2

    human_seat = args.human if args.human is not None else int(game_cfg.get("human_seat") or 0)
    if human_seat and not 1 <= human_seat <= len(board_def.roles):
        print(f"提示：这个板子只有 {len(board_def.roles)} 个座位，{human_seat} 号不在范围内，改回全 AI。")
        human_seat = 0
    # --dump-packet 是 action="append"，所以这里是**列表**（可以传多个座位）
    seat_count = len(board_def.roles)
    bad_dumps = [d for d in (args.dump_packet or []) if not 1 <= int(d) <= seat_count]
    if bad_dumps:
        print(f"提示：--dump-packet {bad_dumps} 不是这个板子的座位号（1~{seat_count}），"
              f"这几个不会打印任何包。")
        args.dump_packet = [d for d in (args.dump_packet or []) if d not in bad_dumps]
    seed = args.seed if args.seed is not None else int(game_cfg.get("seed") or 0)

    # ⚠️ 0 表示"每局随机发牌"（config.yaml 的注释就是这么写的，手机端也固定发 0），
    #    所以要**同时**把它归一成 None 传给引擎和 mock 的 rng —— 只统一一边就会
    #    "发牌固定、行为随机"或反过来（以前就是这样：引擎固定、MockAgent 随机）。
    seed_for_use = seed or None
    rng = random.Random(seed_for_use)
    clients = []

    def factory(seat):
        if seat.is_human:
            return HumanAgent(seat.id)
        if args.mock:
            return MockAgent(seat, rng)
        spec = routing.resolve(cfg, seat)
        # 思考参数按平台风格发（qwen/ark/glm/openai）—— 见 wolf/llm.py 的说明
        client = routing.build_client(
            spec, label=f"{seat.id}号/{spec.model}",
            thinking_style=routing.thinking_style_for(cfg, routing._profile_of(cfg, seat)))
        clients.append(client)
        return LLMAgent(client, seat.id,
                     fallbacks=_build_fallbacks(cfg, seat, game_cfg.get("fallback")))

    async def on_stall(seat, task, error: str, stalled: int = 1) -> str:
        """命令行里的"上游连续失败"处理：暂停，问用户怎么办。

        手机端有弹窗；命令行就地问一句。非交互环境（管道/重定向）拿不到输入时，
        按「跳过」继续 —— 绝不能因为读不到 stdin 就把整局卡死。
        """
        print("\n" + "!" * 78)
        print(f"⏸  {seat.id}号 正在做「{task.kind}」，自动重试全部失败（客户端重试 + 引擎换家都试过了）：")
        print(f"    {str(error)[:400]}")
        print("    上游（中转站）波动很常见，等几秒再试通常就好。")
        print("!" * 78)
        try:
            raw = input("    [r] 再试一次   [s] 跳过这一手（按规则随机处理）   [a] 放弃本局  > ")
        except EOFError:
            print("    （读不到输入，按「跳过」继续）")
            return "skip"
        pick = (raw or "").strip().lower()
        if pick in ("a", "abort", "放弃"):
            return "abort"
        if pick in ("r", "retry", "重试"):
            return "retry"
        return "skip"

    sheriff = parse_sheriff_setting(game_cfg.get("sheriff", "auto"))
    rules_mode = str(game_cfg.get("rules_mode", "room"))
    game = Game(
        board=board_def,
        human_seat=human_seat,
        agent_factory=factory,
        seed=seed_for_use,
        max_days=int(game_cfg.get("max_days", 20)),
        on_event=Reporter(args.dump_packet),
        pk_on_tie=bool(game_cfg.get("pk_on_tie", True)),
        wolf_control=bool(game_cfg.get("wolf_control", True)),
        sheriff=sheriff,
        sheriff_min_seats=int(game_cfg.get("sheriff_min_seats", 11)),
        # 警长每天自选发言方向（顺序/逆序）—— 用户 2026-10-08 加；关掉退回老口径
        sheriff_picks_order=bool(game_cfg.get("sheriff_picks_order", True)),
        # 狼人自爆（变版规则）—— 用户 2026-10-08 加
        wolf_explode=bool(game_cfg.get("wolf_explode", True)),
        # ⚠️ 2026-11-04 审查 P2：CLI 局以前没接这三项 → config 写 extract/限额在 CLI 里静默无效
        context_compress=str(game_cfg.get("context_compress") or "off"),
        call_timeout=float(game_cfg.get("call_timeout") or 0),
        decision_timeout=float(game_cfg.get("decision_timeout") or 0),
    # 四狼刀口完全一致时跳过夜聊（作者 2026-10-09 批准的成本折中）
    wolf_talk_skip_when_unanimous=bool(
        game_cfg.get("wolf_talk_skip_when_unanimous", True)),
    # 预言家锁定上警（作者 2026-10-10）
    seer_auto_run=bool(game_cfg.get("seer_auto_run", True)),
        on_stall=None if args.mock else on_stall,
        rules_mode=rules_mode,
        rules_detail=str(game_cfg.get("rules_detail", "brief")),
        # 所有座位共用的两段文本（取代按座位的角色卡，见 wolf/tabletalk.py）
        player_prompt=tabletalk.player_prompt(cfg, HERE),
        # 打法书（按角色分发）+ 边界口径（屠边优先 → 狼胜）
        playbook_text=(tabletalk.load_playbook(HERE, str(game_cfg.get("playbook_path") or ""))
                       if game_cfg.get("playbook", True) else ""),
        edge_order=str(game_cfg.get("edge_order", "sides_first")),
        auto_retries=int(game_cfg.get("auto_retries", 2)),
        retry_backoff=list(game_cfg.get("retry_backoff") or [3, 8]),
        retry_jitter=float(game_cfg.get("retry_jitter", 0.3)),
        carry_reasoning=bool(game_cfg.get("carry_reasoning", True)),
        carry_reasoning_chars=int(game_cfg.get("carry_reasoning_chars", 300)),
        # 白天流程：决策在前、发言在后（见 wolf/engine.py 的 analyze_round / decide_for_speech）
        decision_first=bool(game_cfg.get("decision_first")),
        meta_text=(tabletalk.load_meta_book(HERE, str(game_cfg.get("meta_book_path") or ""))
                   if game_cfg.get("meta_book", True) else ""),
    )
    if rules_mode in ("room", "both"):
        notice = rules.room_notice(game.rules_text)
        if notice:
            game.ledger.add(1, "day", "announce", notice)

    # 座位人设（表达层，身份无关，每局重新发身份也照样适用）
    personas = persona.load(cfg, HERE)
    for line in personas.warnings:
        print("  ⚠️  " + line)
    for seat in game.seats:
        if seat.id in personas:
            seat.persona = personas.get(seat.id)
            seat.card_name = personas.names().get(seat.id, "")

    if args.list_boards:
        print("内置板子：")
        for key, item in BUILTIN_BOARDS.items():
            if not getattr(item, "visible", True):
                continue
            mark = "←当前" if key == board_def.name else ""
            print(f"  {key:<14} {board_summary(item)} {mark}")
        custom = game_cfg.get("boards") or {}
        if custom:
            print("config.yaml 里的自定义板子：")
            for key in custom:
                try:
                    print(f"  {key:<14} {board_summary(make_board(key, custom))}")
                except Exception as exc:                # noqa: BLE001
                    print(f"  {key:<14} ❌ {exc}")
        return 0

    print(f"板子：{board_def.name} · {board_summary(board_def)}"
          + (f"（{board_def.desc}）" if board_def.desc else ""))
    if personas:
        detail = "、".join(f"{i}号←{personas.sources.get(i, '?')}" for i in sorted(personas.texts))
        print(f"座位人设：{len(personas.texts)} 份（{detail}）")
    print()

    if args.routing:
        print_routing(cfg, game)
        return 0

    # 开跑前把配置问题一次性列出来，别等第一回合才崩
    if not args.mock:
        print_routing(cfg, game)
        issues = routing.problems(cfg, game.seats)
        if issues:
            print("❌ 有座位拿不到模型配置：")
            for line in issues:
                print("   - " + line)
            print("\n最快的办法：把 .env.example 复制成 .env，填上 key。")
            print("只想先看引擎效果：python run_game.py --mock")
            return 2
        try:
            for seat in game.seats:                 # 预建代理：配置有问题现在就报
                game.agent_for(seat)
        except LLMError as exc:
            print(f"❌ {exc}")
            return 2

        # 跨局经验（可选）：每局仍然是全新对话，只注入"去身份化"的复盘笔记
        if game_cfg.get("experience"):
            exp_dir = os.path.join(HERE, str(game_cfg.get("experience_dir") or "experience"))
            notes = experience.load_all(exp_dir)
            injected = 0
            for seat in game.seats:
                client = getattr(seat.agent, "client", None)
                if client is not None and notes.get(client.key):
                    seat.experience = notes[client.key]
                    injected += 1
            print(f"跨局经验：已给 {injected} 个座位注入过往复盘笔记（来自 {exp_dir}）\n")

    if not args.mock and not any(s.is_human for s in game.seats):
        print("提示：全 AI 对局会给每个座位都发真实模型请求（花钱）。想零成本试跑加 --mock。\n")

    # ⚠️ 客户端要活到**所有**还要用它的步骤之后：局后写经验笔记也要用同一个客户端。
    # 以前是 `finally: await client.aclose()` 紧跟在 game.run() 后面，而
    # experience.update_all(...) 在它**之后** —— 写笔记时客户端已经关了，
    # 每个座位都抛错、又被 update_all 静默吞掉，结果一条笔记都写不出来（还白等重试）。
    # 现在：正常路径在经验更新之后关；异常路径在下面的 finally 里兜底关（只关一次）。
    closed = False

    async def close_clients() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for client in clients:
            await client.aclose()

    try:
        winner = await game.run()

        print("\n" + "=" * 78)
        print("公开对局记录（这就是「说出口的话」的完整台账）")
        print("=" * 78)
        print(game.ledger.render_public(game.alive_ids(), game.dead_list(), game.day))

        print("\n" + "=" * 78)
        verdict = {"good": "好人阵营获胜", "wolf": "狼人阵营获胜", "draw": "判和"}.get(winner, winner)
        print(f"结果：{verdict}（第 {game.day} 天结束）")
        print("真实身份：" + "、".join(
            f"{s.id}号={s.role.name}{'（你）' if s.is_human else ''}" for s in game.seats))

        if clients:
            print("\n模型用量（分座位）：")
            total_in = total_out = total_calls = total_cached = 0
            for client in clients:
                u = client.usage
                total_in += u["prompt_tokens"]
                total_out += u["completion_tokens"]
                total_calls += u["calls"]
                total_cached += u.get("cached_tokens", 0)
                print(f"  {client.label} @ {client.host}: 调用 {u['calls']} 次，"
                      f"输入 {u['prompt_tokens']}（缓存 {u.get('cached_tokens', 0)}），"
                      f"输出 {u['completion_tokens']} tokens，"
                      f"错误 {u['errors']}，平均 {u['seconds'] / max(u['calls'], 1):.1f}s")
            print(f"  合计：{total_calls} 次调用，输入 {total_in}（缓存 {total_cached}），"
                  f"输出 {total_out} tokens")

        if not args.no_save:
            save_dir = str(game_cfg.get("save_dir") or "games")
            if not os.path.isabs(save_dir):
                save_dir = os.path.join(HERE, save_dir)  # 相对路径会跟着启动目录跑，钉死在项目里
            path = game.save(save_dir)
            print(f"\n存档：{path}")
            print("复盘：python tools/review.py --reasoning   （看每个模型的发言和思考链）")

        # 局后让每个模型写一份去身份化的经验笔记（可选）——
        # 这一步必须在 close_clients() 之前，否则笔记一条都写不出来
        if game_cfg.get("experience") and not args.mock and game.winner:
            exp_dir = os.path.join(HERE, str(game_cfg.get("experience_dir") or "experience"))
            print("\n正在让每个模型写这一局的复盘笔记…")
            notes = await experience.update_all(game, exp_dir)
            for key, note in notes.items():
                print(f"  ✅ {key}：{note[:60]}…")
    finally:
        await close_clients()
    return 0


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"⚠️  配置文件不存在：{path}")
        print("    会退回内置默认（板子 / 座位路由 / 思考档位都按默认走），注意核对再开局。")
        return {}
    with open(path, encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            print(f"❌ 配置文件不是合法 YAML：{path}")
            print(f"   {exc}")
            raise SystemExit(2)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断。")
        raise SystemExit(130)
