# coding=utf-8
"""实战档：真打对局，用**客观指标**评一个模型打得好不好（不是看它说得好不好听）。

两种打法：
  `--mode single`（默认，便宜）：被测模型只坐 **1 个座位**，其余座位用固定参考对手；
       因为它每局的身份/座位不同，跑 K 局就换 K 个座位 —— 比的是"它自己做得怎么样"。
  `--mode all`（贵，公平）：被测模型坐满所有 AI 座位，比的是"它跟自己人打，能不能赢"。

指标（全部由代码从台账 + 每次决策的原始回复算出来，不靠人看）：
  · 合法率     —— 它给的目标在不在 legal 里（不在就是被引擎兜底改成随机了：说明它没看懂选项）
  · 格式失败   —— 回复解析不出 JSON / 缺必填字段
  · 决策命中率 —— 投票/查验/刀人/毒/枪 有没有打中"对面的阵营"（好人打中狼、狼打中神）
  · 狼队泄漏   —— 狼在公开发言里把队友说成队友（这是真违规；单纯"怀疑队友"是正常战术，只统计不扣分）
  · 警徽去向   —— 警长把警徽传给好人还是狼（新机制，传错等于白送）
  · 存活/胜负   —— 它自己活到第几天、它那一方赢没赢（single 模式下这才是"个人贡献"）

跑法：
    python tools/exam_live.py --mode single --games 3 --seed 7            # 参考对手用 mock（免费）
    python tools/exam_live.py --mode all --games 2 --models tools/exam_models.yaml
    python tools/exam_live.py --selftest                                  # 离线自检（不联网、不花钱）

高难度身份探针（`--roles`，回答作者那个问题：「便宜模型拿到难度高的身份能正常发挥吗？
别直接就把某一队带崩了」）：
    python tools/exam_live.py --board eleven_wolfking --roles seer,witch,wolf_king,wolf,dreamer \\
        --role-games 2 --only DS
    python tools/exam_live.py --roles seer --role-games 2 --baseline reports/exam-live-roles-XX.json

    · 语义：**同一个被测模型**逐局被**固定在**指定身份上（其余座位由同一套参考对手扮演）；
      每局换座位号（避免"位置偏好"被当成能力）。`--role-games N` = 每个身份打几局（默认 2）。
      ⚠️ 只支持 `--mode single`：全座位模式下"固定在某一个身份"没有意义。
    · 板子上没有的身份会被**跳过并说清原因**（例如 `eleven_standard` 没有狼王/摄梦人）。
    · 按身份分别统计：**本阵营胜率**（该身份所在阵营）、决策命中率、合法率、格式失败率、
      狼队泄漏率（只有狼座位有）、**关键机制失误率**（女巫毒到好人 / 预言家验重复或验死人 /
      狼王那一口没换成屠边）。复用现有的 `metrics()`，只是**不改它**、在探针里另算更严的口径。
    · **结论判据**：同身份下本阵营胜率比基线低 ≥15 个百分点，或关键机制失误率 ≥2 倍
      ⇒ 打印「⚠️ 这个模型扛不住 <身份>」。基线用 `--baseline PATH` 传一份**此前跑出来的
      同结构报告**（本工具 `--roles` 落盘的 json）；不传基线时只打印本模型自己的绝对值，
      并标注「无基线，不做结论」。
    · **默认行为一字不变**：不传 `--roles` 时输出与以前完全一样（探针是独立的一条路）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import random
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
                    out.append((name, (lambda sp=spec, nm=name, sid=seat.id:
                                 routing.build_client(sp, label=f"{sid}号/替补{nm}"))))
            except Exception:                             # noqa: BLE001
                continue
    except Exception:                                     # noqa: BLE001
        return []
    return out


def _prod_thinking() -> dict:
    """生产（config.yaml）的思考档表 —— 考试默认跟它一致，否则测的不是真局里的那个模型。

    `llm.thinking` 是"按任务 + 天数"的档位表（例如 speech: {1: low, 3: high}）。
    工具以前不传 thinking，客户端就退到 llm.py 的 DEFAULT_THINKING —— 比生产**想得更少**，
    于是"考试里谁更强"跟"真局里谁更强"可能不是一回事。
    ⚠️ 2026-10-10 起代码兜底也改成"座位满档、非座位低档"，**和生产配置一致** ——
    所以 t_tool_drift 那条"必须和兜底不一样"的检查改成"用特制表证明它真读配置"。
    """
    try:
        return dict((_prod_game_cfg_llm() or {}).get("thinking") or {})
    except Exception:                                     # noqa: BLE001
        return {}


def _prod_game_cfg_llm() -> dict:
    try:
        import yaml
        with io.open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
            return ((yaml.safe_load(fh) or {}).get("llm") or {})
    except Exception:                                     # noqa: BLE001
        return {}


def _prod_game_cfg() -> dict:
    """读 config.yaml 的 game 段（考试要跟生产用同一组开关）。

    读不到就返回空 dict —— 考试不该因为配置文件有问题就跑不起来（调用处的默认值兜底）。
    """
    try:
        import yaml
        with io.open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
            return ((yaml.safe_load(fh) or {}).get("game") or {})
    except Exception:                                     # noqa: BLE001
        return {}

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from exam import Model, load_env, load_models               # noqa: E402

from wolf import routing, tabletalk                                    # noqa: E402
from wolf.agents import LLMAgent, MockAgent                 # noqa: E402
from wolf.engine import Game, timeouts_from_cfg                # noqa: E402
from wolf.retired_boards import RETIRED_BOARDS, retired_board          # noqa: E402
from wolf.roles import role_name                             # noqa: E402
from wolf.roles import make_board as _product_make_board      # noqa: E402


def make_board(name, custom: dict | None = None):
    """实测工具用的板子解析：退役的 11 人板走测试副本（自检里还在用那几张）。"""
    if isinstance(name, str) and not (custom and name in custom) and name in RETIRED_BOARDS:
        return retired_board(name)
    return _product_make_board(name, custom)

GODS = ("seer", "witch", "hunter", "guard")


class Recorder:
    """包住一个代理，把它每次"看到了什么选项、回了什么"记下来（指标全靠这份记录）。"""

    def __init__(self, inner, seat_id: int, log: list, mine: bool = True,
                 state_fn=None) -> None:
        self.inner = inner
        self.seat_id = seat_id
        self.log = log
        self.mine = mine                       # 是不是被测模型自己的决策
        #: 身份探针才需要：决策**当时**的局面快照（第几天、谁还活着）。
        #: 为什么必须"当时"：女巫毒人/狼王带人这些判定要问"那一刻谁活着"，
        #: 打完再回头算会把"当时活着、后来死了"的人算错（验死人/清零都靠它）。
        #: 不传（默认）= 老行为，日志里不多任何字段。
        self.state_fn = state_fn

    async def ask(self, packet, task):          # noqa: ANN001
        reply = await self.inner.ask(packet, task)
        record = {"seat": self.seat_id, "task": task.kind, "legal": list(task.legal),
                  "reply": reply if isinstance(reply, dict) else {}, "mine": self.mine}
        if self.state_fn is not None:
            try:
                record.update(self.state_fn())
            except Exception:                   # noqa: BLE001
                pass
        self.log.append(record)
        return reply

    def last_meta(self):                        # noqa: ANN201
        return self.inner.last_meta()

    def set_context(self, *a, **k):             # noqa: ANN002, ANN003
        setter = getattr(self.inner, "set_context", None)
        if setter:
            return setter(*a, **k)
        return None

    @property
    def uses_model(self) -> bool:
        return bool(getattr(self.inner, "uses_model", False))

    # ⚠️ 换家/重试要用到这两个：不透出来的话，引擎 `getattr(agent, "use_fallback")`
    #    拿到 None —— 考试局里"配了换家"等于白配（实测发现）。
    def use_fallback(self):                     # noqa: ANN201
        fn = getattr(self.inner, "use_fallback", None)
        return fn() if callable(fn) else None

    @property
    def model_now(self) -> str:
        return str(getattr(self.inner, "model_now", "") or "")


def spec_for(model: Model) -> routing.SeatLLM:
    # ⚠️ 思考档必须跟生产一致（见 _prod_thinking 的说明）：不带的话客户端会退到
    #    代码里的 DEFAULT_THINKING，比生产想得少，"考试结论"就代表不了真局。
    return routing.SeatLLM(base_url=model.base_url, api_key=model.api_key, model=model.model,
                           timeout=model.timeout, max_tokens=model.max_tokens,
                           max_retries=4, extra_body=model.extra_body,
                           thinking=_prod_thinking() or None)


def is_mock(model: Model) -> bool:
    """`mock://` 开头的"模型"= 用离线 mock 代理冒充被测模型。

    自检必须走这条：第一版没有它，自检里的假模型真的去连 `http://fake` 了 ——
    每次决策都要耗 4 次重试 + 退避（21 次决策跑了好几分钟），而且全部记成"格式失败"，
    自检看起来像"指标坏了"，其实是自检自己在打网络。
    """
    return str(model.base_url).startswith("mock://")


async def preflight(model: Model) -> tuple[bool, str]:
    """开测前先打一发最小请求：key 错/地址错/没额度，现在就知道，别等打完 3 局才发现。"""
    res = await model.ask("只回复一个 JSON：{\"ok\": true}", "你是连通性探针。")
    if res.get("ok"):
        return True, f"连通正常（{res.get('elapsed')}s）"
    return False, str(res.get("error"))[:200]


def metrics(game: Game, log: list, my_seats: set[int]) -> dict:
    """算指标。`my_seats` = 哪些座位是被测模型的。"""
    seats = {s.id: s for s in game.seats}
    mine = [r for r in log if r["mine"] and r["seat"] in my_seats]
    wolves = {s.id for s in game.seats if s.role_key == "wolf"}

    out: dict = {"decisions": len(mine), "illegal": 0, "format_fail": 0, "hits": 0,
                 "hit_total": 0, "mate_exposed": 0, "wolf_speeches": 0,
                 "badge_to_wolf": 0, "badge_decisions": 0, "survived_days": [],
                 "camp_win": None, "days": game.day, "winner": game.winner}

    def is_wolf(seat_id) -> bool:
        return seat_id in wolves

    for r in mine:
        reply = r["reply"]
        if reply.get("_error"):
            out["format_fail"] += 1
            continue
        legal = [int(x) for x in (r["legal"] or [])]
        task = r["task"]
        # 它给的目标（不同任务字段名不同）
        raw = reply.get("vote", reply.get("target", reply.get("check",
              reply.get("shoot", reply.get("to")))))
        if task == "sheriff_run" or task in ("speech", "wolf_talk", "last_words", "sheriff_speech"):
            raw = None
        if raw is not None and legal:
            try:
                num = int(str(raw).strip() or 0)
            except ValueError:
                num = -1
            if num not in legal:
                out["illegal"] += 1
        # 命中率：只有"打中对面阵营"才算命中
        me_wolf = is_wolf(r["seat"])
        target = raw
        if isinstance(target, (int, float)) and int(target) in seats:
            t_seat = seats[int(target)]
            out["hit_total"] += 1
            if task in ("vote", "seer_check", "witch", "hunter_shoot", "wolf_kill"):
                if me_wolf and t_seat.role_key in ("seer", "witch", "hunter", "guard", "idiot"):
                    out["hits"] += 1
                if (not me_wolf) and t_seat.role_key == "wolf":
                    out["hits"] += 1
        # 狼队泄漏：公开发言里把队友说成队友（"我的狼队友是X"）
        if task in ("speech", "wolf_talk") and me_wolf and r["task"] == "speech":
            out["wolf_speeches"] += 1
            text = str(reply.get("speech") or "")
            for mate in wolves - {r["seat"]}:
                if f"{mate}号" in text and ("狼队友" in text or "我的狼" in text):
                    out["mate_exposed"] += 1
                    break
        # 警徽去向：传给了狼就是白送
        if task == "badge":
            out["badge_decisions"] += 1
            if isinstance(reply.get("to"), (int, float)) and int(reply["to"]) in wolves:
                out["badge_to_wolf"] += 1

    for sid in my_seats:
        s = seats.get(sid)
        out["survived_days"].append(s.death_day - 1 if (s and s.death_day) else game.day)
    if my_seats:
        camps = {seats[sid].camp for sid in my_seats if sid in seats}
        if len(camps) == 1:
            camp = camps.pop()
            out["camp_win"] = (game.winner == ("wolf" if camp == "wolf" else "good"))
    return out


# --------------------------------------------------------------------------- #
# 高难度身份探针：把被测模型固定在某个身份上，按身份分项考核
# --------------------------------------------------------------------------- #

#: 探针里算"命中"的任务（与 metrics() 基本一致，外加狼王那一口 —— 它也是一次打人）
HIT_TASKS = ("vote", "seer_check", "witch", "hunter_shoot", "wolf_kill", "wolf_king_take")

#: 判据阈值（`docs/考题设计.md` §3）：
#:   本阵营胜率比基线低 ≥ 15 个百分点，或关键机制失误率 ≥ 2 倍 ⇒ "扛不住这个身份"
WIN_DROP_LIMIT = 0.15
MECH_RATIO_LIMIT = 2.0


def force_seat_role(game: Game, seat_id: int, role_key: str) -> bool:
    """把被测座位**换成**指定身份：跟拿到那个身份的座位**对调**（不是硬改 role_key）。

    为什么必须对调：直接写 `seat.role_key = role_key` 会让角色集合多一个少一个 ——
    板子构成、屠边判定、胜负全都跟着变，那测的就不是"这个模型拿到这个身份"，
    而是"这个模型拿到一个板子上不存在的身份"。
    ⚠️ 必须在 `game.run()` 之前调用（agent 是懒创建的，此刻还没有人缓存过身份）。
    """
    seat = game.seat_by_id(seat_id)
    if seat.role_key == role_key:
        return True
    other = next((s for s in game.seats if s.role_key == role_key and s.id != seat_id), None)
    if other is None:
        return False
    seat.role_key, other.role_key = other.role_key, seat.role_key
    return True


def board_roles(board_name: str) -> list[str]:
    return list(make_board(board_name).roles)


def _target_of(task: str, reply: dict):
    """从回复里取"它选了谁"（各任务字段名不同；witch 的毒在 poison、狼王在 take）。"""
    if task == "witch":
        return reply.get("poison")
    if task == "wolf_king_take":
        return reply.get("take", reply.get("shoot", reply.get("target")))
    if task in ("vote", "seer_check", "wolf_kill", "dream", "lucky", "hunter_shoot"):
        return reply.get("vote", reply.get("target", reply.get("check", reply.get("shoot"))))
    return None


def _seat_int(value) -> int | None:
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return num


def probe_stats(game: Game, log: list, my_seats: set[int], role_key: str) -> dict:
    """身份探针要的两类口径（**不动 `metrics()`**，所以默认输出一字不变）。

    ① **按阵营算的命中率**：现有 `metrics()` 把 `role_key == "wolf"` 当狼，
       于是**狼王（wolf_king）座位会被当成好人**、狼王座位的命中率是反的。
       探针按 `camp` / 神边民边（引擎同款判据）重算一份，并在报告里写明两套口径的差别。
    ② **关键机制失误**（作者最担心的那类崩法）：
       · 女巫：毒到**好人阵营** = 失误（毒到狼算命中，不计失误）；分母是她还有药的每一次
         夜间决策（引擎只在还有药时才叫她），**「不用毒」不算失误**；
       · 预言家：**验重复**（这一局自己已经验过的人）或**验死人**（那一刻已经出局的人）；
       · 狼王：那一口**没换成屠边**（带走他之后，好人那边神边/民边都没清零）或名单非空却没带人。
    统计不到的**如实写进 `missing`**，不猜（见下面的注释）。
    """
    seats = {s.id: s for s in game.seats}
    mine = [r for r in log if r["mine"] and r["seat"] in my_seats]
    out: dict = {"hits": 0, "hit_total": 0, "mech_checked": 0, "mech_mistakes": 0,
                 "mech_detail": [], "missing": [], "checked_seats": []}
    checked: set[int] = set()                     # 预言家这一局已经验过谁（判"验重复"）
    has_state = any("alive" in r for r in mine)

    for r in mine:
        reply = r["reply"] or {}
        if reply.get("_error"):
            continue
        task = r["task"]
        legal = [int(x) for x in (r["legal"] or [])]
        target = _seat_int(_target_of(task, reply))
        alive = set(r.get("alive") or [])
        day = r.get("day")
        where = f"第 {day} 天" if day else "本局"

        if task in HIT_TASKS and target is not None and target in seats:
            out["hit_total"] += 1
            me = seats[r["seat"]]
            tgt = seats[target]
            tgt_god = tgt.kind == "god" or getattr(tgt.role, "also_kind", None) == "god"
            if me.camp == "wolf" and tgt.camp != "wolf" and tgt_god:
                out["hits"] += 1
            if me.camp != "wolf" and tgt.camp == "wolf":
                out["hits"] += 1

        if role_key == "witch" and task == "witch":
            # ⚠️ 分母 = 女巫"手里还有药"的夜间决策次数：引擎**只在还有药时才叫她**
            #    （两瓶都用完那一夜根本不发调用），所以每一次调用都是一次"有机会"。
            #    不这么算的话，"一次毒都不放"会变成"无样本"，看不出毒错率。
            out["mech_checked"] += 1
            if target is None or target in (0,):
                continue                          # 不用毒 = 一个正常选择，不算失误
            if target not in seats:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(f"{where}：毒的目标 {target} 号不存在")
            elif target not in legal:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(
                    f"{where}：毒的目标 {target} 号不在引擎给的合法名单里（这一夜药没花掉）")
            elif seats[target].camp != "wolf":
                out["mech_mistakes"] += 1
                out["mech_detail"].append(
                    f"{where}：毒了 {target} 号（好人阵营）—— 毒到自己人")

        elif role_key == "seer" and task == "seer_check":
            out["mech_checked"] += 1
            if target is None:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(f"{where}：没给出验人目标")
                continue
            bad = []
            if target in checked:
                bad.append("验重复（这个人它这局已经验过）")
            if has_state and alive and target not in alive:
                bad.append("验了当时已经出局的人")
            if target not in legal:
                bad.append("目标不在引擎给的合法名单里")
            if bad:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(f"{where}：验 {target} 号 —— " + "、".join(bad))
            checked.add(target)

        elif role_key == "wolf_king" and task == "wolf_king_take":
            out["mech_checked"] += 1
            if target is None:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(
                    f"{where}：名单里有 {len(legal)} 个可带的好人，但它一个人都没带（技能作废）")
                continue
            if not has_state or not alive:
                continue                          # 拿不到当时局面 ⇒ 不判"清没清零"（见 missing）
            # "让某一边清零" = 带走他之后，好人那边的神边或民边变成 0（等于当场换成屠边）
            good = [seats[i] for i in sorted(alive) if i in seats and seats[i].camp != "wolf"
                    and i != target]
            gods = [s for s in good if s.kind == "god"
                    or getattr(s.role, "also_kind", None) == "god"]
            vils = [s for s in good if s.kind == "villager"
                    or getattr(s.role, "also_kind", None) == "villager"]
            if not gods or not vils:
                out["checked_seats"].append(f"{where}带走 {target} 号 → 直接换成屠边")
            else:
                out["mech_mistakes"] += 1
                out["mech_detail"].append(
                    f"{where}：带走 {target} 号，但好人那边还剩神 {len(gods)} / 民 {len(vils)}"
                    " —— 这一口没换成屠边（可能只是削弱，需人工看）")

    if not has_state and role_key in ("seer", "wolf_king"):
        # 只对**真的需要局面快照**的两个身份说这句话（女巫/狼人看到它只会困惑）
        out["missing"].append(
            "本局日志里没有「决策当时谁还活着」的快照 ⇒ "
            + ("狼王那一口判不了『带走他之后有没有一边清零』" if role_key == "wolf_king"
               else "预言家『验死人』这一条判不了（验重复仍可判）")
            + "（探针模式才会记这份快照）")
    for role, why in (
            ("dreamer", "摄梦人「白摄」没统计：要判定「白摄」得把当晚狼刀目标和摄梦目标对齐，"
                        "而摄梦还有防毒/防奶穿的作用，只看刀口会误判 —— 留作人工复核"),
            ("witch", "女巫「该救没救 / 救错人」没统计：救得对不对要看刀口是不是狼自刀这类战术，"
                      "不是机制失误"),
            ("seer", "预言家「报了查杀但票型平」没统计：那是决策质量（Z 类），不是机制失误"),
            ("wolf", "普通狼没有单独的机制失误口径：狼队的账在决策命中率 / 狼队泄漏率里"),
            ("wolf_king", "狼王的账只算「这一口有没有直接换成屠边」：中局带走关键神位的价值"
                          "算不出来，会低估它"),
            ("lucky", "幸运儿三选一的得失没统计（毒/守/验哪个更值要靠 EV 枚举，本版没做）"),
    ):
        if role == role_key:
            out["missing"].append(why)
    return out


def aggregate_role(runs: list[dict], role_key: str) -> dict:
    """把一个身份下所有局合成一份统计（本阵营胜率 / 合法率 / 格式失败 / 命中 / 泄漏 / 机制失误）。"""
    tot = {"games": len(runs), "wins": 0, "decisions": 0, "illegal": 0, "format_fail": 0,
           "hits": 0, "hit_total": 0, "metric_hits": 0, "metric_hit_total": 0,
           "mate_exposed": 0, "wolf_speeches": 0, "badge_to_wolf": 0, "badge_decisions": 0,
           "mech_checked": 0, "mech_mistakes": 0, "days": 0}
    detail: list[str] = []
    missing: list[str] = []
    seats_seen: list[int] = []
    for r in runs:
        m, p = r["metrics"], r.get("probe") or {}
        tot["wins"] += 1 if m["camp_win"] else 0
        for k in ("decisions", "illegal", "format_fail", "mate_exposed", "wolf_speeches",
                  "badge_to_wolf", "badge_decisions"):
            tot[k] += m[k]
        tot["metric_hits"] += m["hits"]
        tot["metric_hit_total"] += m["hit_total"]
        tot["hits"] += p.get("hits", 0)
        tot["hit_total"] += p.get("hit_total", 0)
        tot["mech_checked"] += p.get("mech_checked", 0)
        tot["mech_mistakes"] += p.get("mech_mistakes", 0)
        tot["days"] += max(m["survived_days"] or [0])
        detail += list(p.get("mech_detail") or [])
        missing += [x for x in (p.get("missing") or []) if x not in missing]
        seats_seen += list(r.get("my_seats") or [])
    n = max(1, tot["games"])
    d = max(1, tot["decisions"])
    ht = max(1, tot["hit_total"])
    tot.update({"camp_win_rate": round(tot["wins"] / n, 4),
                "legal_rate": round(1 - tot["illegal"] / d, 4),
                "format_fail_rate": round(tot["format_fail"] / d, 4),
                "hit_rate": round(tot["hits"] / ht, 4) if tot["hit_total"] else None,
                "metric_hit_rate": (round(tot["metric_hits"] / max(1, tot["metric_hit_total"]), 4)
                                    if tot["metric_hit_total"] else None),
                "leak_rate": (round(tot["mate_exposed"] / max(1, tot["wolf_speeches"]), 4)
                              if tot["wolf_speeches"] else None),
                "mech_rate": (round(tot["mech_mistakes"] / max(1, tot["mech_checked"]), 4)
                              if tot["mech_checked"] else None),
                "days_avg": round(tot["days"] / n, 1),
                "role": role_key, "seats": sorted(set(seats_seen)),
                "mech_detail": detail, "missing": missing})
    return tot


def print_role_report(name: str, role_key: str, runs: list[dict], tot: dict) -> None:
    print("\n" + "=" * 78)
    print(f"身份探针：{role_name(role_key)}（{role_key}）　被测模型：{name}")
    print("=" * 78)
    print(f"{'局':<4}{'座位':<8}{'胜负':<8}{'我活到':<8}{'决策':<6}{'非法':<6}"
          f"{'格式挂':<8}{'命中(阵营)':<12}{'机制失误':<10}")
    for i, r in enumerate(runs, 1):
        m, p = r["metrics"], r.get("probe") or {}
        win = "胜 ✅" if m["camp_win"] else ("负" if m["camp_win"] is False else "—")
        hit = f"{p.get('hits', 0)}/{p.get('hit_total', 0)}" if p.get("hit_total") else "—"
        mech = (f"{p.get('mech_mistakes', 0)}/{p.get('mech_checked', 0)}"
                if p.get("mech_checked") else "—")
        print(f"{i:<4}{str(r['my_seats']):<8}{win:<8}{max(m['survived_days'] or [0]):<8}"
              f"{m['decisions']:<6}{m['illegal']:<6}{m['format_fail']:<8}{hit:<12}{mech:<10}")
    hit_txt = (f"{tot['hits']}/{tot['hit_total']}（{tot['hit_rate']:.0%}）"
               if tot["hit_total"] else "—（没有可判定的打人决策）")
    mech_txt = (f"{tot['mech_mistakes']}/{tot['mech_checked']}（{tot['mech_rate']:.0%}）"
                if tot["mech_checked"] else "—（这个身份没有可判定的机制动作）")
    leak_txt = (f"{tot['mate_exposed']}/{tot['wolf_speeches']}（{tot['leak_rate']:.0%}）"
                if tot["wolf_speeches"] else "—（没有公开发言）")
    print("-" * 78)
    print(f"合计：{tot['games']} 局（座位 {tot['seats']}）　"
          f"**本阵营胜率 {tot['wins']}/{tot['games']}（{tot['camp_win_rate']:.0%}）**　"
          f"平均活到 {tot['days_avg']} 天")
    print(f"　　　决策 {tot['decisions']} 次　合法率 {tot['legal_rate']:.0%}"
          f"（非法 {tot['illegal']}）　格式失败 {tot['format_fail']} 次"
          f"（{tot['format_fail_rate']:.0%}）")
    print(f"　　　决策命中率 {hit_txt}　狼队泄漏率 {leak_txt}　"
          f"警徽送狼 {tot['badge_to_wolf']}/{tot['badge_decisions']}")
    print(f"　　　**关键机制失误率 {mech_txt}**")
    if tot["metric_hit_rate"] is not None and tot["hit_total"]:
        print(f"　　　（对照：现有 metrics() 的命中率 = {tot['metric_hits']}/"
              f"{tot['metric_hit_total']}（{tot['metric_hit_rate']:.0%}）—— 它只把 "
              f"role_key == 「wolf」当狼，**狼王座位会被算成好人**，"
              f"所以上面那一行用的是按阵营重算的口径）")
    for line in tot["mech_detail"]:
        print(f"  · 机制失误：{line}")
    for line in tot["missing"]:
        print(f"  ⚠️ 缺口（没统计到的）：{line}")


def baseline_role(base: dict, model_name: str, role_key: str) -> tuple[dict | None, str]:
    """从基线报告里取"同身份"的对照：优先同模型，其次基线的多个模型取均值。"""
    models = (base or {}).get("models") or {}
    mine = ((models.get(model_name) or {}).get("roles") or {}).get(role_key)
    if mine:
        return mine, f"同名模型（{model_name}）"
    vals = [m["roles"][role_key] for m in models.values()
            if isinstance(m, dict) and role_key in (m.get("roles") or {})]
    if not vals:
        return None, ""
    n = len(vals)

    def avg(key):                                                  # noqa: ANN001, ANN202
        nums = [float(v[key]) for v in vals if isinstance(v.get(key), (int, float))]
        return round(sum(nums) / len(nums), 4) if nums else None

    blend = {"camp_win_rate": avg("camp_win_rate"), "mech_rate": avg("mech_rate"),
             "hit_rate": avg("hit_rate"), "legal_rate": avg("legal_rate"),
             "games": sum(int(v.get("games") or 0) for v in vals)}
    return blend, f"基线里 {n} 个模型的均值（基线中没有同名模型）"


def role_verdicts(model_name: str, summary: dict, base: dict | None,
                  baseline_desc: str = "") -> list[str]:
    """按 §3 的判据给出结论（没有基线就只报绝对值 + 明确写"不做结论"）。"""
    lines: list[str] = []
    for role_key, tot in summary.items():
        win, mech = tot["camp_win_rate"], tot["mech_rate"]
        head = f"{role_name(role_key)}（{role_key}）"
        if not base:
            lines.append(f"  {head}：本阵营胜率 {win:.0%}、关键机制失误率 "
                         + (f"{mech:.0%}" if mech is not None else "无样本")
                         + "　→ **无基线，不做结论**")
            continue
        ref, desc = baseline_role(base, model_name, role_key)
        if not ref or ref.get("camp_win_rate") is None:
            lines.append(f"  {head}：基线里没有这个身份的记录 → 不做结论")
            continue
        base_win = float(ref["camp_win_rate"])
        base_mech = ref.get("mech_rate")
        drop = base_win - win
        win_bad = drop >= WIN_DROP_LIMIT
        mech_bad = False
        if mech is not None:
            if base_mech is None:
                mech_bad = False
            elif float(base_mech) > 0:
                mech_bad = mech >= float(base_mech) * MECH_RATIO_LIMIT
            else:
                mech_bad = mech > 0                      # 基线 0 失误：出一手就是明显更差
        why = []
        if win_bad:
            why.append(f"本阵营胜率 {win:.0%} 比基线 {base_win:.0%} 低 {drop * 100:.0f} 个百分点"
                       f"（阈值 ≥{WIN_DROP_LIMIT * 100:.0f}）")
        if mech_bad:
            why.append(f"关键机制失误率 {mech:.0%} vs 基线 "
                       + (f"{float(base_mech):.0%}" if base_mech else "0%")
                       + f"（阈值 ≥{MECH_RATIO_LIMIT:.0f} 倍）")
        tail = f"　（基线：{desc}{('，' + baseline_desc) if baseline_desc else ''}）"
        if why:
            lines.append(f"  ⚠️ **这个模型扛不住 {head}**：" + "；".join(why) + tail)
        else:
            mech_txt = f"{mech:.0%}" if mech is not None else "无样本"
            base_mech_txt = f"{float(base_mech):.0%}" if base_mech is not None else "无样本"
            lines.append(f"  ✅ {head}：扛得住（本阵营胜率 {win:.0%} vs 基线 {base_win:.0%}；"
                         f"关键机制失误率 {mech_txt} vs 基线 {base_mech_txt}）" + tail)
    return lines


def load_baseline(path: str) -> tuple[dict | None, str]:
    """读 `--baseline`（本工具 `--roles` 落盘的那份 json）。读不到/结构不对 ⇒ 明确报错。"""
    if not path:
        return None, ""
    if not os.path.exists(path):
        return None, f"❌ 基线文件不存在：{path}"
    try:
        data = json.loads(io.open(path, encoding="utf-8").read())
    except Exception as exc:                                       # noqa: BLE001
        return None, f"❌ 基线文件读不了（{type(exc).__name__}: {exc}）：{path}"
    if not isinstance(data, dict) or data.get("kind") != "exam-live-role-probe":
        return None, (f"❌ 基线结构不对（要的是 `--roles` 跑出来的 json，"
                      f"kind=exam-live-role-probe）：{path}")
    desc = (f"基线 {os.path.basename(path)}：板子 {data.get('board')}，"
            f"参考对手 {data.get('reference')}，每个身份 "
            f"{data.get('role_games')} 局，含 {len(data.get('models') or {})} 个模型")
    return data, desc


async def play_one(model: Model, mode: str, board_name: str, seed: int, my_seat: int,
                   reference: str = "mock", max_days: int = 12,
                   rules_mode: str = "room", rules_detail: str = "brief",
                   force_role: str | None = None, record_state: bool = False) -> dict:
    """打一局。返回 {game, log, metrics}。

    ⚠️ `rules_mode` 必须跟生产一致（config.yaml 的 `game.rules_mode`，默认 room）：
    第一版没传 → 引擎默认 off → 实战档在"模型没见过规则书"的世界里打分，
    而笔试实测"给不给规则"能差 20~30 个百分点（71~82% → 100%）——
    那测的就不是同一个模型了。

    `force_role` / `record_state` = **身份探针专用**（不传时行为与以前完全一样）：
      · `force_role`：把被测座位**对调**成这个身份（见 `force_seat_role`）；
      · `record_state`：日志里多记一条"决策当时的局面快照"（第几天、谁还活着）——
        女巫毒人/狼王带人这些机制判断必须用**当时**的存活，打完再回头算会算错。
    """
    board = make_board(board_name)
    rng = random.Random(seed)
    log: list = []
    clients: list = []

    def _state() -> dict:                       # noqa: ANN202
        """决策当时的局面快照（`game` 在下面才构造，运行时才被调用，闭包拿得到）。"""
        return {"day": game.day, "alive": sorted(game.alive_ids())}

    state_fn = _state if record_state else None
    #: ⚠️ 考试也要吃**生产那套保护与提示层**（2026-10-07 复查补上）：
    #:   上游挂住时生产有单次上限（实测踩过一次 724 秒），考试如果没上限就会一直等；
    #:   提示层（通用提示词/打法书/概念库进包段）不一致的话，考试成绩跟真实对局不可比。
    prod = _prod_game_cfg()

    if mode == "all":
        my_seats = set(range(1, len(board.roles) + 1))
    else:
        # ⚠️ 座位号必须按**本局板子人数**夹紧：以前写死 `1 + (i % 11)`，
        #    用 9 人板跑第 9 局就会得到 18 号 —— 那个座位根本不存在，
        #    factory 永远匹配不上，于是"这一局 0 次决策"却照样计入合计和胜率。
        my_seat = ((int(my_seat) - 1) % len(board.roles)) + 1
        my_seats = {my_seat}

    def factory(seat):                          # noqa: ANN001, ANN202
        if seat.id in my_seats:
            if is_mock(model):                  # 自检模式：用 mock 冒充被测模型
                return Recorder(MockAgent(seat, rng), seat.id, log, mine=True, state_fn=state_fn)
            client = routing.build_client(
                spec_for(model), label=f"{seat.id}号/{model.name}",
                thinking_style=routing.thinking_style_for(prod, getattr(model, "profile", "")))
            clients.append(client)
            return Recorder(LLMAgent(client, seat.id,
                              fallbacks=_build_fallbacks(prod, seat,
                                                         prod.get("fallback"))),
                     seat.id, log, mine=True, state_fn=state_fn)
        if reference == "mock":
            return Recorder(MockAgent(seat, rng), seat.id, log, mine=False, state_fn=state_fn)
        # ⚠️ 参考对手**每个座位一个客户端**：共用一个的话，多个座位并发决策时
        # `client.last_call` 会串味（transcript/用量记到别人头上）。
        client = routing.build_client(spec_for(reference), label=f"参考/{reference.name}")
        clients.append(client)
        return Recorder(LLMAgent(client, seat.id,
                              fallbacks=_build_fallbacks(prod, seat,
                                                         prod.get("fallback"))),
                     seat.id, log, mine=False, state_fn=state_fn)

    # 上游挂住时的两层保险，与真局（status / 建局）走**同一个**取值函数 —— 考试分数才跟真局可比
    _call_timeout, _decision_timeout = timeouts_from_cfg(prod)
    game = Game(board=board, agent_factory=factory, seed=seed, max_days=max_days,
                on_event=lambda *a, **k: None,
                rules_mode=rules_mode, rules_detail=rules_detail,
                # ⚠️ 考试也要吃生产的那套保护：不然上游一挂，考试局会像生产局一样无声卡住
                #    （2026-10-07 实测踩过：一次调用 724 秒）。这里从 config.yaml 读同一组值，
                #    兜底也走 `timeouts_from_cfg` —— 与真局是**同一对数**
                #    （2026-10-08 收敛前这里写的是 150/300，跟服务端 status 的 300 不一致）。
                call_timeout=_call_timeout,
                decision_timeout=_decision_timeout,
                # 提示层也跟生产一致，否则考试成绩跟真实对局不可比
                player_prompt=tabletalk.player_prompt({"game": prod}, HERE),
                playbook_text=(tabletalk.load_playbook(HERE, str(prod.get("playbook_path") or ""))
                               if prod.get("playbook", True) else ""),
                meta_text=(tabletalk.load_meta_book(HERE, str(prod.get("meta_book_path") or ""))
                           if prod.get("meta_book", True) else ""),
                edge_order=str(prod.get("edge_order", "sides_first")),
                auto_retries=int(prod.get("auto_retries", 2)),
                # ⚠️ 2026-10-07 审查发现：这里漏了 decision_first，导致考核跑的是
                #    "先发言后投票"的老流程，而生产是"发言前先定票"——两边不可比。
                decision_first=bool(prod.get("decision_first", False)),
                pk_on_tie=bool(prod.get("pk_on_tie", True)),
                wolf_control=bool(prod.get("wolf_control", True)),
                sheriff=prod.get("sheriff"),
                # 警长自选发言方向：考试局跟生产同口径（否则两边的调用次数与信息量不可比）
                sheriff_picks_order=bool(prod.get("sheriff_picks_order", True)),
                wolf_explode=bool(prod.get("wolf_explode", True)),
                retry_backoff=list(prod.get("retry_backoff") or [3, 8]),
                retry_jitter=float(prod.get("retry_jitter", 0.3)),
                carry_reasoning=bool(prod.get("carry_reasoning", True)),
                carry_reasoning_chars=int(prod.get("carry_reasoning_chars", 300)))
    if force_role:
        # ⚠️ 必须在 `game.run()` 之前、且必须是对调（见 force_seat_role 的说明）。
        #    调不动就直接抛 —— 悄悄按随机身份打下去，会让"seer 那一栏"里混进别人的成绩。
        if not force_seat_role(game, sorted(my_seats)[0] if my_seats else 1, force_role):
            raise RuntimeError(f"板子 {board_name} 上没有身份 {force_role}，"
                               f"没法把被测座位固定成它（棋盘身份：{board.roles}）")
    started = time.time()
    await game.run()
    elapsed = time.time() - started
    for c in clients:
        try:
            await c.aclose()
        except Exception:                       # noqa: BLE001
            pass
    m = metrics(game, log, my_seats)
    m["seconds"] = round(elapsed, 1)
    m["usage"] = dict(model.usage)
    out = {"game": game, "log": log, "metrics": m, "my_seats": sorted(my_seats)}
    if force_role:
        # 身份探针的额外口径（按阵营的命中率 + 关键机制失误）；不改 metrics() 本身
        out["probe"] = probe_stats(game, log, my_seats, force_role)
        out["role"] = force_role
    return out


async def run_role_probe(model: Model, roles: list[str], role_games: int, board_name: str,
                         seed: int, reference, max_days: int, rules_mode: str,
                         rules_detail: str) -> dict:
    """身份探针主循环：同一个模型逐局**被固定在**某个身份上，按身份分别统计。

    座位：跨身份、跨局**连续轮换**（`game_idx`），所以既换了座位号、又不会每个身份
    都从 1 号座开始（位置偏好被摊平）。种子随局号前进，可用 `--seed` 复现。
    返回的 dict 就是落盘的那份基线结构（`kind=exam-live-role-probe`）。
    """
    board_seats = max(1, len(board_roles(board_name)))
    summary: dict[str, dict] = {}
    game_idx = 0
    for role_key in roles:
        runs: list[dict] = []
        for i in range(max(1, role_games)):
            seat = (game_idx % board_seats) + 1
            game_idx += 1
            r = await play_one(model, "single", board_name, seed=seed + game_idx, my_seat=seat,
                               reference=reference, max_days=max_days,
                               rules_mode=rules_mode, rules_detail=rules_detail,
                               force_role=role_key, record_state=True)
            m = r["metrics"]
            p = r.get("probe") or {}
            print(f"    第 {i + 1}/{max(1, role_games)} 局　{seat} 号座（"
                  f"{role_name(role_key)}）：{'胜' if m['camp_win'] else '负'}　{m['days']} 天　"
                  f"决策 {m['decisions']}　非法 {m['illegal']}　"
                  f"命中 {p.get('hits', 0)}/{p.get('hit_total', 0)}　"
                  f"机制失误 {p.get('mech_mistakes', 0)}/{p.get('mech_checked', 0)}　"
                  f"{m['seconds']}s")
            runs.append(r)
        tot = aggregate_role(runs, role_key)
        summary[role_key] = tot
        print_role_report(model.safe_desc, role_key, runs, tot)
        if tot["games"] < 3:
            print(f"  ⚠️ 这个身份只打了 {tot['games']} 局 —— 样本很小"
                  f"（胜率差 15 个百分点 ≈ 1 局之差），结论只能当线索，别当定论")
    return {"kind": "exam-live-role-probe", "version": 1,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "board": board_name, "mode": "single",
            "reference": (reference if isinstance(reference, str) else reference.name),
            "role_games": max(1, role_games),
            "models": {model.name: {"roles": summary}}}


def save_role_report(path: str, payload: dict) -> str:
    """落盘身份探针报告；**同名文件已存在就按模型名合并**（方便一个个模型攒基线）。"""
    merged = dict(payload)
    if os.path.exists(path):
        try:
            old = json.loads(io.open(path, encoding="utf-8").read())
            if isinstance(old, dict) and old.get("kind") == "exam-live-role-probe":
                models = dict(old.get("models") or {})
                models.update(payload.get("models") or {})
                merged["models"] = models
                for k in ("board", "reference", "role_games", "mode"):
                    merged.setdefault(k, old.get(k))
        except Exception:                                  # noqa: BLE001
            pass                                            # 读不动就当新文件写（不吞掉本次成绩）
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=1)
    return path


def print_metrics(name: str, runs: list[dict]) -> None:
    print("\n" + "=" * 78)
    print(f"实战成绩：{name}")
    print("=" * 78)
    print(f"{'局':<4}{'我的座位':<12}{'身份':<8}{'胜负':<8}{'我活到':<8}"
          f"{'决策':<6}{'非法':<6}{'格式挂':<8}{'命中':<10}{'警徽送狼':<10}")
    tot = {"decisions": 0, "illegal": 0, "format_fail": 0, "hits": 0, "hit_total": 0,
           "wins": 0, "n": 0, "days": 0, "badge_to_wolf": 0, "badge_decisions": 0}
    for i, r in enumerate(runs, 1):
        m = r["metrics"]
        g = r["game"]
        roles = "、".join(f"{sid}号{role_name(g.seat_by_id(sid).role_key)}"
                          for sid in r["my_seats"][:3])
        if m["camp_win"] is True:
            win = "胜 ✅"
        elif m["camp_win"] is False:
            win = "负"
        else:
            # 全座位模式：两边都是自己人，看谁赢就行
            win = {"wolf": "狼胜", "good": "好人胜", "draw": "流局",
                   "aborted": "中止"}.get(game_winner := m["winner"], str(game_winner))
        hit = f"{m['hits']}/{m['hit_total']}" if m["hit_total"] else "—"
        badge = (f"{m['badge_to_wolf']}/{m['badge_decisions']}"
                 if m["badge_decisions"] else "—")
        print(f"{i:<4}{str(r['my_seats']):<12}{roles:<8}{win:<8}"
              f"{max(m['survived_days'] or [0]):<8}{m['decisions']:<6}{m['illegal']:<6}"
              f"{m['format_fail']:<8}{hit:<10}{badge:<10}")
        for k in ("decisions", "illegal", "format_fail", "hits", "hit_total",
                  "badge_to_wolf", "badge_decisions"):
            tot[k] += m[k]
        tot["n"] += 1
        tot["wins"] += 1 if m["camp_win"] else 0
        tot["days"] += max(m["survived_days"] or [0])
    print("-" * 78)
    if tot["n"]:
        legal_rate = 1 - tot["illegal"] / max(1, tot["decisions"])
        print(f"合计：{tot['n']} 局　胜 {tot['wins']}/{tot['n']}　"
              f"决策 {tot['decisions']} 次　合法率 {legal_rate:.0%}　"
              f"格式失败 {tot['format_fail']} 次　"
              f"命中 {tot['hits']}/{tot['hit_total']}"
              + (f"　警徽送狼 {tot['badge_to_wolf']}/{tot['badge_decisions']}"
                 if tot["badge_decisions"] else ""))


async def selftest() -> int:
    """离线跑一遍：用 mock 当"被测模型"，确认指标能算出来（不联网、不花钱）。

    顺便**免费验证身份探针**（`--roles` 那条路）：mock 假扮被测模型固定在两个身份上各打 1 局，
    断言"身份真的被固定住了、指标真的算出来了"；再故意点一个本板子没有的身份，
    确认它给的是清楚提示、而不是静默跳过或崩掉。
    """
    print("=" * 78)
    print("实战档自检：用 mock 假扮被测模型，跑 2 局，确认指标能算出来")
    print("=" * 78)
    fake = Model(name="mock-被测", base_url="mock://offline", api_key="x", model="mock")
    for mode in ("single", "all"):
        runs = []
        for i, seat in enumerate((3, 7), 1):
            # 自检要快：板子小一点、天数封顶 5 天（真实考核时用 11 人板 + 默认天数）
            r = await play_one(fake, mode, "eleven_dream", seed=100 + i,
                               my_seat=seat, reference="mock", max_days=5)
            runs.append(r)
            m = r["metrics"]
            assert m["decisions"] > 0, f"{mode} 模式没记录到任何决策"
            assert m["decisions"] == len([x for x in r["log"] if x["mine"]]), "决策数对不上"
        print_metrics(f"mock（模式 {mode}）", runs)

    print("\n" + "=" * 78)
    print("身份探针自检：把 mock 固定在 2 个身份上各打 1 局（免费），验证固定逻辑与指标")
    print("=" * 78)
    probe_roles = ["seer", "witch"]
    probe = await run_role_probe(fake, probe_roles, 1, "eleven_dream", seed=200,
                                 reference="mock", max_days=5,
                                 rules_mode="off", rules_detail="brief")
    for role_key in probe_roles:
        tot = probe["models"]["mock-被测"]["roles"][role_key]
        assert tot["games"] == 1 and tot["decisions"] > 0, f"{role_key} 探针没记录到决策"
        assert (tot["mech_rate"] is not None) or tot["mech_checked"] == 0, "机制口径算错了"
        print(f"      ✔ {role_key}：本阵营胜率 {tot['camp_win_rate']:.0%}、"
              f"决策 {tot['decisions']} 次、机制失误 {tot['mech_mistakes']}"
              f"/{tot['mech_checked']}")
    # 固定身份必须真的生效：再打一局，直接检查它坐的座位是不是那个身份
    check = await play_one(fake, "single", "eleven_dream", seed=205, my_seat=4,
                           reference="mock", max_days=5, force_role="witch", record_state=True)
    got_role = check["game"].seat_by_id(check["my_seats"][0]).role_key
    assert got_role == "witch", f"身份没固定住：4 号座是 {got_role}"
    assert check["probe"]["mech_checked"] >= 0 and "alive" in check["log"][0], "没记局面快照"
    print(f"      ✔ 固定身份生效：4 号座 = {role_name(got_role)}；"
          f"{check['my_seats'][0]} 号座那时还活着 {len(check['log'][0]['alive'])} 人")
    # 板子上没有的身份：必须是一句清楚提示（不是静默跳过，也不是崩在引擎里）
    missing = [r for r in ("wolf_king",) if r not in board_roles("eleven_dream")]
    if missing:
        try:
            await play_one(fake, "single", "eleven_dream", seed=206, my_seat=5,
                           reference="mock", max_days=5, force_role=missing[0])
            print(f"      ❌ 板子上没有 {missing[0]}，却打完了（应该报错）")
            return 1
        except RuntimeError as exc:
            print(f"      ✔ 板子上没有的身份会被拦下来：{str(exc)[:70]}…")
    print("\n✅ 实战档自检通过：单座位 / 全座位两种模式都能跑完并算出指标；"
          "身份探针能固定在指定身份上并算出各项指标。")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="实战档：真打对局，按客观指标评分")
    parser.add_argument("--mode", choices=("single", "all"), default="single")
    parser.add_argument("--games", type=int, default=2, help="single 模式下跑几局（每局换一个座位）")
    parser.add_argument("--board", default="eleven_wolfking")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--models", default=os.path.join(HERE, "tools", "exam_models.yaml"))
    parser.add_argument("--only", default="", help="只测名字里含这段文字的模型")
    parser.add_argument("--reference", default="mock",
                        help="single 模式下其余座位用什么对手（mock 或清单里某个模型名）")
    parser.add_argument("--selftest", action="store_true", help="离线自检（不联网）")
    # ⚠️ 这个参数 2026-10-06 被漏注册过：`play_one(..., max_days=args.max_days)` 还在，
    #    于是**每次非 --selftest 调用都会 AttributeError 崩掉**（先打完一发付费预检才崩）。
    #    加参数的同时在 t_exam.py 里加了一条"parser 必须有 --max-days"的回归断言。
    parser.add_argument("--max-days", type=int, default=12, help="一局最多几天")
    parser.add_argument("--rules-mode", default=None,
                        help="规则书投放方式；默认读 config.yaml 的 game.rules_mode（跟生产一致）")
    parser.add_argument("--rules-detail", default=None,
                        help="brief / full；默认读 config.yaml 的 game.rules_detail")
    # ---- 高难度身份探针（不传 --roles 时这几项完全不生效，默认行为一字不变）----
    parser.add_argument("--roles", default="",
                        help="身份探针：逗号分隔的身份（如 seer,witch,wolf_king,wolf,dreamer）；"
                             "同一个模型逐局被固定在某个身份上，每个身份单独出统计与结论。"
                             "留空 = 老行为（不做探针）")
    parser.add_argument("--role-games", type=int, default=2, help="身份探针里每个身份打几局（默认 2）")
    parser.add_argument("--baseline", default="",
                        help="身份探针的对照基线：一份此前用 --roles 跑出来的 json；"
                             "不传就只打印绝对值并标注「无基线，不做结论」")
    parser.add_argument("--role-out", default="",
                        help="身份探针报告写到哪个 json（默认 reports/exam-live-roles-<模型>-<时间>.json）；"
                             "同一个路径会把不同模型的成绩**合并**进去，方便一个个模型攒基线")
    args = parser.parse_args()

    load_env(os.path.join(HERE, ".env"))
    # 规则书投放方式默认**跟生产走**：不读 config 的话会在"没规则书"的世界里给模型打分
    cfg_path = os.path.join(HERE, "config.yaml")
    game_cfg = {}
    if os.path.exists(cfg_path):
        import yaml
        game_cfg = (yaml.safe_load(io.open(cfg_path, encoding="utf-8")) or {}).get("game") or {}
    rules_mode = args.rules_mode or str(game_cfg.get("rules_mode", "room"))
    rules_detail = args.rules_detail or str(game_cfg.get("rules_detail", "brief"))
    print(f"规则书投放：rules_mode={rules_mode} / rules_detail={rules_detail}"
          f"（{'命令行指定' if args.rules_mode else '跟 config.yaml 一致'}）")
    if args.selftest:
        return await selftest()

    models = load_models(args.models)
    if args.only:
        models = [m for m in models if args.only in m.name]
    if not models:
        print(f"❌ 没有可测的模型：在 {args.models} 里填上 API")
        return 1
    reference: str | Model = "mock"
    if args.reference != "mock":
        reference = next((m for m in load_models(args.models) if m.name == args.reference), None)
        if reference is None:
            print(f"❌ 清单里没有叫 {args.reference} 的参考模型")
            return 1

    # ---- 高难度身份探针（`--roles`）----
    # ⚠️ 这一段只在传了 --roles 时才走；不传时下面的流程与以前**一字不变**。
    roles = [x.strip() for x in str(args.roles or "").split(",") if x.strip()]
    if roles:
        if args.mode != "single":
            print("❌ --roles 身份探针只支持 --mode single（全座位模式下"
                  "「把被测模型固定在某个身份上」没有意义）")
            return 1
        have = board_roles(args.board)
        unknown = [r for r in roles if r not in have]
        roles = [r for r in roles if r in have]
        if unknown:
            print(f"⚠️ 板子 {args.board} 上没有这些身份，已跳过：{'、'.join(unknown)}")
            print(f"   这张板子的身份：{'、'.join(have)}"
                  f"（想要狼王/摄梦人请用 --board eleven_wolfking）")
        if not roles:
            print("❌ 没有可测的身份（--roles 里没有一个在这张板子上）")
            return 1
        base, base_desc = load_baseline(args.baseline)
        if args.baseline and base is None:
            print(base_desc)
            print("   （不给基线也能跑，只是不会下「扛不住」的结论）")
            return 1
        print(f"\n身份探针：每个身份 {max(1, args.role_games)} 局，身份 = "
              f"{'、'.join(role_name(r) + '（' + r + '）' for r in roles)}")
        if base_desc:
            print(base_desc)
            if base.get("board") and base["board"] != args.board:
                print(f"⚠️ 基线板子（{base['board']}）与本次（{args.board}）不同 —— "
                      "结论只能当参考")
        for model in models:
            print(f"\n▸ 身份探针开始：{model.safe_desc}（板子 {args.board}，"
                  f"参考对手 {args.reference}）")
            ok, why = await preflight(model)
            print(f"    连通性预检：{'✅ ' + why if ok else '❌ ' + why}")
            if not ok:
                print("    跳过这个模型（先把 key / base_url 弄对，别白花这几局的钱）")
                continue
            payload = await run_role_probe(model, roles, max(1, args.role_games), args.board,
                                           args.seed, reference, args.max_days,
                                           rules_mode, rules_detail)
            summary = payload["models"][model.name]["roles"]
            print("\n" + "=" * 78)
            print(f"结论判据（本阵营胜率低 ≥{WIN_DROP_LIMIT:.0%}，"
                  f"或关键机制失误率 ≥{MECH_RATIO_LIMIT:.0f} 倍 ⇒ 扛不住这个身份）")
            print("=" * 78)
            for ln in role_verdicts(model.name, summary, base, base_desc):
                print(ln)
            if not base:
                print("  （没传 --baseline ⇒ 只报绝对值、不下结论。"
                      "想比基线：先跑一个强模型存下这份 json，再用 --baseline 指过来）")
            os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
            out = args.role_out or os.path.join(
                HERE, "reports", f"exam-live-roles-{model.name}-{time.strftime('%H%M%S')}.json")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            save_role_report(out, payload)
            print(f"\n身份探针报告已存：{out}")
            print("（它就是 `--baseline` 要的那份文件；同一个 --role-out 路径会按模型名合并）")
        return 0

    for model in models:
        print(f"\n▸ 开始测 {model.safe_desc}（模式 {args.mode}，板子 {args.board}）")
        ok, why = await preflight(model)
        print(f"    连通性预检：{'✅ ' + why if ok else '❌ ' + why}")
        if not ok:
            print("    跳过这个模型（先把 key / base_url 弄对，别白跑几局）")
            continue
        runs = []
        board_seats = len(make_board(args.board).roles)
        for i in range(max(1, args.games)):
            seat = ((i) % board_seats) + 1          # 座位号按板子人数循环，别越界
            r = await play_one(model, args.mode, args.board, seed=args.seed + i,
                               my_seat=seat, reference=reference, max_days=args.max_days,
                               rules_mode=rules_mode, rules_detail=rules_detail)
            m = r["metrics"]
            print(f"    第 {i + 1} 局：{'胜' if m['camp_win'] else '负'}　"
                  f"{m['days']} 天　决策 {m['decisions']}　非法 {m['illegal']}　"
                  f"命中 {m['hits']}/{m['hit_total']}　{m['seconds']}s")
            runs.append(r)
        print_metrics(model.safe_desc, runs)
        # ⚠️ 默认写到 reports/（以前落在项目根，几十个 json 堆在一起）
        out_dir = os.path.join(HERE, "reports")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"exam-live-{model.name}-{time.strftime('%H%M%S')}.json")
        with io.open(out, "w", encoding="utf-8") as fh:
            json.dump([{"metrics": r["metrics"], "my_seats": r["my_seats"],
                        "ledger": [e.text for e in r["game"].ledger.entries]} for r in runs],
                      fh, ensure_ascii=False, indent=1)
        print(f"明细已存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
