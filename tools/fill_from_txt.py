# coding=utf-8
"""从一份手写的 txt 里读出「平台地址 / 模型名 / key」，写成 .env 里的平台档案，并逐个验证。

关键点：**不猜模型名**。它会问网关的 /v1/models「这把 key 允许用哪些模型」，
你 txt 里写的名字如果不对，会自动改成允许的那个，并明确告诉你改了什么。

txt 怎么写都行，只要满足：
  - 里面有一行 Base URL（http/https 开头）
  - 每个 key（sk- 开头）所在的那一行里，能找到一个模型名

跑法：
    python tools/fill_from_txt.py "C:\\Users\\China\\Desktop\\api.txt" --dry-run
    python tools/fill_from_txt.py "C:\\Users\\China\\Desktop\\api.txt"
    python tools/fill_from_txt.py "C:\\Users\\China\\Desktop\\api.txt" --all-allowed
        # 一把 key 允许好几个模型时，每个都建一个档案（例如 QWEN + QWENMAX）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from server import ENV_PATH, env_profiles, load_env, update_env_file  # noqa: E402
from wolf import routing  # noqa: E402
from wolf.llm import LLMError  # noqa: E402

URL_RE = re.compile(r"https?://[^\s,;，；]+")
KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
MODEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[-.][a-z0-9]+)+\b")
#: 中文/全角字符紧贴英文时会把 \b 吃掉，先换成空格再找模型名
CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+")
NOT_A_MODEL = ("sk-", "http", ".com", ".cn", ".net", ".io", ".org", "www.")

PROBE = ('（连通性测试）【当前任务】确认你能收到请求。\n'
         '【输出格式】只输出一个 JSON 对象：{"ok": true}')


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def parse(path: str) -> tuple[str, list[dict], list[str]]:
    """返回 (base_url, [{profile, model, key, candidates, line}], 备注)。"""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()

    notes: list[str] = []
    urls = URL_RE.findall(raw)
    base_url = urls[0].rstrip("/") if urls else ""
    if len(set(urls)) > 1:
        notes.append(f"文件里有多个地址，用了第一个：{base_url}")
    if not base_url:
        notes.append("没找到 Base URL")

    body = raw
    for url in set(urls):                       # 免得 api.xxx.com 被当成模型名
        body = body.replace(url, " ")

    found: list[dict] = []
    for lineno, line in enumerate(body.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        keys = KEY_RE.findall(line)
        if not keys:
            continue
        models = [m for m in MODEL_RE.findall(CJK_RE.sub(" ", line))
                  if not any(bad in m for bad in NOT_A_MODEL)]
        models = list(dict.fromkeys(models))
        if not models:
            notes.append(f"第 {lineno} 行有 key 但没认出模型名，跳过了：{line[:40]}…")
            continue
        if len(models) > 1:
            notes.append(f"第 {lineno} 行有多个候选模型 {models}，会以网关允许的为准")
        for key in keys:
            found.append({"profile": re.match(r"[a-z]+", models[0]).group().upper(),
                          "model": models[0], "key": key,
                          "candidates": models, "line": lineno})

    merged: dict[str, dict] = {}
    for item in found:
        if item["profile"] in merged:
            notes.append(f"档案 {item['profile']} 出现两次，用最后一条")
        merged[item["profile"]] = item
    return base_url, list(merged.values()), notes


def profile_names(model: str, taken: set[str]) -> str:
    """qwen3.8-max → QWENMAX；glm-5.3-flash → GLM。"""
    letters = re.match(r"[a-z]+", model)
    base = letters.group().upper() if letters else model.upper()
    if base not in taken:
        return base
    tail = re.split(r"[-.]", model)[-1].upper()
    tail = re.sub(r"[^A-Z0-9]", "", tail)
    return (base + tail) if tail and (base + tail) not in taken else f"{base}{abs(hash(model)) % 1000}"


# --------------------------------------------------------------------------- #
# 问网关 + 验证
# --------------------------------------------------------------------------- #

async def list_models(base_url: str, key: str) -> list[str]:
    """OpenAI 兼容的 /v1/models：这把 key 允许用哪些模型。"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(base_url.rstrip("/") + "/models",
                                    headers={"Authorization": f"Bearer {key}"})
            if resp.status_code >= 400:
                return []
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            if isinstance(items, list):
                return [str(m.get("id") if isinstance(m, dict) else m) for m in items]
    except Exception:                              # noqa: BLE001
        return []
    return []


async def probe(base_url: str, model: str, key: str, timeout: float = 45) -> dict:
    spec = routing.SeatLLM(base_url=base_url, api_key=key, model=model, timeout=timeout)
    client = routing.build_client(spec, label=f"{spec.host}/{model}")
    started = time.time()
    try:
        reply = await client.chat_json(PROBE)
        return {"ok": True, "elapsed": round(time.time() - started, 1),
                "usage": client.usage, "sample": str(reply)[:60]}
    except LLMError as exc:
        return {"ok": False, "elapsed": round(time.time() - started, 1), "error": str(exc)[:200]}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "elapsed": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        await client.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser(description="从 txt 填 .env 并验证")
    parser.add_argument("txt")
    parser.add_argument("--dry-run", action="store_true", help="只解析和验证，不写 .env")
    parser.add_argument("--all-allowed", action="store_true",
                        help="一把 key 允许多个模型时，每个都建一个档案")
    args = parser.parse_args()

    if not os.path.exists(args.txt):
        print(f"❌ 找不到文件：{args.txt}")
        return 1

    base_url, items, notes = parse(args.txt)
    print("=" * 78)
    print(f"从 {args.txt} 读到：Base URL = {base_url or '（没找到）'}")
    for item in items:
        print(f"  {item['model']:<26} key=****{item['key'][-4:]}（第 {item['line']} 行）")
    for note in notes:
        print("  · " + note)
    print("=" * 78)

    if not items or not base_url:
        print("❌ 解析不出可用信息")
        return 1

    # 1) 问网关允许哪些模型，并纠正 txt 里写错的名字
    print("\n问网关每把 key 允许用哪些模型：")
    plan: list[dict] = []
    taken: set[str] = set()
    for item in items:
        allowed = await list_models(base_url, item["key"])
        parsed = item["model"]
        if not allowed:
            chosen = [parsed]
            print(f"  ****{item['key'][-4:]}：查不到模型列表，就按你写的 {parsed} 试")
        else:
            print(f"  ****{item['key'][-4:]}：{'、'.join(allowed)}")
            if parsed in allowed:
                chosen = allowed if args.all_allowed else [parsed]
            elif len(allowed) == 1:
                chosen = allowed
                print(f"      ⚠️ 你写的 {parsed} 不在允许列表里，改用 {allowed[0]}")
            else:
                chosen = allowed if args.all_allowed else [allowed[0]]
                print(f"      ⚠️ 你写的 {parsed} 不在允许列表里，改用 {chosen[0]}"
                      f"（其余可选：{'、'.join(allowed[1:])}）")
        for model in chosen:
            name = profile_names(model, taken)
            taken.add(name)
            plan.append({"profile": name, "model": model, "key": item["key"]})

    # 2) 逐个打一发真请求
    # ⚠️ --dry-run 的名义是"只解析和验证，不写 .env"，但以前它照样打真实请求（花钱）。
    # 现在 dry-run 一律跳过探测。
    if args.dry_run:
        print(f"\n（--dry-run：只解析，不发请求；本来要打 {len(plan)} 发真实请求验证）")
        for entry in plan:
            print(f"   {entry['profile']:<10} {entry['model']}")
    else:
        print(f"\n逐个打一发真实请求验证（共 {len(plan)} 发，按平台计费）：")
    for entry in plan if not args.dry_run else []:
        res = await probe(base_url, entry["model"], entry["key"])
        entry.update(res)
        if res["ok"]:
            used = res.get("usage") or {}
            print(f"  ✅ [{entry['profile']:<9}] {entry['model']:<24} 通了"
                  f"（{res['elapsed']}s，输入 {used.get('prompt_tokens', 0)} / "
                  f"输出 {used.get('completion_tokens', 0)} tokens）")
        else:
            print(f"  ❌ [{entry['profile']:<9}] {entry['model']:<24} {res['error']}")

    ok_entries = [e for e in plan if e.get("ok")]
    if not ok_entries:
        print("\n❌ 一个都没通，先不写 .env。把上面的报错发我。")
        return 1

    # 3) 写进 .env
    updates: dict[str, str] = {}
    for entry in ok_entries:
        prefix = entry["profile"]
        updates[f"{prefix}_BASE_URL"] = base_url
        updates[f"{prefix}_MODEL"] = entry["model"]
        updates[f"{prefix}_API_KEY"] = entry["key"]

    if args.dry_run:
        print(f"\n（--dry-run：本应写入 {len(updates)} 个变量，这次没写）")
        return 0

    result = update_env_file(updates)
    load_env(ENV_PATH, force=True)
    print(f"\n已写入 {ENV_PATH}")
    print(f"  覆盖：{result['replaced'] or '（无）'}")
    print(f"  新增：{result['appended'] or '（无）'}")
    print(f"  备份：{ENV_PATH}.bak")
    print("\n现在 .env 里的平台：")
    for p in env_profiles():
        print(f"  {p['profile']:<10} {'✅ 有key' if p['has_key'] else '❌ 没key'}  "
              f"{p['base_url'] or '(未设)'}  ·  {p['model'] or '(未设)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
