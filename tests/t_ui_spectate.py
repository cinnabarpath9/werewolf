# coding=utf-8
"""观战模式 + 法官播报 + 压缩开关的接线回归（2026-11-04）。

- 观战：前端必须有 `#btnSpectate`，点它发的开局报文是 `human_seat: 0`（全 AI、不占座位）；
  结束/普通开局要能清掉观战标记；
- 法官播报：8 条预录音频必须在 `web/audio/narrator/`，前端按公告关键词挑一条播放；
- 压缩：`game.context_compress` 默认 `off`，且 `Game` 构造参数存在。

跑法：python tests/t_ui_spectate.py
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

N = 0
BAD = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


html = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()

print("=== 一、观战模式 ===")
check("1a 有观战按钮 #btnSpectate", 'id="btnSpectate"' in html)
check("1b 点子有绑定", "btnSpectate').onclick = confirmSpectate" in html)
check("1c 观战发的是 human_seat: 0（空出座位）", "human_seat: 0" in html)
check("1d 重开一局时保持观战（human_seat 用 S.spectate ? 0 : curSeat()）",
      "S.spectate ? 0 : curSeat()" in html)
check("1e 结束/普通开局清观战标记", html.count("S.spectate = false;") >= 2)


check("1f 观战态有可见提示（观战牌）", "观战中（全 AI）" in html)

print("\n=== 二、法官播报（预录音频） ===")
clips = ["night_fall", "day_break", "badge_election", "badge_pk",
         "vote_start", "peaceful_night", "someone_died", "game_over"]
missing = [c for c in clips
           if not os.path.isfile(os.path.join(ROOT, "web", "audio", "narrator", c + ".mp3"))]
check("2a 8 条播报音频都在", not missing, str(missing))
check("2b 前端有 narratorSay 接线", "function narratorSay(" in html and "narratorSay(e.text)" in html)
check("2c 播报走本地文件（不是实时 TTS）", "audio/narrator/" in html and "wolf.narrator" in html)

check("2d #askFold 面板收起有接线（且保留输入内容）",
      'id = \'askFold\'' in html or "askFold" in html)

print("\n=== 三、压缩开关 ===")
cfg = io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8-sig").read()
check("3a 默认值是 off", re.search(r"^\s*context_compress: off\s*$", cfg, re.M) is not None)
sys.path.insert(0, ROOT)
from wolf.engine import Game                                   # noqa: E402
from wolf.roles import make_board                              # noqa: E402
_g = Game(board=make_board("six_all_reveal"), human_seat=0, agent_factory=lambda s: None,
          on_event=lambda *a, **k: None, context_compress="on")
check("3b 非法值按 off 处理", _g.context_compress == "off", _g.context_compress)
check("3c 非法值留下一条告警（不静默）",
      any("context_compress" in w for w in getattr(_g, "_config_warns", [])),
      str(getattr(_g, "_config_warns", []))[:60])
check("3d 测量脚本在", os.path.isfile(os.path.join(ROOT, "tools", "context_ab.py")))

print("\n" + "=" * 60)
print(f"共 {N} 项：通过 {N - BAD}，失败 {BAD}")
raise SystemExit(1 if BAD else 0)