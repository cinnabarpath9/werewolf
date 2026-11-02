# coding=utf-8
"""三类座位代理：Mock（离线自测）/ LLM（真实模型）/ Human（你自己）。

隔离纪律：代理**只能**拿到 packet 和 task，不允许访问 Game 对象。
MockAgent 只额外持有自己的那个 Seat（自己的身份、自己的私有经历），
这跟真人玩家手里有自己的角色牌是一个意思。
"""

from __future__ import annotations

import asyncio
import json
import re

from .context import Task
from .engine import truthy
from .llm import LLMClient, LLMError
from .roles import ROLES, seats_text

#: 后台任务（目前只有"关掉旧客户端"）。事件循环对 task 只持弱引用，
#: 不把引用留在模块级集合里，可能任务没跑完就被 GC —— 关连接就半途而废。
_BG_TASKS: set = set()


def _spawn(coro) -> None:
    """跑一个不需要 await 的后台协程，并保住它的引用（见 `_BG_TASKS`）。"""
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


class BaseAgent:
    async def ask(self, packet: str, task: Task) -> dict:  # pragma: no cover
        raise NotImplementedError

    def last_meta(self) -> dict:
        """最近一次调用的原始材料（推理链 / 原始回复 / 用量），给复盘用。"""
        return {}

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# 离线自测：不调用任何模型，用自己看得见的信息做随机但合理的决策
# --------------------------------------------------------------------------- #

class MockAgent(BaseAgent):
    def __init__(self, seat, rng) -> None:
        self.seat = seat
        self.rng = rng

    def _pick(self, legal: list[int]) -> int | None:
        return self.rng.choice(legal) if legal else None

    def _pick_kill(self, legal: list[int], task: Task) -> int | None:
        """狼刀的随机目标：**优先只在好人里挑**（`task.extra["good"]` 由引擎塞进来）。

        ⚠️ 2026-10-08：出刀合法池改成"全体存活（含狼）"之后（用户要求支持自刀/刀队友），
        纯随机会让**随机狼队 36% 的刀砍在自己人身上** —— 实测 `t_mock30` 的狼胜率
        从 93% 掉到 50%。自刀是**刻意战术**，不该是随机默认值；跟引擎的兜底口径保持一致
        （见 engine.night 里"写糊了才兜底，且兜底只在好人里随机"）。
        真人/模型的显式选择不受影响：它们照样能点自己人。
        """
        good = [i for i in (task.extra or {}).get("good") or [] if i in legal]
        return self._pick(good or legal)

    async def ask(self, packet: str, task: Task) -> dict:
        legal = list(task.legal)
        pick = self._pick(legal)
        kind = task.kind

        if kind == "wolf_intent":
            # 「决策在前」：夜里先私下定刀口，再去讨论（见 engine.decide_wolf_round）
            return {"reads": [{"seat": i, "role": "未知", "why": "随便看看"}
                              for i in legal],
                    "summary": "今晚先刀一个看起来最像神的。",
                    "target": self._pick_kill(legal, task), "reason": "先定个刀口，讨论再圆回来"}
        if kind == "wolf_kill":
            return {"target": self._pick_kill(legal, task),
                    "reason": self.rng.choice(["他发言太跳", "像神", "先刀这个试试"])}
        if kind == "seer_check":
            return {"target": pick, "reason": "挑一个没验过的"}
        if kind == "sheriff_order":
            # 警长选发言方向：mock 一律选**顺序**（保持 mock 局轨迹稳定；逆序由剧本回归覆盖）
            return {"reverse": False, "reason": "先按顺序，稳一点"}
        if kind == "wolf_explode":
            # 自爆：mock 一律**不爆**（保持 mock 局轨迹与调用次数稳定；自爆由剧本回归覆盖）
            return {"explode": False, "who": None, "reason": "还不至于，先看看"}
        if kind == "dream":
            # 摄梦人（本项目默认板子）：mock 走"盲梦"——在合法目标里随机挑一个。
            # 真实模型按攻略的"保护/举刀/刺杀/连狼"四式选，mock 只需把规则链路跑通。
            return {"reads": [{"seat": i, "camp": "good", "why": "随便看看"} for i in legal],
                    "summary": "第一夜没信息，先盲摄一个像狼的。",
                    "target": self._pick(legal), "reason": "盲摄"}
        if kind == "guard":
            # 守卫：mock 走"盲守"——在合法目标里随机挑一个。
            # ⚠️ 2026-10-25：以前这里没有分支，靠引擎 `coerce_seat` 的随机兜底"顺手"守一个人；
            #    引擎改成"没给目标就按空过处理"之后，mock 会变成**每晚都弃守**，
            #    于是"守住 → 不死 / 同守同救"这条结算链在 mock 局里再也跑不到。
            #    mock 必须显式给目标；弃守那条路由剧本回归（t_rules_new 20）覆盖。
            return {"reads": [{"seat": i, "camp": "good", "why": "随便看看"} for i in legal],
                    "summary": "先盲守一个像狼要刀的。", "abstain": False,
                    "target": self._pick(legal), "reason": "盲守"}
        # ⚠️ 养鸦人的诅咒**不再是一个夜间任务**（用户 2026-10-09 改定时机）：
        #    它现在发生在白天的放逐投票那一刻，和投票合并进同一次调用 ——
        #    见 `analyze` / `vote` 分支里的 `task.extra["curse"]`。
        if kind == "lucky":
            # 幸运儿（本项目民牌）：mock 用**查验**（三选一里最容易验证的那条），用一次就没了
            return {"reads": [{"seat": i, "camp": "good", "why": "随便看看"} for i in legal],
                    "summary": "用幸运验一个看看。", "use": True, "skill": "check",
                    "target": self._pick(legal), "reason": "mock 用查验"}
        if kind == "witch":
            wants_save = self.rng.random() < 0.6
            return {"save": wants_save, "poison": None, "reason": "毒药先留着"}
        if kind == "hunter_shoot":
            return {"shoot": None, "reason": "不确认，先不开枪"}
        if kind == "wolf_king_take":
            # 狼王出局时"带走谁"（新板子 `eleven_wolfking`）。mock 走**随机带走一个好人**：
            # 这条链路（带人 → 判屠边）必须真的被跑到，否则 mock/回归局里狼王等于白板。
            # 名单里只有好人（引擎已排除狼队友），所以随机也不会误伤队友。
            return {"take": self._pick(legal), "reason": "mock：带走一个看看"}
        if kind == "speech_plan":
            # 「决策 + 发言」合并调用（见 engine.decide_for_speech）：**一次回复里既有决策、
            # 也有说出口的正文**。`speech` 必须给：不给的话引擎会为这一轮再补问一次发言，
            # mock 局的调用次数就退化成「两次」，t_mock30 / t_ws_e2e 量到的也不是真实链路。
            body = self._speech(pick)
            return {"reads": [{"seat": i, "camp": "good", "role": "未知",
                              "why": "先按现在这些信息看"} for i in (legal or [])],
                    "summary": "我先按目前为止的发言判断。",
                    "tiers": [{"seat": i, "tier": "中", "good_reason": "没证据",
                               "wolf_reason": "也说不清"} for i in (legal or [])],
                    "protect": [], "attack": [pick] if pick else [],
                    "claim": body.get("claim") or "无",
                    "stance": "我先跟着最像狼的那个走。",
                    "role_plan": "按身份该干嘛干嘛。", "vote": pick,
                    "speech": body.get("speech") or "（我过，先听后面的。）",
                    "suspect": body.get("suspect") or [], "reason": body.get("reason") or ""}
        if kind == "analyze":
            # 分析/决策层：读牌 + 定票（见 engine.analyze_round）
            reads = [{"seat": i, "camp": ("wolf" if self.rng.random() < 0.3 else "good"),
                      "role": "未知", "why": "随便看看"} for i in legal]
            out = {"reads": reads, "summary": "我先按现在这点信息判断。",
                   "tiers": [{"seat": i, "tier": "中", "good_reason": "没证据",
                              "wolf_reason": "也说不清"} for i in legal],
                   "vote": pick, "reason": "先定个票，发言再圆回来"}
            # 养鸦人（本项目民牌）：诅咒挪到**放逐投票**这一刻，和投票同一次调用完成
            # （用户 2026-10-09 改定的时机）。mock 就咒自己准备投的那个人：
            # 语义合理，也把"被咒者多一票"这条链路真的跑起来。
            if (task.extra or {}).get("curse"):
                cl = list((task.extra or {}).get("curse_legal") or legal)
                out["curse_target"] = (pick if pick in cl
                                       else (self.rng.choice(cl) if cl else None))
            return out
        if kind == "vote_intent":
            # 「决策在前」模式：先私下定票，再发言（见 engine.analyze_round）
            return {"target": pick, "reason": "先定个票，发言再圆回来",
                    "confidence": "medium"}
        if kind == "vote":
            out = {"vote": pick,
                   "reason": self.rng.choice(["跟票", "发言有漏洞", "他不像好人"])}
            # 养鸦人：这一票之前先决定诅咒（老流程 / 其它路径）
            if (task.extra or {}).get("curse"):
                cl = list((task.extra or {}).get("curse_legal") or legal)
                out["curse_target"] = (pick if pick in cl
                                       else (self.rng.choice(cl) if cl else None))
            return out
        if kind == "speech":
            return self._speech(pick)
        # --- 遗言 / 警长（11 人及以上才有）---
        if kind == "sheriff_run":
            # 上警三选一（用户 2026-10-09：前置位 / 后置位 / 不上警）。
            # 预言家永远前置位（跟打法书口径一致，免得 mock 局跑出反常识流程）；其余随机分三段。
            if getattr(self.seat, "role_key", "") == "seer":
                return {"run": "front", "reason": "预言家必须上警争警徽，而且要前置位"}
            roll = self.rng.random()
            if roll < 0.34:
                return {"run": "front", "reason": "随机：前置位上警（先占位）"}
            if roll < 0.62:
                return {"run": "back", "reason": "随机：后置位上警（听完再总结拉票）"}
            return {"run": False, "reason": "随机决定不上警"}
        if kind == "sheriff_speech":
            return self._speech(pick)
        if kind == "sheriff_vote":
            # 都投座位号最小的那个候选人：随机投的话 11 个选民摊在好几个候选人上，
            # 平票 → 警徽流失 会变成常态，整条"警长在位"的链路（1.5 票 / 传徽）就永远跑不到
            return {"vote": min(legal) if legal else None, "reason": "跟着第一个上警的人走"}
        if kind == "last_words":
            role = ROLES[self.seat.role_key]
            if role.camp == "wolf":
                return {"speech": "我是好人，你们投错人了，剩下的狼就在刚才带票的人里面。",
                        "claim": "无"}
            return {"speech": f"我是{role.name}，我出局了，剩下的靠你们了，注意看谁在带票。",
                    "claim": role.name}
        if kind == "badge":
            to = pick if self.rng.random() < 0.7 else None
            return {"to": to, "reason": "把警徽给一个看着像好人的" if to else "撕掉警徽"}
        return {}

    def _speech(self, suspect: int | None) -> dict:
        role = ROLES[self.seat.role_key]
        checks = [e for e in self.seat.private_events if "查验" in e]
        if role.key == "seer" and checks:
            last = checks[-1]
            m = re.search(r"查验了 (\d+)号，结果是【(.+?)】", last)
            if m:
                # 同一条纪律：suspect 可能是 None，别把 None 拼进发言
                tail = (f"好人跟我走，今天就出 {suspect}号 或者我验出来的狼。"
                        if suspect is not None else
                        f"好人跟我走，今天就出 {m.group(1)}号。")
                text = f"我是预言家。{m.group(1)}号 我验过，是{m.group(2)}。" + tail
                return {"speech": text, "claim": "预言家",
                        "suspect": [int(m.group(1))], "reason": "把查验结果说出来"}
        # ⚠️ legal 为空时 suspect 是 None，直接 f-string 会说出"None号 刚才那段发言有点急"
        # 这种明显是机器故障的发言（实测）。没有合法目标就换成"先不点名"这类话。
        if suspect is None:
            if role.camp == "wolf":
                return {"speech": "我是好人。这轮我先不点名，听听后面几个人的发言再说。",
                        "claim": "无", "suspect": [], "reason": "装好人，先不推人"}
            return {"speech": "我就是个村民，没什么信息。这轮先不点名，听听后面怎么说。",
                    "claim": "无", "suspect": [], "reason": "没用信息，先过"}
        if role.camp == "wolf":
            return {"speech": f"我是好人。{suspect}号 刚才那段发言有点急，我先怀疑他。",
                    "claim": "无", "suspect": [suspect], "reason": "装好人，推一个下去"}
        return {"speech": f"我就是个村民，没什么信息。{suspect}号 我先保留意见，听听后面怎么说。",
                "claim": "无", "suspect": [suspect], "reason": "没用信息，先过"}


# --------------------------------------------------------------------------- #
# 真实模型
# --------------------------------------------------------------------------- #

class LLMAgent(BaseAgent):
    """真实模型座位。`uses_model=True` 让引擎知道：这个座位的失败属于"上游波动"，
    可以触发"暂停整局、弹窗问用户"那条链路（见 engine.Game.act 与 server.Room.on_stall）。"""

    uses_model = True

    def __init__(self, client: LLMClient, seat_id: int,
                 fallbacks: list | None = None) -> None:
        self.client = client
        self.seat_id = seat_id
        self._day = 1                  # 引擎每次调用前会告诉我们现在是第几天
        #: 替补名单：[(平台档案名, 出厂函数或 client), ...]。
        #: ⚠️ **懒建**：出厂函数只在真的要换家时才调用 —— 一个 LLMClient 就是一个
        #:    httpx 连接池，开局把 10 个座位 × 4 家全建出来＝40 个池子，而且没用到的
        #:    永远不会关（实测发现）。传现成 client 也支持（测试会这么用）。
        self.fallbacks = list(fallbacks or [])
        #: 原客户端（换家后**仍然留着引用**）：这一局的复盘经验要归到它名下，
        #: 而不是归给临时顶替的那家（见 experience.update_all）。
        self.primary = client
        self.primary_model = str(getattr(client, "model", "") or "")
        self.switched_to = ""          # 换到了哪家（空 = 一直用原来的）
        self.switch_count = 0
        self._spare: list = []         # 已经建出来的替补客户端（关代理时要一起关）

    def use_fallback(self) -> str | None:
        """换到下一个替补平台，返回平台名（没得换返回 None）。

        老客户端**异步关掉**（它可能还挂着一条永远不会回来的连接）；关不掉只忽略，
        绝不因为"关连接失败"把整手决策搞崩。
        """
        if not self.fallbacks:
            return None
        name, maker = self.fallbacks.pop(0)
        if callable(maker) and not hasattr(maker, "chat_json"):
            nxt = maker()                          # 出厂函数：现在才建连接池
            self._spare.append(nxt)
        else:
            nxt = maker                            # 现成的客户端（测试用）
        old = self.client
        self.client = nxt
        self.switched_to = name
        self.switch_count += 1
        try:
            _spawn(old.aclose())               # 保住引用，别让 task 被 GC 掉
        except Exception:                         # noqa: BLE001
            pass
        return name

    @property
    def model_now(self) -> str:
        return str(getattr(self.client, "model", "") or self.primary_model)

    def set_context(self, day: int, task_kind: str = "") -> None:
        self._day = int(day or 1)

    async def ask(self, packet: str, task: Task) -> dict:
        extra = self.client.extra_for(task.kind, self._day)   # 按任务 + 天数选思考档
        spent = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                 "elapsed": 0.0, "raw": "", "reasoning": "", "model": "", "thinking": {}}
        self._spent = spent

        seen = [None]

        def absorb() -> None:
            call = self.client.last_call or {}
            if not call:
                # 这次调用根本没成功（last_call 还是空的），别凭空记 1 次调用：
                # 原来只判 `call is seen[0]`，而 seen[0] 初值是 None，
                # 于是 3 次全失败会记成 calls=1（实测），把失败当成功计账。
                return
            if call is seen[0]:
                # 这次调用没成功（last_call 没被更新），别把上一次的账再加一遍
                return
            seen[0] = call
            spent["calls"] += 1
            spent["prompt_tokens"] += call.get("prompt_tokens") or 0
            spent["completion_tokens"] += call.get("completion_tokens") or 0
            spent["cached_tokens"] += call.get("cached_tokens") or 0
            spent["elapsed"] = round(spent["elapsed"] + (call.get("elapsed") or 0), 2)
            for key in ("raw", "reasoning", "model"):
                if call.get(key):
                    spent[key] = call[key]
            if call.get("thinking"):
                spent["thinking"] = call["thinking"]

        try:
            reply = self._strip_system(await self.client.chat_json(packet, extra=extra))
        except LLMError as exc:
            absorb()
            return {"_error": str(exc)}
        absorb()

        # 开了思考的模型偶尔会漏字段（实测 reasoning_effort=high 时丢过 claim/reason）。
        # 缺了就再问一次，而且要求它**整份重写**：之前是"缺什么补什么"，
        # 结果把两次生成拼在一起，出现过「发言=我就是个村民」+「声称自己是预言家」这种矛盾台账。
        required = tuple(getattr(task, "required", ()) or ())
        missing = [k for k in required if reply.get(k) in (None, "", [], {})]
        # ⚠️ **自爆豁免**（用户 2026-10-09 的口径：自爆不额外占调用）：
        #    狼在"发言前决策"那一次里如果选了自爆（explode=true），后面的 reads/vote/speech
        #    就没有意义了。这时**绝对不能**因为"缺字段"再发一次"整份重写"的请求 ——
        #    那等于每自爆一次多花一次调用，正好是用户要避免的东西。
        #    （引擎在这些任务上带 `extra={"explode_exempt": True}`。）
        if (missing and truthy(reply.get("explode"), default=False)
                and (getattr(task, "extra", None) or {}).get("explode_exempt")):
            missing = []
        if missing:
            repair = (
                packet
                + "\n\n【你上一次的输出】" + json.dumps(reply, ensure_ascii=False)[:800]
                + "\n\n【问题】上面的输出缺少这些字段：" + "、".join(missing)
                + "。请**整份重写**一遍完整的 JSON（把刚才说过、而且仍然成立的内容一起重写进去，"
                  "不要只补那几个字段，否则前后会自相矛盾）。必须包含："
                + "、".join(required) + "。只输出这一个 JSON，不要别的内容。"
            )
            try:
                fixed = self._strip_system(await self.client.chat_json(repair, extra=extra))
            except LLMError:
                fixed = {}
            absorb()
            fixed_missing = [k for k in required if fixed.get(k) in (None, "", [], {})]
            if fixed and not fixed_missing:
                fixed["_repaired"] = missing          # 完整的第二份：整份替换，杜绝拼接
                fixed["_replaced"] = True
                return fixed
            if len(fixed_missing) < len(missing):
                for key, value in reply.items():      # 也没补全，但比原件强 → 用补件、空位留原件
                    fixed.setdefault(key, value)
                fixed["_repaired"] = missing
                fixed["_still_missing"] = fixed_missing
                return fixed
            reply["_repaired"] = missing
            reply["_still_missing"] = missing
        return reply

    @staticmethod
    def _strip_system(reply: dict) -> dict:
        """丢掉模型自己吐的下划线字段。

        否则它写一个 `_error` 出来，引擎就当成"这次调用失败"、改走随机选择。
        """
        return {k: v for k, v in (reply or {}).items()
                if not (isinstance(k, str) and k.startswith("_"))}

    def last_meta(self) -> dict:
        # 一次 ask 可能含"补齐"的第二次调用，所以这里是两次的合计（原来只记到第二次）
        meta = dict(getattr(self, "_spent", None) or self.client.last_call)
        # 换家信息也带上：引擎据此决定要不要把"替补的思路摘要"写进这个座位的私有经历
        if self.switched_to:
            meta["fallback_to"] = self.switched_to
            meta["fallback_from"] = self.primary_model
            meta["fallback_count"] = self.switch_count
        return meta

    async def aclose(self) -> None:
        await self.client.aclose()
        # 已经建出来的替补客户端也要关：换过家又换回来、或连着换两家时，
        # 不关就会留下没用的连接池（实测发现的问题之一）。
        for extra in self._spare:
            if extra is self.client:
                continue
            try:
                await extra.aclose()
            except Exception:                     # noqa: BLE001
                pass
        self._spare.clear()


# --------------------------------------------------------------------------- #
# 你自己
# --------------------------------------------------------------------------- #

class HumanAgent(BaseAgent):
    """把 packet 打给你看，然后读你的输入。"""

    def __init__(self, seat_id: int, show_packet: bool = True) -> None:
        self.seat_id = seat_id
        self.show_packet = show_packet

    async def _input(self, prompt: str) -> str:
        return (await asyncio.to_thread(input, prompt)).strip()

    async def ask(self, packet: str, task: Task) -> dict:
        if self.show_packet:
            print("\n" + "=" * 78)
            print(packet)
            print("=" * 78)

        kind = task.kind
        legal = list(task.legal)
        hint = "（" + seats_text(legal) + "）" if legal else ""

        if kind == "speech":
            text = await self._input("你的发言：")
            claim = await self._input("你要声称的身份（直接回车 = 不声明）：")
            return {"speech": text or "（我过）", "claim": claim or "无",
                    "suspect": [], "reason": "人类玩家自己说的"}

        # --- 遗言 / 警长（11 人及以上才有）---
        # ⚠️ 同上：`kind == "curse"` 这个夜间提问已经取消，诅咒改成在投票面板里问
        #    （见下面 vote 分支里的 `task.extra["curse"]`）。
        if kind == "lucky":
            use = await self._input("幸运儿：今晚要用掉这一次性技能吗？(y/N)：")
            if not truthy(use, default=False):
                return {"use": False, "reason": "人类玩家留着不用"}
            skill = await self._input("用哪个？填 1=查验 2=毒 3=守护：")
            table = {"1": "check", "2": "poison", "3": "guard"}
            sk = table.get(skill.strip(), "check")
            tgt = await self._input(f"目标是谁？{hint}：")
            # ⚠️ 兜底原来写成 `self.legal[0]` —— HumanAgent 没有 `legal` 属性（只有 task 传进来的
            #    局部 `legal`），真人目标输入非数字时会 AttributeError。这里用本任务的合法名单。
            return {"use": True, "skill": sk,
                    "target": int(tgt) if tgt.strip().isdigit() else (legal[0] if legal else 0),
                    "reason": "人类玩家自己决定的"}
        if kind == "dream":
            # ⚠️ 这里原来写 `self.seat.id` —— HumanAgent 只有 `seat_id`，没有 `seat`：
            #    真人拿到摄梦人牌时**当场崩**（AttributeError）。本项目摄梦人**可以空过**
            #    （用户 2026-10-09 改定），所以文案也不再写"必须"。
            raw = await self._input(
                f"你今晚要摄梦谁？（直接回车 = 今晚不摄/空过；也可以填自己 {self.seat_id}）{hint}：")
            if raw.strip().isdigit() and int(raw) > 0:
                return {"target": int(raw), "reason": "人类玩家自己决定的"}
            return {"abstain": True, "reason": "人类玩家选择今晚不摄（空过）"}

        if kind == "last_words":
            text = await self._input("你的遗言（直接回车 = 不留遗言）：")
            if not text.strip():
                return {"abstain": True, "reason": "人类玩家选择不留遗言"}
            claim = await self._input("遗言里要声称的身份（直接回车 = 不声明）：")
            return {"speech": text.strip(), "claim": claim or "无", "reason": "人类玩家的遗言"}

        if kind == "sheriff_run":
            raw = await self._input("要不要上警？前置位填 f（先发言）、后置位填 b（听完再总结拉票）、"
                                    "不上警填 n（默认 n）：")
            low = normalize_word(raw)
            if low in ("f", "front", "前", "前置", "前置位"):
                return {"run": "front", "reason": "人类玩家自己决定的"}
            if low in ("b", "back", "后", "后置", "后置位"):
                return {"run": "back", "reason": "人类玩家自己决定的"}
            return {"run": False, "reason": "人类玩家自己决定的"}

        if kind == "sheriff_order":
            raw = await self._input("今天从哪个方向发言？顺序填 s，逆序填 r（默认 s）：")
            rev = normalize_word(raw) in ("r", "reverse", "逆", "逆序")
            return {"reverse": rev, "reason": "人类玩家（警长）自己决定的"}

        if kind == "wolf_explode":
            raw = await self._input("要不要现在自爆（亮身份出局、吞掉警徽、今晚照常刀人）？(y/N)：")
            hit = truthy(raw, default=False)
            return {"explode": hit, "who": self.seat_id if hit else None,
                    "reason": "人类玩家（狼）自己决定的"}

        if kind == "sheriff_speech":
            text = await self._input("你的竞选发言：")
            claim = await self._input("要声称的身份（直接回车 = 不声明）：")
            return {"speech": text or "（我没什么要说的。）", "claim": claim or "无",
                    "reason": "人类玩家的竞选发言"}

        if kind == "badge":
            raw = await self._input(f"警徽怎么处理？填座位号传承 {hint}，填 0 = 撕掉警徽：")
            if raw.isdigit() and int(raw) > 0:
                return {"to": int(raw), "reason": "人类玩家决定传承警徽"}
            return {"to": None, "reason": "人类玩家决定撕掉警徽"}

        if kind == "witch":
            victim = task.extra.get("victim")
            if victim is None:
                # 2026-10-25（作者口径）：没有刀口时**不告诉女巫"狼队空刀"** ——
                # 她只该知道"今晚没有人被击杀"，至于原因是没出刀还是被挡住，她分不清。
                # 没有可救的人，所以直接跳过"要不要救"，只问毒药。
                print("    （今晚没有人被击杀，你没有可救的目标）")
                save, save_raw = False, ""
            else:
                save_raw = await self._input(f"今晚被杀的是 {victim}号。用解药救吗？(y/N)：")
            if victim is not None and not save_raw.strip():
                return {"abstain": True, "reason": "人类玩家没输入（视为弃权，不用药）"}
            # 用和引擎同一张"是/否"表：能认 是/救/y/1，也能认 否/不救/n/0
            save = truthy(save_raw, default=False) if victim is not None else False
            poison = None
            if not save and task.extra.get("has_poison"):
                p = await self._input(f"要用毒药吗？填座位号或 0 不用 {hint}：")
                if p.isdigit() and int(p) > 0:
                    poison = int(p)
            return {"save": save, "poison": poison, "reason": "人类玩家自己决定的"}

        # 狼队讨论是人类狼的"发言"环节：要的是文字，不是号码
        # （不给分支的话人类狼只能干等到超时，整晚阶段都会被卡住）
        if kind == "wolf_talk":
            say = await self._input("说给队友听：你今晚想杀谁、为什么？（直接回车 = 没意见）")
            target = None
            pick = await self._input(f"顺手标一下你想刀谁 {hint}（填 0 或回车 = 不指定）：")
            if pick.isdigit() and int(pick) > 0:
                target = int(pick)
            return {"speech": say.strip() or "（我这轮没意见，跟队友走。）",
                    "target": target, "reason": "人类玩家的讨论发言"}

        field = {"wolf_kill": "target", "seer_check": "target",
                 "vote": "vote", "hunter_shoot": "shoot",
                 # 狼王出局时"带走谁"（新板子 `eleven_wolfking`）：跟猎人开枪同一套选号面板
                 "wolf_king_take": "take",
                 "sheriff_vote": "vote", "badge": "to"}.get(kind, "target")
        # 养鸦人（本项目民牌）：诅咒发生在**放逐投票这一刻**，和投票同一次调用 ——
        # 所以先问"要不要诅咒、咒谁"，再问投谁（用户 2026-10-09 改定的时机）。
        curse_target = None
        if kind == "vote" and (task.extra or {}).get("curse"):
            cl = list((task.extra or {}).get("curse_legal") or legal)
            c_hint = "（" + seats_text(cl) + "）" if cl else "（今天没有可咒的目标）"
            want = await self._input(f"养鸦人：这一票之前要不要用诅咒？（直接回车/y = 用，n = 不用）")
            if cl and (not want.strip() or want.strip().lower().startswith("y")):
                who = await self._input(f"诅咒谁？{c_hint}：")
                if who.strip().isdigit() and int(who) in cl:
                    curse_target = int(who)
        raw = await self._input(f"请选择 {hint}（填 0 或直接回车 = 放弃这一手）：")
        if raw.isdigit():
            value = int(raw)
            if value == 0:
                return {"abstain": True, "reason": "人类玩家选择放弃",
                        "curse_target": curse_target}
            out = {field: value, "reason": "人类玩家自己决定的"}
            if curse_target is not None:
                out["curse_target"] = curse_target
            return out
        return {"abstain": True, "reason": "人类玩家没给出有效输入（视为放弃）",
                "curse_target": curse_target}


# --------------------------------------------------------------------------- #
# 手机端的你
# --------------------------------------------------------------------------- #

class WebHumanAgent(BaseAgent):
    """把上下文推给浏览器，然后等你点按钮。room 由 server.py 提供。"""

    def __init__(self, seat_id: int, room) -> None:
        self.seat_id = seat_id
        self.room = room
        #: 2026-11-03：托管替玩家出手时，server 会把那次调用的用量写在这里，
        #: engine 的 transcript 就能记下"模型/耗时/token"，不再是一行空记录。
        self._last_meta: dict = {}

    def last_meta(self) -> dict:
        return dict(self._last_meta or {})

    async def ask(self, packet: str, task: Task) -> dict:
        self._last_meta = {}          # 每一手开始先清空，免得上一手的托管用量串过来
        room = self.room
        # 2026-10-31 联机：一个房间里可能同时有多个真人（投票/狼队决策是 gather 并发问的）。
        # 所以「提问」必须按座位分别建 future，不能再共用一个 `room.awaiting`。
        future = room.ask(self.seat_id, {
            "type": "ask",
            "seat": self.seat_id,
            "kind": task.kind,
            "text": task.text,
            "schema": task.schema,
            "legal": list(task.legal),
            "extra": dict(task.extra),
            "packet": packet,
        })
        #: 这一手的面板编号：必须在**父类清掉 pending_ask 之前**抓下来，
        #: 超时后补发的 `ask_done` 要靠它让前端只清对应那一个面板。
        pending_id = (room.pending_asks.get(self.seat_id) or {}).get("ask_id")
        try:
            action = await asyncio.wait_for(future, timeout=room.human_timeout)
        except asyncio.TimeoutError:
            room.close_ask(self.seat_id, pending_id)   # 这一手已经作废，别在重连时把过期面板补回来
            # ⚠️ 2026-10-31 外部审查 P1-04：这条通知以前是 `room.broadcast(...)` —— 全房都收。
            #    夜里只有狼 / 预言家 / 女巫 / 摄梦人 / 守卫在动，联机局里别人一收到这条，
            #    就知道「某个真人正在做夜间行动」，等于把身份类别漏出去。现在只发给本人。
            _notice = {"type": "notice",
                       "text": "太久没动，这一手按「弃权」处理（不会替你随机选人）。"}
            _notify = getattr(room, "notify_seat", None)
            if callable(_notify):
                try:
                    _notify(self.seat_id, _notice)
                except Exception as exc:        # noqa: BLE001 —— 通知失败不影响对局
                    print(f"[ask] 超时通知没发出去（座位 {self.seat_id}）：{type(exc).__name__}: {exc}")
            else:
                room.broadcast(_notice)         # 旧房间/测试桩没有 notify_seat：保持旧行为
            # ⚠️ 2026-10-25：超时后必须补一条 `ask_done`，否则手机上的行动面板会一直赖着，
            #    玩家回来点它只会收到「现在不是你行动的时间」。以前这件事由 `server.py`
            #    的 `ServerHumanAgent` **临时替换全局 `asyncio.wait_for`** 来实现 ——
            #    那会误伤同一事件循环里别的协程，已改成这里的显式钩子。
            hook = getattr(room, "on_human_timeout", None)
            if callable(hook):
                try:
                    hook(self.seat_id, pending_id)
                except Exception as exc:        # noqa: BLE001 —— 收尾广播失败不影响对局
                    # 2026-10-31 外部审查 D 批 P2：这里原来裸 `pass`，钩子一失败面板就
                    # 永远留在手机上、日志里一个字都没有。至少留一行。
                    print(f"[ask] on_human_timeout 失败（座位 {self.seat_id}）："
                          f"{type(exc).__name__}: {exc}")
            return {"abstain": True, "reason": "人类玩家超时（弃权）"}
        finally:
            room.drop_ask(self.seat_id, future)
        # 空 dict（页面上什么都没选就点了确认）也算弃权，不能让它掉进"随机选一个"的分支
        if not isinstance(action, dict) or not action:
            return {"abstain": True, "reason": "空输入（弃权）"}
        return action
