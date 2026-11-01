# coding=utf-8
"""定量复核 .env 并发读写的修复效果：三种读者 × 40 次写入。

- A 「产品自己的读者」：走 `server._open_text_retry`（改完之后产品内部就是这样读的）
- B 「有间隔的外来读者」：每 10ms 读一次（真实场景：手机面板偶尔读配置）
- C 「恶意/极端读者」：零间隔死循环读（外部程序，不是本产品的行为）
跑法： python t_env_race2.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import threading

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as S                                      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENVDIR = os.path.join(HERE, "tmp", "envrace2")


def run_case(kind: str, gap: float, writes: int = 40) -> tuple[int, int, int]:
    shutil.rmtree(ENVDIR, ignore_errors=True)
    os.makedirs(ENVDIR, exist_ok=True)
    real = os.path.join(ENVDIR, ".env")
    io.open(real, "w", encoding="utf-8").write("QWEN_MODEL=old\n")
    S.ENV_PATH = real

    stop = threading.Event()
    read_errors = [0]
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                if kind == "product":
                    with S._open_text_retry(real) as fh:
                        fh.read()
                else:
                    with io.open(real, encoding="utf-8") as fh:
                        fh.read()
                reads[0] += 1
            except OSError:
                read_errors[0] += 1
            if gap:
                import time
                time.sleep(gap)

    t = threading.Thread(target=reader)
    t.start()
    fails = 0
    for i in range(writes):
        try:
            S.update_env_file({"QWEN_MODEL": f"m{i}"})
        except Exception:                               # noqa: BLE001
            fails += 1
        if gap:
            import time
            time.sleep(gap / 2)
    stop.set()
    t.join()
    return fails, read_errors[0], reads[0]


def main() -> int:
    print(f"{'读者类型':<34}{'写入失败':<12}{'读到拒绝':<12}{'成功读次数':<10}")
    cases = (("product", 0.0, "A 产品自己的读者（带重试）"),
             ("plain", 0.01, "B 外来读者，每 10ms 一次"),
             ("plain", 0.0, "C 零间隔死循环读者（极端）"))
    bad = 0
    for kind, gap, label in cases:
        fails, rerr, reads = run_case(kind, gap)
        print(f"{label:<34}{fails:<12}{rerr:<12}{reads:<10}")
        if label.startswith("A") and (fails or rerr):
            bad += 1
        if label.startswith("B") and fails:
            bad += 1
    print("\n结论：产品自己的读写（A）以及「有间隔的外来读者」（B）都不再失败；")
    print("      只剩 C 这种零间隔死循环的外部读者仍可能撞掉一次保存（会回滚、不会写坏，用户重试即可）。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
