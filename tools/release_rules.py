# coding=utf-8
"""发布/镜像共用的"清洗与检查"规则（2026-11-03 从 build_release / check_release 抽出）。

为什么要有这个文件：审查发现三套"分发定义"（发布包白名单、Docker 镜像 `COPY . .`、Render 蓝本）
各自决定带什么，于是镜像里带着作者的个人阵容、首局必失败。现在：
  · 清洗规则只有这一份，`build_release.py` 与 `Dockerfile` 都调它；
  · 清洗完**必须断言**（`assert_packaged`）：阵容为空、兜底档案在模板集合里；
    不满足直接抛异常 —— 以前正则没匹配到是静默的，那才是最危险的情况。

⚠️ 这里对 `config.yaml` 做的是"结构化改写"（yaml → 改 dict → dump 回文本），
   不再靠"4 空格缩进 + 某句注释"的锚点。注释会丢是代价，换来的是**改不坏**。
"""
from __future__ import annotations

import re

#: 发布包里允许出现的兜底档案（都必须在 `.env.example` 里有对应三行）
ALLOWED_DEFAULT_PROFILES = ("DEEPSEEK", "QWEN", "GLM", "ARK", "HAIKU", "MIMO")

#: 文本级清洗（仍然保留，处理那些不适合结构化改写的项）
SANITIZE_TEXT: dict[str, list[tuple[str, str]]] = {
    "config.yaml": [
        # 作者自己的 https 提示地址（Tailscale 域名），别人装上不该看到
        (r"(?m)^\s*public_https_base:.*$",
         'public_https_base: ""          # 留空即可；要提示条就写 .env 的 WOLF_PUBLIC_HTTPS_BASE'),
    ],
}

#: 硬编码 key 的识别（与 check_release 共用）
KEY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "疑似 OpenAI 风格 key（sk-…）"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "疑似 GitHub token（ghp_…）"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "疑似 AWS key（AKIA…）"),
    # 带引号的字面量：`api_key = "…"`
    (re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*"
                r"['\"][A-Za-z0-9_\-\.\+=/]{20,}['\"]"), "疑似硬编码的 key/token（带引号）"),
    # ⚠️ 2026-11-03 审查 P1-9：YAML 里合法的无引号写法以前完全漏检
    (re.compile(r"(?im)^\s*(?:[A-Z0-9_]+_)?(api[_-]?key|secret[_-]?key|access[_-]?token|password)\s*:\s*"
                r"[A-Za-z0-9_\-\.\+=/]{20,}\s*$"), "疑似硬编码的 key/token（无引号 YAML）"),
    # `FOO_API_KEY = "..."`（带下划线前缀的常量名，`\b` 边界抓不到）
    (re.compile(r"(?im)^\s*[A-Z0-9_]*(?:API_KEY|SECRET_KEY|ACCESS_TOKEN|PASSWORD)\s*=\s*"
                r"['\"][A-Za-z0-9_\-\.\+=/]{20,}['\"]"), "疑似硬编码的 key/token（常量赋值）"),
)


def _load_yaml(text: str):
    import yaml                                        # 延迟导入：只有打包/镜像需要

    return yaml.safe_load(text) or {}


def sanitize_config(text: str) -> tuple[str, dict]:
    """把作者的个人阵容从 config 里摘掉，返回 (新文本, 打包后用到的配置字典)。

    做三件事：
      1. `llm.seats` 清空（11 个座位全部跟随兜底）；
      2. `llm.default.profile` 改成模板档案（DEEPSEEK，`.env.example` 里有）；
      3. 其余文本级清洗（public_https_base）。
    """
    cfg = _load_yaml(text)
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    if llm.get("seats"):
        llm["seats"] = {}
    default = llm.get("default") if isinstance(llm.get("default"), dict) else {}
    profile = str(default.get("profile") or "").upper()
    if profile not in ALLOWED_DEFAULT_PROFILES:
        default["profile"] = "DEEPSEEK"
        llm["default"] = default
    cfg["llm"] = llm

    dumped = __import__("yaml").safe_dump(cfg, allow_unicode=True, sort_keys=False)
    # 结构化 dump 之后再做文本级清洗（public_https_base 那类）
    for pattern, repl in SANITIZE_TEXT.get("config.yaml", []):
        dumped = re.sub(pattern, repl, dumped)
    return dumped, cfg


def assert_packaged(cfg: dict) -> None:
    """打包/镜像后的硬检查：阵容必须是空的、兜底必须是模板档案。

    为什么用断言而不是"尽量清洗"：正则漏匹配是静默的，而阵容泄漏是真实事故。
    """
    llm = (cfg or {}).get("llm") or {}
    seats = llm.get("seats")
    if seats:
        raise SystemExit(f"发布配置里仍有作者阵容：llm.seats={list(seats)[:5]}…（清洗没生效）")
    profile = str((llm.get("default") or {}).get("profile") or "").upper()
    if profile not in ALLOWED_DEFAULT_PROFILES:
        raise SystemExit(f"发布配置的兜底档案是 {profile!r}，不在模板集合 "
                         f"{ALLOWED_DEFAULT_PROFILES} 里（.env.example 没有它）")


def sanitize_file(path: str) -> dict:
    """就地清洗一个 config.yaml 文件（Dockerfile 构建期用）。返回打包后的配置字典。"""
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    new_text, cfg = sanitize_config(text)
    assert_packaged(cfg)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    return cfg