# tools/（只放玩家/部署者会用到的脚本）

> 2026-11-06：公开仓库只保留一个**模型考试**入口（`exam_paper.py` + `exam_papers.json`）。
> 更早的 134 题/高难题库/参数扫描那套开发工具只在作者本机，不进仓库也不进发布包。

## 打包与发布

| 脚本 | 用途 | 什么时候用 |
|---|---|---|
| `build_release.py` | 组装运行包（`--flavor desktop/android --zip`） | 发版 |
| `check_release.py` | 发布包自检：必备文件 / 密钥扫描 / `config.yaml` 能否解析 | 发版前、打包后 |
| `release_rules.py` | 清洗规则（阵容清空 / 兜底档案 / 个人串），打包与镜像共用 | 被上面两个调用 |
| `build_exe.py` | Windows 客户端（PyInstaller onedir）+ itch zip | 发客户端 |
| `build_play_site.py` | 静态试玩站（`dist-play/` + `werewolf-<版本>-在线试玩站.zip`） | 发 Pages/静态托管 |
| `make_narrator.py` | 用 `VOICE_*` 一次性生成 8 条法官播报 mp3 | 换播报台词 |

## 模型考试（三套卷）

| 文件 | 用途 |
|---|---|
| `exam_paper.py` | 考试脚本：`--paper basic\|strength\|full`，输出 Markdown + raw.json |
| `exam_papers.json` | 题库：基础 30 + 实力 30（全量 60），题目/选项/答案/解析都在这里 |

```powershell
python tools/exam_paper.py --selftest                 # 离线自检（不联网）
python tools/exam_paper.py --list                     # 看三套卷

# 模型清单 YAML：name / base_url / api_key（建议 env:XXX）/ model
python tools/exam_paper.py --paper basic --models my_models.yaml

# 或者直接用项目 .env 里的档案
python tools/exam_paper.py --paper full --profile DEEPSEEK,GLM
```

判据：**基础卷 ≥95% 才建议直接玩；90%~95% 是边缘；<90% 不建议。**
实力卷没有硬门槛，报告按 A（≥90%）/ B（75%~89%）/ C（60%~74%）/ D（<60%）分档；
分数越高，通常越能撑住真实对局。详细说明见根目录 `模型测试分数.md` §6。

## 玩家/部署者自检

| 脚本 | 用途 |
|---|---|
| `review.py --reasoning` | 复盘最近一局（带思维链）；`--list` / `--seat N --day N` |
| `packet_size.py` | 量提示层体量（怀疑"为什么贵/为什么慢"） |
| `test_boards.py` | 板子与技能自检 |
| `test_routing.py` | 座位路由自检（换模型后确认没配错） |
| `voice_check.py` | 语音连通性自检 |
| `meta_odds.py` | 发牌/位置概率枚举（`content/06_概念库.md` 里的数字来源） |
| `verify_isolation.py` | **防全知独立验证器**：对真实 packet 双向核对（泄漏/覆盖缺口），`--mutate` 自证会红；**发版前必跑** |

约定：这些脚本都从 `config.yaml` / `.env` 读配置，**不需要改代码**。
开发用的题库、基准、参数扫描、一次性探针**不在开源仓库**里。

---

## 给维护者

- 新增"玩家会用"的脚本 → 放进本目录并登记到 `build_release.py` 的 `TOOLS_KEEP`；
- 新增"只给开发用"的脚本 → 放本目录但加进 `.gitignore`（不进仓库、不进包）。