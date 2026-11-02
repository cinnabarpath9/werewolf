# coding=utf-8
"""托管模型（「托管一手」）的两个小工具：提示补强 + 平台风格推断。

为什么单独一个文件：2026-11-03 查存档发现**托管发言只有 24~30 字，而 AI 座位平均 243 字**。
根因三条：
  1. 托管拿的是**人类面板的 packet**（"请说明你的判断…350 字以内"），没有 AI 座位那份
     "先想清楚再说话 / 逐个点评 / 立场与票型"的脚手架；
  2. 托管当时固定用 `plain` 风格 → 配的思考档（low/high）**根本没发出去**；
  3. 默认档案是 economy 的便宜模型，本来就话少。

这里把 1、2 修掉：补一段明确的「托管要求」，并按平台把思考档真正发出去。
`butler_client()` 调这两个函数。
"""
from __future__ import annotations

#: 哪些任务需要"像人一样把话说完整"（其余任务保持原样，不硬塞字数）
SPEECH_TASKS = ("speech", "speech_plan", "sheriff_speech", "last_words", "sheriff_speech_pk")

#: 追加到 packet 末尾的托管要求（只对发言类任务）
SPEECH_REQUIREMENT = """
【托管要求（这一条比上面任何"简短"的提示都优先）】
你不是在填表，是在替一位真人玩家发言。请像真人一样把话说完整：
· 先给结论（你怀疑谁 / 你信谁 / 这轮倾向投谁），再给 1~2 条理由；
· 至少 3~5 句、不少于 80 字（上限仍是 350 字）；
· 主动点名场上的人（"X号像狼，因为…"），不要只写一句场面话；
· 不要用「X号。」开头复述自己的座位号，也不要写旁白或心理描写。
"""


def augment_packet(packet: str, task_kind: str) -> str:
    """给发言类任务补上"把话说完整"的要求；其它任务原样返回。"""
    text = str(packet or "")
    if not text or str(task_kind or "") not in SPEECH_TASKS:
        return text
    if "【托管要求" in text:            # 幂等：重复调用不会叠两段
        return text
    return text.rstrip() + "\n" + SPEECH_REQUIREMENT


def infer_thinking_style(base_url: str) -> str:
    """按渠道推断"认哪套思考参数"，让托管配置的思考档真的生效。

    只推断**明确知道**的三类；其余一律 `plain`（宁可不发，也不发错字段招 400）。
    """
    url = str(base_url or "").lower()
    if "volces.com" in url or "ark.cn" in url or "ark-" in url:
        return "ark"
    if "qianfan" in url or "baidubce.com" in url:
        return "openai"          # 千帆的 OpenAI 兼容层认 reasoning_effort
    if "openai.com" in url or "deepseek.com" in url:
        return "openai"
    return "plain"


def thinking_table(level: str) -> dict:
    """把「思考档」变成 `LLMClient` 要的那张表；空/非法 = 空表（客户端会回退它自己的默认档）。"""
    lv = str(level or "").strip().lower()
    if lv not in ("off", "low", "high"):
        return {}
    return {"speech": {1: lv}, "decision": {1: lv}, "aux": {1: lv}}

def build_speech_call(game, seat, day: int):
    """用**真实模型座位那套**上下文给托管拼一次发言调用（作者 2026-11-03 要求）。

    以前托管拿的是人类面板的短提示（"请说明你的判断…"），比 AI 座位少一截脚手架，
    于是 243 字 vs 27 字。这里直接复用 `context.build()`（全系统唯一的 packet 装配器），
    任务说明也按 AI 座位的 speech_plan 口径写 —— 规则书/打法书/概念库/台账一个不少。

    返回 `(task, packet)`。
    """
    from wolf import context
    from wolf.context import Task

    others = [i for i in game.alive_ids() if i != seat.id]
    text = (
        f"第 {day} 天，马上轮到你发言了。**这一次回复里把两件事一起做完**：\n"
        f"1) 先想清楚（决策）：回顾你已知的信息 + 目前为止所有人的发言，"
        f"逐个点评活着的玩家（{'、'.join(f'{i}号' for i in others)}）：好人还是狼？像什么身份？为什么？\n"
        f"2) 判断你该**保谁**、该**打谁**（要打就打最像狼的）。\n"
        f"3) 定下你的立场和此刻的投票倾向（vote，只是倾向，投票前还会再定一次）。\n"
        f"4) 用 `speech` 写出你这一轮**要说出口的话**（口语化，350 字以内）："
        f"先给结论、再给 1~2 条理由，至少两三句；不要用「X号。」开头复述自己的座位号。"
    )
    schema = (
        '{"reads": [{"seat": <编号>, "camp": "wolf|good", "role": "<猜的身份或 未知>", '
        '"why": "<一句>"}], "summary": "<两三句局势判断>", '
        '"protect": [<你要保的编号>], "attack": [<你要打的编号>], '
        '"claim": "<你打算声称的身份，没有就写 无>", "stance": "<立场一句话>", '
        '"vote": <此刻倾向投谁>, "speech": "<你要说出口的话，口语化，350 字以内>"}'
    )
    task = Task(kind="speech_plan", text=text, schema=schema, legal=list(others),
                required=("reads", "vote", "speech"))
    return task, context.build(game, seat, task)

