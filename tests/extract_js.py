import io, re, sys
import os
src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html"), encoding="utf-8").read()
scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
print("内联 script 段数:", len(scripts), "总字符:", sum(len(s) for s in scripts))
# ⚠️ 路径必须**跟着脚本走**，不能跟着当前目录走：
#    以前是 io.open("index_inline.js", ...) —— 从仓库根跑生成一份、从 tests/ 跑又生成一份，
#    于是同一个生成物在仓库里存了两份（各 135 KB）而且都被跟踪。
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_inline.js")
io.open(OUT, "w", encoding="utf-8").write("\n;\n".join(scripts))
