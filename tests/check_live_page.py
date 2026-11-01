import httpx, hashlib, io, re
# ⚠️ 2026-10-09 补：这个脚本以前没有重配 stdout —— Windows 控制台是 GBK，
#    一打 ✅/❌ 就 UnicodeEncodeError 崩掉（假失败）。
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")
html = httpx.get("http://127.0.0.1:8077/", timeout=10).text
import os
disk = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html"), encoding="utf-8").read()
print("在线页面 sha256:", hashlib.sha256(html.encode("utf-8")).hexdigest()[:16])
print("磁盘页面 sha256:", hashlib.sha256(disk.encode("utf-8")).hexdigest()[:16])
print("一致:", hashlib.sha256(html.encode("utf-8")).hexdigest() == hashlib.sha256(disk.encode("utf-8")).hexdigest())
for mark in ("本局正在收尾", "ASR_TIMEOUT_MS", "revokeObjectURL", "cleaning", "RECONNECT_MAX"):
    print(f"  在线页面含 {mark!r}: {mark in html}")

# ⚠️ 2026-10-09 补：以前只 print 两个 sha，没有退出码（假回归门）。
_marks = ("本局正在收尾", "ASR_TIMEOUT_MS", "revokeObjectURL", "cleaning", "RECONNECT_MAX")
_missing = [m for m in _marks if m not in html]
# ⚠️ 2026-10-09 修：以前直接比两份文本的 sha256 —— 但**磁盘那份是用文本模式读的**
#    （Python 会把 CRLF 归一成 LF），而线上拿到的是服务端原样发的字节。
#    实测内容 diff = 0 行、长度却差 2685 字节（≈行数）→ 这条判据**天生不可能通过**，
#    这正是它当初不敢设退出码的原因。现在两边都先归一化行尾再比。
def _norm(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n")
_same = hashlib.sha256(_norm(html).encode("utf-8")).hexdigest() == \
        hashlib.sha256(_norm(disk).encode("utf-8")).hexdigest()
print()
print("判定：", "✅ 线上页面与磁盘一致、标记齐全" if (_same and not _missing)
      else f"❌ 页面不一致={not _same} 缺标记={_missing}")
raise SystemExit(0 if (_same and not _missing) else 1)
