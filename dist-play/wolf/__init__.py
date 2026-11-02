# coding=utf-8
"""狼人杀内核。

导入这个包时会顺手把控制台输出切成 UTF-8：
Windows 默认是 GBK，脚本里打印 ✅/❌/🏁 会直接 UnicodeEncodeError 崩掉
（看起来像"自检没过"，其实是编码问题）。放在这里，凡是 import wolf 的脚本都自动生效。
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")   # type: ignore[attr-defined]
    except Exception:                           # noqa: BLE001
        pass

__all__ = ["roles", "ledger", "context", "llm", "agents", "engine",
           "routing", "persona", "experience"]
