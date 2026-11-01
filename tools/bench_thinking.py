# coding=utf-8
"""开思考到底能不能提升智力？用可自动判分的题来测。

做法：
1. 读一局**真实打过的对局存档**，从台账里**用代码生成**一批有唯一正确答案的问题
   （答案由程序算出来，不是我手写的，所以判分是客观的）
2. 把「公开记录 + 这批问题」原样发给同一个模型的几种思考设置
3. 自动判分，比准确率 / 耗时 / token

考的是"在长文本里精确找事实并算清楚"，这正好是狼人杀最吃的能力。

跑法：
    python tools/bench_thinking.py                 # 用最近一局存档
    python tools/bench_thinking.py --repeat 2      # 每种设置跑两遍
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import io
import json
import os
import re
import sys
import time

for _stream in (sys.stdout, sys.stderr):          # GBK 控制台友好
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from wolf.llm import extract_json  # noqa: E402 —— 复用同一个（已修好的）JSON 解析器

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VARIANTS = [
    ("关思考", {"enable_thinking": False}),
    ("低思考", {"reasoning_effort": "low"}),
    ("开思考", {"enable_thinking": True}),
    ("最高思考", {"reasoning_effort": "high"}),
]

QUESTIONS_JSON = ('{"answers": [{"q": 1, "a": "<答案>"}, {"q": 2, "a": "<答案>"}, ...]}')

#: 这些前缀的"发言"不算白天轮流发言那一轮：PK 辩护 / 遗言 / 警长竞选发言 / 竞选 PK 发言。
# 引擎是拿 kind='speech' + 前缀来区分它们的（见 wolf/engine.py），所以出题时也必须同步排除，
# 否则题面说"几个玩家发言"、判分却按排除后的口径算，答对的会被判错。
SPEECH_SKIP_PREFIXES = ("【PK】", "【遗言】", "【竞选】", "【竞选PK】")


# --------------------------------------------------------------------------- #
# 从存档里生成有唯一答案的题
# --------------------------------------------------------------------------- #

def build_qa(game: dict) -> tuple[str, list[dict]]:
    public = [e for e in game["ledger"] if "public" in (e.get("visible_to") or [])]
    days = sorted({e["day"] for e in public if e["day"] > 0})
    votes = [e for e in public if e["kind"] == "vote"]
    speeches = [e for e in public if e["kind"] == "speech"]
    claims = [e for e in public if e["kind"] == "claim"]

    qa: list[dict] = []

    def add(q: str, a) -> None:
        if a is not None and a != "":
            qa.append({"q": q, "a": str(a)})

    def votes_on(day: int) -> list[dict]:
        """这一天的**决定性那一轮**的票。

        平票会进 PK 再投一轮：两轮相加会把票数算多（实测：第一轮 3号得 1 票、
        PK 轮又得 3 票，公告说"3号 得 3 票" = PK 轮的票数才是决定放逐的那个数），
        所以这里只取当天的最后一轮。

        ⚠️ 还要排除**警长竞选的票**（`data.sheriff_vote`）：它们也是 kind=vote 的条目、
        day 也相等，混进来就会出现"第 1 天 2 号拿到 7 票"这种假真值
        （实际放逐票只有 1 票，另外 6 票是竞选票）。exam_bank 里早就记过这个坑，这里补上。
        """
        today = [e for e in votes
                 if e["day"] == day and not (e.get("data") or {}).get("sheriff_vote")]
        if not today:
            return []
        last = max((e.get("data") or {}).get("round", 1) for e in today)
        return [e for e in today if (e.get("data") or {}).get("round", 1) == last]

    def speeches_on(day: int) -> list[dict]:
        # PK 轮的辩护发言不算"这一天的常规发言"（引擎给它们加了【PK】前缀）；
        # 遗言（【遗言】）和警长竞选发言（【竞选】）同样不算 —— 它们不是"白天轮流发言"那一轮。
        return [e for e in speeches
                if e["day"] == day
                and not (e.get("text") or "").startswith(SPEECH_SKIP_PREFIXES)]

    # 1) 某天有几票投给某人
    for day in days:
        tally: dict[int, int] = {}
        for e in votes_on(day):
            target = (e.get("data") or {}).get("target")
            if target:
                tally[target] = tally.get(target, 0) + 1
        if tally:
            top = max(tally.items(), key=lambda kv: kv[1])
            add(f"第 {day} 天最后一轮投票里，{top[0]} 号一共拿到几票？请只回答数字。", top[1])

    # 2) 某人某天投给了谁（同样只看决定性那一轮，免得 PK 两轮的票混在一起）
    for day in days[:2]:
        for e in votes_on(day)[:3]:
            target = (e.get("data") or {}).get("target")
            if target:
                add(f"第 {day} 天最后一轮投票时，{e['seat']} 号把票投给了几号？请只回答数字。", target)

    # 3) 一共几个人声称自己是预言家
    # ⚠️ 题目问的是"几个**玩家**"，所以必须去重：
    # 同一个人第 1、2 天各跳一次预言家只能算 1 个（原来直接 len() 数的是条目数，会把答对的判错）
    seer_claims = {e["seat"] for e in claims if "预言家" in e["text"]}
    add("整局里，一共有几个玩家声称自己是「预言家」？请只回答数字。", len(seer_claims))

    # 4) 某天几个人发言
    # ⚠️ 题面必须和判分口径一致：`speeches_on()` 把【PK】辩护、【遗言】、【竞选】都排除在外，
    # 而题面原来只说"一共有几个玩家发言" —— 模型把它们数进去就被判错（答对了判错）。
    # 这里选"明确写清不含哪些"（另一种改法是把它们也算进去，两边只要一致就行）。
    for day in days:
        n = len(speeches_on(day))
        if n:
            add(f"第 {day} 天白天，一共有几个玩家发言？"
                f"（不含 PK 轮辩护、遗言和警长竞选发言）请只回答数字。", n)

    # 5) 某天第一个发言的是谁
    for day in days:
        first = speeches_on(day)
        if first:
            add(f"第 {day} 天白天，第一个发言的是几号？请只回答数字。", first[0]["seat"])

    # 6) 某人总共发过几次言（同样只算"白天轮流发言"那一轮）
    regular = [e for e in speeches
               if not (e.get("text") or "").startswith(SPEECH_SKIP_PREFIXES)]
    counter: dict[int, int] = {}
    for e in regular:
        counter[e["seat"]] = counter.get(e["seat"], 0) + 1
    if counter:
        who = max(counter.items(), key=lambda kv: kv[1])
        add(f"整局里，{who[0]} 号一共发过几次言？"
            f"（不含 PK 轮辩护、遗言和警长竞选发言）请只回答数字。", who[1])

    # 7) 一共有几天（用存档自己的 day，而不是"台账里出现过的最大天数"）
    add("这局一共进行了几天？请只回答数字。", int(game.get("day") or (max(days) if days else 0)))

    # 8) 某人声称的身份（真值要归一成身份名：模型可能写"预言家，我第一晚验了3号"这种长句子）
    if claims:
        pick = claims[0]
        role = re.search(r"【(.+?)】", pick["text"])
        if role:
            raw = role.group(1)
            name = next((r for r in ("狼人", "预言家", "女巫", "摄梦人", "愚者", "守墓人",
                                "养鸦人", "幸运儿", "猎人", "守卫", "村民") if r in raw), None)
            if name:
                add(f"{pick['seat']} 号声称自己是什么身份？请只回答身份名。", name)

    transcript = "\n".join(
        (f"[第{e['day']}天] " + (f"{e['seat']}号：" if e.get("seat") else "公告：") + e["text"])
        for e in public if e["day"] > 0
    )
    return transcript, qa[:10]


# --------------------------------------------------------------------------- #
# 调用 + 判分
# --------------------------------------------------------------------------- #

async def ask(base_url: str, key: str, model: str, extra: dict, prompt: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你是狼人杀复盘助手。只根据给你的公开记录回答，只输出一个 JSON 对象。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # ⚠️ 不能给小值：思考档位越高、思考链越长，4000 会被思考链吃光 → 正文空 →
        #    这一档被误判成"最差"（exam.py / config.yaml 里都记过这个坑）。
        "max_tokens": 16000,
    }
    payload.update(extra)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(base_url.rstrip("/") + "/chat/completions",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"}, json=payload)
            elapsed = time.time() - started
            if resp.status_code >= 400:
                return {"ok": False, "elapsed": round(elapsed, 1), "error": resp.text[:150]}
            data = resp.json()
            used = data.get("usage") or {}
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return {"ok": True, "elapsed": round(elapsed, 1), "text": text,
                    "prompt": int(used.get("prompt_tokens") or 0),
                    "completion": int(used.get("completion_tokens") or 0)}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "elapsed": round(time.time() - started, 1),
                "error": f"{type(exc).__name__}: {exc}"[:150]}


def parse_answers(text: str) -> dict[int, str]:
    """从回复里抠出 {题号: 答案}。"""
    out: dict[int, str] = {}
    try:
        # 复用已经修好的解析器：原来的贪婪正则 r"\{.*\}" 会把 JSON 后面的
        # 任何一句话/花括号一起吃进去，导致 json.loads 失败、整份判 0 分
        data = extract_json(text)
    except Exception:                          # noqa: BLE001
        data = {}
    if isinstance(data, dict):
        for item in (data.get("answers") or []):
            try:
                out[int(item.get("q"))] = str(item.get("a", "")).strip()
            except (TypeError, ValueError):
                continue
        if out:
            return out
    for q, a in re.findall(r'"?q"?\s*[:：]\s*(\d+)\D+?"?a"?\s*[:：]\s*"([^"]*)"', text):
        out[int(q)] = a.strip()
    return out


CN_NUM = {"零": "0", "一": "1", "两": "2", "二": "2", "三": "3", "四": "4",
          "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def norm(value: str) -> str:
    """归一化：阿拉伯数字优先，其次把汉字数字翻成数字，最后只留汉字。

    以前"期望 3、答三号"会被判错（汉字数字没翻），而那其实是答对了。
    """
    value = str(value).strip()
    digits = re.findall(r"\d+", value)
    if digits:
        return digits[0]
    for cn, num in CN_NUM.items():
        if cn in value:
            return num
    return re.sub(r"[^\u4e00-\u9fff]", "", value)


def score(qa: list[dict], answers: dict[int, str]) -> tuple[int, list[str]]:
    right, wrong = 0, []
    for i, item in enumerate(qa, 1):
        got = answers.get(i, "")
        if norm(got) and norm(got) == norm(item["a"]):
            right += 1
        else:
            wrong.append(f"第{i}题 期望「{item['a']}」得到「{got or '（没答）'}」")
    return right, wrong


async def main() -> int:
    parser = argparse.ArgumentParser(description="测开思考能不能提升智力")
    parser.add_argument("--game", default=None, help="存档路径；默认用最新一局")
    parser.add_argument("--profile", default="QWEN")
    # ⚠️ 型号名**不要写死**：留空就用 .env 里的 <PROFILE>_MODEL。
    #    以前默认写死 "qwen3.8-flash" —— 谁要是换了 --profile（例如 DEEPSEEK，走火山方舟）
    #    却忘了给 --model，就会把 qwen 的型号名发到方舟的端点上（必然模型不存在）。
    parser.add_argument("--model", default="",
                        help="留空就用 .env 里 <PROFILE>_MODEL（推荐）")
    parser.add_argument("--repeat", type=int, default=2, help="每种设置跑几遍")
    args = parser.parse_args()

    # 读 .env
    env = {}
    for line in io.open(os.path.join(HERE, ".env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                env[k.strip()] = v.strip()

    if args.game:
        path = args.game
    else:
        # ⚠️ 以前是 sorted(...)[-1]，games/ 为空（或还没打过真局）时直接 IndexError，
        # 只留一个 Python 堆栈，看不出该怎么办。
        found = sorted(glob.glob(os.path.join(HERE, "games", "game-*.json")))
        if not found:
            print(f"❌ 找不到存档：{os.path.join(HERE, 'games')} 里没有 game-*.json。")
            print("   这个基准要拿**真模型打过的对局存档**出题；先打一局，"
                  "或者用 --game <存档路径> 指定一个。")
            return 1
        path = found[-1]
    if not os.path.exists(path):
        print(f"❌ 存档不存在：{path}")
        return 1
    try:
        game = json.load(io.open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"❌ 读不了这个存档：{path}（{type(exc).__name__}: {exc}）")
        return 1
    transcript, qa = build_qa(game)
    if not qa:
        print("❌ 这局生成不出可判分的题，换一局（需要真模型打的局，有完整台账）")
        return 1

    print("=" * 78)
    print(f"存档：{os.path.basename(path)}　（{game['day']} 天，"
          f"{len(game.get('transcript') or [])} 次真模型调用）")
    print(f"自动生成 {len(qa)} 道有唯一答案的题；公开记录 {len(transcript)} 字")
    print("=" * 78)
    for i, item in enumerate(qa, 1):
        print(f"  {i}. {item['q']}　→ 答案 {item['a']}")

    prompt = (
        "下面是一局狼人杀的完整公开记录（谁说了什么、谁投了谁）。\n"
        "请根据它回答最后的问题，**只输出 JSON**，格式：\n"
        f"{QUESTIONS_JSON}\n\n"
        "———— 公开记录 ————\n" + transcript + "\n\n"
        "———— 问题 ————\n"
        + "\n".join(f"{i}. {item['q']}" for i, item in enumerate(qa, 1))
    )

    base_url = env.get(f"{args.profile}_BASE_URL", "")
    key = env.get(f"{args.profile}_API_KEY", "")
    if not (base_url and key):
        print(f"❌ .env 里 {args.profile} 没配好")
        return 1
    # 型号名以 .env 为权威（--model 只是可选覆盖），避免"profile 换了、型号名没换"
    model_now = args.model or env.get(f"{args.profile}_MODEL", "")
    if not model_now:
        print(f"❌ {args.profile} 没有型号名：--model 没给，.env 里也没有 {args.profile}_MODEL")
        return 2
    print(f"▸ 平台 {args.profile}｜型号 {model_now}｜端点 {base_url}")

    results = []
    for label, extra in VARIANTS:
        for run in range(args.repeat):
            res = await ask(base_url, key, model_now, extra, prompt)
            if not res["ok"]:
                print(f"\n▸ {label} #{run + 1} ❌ {res['error']}")
                continue
            answers = parse_answers(res["text"])
            right, wrong = score(qa, answers)
            results.append({"label": label, **res, "right": right, "wrong": wrong})
            print(f"\n▸ {label} #{run + 1}：答对 {right}/{len(qa)}　"
                  f"{res['elapsed']}s　输入 {res['prompt']} / 输出 {res['completion']} tokens")
            for line in wrong[:3]:
                print("     ✗ " + line)

    print("\n" + "=" * 78)
    print(f"{'设置':<10}{'平均正确率':>12}{'平均耗时':>10}{'平均输出':>10}")
    for label, _ in VARIANTS:
        rows = [r for r in results if r["label"] == label]
        if not rows:
            continue
        acc = sum(r["right"] for r in rows) / len(rows) / len(qa)
        el = sum(r["elapsed"] for r in rows) / len(rows)
        out = sum(r["completion"] for r in rows) / len(rows)
        print(f"{label:<10}{acc:>11.0%}{el:>9.0f}s{out:>10.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
