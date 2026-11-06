# coding=utf-8
"""发布前闸门：确认打包产物里没有「不该出门的东西」。

为什么要有它
------------
对标某商业 Demo 时发现：它的正式包里躺着 `软件著作权申请资料/`（含业务理解、
代码提取清单）、整套 gdUnit 测试、`addons/`、`.import`/`.remap` —— 既泄密又占体积。
本项目的同类风险是 `.env`、`配对码.txt`、`联机码.txt`、`games/`（真局存档含整局 packet）、
`docs/`、`tests/`、`_junk/`。这些东西留在仓库里是对的，进包是错的。

用法
----
    python tools/check_release.py <目录或 .zip>          # 检查一个准备发出去的包
    python tools/check_release.py <目录> --warn-only     # 只警告不改退出码
    python tools/check_release.py <目录> --no-required   # 不发完整包时跳过必备清单

退出码：0 通过；1 有违规；2 路径不存在。

判据分四层
----------
ERROR（一定不能发布）：.env / 配对码.txt / 联机码.txt / games/ 或 game-*.json / docs/ / tests/ /
        __pycache__ / .git / reports/ / var/ / _junk / 疑似真实密钥
WARN （建议清掉，不拦）：addons/ / *.log / .DS_Store / Thumbs.db
必备（缺了跑不起来）：server.py / run_game.py / config.yaml / web/index.html /
        wolf/__init__.py / content/01_规则书.md
玩家可见文本：web/index.html 的中文串 / web/site.json 里不许出现 packet、frag: 等工程词
        （由 check_player_text.py 扫；见该文件头部对“哪些字段算玩家可见”的界定）
"""
from __future__ import annotations

import os
import re
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

#: 缺了就跑不起来的东西（相对包根）
REQUIRED = [
    "server.py",
    "run_game.py",
    "config.yaml",
    "web/index.html",
    "wolf/__init__.py",
    "content/01_规则书.md",
]

#: 出现即判 ERROR 的路径片段（小写比较，均按 `/` 归一后的相对路径）
#: ⚠️ `.env` 不写在这里：它是**文件名**规则，`.env.example` 反而是该发的模板 ——
#:    见 scan() 里按 basename 判的那一段（写成子串会把模板一起误伤）。
BANNED = [
    ("配对码.txt", "手机配对码（跨重启不变，等于长期凭证）"),
    ("联机码.txt", "联机码（朋友加入房间的凭证，也不该进包）"),
    ("__pycache__/", "Python 字节码缓存"),
    (".pyc", "Python 字节码"),
    (".git/", "git 仓库元数据"),
    ("docs/", "维护文档（交接主文档/历史日志/复核报告）"),
    ("tests/", "回归脚本（含假上游与测试密钥形状）"),
    ("reports/", "考核报告"),
    ("_junk", "历史测试残留"),
    ("var/", "运行期缓存（语音缓存等）"),
    ("games/", "对局存档（含整局 packet 与思考链）"),
]

#: 只警告、不拦的路径片段
WARN = [
    # ⚠️ 2026-10-31：`tools/` 不再整体告警 —— `build_release.py` 已改成白名单（只带玩家会用到的
    #    复盘/自检那几个），再按目录告警只是噪音。真要加工具，去改那边的 `TOOLS_KEEP`。
    ("addons/", "编辑器插件"),
    ("_mock/", "演练局存档"),
    (".log", "日志"),
    (".ds_store", "macOS 垃圾文件"),
    ("thumbs.db", "Windows 缩略图缓存"),
]

#: 文本类扩展名才扫内容
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".cfg", ".ini", ".env",
               ".bat", ".cmd",
            ".sh", ".ps1", ".html", ".js", ".css", ".toml"}

#: 存档文件名形态（`game-2026...json` / `game-*.json` 都要拦）
SAVE_RE = re.compile(r"(^|/)game-[^/]*\.json$", re.I)

#: 作者个人信息 / 本机痕迹：发布包里一律 ERROR（2026-10-31 打包清洗）。
#: 只放**确实属于作者这台机器**的串，不放通用词（否则文档里的示例地址会误报）。
PERSONAL_PATTERNS = [
    (re.compile("tail6" + "fd760", re.I), "作者自己的 tailnet 名（个人信息）"),
    (re.compile(r"home" + r"-pc\.", re.I), "作者的机器名（出现在 tailnet 地址里）"),
    (re.compile(r"C:\\+Users\\+", re.I), "本机绝对路径（C: 盘用户目录）"),
    (re.compile("deepseek" + "-harness", re.I), "作者的开发目录名"),
]

#: 疑似真实密钥（占位符 ${X} / xxx / <your-key> 不会命中：它们不是 24+ 位的字母数字串）
# 2026-11-03 审查 P1-9：规则与清洗端共用一份（tools/release_rules.py）
from release_rules import ALLOWED_DEFAULT_PROFILES, KEY_PATTERNS  # noqa: E402
import check_player_text  # noqa: E402


class Result:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.warns: list[tuple[str, str]] = []

    def err(self, path: str, why: str) -> None:
        self.errors.append((path, why))

    def warn(self, path: str, why: str) -> None:
        self.warns.append((path, why))


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):        # 审查 P2：lstrip("./") 会把 ".env" 削成 "env"
        rel = rel[2:]
    return rel


def list_dir(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(_norm(os.path.relpath(full, root)))
    return sorted(out)


def list_zip(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(_norm(n) for n in zf.namelist() if not n.endswith("/"))


def read_text(root: str, rel: str) -> str:
    full = os.path.join(root, rel)
    try:
        if os.path.getsize(full) > 2_000_000:
            return ""
        with open(full, "rb") as f:
            head = f.read(4096)
        if b"\x00" in head:
            return ""                                   # 二进制，跳过
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def scan(paths: list[str], read=None) -> Result:
    """纯函数式核心：只看相对路径列表（+ 可选的内容读取器），方便单测直接喂假数据。"""
    res = Result()
    low = [(p, p.lower()) for p in paths]

    for p, lp in low:
        base = lp.rsplit("/", 1)[-1]
        # `.env` 是文件名规则：`.env` / `.env.bak*` 一律 ERROR，`.env.example` 是模板、可发。
        if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
            res.err(p, "环境变量文件（里面有平台 key）")
        elif base == ".env.example":
            res.warn(p, "环境变量模板（占位符，可以发）")
        for frag, why in BANNED:
            if frag in lp:
                res.err(p, why)
                break
        if SAVE_RE.search(lp):
            res.err(p, "对局存档（含整局对话与思考链）")
        for frag, why in WARN:
            if frag in lp:
                res.warn(p, why)
                break

    if read is not None:
        for p, lp in low:
            # ⚠️ 2026-11-04 审查 P1：`os.path.splitext(".env.example")` 返回 ('.env', '.example')，
            #    `.example` 不在 TEXT_EXT → 模板里的密钥**整段跳过扫描**。
            #    按"文件名以 .env 开头"兜住。
            _base = os.path.basename(lp)
            if (os.path.splitext(lp)[1].lower() not in TEXT_EXT
                    and not _base.startswith(".env")):
                continue
            text = read(p)
            if not text:
                continue
            for rx, why in KEY_PATTERNS:
                if rx.search(text):
                    res.err(p, why)
                    break
            for rx, why in PERSONAL_PATTERNS:
                if rx.search(text):
                    res.err(p, why)
                    break
    return res


def check_required(paths: list[str]) -> list[str]:
    have = {p.lower() for p in paths}
    missing = [r for r in REQUIRED if r.lower() not in have]
    if not missing:
        return []
    # 允许 zip「多包一层目录」（Release 常见：werewolf-1.0.0/server.py）。
    # 只有**所有**文件都在同一个顶层目录下时才剥掉它，避免把一些散装目录误判成包根。
    tops = {p.split("/", 1)[0] for p in paths if "/" in p}
    if len(tops) == 1:
        prefix = next(iter(tops)) + "/"
        if all(p.startswith(prefix) for p in paths):
            stripped = {p[len(prefix):].lower() for p in paths}
            if all(r.lower() in stripped for r in REQUIRED):
                return []
    return missing


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().split("用法")[0].strip())
        print("用法：python tools/check_release.py <目录或 .zip> [--no-required] [--warn-only]")
        return 2
    target = argv[1]
    no_required = "--no-required" in argv
    warn_only = "--warn-only" in argv
    if not os.path.exists(target):
        print(f"路径不存在：{target}")
        return 2

    is_zip = os.path.isfile(target) and target.lower().endswith(".zip")
    zf = None
    if is_zip:
        zf = zipfile.ZipFile(target)
        paths = sorted(_norm(n) for n in zf.namelist() if not n.endswith("/"))

        def read(rel: str) -> str:
            _b = os.path.basename(rel)
            if os.path.splitext(rel)[1].lower() not in TEXT_EXT and not _b.startswith(".env"):
                return ""
            try:
                info = zf.getinfo(rel)
            except KeyError:
                return ""
            if info.file_size > 2_000_000:
                return ""
            raw = zf.read(rel)
            if b"\x00" in raw[:4096]:
                return ""
            return raw.decode("utf-8", "replace")
    else:
        paths = list_dir(target)
        def read(rel: str) -> str:
            return read_text(target, rel)

    print(f"== 发布检查：{target} ==")
    print(f"   文件数：{len(paths)}" + ("（zip）" if is_zip else ""))
    res = scan(paths, read)
    text_hits = check_player_text.scan(paths, read)
    if zf is not None:
        zf.close()

    missing = [] if no_required else check_required(paths)
    print("\n[1/4] 必备文件")
    if no_required:
        print("  SKIP  --no-required")
    elif missing:
        print("  FAIL  缺：" + "、".join(missing))
    else:
        print(f"  PASS  {len(REQUIRED)} 项都在")

    # ⚠️ 2026-11-04 事故：清洗把 config.yaml 的缩进弄坏，包能打出来但装上新机器起不来。
    #    这里对**打好的包**再验一次：配置必须能解析、阵容必须空。
    print("\n[2/4] config.yaml 能解析")
    cfg_problem = ""
    if no_required:
        print("  SKIP  --no-required")
    else:
        cfg_text = None
        try:
            if is_zip:
                with zipfile.ZipFile(target) as z2:
                    # 只认文件名**完全等于** config.yaml 的条目，并取层级最浅的那份
                    _names = [n for n in z2.namelist()
                              if n.replace("\\", "/").split("/")[-1] == "config.yaml"]
                    name = sorted(_names, key=lambda n: n.count("/"))[0] if _names else None
                    if name:
                        cfg_text = z2.read(name).decode("utf-8", "ignore")
            else:
                cfg_path = os.path.join(target, "config.yaml")
                if os.path.isfile(cfg_path):
                    with open(cfg_path, encoding="utf-8-sig") as fh:
                        cfg_text = fh.read()
        except Exception as exc:                          # noqa: BLE001
            cfg_problem = f"读不出来：{exc}"
        if cfg_text is None:
            cfg_problem = cfg_problem or "包里没有 config.yaml"
        else:
            try:
                import yaml
                parsed = yaml.safe_load(cfg_text) or {}
                _llm = parsed.get("llm") or {}
                seats = _llm.get("seats") or {}
                if seats:
                    cfg_problem = f"阵容没清空：{list(seats)[:5]}"
                prof = str((_llm.get("default") or {}).get("profile") or "").upper()
                if prof and prof not in ALLOWED_DEFAULT_PROFILES:
                    cfg_problem = f"兜底档案 {prof!r} 不在模板集合 {ALLOWED_DEFAULT_PROFILES}"
            except Exception as exc:                      # noqa: BLE001
                cfg_problem = f"YAML 解析失败：{type(exc).__name__}: {exc}"
        print(f"  FAIL  {cfg_problem}" if cfg_problem else "  PASS  可解析、阵容为空")

    print("\n[3/4] 不该带出门的东西")
    if res.errors:
        print(f"  FAIL  {len(res.errors)} 项违规：")
        for p, why in res.errors[:40]:
            print(f"        - {p}    （{why}）")
        if len(res.errors) > 40:
            print(f"        …… 还有 {len(res.errors) - 40} 项")
    else:
        print("  PASS  没有 ERROR 级文件")

    if res.warns:
        print(f"  WARN  {len(res.warns)} 项建议清掉（不拦）：")
        for p, why in res.warns[:10]:
            print(f"        - {p}    （{why}）")

    print("\n[4/4] 玩家可见文本（页面/标题里的工程词）")
    if text_hits:
        print(f"  FAIL  {len(text_hits)} 处：")
        for p, line, term, snippet in text_hits[:20]:
            where = f"{p}:{line}" if line else p
            print(f"        - {where}    [{term}] {snippet}")
        if len(text_hits) > 20:
            print(f"        …… 还有 {len(text_hits) - 20} 处")
    else:
        print("  PASS  没有工程词")

    bad = bool(res.errors) or bool(missing) or bool(cfg_problem) or bool(text_hits)
    print()
    if bad and not warn_only:
        print("结论：不能发布 ❌")
        return 1
    if bad:
        print("结论：有违规，但 --warn-only 放行 ⚠️")
        return 0
    print("结论：可以发布 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
