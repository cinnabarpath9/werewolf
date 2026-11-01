# coding=utf-8
"""测试用的内容文件读取（2026-10-31 拆书后的统一入口）。

以前测试直接读 `content/打法书.md`；现在打法书拆成两份（战术书 + 角色书），
规则/术语/概念也带上了 00~07 的序号。测试都从这里拿路径与原文，改名时只改这一处。
"""
from __future__ import annotations

import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTENT = os.path.join(ROOT, "content")

RULES = os.path.join(CONTENT, "01_规则书.md")
FLOW = os.path.join(CONTENT, "02_流程书.md")
BOARDS = os.path.join(CONTENT, "03_板子规则书.md")
ROLES = os.path.join(CONTENT, "04_角色书.md")
TACTICS = os.path.join(CONTENT, "05_战术书.md")
CONCEPT = os.path.join(CONTENT, "06_概念库.md")
TERMS = os.path.join(CONTENT, "07_术语库.md")


def read(path: str) -> str:
    try:
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except OSError:
        return ""


def playbook_raw() -> str:
    """整本打法书（战术书 + 角色书，保留 frag 标记）—— 与 `tabletalk.load_playbook` 默认读到的内容一致。"""
    return read(TACTICS) + "\r\n" + read(ROLES)
