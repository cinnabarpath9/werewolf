# tools/ 里有什么

| 文件 | 干什么 | 什么时候用 |
| --- | --- | --- |
| `exam.py` | 笔试：让模型答题库（规则/检索/注入/算术） | 想横向比模型"懂不懂规则" |
| `exam_bank.py` | 题库生成与自检（含引擎现算的题） | 出题、改规则后复核 |
| `exam_live.py` | 实战考核：真模型打完整局并按指标评分 | 比"谁打得更好" |
| `rules_ab.py` | 规则书投放方式 A/B（room / packet / both / off） | 验证"给不给规则"的影响 |
| `bench_thinking.py` | 思考档位基准（off/low/high 的延迟与 token） | 定思考档、估成本 |
| `compare_models.py` | 多模型对比跑分 | 选模型 |
| `smoke_real.py` | 真模型冒烟：单座位跑几手，看连通/格式/延迟 | 换 key、换平台后先跑它 |
| `test_boards.py` / `test_routing.py` | 板子与座位路由自检 | 改板子/改路由后 |
| `project_audit.py` | **项目自查**：死配置键 / 死函数 / 漏登记的测试 / 临时垃圾（有问题退出码 1） | 定期体检、发版前 |
| `balance_probe.py` | **轮次/平衡探针**：对照实验量「狼的胜率里多少是结构性的、多少是好人乱投票」 | 怀疑板子偏狼、改规则后复核平衡 |
| `check_release.py` | **发布闸门**：产物里不许出现 .env / 配对码 / 联机码 / 存档 / docs / tests / 硬编码 key（有问题退出码 1） | 打包发人之前 |
| `build_release.py` | **组装运行包**：按白名单拷到 `dist/werewolf-<flavor>-<版本>/`（`--flavor desktop|android`、`--zip` 再打 zip），并自动跑 `check_release.py` | 开源发 Release / 发给朋友本地部署 |
| `packet_size.py` | 量提示层体量：提示词 / 打法书各片段 / 概念库 `meta` / 按板子组装的规则书 | 改提示层、怀疑包变大 |
| `meta_odds.py` | 位置概率表的数值复核（概念库 `frag:meta` 里那几张表） | 改概率段前后 |
| `pick_save.py` | 从存档里挑一个够丰富的当题源 | 被 exam_bank 调用 |
| `tools/review.py` | 把存档渲染成人看的复盘 | 想读某一局 |
| `voice_check.py` | 语音链路自检（音色/合成/缓存） | 换语音服务后 |
| `ws_autoplay.py` | 脚本自动打一局手机端链路 | 验前端/服务端联调 |
| `exam_hard.py` / `exam_hard2.py` / `exam_hard3.py` | 难度题生成器（六类 W/X/Y/Z/S/V，答案由引擎现算） | 改规则后重出难题、复核闸门 |
| `exam_retired.py` | 退役机制考核（旧板子 / 旧题型的对照） | 确认「退役之后行为没变」 |
| `exam_merge.py` | 把多份考核报告合并成一张对比表 | 一次跑了好几家模型 |
| `fill_from_txt.py` | 从文本批量灌 `.env` 档案（写前走白名单校验） | 换机器、批量填 key |
| `check_api.py` | 平台连通性与参数形状自查 | 换 key、换中转站 |
| `call_structure.py` | 打印一次真实调用的报文结构（脱敏） | 接新平台、查字段 |
| `test_routing.py` | 座位路由自检（与 `test_boards.py` 同族，单独跑也行） | 改路由后 |
| `legacy/` | **历史探针**（a1_*/a3_* 等一次性诊断） | 只作留证；跑之前先看文件头的说明 |

> ⚠️ **发布包里只带 7 个**（`review` / `packet_size` / `test_boards` / `test_routing` / `voice_check` /
> `build_release` / `check_release` + 本文件）。表里其余（考题、基准、参数扫描）只在源码仓库，
> 发给玩家既没用、也容易被当成「官方跑分工具」。

约定：**要跟生产可比的工具**（`exam.py` / `exam_live.py` / `smoke_real.py`）从 `config.yaml`
读生产参数（思考档 / 温度 / 规则投放），所以「工具跑出来的数」跟真局可比 —— 这条以前漏过，
见 `docs/归档/历史变更日志.md` §26。
⚠️ 但**参数扫描类工具不是复现生产**：`bench_thinking.py`（对比 off/low/high）、
`compare_models.py`（对比各家参数写法）、`rules_ab.py`（对比规则投放方式）都是**自带参数**的一次性
对照实验，`enable_thinking` / `reasoning_effort` / `thinking_budget` 在脚本里成组写死，
**不走 `thinking_style` 那套翻译** —— 拿它们的数据当「真局里的模型表现」会错，
它们的用途只是「同一条件下横向比档位 / 比模型 / 比投放方式」。
（2026-10-07 深夜把这句改准：原文笼统写「工具都从 config.yaml 读生产参数」，与这三条不符。）
