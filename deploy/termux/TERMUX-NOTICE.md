# 随包附带的 Termux（官方 APK，未修改）

手机端 zip 里附带的 `TERMUX-APK/com.termux_1002.apk` 是 **F-Droid 官方构建的 Termux 0.118.3**，
不是我们改过的版本，也不是 Termux 官方对我们项目的背书。

- 包名：`com.termux`
- 版本：`0.118.3`（versionCode `1002`）
- 来源：<https://f-droid.org/repo/com.termux_1002.apk>
- 项目主页 / 源码：<https://github.com/termux/termux-app>
- SHA-256：`E6265A57EB5CA363808488E3B01955958BED93BC0C8A0D281849B363B11027EC`
- 许可：**GPLv3 only**（见同目录 `TERMUX-LICENSE.txt` 与 `GPL-3.0.txt`）

## 为什么附带

安卓用户不用先去 F-Droid 找 Termux，少一步。安装顺序：

1. 先装 `TERMUX-APK/com.termux_1002.apk`（系统会提示“未知来源”，允许本次安装）；
2. 再按 `ANDROID-README.md` 解压本项目、在 Termux 里跑 `ANDROID-INSTALL.sh`。

## 注意

- 如果手机里已经装了 **应用商店的旧版 Termux**，它和 F-Droid 版签名不同，可能装不上；
  先卸载旧版（会丢 Termux 里的旧数据），或继续用旧版并自己确认能跑 Python。
- Termux 的软件包（Python、pip 依赖）仍然在首次运行时从网络下载，需要联网。
- 我们只分发**未修改**的官方 APK；如果你要改 Termux 再分发，必须按 GPLv3 提供对应源码、
  构建脚本和许可证，并注意 Termux 名称/图标的使用。
- 本文件与 APK 一起分发，不要删除。