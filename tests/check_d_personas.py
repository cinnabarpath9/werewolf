# coding=utf-8
"""人设卡自检：`content/personas/` 那 10 张卡能不能被项目自己的加载器零告警读进来。

⚠️ 2026-10-09 改过两处：
  · 源目录以前是**用户 D 盘的绝对路径**（`D:\\新建文件夹\\狼人杀_人设`）→ 换台机器就 SKIP、
    等于没检查；现在读仓库里的 `content/personas/`（用户选的方案 c：卡进仓当素材）。
  · 卡的命名也从"按姓名"改成"按座位号"（`persona.load` 读的就是 `<座位>.md`），
    所以这里不再做"姓名 → 座位"的映射，直接加载。

⚠️ 2026-10-11 加了一整套判据（第一次逐张重写这 10 张卡时立的规矩），因为原来只验"加载零告警"：
  · **结构**：六项卡头（name/age/job/**traits**/background/relations）+ 正文只有
    `## 打法` / `## 说话方式` 两节（性格挪进了卡头的 traits，见 persona.render 的打印顺序）；
  · **排版**：全文**零 ASCII 双引号**（中文只用「」，对齐 `规范.md` §8 —— 打法书早有这道闸门，
    人设卡一直漏着）；
  · **越界**：`persona.lint_persona` 零命中（角色名/身份词/队友/查验…）；
  · **关系网**：按**姓名**互查 —— 每条关系提到的名字都得真有其人，且**默认必须双向**
    （允许单向的写在 `ONE_WAY_OK` 里，并写清为什么）；
  · **音色**：有卡的非真人座位都要在 `config.yaml` 的 `voice.seats` 里配到音色；
  · **软告警**（只打印、不判红）：绝对化/时序词命中 —— 那是 2026-10-07 用户亲自踩过的坑
    （"加了性格/打法之后反而把模型的思路固定死了"），留给人工复核。

判据里"卡"的渲染字数、字段长度都按 `persona.Persona` 的结构量，不另外 parse 一遍。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import yaml                                              # noqa: E402

from wolf import persona                                 # noqa: E402

SRC = os.path.join(ROOT, "content", "personas")

N = 0
BAD = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global N, BAD
    N += 1
    if not ok:
        BAD += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   | {extra}" if extra else ""))


#: 允许**单向**的关系（A 提到 B、B 不提 A）。写在这里的每一条都要有理由 ——
#: 人设里"不熟的人本来就不互相点名"是正常的，但**新加**的单向关系必须先想清楚。
ONE_WAY_OK = {
    (5, 7): "白清弦看得出赵徽柔的天分，赵徽柔不服管 —— 单方面评价",
    (5, 8): "白清弦把赵泠汐当一路人，赵泠汐的关系里写的是姑母/表妹/朋友",
    (7, 3): "赵徽柔记得江离当众下过她的面子，江离那边只提 1/2/4/9",
    (7, 6): "赵徽柔把安迟迟当实习生，安迟迟那边只提 1/2/5/10",
    (10, 5): "沈栖迟觉得有白清弦在她就不慌，白清弦那边只提 1/6/7/8/9",
}

#: 软告警词表（命中只打印，不判红）：绝对化 + 时序硬规定。
#: 依据：2026-10-07 用户实测"加了性格/打法人设之后，反而把模型的思路固定死了"。
SOFT_WORDS = ("从不", "绝不", "永远", "一定", "总是", "总想", "从来不",
              "前半场", "前半局", "前面几轮", "第一个开口", "必然", "永远不")

if not os.path.isdir(SRC):
    print(f"源目录不存在（{SRC}）→ 这台机器上检查不了那 10 张卡，SKIP。")
    raise SystemExit(0)

files = sorted(f for f in os.listdir(SRC) if re.fullmatch(r"\d+\.md", f))
print(f"=== 一、{SRC} ===")
print(f"  座位卡文件：{files}（共 {len(files)} 张）")

ps = persona.load({"game": {"personas_dir": SRC}}, "")
print(f"\n=== 二、加载器结果：{len(ps.texts)}/{len(files)} 张，告警 {len(ps.warnings)} 条 ===")
for w in ps.warnings:
    print("  ⚠️", w)
if not ps.warnings:
    print("  （零告警）")
check("2a 十张卡零告警加载", not ps.warnings and len(ps.texts) >= 10,
      f"告警 {len(ps.warnings)} 条 / 加载 {len(ps.texts)} 张")

print("\n=== 三、每张卡的结构（六项卡头 + 两节正文 + 字数）===")
print(f"  {'座位':<4}{'姓名':<5}{'traits':>7}{'经历':>5}{'关系':>4}{'正文':>5}{'渲染':>6}")
_struct_bad: list[str] = []
for seat in sorted(ps.personas):
    p = ps.personas[seat]
    raw = io.open(os.path.join(SRC, f"{seat}.md"), encoding="utf-8").read()
    heads = re.findall(r"^##\s*(.+)$", raw, re.M)
    problems = []
    if not (p.name and p.age and p.job and p.traits and p.background and p.relations):
        problems.append("卡头有缺项")
    if heads != ["打法", "说话方式"]:
        problems.append(f"正文小节={heads or '无'}")
    if len(ps.texts[seat]) > 700:
        problems.append(f"渲染 {len(ps.texts[seat])} 字 >700")
    if problems:
        _struct_bad.append(f"{seat}号：" + "、".join(problems))
    print(f"  {seat:>2}号 {p.name:<4}{len(p.traits):>7}{len(p.background):>5}"
          f"{len(p.relations):>4}{len(p.style):>5}{len(ps.texts[seat]):>6}"
          + ("   ❌ " + "、".join(problems) if problems else ""))
check("3a 十张卡都是「六项卡头齐 + 正文只有 打法/说话方式 两节 + 渲染 ≤700 字」",
      not _struct_bad, "；".join(_struct_bad))
check("3b traits（性格）都不为空 —— 它已经不该出现在正文里",
      all(p.traits for p in ps.personas.values()))

print("\n=== 四、排版与用词（对齐 规范.md §8）===")
_quote_bad, _lint_bad = [], []
for seat in sorted(ps.personas):
    raw = io.open(os.path.join(SRC, f"{seat}.md"), encoding="utf-8").read()
    if '"' in raw:
        _quote_bad.append(f"{seat}号×{raw.count(chr(34))}")
    if "「" not in raw:
        _quote_bad.append(f"{seat}号 一个「」都没有")
    hits = persona.lint_persona(ps.personas[seat],
                                {s: x.name for s, x in ps.personas.items()})
    if hits:
        _lint_bad.append(f"{seat}号：{hits[:2]}")
check("4a 全文零 ASCII 双引号（中文只用「」）", not _quote_bad, "；".join(_quote_bad))
check("4b lint_persona 零命中（角色名/身份词/队友/查验…）",
      not _lint_bad, "；".join(_lint_bad))

print("\n=== 五、关系网（按**姓名**互查：无孤儿 + 默认双向）===")
names = {p.name: sid for sid, p in ps.personas.items()}
_orphan, _oneway, _graph = [], [], []
for seat in sorted(ps.personas):
    p = ps.personas[seat]
    refs: list[tuple[int, str]] = []
    for rel in p.relations:
        hit = [(sid, nm) for nm, sid in names.items() if nm != p.name and nm in rel]
        if not hit:
            # 没点名任何人：可能是"我是她姐姐留下的孩子"这种没写名字的
            continue
        refs.extend(hit)
    for sid, nm in refs:
        # 对方那张卡里有没有出现本卡的名字？
        other = ps.personas[sid]
        back = any(p.name in r for r in other.relations)
        if not back and (seat, sid) not in ONE_WAY_OK:
            _oneway.append(f"{seat}号({p.name})→{sid}号({nm}) 单向")
    _graph.append(f"{p.name:<4}({seat}号) → "
                  + (", ".join(f"{nm}({sid}号)" for sid, nm in refs) or "（没点名）"))
for line in _graph:
    print("  " + line)
check("5a 关系里点到的名字都真有其人（无孤儿）", not _orphan, "；".join(_orphan))
check("5b 关系默认双向（单向的必须在 ONE_WAY_OK 里写明理由）",
      not _oneway, "；".join(_oneway) if _oneway else f"白名单里 {len(ONE_WAY_OK)} 条单向，其余全部双向")

print("\n=== 六、音色配得上 + 软告警（写死思路的苗头）===")
cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
voice = (cfg.get("voice") or {})
vseats = {int(k) for k in (voice.get("seats") or {})}
human_seat = int(((cfg.get("game") or {}).get("human_seat") or 0))
_missing_voice = [f"{s}号({ps.personas[s].name})" for s in sorted(ps.personas)
                  if s not in vseats and s != human_seat]
check("6a 有卡的非真人座位都配了音色（voice.seats）", not _missing_voice,
      "；".join(_missing_voice) if _missing_voice else f"音色 {len(vseats)} 条，真人座位 {human_seat}")
soft: list[str] = []
for seat in sorted(ps.personas):
    raw = io.open(os.path.join(SRC, f"{seat}.md"), encoding="utf-8").read()
    hit = [w for w in SOFT_WORDS if w in raw]
    if hit:
        soft.append(f"{seat}号{ps.personas[seat].name}：{'、'.join(hit)}")
if soft:
    print("  ⚠️ 软告警（可能压住'按局面调整'，人工看一眼，不判红）：")
    for line in soft:
        print("     · " + line)
else:
    print("  （没有绝对化/时序硬规定的苗头）")

print(f"\n共 {N} 项：通过 {N - BAD}，失败 {BAD}")
sys.exit(1 if BAD else 0)
