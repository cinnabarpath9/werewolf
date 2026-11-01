# coding=utf-8
"""量化 .env 原子写在 Windows 上的一个副作用：一边读一边写时 os.replace 会被拒绝。

背景：进程内 6 线程并发写已经修好了（F1 通过）。但 Windows 上 `os.replace` 要求目标文件
没有被别人以"不共享删除"的方式打开 —— Python 的 open() 就是这种。所以**读的人在场**时，
写的人可能直接 PermissionError（代码会回滚，不会写坏，但这次保存就是失败了）。

这里量一下：不同读取频率下，40 次写入里有几次失败。
跑法： python t_env_race.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as S                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENVDIR = os.path.join(HERE, "tmp", "envrace")


def run_case(read_gap: float, writes: int = 40) -> tuple[int, int]:
    shutil.rmtree(ENVDIR, ignore_errors=True)
    os.makedirs(ENVDIR, exist_ok=True)
    real = os.path.join(ENVDIR, ".env")
    io.open(real, "w", encoding="utf-8").write("QWEN_MODEL=old\n")
    S.ENV_PATH = real

    stop = threading.Event()
    read_errors: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                with io.open(real, encoding="utf-8") as fh:
                    fh.read()
                reads[0] += 1
            except OSError as exc:
                read_errors.append(f"{type(exc).__name__}")
            if read_gap:
                time.sleep(read_gap)

    t = threading.Thread(target=reader)
    t.start()
    fails = 0
    for i in range(writes):
        try:
            S.update_env_file({"QWEN_MODEL": f"m{i}"})
        except Exception:                               # noqa: BLE001
            fails += 1
        if read_gap:
            time.sleep(read_gap / 2)
    stop.set()
    t.join()
    return fails, len(read_errors)


def main() -> int:
    print(f"{'读取频率':<14}{'40 次写入失败':<16}{'读取失败':<10}")
    for gap, label in ((0.0, "不停读(最狠)"), (0.001, "每 1ms 读"),
                       (0.01, "每 10ms 读"), (0.05, "每 50ms 读"),
                       (0.2, "每 200ms 读")):
        fails, rerr = run_case(gap)
        print(f"{label:<14}{fails:<16}{rerr:<10}")
    print("\n结论：写失败的每一次都会回滚（.env 不会写坏），但这一次保存对用户就是失败的；")
    print("      读取失败是毫秒级的瞬时拒绝，重试即可。真实场景：手机面板读配置的同时，")
    print("      电脑上正好在跑 tools/fill_from_txt.py 写 .env。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
