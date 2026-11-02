# coding=utf-8
"""一键回归：把整套回归跑一遍，出汇总表。三档速度，覆盖不缩水。

为什么要有它：测试散在 `werewolf/tests/` 下（70+ 个 t_*.py / check_*.py，还在加），
每次都要手动起 8086 测试服务、逐个跑、还要记得哪些需要线上 8077、哪些要真浏览器。
这份清单把这些**依赖关系写死在代码里**，以后一条命令就能确认"没改坏东西"。

用法：
    python run_all.py                 # 全量（默认）：盘上每个脚本都跑，最慢但覆盖最全
    python run_all.py --quick         # 日常回归：跳过真浏览器脚本 + 两局长跑 mock 局
    python run_all.py --fast          # 冒烟：只跑"纯本地、秒级"的（实测 ~25 秒）
    python run_all.py t_rules_new     # 只跑名字里含这段的
    python run_all.py --timeout 600   # 单个脚本超时改成 600 秒（默认 300）

  ⚠️ 三档是**三段各自成立的清单**，不是"层层收窄"：
     · `--fast`  = 纯本地 + 秒级（没有 mock30，也没有"自己在等超时/退避"的那批）
     · `--quick` = **除真浏览器脚本 + 两局长跑局之外的全部**（含 L2-medium 那批几十秒的）
     · 默认      = 盘上有的全跑
     `--fast` ⊂ `--quick` ⊂ 全量；每一档跑完都会打印"跑了 N 项、跳过了谁、按什么判据跳的"。

  ⏱ 耗时（2026-10-30 实测，同一台机器）：`--fast` 82 项 / 48.4 秒；
  `--quick` 99 项 / **140.6 秒**；全量 119 项（2026-10-30 未跑）。
  ⚠️ 「项」是**行数**：119 = 117 个脚本 + `t_ws_e2e` 按 3 个板子多出的 2 行，别拿 82 去减 119。
  旧基准（2026-10-11）：`--fast` 70 / `--quick` 93 / 全量 107 —— 早就不对了，别照抄。
  括号里是那一轮优化前的数：`--quick` 249 秒、全量 310 秒。
  快在两处：① 单房间的服务端原来把四个最慢的脚本串成一条 287 秒的链 —— 现在按
  `TEST_INSTANCES` 拆成 8086 / 8088 / 8089 三台实例（各自 save_dir）并发跑；
  ② 客户端重试之间的退避基数变成可配（`retry_sleep`，生产默认 1.5 秒不变），
  `t_stall_ws.py` 用 0.1 秒 ⇒ 它一项 168 → 41 秒。
  📌 还剩的"内容慢"：`t_adversarial`(101s) / `t_cancel_paths`(84s) 各自要打 5~6 局 mock、
  `本地A` 那 5 项等的是脚本内部写死的超时窗口（98 秒）—— 这些不是 runner 能压的。

  剩下能省的只有固定开销：探活替固定 sleep、服务只起一次、纯检查脚本并行。

分档依据（判据全部写在下面各组注释里，**不靠探测**）：
    L1_PARALLEL_SAFE   纯本地检查，无共用端口 / 无浏览器 / 不写共享目录 → 可并行
                        （含"自己现挑空闲端口起临时实例"的 t_ask_id / t_tts_cached：不占 8086）
    L2_LOCAL_SERIAL    纯本地、单脚本 < 8 秒（要跑引擎整局）→ 串行，避免和别的测试抢机器
    L2_LOCAL_MEDIUM    纯本地，但每个 4~40 秒（脚本自己在等超时/退避/收尾窗口）→ 不算"秒级"
    LONG_LOCAL         纯本地但要几分钟（引擎退避重试 × 整局 / 30 局 mock）
    NEEDS_8086         runner 拉起的那台临时测试实例（--mock-default）
    NEEDS_8077         线上用户服务（必须已经在跑；没跑会明确报错，不是"静默变绿"）
    NEEDS_8087         自己拉起假上游 + 测试服务（t_stall_ws）/ 需要外部假语音实例（t_asr_path）
    BROWSER            真启动 Edge + CDP 的页面测试

约定：
  · 输出汇总：每个脚本的通过/失败 + 慢榜 top5 + 整轮耗时 + 可复制的失败日志路径；
  · 单个脚本一个可配超时（默认 300 秒），超时判失败，并**连子进程树一起清掉**
    （有的脚本自己会起假上游/测试服务/子进程，只杀直接子进程会留下孤儿占端口）；
  · 收尾清理：浏览器 profile / tmp / __pycache__ / 散落的 *.log 一律归位。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:                                      # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠️ 项目在 `default-workspace\werewolf`：第一版写成 `dirname(HERE)`（= default-workspace），
#    于是拉起测试服务时 `can't open file '...\default-workspace\server.py'` —— 所有需要 8086 的
#    测试全报 ConnectionRefused（看起来像 11 个测试坏了，其实是我的 runner 起错了目录）。
WW = os.path.dirname(HERE)          # tests/ 的上一级就是项目根
PY = sys.executable
LOG_DIR = os.path.join(HERE, "_logs")

PORT_TEST = 8086                    # runner 拉起的临时测试实例
PORT_FAKE_LLM = 8098                # 给那台实例用的**本地假上游**（测试档绝不花钱，见下）
PORT_LIVE = 8077                    # 线上用户服务（页面/存档接口测试要它）
PORT_FAKE = 8087                    # t_stall_ws 自己拉起的那台（假上游 + stall-config）
# ⚠️ 为什么 8086 实例必须配一个**本地假上游**（2026-10-10 晚，作者的账单里抓到）：
#    `tests/test-config.yaml` 原来写的是**生产档案**（profile: QWEN / DEEPSEEK / GLM），
#    而 `t_cancel_paths.py` 第四节要验"真模型调用时放弃本局"，发的是 `mock: false`
#    ⇒ 8086 这台测试实例**真的去打用户的付费渠道**（实测一次全量 3 次真实调用）。
#    `--mock-default` 只改默认值，那条显式 `mock: false` 正好绕过它。
#    现在测试配置只指向 8098 上的 `fake_llm.py`：请求照发、字段照回，但**一分钱不花**，
#    而且 fake 的 `/stats` 计数会打印在汇总里（= 这一档到底打了几次上游的可核对证据）。
DEFAULT_TIMEOUT = 300.0             # 单个脚本超时（秒）：防止个别挂住的脚本拖住整轮
# ⚠️ 例外：**自己就要跑几分钟**的脚本给更长上限，否则"正常但慢"会被误判成超时。
#    2026-10-07 实测：t_exam_live_cli.py 平时约 273 秒，机器一忙就撞 300 秒上限
#    （全量回归因此出现唯一一条假红），这不是它挂住，是上限压得太近。
#    2026-10-08 复查（同一条脚本又"真挂死"了一次，PID 31752 静默 484 秒、CPU 累计才
#    0.3 秒）：那 273 秒里**大部分是真联网在等上游** —— 它当时只桩了预检，打局那一路
#    仍在真连方舟。现在打局也打桩了，并且脚本**自带 180 秒内部硬超时**（到点自己判失败、
#    打印挂在哪一步 + 最后一条日志 + 建议查什么，然后退出）。
#    所以 900 秒那条必须收回：900 秒的含义是"整轮被拖 15 分钟"，而这次是真挂死，
#    runner 却一直不动手。420 = 273 秒（老口径的正常耗时）的 1.5 倍 = "正常慢"的安全
#    余量；真挂死时最多拖 7 分钟（内部 180 秒先动手，420 秒是 runner 侧的兜底）。
#    📌 顺带纠正一个说法："run_all 缺少单测试超时"**不成立** —— runner 本来就有：
#    DEFAULT_TIMEOUT（300 秒）+ 下面这张例外表 + 超时 `kill_tree` 连子进程树一起杀
#    （见 run_subprocess / kill_tree）。这次没在 300 秒被砍，是因为它被单独放宽到了 900。
TIMEOUT_OVERRIDE = {
    "t_exam_live_cli.py": 420.0,    # 真调 exam_live 打一局（老口径 ~273s，留 1.5 倍余量；
                                    #   脚本内部还有 180s 硬超时，见上面那段）
    "t_mock30.py": 900.0,           # 30 局 mock
    "t_stall_ws.py": 600.0,         # 自拉假上游 + 整局 WS + 引擎退避
    "t_stall.py": 600.0,            # 真退避序列（3s→8s）
}


def timeout_for(script: str, default: float) -> float:
    """单个脚本的超时：先看例外表，再看命令行 --timeout。"""
    return float(TIMEOUT_OVERRIDE.get(os.path.basename(script), default))

# ══════════════════════════════════════════════════════════════════════════════
#  分档清单 —— 判据写死在组名与注释里，不靠"探测"猜
# ══════════════════════════════════════════════════════════════════════════════

# L1：**纯本地检查**。判据（三条全中才算）：
#   ① 不连任何**共用**端口（不 import httpx/websockets，或只连"自己现挑空闲端口起的那台"）；
#   ② 不开浏览器（不 import cdp_lite）；
#   ③ 不写共享目录（不碰 games/，不写 personas/、.env；临时件只落 tests/tmp/ 自己的子目录）。
#   因为互不干扰，这一组可以**并行**跑（并发度见 PARALLEL_WORKERS）。
L1_PARALLEL_SAFE = (
    "check_call_signatures.py",       # AST 核调用签名
    "check_camp_packet.py",           # 阵营分发（读 content/ 只读）
    "check_deal_seed.py",             # 发牌随机性（进程内起 Room，不落盘）
    "check_d_personas.py",            # 人设校验（临时副本落 tests/tmp_personas/）
    "check_god_section.py",           # 神牌章节分发
    "check_meta_integrity.py",          # 进包段：不许在压缩/改写中丢内容（数字/关键词/阵营）（1s）
    "check_meta_split.py",            # 玄学书分发
    "check_misc_wiring.py",           # 接线小检查
    "check_rules_per_board.py",       # 按板子组装规则
    "check_speech_tasks.py",          # 发言任务落哪个思考档
    "check_settings_store.py",        # 页面设置存储：key 留空不改 / 座位串 / 语音环境变量（0.1s）
    "t_api_tuning.py",                # 「模型参数」覆盖真的进了座位路由（纯本地，0.2s）
    "t_reports.py",                   # 对局统计：只带摘要 / 去重 / 胜率按局算（纯本地，0.1s）
    "t_model_badge.py",               # 右上角模型徽章：白天带模型、夜里/狼任务脱敏（纯本地，0.2s）
    "check_multiplayer.py",           # 联机：多真人座位 / 并发提问路由 / 台账按座位过滤（0.1s）
    "t_ask_id.py",                    # ask 编号协议（**自己现挑空闲端口**起临时实例，不占 8086）
    "t_audit_1030.py",                # 2026-10-30 外部审查那轮修复的回归（纯本地，1s）
    "t_settings_midgame.py",          # 对局进行中保存设置不会把这一局搞崩（纯本地 mock 局，0.3s）
    "t_autosave.py",                  # 途中自动存盘：覆盖同一个文件、in_progress 标记、原子替换（0.3s）
    "check_gates.py",                 # 闸门自查：每个测试脚本都必须"能变红"（假回归门防护，0.1s）
    "t_check_release.py",            # 发布闸门：.env/存档/docs/tests/密钥不许进包（纯本地，0.7s）
    "t_cached_tokens.py",            # 缓存命中记账：各家字段写法 + 没返回记 0（纯本地，0.2s）
    "t_stale_generation.py",         # 代际校验：放弃/重开后迟到的回复与旧局事件必须丢（纯本地，0.3s）
    "t_prompt_discipline.py",        # 进包纪律：消息可信度 + 三条纪律真的到包（mock 局，1s）
    "check_glossary.py",              # 术语库与模型版速查必须对得上（含长度预算，0.2s）
    "check_playbook_coverage.py",      # 默认板子每个角色都要有规则片段+打法章节（0.2s）
    "check_playbook_phases.py",        # 打法书按阶段切之后：core 不许丢、越界不许有（0.3s）
    "check_prefix_cache.py",           # 前缀缓存：确定性 + 静态前缀占比 + 块顺序（1s）
    "check_inline_js.py",              # 内联 JS 语法体检（node --check，0.3s）
    "check_exam_bank.py",              # 考核题库：每个板子都能跑通+零告警（1s）
    "check_exam_hard.py",              # 笔试 2.0 高难度题库（51 道 / 42 项，含变异自证）~3s
    "check_exam_hard2.py",             # 第二批难题（J 局势/Z2 决策/R 规则 22 道 / 52 项，含变异自证）~6s
    "check_exam_hard3.py",             # 第三批 J2 题（真实复盘取材的模糊信息判断 8 道 / 47 项，逐要点正反例）~5s
    "check_j2_run.py",                 # J2 三档实测报告：15 臂齐不齐 + 报告与 raw.json 能不能对上（报告不在时显式 SKIP）
    "t_human_roles_new.py",
    "t_stream_retry.py",              # 流式+静默超时（卡住的流必须抛可重试的错误；本地，~1s）           # 真人玩三张技能民牌（摄梦空过/鸦诅咒进投票/幸运儿三选一）+ 前端面板（本地，~2s）
    "check_role_names.py",             # 改名/新增角色后：自称识别/敏感词/复盘名表都要跟上（0.3s）
    "t_claim_chapter.py",             # 狼「穿身份」参考章与开关（纯本地）
    "t_conflict_scan2.py",            # 全仓 AST 扫描（只读）
    "t_human_timeout.py",             # 真人超时要补发 ask_done（**自己现挑空闲端口**起临时实例）
    "t_lite_api.py",                  # lite 复盘接口（**自己现挑空闲端口**起临时实例，不占 8077/8086）
    "t_experience.py",                # 经验库文件名改版（临时 store 落 tests/tmp/）
    "t_experience_race.py",           # 经验库并发写（临时 store 落 tests/tmp/）
    "t_guard_seed_probe.py",          # 守卫种子探测（纯引擎）
    "t_json_repair.py",               # JSON 温和修复（纯函数）
    "t_mutation_new.py",              # 变异测试（进程内 monkeypatch，不改文件）
    "t_new_code_audit.py",            # 新增代码接线复扫（只读 + 起一个只读子进程）
    "t_night_calls.py",               # 夜间调用清单 / 女巫双药空不再调用 / 死人不再被叫（0.4s）
    "t_playbook.py",                  # 打法书按角色分发
    "t_routing_config.py",            # 配置写错的预检
    "t_say_and_alive.py",             # 配音口径（真人坐狼时夜聊也配）+ 公告后补推 status（0.5s）
    "t_tabletalk.py",                 # 提示层 / 概念库的进包段落（概率参考）
    "t_timeline_tiers.py",            # 发言顺序 + 身份校验进了包
    "t_timeout_defaults.py",          # 手机看到的秒数 == 引擎真正用的上限（两把尺回归，0.3s）
    "t_tool_drift.py",                # 工具与生产的参数不漂移
    "t_tts_cached.py",                # TTS 缓存（**自己现挑空闲端口**起临时实例，不占 8086）
    "t_voice.py",                     # 音色层配置解析（--live 才真合成，默认不联网）
)

# L2：**纯本地**，但要真跑引擎整局（CPU / 磁盘密集）。判据是文件头那条「单脚本 < 8 秒」，
#   组内绝大多数 < 1.5 秒（最慢的 t_exam_live_cli.py 打一整局是 2.3 秒）。
#   不并行：它们会同时抢 CPU、同时写 tests/tmp 下的引擎存档，串行既稳又不慢。
L2_LOCAL_SERIAL = (
    "t_decision_first.py",            # 决策在前、发言在后（打整局 mock）      ~0.2s
    "t_deal_shuffle.py",              # 每局重新洗牌（打整局）                  ~0.4s
    "t_deep_probe.py",                # 换家/重试的缺陷复核                     ~0.2s
    "t_deep_probe2.py",               # 换家/重试的时序复核                     ~0.2s
    "t_edge_order.py",                # 屠边优先 → 狼胜                         ~0.1s
    "t_exam.py",                      # 考核工具链离线自检                      ~1.0s
    "t_exam_empty.py",                # 空回复：会重试 / 记「未答」不画 ❌ / 劈半 attempts 累加
                                      #   + finish=length 标记 + 变异自证（本机假上游，~2s）
                                      #   ⚠️ 守的是 2026-10-13 那三处：空回复不重试、被判成答错、
                                      #     劈半回填写死 empty/attempts ⇒ 档位横向对比被污染
    "t_exam_live_cli.py",             # 考核 CLI 打一整局：2026-10-08 起**两条路都打桩** +
                                      #   出口闸 + 180s 内部硬超时，全程离线、实测 2.3 秒
                                      #   （以前 273 秒且真花钱联网；旧注释「模型桩回空 →
                                      #   引擎重试退避 ≈ 4~5 分钟」已不成立，故从 LONG_LOCAL 移来）
    "t_fallback.py",                  # 换家顶替 + 摘要回传                     ~0.4s
    "t_fallback_conflicts.py",        # 换家与 Recorder/存档不冲突              ~0.6s
    "t_fallback_thinking.py",         # 换手那一手的思考档位（目标档案风格 × 座位档位）+
                                      #   新的 llm.fallback_thinking（不配=现状、配了按目标覆盖）~0.3s
                                      #   ⚠️ 守的是 E58：5 号（MIMO+low）换到 DEEPSEEK 顶班时发的
                                      #     是 ark 的 low（开着思考），作者要能点名改成 off
    "t_heal_state.py",                # 女巫面板 healState                      ~0.1s
    "t_meta_odds.py",                 # 概率表 vs 发牌枚举（成圈口径）           ~1s
    "t_mock_save_isolated.py",        # mock 存档不许进 games/ 根 + prune 先删 mock（30 项，~0.6s）
                                      #   ⚠️ 守的是"t_page_persona / t_page_rules 这类浏览器页面测试
                                      #     在用户那台 8077 上打 mock 局 → 写进生产 games/，
                                      #     把真局挤出 keep_games 窗口"这条链（2026-10-12 修）
    "t_persona.py",                   # 人设扩容（角色卡）复核                  ~0.1s
    "t_rules_new.py",                 # 遗言 + 警长新规则                       ~0.2s
    "t_save_dir_isolated.py",         # Room 自己的 save_dir 必须被尊重（真跑一局，~2s）
                                      #   ⚠️ 守的是"回归别往用户 games/ 写"这条：漏了它，
                                      #     测试局会把真局挤出 keep_games 的保留窗口
    "t_sheriff_order.py",             # 警徽 / 遗言 / 猎人开枪交叉              ~0.2s
    "t_stall_concurrent.py",          # 多座位同时卡住只点一次（进程内 Room）    ~0.7s
    "t_thinking_style.py",            # 思考档位按平台风格                      ~0.3s
    "t_think_budget.py",              # 不许砍在思考中途                        ~3.9s
    "t_tools.py",                     # tools/ 两个脚本的修复                   ~0.2s
    "t_watchdog.py",                  # 「放弃本局」的硬复位兜底                ~4.4s
    "t_window.py",                    # 收尾窗口有多长                          ~6.6s
    "t_wolf_self_kill.py",            # 出刀目标含狼：自刀/刀队友必须真的落地（剧本整局）~0.3s
    "t_sheriff_order_choice.py",      # 警长自选发言方向：顺序/逆序真的照做（剧本整局）~0.6s
    "t_wolf_explode.py",              # 狼人自爆：过半才爆、吞警徽、跳过当天、当晚照常刀人 ~0.4s
    "t_sheriff_positions.py",         # 上警三选一（前置/后置/不上警）与警上发言顺序 ~0.5s
    "t_board_guard.py",               # 新板子 eleven_guard（猎人→守卫）+ 守夜规则执行点 ~1.5s
    "t_dreamer.py",                   # 摄梦人（默认板子的新角色）+ 奶穿口径 ~1.5s
    "t_wolf_king.py",                 # 狼王版（**新默认板子**）：首夜无刀 / 狼王带人 / 结算次序 ~1.5s
    "t_reveal_death.py",              # 新板子：屠城（杀光所有好人才赢）+ 出局亮身份（明牌板）~1s
    "t_roles_v2.py",                  # 守墓人/养鸦人/幸运儿（民牌）+ 愚者改名 + 双层奶穿 ~2s
    "t_review_fixes.py",              # 2026-11-03 审查修复：隔离/清洗/发布规则/题库判分 ~0.5s
)

# L2-medium：**也是纯本地**，但每个要 4~40 秒 —— 因为脚本自己在等"超时 / 退避 / 收尾窗口"，
#   不是机器慢，也**不是 runner 能压掉的固定开销**（压掉就等于把被测的超时行为改了）。
#   判据：实测 ≥ 4s 的纯本地脚本。`--fast` 要的是"秒级"，所以不带它们；
#   `--quick` 和全量都照跑。
L2_LOCAL_MEDIUM = (
    "t_auto_retry.py",                # 引擎自动重试 + 退避（3s→8s）            实测 ~2.5s
    "t_call_timeout_human.py",        # call_timeout 只砍模型座位               实测 ~4.6s
    "t_env_race.py",                  # .env 并发写：40 次写 × 读者在场         实测 ~15s
    "t_env_race2.py",                 # 同上定量版（三种读者 × 40 次写）        实测 ~12s
    "t_exam_exit.py",                 # exam.py 退出码：预检要连一个死端口（等超时）实测 ~26s
    "t_inproc.py",                    # server 收尾窗口：假客户端 aclose 卡 600s 实测 ~21s
    "t_stall.py",                     # 上游波动：真退避序列（MockTransport 挡本地）实测 ~39s
)

# 长跑（纯本地，但要几分钟）—— 判据：实测 > 60 秒，或跑一整局真 WS mock 局。
#   为什么单独一组：`--fast` 要的是"秒级"，`--quick` 要的是"6 分钟内跑完日常回归"，
#   这两条都需要知道谁是真慢的。⚠️ 全量（默认）**照跑不误**。
#   📌 2026-10-08：t_exam_live_cli.py 已移出本组 → L2_LOCAL_SERIAL。它的旧登记注释写的是
#   「真调 exam_live.main() 打一局：模型桩回空 → 引擎自动重试退避（3s→8s）× 整局 ≈ 4~5 分钟，
#   实测最大头」—— 这段现在**不成立**：两条路都打桩 + 出口闸 + 180 秒内部硬超时后，实测 2.3 秒、
#   零真实联网（旧的 273 秒里大部分是真连方舟在等上游，而且真花钱）。
LONG_LOCAL = (
    "t_mock30.py",                    # 30 局 ×2（开/关狼刀控制）纯 mock：CPU 密集
)

# 需要 runner 那台 8086 临时测试实例（--mock-default，临时 save_dir）。
#   判据：脚本里写死 `PORT = 8086` 或打 `127.0.0.1:8086`。
#   这一组**只依赖那台共用的服务**，所以 runner 只要"先探活、确认就绪"就够，
#   脚本自己会复用（`t_human_path.py` 就是显式"8086 已起就复用"的写法）。
NEEDS_8086 = (
    "t_adversarial.py",               # 对抗性复核（畸形载荷 / 并发开局 / 存档往返）
    "t_cancel_paths.py",              # 取消 / 并行路径
    "t_human_path.py",                # 真人坐在 6 号的整条 WS 链路（ask 编号 / 面板键）
    "t_http.py",                      # HTTP 层（体积上限、退避、token 闸门）
    "t_isolation_push.py",            # 信息隔离的推送侧（整局 WS + 存档对账）
    "t_ws_e2e.py",                    # 真 WS 端到端整局（**带板子参数跑三次**，见 BOARD_RUNS）
)

# 需要**线上 8077**（用户的正式服务，必须已经在跑）。
#   判据：脚本里打 `127.0.0.1:8077`。这里既有无浏览器的接口测试，也有页面测试。
NEEDS_8077 = (
    "check_live.py",                  # 线上服务 /api/status
    "check_live_page.py",             # 线上页面 sha256 对盘上 web/index.html
    "check_save_rules.py",            # 存档是不是记了 rules_mode（会往 games/ 写一局）
    "t_save_audio.py",                # 存档留档 + 按序号取语音接口
    "t_server_fixes.py",              # 服务端三处修复（卡住弹窗 / 存档目录 / Host 白名单）
    "t_tts_e2e.py",                   # /api/tts 端到端 + 缓存命中
)

# 需要真浏览器（Edge + CDP，cdp_lite.Page）。判据：import cdp_lite。
#   这一组既慢又不稳（抢 profile、抢 9731~9747 的调试端口），所以 --fast/--quick 都不跑。
BROWSER = (
    "t_bg_invalidate.py",
    "t_blob_reject.py",
    "t_frontend_gaps.py",             # 同时要 8077（线上）与 8086（临时实例）
    "t_frontend_state.py",
    "t_page_asks.py",
    "t_page_checks.py",
    "t_page_diag.py",
    "t_page_persona.py",
    "t_page_review.py",               # 复盘二级页面 / 放弃后看复盘 / 底部不再卡「对局进行中」/ 倒计时
    "t_page_rules.py",
    "t_page_stale.py",                # 过期回调（ask_done 编号 / CLIP 代际守卫）
    "t_page_stall.py",
    "t_voice_queue.py",
)

# 需要 8087：要么自己 `subprocess.Popen` 拉起"假上游 + 测试服务"（t_stall_ws），
#   要么要一台外部假语音实例（t_asr_path，没起就自己 SKIP）。
NEEDS_8087 = (
    "t_asr_path.py",
    "t_stall_ws.py",                  # 自己拉 8099 假上游 + 8087 服务，还要跑完两局（≈3 分钟）
)

# `t_ws_e2e.py` 不是跑一次：它按板子参数跑两遍（原来 runner 里写死的两行）。
#: `t_ws_e2e` 要按**真实存在**的板子各跑一遍（覆盖"换个板子也能跑通"这个意图）。
#: 2026-10-30：产品只剩四张板子（默认狼王版 / 12 人预女猎白 / 9 人屠城 / 6 人屠城明牌），
#: 四张都过一遍 —— 首夜无刀、警长、屠城、出局亮身份都在真实 WS 链路上。
BOARD_RUNS = ("eleven_wolfking", "twelve_standard", "nine_all", "six_all_reveal")

# 并行度：L1 都是秒级进程，4 个足够（再高就是抢 CPU 和磁盘，收益递减）。
PARALLEL_WORKERS = 4


# ══════════════════════════════════════════════════════════════════════════════
#  重活车道（2026-10-08 加）—— 让"在等超时/退避"的脚本彼此重叠，而不是一律排队
# ══════════════════════════════════════════════════════════════════════════════
#  **为什么能并行**：这些脚本慢的原因是**自己在 sleep**（等退避、等收尾窗口、等上游超时），
#  这段时间 CPU 基本是空的。上一轮全量的日志时间窗（`tests/_logs/`）算过：整轮 691.6 秒里，
#  最慢 11 个脚本吃掉 573 秒（83%），而它们几乎不占 CPU。
#  **车道内部仍然串行**（该排队的照旧排队），只有**车道之间**并发。
#  ⚠️ 分档（谁在哪一档跑）与断言**一个字都没改**：车道只决定"什么时候跑"。
#     `--fast` 仍只跑 L1+L2；`--quick` 仍不含浏览器与长跑局；车道成员是从**本档实际要跑的
#     那一串**里挑的，没登记的落进最后那条串行车道。
LANE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 8086 那条链**必须**留给"直接读盘上存档目录"的三个脚本（见 TEST_INSTANCES 的说明）：
    #   它们读/写 `tests/tmp/games`（8086 的 save_dir），换到别的实例上就对不上账了。
    # `t_frontend_gaps.py` 同时要 8077（页面）与 8086（往它的存档目录塞假存档），并进这条链。
    ("8086 存档/页面链", ("t_frontend_gaps.py", "t_isolation_push.py", "t_ws_e2e.py")),
    # 下面两条链各自一台实例（派生配置、各自的 save_dir）：它们只通过 HTTP/WS 说话，
    # 不碰盘上的存档目录，所以可以并行 —— 这是这一轮提速的主要来源（详见 TEST_INSTANCES）。
    ("8088 取消/HTTP 链", ("t_cancel_paths.py", "t_http.py", "t_human_path.py")),
    ("8089 对抗性链", ("t_adversarial.py",)),
    # 抢 Edge profile 与 CDP 调试端口（9731~9747），而且打的是**同一台 8077** →
    # 浏览器组与那 6 个纯接口脚本放**同一条**车道串行，免得两边同时压线上服务
    # （t_page_review 要列存档，而 check_save_rules 会往 games/ 写一局）。
    ("8077（浏览器 + 接口）", tuple(n for n in BROWSER if n != "t_frontend_gaps.py") + NEEDS_8077),
    # ⚠️ 顺序要紧：`t_asr_path` 靠"8087 没人监听"来 SKIP；`t_stall_ws` 一起来它就
    #    会去打那台假上游实例。所以 asr 必须排在前面，且两条**同车道**（不能并行）。
    ("8087 假上游", ("t_asr_path.py", "t_stall_ws.py")),
    # 纯本地的三个"慢等"脚本。⚠️ 前三个（t_inproc / t_window / t_watchdog）都用
    #    `tests/tmp/games` 当存档目录（实测同一路径）→ 必须同车道串行，否则互相清目录。
    ("本地A", ("t_inproc.py", "t_window.py", "t_watchdog.py", "t_exam_exit.py", "t_stall.py")),
    # t_env_race / t_env_race2 都写 `tmp/envrace*/.env`（各写各的子目录，但都叫 .env）
    # → 保守起见同车道串行；其余三个是进程内引擎测试，不占端口、不写共享目录。
    ("本地B", ("t_env_race.py", "t_env_race2.py", "t_call_timeout_human.py",
               "t_auto_retry.py", "t_think_budget.py")),
)

#: 没登记进上面任何车道的脚本落这里（仍然串行，行为与加车道之前一模一样）。
LANE_REST = "其余（串行）"

#: 会发 `mock: false`（= 让服务端真去调模型）的测试脚本。用途只有一个：跑完拿它跟
#: "本地假上游收到几次请求"对账（见汇总里的上游审计）。
#: ⚠️ 新增这类脚本时也要往这里加 —— `check_gates.py` 第四节会拦"没登记的 mock: false"。
#: `t_stall_ws.py` 不在此列：它拉的是**自己那台 8099 假上游**，与测试实例的配置无关。
MOCK_FALSE_SCRIPTS = ("t_cancel_paths.py",)

# ══════════════════════════════════════════════════════════════════════════════
#  测试实例（2026-10-11：从"一台 8086"变成"三台，各自 save_dir"）
# ══════════════════════════════════════════════════════════════════════════════
#  **为什么要多台**：服务端是**单房间**的 —— 一条车道同一时刻只能跑一局。这一档最慢的
#  四个脚本 t_adversarial(101s) / t_cancel_paths(84s) / t_frontend_gaps(54s) / t_http(34s)
#  原来全挤在 8086 上串行 = 287 秒，而机器基本闲着（Edge 那 12 个测试一跑完就空了）。
#  拆成三台之后，最慢的一条车道 287 → 118 秒（整轮 310 → 约 150 秒），
#  **断言一个字都没改**：改的只是"谁和谁同时跑"。
#
#  **怎么分**（关键约束：有三类脚本会**直接读盘上的存档目录**，必须留在 8086）：
#    · `t_isolation_push` / `t_ws_e2e` 读 `tests/tmp/games` 找"最新那一局"；
#    · `t_frontend_gaps` 往 `tests/tmp/games` **写一份假存档**给页面看。
#    所以这三类留在 8086（用 `test-config.yaml` 原样）；另外两组用**派生配置**
#    （`tests/tmp/test-config-<port>.yaml`：从 `test-config.yaml` 现读现改 save_dir/voice_cache_dir，
#      保证"只指向本地假上游"这条性质跟着一起派生过去，不会漏）。
TEST_INSTANCES: tuple[dict, ...] = (
    # 8086：原样用 tests/test-config.yaml（save_dir = tests/tmp/games）
    {"port": 8086, "config": "", "tag": "8086",
     "lane": "8086 存档/页面链"},
    # 8088：取消/HTTP 那一组（各自只用自己的 save_dir）
    {"port": 8088, "config": "", "tag": "8088",
     "lane": "8088 取消/HTTP 链"},
    # 8089：对抗性那一组（最慢的一个，单独一台）
    {"port": 8089, "config": "", "tag": "8089",
     "lane": "8089 对抗性链"},
)


def instance_config(port: int) -> str:
    """给非 8086 的实例生成一份派生配置（只改 save_dir / voice_cache_dir）。

    ⚠️ **从 `test-config.yaml` 现读现改**，不是复制一份静态文件：那份配置里
    "llm 只指向本地假上游"这条性质必须跟着派生过去（否则新实例又会指向真渠道 ⇒ 真花钱）。
    """
    import yaml                                             # noqa: PLC0415
    src = os.path.join(HERE, "test-config.yaml")
    cfg = yaml.safe_load(io.open(src, encoding="utf-8").read()) or {}
    game = cfg.setdefault("game", {})
    tmp = os.path.join(HERE, "tmp")
    os.makedirs(tmp, exist_ok=True)
    game["save_dir"] = os.path.join(tmp, f"games-{port}")
    game["voice_cache_dir"] = os.path.join(tmp, f"voice-{port}")
    dst = os.path.join(tmp, f"test-config-{port}.yaml")
    with io.open(dst, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    return dst


def lane_port(lane: str) -> int | None:
    """这条车道跑在哪台测试实例上（没登记的 = 不用实例）。"""
    for inst in TEST_INSTANCES:
        if inst["lane"] == lane:
            return int(inst["port"])
    return None


def lane_env_problems(lanes: list[tuple[str, list[tuple[str, str | None]]]]) -> list[str]:
    """自查：**车道声明的实例端口，脚本真的读走了吗？**

    ⚠️ 为什么必须有这个（2026-10-11 实测踩到）：拆车道时 `t_adversarial.py` 被分到 8089，
    但它当时**不读** `WW_TEST_PORT` ⇒ 它照样去打 8086，和"8086 存档/页面链"上的
    `t_ws_e2e` / `t_isolation_push` **同时开局**。表现是"B1 并发两次开局只有一个真的开了"
    假红（一条连接 30 秒收到 6 个 `you`）。这种错**不报端口错**、只报断言失败，
    排查起来非常绕 —— 所以在起服务之前就把"谁没读端口"点名出来。
    """
    bad: list[str] = []
    for lane, items in lanes:
        port = lane_port(lane)
        if port is None or port == PORT_TEST:
            continue
        for name, _board in items:
            try:
                src = io.open(os.path.join(HERE, name), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if "WW_TEST_PORT" not in src:
                bad.append(f"{name} 跑在「{lane}」({port}) 上，但它不读 WW_TEST_PORT")
    return bad


def build_lanes(scripts: list[str], boards: tuple[str, ...]
                ) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """把**本档实际要跑的**脚本排进车道。返回 [(车道名, [(脚本, 板子或 None), …]), …]。

    分档语义不变：`scripts` 是这一档已经筛好的串行清单（不含 L1 那批并行的），
    所以 `--fast/--quick` 不会因为车道而多跑或少跑任何一项。
    """
    owner: dict[str, str] = {}
    for lane, names in LANE_RULES:
        for n in names:
            owner.setdefault(n, lane)                    # 先到先得：重复登记时不让两条车道都想要它
    rest: dict[str, list[tuple[str, str | None]]] = {}
    for n in scripts:
        if n == "t_ws_e2e.py":                           # 由 boards 带参数跑几次，见 BOARD_RUNS
            continue
        rest.setdefault(owner.get(n, LANE_REST), []).append((n, None))
    for b in boards:
        suite = owner.get("t_ws_e2e.py", TEST_INSTANCES[0]["lane"])
        rest.setdefault(suite, []).append(("t_ws_e2e.py", b))
    order = [lane for lane, _ in LANE_RULES] + [LANE_REST]
    return [(lane, rest[lane]) for lane in order if rest.get(lane)]


# ══════════════════════════════════════════════════════════════════════════════
#  基础设施
# ══════════════════════════════════════════════════════════════════════════════

def port_open(port: int, timeout: float = 0.4) -> bool:
    """探活一个本地端口（不建长连接，连上就关）。"""
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int, timeout: float = 15.0, interval: float = 0.2) -> float:
    """探活轮询：每 0.2 秒连一次，最多等 timeout 秒。返回等了多久（秒）；-1 表示超时。

    为什么不用 `time.sleep(3)`：固定 sleep 只有两种结局 —— 慢了白等、快了照样挂。
    探活是"一就绪立刻走"，实测把每档的固定等待从 3.0s 压到 0.3~0.5s。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_open(port):
            return time.time() - t0
        time.sleep(interval)
    return -1.0


class TestServer:
    """runner 拉起的一台临时测试实例（临时 save_dir、只跑 mock、不花模型钱）。

    起一次、三个档共用；脚本侧不再各自启动 / 各自 sleep。启动用**探活轮询**（每 0.2 秒连一次，
    最多 15 秒），就绪就立刻走 —— 替代原来的固定 `time.sleep(3)`。
    启动失败会把 err 日志的尾巴直接打出来（而不是让 11 个测试各报一次 ConnectionRefused）。

    ⚠️ 如果探活时发现**这个端口已经有一台在跑**（别的手册/别的 agent 起的，启动命令一样），
    就直接复用、跑完**不动它**（`adopted=True`）—— 抢端口只会把两边都跑坏。

    ⚠️ 2026-10-11：**不止一台**（见 `TEST_INSTANCES`）。服务端是**单房间**的，
    一条车道同时只能有一局；原来四个最慢的脚本（t_adversarial / t_cancel_paths /
    t_frontend_gaps / t_http，合计 ~273 秒）全挤在 8086 上串行，而机器是闲的。
    现在按"171 秒 → 118 秒"重新分到 8086 / 8088 / 8089 三台上（各自 save_dir）。
    """

    def __init__(self, port: int = PORT_TEST, config: str = "", tag: str = "8086") -> None:
        self.port = int(port)
        self.config = config or os.path.join(HERE, "test-config.yaml")
        self.tag = tag
        self.proc: subprocess.Popen | None = None
        self.logs: list[io.TextIOWrapper] = []
        self.wait_s = 0.0
        self.err_tail = ""
        self.adopted = False

    def _err_path(self) -> str:
        return os.path.join(LOG_DIR, f"runall-{self.tag}.err.log")

    def start(self) -> bool:
        if port_open(self.port):
            self.adopted = True                            # 已经有了 → 复用，不抢也不关
            self.wait_s = 0.0
            return True
        os.makedirs(LOG_DIR, exist_ok=True)
        out = open(os.path.join(LOG_DIR, f"runall-{self.tag}.out.log"), "w", encoding="utf-8")
        err = open(self._err_path(), "w", encoding="utf-8")
        self.logs = [out, err]
        self.proc = subprocess.Popen(
            [PY, "-u", "server.py", "--port", str(self.port),
             "--config", self.config, "--mock-default"],
            cwd=WW, stdout=out, stderr=err)
        self.wait_s = wait_port(self.port, timeout=15.0)
        if self.wait_s < 0:
            try:
                err.flush()
                self.err_tail = io.open(self._err_path(),
                                        encoding="utf-8", errors="replace").read()[-800:]
            except OSError:
                pass
            return False
        return True

    def stop(self) -> None:
        if self.proc is not None:                          # 只关自己拉起来的那台
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:                              # noqa: BLE001
                self.proc.kill()
        for fh in self.logs:
            try:
                fh.close()
            except Exception:                              # noqa: BLE001
                pass


class FakeUpstream:
    """给 8086 那台测试实例用的**本地假上游**（`tests/fake_llm.py`）。

    为什么单独一个类（而不是复用 TestServer）：它不吃 `--config`，命令行也不同
    （`--port / --fail-first`），生命周期还跟着测试实例走 —— 放在一起会互相污染参数。
    跑完**不关**别人已经在跑的那台（`adopted`），与 TestServer 同一套礼貌。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.logs: list[io.TextIOWrapper] = []
        self.adopted = False

    def start(self) -> bool:
        if port_open(PORT_FAKE_LLM):
            self.adopted = True                            # 已经有一台 → 复用，不动它
            return True
        os.makedirs(LOG_DIR, exist_ok=True)
        out = open(os.path.join(LOG_DIR, "runall-fake-llm.out.log"), "w", encoding="utf-8")
        self.logs = [out]
        # `--fail-first 0`：默认每个请求都成功。需要造抖动的脚本（t_stall_ws 用的是
        # 它自己那台 8099）会自己 POST /reset 改这个值，两边互不干扰。
        self.proc = subprocess.Popen(
            [PY, "-u", "fake_llm.py", "--port", str(PORT_FAKE_LLM), "--fail-first", "0"],
            cwd=HERE, stdout=out, stderr=subprocess.STDOUT)
        return wait_port(PORT_FAKE_LLM, timeout=15.0) >= 0

    def stats(self) -> dict:
        """读假上游的计数器（= "这一档到底打了几次上游"的可核对证据）。"""
        import json as _json                                # noqa: PLC0415
        import urllib.request                               # noqa: PLC0415
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT_FAKE_LLM}/stats", timeout=3) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception:                                   # noqa: BLE001
            return {}

    def stop(self) -> None:
        if self.proc is not None:                           # 只关自己拉起来的那台
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:                               # noqa: BLE001
                self.proc.kill()
        for fh in self.logs:
            try:
                fh.close()
            except Exception:                               # noqa: BLE001
                pass


def summarize(text: str) -> tuple[str, int]:
    """从输出里抠出'通过 X，失败 Y'那一行。"""
    m = re.findall(r"共\s*(\d+)\s*项(?:检查)?：通过\s*(\d+)，失败\s*(\d+)", text)
    if m:
        total, ok, bad = (int(x) for x in m[-1])
        return f"{ok}/{total}", bad
    if "✅" in text:
        return "✅", 0
    if "无异常" in text or "全部跑完" in text:
        return "✅", 0
    return "—", 0


def log_path(mode: str, name: str) -> str:
    """每个模式一份日志，互不覆盖；路径可直接复制。"""
    safe = re.sub(r"[^\w.\-]+", "_", name)
    return os.path.join(LOG_DIR, f"runall-{mode}-{safe}.log")


def kill_tree(proc: subprocess.Popen) -> str:
    """杀掉子进程**和它拉起的整棵树**。返回用的是哪种办法（写进日志，便于事后判断）。

    为什么不能只 `proc.kill()`：有的脚本自己会 `subprocess` 起东西（`t_exam_exit.py` 调
    `tools/exam.py`、`t_stall_ws.py` 拉假上游 + 测试服务、`t_new_code_audit.py` 起只读子进程）。
    只杀直接子进程，孙进程会变成孤儿继续占着端口 / 文件句柄 —— 表现就是"明明判了超时，
    下一项还是连不上 8086"。所以 Windows 上用 `taskkill /T /F` 连树杀，失败再退回逐级 kill。
    """
    if proc.poll() is not None:
        return "已自行退出"
    if os.name == "nt":
        try:
            r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return f"taskkill /T /F（PID {proc.pid} 整棵树）"
        except Exception:                                  # noqa: BLE001
            pass
    try:
        proc.terminate()
        proc.wait(timeout=8)
        return f"terminate（PID {proc.pid}）"
    except Exception:                                      # noqa: BLE001
        pass
    try:
        proc.kill()
        proc.wait(timeout=8)
        return f"kill（PID {proc.pid}）"
    except Exception:                                      # noqa: BLE001
        return "杀不掉（进程可能已经僵死，请手工检查）"


def _read_console(path: str) -> str:
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def run_subprocess(argv: list[str], timeout: float,
                   env: dict | None = None) -> tuple[str, int | str, bool, str]:
    """带超时跑一个子进程。返回（输出, 退出码或 TIMEOUT, 是否超时, 杀进程说明）。

    不用 `subprocess.run(timeout=)` 的原因：那条路在超时时会抛异常，**拿不到 pid**，
    所以没法连子进程树一起杀（见 kill_tree 的说明）。
    输出走临时文件而不是管道：子进程如果又拉孙子进程，管道会一直不 EOF（"卡住"的常见真因），
    临时文件没这个问题，而且超时后能读到它临死前已经吐出来的内容。

    `env` 是**附加**的环境变量（如 `WW_TEST_PORT=8088`）：合并进当前环境，不覆盖其他键。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix="runall-out-", suffix=".log", dir=LOG_DIR)
    os.close(fd)
    # Windows 上开新进程组：不给它自己处理 Ctrl+C 的机会，顺手也让 taskkill /T 更好使
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    if env:
        kwargs["env"] = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
    fatal = ""
    try:
        with open(out_path, "wb") as fh:
            try:
                proc = subprocess.Popen(argv, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, **kwargs)
            except OSError as exc:
                fatal = f"[runner] 起不来：{exc}\n"
                return fatal, "ERROR", False, ""
            try:
                rc: int | str = proc.wait(timeout=timeout)
                return _read_console(out_path), rc, False, ""
            except subprocess.TimeoutExpired:
                how = kill_tree(proc)
                tail = _read_console(out_path)
                msg = (f"\n\n[runner] ⏱ 超过 {timeout:.0f} 秒仍未结束 → 判失败。\n"
                       f"[runner] 已清理：{how}\n")
                return tail + msg, "TIMEOUT", True, how
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def _finish(label: str, out: str, rc: int | str, dt: float, mode: str,
            timed_out: bool, killed: str = "") -> dict:
    got, bad = summarize(out)
    path = log_path(mode, label)
    os.makedirs(LOG_DIR, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return {"name": label, "got": got, "bad": bad, "rc": rc, "dt": dt,
            "out": out, "log": path, "timeout": timed_out, "killed": killed}


def run_one(name: str, mode: str, timeout: float, board: str | None = None,
            env: dict | None = None) -> dict:
    """跑一个脚本，输出收进内存（并行时保证汇总不乱），落盘一份日志。返回结果 dict。

    `board` 只有 `t_ws_e2e.py` 用（它必须带板子参数跑，共 3 个板子，见 `BOARD_RUNS`）。
    `env` 是附加环境变量：车道用 `WW_TEST_PORT` 告诉脚本"你跑在哪台测试实例上"
    （见 TEST_INSTANCES；不写就是脚本自己的默认值 8086）。
    """
    label = f"t_ws_e2e({board})" if board else name
    argv = [PY, "-u", os.path.join(HERE, name)] + ([board] if board else [])
    t0 = time.time()
    # 「正常但慢」的脚本给更长上限（见 TIMEOUT_OVERRIDE），别把慢判成挂
    out, rc, timed_out, killed = run_subprocess(argv, timeout_for(name, timeout), env=env)
    dt = time.time() - t0
    return _finish(label, out, rc, dt, mode, timed_out, killed)


# ══════════════════════════════════════════════════════════════════════════════
#  分档
# ══════════════════════════════════════════════════════════════════════════════

def all_scripts() -> list[str]:
    """全部回归脚本（t_ws_e2e 由 BOARD_RUNS 带参数跑，所以从主列表里摘掉）。"""
    scripts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "t_*.py")))
    scripts += [os.path.basename(p) for p in glob.glob(os.path.join(HERE, "check_*.py"))]
    return [s for s in scripts if s != "t_ws_e2e.py"]


#: `--fast` = 纯本地 + 秒级：L1（并行检查）+ L2（< 8 秒的本地脚本）
FAST = L1_PARALLEL_SAFE + L2_LOCAL_SERIAL
#: `--quick` = **除真浏览器 + 长跑局 + 需要线上 8077 之外的全部**
#:   （含 L2-medium 那批"纯本地但每个几十秒"的）。
#: ⚠️ 2026-10-25 修：以前 `NEEDS_8077` 这一组会被 `--quick` 带上 —— 没先起线上服务时它们**硬红**
#:    （`check_live.py` / `check_live_page.py` / `t_tts_e2e.py` / `t_save_audio.py` 的第 4 节），
#:    看起来像代码坏了，实际只是环境没起。要打线上服务的，走全量。
QUICK = tuple(s for s in all_scripts() + ["t_ws_e2e.py"]
              if s not in BROWSER and s not in LONG_LOCAL and s not in NEEDS_8077)


def tier_for(args) -> str:
    if args.fast:
        return "fast"
    if args.quick:
        return "quick"
    return "full"


ALL_GROUPS = (L1_PARALLEL_SAFE, L2_LOCAL_SERIAL, L2_LOCAL_MEDIUM, LONG_LOCAL, NEEDS_8086,
              NEEDS_8077, BROWSER, NEEDS_8087)
#: 分类标签 → "为什么这一档不带它"的说明（汇总里按这个分组打印）
WHY = {
    "L1": "纯本地检查（本档带它）",
    "L2": "纯本地秒级（本档带它）",
    "MEDIUM": "纯本地但要 4~40 秒（脚本自己在等超时/退避/收尾窗口，压不掉）",
    "LONG": "长跑局（纯本地但要几分钟）",
    "8086": "要 runner 拉起的 8086 临时测试实例",
    "8077": "要线上 8077 用户服务",
    "BROWSER": "真浏览器（Edge + CDP）",
    "8087": "要 8087（自己拉起假上游 / 或要外部假语音实例）",
    "UNCLASSIFIED": "未分类：没登记分档（⚠️ 请补进 run_all.py 的清单）",
}


def classify(name: str) -> str:
    """给一个脚本贴上分档标签（用"在哪一组里"来表示分类）。

    ⚠️ 这是给**没登记的**脚本兜底用的：新写的测试如果忘了登记，`--fast/--quick` 会把它
    当成"未分类"跳掉并**大声点名**，而全量（默认）仍然照跑 —— 宁可多说一句，
    也绝不能让新测试在没人注意的情况下从回归里消失。
    """
    for group, tag in ((L1_PARALLEL_SAFE, "L1"), (L2_LOCAL_SERIAL, "L2"),
                       (L2_LOCAL_MEDIUM, "MEDIUM"), (LONG_LOCAL, "LONG"),
                       (NEEDS_8086, "8086"), (NEEDS_8077, "8077"),
                       (BROWSER, "BROWSER"), (NEEDS_8087, "8087")):
        if name in group:
            return tag
    if name == "t_ws_e2e.py":
        return "8086"
    return "UNCLASSIFIED"


def audit(everything: list[str]) -> list[str]:
    """自查：登记的清单和目录里实际有的脚本对不对得上。返回一串要提醒的话。"""
    notes: list[str] = []
    on_disk = set(everything) | {"t_ws_e2e.py"}
    unknown = sorted(n for n in on_disk if classify(n) == "UNCLASSIFIED")
    if unknown:
        notes.append(f"⚠️ 有 {len(unknown)} 个脚本没登记分档（全量照跑；--fast/--quick 会跳过，"
                     f"请把它们补进 run_all.py 的清单）：{'、'.join(unknown)}")
    grouped: dict[str, list[str]] = {}
    for g in ALL_GROUPS:
        for n in g:
            grouped.setdefault(n, []).append(classify(n))
    dup = {n: tags for n, tags in grouped.items() if len(tags) > 1}
    if dup:
        notes.append("⚠️ 这些脚本被登记进了多组（分档会以先命中的那组为准）："
                     + "、".join(f"{n}={tags}" for n, tags in sorted(dup.items())))
    gone = sorted(n for n in set().union(*[set(g) for g in ALL_GROUPS]) if n not in on_disk)
    if gone:
        notes.append(f"⚠️ 清单里有 {len(gone)} 个脚本在目录里找不到（可能是改名/删了，"
                     f"请同步清单）：{'、'.join(gone)}")
    return notes


def select(args, everything: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """返回（要跑的脚本，跳过的 [(脚本, 原因)]）。原因分类就是"为什么这一档不带它"。"""
    tier = tier_for(args)
    if tier == "fast":
        keep, why = tuple(FAST), "纯本地 + 秒级之外的一律不跑（--fast 判据）"
        want = set(FAST)
    elif tier == "quick":
        keep, why = tuple(QUICK), None
        want = set(QUICK)
    else:
        # 全量：**盘上有的全跑**（不限于清单）—— 新测试不会因为没人登记就漏跑
        keep, why = tuple(sorted((set(everything) | {"t_ws_e2e.py"}))), None
        want = set(everything) | {"t_ws_e2e.py"}

    skipped: list[tuple[str, str]] = []
    if why is None:
        # quick / full：逐项查"为什么没带它"，这样输出的跳过原因永远和分档清单一致
        for s in sorted((set(everything) | {"t_ws_e2e.py"}) - want):
            skipped.append((s, WHY.get(classify(s), "不在本档清单里")))
    else:
        # fast：一句话说清判据；具体是谁被跳掉由上面的汇总统一列
        for s in sorted((set(everything) | {"t_ws_e2e.py"}) - want):
            tag = classify(s)
            skipped.append((s, f"{WHY.get(tag, tag)} → --fast 不带"))

    keep = [s for s in keep if s in (set(everything) | {"t_ws_e2e.py"})]
    if args.filter:
        keep = [s for s in keep if args.filter in s]
    return list(keep), skipped


def verdict(r: dict) -> str:
    return "OK " if (r["rc"] == 0 and r["bad"] == 0) else "FAIL"


# ══════════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="", help="只跑文件名含这段的")
    ap.add_argument("--quick", action="store_true",
                    help="日常回归：跳过真浏览器脚本 + 两局长跑局（纯本地那批几十秒的照跑）")
    ap.add_argument("--fast", action="store_true",
                    help="冒烟：只跑纯本地、秒级的脚本（实测 ~25 秒）")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"单个脚本超时秒数（默认 {DEFAULT_TIMEOUT:.0f}）；超时判失败并打印")
    args = ap.parse_args()

    everything = all_scripts()
    #: 全量的**总行数**：`all_scripts()` 已经把 t_ws_e2e 摘掉了（见它的 docstring），
    #: 它的两次跑由 BOARD_RUNS 提供 → 直接相加。⚠️ 这里以前写成 `- 1`，注释却写着「所以 +1」
    #: （自相矛盾），于是全量汇总一直显示「90/89」这种分母比分子小的怪数（2026-10-09 修）。
    total_all = len(everything) + len(BOARD_RUNS)
    mode = tier_for(args)
    scripts, skipped = select(args, everything)
    plain = [s for s in scripts if s != "t_ws_e2e.py"]
    want_ws = ("t_ws_e2e.py" in scripts)
    if want_ws and args.filter and args.filter not in "t_ws_e2e.py":
        want_ws = False
    boards = BOARD_RUNS if want_ws else ()
    n_rows = len(plain) + len(boards)

    banner = {"fast": "冒烟（纯本地 + 秒级）", "quick": "日常回归（跳过真浏览器 + 长跑局）",
              "full": "全量"}[mode]
    print("=" * 84)
    print(f"狼人杀回归 · {banner}　本档 {n_rows}/{total_all} 项　单个超时 {args.timeout:.0f}s")
    print("=" * 84)
    for note in audit(everything):                     # 清单 vs 目录的自查（新测试不会静默漏跑）
        print(note)
    if skipped and len(skipped) < len(everything):
        print(f"（本档跑 {n_rows} 项；跳过的 {len(skipped)} 项清单在最后统一列出）")
    print()

    # ---- 依赖探活：只探当前这一档真的需要的端口，不为用不上的服务白等 ----
    #（测试实例起哪几台由下面的 `needed_ports` 决定 —— 按车道现算，见 TEST_INSTANCES）
    need_77 = any(s in NEEDS_8077 or s in BROWSER for s in plain)
    live77 = port_open(PORT_LIVE)
    print(f"线上 {PORT_LIVE}：{'在线' if live77 else '**没开**'}"
          f"{'（本档有脚本要用它）' if need_77 else '（本档不用它）'}")
    if need_77 and not live77:
        print(f"  ⚠️ 本档需要线上 {PORT_LIVE}，但它没开 → 相关脚本会失败（先起服务再跑）")
    if port_open(PORT_FAKE):
        print(f"假语音实例 {PORT_FAKE}：在线（t_asr_path 会真跑）")
    else:
        print(f"假语音实例 {PORT_FAKE}：没开（t_asr_path 自己 SKIP；t_stall_ws 会自己拉起一套）")

    t_all = time.time()
    rows: list[dict] = []
    fake = FakeUpstream()
    fake_stats: dict = {}
    #: 本档要用哪些测试实例：**先按车道算出来**，再只起这些（`--fast` 一个都不起）。
    par_preview = [s for s in plain if s in L1_PARALLEL_SAFE]
    serial_preview = [s for s in plain if s not in par_preview]
    lanes_preview = build_lanes(serial_preview, boards)
    needed_ports: list[int] = []
    for lane, _items in lanes_preview:
        p = lane_port(lane)
        if p is not None and p not in needed_ports:
            needed_ports.append(p)
    srvs: list[TestServer] = []
    # 车道端口自查：谁被分到了非 8086 的实例上、却没读 `WW_TEST_PORT`（会静默挤回 8086）
    env_bad = lane_env_problems(lanes_preview)
    if env_bad:
        print("⚠️ 车道端口自查：下面这些脚本会**悄悄跑回 8086**，和那条链上的脚本抢同一个房间：")
        for line in env_bad:
            print(f"   · {line}")
        print("   （修法：文件里写 `PORT = int(os.environ.get(\"WW_TEST_PORT\") or 8086)`）")
        print()
    if needed_ports:
        if fake.start():
            print(f"假上游 {PORT_FAKE_LLM}：{'**已经在跑 → 复用**' if fake.adopted else '已就绪'}"
                  f"（测试实例的模型都打它 ⇒ 这一档不会花你的钱）")
        else:
            print(f"假上游 {PORT_FAKE_LLM}：**15 秒内没起来** → 非 mock 的脚本会连不上"
                  f"（不会花钱，但那一项会失败；fake_llm.py 要 fastapi/uvicorn）")
    for inst in TEST_INSTANCES:
        port = int(inst["port"])
        if port not in needed_ports:
            continue
        # 8086 用现成那份；其余端口**现生成**派生配置（各自 save_dir，见 instance_config）
        cfg_path = inst["config"] or (os.path.join(HERE, "test-config.yaml") if port == PORT_TEST
                                      else instance_config(port))
        srv = TestServer(port, cfg_path, inst["tag"])
        srvs.append(srv)
        if srv.start() and srv.adopted:
            print(f"测试服务 {port}：**已经在跑 → 直接复用**（跑完不动它）")
        elif srv.proc is not None and srv.wait_s >= 0:
            print(f"测试服务 {port}：已就绪（探活等了 {srv.wait_s:.1f}s，PID {srv.proc.pid}）")
        else:
            print(f"测试服务 {port}：**15 秒内没起来** → 需要它的脚本会失败")
            if srv.err_tail:
                print("  ── 启动 stderr 尾巴 ──")
                for line in srv.err_tail.strip().splitlines()[-8:]:
                    print(f"  | {line}")
    if not needed_ports:
        print(f"测试服务 {PORT_TEST}：本档不需要 → 不起（省掉一次启动 + 固定等待）")
    print()

    # 失败脚本的日志路径要能直接复制 → 一律给绝对路径
    try:
        # ── 阶段 A：纯本地检查并行跑（输出先收进内存，最后按固定顺序打印）──
        #    判据：这一组不连端口、不开浏览器、不写共享目录（见 L1_PARALLEL_SAFE 注释），
        #    所以彼此之间、以及和后面的串行脚本之间都不会抢资源。
        par = [s for s in plain if s in L1_PARALLEL_SAFE]
        serial = [s for s in plain if s not in par]

        results: dict[str, dict] = {}
        if par:
            print(f"[A] 并行 {len(par)} 个纯本地检查（并发度 {PARALLEL_WORKERS}，"
                  f"输出按下面固定顺序打印）…")
            spawn_lock = threading.Lock()
            last = [0.0]

            def _guarded(name: str) -> dict:
                # 子进程错峰启动：一次别把 4 个 python 同时怼到磁盘/CPU 上
                with spawn_lock:
                    gap = 0.2 - (time.time() - last[0])
                    if gap > 0:
                        time.sleep(gap)
                    last[0] = time.time()
                return run_one(name, mode, args.timeout)

            with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
                for name, res in zip(par, pool.map(_guarded, par)):
                    results[name] = res
            for name in par:                                  # 固定顺序打印，汇总不乱
                r = results[name]
                rows.append(r)
                print(f"  [{verdict(r)}] {name:<28} {r['got']:>8}　{r['rc']:>7}　{r['dt']:>6.1f}s")
            print()

        # ── 阶段 B：其余脚本**按车道并发**（车道内串行、车道之间并行，判据见 LANE_RULES）──
        #    加车道之前这里是"一个 for 循环全串行"：那些自己在等超时/退避的脚本会把它
        #    排成一条长队（上一轮全量整轮 691.6 秒，其中 83% 是这条队里 11 个脚本在 sleep）。
        #    断言、分档、日志格式都没变；变的只有"谁和谁同时跑"。
        lanes = build_lanes(serial, boards)
        if lanes:
            n_items = sum(len(v) for _, v in lanes)
            print(f"[B] {n_items} 项，按 {len(lanes)} 条车道并发（车道内串行，车道之间并行）：")
            for lane, items in lanes:
                print(f"      · {lane}（{len(items)} 项）")
            done: list[dict] = []
            done_lock = threading.Lock()

            def _run_lane(items: list[tuple[str, str | None]], lane: str) -> None:
                port = lane_port(lane)
                env = {"WW_TEST_PORT": port} if port else None
                local: list[dict] = []
                for name, board in items:
                    local.append(run_one(name, mode, args.timeout, board=board, env=env))
                with done_lock:
                    done.extend(local)
                    spent = sum(r["dt"] for r in local)
                    print(f"      ✅ 车道「{lane}」跑完 {len(local)} 项（合计 {spent:.1f}s）")

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
                list(pool.map(lambda kv: _run_lane(kv[1], kv[0]), lanes))
            # 结果**按车道的定义顺序**打印（车道之间是并发的，日志顺序不能跟着完成顺序跑，
            # 否则两次跑的日志没法逐行对照）
            got_by_key = {(r["name"]): r for r in done}
            for _lane, items in lanes:
                for name, board in items:
                    key = f"t_ws_e2e({board})" if board else name
                    r = got_by_key[key]
                    rows.append(r)
                    print(f"  [{verdict(r)}] {r['name']:<28} {r['got']:>8}　{r['rc']:>7}　{r['dt']:>6.1f}s")
                    if r["timeout"]:
                        print(f"        ⏱ 超时 {args.timeout:.0f}s → 已判失败，详情见 {r['log']}")
    finally:
        for srv in srvs:
            srv.stop()
        # 假上游的计数要在**关掉之前**读：它就是"这一档一共打了几次上游、有没有花钱"的证据。
        fake_stats = fake.stats()
        fake.stop()

    # 收尾清理：浏览器 profile 一次几百 MB；临时目录与 __pycache__ 也都是可再生的。
    #   漏在 tests/ 根上的 *.log 一律挪进 _logs/，保持目录干净。
    for d in glob.glob(os.path.join(HERE, "_profiles", "edge-*")):
        shutil.rmtree(d, ignore_errors=True)
    for name in ("tmp", "tmp_personas", "a3_exp_tmp", "a3_personas_tmp", "a4_mut",
                 "voice_seats"):
        shutil.rmtree(os.path.join(HERE, name), ignore_errors=True)
    for d in glob.glob(os.path.join(HERE, "**", "__pycache__"), recursive=True):
        shutil.rmtree(d, ignore_errors=True)
    for f in glob.glob(os.path.join(HERE, "*.log")):
        try:
            shutil.move(f, os.path.join(LOG_DIR, os.path.basename(f)))
        except OSError:
            pass

    # ── 汇总 ──
    elapsed = time.time() - t_all
    failed = [r for r in rows if r["rc"] != 0 or r["bad"]]
    print("\n" + "=" * 84)
    print(f"共 {len(rows)} 项（本档 {len(rows)}/{total_all}）：通过 {len(rows) - len(failed)}，"
          f"失败 {len(failed)}　整轮耗时 {elapsed:.1f}s（{elapsed / 60:.1f} 分钟）")

    slow = sorted(rows, key=lambda r: -r["dt"])
    if slow:
        print("\n最慢 5 项：")
        for r in slow[:5]:
            print(f"  {r['dt']:>7.1f}s  {r['name']}"
                  f"{'　⏱ 撞到超时上限' if r['timeout'] else ''}")
    # 花钱审计：这一档所有"非 mock 的模型调用"都该落在本地假上游上。
    # ⚠️ 这里要**交叉验证**，不能只看假上游起没起来：8086 也可能是**别处起的旧实例**
    #    （runner 会礼貌复用），那台用的是它启动时那份配置 —— 万一那份指向真渠道，
    #    本档里发 `mock: false` 的脚本就会真的花钱（这正是 2026-10-10 那次账单事故的形态）。
    if needed_ports:
        real_callers = [s for s in plain if s in MOCK_FALSE_SCRIPTS]
        got = int((fake_stats or {}).get("requests") or 0)
        if got:
            print(f"\n上游审计：本地假上游 {PORT_FAKE_LLM} 收到 {got} 次请求"
                  f"（成功 {fake_stats.get('ok', 0)} / 失败 {fake_stats.get('failed', 0)}）"
                  f" ⇒ 这一档的模型调用**全部打在本机**，没有花你的钱。")
        elif real_callers:
            print(f"\n⚠️ 上游审计：本档有会发 `mock: false` 的脚本（{'、'.join(real_callers)}），"
                  f"但本地假上游 {PORT_FAKE_LLM} **一次请求都没收到** ——"
                  f"说明测试实例用的不是本 runner 的测试配置（多半是别处起的旧实例）。"
                  f"\n   如果那台指向真渠道，这一项就会**真的花钱**：先把它杀掉再跑"
                  f"（`netstat -ano | findstr :808`）。")
        else:
            print(f"\n上游审计：本地假上游 {PORT_FAKE_LLM} 收到 0 次请求"
                  f"（本档没有非 mock 的模型调用，符合预期）。")
    # ⚠️ 预警要按**每个脚本各自的上限**算：长跑脚本走 TIMEOUT_OVERRIDE（如 t_exam_live_cli
    #    是 420s），以前这里统一用 --timeout（默认 300s）→ 它 756 秒跑完还报"离 300s 不足 20%"，
    #    文案与判定不一致（2026-10-07 自查发现）。
    #    📌 仍未修：真正超时那条提示（上面 `⏱ 超时 Ns`）用的还是 args.timeout，见待完善清单 E15。
    base = args.timeout if args.timeout > 0 else DEFAULT_TIMEOUT
    near = [r for r in rows
            if not r["timeout"] and r["dt"] >= timeout_for(r["name"], base) * 0.8]
    if near:
        print("  ⚠️ 离各自超时上限不足 20% 的："
              + "、".join(f"{r['name']}（{r['dt']:.0f}s / 上限 "
                          f"{timeout_for(r['name'], base):.0f}s）" for r in near))

    if skipped:
        print(f"\n跳过了 {len(skipped)} 项（本档没跑的），按判据分组：")
        buckets: dict[str, list[str]] = {}
        for name, why in skipped:
            buckets.setdefault(why, []).append(name)
        for why, names in buckets.items():
            print(f"  · {why}（{len(names)} 项）：{'、'.join(names)}")
        print("  （三档的判据都写在 run_all.py 顶部的分档清单注释里）")

    if failed:
        print(f"\n失败 {len(failed)} 项 —— 日志路径可直接复制：")
        for r in failed:
            extra = "（超时）" if r["timeout"] else ""
            print(f"  ❌ {r['name']}（{r['got']}，exit={r['rc']}）{extra}")
            if r.get("killed"):
                print(f"     ⤷ 超时清理：{r['killed']}")
            print(f"     {r['log']}")
    else:
        print(f"\n本档全部通过 ✅（跑 {len(rows)} 项，跳过 {len(skipped)} 项）")
    print("=" * 84)
    return 1 if failed else 0


raise SystemExit(main())
