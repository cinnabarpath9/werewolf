# coding=utf-8
"""浏览器内跑一局（Pyodide 用）——「零安装试玩版」。

为什么要这个文件：公共体验实例要么吃作者的免费额度、要么只能一局一房间。
把引擎搬进访客自己的浏览器，就同时满足「零登录 / 多人同时 / 不占别人额度」。
代价是功能要砍：没有语音、没有存档、没有联机，只有「发牌 → 发言 → 投票 → 结算」。

只依赖 Pyodide 提供的两样东西：
  · `js.window.__wolfAsk(json)` —— 把提问交给页面，返回一个 Promise（玩家点按钮后 resolve 一段 JSON）；
  · `pyodide.http.pyfetch`       —— 直接打访客自己填的 OpenAI 兼容端点（跨域由渠道决定）。

⚠️ 这里**不发任何思考参数**（reasoning_effort / thinking）：不知道访客那家认哪套，发错就 400。
"""
from __future__ import annotations

import json

from wolf import llm
from wolf.agents import LLMAgent
from wolf.context import Task
from wolf.engine import Game
from wolf.roles import make_board


class BridgeHuman:
    """把「轮到你」交给页面，等玩家点完再返回。"""

    def __init__(self, seat_id: int) -> None:
        self.seat_id = int(seat_id)

    async def ask(self, packet: str, task: Task) -> dict:
        from js import window

        payload = {
            "seat": self.seat_id,
            "kind": task.kind,
            "text": task.text,
            "schema": task.schema,
            "legal": list(task.legal or []),
            "extra": dict(task.extra or {}),
            "packet": packet,
        }
        raw = await window.__wolfAsk(json.dumps(payload, ensure_ascii=False))
        return json.loads(str(raw or "{}"))


class BrowserClient:
    """最小的 OpenAI 兼容客户端（只要 LLMAgent 用到的那几个成员）。"""

    def __init__(self, base_url: str, model: str, api_key: str,
                 temperature: float = 1.0, max_tokens: int = 6000) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "")
        self.api_key = str(api_key or "")
        self.temperature = temperature
        self.max_tokens = int(max_tokens)
        self.max_retries = 1
        self.last_call = None
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}

    def extra_for(self, task_kind: str, day: int) -> dict:
        return {}

    async def aclose(self) -> None:
        return None

    async def chat_json(self, packet: str, extra: dict | None = None) -> dict:
        from pyodide.http import pyfetch

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": llm.SYSTEM_PROMPT},
                {"role": "user", "content": packet},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        resp = await pyfetch(
            url=self.base_url + "/chat/completions",
            method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status >= 400:
            body = await resp.string()
            raise llm.LLMError(f"HTTP {resp.status}: {body[:200]}", status=resp.status)
        data = await resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {}) or {}
        text = message.get("content") or ""
        if not isinstance(text, str):
            text = str(text)
        self.last_call = {
            "model": self.model, "raw": text, "reasoning": "", "thinking": {},
            "prompt_tokens": int((data.get("usage") or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((data.get("usage") or {}).get("completion_tokens") or 0),
            "cached_tokens": 0, "elapsed": 0.0, "stream": False,
        }
        self.usage["calls"] += 1
        return llm.extract_json(text)


def _factory(cfg: dict):
    from wolf.agents import MockAgent            # 演练模式：规则 Bot，不花钱
    import random as _random

    rng = _random.Random(int(cfg.get("seed") or 7))

    def factory(seat):
        if seat.id == int(cfg.get("human_seat") or 0):
            return BridgeHuman(seat.id)
        if cfg.get("mock"):
            return MockAgent(seat, rng)
        client = BrowserClient(
            base_url=cfg.get("base_url") or "",
            model=cfg.get("model") or "",
            api_key=cfg.get("api_key") or "",
            temperature=float(cfg.get("temperature") or 1.0),
            max_tokens=int(cfg.get("max_tokens") or 6000),
        )
        return LLMAgent(client, seat.id)

    return factory


async def run_game(board: str, human_seat: int, base_url: str, model: str,
                   api_key: str, seed: int = 0, max_days: int = 20,
                   mock: bool = False) -> str:
    """跑一局，返回 winner（good/wolf/draw）。事件通过页面的 __wolfEvent 发出。"""
    from js import window

    cfg = {"human_seat": int(human_seat), "base_url": base_url, "model": model,
           "api_key": api_key, "mock": bool(mock), "seed": int(seed or 7)}

    def on_event(kind: str, **data) -> None:
        entry = data.get("entry")
        payload: dict = {"kind": kind}
        if entry is not None:
            payload["entry"] = {
                "day": getattr(entry, "day", 0),
                "kind": getattr(entry, "kind", ""),
                "seat": getattr(entry, "seat", None),
                "text": getattr(entry, "text", ""),
                "visible_to": list(getattr(entry, "visible_to", ()) or ()),
            }
        for key in ("seat", "task", "winner", "day", "text", "warn"):
            if key in data and key != "entry":
                payload[key] = data[key]
        try:
            window.__wolfEvent(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:                                  # noqa: BLE001
            pass

    # 把「规则/打法/概念」也带上（和真机对局同口径），否则模型等于裸考
    from wolf import tabletalk

    root = "."
    try:
        playbook = tabletalk.load_playbook(root)
        meta = tabletalk.load_meta_book(root, "")
        prompt = tabletalk.player_prompt({}, root)
    except Exception:                                   # noqa: BLE001
        playbook, meta, prompt = "", "", ""

    game = Game(board=make_board(board), human_seat=int(human_seat) or 0,
                agent_factory=_factory(cfg), seed=seed or None,
                max_days=int(max_days), on_event=on_event,
                playbook_text=playbook, meta_text=meta, player_prompt=prompt,
                rules_mode="packet", rules_detail="brief")
    winner = await game.run()
    on_event("end", winner=winner, day=game.day)
    return winner
