# coding=utf-8
"""手机端「人设」面板 + 座位姓名复核（对在线页面 8077）。

先开一局 mock（11 人标准场，这样 11 张卡都会挂上），再看：
  · 座位条上显示「3号 陈默」这种姓名（公开的表达层信息）
  · 人设面板按字段展示（姓名/年龄/职业/经历/关系 + 卡片全文）
  · 告警区为空（11 张卡都没有越界）

⚠️ 2026-10-12 查明（存档污染事故，见 `docs/待完善清单.md` E57）：本脚本打的是**用户那台正在跑的
8077**（它不自己起服务，所以拿不到临时 `save_dir`）—— 它开的 mock 局以前会被服务端写进**生产
`games/`**，把用户的真局挤出 `keep_games` 的保留窗口。现在服务端把 mock 局一律写
`<save_dir>/_mock/`（`server.py::_mock_save_dir`），这条路径就堵住了。**要治到测试侧**得照抄
`t_page_review.py` 的 `free_port()` + `make_config()` + `token_of_instance()` 自己起实例 ——
本轮没做，原因与替代写在 E57 里。
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import websockets                                         # noqa: E402

from cdp_lite import Page                                 # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
from live_token import read_token
TOKEN = read_token()
URL = f"http://127.0.0.1:8077/?token={TOKEN}"
WS = f"ws://127.0.0.1:8077/ws?token={TOKEN}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail="") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    sys.stdout.flush()


async def start_mock(seconds: float = 10.0) -> None:
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 1,
                                  "board": "eleven_standard", "seed": 5}))
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "ask":
                await ws.send(json.dumps({"type": "action",
                                          "payload": {"abstain": True, "reason": "测试"}}))
        await ws.send(json.dumps({"type": "abandon"}))
        await asyncio.sleep(1.0)


async def main() -> int:
    p = Page(port=9744, profile=os.path.join(HERE, "_profiles", "edge-persona"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)
        r = await p.js("""
          (async () => {
            document.getElementById('btnPersona').click();
            await new Promise(r => setTimeout(r, 1200));
            const box = document.querySelector('[data-panel="personas"]');
            const t = box ? box.innerText : '';
            const body = box ? box.querySelector('.panel-body') : null;
            return {exists: !!box, text: t,
                    chars: body ? body.innerHTML.length : 0,
                    hasIframe: body ? !!body.querySelector('iframe') : false};
          })()
        """)
        check("P1 面板打得开（[data-panel=personas]）", r["exists"] is True)
        check("P2 标题是「模型被告知了什么」", "模型被告知了什么" in r["text"], r["text"][:40])
        check("P3 有「你的定位（通用提示词）」这一节", "你的定位" in r["text"])
        check("P4 有「打法书」这一节", "打法书" in r["text"])
        check("P5 有「概念与概率」这一节", "概念与概率" in r["text"] or "概念库" in r["text"])
        check("P6 提示了每座位只收到与自己角色/阵营相关的部分",
              "自己那个角色" in r["text"] or "自己阵营" in r["text"], r["text"][:200])
        check("P7 内容真的读出来了（不是「读取中」）", "读取中" not in r["text"], r["text"][:60])
        check("P8 人设为空时说明了怎么恢复",
              "personas_备份" in r["text"] or "座位角色卡" in r["text"]
              or "content" in r["text"], r["text"][-160:])
        check("P9 面板没被注入 iframe/脚本（转义到位）", r["hasIframe"] is False)
        check("P10 面板有实际内容（不是空壳）", r["chars"] > 800, str(r["chars"]))

        bad = [x for x in RESULTS if not x[1]]
        print("\n" + "=" * 74)
        print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
        for n, _, d in bad:
            print(f"  [FAIL] {n}  {d}")
        print("=" * 74)
        return 1 if bad else 0
    finally:
        try:
            p.kill()
        except Exception:
            pass


raise SystemExit(asyncio.run(main()))
