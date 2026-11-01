# coding=utf-8
"""**对局进行中保存页面设置**会不会把这一局搞崩（2026-10-31，作者问的）。

背景：页面设置接口（`/api/settings/local/*`）保存时会立刻
`settings_store.apply(..., clear_missing=True)` 改 `os.environ` 与 `cfg.llm.thinking_style`，
而这时可能正有一局在跑。作者的口径是「可以中途改设置，但要先确认它不会把对局或进程弄崩」。

结论（本脚本守的就是这几条）：
  1. 运行中的座位用的是**开局时就建好的** LLM 客户端（base_url / key / model / 思考档位
     都在 `LLMClient.__init__` 里拍成快照），所以中途改设置**不会**改变已在对局里的那些调用；
  2. `apply()` 只碰 `os.environ` 与 `cfg`，不碰 `Game` / `Room` 的任何对局状态；
  3. 改完 `VoiceConfig` 也不影响文字流程（语音失败只影响声音，不影响胜负）。

所以：**允许中途保存**，影响范围是"下一局生效 + 语音立刻换"，不是"这一局崩"。本脚本用
一局 mock 对局把这条钉住：开局 → 中途 apply → 这一局必须照常打完并给出胜负。

跑法： python t_settings_midgame.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wolf import settings_store, voice                       # noqa: E402
from wolf.agents import MockAgent                            # noqa: E402
from wolf.engine import Game                                 # noqa: E402
from wolf.roles import make_board                            # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + detail) if detail else ""))
    sys.stdout.flush()


def build_mock_game(max_days: int = 2) -> Game:
    rng = random.Random(7)

    def factory(seat):
        return MockAgent(seat.id, random.Random(1000 + seat.id))

    return Game(board=make_board("eleven_wolfking"), seed=7, agent_factory=factory,
                on_event=lambda *a, **k: None, max_days=max_days)


async def main() -> int:
    print("=== 1) 中途改设置：对局照常打完 ===")
    cfg: dict = {"llm": {"thinking_style": {}}}
    data = settings_store.normalize({
        "apis": [{"id": "ZZMID", "name": "ZZMID", "base_url": "http://127.0.0.1:8098/v1",
                  "model": "fake-model", "key": "fake-key"}],
        "seats": {"1": "ZZMID", "2": "ZZMID"},
        "voice": {"base_url": "", "api_key": "", "tts_model": "", "asr_model": ""},
    })

    g = build_mock_game(max_days=2)
    crashed: list[str] = []

    async def tweak() -> None:
        """模拟"对局跑到一半，用户在页面上点了保存"。"""
        await asyncio.sleep(0.05)
        try:
            settings_store.apply(cfg, data, clear_missing=True)   # 与保存接口同一条路径
            voice.VoiceConfig.from_env(cfg)                        # 与 voice 保存接口同一条路径
        except Exception as exc:                                   # noqa: BLE001
            crashed.append(f"{type(exc).__name__}: {exc}")

    winner = await asyncio.gather(g.run(), tweak(), return_exceptions=True)
    got = winner[0]
    check("1a 中途 apply 没有抛异常", not crashed, "；".join(crashed))
    check("1b 这一局照常打完（拿到胜负，不是异常/中断）",
          isinstance(got, str) and got in ("wolf", "good", "draw"), repr(got))
    check("1c 台账非空（说明流程真的推进到了结尾）", len(g.ledger.entries) > 10,
          f"entries={len(g.ledger.entries)}")

    print("\n=== 2) 中途改的是「下一局生效」，不是「这一局换模型」 ===")
    print("  （说明：LLMClient 在 __init__ 里就把 base_url/key/model/思考档位拍成快照，")
    print("    运行中的座位不会被中途改设置换掉；这条靠代码结构保证，脚本只做静态断言。）")
    src = open(os.path.join(ROOT, "wolf", "llm.py"), encoding="utf-8").read()
    check("2a 客户端在构造时拍下 base_url / model（不是每次请求现读配置）",
          'self.base_url = base_url.rstrip("/")' in src and "self.model = model" in src,
          "self.base_url / self.model")
    check("2b key 在建客户端时就绑进 httpx 的请求头（中途改设置换不掉已建好的客户端）",
          'f"Bearer {api_key}"' in src, "Authorization 头在 __init__ 里")

    print("\n=== 3) 设置写盘不会在项目根留下临时文件 ===")
    tmp = tempfile.mkdtemp(prefix="wolf_midgame_")
    try:
        path = settings_store.save(tmp, data)
        check("3a 落盘成功", os.path.isfile(path), path)
        left = [n for n in os.listdir(tmp) if ".tmp" in n]
        check("3b 没有残留 .tmp 文件", not left, str(left))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for name, _, detail in bad:
        print(f"  [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
