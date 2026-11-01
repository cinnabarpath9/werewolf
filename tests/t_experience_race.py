# coding=utf-8
"""挖"经验库写回"：并发写同一个 key、坏文件容错、临时文件残留、换家后的归属。

怀疑点（读代码看出来的）：
  A. 临时文件名只带 pid → **同一进程内两处并发写同一个 key 会撞同一个 tmp 文件**；
  B. 换家之后 `update_all` 用 `agent.client`（当前=替补）当 key →
     经验会记到**替补模型**名下，而实际上一局里绝大多数手都是原模型打的；
  C. 坏文件 / 半截 JSON / 残留 tmp 的容错；
  D. 文件被外部删掉/目录不可写时，不该把整局收尾搞崩。
"""
import asyncio
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from wolf import experience                              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   | " + str(detail)) if detail else ""))


def probe_concurrent_writes() -> None:
    store = tempfile.mkdtemp(prefix="exp_conc_")
    key = "api.52pay.com/deepseek-v4.1-flash"
    errors: list[str] = []
    big = "经验" * 20000                        # 足够大，写一半被打断就能看出来

    def writer(tag: str) -> None:
        for i in range(6):
            try:
                experience.write_note(store, key, {"key": key, "note": f"{tag}-{i}-{big}"})
            except Exception as exc:             # noqa: BLE001
                errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = experience._path(store, key)
    ok_json = False
    note = ""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ok_json = isinstance(data, dict) and isinstance(data.get("note"), str)
        note = str(data.get("note") or "")
    except Exception as exc:                     # noqa: BLE001
        errors.append(f"读回失败: {type(exc).__name__}: {exc}")
    leftovers = [os.path.basename(p) for p in glob.glob(os.path.join(store, "*.tmp"))]
    check("A1 4 线程并发写同一个 key：文件仍是完整合法 JSON", ok_json, "；".join(errors[:3]))
    check("A1b 并发写**一次都没失败**（修之前 4 个线程里 3 个 PermissionError）",
          not errors, "；".join(errors[:3]))
    check("A2 写进去的内容完整（没被截断）", note.endswith("-5-" + big) or len(note) > 40000,
          f"长度 {len(note)}")
    check("A3 没有残留 tmp 文件", not leftovers, str(leftovers[:4]))
    shutil.rmtree(store, ignore_errors=True)


def probe_corrupt_files() -> None:
    store = tempfile.mkdtemp(prefix="exp_bad_")
    key = "api.52pay.com/glm-5.3-flash"
    experience.write_note(store, key, {"key": key, "note": "好笔记"})
    # 塞三个坏文件：半截 JSON、非 dict、空文件
    with open(os.path.join(store, "half.json"), "w", encoding="utf-8") as fh:
        fh.write('{"key": "x", "note": "截断')
    with open(os.path.join(store, "list.json"), "w", encoding="utf-8") as fh:
        fh.write("[1,2,3]")
    open(os.path.join(store, "empty.json"), "w", encoding="utf-8").close()
    # 残留 tmp（模拟上一次进程被杀）
    with open(os.path.join(store, "junk.json.99999.tmp"), "w", encoding="utf-8") as fh:
        fh.write('{"key": "junk", "note": "残留"}')
    notes = experience.load_all(store)
    check("B1 坏文件全被跳过、好笔记仍然读得到", notes.get(key) == "好笔记", str(list(notes))[:120])
    check("B2 残留 tmp 不被当成笔记读进来", "junk" not in "".join(notes.values()),
          str(list(notes))[:120])
    shutil.rmtree(store, ignore_errors=True)


def probe_unwritable_dir() -> None:
    store = os.path.join(tempfile.mkdtemp(prefix="exp_ro_"), "sub")
    # 让目标目录"写不进去"：把父目录做成只读（Windows 上不可靠），改用"父路径是文件"
    parent = os.path.dirname(store)
    with open(parent + "\\blocker", "w", encoding="utf-8") as fh:
        fh.write("x")
    bad_store = parent + "\\blocker\\sub"        # 父路径是文件 → makedirs 必失败
    try:
        experience.write_note(bad_store, "k", {"key": "k", "note": "n"})
        check("C1 写不进去时抛的是可预期的异常（调用方会兜住）", False, "居然成功了")
    except Exception as exc:                     # noqa: BLE001
        check("C1 写不进去时抛可预期异常（上层有 try 兜住）",
              isinstance(exc, OSError), f"{type(exc).__name__}: {exc}")
    check("C2 load_all 对不存在的目录返回空 dict",
          experience.load_all(os.path.join(parent, "nope")) == {})
    shutil.rmtree(parent, ignore_errors=True)


class FakeClient:
    def __init__(self, host, model):
        self.host = host
        self.model = model
        self.key = f"{host}/{model}"
        self.last_call = {}

    def extra_for(self, task_kind, day):
        return {}

    async def chat_json(self, packet, extra=None):
        return {"note": "复盘后的新笔记"}

    async def aclose(self):
        pass


class FakeAgent:
    uses_model = True

    def __init__(self, client, switched_to=""):
        self.client = client
        self.primary = client                     # 原模型（换家后仍然留着引用）
        self.switched_to = switched_to
        self.primary_model = client.model

    async def ask(self, packet, task):
        return {"note": "复盘后的新笔记"}


class FakeSeat:
    def __init__(self, seat_id, agent):
        self.id = seat_id
        self.agent = agent
        self.role_key = "villager"
        self.camp = "good"
        self.alive = True
        self.death_day = 0
        self.transcript = []


class FakeGame:
    def __init__(self, seats):
        self.seats = seats
        self.winner = "good"
        self.transcript = []
        self.day = 1


async def probe_attribution() -> None:
    """换家之后，经验该记在**原模型**名下（一局里绝大多数手是他打的）。"""
    store = tempfile.mkdtemp(prefix="exp_attr_")
    primary = FakeClient("api.52pay.com", "glm-5.3-flash")
    backup = FakeClient("api.52pay.com", "deepseek-v4.1-flash")
    agent = FakeAgent(backup, switched_to="DEEPSEEK")   # 当前 client 是替补
    agent.primary = primary                             # 原模型
    game = FakeGame([FakeSeat(3, agent)])
    updated = await experience.update_all(game, store, old_notes={})
    keys = list(updated)
    check("D1 复盘笔记记在**原模型**名下、不是替补名下",
          keys == [primary.key], f"{keys}（原模型 {primary.key}／替补 {backup.key}）")
    shutil.rmtree(store, ignore_errors=True)


async def main() -> int:
    probe_concurrent_writes()
    probe_corrupt_files()
    probe_unwritable_dir()
    await probe_attribution()

    bad = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 74)
    print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for n, _, d in bad:
        print(f"  [FAIL] {n}  {d}")
    print("=" * 74)
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
