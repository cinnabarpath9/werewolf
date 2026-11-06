# coding=utf-8
"""玩家可见文本 lint：防止工程词漏进玩家会看到的页面。

为什么要有
----------
发布包里「有没有夹带开发资料」由 `check_release.py` 管；它管的是**文件级**泄漏。
另一种泄漏是**文本级**的：页面按钮、复盘提示、设置文案里冒出 `packet`、`frag:`、
`engine.py` 这类只有维护者才该看的词。玩家看不懂，也容易把内部实现当成玩法。

范围（先定死，避免误伤安装手册）
--------------------------------
只扫**玩家在游戏里会看到**的三类东西：
  1. `web/index.html` / `web/play.html` 的 HTML 正文，以及引号包起来、且含中文的
     JavaScript 字符串（按钮/提示/复盘/设置文案）；
  2. `web/site.json` 的字符串值（页面标题、描述）。

**不扫** `README.md` / `部署说明.md`：安装手册本来就要出现 `server.py`、
`config.yaml`、`python` 命令，把它们判成泄漏只会制造假红。
**不扫** `content/*.md`：里面混着维护者注记（`frag:`、`engine.py`），
不是每一段都会进玩家面板；要扫得先按 `frag:` 切出真正可见的片段，另做。
也**不扫** `.py` 源码和代码注释：维护者需要这些词。

用法
----
    python tools/check_player_text.py              # 扫源码里的两个页面文件
    python tools/check_player_text.py <目录或.zip>  # 扫打好的发布包
    python tools/check_player_text.py --warn-only  # 只警告，不改退出码

退出码：0 通过；1 有命中；2 路径不存在。
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

#: 词 -> 为什么不该让玩家看到。
#: 只放**维护者内部叫法**，不放 API key、token、温度这类玩家需要认识的词。
TERMS = {
    "frag:": "内部片段标记",
    "packet": "内部叫法“信息包/packet”",
    "transcript": "内部叫法“调用留痕”",
    "ledger": "内部叫法“台账”",
    "state_version": "内部作废机制",
    "supersede": "内部作废接口",
    "prefetch": "内部预取机制",
    "engine.py": "引擎文件名",
    "server.py": "服务端文件名",
    "context.py": "上下文模块文件名",
    "ledger.py": "台账模块文件名",
    "llm.py": "模型客户端文件名",
    "routing.py": "路由模块文件名",
    "pytest": "测试框架名",
    "fixture": "测试夹具名",
    "mock": "内部叫法“演练/mock”",
    "raw": "接口原始字段名",
    "reply": "接口原始字段名",
}

HTML_NAMES = {"web/index.html", "web/play.html"}
JSON_NAMES = {"web/site.json"}


def _strip_js_comments(src: str) -> str:
    """去掉 JS/HTML 注释，保留字符串内容。

    只做词法级处理，不追求完整 JS 语法；目的是别把注释里的 `engine.py` 误报成
    玩家可见文本。字符串里的 `//`（例如 URL）不会被截断。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote = ""
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            # ⚠️ 块注释里的换行必须**原样补回**：否则注释后面的行号整体前移，
            #    报错会指到错的位置（旧 1.2.34 包实测差 35 行）。
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _quoted_values(text: str):
    """按 JS 引号切字符串；反引号允许跨行，单/双引号必须同一行闭合。

    产出 `(起始行号, 字符串内容)`。跨行模板必须整体取，不能按行取 —— 否则
    `packet` 落在模板第二行时会漏扫（发版文本闸门的假阴性）。
    """
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch not in "'\"`":
            i += 1
            continue
        quote = ch
        start = line
        i += 1
        buf: list[str] = []
        closed = False
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "\n":
                    line += 1
                    i += 2
                    continue
                buf.append(nxt)
                i += 2
                continue
            if c == "\n":
                if quote != "`":       # 单/双引号字符串不能跨行
                    break
                line += 1
                buf.append(c)
                i += 1
                continue
            if c == quote:
                closed = True
                i += 1
                break
            buf.append(c)
            i += 1
        if closed:
            value = "".join(buf)
            value = re.sub(r"\$\{[^}]*\}", "", value, flags=re.S)
            yield start, value
        # 未闭合：只跳过这个开引号，继续扫后面的文本，避免把整个文件吞掉。


def _strip_html_comments(src: str) -> str:
    # 同样保留换行：HTML 注释后面的行号不能前移。
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"),
                  src, flags=re.S)


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _term_in(value: str, term: str) -> bool:
    """英文词按整词匹配，避免 `isMock`/`draw` 这种误报；带符号的词按子串。"""
    if re.fullmatch(r"[a-z_]+", term):
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) +
                              r"(?![A-Za-z0-9_])", value, re.I))
    return term.lower() in value.lower()


class _HTMLText(HTMLParser):
    """收集 HTML 里**直接显示给玩家**的文本节点（跳过 script/style）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append((self.getpos()[0], data.strip()))


def _scan_html(text: str, rel: str) -> list[tuple[str, int, str, str]]:
    clean = _strip_html_comments(_strip_js_comments(text))
    hits: list[tuple[str, int, str, str]] = []
    # JS 字符串：按钮/提示/模板文案都在这里，按引号整体切（可跨行）。
    for lineno, value in _quoted_values(clean):
        if not _has_cjk(value):
            continue
        for term in TERMS:
            if _term_in(value, term):
                hits.append((rel, lineno, term, value[:100].replace("\n", " ")))
    # HTML 正文：`<p>玩家看到 packet</p>` 不在引号里，单靠上面的扫描会漏。
    page = _HTMLText()
    try:
        page.feed(text)
    except Exception:                         # noqa: BLE001
        pass
    for lineno, value in page.parts:
        for term in TERMS:
            if _term_in(value, term):
                hits.append((rel, lineno, term, value[:100]))
    return hits


def _scan_json(text: str, rel: str) -> list[tuple[str, int, str, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    hits: list[tuple[str, int, str, str]] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and _has_cjk(node):
            for term in TERMS:
                if _term_in(node, term):
                    hits.append((rel, 0, term, f"{path}: {node[:80]}"))

    walk(data)
    return hits


def _match_kind(rel: str) -> str:
    low = rel.replace("\\", "/").lower()

    def is_one(name: str) -> bool:
        return low == name or low.endswith("/" + name)

    if any(is_one(n) for n in HTML_NAMES):
        return "html"
    if any(is_one(n) for n in JSON_NAMES):
        return "json"
    return ""


def scan(paths: list[str], read) -> list[tuple[str, int, str, str]]:
    """纯函数式入口：paths 是相对路径列表，read(rel) 返回文本。"""
    hits: list[tuple[str, int, str, str]] = []
    for p in paths:
        kind = _match_kind(p)
        if not kind:
            continue
        text = read(p)
        if not text:
            continue
        if kind == "html":
            hits.extend(_scan_html(text, p))
        else:
            hits.extend(_scan_json(text, p))
    return hits


def source_paths() -> list[str]:
    return [p for p in ("web/index.html", "web/play.html", "web/site.json")
            if os.path.isfile(os.path.join(BASE, p))]


def list_dir(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(out)


def _read_dir(root: str, rel: str) -> str:
    full = os.path.join(root, rel)
    try:
        if os.path.getsize(full) > 2_000_000:
            return ""
        with open(full, "rb") as fh:
            head = fh.read(4096)
        if b"\x00" in head:
            return ""
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_zip(zf: zipfile.ZipFile, rel: str) -> str:
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


def main(argv: list[str]) -> int:
    warn_only = "--warn-only" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    target = args[0] if args else BASE
    if not os.path.exists(target):
        print(f"路径不存在：{target}")
        return 2

    if os.path.isfile(target) and target.lower().endswith(".zip"):
        with zipfile.ZipFile(target) as zf:
            paths = sorted(n for n in zf.namelist() if not n.endswith("/"))
            hits = scan(paths, lambda rel: _read_zip(zf, rel))
    else:
        paths = source_paths() if not args else list_dir(target)
        hits = scan(paths, lambda rel: _read_dir(target, rel))

    if hits:
        print(f"玩家可见文本：命中 {len(hits)} 处")
        for rel, line, term, snippet in hits[:40]:
            where = f"{rel}:{line}" if line else rel
            print(f"  - {where}  [{term}]  {snippet}")
        if len(hits) > 40:
            print(f"  …… 还有 {len(hits) - 40} 处")
        if warn_only:
            print("结论：有命中，但 --warn-only 放行 ⚠️")
            return 0
        print("结论：有工程词漏进玩家可见文本 ❌")
        return 1
    print("玩家可见文本：没有工程词 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))