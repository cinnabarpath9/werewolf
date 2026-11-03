# tools/（只放玩家/部署者会用到的脚本）

> 2026-11-04：**开发用工具与题库已从公开仓库撤下**（本机保留、`.gitignore` 忽略）。
> 这里只列"开源仓库里还有"的脚本；考题、基准、参数扫描那一族不在仓库里，也不需要你跑。

## 打包与发布

| 脚本 | 用途 | 什么时候用 |
|---|---|---|
| `build_release.py` | 组装运行包（`--flavor desktop/android --zip`） | 发版 |
| `check_release.py` | 发布包自检：必备文件 / 密钥扫描 / `config.yaml` 能否解析 | 发版前、打包后 |
| `release_rules.py` | 清洗规则（阵容清空 / 兜底档案 / 个人串），打包与镜像共用 | 被上面两个调用 |
| `build_exe.py` | Windows 客户端（PyInstaller onedir）+ itch zip | 发客户端 |
| `build_play_site.py` | 静态试玩站（`dist-play/` + `werewolf-<版本>-在线试玩站.zip`） | 发 Pages/静态托管 |
| `make_narrator.py` | 用 `VOICE_*` 一次性生成 8 条法官播报 mp3 | 换播报台词 |

## 玩家/部署者自检

| 脚本 | 用途 |
|---|---|
| `review.py --reasoning` | 复盘最近一局（带思维链）；`--list` / `--seat N --day N` |
| `packet_size.py` | 量提示层体量（怀疑"为什么贵/为什么慢"） |
| `test_boards.py` | 板子与技能自检 |
| `test_routing.py` | 座位路由自检（换模型后确认没配错） |
| `voice_check.py` | 语音连通性自检 |
| `meta_odds.py` | 发牌/位置概率枚举（`content/06_概念库.md` 里的数字来源） |
| `context_ab.py` | 往日发言压缩的量测（`off` vs `extract` 的字数/token，`--dump` 出两版提示包） |

约定：这些脚本都从 `config.yaml` / `.env` 读配置，**不需要改代码**。
开发用的题库、基准、参数扫描、一次性探针**不在开源仓库**里；要复现作者当时的实验，得在本机开发环境里跑。

---

## 给维护者

- 新增"玩家会用"的脚本 → 放进本目录并登记到 `build_release.py` 的 `TOOLS_KEEP`；
- 新增"只给开发用"的脚本 → 放本目录但加进 `.gitignore`（不进仓库、不进包）。