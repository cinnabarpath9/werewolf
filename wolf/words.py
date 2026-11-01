# coding=utf-8
"""「是 / 否」这类回答的公共归一表（2026-10-08 新增）。

为什么单独开一个模块
--------------------
这张表以前只活在 `wolf/engine.py` 里（`TRUE_WORDS` / `FALSE_WORDS` /
`normalize_word` / `truthy`），而 `wolf/context.py` 自己又写了一份**更窄**的假值表
（只有 `false/no/off/0/空`）—— 于是同一句「关」在引擎里认、在配置开关上不认：
`game.attach_claimed_chapter: 关` 会被读成 **True**（"看起来关了其实还开着"）。

靠 `from .engine import ...` 让两边共用是**不行的**：`engine` 反向依赖 `context`
（`from . import context as ctx`），那样就是一个循环 import。
所以把表挪到这个**谁都不依赖**的小模块里，`engine` 与 `context` 各自从它取。

⚠️ 这里只放"纯字符串判定"，不放任何游戏逻辑 —— 谁都能安全 import。
"""

from __future__ import annotations

#: 模型/配置把布尔写成各种花样时的归一化表（`bool("false")` 是 True，所以必须显式判）。
#: 中文那几项是 2026-10-08 补的：用户在 config.yaml 里就爱写「开 / 关」。
#: ⚠️ `on` / `off` 是原来 context 那份窄表里有的（`off` 少写了会让
#:    `attach_claimed_chapter: off` 被读成**开**）—— 合并两张表时一个都不能漏。
TRUE_WORDS = {"true", "yes", "y", "1", "是", "救", "用", "开", "开启", "on", "true.", "对"}
FALSE_WORDS = {"false", "no", "n", "0", "否", "不救", "不用", "不开", "关", "关闭", "off",
               "false.", "错", "不"}


def normalize_word(value) -> str:
    """把"是/否"这类回答归一成小写关键词。

    truthy 和"识别不了就写提示"那处**必须共用这一个函数**：
    否则会出现「按'救'执行了」+「提示说你写的不认识，我按不救处理」这种自相矛盾的私有经历。

    ⚠️ 顺手剥掉两端引号：YAML 里写 `attach_claimed_chapter: "false"` 拿到的是带引号的字符串。
    """
    return str(value if value is not None else "").strip().strip('"\'').strip("。.").strip().lower()


def truthy(value, default: bool = False) -> bool:
    """把模型给的"是/否"归一成布尔。判不出来就用 default（并让调用方记一笔）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = normalize_word(value)
    if not text:
        return default
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return default
