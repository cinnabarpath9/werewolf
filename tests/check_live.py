import io, json, re, httpx
# ⚠️ 2026-10-09 补：这个脚本以前没有重配 stdout —— Windows 控制台是 GBK，
#    一打 ✅/❌ 就 UnicodeEncodeError 崩掉（假失败）。
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")
tok = ""
import os
for line in io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "配对码.txt"), encoding="utf-8-sig"):
    m = re.search(r"[A-Z0-9]{6}", line)
    if m: tok = m.group(); break
r = httpx.get("http://127.0.0.1:8077/api/status", params={"token": tok}, timeout=10)
j = r.json()
print("用户 8077 服务：HTTP", r.status_code)
print("  运行中:", j.get("running"), " 收尾中:", j.get("cleaning"), " mock:", j.get("mock"), " 板子:", j.get("board"))
print("  （只打印状态字段，不打印身份字段）")

# ⚠️ 2026-10-09 补：以前只 print 状态，没有退出码（假回归门）。
_ok = (r.status_code == 200) and isinstance(j, dict) and ("running" in j)
print("判定：", "✅ 8077 在线且状态可读" if _ok else f"❌ HTTP {r.status_code} / 响应不完整")
raise SystemExit(0 if _ok else 1)
