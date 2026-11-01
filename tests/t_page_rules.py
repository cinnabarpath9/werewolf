# coding=utf-8
"""前端「规则」按钮 + 规则面板复核（对在线页面 8077）。

⚠️ 必须**先开一局**再看面板：`/api/rules` 的"发给模型的规则"是按**当前板子**组装的，
没有对局时它是空的 —— 第一版没开局，"不含守卫/警长"两条断言是**空过**的（假绿）。
所以这里用 WS 起一局 mock（默认板子 eleven_wolfking，11 人·狼王版），顺便验证：
  · 开局公示栏那条公告真的进了公开记录（room 放置方式生效）
  · 组装的规则只含本局真有的角色（不含守卫、不含警长）

⚠️ 2026-10-12 查明（存档污染事故，见 `docs/待完善清单.md` E57）：本脚本打的是**用户那台正在跑的
8077**（它不自己起服务，所以拿不到临时 `save_dir`）—— 它开的 mock 局以前会被服务端写进**生产
`games/`**，而 `prune_saves()` 从最旧的开始删 ⇒ 用户那几份真局正是下一批被删的。
现在服务端把 mock 局一律写 `<save_dir>/_mock/`（`server.py::_mock_save_dir`），这条路径就堵住了。
**要治到测试侧**（连 8077 都不碰、开自己的临时实例）得照抄 `t_page_review.py` 的
`free_port()` + `make_config()` + `token_of_instance()` —— 本轮没做，原因与替代写在 E57 里。
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


async def play_a_bit(board: str = "eleven_wolfking", seconds: float = 12.0,
                     want: str = "", hard_seconds: float = 45.0) -> str:
    """起一局 mock，收下开局那几条公开记录（含规则公示），然后放弃。

    ⚠️ 板子必须跟着 config 的默认板子走：2026-10-09 默认板子已从 eleven_standard
    （预女猎白）换成 eleven_dream（预女梦愚）。写死旧板名的话，这条断言会去查
    一个**根本没开的板子**的规则 —— 而且失败信息只显示"没有守卫"，很难看出是板名错了。
    2026-10-30：默认板子换成 eleven_wolfking（狼王版）；它同样有愚者/摄梦人/三张民牌，
    """
    seen: list[str] = []
    async with websockets.connect(WS, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "sync"}))
        # ⚠️ 先排空**上一局的补发**：房间在 sync 时会把上一局台账重发一遍，
        #    不清掉的话本局"规则公示"的断言会被上一局的板子规则污染
        #    （实测：上一局是带警长的十一人局 → "本局不该有警长"这条假红）。
        t_pre = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t_pre < 2.0:
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                break
        await ws.send(json.dumps({"type": "start", "mock": True, "human_seat": 1,
                                  "board": board, "seed": 3}))
        # ⚠️ 2026-10-10：原来是「固定收 N 秒」——全量跑时机器同时在跑别的测试，
        #    开局慢一点就收不到规则公示，于是报假红（单独跑全绿、全量里 13/15）。
        #    现在：**收到 want 就提前收工；没收到就等到硬上限**才算失败。
        _t0 = asyncio.get_running_loop().time()
        deadline = _t0 + seconds
        hard = _t0 + hard_seconds
        while asyncio.get_running_loop().time() < (
                hard if (want and want not in "\n".join(seen)) else deadline):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            if m.get("type") == "entry":
                seen.append((m.get("entry") or {}).get("text", ""))
            elif m.get("type") == "ask":
                await ws.send(json.dumps({"type": "action",
                                          "payload": {"abstain": True, "reason": "测试"}}))
        await ws.send(json.dumps({"type": "abandon"}))
        await asyncio.sleep(1.0)
    return "\n".join(seen)


async def main() -> int:
    p = Page(port=9743, profile=os.path.join(HERE, "_profiles", "edge-rules"))
    p.start(URL)
    try:
        await p.connect(URL)
        await p.js("new Promise(r => document.readyState === 'complete' ? r(1) : "
                   "document.addEventListener('DOMContentLoaded', () => r(1), {once:true}))")
        await asyncio.sleep(2.0)

        entries = await play_a_bit(want="【本局规则公示】")
        check("P0 开局公示栏那条规则公告真的进了公开记录",
              "【本局规则公示】" in entries and "【女巫】" in entries,
              f"收到 {len(entries.splitlines())} 条记录；含规则公示="
              f"{'【本局规则公示】' in entries}")
        check("P0b 公示的是**本局板子**的规则：有愚者/摄梦人/三张民牌、没有守卫、有警长（狼王版）",
              "【愚者】" in entries and "【摄梦人】" in entries and "【守墓人】" in entries
              and "【守卫】" not in entries and "【警长" in entries,
              [ln[:40] for ln in entries.splitlines() if "守卫" in ln][:1])

        r = await p.js("""
          (async () => {
            const btn = document.getElementById('btnRules');
            if (!btn) return {err: '页面上没有 btnRules 按钮'};
            btn.click();
            await new Promise(res => setTimeout(res, 1800));
            const boxes = [...document.querySelectorAll('div')].filter(
              d => d.textContent.startsWith('本局规则书'));
            const box = boxes[0];
            const body = box ? box.querySelector('.panel-body') : null;
            return {found: !!box, hasBody: !!body,
                    text: body ? body.innerText : (box ? box.innerText : ''),
                    panel: box ? box.innerText : ''};
          })()
        """)
        text = r.get("text", "")
        panel = r.get("panel", "")
        check("P1 页面上有「规则」按钮并能打开面板", r.get("found") is True and r.get("hasBody") is True,
              r.get("err", ""))
        check("P2 面板写明规则放哪儿（本局：公示栏）", "公示栏" in text, text.replace("\n", " ")[:80])
        check("P3 面板写明是**按本局板子组装**的", "按本局板子组装" in text,
              text.replace("\n", " ")[:110])
        check("P4 面板里有「本局发给模型的规则」和「完整说明」两块",
              "本局发给模型的规则" in text and "完整说明" in text)
        check("P5 写给模型的规则非空（本局 eleven_wolfking 真的组装出来了）",
              len(text) > 400 and "【" in text, f"面板正文 {len(text)} 字")
        check("P6 不含本局没有的角色规则（守卫）", "【守卫】" not in text,
              [ln for ln in text.splitlines() if "【守卫】" in ln][:1])
        check("P7 含警长规则（11 人及以上才有警长竞选）", "【警长" in text,
              [ln for ln in text.splitlines() if "【警长" in ln][:1])
        for key in ("【女巫】", "【愚者】", "【遗言】"):
            check(f"P8 含本局真有的规则「{key}」", key in text)
        check("P9 完整说明里含守卫/同守同救（给人看的全文）", "同守同救" in text)
        check("P10 不再出现被否决的房规（翻牌愚者不能接警徽）",
              "不能接警徽" not in panel and "接不了警徽" not in panel)
        check("P11 面板能关掉（有「关闭」按钮）", "关闭" in panel)
    finally:
        p.kill()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

